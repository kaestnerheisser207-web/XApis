import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from api_watch.config import load_config, load_secrets
from api_watch.execution import ProbeContext
from api_watch.manifest import coverage, plan_fingerprint, registry, uncovered_sources
from api_watch.scaffold import initialize
from api_watch.state import Store
from api_watch.suite import run_suite


class SuiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'state')
        self.config = load_config()
        self.config['samples']['test_account_id'] = '123'
        self.config['writes']['enabled'] = True
        self.secrets = {'cookies': '', 'proxy': None, 'webhook': None, 'login': {}}
        self.net = patch('curl_cffi.requests.Session.request', side_effect=AssertionError('network forbidden'))
        self.http = patch('requests.sessions.Session.request', side_effect=AssertionError('network forbidden'))
        self.net.start()
        self.http.start()

    def tearDown(self):
        self.net.stop()
        self.http.stop()
        self.store.close()
        self.temp.cleanup()

    def ctx(self, run_id=None):
        return ProbeContext(self.config, self.secrets, self.store, scenario='writes',
                            auth=object(), actor_id='123', run_id=run_id)

    def test_every_exposed_network_method_registered(self):
        self.assertEqual(uncovered_sources(), [])
        reduced = [row for row in registry() if row['source'] != 'x_apis.x_media_api.XMediaAPI.status']
        self.assertEqual(uncovered_sources(reduced), ['x_apis.x_media_api.XMediaAPI.status'])

    def test_registry_has_unique_ids_and_coverage_missing_credentials_is_explicit(self):
        ids = [row['id'] for row in registry()]
        self.assertEqual(len(ids), len(set(ids)))
        report = coverage(self.config, self.secrets, self.store)
        self.assertFalse(report['all_passed'])
        for row in report['probes']:
            if row['scenario'] == 'lifecycle' and not row['id'].startswith('lifecycle.login.'):
                self.assertIn('cookies', row['missing'])

    def test_read_suite_records_every_item_without_cookie_or_network(self):
        self.config['probes']['guest'] = False
        results = run_suite(self.config, self.secrets, self.store)
        expected = {row['id'] for row in registry() if row['scenario'] == 'reads'}
        self.assertTrue(expected <= {row['check_id'] for row in results})
        self.assertTrue(all(row['status'] != 'ok' for row in results))

    def test_cross_restart_different_mutation_blocked_after_unknown(self):
        first = self.ctx('same')
        action = Mock(side_effect=TimeoutError('secret-cookie'))
        first.step('write.tweet.create', action, mutating=True)
        first.finish()
        second = self.ctx('same')
        other = Mock(return_value={})
        result = second.step('write.follow.create', other, mutating=True)
        self.assertFalse(result.ok)
        other.assert_not_called()
        raw = self.store.db.execute('SELECT result FROM probe_steps').fetchone()[0]
        self.assertNotIn('secret-cookie', raw)

    def test_cross_restart_owned_resource_blocks_new_writes_but_allows_cleanup(self):
        first = self.ctx('same')
        first.step('write.tweet.create', lambda: {}, mutating=True)
        first.save_resource('tweet', '111', marker=first.marker)
        second = self.ctx('same')
        action = Mock(return_value={})
        self.assertFalse(second.step('write.tweet.reply', action, mutating=True).ok)
        action.assert_not_called()
        self.assertTrue(second.step('cleanup.tweet', action, mutating=True, cleanup=True).ok)

    def test_run_id_cannot_change_actor_or_scenario(self):
        self.ctx('same')
        with self.assertRaises(ValueError):
            ProbeContext(self.config, self.secrets, self.store, scenario='writes', actor_id='456', run_id='same')

    def test_changed_secret_and_fixture_invalidate_old_evidence(self):
        first = plan_fingerprint(self.config, self.secrets)
        for key, value in [('chat_pin', '9876'), ('login', {'username': 'changed'}), ('proxy', 'http://proxy.invalid')]:
            self.assertNotEqual(first, plan_fingerprint(self.config, dict(self.secrets, **{key: value})))
        fixture = self.root / 'identity.json'
        fixture.write_text('one')
        secret = dict(self.secrets, identity_file=str(fixture))
        original = plan_fingerprint(self.config, secret)
        fixture.write_text('two')
        self.assertNotEqual(original, plan_fingerprint(self.config, secret))

    def test_private_scaffold_and_explicit_credentials(self):
        root = self.root / 'project'
        initialize(root)
        self.assertTrue((root / 'fixtures/probe.png').is_file())
        self.assertTrue((root / 'fixtures/probe.mp4').is_file())
        account = root / 'secrets/account.json'
        self.assertEqual(account.stat().st_mode & 0o777, 0o600)
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(load_secrets(root / 'monitor.env')['cookies'], '')
        with self.assertRaises(ValueError):
            initialize(root)

    def test_replacing_release_signature_material_invalidates_probe_evidence(self):
        from utils.resource_paths import asset_path
        profile = self.root / 'transaction-profile.json'
        profile.write_text('first-public-profile')
        with patch('utils.resource_paths.asset_path', side_effect=lambda name:
                   profile if name == 'transaction_l1.json' else asset_path(name)):
            first = plan_fingerprint(self.config, self.secrets)
            profile.write_text('second-public-profile')
            self.assertNotEqual(first, plan_fingerprint(self.config, self.secrets))

    def test_input_secrets_never_in_coverage(self):
        secrets = dict(self.secrets, cookies='auth_token=very-secret; ct0=test',
                       chat_pin='PIN-SECRET', login={'username': 'private-person', 'password': 'secret-pass'})
        report = json.dumps(coverage(self.config, secrets))
        for secret in ('very-secret', 'PIN-SECRET', 'private-person', 'secret-pass'):
            self.assertNotIn(secret, report)

    def test_acknowledgement_releases_gate_without_changing_failure_evidence(self):
        from api_watch.recovery import acknowledge
        first = self.ctx('old')
        failed = first.step('write.tweet.create', Mock(side_effect=TimeoutError()), mutating=True)
        first.finish()
        self.store.record(first.results['write.tweet.create'], threshold=1)
        fresh = self.ctx('new')
        self.assertTrue(fresh.unresolved_prior())
        acknowledge(self.store, 'old', 'verified by operator')
        self.assertFalse(fresh.unresolved_prior())
        self.assertEqual(self.store.report()['checks'][0]['status'], 'unconfirmed')

    def test_cleanup_rejects_wrong_account_before_network(self):
        from api_watch.recovery import cleanup
        first = self.ctx('old')
        first.save_resource('tweet', '12345', marker=first.marker)
        first.finish()
        wrong = copy.deepcopy(self.config)
        wrong['samples']['test_account_id'] = '456'
        with patch('api_watch.recovery._authenticate') as authenticate, self.assertRaises(ValueError):
            cleanup(wrong, self.secrets, self.store, 'old')
        authenticate.assert_not_called()

    def test_cleanup_follow_accepts_rest_receipt_and_verifies_restored_state(self):
        from api_watch.recovery import cleanup
        first = self.ctx('old')
        first.save_resource('follow', '202', original_following=False, peer_username='testpeer')
        first.finish()
        def authenticate(ctx):
            ctx.auth, ctx.actor_id = object(), '123'
            return True
        def user(following):
            return {'data': {'user': {'result': {'rest_id': '202', 'legacy': {'following': following}}}}}
        with patch('api_watch.recovery._authenticate', side_effect=authenticate), \
             patch('x_apis.x_api.XAPI.get_user_info', side_effect=[user(True), user(False)]), \
             patch('x_apis.x_write_api.XWriteAPI.unfollow_user', return_value={'id_str': '202', 'following': False}) as unfollow:
            rows = cleanup(self.config, self.secrets, self.store, 'old')
        unfollow.assert_called_once()
        self.assertTrue(rows and all(row['status'] == 'ok' for row in rows))
        self.assertTrue(first.resources('follow')[0]['cleaned'])

    def test_cleanup_wrong_cookie_cannot_reassign_original_run_or_return_success(self):
        from api_watch.recovery import cleanup
        first = self.ctx('old')
        first.save_resource('follow', '202', original_following=False, peer_username='peer')
        first.finish()
        secrets = dict(self.secrets, cookies='auth_token=wrong; ct0=test')
        def transaction(auth, scope, pacer):
            auth._transaction = object()
            return {'check_id': scope + '.transaction', 'status': 'ok', 'summary': 'test', 'details': {}}
        wrong = {'data': {'viewer': {'user_results': {'result': {
            'rest_id': '999', 'core': {'screen_name': 'wronguser'}}}}}}
        with patch('api_watch.suite._prepare_transaction', side_effect=transaction), \
             patch('x_apis.x_api.XAPI.get_viewer', return_value=wrong), \
             patch('x_apis.x_write_api.XWriteAPI.unfollow_user') as unfollow:
            rows = cleanup(self.config, secrets, self.store, 'old')
        unfollow.assert_not_called()
        self.assertTrue(any(row['status'] != 'ok' for row in rows))
        self.assertEqual(self.store.db.execute('SELECT actor_id FROM probe_runs WHERE run_id=?', ('old',)).fetchone()[0], '123')
        self.assertFalse(first.resources('follow')[0]['cleaned'])

    def test_completed_read_journal_pruned_but_unconfirmed_writes_preserved(self):
        first = self.ctx('old')
        first.step('write.tweet.create', Mock(side_effect=TimeoutError()), mutating=True)
        first.finish()
        completed = ProbeContext(self.config, self.secrets, self.store, scenario='reads')
        completed.finish()
        self.store.db.execute('UPDATE probe_runs SET updated_at=0')
        self.store.db.commit()
        self.store.prune(1, now=200000)
        rows = self.store.db.execute('SELECT run_id FROM probe_runs').fetchall()
        self.assertEqual([row[0] for row in rows], ['old'])


if __name__ == '__main__':
    unittest.main()
