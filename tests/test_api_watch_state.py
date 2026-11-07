import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock

from api_watch.config import ConfigError, load_config, load_secrets
from api_watch.runner import flush_alerts, healthy, observe_operations, run, scan_results
from api_watch.state import Store, process_lock


def result(status='contract_error', elapsed=1):
    return {'check_id': 'auth.search', 'status': status, 'summary': status,
            'details': {'duration_ms': elapsed}}


def snapshot(qid='old'):
    return {'complete': True, 'family': 'client-web', 'scope': 'guest',
            'operations': {'Viewer': {'queryId': qid, 'operationType': 'query',
                                      'featureSwitches': [], 'fieldToggles': []}},
            'featureDefaults': {}, 'assets': {}, 'materials': {}}


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_failure_threshold_survives_restart_and_deduplicates_timings(self):
        self.assertIsNone(self.store.record(result(), now=1))
        self.store.close()
        self.store = Store(self.root)
        event = self.store.record(result(elapsed=12), now=2)
        self.assertEqual(event['kind'], 'issue')
        self.assertIsNone(self.store.record(result(elapsed=44), now=3))
        self.assertEqual(len(self.store.pending()), 1)
        self.assertEqual(self.store.record(result('ok'), now=4)['kind'], 'recovered')
        self.assertIsNone(self.store.record(result('ok'), now=5))

    def test_blocked_and_disabled_do_not_resolve_incident(self):
        self.store.record(result(), threshold=1)
        self.assertIsNone(self.store.record(result('blocked')))
        self.assertIsNone(self.store.record(result('disabled')))
        self.assertTrue(self.store.report()['checks'][0]['incident_open'])
        self.assertEqual(self.store.record(result('ok'))['kind'], 'recovered')

    def test_alternating_failures_still_reach_alert_threshold(self):
        self.assertIsNone(self.store.record(result('contract_error')))
        self.assertIsNotNone(self.store.record(result('network_error')))

    def test_current_candidate_not_pruned_after_reobservation(self):
        fingerprint = self.store.save_snapshot('guest/client-web', snapshot(), now=0)
        self.store.save_snapshot('guest/client-web', snapshot(), now=200000)
        self.store.prune(1, now=200001)
        self.store.accept_baseline('guest/client-web', fingerprint)

    def test_resource_incidents_are_isolated_across_frontend_families(self):
        old = dict(result('changed'), check_id='resources.guest.client-web')
        self.store.record(old)
        self.assertIsNone(self.store.record(dict(result('ok'), check_id='resources.guest.x-web')))
        self.assertEqual(self.store.report()['pending_alerts'], 1)

    def test_partial_observation_preserves_changes_when_operation_disappears(self):
        def observation(operations):
            return {'family': 'x-web', 'operations': operations, 'complete': False}
        first = {'viewerQuery': {'queryId': 'a', 'operationType': 'query'}}
        second = {'viewerQuery': {'queryId': 'b', 'operationType': 'query'}}
        observe_operations('guest', observation(first), self.store)
        self.assertEqual(observe_operations('guest', observation(second), self.store)['status'], 'changed')
        third = {'unrelatedQuery': {'queryId': 'z', 'operationType': 'query'}}
        change = observe_operations('guest', observation(third), self.store)
        self.assertEqual(change['status'], 'changed')
        self.assertIn('viewerQuery', change['details']['diff'])
        self.assertEqual(observe_operations('guest', observation(first), self.store)['status'], 'ok')

    def test_unobserved_local_drift_does_not_falsely_recover(self):
        def compare(value):
            return {'SearchTimeline': {'fields': {'queryId': 'drift'}}} if 'SearchTimeline' in value['operations'] else {}
        with patch('api_watch.runner.compare_installed', side_effect=compare):
            first = observe_operations('guest', {'family': 'x-web', 'operations': {
                'SearchTimeline': {'queryId': 'new', 'operationType': 'query'}}}, self.store)
            second = observe_operations('guest', {'family': 'x-web', 'operations': {
                'OtherQuery': {'queryId': 'other', 'operationType': 'query'}}}, self.store)
        self.assertEqual(first['status'], 'changed')
        self.assertEqual(second['status'], 'changed')
        self.assertIn('SearchTimeline', second['details']['local_diff'])

    def test_snapshot_never_overwrites_baseline_until_explicit_acceptance(self):
        first = self.store.save_snapshot('guest/client-web', snapshot())
        self.store.accept_baseline('guest/client-web', first)
        second = self.store.save_snapshot('guest/client-web', snapshot('new'))
        self.assertEqual(self.store.baseline('guest/client-web')['operations']['Viewer']['queryId'], 'old')
        self.store.accept_baseline('guest/client-web', second)
        self.assertEqual(self.store.baseline('guest/client-web')['operations']['Viewer']['queryId'], 'new')
        partial = self.store.save_snapshot('guest/x-web', {'complete': False})
        with self.assertRaises(ValueError):
            self.store.accept_baseline('guest/x-web', partial)

    def test_pruning_preserves_baseline_and_undelivered_alert(self):
        first = self.store.save_snapshot('guest/client-web', snapshot(), now=0)
        self.store.accept_baseline('guest/client-web', first)
        self.store.save_snapshot('guest/client-web', snapshot('new'), now=0)
        self.store.record(result(), threshold=1, now=0)
        self.store.prune(1, now=200000)
        self.assertEqual(len(self.store.report()['snapshots']), 1)
        self.assertEqual(self.store.report()['pending_alerts'], 1)

    def test_process_lock_and_private_permissions(self):
        with process_lock(self.root):
            with self.assertRaises(RuntimeError):
                with process_lock(self.root):
                    pass
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.root / 'state.sqlite3').stat().st_mode & 0o777, 0o600)

    def test_webhook_failure_keeps_event_with_backoff_and_no_error_text(self):
        import requests
        event = self.store.record(result(), threshold=1, now=0)
        fake = Mock()
        fake.__enter__ = Mock(return_value=fake)
        fake.__exit__ = Mock(return_value=False)
        fake.post.side_effect = requests.ConnectionError('secret-cookie secret-proxy')
        output = io.StringIO()
        with patch('requests.Session', return_value=fake), contextlib.redirect_stdout(output):
            self.assertEqual(flush_alerts(self.store, 'https://notify.invalid/secret'), 1)
        self.assertNotIn('secret', output.getvalue())
        self.assertEqual(self.store.report()['pending_alerts'], 1)
        self.assertFalse(self.store.pending())
        row = self.store.db.execute('SELECT payload FROM events').fetchone()
        self.assertEqual(json.loads(row[0])['event_id'], event['event_id'])
        self.assertFalse(fake.post.call_args.kwargs['allow_redirects'])

    def test_real_local_webhook_receives_issue_then_recovery_once(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(204)
                self.end_headers()
            def log_message(self, *args):
                pass
        server = HTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}/events'
            self.store.record(result(), threshold=1)
            self.assertEqual(flush_alerts(self.store, url), 0)
            self.store.record(result())
            flush_alerts(self.store, url)
            self.store.record(result('ok'))
            self.assertEqual(flush_alerts(self.store, url), 0)
            self.assertEqual([event['kind'] for event in received], ['issue', 'recovered'])
            self.assertNotEqual(received[0]['event_id'], received[1]['event_id'])
            self.assertEqual(self.store.report()['pending_alerts'], 0)
        finally:
            server.shutdown()
            worker.join()
            server.server_close()

    def test_scan_partial_does_not_save_or_replace_snapshot(self):
        old = self.store.save_snapshot('guest/client-web', snapshot())
        self.store.accept_baseline('guest/client-web', old)
        with patch('api_watch.frontend.scan_frontend', return_value={
                'check_id': 'frontend.guest', 'status': 'incomplete',
                'summary': 'partial', 'details': {}, 'snapshot': snapshot('new')}):
            rows = list(scan_results(load_config(), self.root, {'cookies': '', 'proxy': None},
                                     self.store, threading.Event()))
        self.assertEqual(rows[0]['status'], 'incomplete')
        self.assertEqual(len(self.store.report()['snapshots']), 1)
        self.assertNotIn('snapshot', rows[0])

    def test_incomplete_new_stack_still_detects_entry_version_change(self):
        item = {'check_id': 'frontend.guest', 'status': 'incomplete', 'summary': 'partial',
                'details': {'family': 'x-web', 'observed_fingerprint': 'a' * 64}, 'snapshot': None}
        with patch('api_watch.frontend.scan_frontend', side_effect=lambda *a, **k: json.loads(json.dumps(item))):
            list(scan_results(load_config(), self.root, {'cookies': '', 'proxy': None}, self.store, threading.Event()))
            item['details']['observed_fingerprint'] = 'b' * 64
            rows = list(scan_results(load_config(), self.root, {'cookies': '', 'proxy': None}, self.store, threading.Event()))
        self.assertEqual(rows[0]['status'], 'changed')
        self.assertEqual(rows[1]['status'], 'incomplete')
        self.assertEqual(self.store.report()['snapshots'], [])

    def test_offline_once_persists_results_and_stopped_heartbeat(self):
        config = load_config()
        config['frontend']['enabled'] = False
        with patch('api_watch.suite.run_suite', return_value=[result('ok')]) as probes, contextlib.redirect_stdout(io.StringIO()):
            code = run(config, self.root, lambda: {'cookies': '', 'proxy': None, 'webhook': None}, once=True)
        self.assertEqual(code, 0)
        self.assertEqual(probes.call_count, 1)
        self.assertEqual(self.store.report()['checks'][0]['status'], 'ok')
        self.assertFalse(healthy(self.root))

    def test_missing_cookie_continues_anonymous_checks(self):
        config = load_config()
        config['frontend']['enabled'] = False
        with patch('api_watch.suite.run_suite', return_value=[dict(result('ok'), check_id='guest.user')]) as probes, contextlib.redirect_stdout(io.StringIO()):
            code = run(config, self.root, lambda: {'cookies': '', 'proxy': None, 'webhook': None,
                                                  'credentials_error': True}, once=True)
        self.assertEqual(code, 2)
        probes.assert_called_once()
        self.assertEqual(probes.call_args.args[1]['cookies'], '')

    def test_expired_session_pauses_across_runs_until_cookie_changes(self):
        config = load_config()
        config['frontend']['enabled'] = False
        secrets = {'cookies': 'auth_token=one; ct0=a', 'proxy': None, 'webhook': None}
        def fake(probe_config, actual_secrets, store, **kwargs):
            return [dict(result('disabled' if actual_secrets.get('auth_paused') else 'auth_expired'),
                         check_id='auth.viewer')]
        with patch('api_watch.suite.run_suite', side_effect=fake) as probes, contextlib.redirect_stdout(io.StringIO()):
            run(config, self.root, lambda: dict(secrets), once=True)
            run(config, self.root, lambda: dict(secrets), once=True)
            self.assertTrue(probes.call_args.args[1]['auth_paused'])
            secrets['cookies'] = 'auth_token=one; ct0=b'
            run(config, self.root, lambda: dict(secrets), once=True)
            # The new credential is tried; this synthetic response expires it
            # again after the call, so record the input in the side effect.
            self.assertEqual(probes.call_count, 3)

    def test_phase_stall_and_stopped_state_fail_healthcheck(self):
        path = self.root / 'heartbeat.json'
        data = {'heartbeat_at': 1000, 'phase_started': 0, 'phase': 'scan', 'phase_limit': 900}
        path.write_text(json.dumps(data))
        self.assertFalse(healthy(self.root, now=1001))
        data['phase'] = 'idle'
        path.write_text(json.dumps(data))
        self.assertTrue(healthy(self.root, now=1001))
        self.assertFalse(healthy(self.root, now=1100))


class ConfigTests(unittest.TestCase):
    def test_no_automatic_dotenv_or_business_credentials(self):
        with patch.dict('os.environ', {'X_COOKIES': 'DO-NOT-USE', 'X_PROXY': 'DO-NOT-USE'}, clear=True):
            self.assertEqual(load_secrets()['cookies'], '')
            self.assertIsNone(load_secrets()['proxy'])

    def test_file_based_cookie_reloads_and_missing_cookie_preserves_proxy(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {}, clear=True):
            root = Path(directory)
            env = root / 'monitor.env'
            env.write_text('API_WATCH_COOKIES_FILE=cookies.txt\nAPI_WATCH_PROXY=http://proxy.invalid:8080\n')
            self.assertTrue(load_secrets(env)['credentials_error'])
            self.assertEqual(load_secrets(env)['proxy'], 'http://proxy.invalid:8080')
            (root / 'cookies.txt').write_text('auth_token=one; ct0=test')
            self.assertIn('one', load_secrets(env)['cookies'])
            (root / 'cookies.txt').write_text('auth_token=two; ct0=test')
            self.assertIn('two', load_secrets(env)['cookies'])

    def test_invalid_config_is_rejected_without_echoing_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            for bad in ({'secret-token': 1}, {'probe_interval_seconds': 0},
                        {'probes': {'guest': 'false'}}, {'probes': {'username': '../secret'}},
                        {'frontend': {'max_assets': 1.1}}):
                path.write_text(json.dumps(bad))
                with self.assertRaises(ConfigError) as raised:
                    load_config(path)
                self.assertNotIn('secret', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
