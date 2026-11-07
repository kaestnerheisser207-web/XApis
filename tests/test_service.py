"""Provider HTTP contract and bounded transport; all upstreams are synthetic."""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from curl_cffi import CurlOpt
from utils.transaction import ClientTransaction

from builder import client as http
from builder.request_scope import BoundedTransport, current_transport, request_scope
from x_apis import XClient, XClientError
from xapis_service.app import create_app, sdk_client
from xapis_service.config import Settings


KEY = 'synthetic-service-key-only-for-tests-1234'
PROFILE = {'user_id': '123', 'username': 'reader', 'name': 'Reader', 'description': None, 'followers': None}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.sdk = Mock()
        self.sdk.users.get.return_value = PROFILE
        self.sdk.posts.search.return_value = {'items': [], 'next_cursor': 'cursor-next'}
        self.sdk.users.posts.return_value = {'items': [], 'next_cursor': None}
        self.sdk.comments.list.return_value = {'items': [], 'next_cursor': None}
        self.factory = Mock(return_value=self.sdk)
        self.app = create_app(Settings(KEY), client_factory=self.factory)
        self.client = self.app.test_client()
        self.headers = {'Authorization': 'Bearer ' + KEY}
        self.body = {'capability': 'profile.get', 'payload': {'username': 'reader'},
                     'account': {'cookies': 'auth_token=PRIVATE_COOKIE; ct0=PRIVATE_CSRF'}}

    def send(self, body=None, **kwargs):
        return self.client.post('/v1/execute', json=body or self.body, headers=self.headers, **kwargs)

    def test_authentication_precedes_upstream_and_json_parsing(self):
        response = self.client.post('/v1/execute', data='invalid json')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json['error']['code'], 'service_unauthorized')
        self.factory.assert_not_called()

    def test_health_does_not_claim_upstream_acceptance(self):
        self.assertEqual(self.client.get('/healthz').status_code, 200)
        self.assertFalse(self.client.get('/readyz').json['upstream_checked'])
        self.factory.assert_not_called()

    def test_profile_and_account_are_forwarded_once_without_environment_fallback(self):
        with patch.dict(os.environ, {'X_COOKIES': 'HOST_SECRET', 'X_PROXY': 'http://HOST_SECRET:80'}):
            response = self.send()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {'data': PROFILE})
        self.assertEqual(self.factory.call_args.args[0], {'cookies': self.body['account']['cookies'], 'proxy': None})
        self.sdk.users.get.assert_called_once_with('reader')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertTrue(response.headers['X-Request-ID'])
        self.assertIsNone(current_transport())

    def test_each_page_is_one_business_call_with_unchanged_cursor(self):
        for capability, payload, method, expected in (
            ('content.search', {'query': 'from:reader', 'cursor': 'cursor-old', 'count': 7},
             self.sdk.posts.search, {'query': 'from:reader', 'cursor': 'cursor-old', 'count': 7}),
            ('content.list_by_actor', {'username': 'reader', 'cursor': 'cursor-old'},
             self.sdk.users.posts, {'username': 'reader', 'cursor': 'cursor-old'}),
        ):
            body = {**self.body, 'capability': capability, 'payload': payload}
            response = self.send(body)
            self.assertEqual(response.status_code, 200)
            method.assert_called_once_with(**expected)
        self.assertEqual(self.factory.call_count, 2)

    def test_comment_cursor_mapping(self):
        response = self.send({**self.body, 'capability': 'reply.list',
                              'payload': {'content_id': '123', 'cursor': 'next'}})
        self.assertEqual(response.status_code, 200)
        self.sdk.comments.list.assert_called_once_with('123', cursor='next')

    def test_invalid_inputs_never_open_account(self):
        cases = [None, [], {}, {**self.body, 'unknown': True},
                 {**self.body, 'capability': 'message.send'},
                 {**self.body, 'account': {}},
                 {**self.body, 'account': {'cookies': 'auth_token=incomplete'}},
                 {**self.body, 'payload': {'username': 'reader', 'extra': 'ignored?'}},
                 {**self.body, 'account': {**self.body['account'], 'proxy': 'file:///etc/passwd'}},
                 {**self.body, 'account': {**self.body['account'], 'proxy': 'http://host:bad'}},
                 {**self.body, 'payload': {'username': 'https://other.invalid/reader'}},
                 {**self.body, 'capability': 'content.search', 'payload': {'query': 'x', 'count': True}},
                 {**self.body, 'capability': 'content.search', 'payload': {'query': 'x', 'product': 'People'}}]
        for body in cases:
            response = self.client.post('/v1/execute', data=json.dumps(body),
                                        content_type='application/json', headers=self.headers)
            self.assertEqual(response.status_code, 400, body)
        self.factory.assert_not_called()

    def test_non_json_numbers_duplicates_bad_encoding_and_oversize_are_rejected(self):
        samples = [json.dumps(self.body).replace('"reader"', 'NaN'),
                   json.dumps({**self.body, 'capability': 'content.search', 'payload': {'query': 'x', 'cursor': float('nan')}}),
                   '{"capability":"profile.get","capability":"profile.get"}', '{', b'\xff']
        for raw in samples:
            response = self.client.post('/v1/execute', data=raw, content_type='application/json', headers=self.headers)
            self.assertEqual(response.status_code, 400)
        response = self.client.post('/v1/execute', data='x' * 65537, content_type='application/json', headers=self.headers)
        self.assertEqual(response.status_code, 413)
        self.factory.assert_not_called()

    def test_errors_are_structured_and_secret_free(self):
        for exc, status, code in ((TimeoutError('PRIVATE_TOKEN'), 504, 'timeout'),
                                   (XClientError('auth_required', '登录失效'), 401, 'auth_required'),
                                   (RuntimeError('PRIVATE_TOKEN'), 502, 'operation_failed')):
            self.factory.side_effect = exc
            response = self.send()
            self.assertEqual((response.status_code, response.json['error']['code']), (status, code))
            self.assertFalse(response.json['error']['retryable'])
            self.assertNotIn('PRIVATE', response.text)
        self.assertIsNone(current_transport())

    def test_retry_after_is_preserved_but_does_not_authorize_replay(self):
        self.factory.side_effect = XClientError('rate_limited', '限流', retry_after=9, upstream_codes=(88,))
        response = self.send()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers['Retry-After'], '9')
        self.assertEqual(response.json['error']['upstream_codes'], [88])
        self.factory.assert_called_once()

    def test_total_budget_includes_initialization_and_suppresses_late_success(self):
        def late(account, transport):
            transport.deadline = 0
            return self.sdk
        self.factory.side_effect = late
        response = self.send()
        self.assertEqual(response.status_code, 504)
        self.sdk.users.get.assert_not_called()

    def test_busy_worker_does_not_start_a_second_request(self):
        entered, release = threading.Event(), threading.Event()
        def hold(account, transport):
            entered.set()
            if not release.wait(5):
                raise AssertionError('test did not release worker')
            return self.sdk
        self.factory.side_effect = hold
        result = []
        worker = threading.Thread(target=lambda: result.append(self.app.test_client().post(
            '/v1/execute', json=self.body, headers=self.headers)))
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.send().status_code, 503)
            self.assertEqual(self.factory.call_count, 1)
        finally:
            release.set(); worker.join(5)
        self.assertEqual(result[0].status_code, 200)

    def test_response_size_is_bounded_and_slot_released(self):
        self.sdk.users.get.return_value = {**PROFILE, 'description': 'x' * (2 * 1024 * 1024)}
        self.assertEqual(self.send().status_code, 502)
        self.sdk.users.get.return_value = PROFILE
        self.assertEqual(self.send().status_code, 200)

    def test_openapi_capabilities_match_runtime(self):
        document = self.client.get('/openapi.json', headers=self.headers).json
        variants = document['paths']['/v1/execute']['post']['requestBody']['content']['application/json']['schema']['oneOf']
        declared = [item['properties']['capability']['const'] for item in variants]
        enabled = self.client.get('/v1/capabilities', headers=self.headers).json['capabilities']
        self.assertEqual(declared, enabled)

    def test_real_sdk_malformed_read_is_not_http_success(self):
        sdk = XClient.from_cookies(self.body['account']['cookies'])
        self.factory.return_value = sdk
        with patch('x_apis.client.XAPI.search_work', return_value={'data': {}}) as upstream:
            response = self.send({**self.body, 'capability': 'content.search', 'payload': {'query': 'reader'}})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json['error']['code'], 'invalid_response')
        upstream.assert_called_once()

    def test_logs_have_no_cookies_proxy_or_query(self):
        body = {**self.body, 'capability': 'content.search', 'payload': {'query': 'PRIVATE_QUERY'},
                'account': {**self.body['account'], 'proxy': 'http://user:PRIVATE_PROXY@proxy.invalid:8080'}}
        with self.assertLogs('xapis_service.requests', level='INFO') as logs:
            self.send(body)
        self.assertNotIn('PRIVATE', '\n'.join(logs.output))
        self.assertNotIn(KEY, '\n'.join(logs.output))

    def test_service_configuration_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env()
        for value in ('NaN', 'Infinity', '0'):
            with patch.dict(os.environ, {'XAPIS_API_KEY': KEY, 'XAPIS_REQUEST_TIMEOUT': value}, clear=True):
                with self.assertRaises(ValueError):
                    Settings.from_env()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'key'
            path.write_text(KEY); path.chmod(0o600)
            with patch.dict(os.environ, {'XAPIS_API_KEY_FILE': str(path)}, clear=True):
                self.assertEqual(Settings.from_env().api_key, KEY)
                path.chmod(0o644)
                with self.assertRaises(ValueError):
                    Settings.from_env()


class TransportTests(unittest.TestCase):
    def session(self):
        fake = Mock()
        def request(method, url, **options):
            options['content_callback'](b'{"ok":true}')
            return SimpleNamespace(content=b'', status_code=200)
        fake.request.side_effect = request
        return fake

    def test_sdk_transport_uses_scoped_session_and_cleans_up(self):
        session = self.session()
        factory = Mock(return_value=session)
        with request_scope(5, session_factory=factory):
            result = http.get('https://x.com/i/api/example', timeout=30)
            self.assertEqual(result.content, b'{"ok":true}')
        options = session.request.call_args.kwargs
        self.assertLessEqual(options['timeout'], 5)
        self.assertFalse(options['allow_redirects'])
        self.assertTrue(options['discard_cookies'])
        self.assertEqual(options['proxies'], {'all': ''})
        factory.assert_called_once_with(trust_env=False, curl_options={CurlOpt.NOPROXY: ''})
        session.close.assert_called_once()
        self.assertIsNone(current_transport())

    def test_response_limit_deadline_and_untrusted_host_abort(self):
        session = self.session()
        transport = BoundedTransport(5, max_bytes=3, session_factory=lambda **kw: session)
        try:
            with self.assertRaises(XClientError) as caught:
                transport.get('https://x.com/data')
            self.assertEqual(caught.exception.code, 'invalid_response')
            self.assertEqual(session.request.call_count, 1)
            for url in ('http://x.com/data', 'https://evil.invalid/data', 'https://x.com:444/data'):
                with self.assertRaises(XClientError):
                    transport.get(url)
            self.assertEqual(session.request.call_count, 1)
            transport.deadline = 0
            with self.assertRaises(XClientError) as caught:
                transport.get('https://x.com/data')
            self.assertEqual(caught.exception.code, 'timeout')
            self.assertEqual(session.request.call_count, 1)
        finally:
            transport.close()

    def test_redirect_does_not_execute_a_second_request(self):
        session = self.session()
        session.request.side_effect = None
        session.request.return_value = SimpleNamespace(status_code=302)
        with request_scope(5, session_factory=lambda **kw: session):
            with self.assertRaises(XClientError):
                http.get('https://x.com/data')
        session.request.assert_called_once()

    def test_signature_uses_release_asset_without_network_or_host_override(self):
        from utils.resource_paths import asset_path
        transport = Mock()
        with patch.dict(os.environ, {'XCTID_PROFILE_FILE': '/private/sensitive-file'}), \
             patch('utils.transaction.ClientTransaction.from_cached', wraps=ClientTransaction.from_cached) as profile:
            client = sdk_client({'cookies': 'auth_token=fake; ct0=fake'}, transport)
        profile.assert_called_once_with(str(asset_path('transaction_l1.json')))
        self.assertTrue(client.auth.client_transaction_id('GET', '/synthetic'))
        transport.get.assert_not_called()


if __name__ == '__main__':
    unittest.main()
