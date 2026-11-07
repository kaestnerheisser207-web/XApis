"""Real loopback HTTP process coverage; no external services or credentials."""
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from curl_cffi import CurlOpt
from curl_cffi.requests.utils import set_curl_options

from builder.request_scope import BoundedTransport
from tests.service_fixture import COOKIES, KEY, NEXT_CURSOR, SECOND_COOKIES


class GunicornHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.process = None
        cls.logs = tempfile.TemporaryFile(mode='w+b')
        cls.addClassCleanup(cls.stop_server)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            cls.port = listener.getsockname()[1]
        cls.process = subprocess.Popen([
            sys.executable, '-m', 'gunicorn', 'tests.service_fixture:application',
            '--bind', '127.0.0.1:' + str(cls.port), '--workers', '1', '--worker-class', 'sync',
            '--timeout', '10', '--graceful-timeout', '2', '--access-logfile', '/dev/null',
            '--error-logfile', '-', '--log-level', 'warning'],
            cwd=Path(__file__).resolve().parents[1], stdout=cls.logs, stderr=cls.logs,
            start_new_session=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if cls.process.poll() is not None:
                break
            try:
                status, _, body = cls.request('GET', '/healthz', authenticated=False)
                if status == 200 and body.get('status') == 'ok':
                    return
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.05)
        cls.logs.seek(0)
        raise RuntimeError('Synthetic Gunicorn failed to start: ' + cls.logs.read().decode(errors='replace'))

    @classmethod
    def stop_server(cls):
        if cls.process is not None:
            if cls.process.poll() is None:
                os.killpg(cls.process.pid, signal.SIGTERM)
                try:
                    cls.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(cls.process.pid, signal.SIGKILL)
                    cls.process.wait(timeout=5)
            cls.process = None
        cls.logs.close()

    @classmethod
    def request(cls, method, path, body=None, *, authenticated=True):
        headers = {'Content-Type': 'application/json'}
        if authenticated:
            headers['Authorization'] = 'Bearer ' + KEY
        connection = http.client.HTTPConnection('127.0.0.1', cls.port, timeout=2)
        try:
            connection.request(method, path,
                               body=None if body is None else json.dumps(body, ensure_ascii=False).encode(),
                               headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), json.loads(response.read())
        finally:
            connection.close()

    def invoke(self, capability, payload, *, cookies=COOKIES):
        return self.request('POST', '/v1/execute', {
            'capability': capability, 'payload': payload, 'account': {'cookies': cookies}})

    def test_authentication_and_readiness_over_real_http(self):
        status, headers, body = self.request('POST', '/v1/execute', {}, authenticated=False)
        self.assertEqual(status, 401)
        self.assertEqual(body['error']['code'], 'service_unauthorized')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertTrue(headers['X-Request-ID'])
        status, _, body = self.request('GET', '/readyz', authenticated=False)
        self.assertEqual(status, 200)
        self.assertFalse(body['upstream_checked'])

    def test_all_five_capabilities_cross_the_gunicorn_http_boundary(self):
        cases = (
            ('profile.get', {'username': 'reader'}, 'user_id'),
            ('content.get', {'content_id': '123'}, 'work_id'),
            ('content.search', {'query': 'fixture query'}, 'items'),
            ('content.list_by_actor', {'username': 'reader'}, 'items'),
            ('reply.list', {'content_id': '123'}, 'items'),
        )
        for capability, payload, result_key in cases:
            with self.subTest(capability=capability):
                status, _, body = self.invoke(capability, payload)
                self.assertEqual(status, 200)
                if result_key == 'items':
                    self.assertEqual(body['data']['items'][0]['work_id'], '123')
                    self.assertEqual(body['data']['items'][0]['view_count'], '11')
                else:
                    self.assertEqual(body['data'][result_key], '123')

    def test_opaque_unicode_cursor_survives_two_real_http_pages(self):
        for capability, payload in (
            ('content.search', {'query': '分页 query', 'product': 'Latest', 'count': 1}),
            ('content.list_by_actor', {'username': 'reader', 'count': 1}),
            ('reply.list', {'content_id': '123'}),
        ):
            with self.subTest(capability=capability):
                status, _, first = self.invoke(capability, payload)
                self.assertEqual(status, 200)
                self.assertEqual(first['data']['next_cursor'], NEXT_CURSOR)
                status, _, second = self.invoke(capability, {**payload, 'cursor': first['data']['next_cursor']})
                self.assertEqual(status, 200)
                self.assertEqual(second['data']['items'][0]['work_id'], '124')
                self.assertIsNone(second['data']['next_cursor'])

    def test_error_status_and_retry_semantics_survive_real_http(self):
        for username, expected_status, code in (
            ('timeout', 504, 'timeout'), ('auth_error', 401, 'auth_required'),
            ('rate_limit', 429, 'rate_limited'), ('unavailable', 404, 'user_unavailable'),
        ):
            with self.subTest(username=username):
                status, headers, body = self.invoke('profile.get', {'username': username})
                self.assertEqual(status, expected_status)
                self.assertEqual(body['error']['code'], code)
                self.assertFalse(body['error']['retryable'])
                self.assertNotIn('private-transport', json.dumps(body))
                if status == 429:
                    self.assertEqual(headers['Retry-After'], '3')
                    self.assertEqual(body['error']['upstream_codes'], [88])

    def test_sequential_accounts_do_not_reuse_previous_account_result(self):
        for cookies, user_id in ((COOKIES, '123'), (SECOND_COOKIES, '456'), (COOKIES, '123')):
            status, _, body = self.invoke('profile.get', {'username': 'reader'}, cookies=cookies)
            self.assertEqual(status, 200)
            self.assertEqual(body['data']['user_id'], user_id)
            status, _, body = self.invoke('content.get', {'content_id': '123'}, cookies=cookies)
            self.assertEqual(status, 200)
            self.assertEqual(body['data']['user_id'], user_id)
            self.assertNotIn('auth_token', json.dumps(body))


class CurlProxyOptionTests(unittest.TestCase):
    def options(self, explicit_proxy=None):
        """Use curl_cffi's real option builder without ever performing I/O."""
        handle = Mock()

        def factory(**settings):
            def request(method, url, **options):
                set_curl_options(handle, method, url,
                    params_list=[None, None], headers_list=[None, None], cookies_list=[None, None],
                    proxies_list=[{}, options.get('proxies')], proxy=options.get('proxy'),
                    verify_list=[True, True], curl_options=settings['curl_options'])
                options['content_callback'](b'{}')
                return SimpleNamespace(status_code=200, content=b'')
            return SimpleNamespace(request=request, close=lambda: None)

        transport = BoundedTransport(5, session_factory=factory)
        try:
            options = {'proxies': {'http': explicit_proxy, 'https': explicit_proxy}} if explicit_proxy else {}
            transport.request('GET', 'https://x.com/i/api/synthetic', **options)
        finally:
            transport.close()
        return {call.args[0]: call.args[1] for call in handle.setopt.call_args_list}

    def test_direct_egress_disables_environment_proxy_at_libcurl_boundary(self):
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://127.0.0.1:9',
                                     'ALL_PROXY': 'http://127.0.0.1:9'}):
            options = self.options()
        self.assertIn(CurlOpt.PROXY, options)
        self.assertEqual(options[CurlOpt.PROXY], '')

    def test_explicit_account_proxy_disables_environment_no_proxy_bypass(self):
        with patch.dict(os.environ, {'NO_PROXY': '*', 'no_proxy': '*'}):
            options = self.options('http://127.0.0.1:18089')
        self.assertEqual(options[CurlOpt.PROXY], 'http://127.0.0.1:18089')
        self.assertIn(CurlOpt.NOPROXY, options)
        self.assertEqual(options[CurlOpt.NOPROXY], '')


if __name__ == '__main__':
    unittest.main()
