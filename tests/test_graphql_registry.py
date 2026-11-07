"""Registry refresh must keep the installed release intact on incomplete input."""
import json
import stat
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from utils import graphql_registry as registry


SHELL = ('<script src="https://abs.twimg.com/responsive-web/client-web/main.12345678.js"></script>'
         '<script>var n={1:"ondemand.s",2:"other"};'
         'var h={1:"1111111111111111",2:"2222222222222222"};'
         'window.__INITIAL_STATE__={"featureSwitch":{"defaultConfig":'
         '{"enabled":{"value":false}},"user":{"config":'
         '{"enabled":{"value":true}}}}};</script>')
OP = ('{queryId:"query_v1",operationName:"UserTweets",operationType:"query",'
      'metadata:{featureSwitches:["enabled"],fieldToggles:[]}}')


class RegistryRefreshTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / 'graphql.json'
        self.before = {'operations': {'KeepMe': {'queryId': 'installed'}}, 'featureDefaults': {}}
        self.path.write_text(json.dumps(self.before))
        self.old_bytes = self.path.read_bytes()
        for name, value in [('REGISTRY_PATH', str(self.path)), ('_cache', self.before)]:
            patcher = patch.object(registry, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.shell = patch.object(registry, 'fetch_shell_html', return_value=SHELL).start()
        self.chunk = patch.object(registry, 'fetch_chunk', return_value=OP).start()
        self.addCleanup(patch.stopall)

    def refresh(self, **kwargs):
        return registry.refresh(workers=1, verbose=False, **kwargs)

    def assert_preserved(self):
        self.assertEqual(self.path.read_bytes(), self.old_bytes)
        self.assertIs(registry._cache, self.before)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_authenticated_manifest_and_defaults_use_same_shell(self):
        auth = SimpleNamespace(is_logged_in=True)
        with patch.object(registry, '_fetch_home_shell', return_value=SHELL) as home:
            result = self.refresh(auth=auth)
        home.assert_called_once_with(auth, None)
        self.shell.assert_not_called()
        self.assertTrue(result['featureDefaults']['enabled'])
        self.assertEqual(result, json.loads(self.path.read_text()))
        self.assertIs(registry._cache, result)

    def test_auth_shell_failure_does_not_switch_to_anonymous(self):
        with patch.object(registry, '_fetch_home_shell', side_effect=RuntimeError('shell_failed')):
            with self.assertRaises(RuntimeError):
                self.refresh(auth=SimpleNamespace(is_logged_in=True))
        self.shell.assert_not_called()
        self.assert_preserved()

    def test_incomplete_download_preserves_disk_cache_and_hides_transport_secrets(self):
        self.chunk.side_effect = [OP, RuntimeError('http://user:SECRET@proxy.invalid'), OP]
        with self.assertRaisesRegex(RuntimeError, '1 个分片采集失败') as error:
            self.refresh()
        self.assertNotIn('SECRET', str(error.exception))
        self.assert_preserved()

    def test_empty_or_unsupported_metadata_does_not_replace_registry(self):
        for text in ('// no operations', '{queryId:"new_id",operationName:"Unsupported"}'):
            with self.subTest(text=text):
                self.chunk.return_value = text
                with self.assertRaises(RuntimeError):
                    self.refresh()
                self.assert_preserved()

    def test_conflicting_operation_between_chunks_preserves_registry(self):
        self.chunk.side_effect = [OP, OP.replace('query_v1', 'query_v2'), OP]
        with self.assertRaisesRegex(RuntimeError, '元数据冲突'):
            self.refresh()
        self.assert_preserved()

    def test_html_returned_for_one_chunk_is_not_a_complete_release(self):
        self.chunk.side_effect = [OP, '<!doctype html><html>challenge</html>', OP]
        with self.assertRaisesRegex(RuntimeError, '分片采集失败'):
            self.refresh()
        self.assert_preserved()

    def test_conflicting_operation_within_chunk_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, '元数据冲突'):
            registry._extract_operations(OP + OP.replace('query_v1', 'query_v2'))

    def test_failed_atomic_replace_preserves_installed_file_and_cleans_temporary(self):
        with patch.object(registry.os, 'replace', side_effect=OSError('disk_error')):
            with self.assertRaises(OSError):
                self.refresh()
        self.assert_preserved()

    def test_atomic_refresh_retains_resource_read_permissions(self):
        self.path.chmod(0o644)
        self.refresh()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)

    def test_authenticated_new_stack_or_redirect_is_not_a_valid_manifest(self):
        from builder import client
        for response in (SimpleNamespace(status_code=200, text='x-web/x-web'),
                         SimpleNamespace(status_code=302, text=SHELL)):
            response.raise_for_status = lambda: None
            with self.subTest(status=response.status_code), patch.object(client, 'get', return_value=response) as get:
                with self.assertRaisesRegex(RuntimeError, '未返回 client-web'):
                    registry._fetch_home_shell(SimpleNamespace(cookie={'auth_token': 'SECRET'}))
                self.assertFalse(get.call_args.kwargs['allow_redirects'])


if __name__ == '__main__':
    unittest.main()
