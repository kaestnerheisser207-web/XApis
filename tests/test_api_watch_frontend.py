"""Frontend monitoring safety and semantic-diff regressions (offline only)."""
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import requests

from api_watch.frontend import (_asset_url, diff_snapshots, prune_frontend_cache, scan_frontend)


BASE = 'https://abs.twimg.com/responsive-web/client-web/'
MAIN = BASE + 'main.123456789abc.js'
SIGN = BASE + 'ondemand.s.1111111111111111a.js'
CASTLE = BASE + 'ondemand.castle.2222222222222222a.js'
SHELL = ('<script src="' + MAIN + '"></script>'
         '<script>var n={1:"ondemand.s",2:"ondemand.castle"};'
         'var h={1:"1111111111111111",2:"2222222222222222"};'
         'window.__INITIAL_STATE__={"featureSwitch":{"defaultConfig":'
         '{"enabled":{"value":true},"other":{"value":false},'
         '"responsive_web_castle_public_key":{"value":"public-key"}}}};</script>')
OP = ('{queryId:"query_v1",operationName:"UserTweets",operationType:"query",'
      'metadata:{featureSwitches:["enabled","other"],fieldToggles:[]}}')
# Public metadata observed in viewer-query-DDT3nazs.js on 2026-10-02.
RELAY = ('params:{id:`I-Bd87NnoPJQyeV0WpAOww`,metadata:{},name:`viewerQuery`,'
         'operationKind:`query`,text:null,providedVariables:'
         '{__relay_internal__pv__appviewerisloggedinprovider:e}}')


class Response:
    def __init__(self, text='', status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.body = text.encode()
        self.closed = False

    def iter_content(self, chunk_size):
        yield self.body

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []
        self.cookies = {}
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.handler(url, kwargs)

    def close(self):
        self.closed = True


class FrontendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sessions = []
        self.responses = {}
        self.shell = SHELL
        self.secrets = 'auth_token=SECRET_LOGIN; ct0=SECRET_CSRF'
        self.handler = self.default_handler
        self.session_patch = patch('api_watch.frontend.requests.Session', side_effect=self.make_session)
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)

    def make_session(self):
        session = FakeSession(lambda url, kwargs: self.handler(url, kwargs))
        self.sessions.append(session)
        return session

    def default_handler(self, url, kwargs):
        if url.startswith('https://x.com/'):
            return Response(self.shell)
        if url in self.responses:
            value = self.responses[url]
            if isinstance(value, BaseException):
                raise value
            return value
        return Response(OP if url == MAIN else '// asset')

    def scan(self, **kwargs):
        return scan_frontend(self.root, **kwargs)

    def test_complete_manifest_produces_observation_without_touching_sdk(self):
        with patch('utils.graphql_registry.refresh') as refresh:
            result = self.scan()
        self.assertEqual(result['status'], 'ok')
        self.assertTrue(result['snapshot']['complete'])
        self.assertEqual(result['snapshot']['family'], 'client-web')
        self.assertEqual(result['snapshot']['featureDefaults'], {'enabled': True, 'other': False})
        self.assertEqual(result['details']['assets_downloaded'], 3)
        self.assertEqual(len(self.sessions[0].calls), 1)
        self.assertTrue(all(s.closed for s in self.sessions))
        refresh.assert_not_called()

    def test_partial_download_does_not_produce_deletions_or_snapshot(self):
        baseline = self.scan()['snapshot']
        with tempfile.TemporaryDirectory() as other:
            self.responses[CASTLE] = Response(status=404)
            failed = scan_frontend(Path(other))
        self.assertEqual(failed['status'], 'incomplete')
        self.assertIsNone(failed['snapshot'])
        delta = diff_snapshots(baseline, failed['snapshot'])
        self.assertFalse(delta['comparable'])
        self.assertEqual(delta['operations']['removed'], [])

    def test_hashed_assets_reuse_derived_cache_only(self):
        first = self.scan()
        second = self.scan()
        self.assertEqual(second['status'], 'ok')
        self.assertEqual(second['details']['assets_cached'], 3)
        self.assertEqual(second['details']['assets_downloaded'], 0)
        self.assertEqual(first['snapshot'], second['snapshot'])
        self.assertEqual(len(self.sessions[3].calls), 0)
        cache = list(self.root.rglob('*.json'))
        self.assertEqual(len(cache), 3)
        for path in cache:
            text = path.read_text()
            self.assertNotIn('<script', text)
            self.assertNotIn('window.__INITIAL_STATE__', text)
            self.assertNotIn('// asset', text)
            self.assertEqual(set(json.loads(text)), {'version', 'family', 'sha256', 'operations', 'imports'})

    def test_corrupt_cache_is_refetched(self):
        self.scan()
        path = next(self.root.rglob('*.json'))
        path.write_text('{broken')
        result = self.scan()
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['details']['assets_downloaded'], 1)

    def test_unversioned_assets_are_not_indefinitely_cached(self):
        self.shell = SHELL.replace(MAIN, BASE + 'main-platform.js')
        self.responses[BASE + 'main-platform.js'] = Response(OP)
        self.assertEqual(self.scan()['status'], 'ok')
        self.responses[BASE + 'main-platform.js'] = Response(OP.replace('query_v1', 'query_v2'))
        second = self.scan()
        self.assertEqual(second['details']['assets_downloaded'], 1)
        self.assertEqual(second['snapshot']['operations']['UserTweets']['queryId'], 'query_v2')

    def test_prune_only_known_derived_cache_files_and_enforce_count(self):
        self.scan()
        cache = sorted(self.root.rglob('*.json'))
        unrelated = cache[0].parent / 'keep.json'
        unrelated.write_text('untouched')
        os.utime(cache[0], (0, 0))
        self.assertEqual(prune_frontend_cache(self.root), 1)
        self.assertFalse(cache[0].exists())
        self.assertEqual(prune_frontend_cache(self.root, max_entries=1), 1)
        self.assertTrue(unrelated.exists())
        self.assertEqual(len([p for p in self.root.rglob('*.json') if p.name != 'keep.json']), 1)

    def test_cache_access_keeps_active_assets_from_age_pruning(self):
        self.scan()
        for path in self.root.rglob('*.json'):
            os.utime(path, (0, 0))
        self.assertEqual(self.scan()['details']['assets_cached'], 3)
        self.assertEqual(prune_frontend_cache(self.root), 0)

    def test_cookies_only_sent_to_shell_no_redirects_or_environment_proxy(self):
        result = self.scan(cookies=self.secrets, proxy='http://user:SECRET_PROXY@localhost:8080')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['check_id'], 'frontend.auth')
        self.assertEqual(self.sessions[0].calls[0][0], 'https://x.com/home')
        self.assertEqual(self.sessions[0].calls[0][1]['headers']['cookie'], self.secrets)
        self.assertFalse(self.sessions[0].trust_env)
        for url, kwargs in self.sessions[1].calls:
            self.assertTrue(url.startswith(BASE))
            self.assertNotIn('cookie', kwargs['headers'])
            self.assertNotIn('cookies', kwargs)
            self.assertFalse(kwargs['allow_redirects'])
        output = json.dumps(result) + ''.join(p.read_text() for p in self.root.rglob('*.json'))
        for secret in ('SECRET_LOGIN', 'SECRET_CSRF', 'SECRET_PROXY'):
            self.assertNotIn(secret, output)

    def test_exception_text_cannot_leak_secrets(self):
        self.handler = lambda *_: (_ for _ in ()).throw(
            requests.ConnectionError('auth_token=SECRET_LOGIN proxy=SECRET_PROXY'))
        result = self.scan(cookies=self.secrets)
        self.assertEqual(result['status'], 'network_error')
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(len(self.sessions[0].calls), 1)
        self.assertEqual(len(self.sessions[1].calls), 0)

    def test_redirect_is_not_followed_and_auth_login_is_distinct(self):
        self.handler = lambda *_: Response(status=302, headers={'location': 'https://evil.example/SECRET'})
        result = self.scan(cookies=self.secrets)
        self.assertEqual(result['details']['reason'], 'redirect_rejected')
        self.assertNotIn('evil', json.dumps(result))
        self.assertEqual(len(self.sessions[0].calls), 1)
        self.handler = lambda *_: Response(status=302, headers={'location': '/i/flow/login'})
        self.assertEqual(self.scan(cookies=self.secrets)['status'], 'auth_expired')

    def test_rate_limit_stops_without_retry(self):
        self.responses[MAIN] = Response(status=429)
        result = self.scan()
        self.assertEqual(result['status'], 'rate_limited')
        self.assertEqual(len(self.sessions[1].calls), 1)

    def test_x_web_reports_coverage_boundary_and_traverses_modules(self):
        main = 'https://abs.twimg.com/x-web/x-web/assets/index-ABCDEFGH.js'
        imported = 'https://abs.twimg.com/x-web/x-web/assets/relay-ABCDEFGH.js'
        self.shell = f'<script type="module" src="{main}"></script>'
        self.responses[main] = Response('import "./relay-ABCDEFGH.js";')
        self.responses[imported] = Response('var q={params:{cacheID:"abc",id:"123",metadata:{},'
                                            'name:"UserQuery",operationKind:"query",text:null}};')
        result = self.scan()
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['details']['family'], 'x-web')
        self.assertEqual(result['details']['operations'], 1)
        self.assertEqual(result['details']['assets_downloaded'], 2)
        self.assertEqual(result['details']['reason'], 'x_web_coverage_unverified')
        self.assertIsNone(result['snapshot'])
        self.assertEqual(result['details']['fingerprint_coverage'], 'shell-entry-urls')
        self.assertEqual(len(result['details']['observed_fingerprint']), 64)
        self.assertEqual(len(result['details']['observed_operations_fingerprint']), 64)

    def prepare_x_web_graph(self):
        main = 'https://abs.twimg.com/x-web/x-web/assets/index-ABCDEFGH.js'
        viewer = 'https://abs.twimg.com/x-web/x-web/assets/viewer-query-DDT3nazs.js'
        self.shell = f'<script type="module" src="{main}"></script>'
        imports = [f'./component-{index:08d}.js' for index in range(20)]
        imports.append('./viewer-query-DDT3nazs.js')
        self.responses[main] = Response('var modules=' + json.dumps(imports))
        self.responses[viewer] = Response(RELAY)
        return main, viewer

    def test_current_x_web_backtick_metadata_and_unknown_switches(self):
        self.prepare_x_web_graph()
        result = self.scan(max_assets=2)
        self.assertEqual(result['details']['reason'], 'asset_limit_exceeded')
        self.assertEqual(result['details']['assets_downloaded'], 2)
        self.assertGreater(result['details']['assets_total'], 2)
        self.assertEqual(result['observation'], {
            'family': 'x-web', 'scope': 'guest', 'complete': False,
            'operations': {'viewerQuery': {'queryId': 'I-Bd87NnoPJQyeV0WpAOww',
                                          'operationType': 'query'}}})
        cached_ops = {}
        for path in self.root.rglob('*.json'):
            cached_ops.update(json.loads(path.read_text())['operations'])
        self.assertIsNone(cached_ops['viewerQuery']['featureSwitches'])
        self.assertIsNone(cached_ops['viewerQuery']['fieldToggles'])
        self.assertIsNone(result['snapshot'])

    def test_query_modules_are_observed_before_broad_component_graph(self):
        main, viewer = self.prepare_x_web_graph()
        self.scan(max_assets=2)
        self.assertEqual([url for url, _ in self.sessions[1].calls], [main, viewer])

    def test_interpolated_template_metadata_is_not_treated_as_literal_id(self):
        _, viewer = self.prepare_x_web_graph()
        self.responses[viewer] = Response(RELAY.replace('`I-Bd87NnoPJQyeV0WpAOww`', '`prefix-${dynamic}`'))
        result = self.scan(max_assets=2)
        self.assertEqual(result['observation']['operations'], {})
        self.assertEqual(result['status'], 'incomplete')

    def test_positive_observation_survives_deadline_after_query(self):
        _, viewer = self.prepare_x_web_graph()
        now = [0]

        def saved(root, family, url, data):
            if url == viewer:
                now[0] = 100

        with patch('api_watch.frontend.time.monotonic', side_effect=lambda: now[0]), \
                patch('api_watch.frontend._save_cached', side_effect=saved):
            result = self.scan(max_seconds=10)
        self.assertEqual(result['details']['reason'], 'deadline_exceeded')
        self.assertIn('viewerQuery', result['observation']['operations'])
        self.assertIsNone(result['snapshot'])

    def test_discovered_resource_graph_has_a_separate_hard_limit(self):
        self.prepare_x_web_graph()
        with patch('api_watch.frontend._MAX_DISCOVERED_ASSETS', 10):
            result = self.scan(max_assets=100)
        self.assertEqual(result['details']['reason'], 'discovery_limit_exceeded')
        self.assertEqual(result['status'], 'incomplete')
        self.assertIsNone(result['snapshot'])

    def test_backtick_import_literal_is_discovered_without_evaluation(self):
        main, viewer = self.prepare_x_web_graph()
        self.responses[main] = Response('import(`./viewer-query-DDT3nazs.js`);')
        result = self.scan()
        self.assertIn('viewerQuery', result['observation']['operations'])
        self.assertEqual([url for url, _ in self.sessions[1].calls], [main, viewer])

    def test_entry_fingerprint_survives_incomplete_scan_and_detects_change(self):
        old = 'https://abs.twimg.com/x-web/x-web/assets/index-ABCDEFGH.js'
        new = 'https://abs.twimg.com/x-web/x-web/assets/index-12345678.js'
        self.shell = f'<script src="{old}"></script>'
        self.responses[old] = Response(status=404)
        first = self.scan()
        self.shell = f'<script src="{new}"></script>'
        self.responses[new] = Response(status=404)
        second = self.scan()
        self.assertEqual(first['status'], 'incomplete')
        self.assertEqual(second['status'], 'incomplete')
        self.assertNotEqual(first['details']['observed_fingerprint'], second['details']['observed_fingerprint'])
        self.assertNotIn('observed_operations_fingerprint', second['details'])

    def test_mixed_shells_are_not_combined_or_retried(self):
        self.shell += '<script src="https://abs.twimg.com/x-web/x-web/assets/index-ABCDEFGH.js"></script>'
        result = self.scan()
        self.assertEqual(result['details']['reason'], 'mixed_frontend_assets')
        self.assertEqual(len(self.sessions[0].calls), 1)
        self.assertEqual(len(self.sessions[1].calls), 0)

    def test_cross_family_import_is_rejected(self):
        self.responses[MAIN] = Response(OP + ';import "https://abs.twimg.com/x-web/x-web/assets/index-ABCDEFGH.js"')
        result = self.scan()
        self.assertEqual(result['details']['reason'], 'mixed_frontend_assets')
        self.assertIsNone(result['snapshot'])

    def test_conflicting_query_ids_within_one_file_are_rejected(self):
        self.responses[MAIN] = Response(OP + OP.replace('query_v1', 'query_v2'))
        result = self.scan()
        self.assertEqual(result['details']['reason'], 'operation_conflict')
        self.assertIsNone(result['snapshot'])

    def test_conflicting_query_ids_across_files_are_rejected(self):
        self.responses[CASTLE] = Response(OP.replace('query_v1', 'query_v2'))
        result = self.scan()
        self.assertEqual(result['details']['reason'], 'operation_conflict')
        self.assertIsNone(result['snapshot'])
        self.assertNotIn('UserTweets', result['observation']['operations'])

    def test_zero_operations_and_unsupported_metadata_are_incomplete(self):
        self.responses[MAIN] = Response('// no operations')
        self.assertEqual(self.scan()['details']['reason'], 'no_operations')
        with tempfile.TemporaryDirectory() as other:
            self.responses[MAIN] = Response(OP.replace('featureSwitches', 'newFormat'))
            result = scan_frontend(Path(other))
        self.assertEqual(result['details']['reason'], 'unsupported_operation_metadata')

    def test_asset_limit_deadline_and_cancellation_are_incomplete(self):
        self.assertEqual(self.scan(max_assets=2)['details']['reason'], 'asset_limit_exceeded')
        event = threading.Event()
        event.set()
        self.assertEqual(self.scan(stop_event=event)['details']['reason'], 'cancelled')
        with patch('api_watch.frontend.time.monotonic', side_effect=[0, 100]):
            result = self.scan(max_seconds=1)
        self.assertEqual(result['details']['reason'], 'deadline_exceeded')

    def test_only_expected_cdn_paths_are_allowed(self):
        for url in ('https://evil.example/main.js', 'http://abs.twimg.com/x-web/x-web/main.js',
                    'https://abs.twimg.com@evil.example/x-web/x-web/main.js',
                    'https://abs.twimg.com/x-web/x-web/../main.js',
                    'https://abs.twimg.com/x-web/x-web/main.js?token=SECRET',
                    'https://abs.twimg.com/x-web/x-web/%2e%2e/main.js',
                    'https://abs.twimg.com/unrelated/main.js'):
            self.assertIsNone(_asset_url(url))

    def test_diff_ignores_order_and_timestamps_and_reports_semantics(self):
        baseline = self.scan()['snapshot']
        candidate = copy.deepcopy(baseline)
        candidate['generatedAt'] = 'tomorrow'
        candidate['operations']['UserTweets']['featureSwitches'].reverse()
        self.assertFalse(diff_snapshots(baseline, candidate)['changed'])
        candidate['operations']['UserTweets']['queryId'] = 'query_v2'
        candidate['featureDefaults']['enabled'] = False
        candidate['materials']['site_verification'] = 'a' * 64
        result = diff_snapshots(baseline, candidate)
        self.assertTrue(result['comparable'])
        self.assertTrue(result['changed'])
        self.assertEqual(result['operations']['changed']['UserTweets'], {
            'queryId': {'before': 'query_v1', 'after': 'query_v2'}})
        self.assertEqual(result['featureDefaults']['changed']['enabled'], {'before': True, 'after': False})

    def test_diff_rejects_scope_family_and_incomplete_candidates(self):
        baseline = self.scan()['snapshot']
        for field, value in [('family', 'x-web'), ('scope', 'auth'), ('complete', False)]:
            candidate = copy.deepcopy(baseline)
            candidate[field] = value
            candidate['operations'] = {}
            result = diff_snapshots(baseline, candidate)
            self.assertFalse(result['comparable'])
            self.assertEqual(result['operations']['removed'], [])


if __name__ == '__main__':
    unittest.main()
