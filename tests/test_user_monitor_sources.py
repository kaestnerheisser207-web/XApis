"""监听来源回归测试，只使用模拟响应和本机 HTTP 服务。"""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from builder.auth import XAuth, _GUEST_TTL
from builder.params import GraphQLOperation
from x_apis.client_errors import XClientError
from x_apis.errors import GraphQLError
from x_apis.user_monitor_sources import CookieTimelineSource, GuestTimelineSource


USER = {'data': {'user': {'result': {'rest_id': '123'}}}}


def response(raw, status=200):
    result = Mock(status_code=status, headers={})
    result.json.return_value = raw
    if status >= 400:
        error = RuntimeError('模拟 HTTP 失败')
        error.response = result
        result.raise_for_status.side_effect = error
    return result


def invalid_guest(status=200):
    return response({'errors': [{'code': 239, 'message': 'Bad guest token'}]}, status)


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.sessions = []
        self.session_lock = threading.Lock()
        self.make_session = patch('x_apis.user_monitor_sources.requests.Session',
                                  side_effect=self._make_session).start()
        self.addCleanup(patch.stopall)
        self.get_handler = lambda session, url, kwargs: response(USER)
        self.post_handler = lambda session, url, kwargs: response({'guest_token': 'fresh'})

    def _make_session(self, **kwargs):
        session = Mock()
        session.cookies = {}
        session.owner = threading.get_ident()
        session.config = kwargs
        session.get.side_effect = lambda url, **kw: self.get_handler(session, url, kw)
        session.post.side_effect = lambda url, **kw: self.post_handler(session, url, kw)
        with self.session_lock:
            self.sessions.append(session)
        return session

    def guest(self, **kwargs):
        source = GuestTimelineSource(**kwargs)
        source.auth.guest_token = 'original'
        source.auth._transaction = Mock()
        source.auth._transaction.generate.return_value = 'transaction'
        self.addCleanup(source.close)
        return source

    def cookie(self):
        auth = XAuth(proxies={'https': 'http://localhost:1234'}).prepare_auth(
            'auth_token=test-login; ct0=test-csrf; lang=zh-cn')
        auth._transaction = Mock()
        auth._transaction.generate.return_value = 'transaction'
        client = SimpleNamespace(auth=auth, users=Mock())
        client.users.me.return_value = {'user_id': '999'}
        source = CookieTimelineSource(client)
        self.addCleanup(source.close)
        return source, client

    def test_same_thread_reuses_connection_and_applies_transport_profile(self):
        source = self.guest(proxy='http://localhost:1234')
        self.assertEqual(source.resolve('alice'), '123')
        self.assertEqual(source.resolve('bob'), '123')
        self.assertEqual(len(self.sessions), 1)
        session = self.sessions[0]
        self.assertFalse(session.config['use_thread_local_curl'])
        kwargs = session.get.call_args.kwargs
        self.assertEqual(kwargs['timeout'], 30)
        self.assertEqual(kwargs['impersonate'], session.config['impersonate'])
        self.assertEqual(kwargs['proxies'], {'http': 'http://localhost:1234',
                                            'https': 'http://localhost:1234'})
        self.assertEqual(kwargs['headers']['sec-fetch-mode'], 'cors')
        self.assertIn('user-agent', kwargs['headers'])
        self.assertTrue(kwargs['discard_cookies'])

    def test_threads_own_independent_reused_sessions(self):
        source = self.guest()
        barrier = threading.Barrier(3)

        def work():
            barrier.wait(timeout=5)
            source.resolve('alice')
            source.resolve('bob')

        with ThreadPoolExecutor(max_workers=3) as executor:
            list(executor.map(lambda _: work(), range(3)))
        self.assertEqual(len(self.sessions), 3)
        self.assertEqual(len({session.owner for session in self.sessions}), 3)
        self.assertTrue(all(session.get.call_count == 2 for session in self.sessions))

    def test_close_releases_every_session_once_and_rejects_future_requests(self):
        source = self.guest()
        source.resolve('alice')
        source.close()
        source.close()
        self.sessions[0].close.assert_called_once()
        with self.assertRaises(XClientError) as caught:
            source.resolve('alice')
        self.assertEqual(caught.exception.code, 'source_closed')
        self.assertEqual(self.sessions[0].get.call_count, 1)

    def test_close_waits_until_inflight_request_finishes(self):
        source = self.guest()
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()

        def blocked_get(session, url, kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            session.close.assert_not_called()
            return response(USER)

        self.get_handler = blocked_get
        with ThreadPoolExecutor(max_workers=2) as executor:
            request = executor.submit(source.resolve, 'alice')
            self.assertTrue(entered.wait(5))
            closing = executor.submit(lambda: (source.close(), closed.set()))
            self.assertFalse(closed.wait(0.05))
            release.set()
            self.assertEqual(request.result(timeout=5), '123')
            closing.result(timeout=5)
        self.sessions[0].close.assert_called_once()

    def test_guest_never_reads_environment_or_sends_session_cookies(self):
        with patch.dict('os.environ', {'X_COOKIES': 'auth_token=private; ct0=private'}):
            source = self.guest()
            source.resolve('alice')
            self.sessions[0].cookies['auth_token'] = 'unexpected'
            source.resolve('bob')
        for call in self.sessions[0].get.call_args_list:
            kwargs = call.kwargs
            self.assertNotIn('cookies', kwargs)
            self.assertNotIn('x-twitter-auth-type', kwargs['headers'])
            self.assertNotIn('x-csrf-token', kwargs['headers'])
            self.assertNotIn('private', str(kwargs))
        self.assertFalse(source.auth.is_logged_in)
        self.assertEqual(self.sessions[0].cookies, {})

    def test_cookie_source_freezes_cookies_with_matching_csrf(self):
        source, client = self.cookie()
        client.auth.cookie.update(auth_token='changed-login', ct0='changed-csrf')
        source.resolve('alice')
        session = self.sessions[0]
        kwargs = session.get.call_args.kwargs
        self.assertEqual(source.scope, '999')
        self.assertEqual(kwargs['cookies']['auth_token'], 'test-login')
        self.assertEqual(kwargs['cookies']['ct0'], kwargs['headers']['x-csrf-token'])
        self.assertEqual(kwargs['headers']['x-csrf-token'], 'test-csrf')
        self.assertEqual(kwargs['headers']['x-twitter-client-language'], 'zh-cn')
        client.users.get.assert_not_called()
        session.post.assert_not_called()

    def test_cookie_auth_failure_does_not_refresh_or_mutate_credentials(self):
        source, client = self.cookie()
        self.get_handler = lambda *args: response({'errors': [{'code': 32}]}, 401)
        with self.assertRaises(RuntimeError) as caught:
            source.resolve('alice')
        self.assertEqual(caught.exception.response.status_code, 401)
        self.assertEqual(self.sessions[0].get.call_count, 1)
        self.sessions[0].post.assert_not_called()
        self.assertEqual(client.auth.cookie, source.auth.cookie)

    def test_partial_graphql_error_preserves_error_and_does_not_accept_data(self):
        for factory in (self.guest, lambda: self.cookie()[0]):
            source = factory()
            raw = {'data': USER['data'], 'errors': [{'code': 88, 'message': 'limited'}]}
            self.get_handler = lambda *args: response(raw)
            with self.assertRaises(GraphQLError) as caught:
                source.resolve('alice')
            self.assertEqual(caught.exception.errors[0]['code'], 88)
            self.assertEqual(caught.exception.response.status_code, 200)

    def test_http_403_preserves_source_and_target_graphql_codes(self):
        for factory in (self.guest, lambda: self.cookie()[0]):
            for code in (326, 179):
                with self.subTest(factory=factory, code=code):
                    source = factory()
                    self.get_handler = lambda *args: response({'errors': [{'code': code}]}, 403)
                    with self.assertRaises(GraphQLError) as caught:
                        source.resolve('alice')
                    self.assertEqual(caught.exception.errors[0]['code'], code)
                    self.assertEqual(caught.exception.response.status_code, 403)
                    self.sessions[-1].post.assert_not_called()

    def test_guest_renews_after_three_hours_via_reused_session(self):
        source = self.guest()
        source.auth._guest_ts = time.time() - _GUEST_TTL - 1
        self.assertEqual(source.resolve('alice'), '123')
        self.assertEqual(len(self.sessions), 1)
        session = self.sessions[0]
        self.assertEqual(session.post.call_count, 1)
        self.assertTrue(session.post.call_args.args[0].endswith('/guest/activate.json'))
        self.assertNotIn('cookies', session.post.call_args.kwargs)
        self.assertEqual(session.get.call_args.kwargs['headers']['x-guest-token'], 'fresh')

    def test_explicit_guest_rejection_refreshes_once(self):
        for status in (200, 401):
            with self.subTest(status=status):
                source = self.guest()
                self.get_handler = lambda session, url, kw: (
                    invalid_guest(status) if kw['headers']['x-guest-token'] == 'original'
                    else response(USER))
                self.assertEqual(source.resolve('alice'), '123')
                session = self.sessions[-1]
                self.assertEqual(session.get.call_count, 2)
                self.assertEqual(session.post.call_count, 1)

    def test_message_only_guest_rejection_is_recognized(self):
        source = self.guest()
        self.get_handler = lambda session, url, kw: (
            response({'errors': [{'message': 'Guest token has expired'}]}, 401)
            if kw['headers']['x-guest-token'] == 'original' else response(USER))
        self.assertEqual(source.resolve('alice'), '123')
        self.assertEqual(self.sessions[-1].post.call_count, 1)

    def test_second_guest_rejection_becomes_safe_error(self):
        source = self.guest()
        self.get_handler = lambda *args: invalid_guest()
        with self.assertRaises(XClientError) as caught:
            source.resolve('alice')
        self.assertEqual(caught.exception.code, 'guest_auth_failed')
        self.assertNotIn('fresh', str(caught.exception))
        self.assertEqual(self.sessions[0].get.call_count, 2)
        self.assertEqual(self.sessions[0].post.call_count, 1)

    def test_generic_401_and_rate_limits_do_not_refresh_guest(self):
        for status, errors in ((401, [{'code': 32}]), (403, [{'code': 326}]),
                               (429, [{'code': 88}]), (200, [{'code': 88}])):
            with self.subTest(status=status):
                source = self.guest()
                self.get_handler = lambda *args: response({'errors': errors}, status)
                with self.assertRaises(RuntimeError) as caught:
                    source.resolve('alice')
                self.assertEqual(caught.exception.response.status_code, status)
                self.sessions[-1].post.assert_not_called()

    def test_simultaneous_bad_token_has_one_refresh(self):
        source = self.guest()
        barrier = threading.Barrier(3)

        def get(session, url, kwargs):
            if kwargs['headers']['x-guest-token'] == 'original':
                barrier.wait(timeout=5)
                return invalid_guest()
            return response(USER)

        self.get_handler = get
        with ThreadPoolExecutor(max_workers=3) as executor:
            self.assertEqual(list(executor.map(source.resolve, ['a', 'b', 'c'])), ['123'] * 3)
        self.assertEqual(sum(session.post.call_count for session in self.sessions), 1)
        self.assertEqual(sum(session.get.call_count for session in self.sessions), 6)

    def test_same_token_returned_by_activation_still_coalesces_refreshes(self):
        source = self.guest()
        barrier = threading.Barrier(3)

        def get(session, url, kwargs):
            if session.get.call_count == 1:
                barrier.wait(timeout=5)
                return invalid_guest()
            return response(USER)

        self.get_handler = get
        self.post_handler = lambda *args: response({'guest_token': 'original'})
        with ThreadPoolExecutor(max_workers=3) as executor:
            self.assertEqual(list(executor.map(source.resolve, ['a', 'b', 'c'])), ['123'] * 3)
        self.assertEqual(sum(session.post.call_count for session in self.sessions), 1)

    def test_simultaneous_activation_failure_is_shared(self):
        source = self.guest()
        barrier = threading.Barrier(3)

        def get(*args):
            barrier.wait(timeout=5)
            return invalid_guest()

        self.get_handler = get
        self.post_handler = lambda *args: response({'errors': [{'code': 88}]}, 429)
        with ThreadPoolExecutor(max_workers=3) as executor:
            results = [executor.submit(source.resolve, name) for name in ('a', 'b', 'c')]
            for result in results:
                with self.assertRaises(RuntimeError) as caught:
                    result.result(timeout=5)
                self.assertEqual(caught.exception.response.status_code, 429)
        self.assertEqual(sum(session.post.call_count for session in self.sessions), 1)

    def test_guest_and_transaction_initialization_are_serialized(self):
        source = self.guest()
        source.auth._guest_token = ''
        source.auth._transaction = None
        barrier = threading.Barrier(3)

        def work(name):
            barrier.wait(timeout=5)
            return source.resolve(name)

        with patch('builder.auth.ClientTransaction.from_network', return_value=Mock()) as init:
            with ThreadPoolExecutor(max_workers=3) as executor:
                self.assertEqual(list(executor.map(work, ['a', 'b', 'c'])), ['123'] * 3)
            init.assert_called_once()
        self.assertEqual(sum(session.post.call_count for session in self.sessions), 1)

    def test_fetch_page_preserves_operation_cursor_and_parser_contract(self):
        source = self.guest()
        raw = {'data': {'user': {'result': {'timeline_v2': {'timeline': {
            'instructions': [{'type': 'TimelineAddEntries', 'entries': [
                {'content': {'cursorType': 'Bottom', 'value': 'next'}}]}]}}}}}}
        self.get_handler = lambda *args: response(raw)
        self.assertEqual(source.fetch_page('123', 'previous'), ([], [], 'next'))
        call = self.sessions[0].get.call_args
        self.assertTrue(call.args[0].endswith('/UserTweets'))
        variables = json.loads(call.kwargs['params']['variables'])
        self.assertEqual(variables['cursor'], 'previous')
        self.assertEqual(variables['userId'], '123')
        self.assertFalse(source.chronological)
        self.assertTrue(source.thread_safe)


class LocalTransportTests(unittest.TestCase):
    def test_real_connection_reuse_discards_response_cookie(self):
        observed = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def do_GET(self):
                observed.append((self.client_address, self.headers.get('Cookie')))
                payload = json.dumps(USER).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.send_header('Content-Type', 'application/json')
                self.send_header('Set-Cookie', 'auth_token=should-not-persist; Path=/')
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        source = GuestTimelineSource()
        source.auth.guest_token = 'local-test'
        source.auth._transaction = Mock()
        source.auth._transaction.generate.return_value = 'transaction'
        try:
            with patch.object(GraphQLOperation, 'url', return_value=(
                    f'http://127.0.0.1:{server.server_port}/timeline')):
                self.assertEqual(source.resolve('alice'), '123')
                self.assertEqual(source.resolve('bob'), '123')
            self.assertEqual(observed[0][0], observed[1][0])
            self.assertEqual([cookies for _, cookies in observed], [None, None])
        finally:
            source.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
