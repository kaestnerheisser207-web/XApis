"""持久化监听回归测试，不使用真实 X 凭证，也不执行远端写入。"""
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from x_apis.client_errors import XClientError
from x_apis.errors import GraphQLError
from x_apis.user_monitor import GuestTimelineSource, UserPostMonitor, Webhook, _snowflake, parse_user_page


START = 1790900000.0


def tweet(seconds, uid='123', **legacy):
    tid = str(_snowflake(START + seconds) + 1)
    return {'__typename': 'Tweet', 'rest_id': tid,
            'core': {'user_results': {'result': {'rest_id': uid, 'core': {'screen_name': 'alice'}}}},
            'legacy': {'full_text': 'hello', **legacy}}


def entry(post):
    return {'entryId': 'tweet-' + post['rest_id'],
            'content': {'itemContent': {'tweet_results': {'result': post}}}}


def page(*posts, cursor=None, pinned=None):
    entries = [entry(p) for p in posts]
    if cursor:
        entries.append({'entryId': 'cursor-bottom', 'content': {'cursorType': 'Bottom', 'value': cursor}})
    instructions = [{'type': 'TimelineAddEntries', 'entries': entries}]
    if pinned:
        instructions.insert(0, {'type': 'TimelinePinEntry', 'entry': entry(pinned)})
    return {'data': {'user': {'result': {'timeline_v2': {'timeline': {'instructions': instructions}}}}}}


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = START
        self.client = SimpleNamespace(state_dir=Path(self.tmp.name), auth=object(), users=Mock())
        self.client.users.me.return_value = {'user_id': '999'}
        self.client.users.get.return_value = {'user_id': '123'}
        self.fetch = Mock(return_value=page(tweet(-100)))
        owner = self
        class Source:
            thread_safe = True
            chronological = False
            def __init__(self, client):
                self.scope = client.users.me()['user_id']
            def resolve(self, username):
                return owner.client.users.get(username)['user_id']
            def fetch_page(self, uid, cursor):
                return parse_user_page(owner.fetch(uid, cursor=cursor), uid)
        self.factory = patch('x_apis.user_monitor.CookieTimelineSource', Source)
        self.factory.start()
        self.addCleanup(self.factory.stop)

    def monitor(self, **kwargs):
        monitor = UserPostMonitor(self.client, ['alice'], clock=lambda: self.now, max_rps=20, **kwargs)
        self.addCleanup(monitor.close)
        return monitor

    def advance(self, seconds=60):
        self.now += seconds

    def test_first_start_ignores_history_and_pinned_old_post(self):
        monitor = self.monitor()
        monitor.poll_once()
        self.assertEqual(monitor.status()['delivery']['pending'], 0)
        self.advance()
        self.fetch.side_effect = [page(tweet(50), cursor='next', pinned=tweet(-5000)), page(tweet(30), tweet(-20))]
        monitor.poll_once()
        seen = []
        monitor.deliver_once(seen.append)
        self.assertEqual({e['post']['work_id'] for e in seen}, {tweet(50)['rest_id'], tweet(30)['rest_id']})
        self.assertEqual(self.fetch.call_args.kwargs['cursor'], 'next')

    def test_restart_replays_unacknowledged_and_retains_dedupe(self):
        monitor = self.monitor()
        self.fetch.return_value = page(tweet(1))
        monitor.poll_once()
        monitor.close()
        restored = self.monitor()
        seen = []
        restored.deliver_once(seen.append)
        self.assertEqual(len(seen), 1)
        self.advance()
        restored.poll_once()
        restored.deliver_once(seen.append)
        self.assertEqual(len(seen), 1)
        # 已保存的目标不重新解析用户名，避免名称被他人接管后监听错人。
        self.assertEqual(self.client.users.get.call_count, 1)

    def test_page_budget_is_resumable_across_restart(self):
        monitor = self.monitor(max_pages=1)
        self.fetch.side_effect = lambda uid, cursor: page(tweet(40), cursor='page2') if cursor is None else page(tweet(30), cursor='page3')
        monitor.poll_once()
        self.assertTrue(monitor.status()['targets'][0]['catching_up'])
        monitor.close()
        restored = self.monitor(max_pages=1)
        self.advance()
        self.fetch.side_effect = None
        self.fetch.return_value = page(tweet(20), tweet(-10))
        restored.poll_once()
        self.assertEqual(self.fetch.call_args.kwargs['cursor'], 'page3')
        self.assertFalse(restored.status()['targets'][0]['catching_up'])
        self.assertEqual(restored.status()['delivery']['pending'], 3)

    def test_transport_failure_keeps_page_checkpoint(self):
        monitor = self.monitor(max_pages=1)
        self.fetch.side_effect = lambda uid, cursor: page(tweet(40), cursor='page2') if cursor is None else page(tweet(30), cursor='page3')
        monitor.poll_once()
        self.advance()
        self.fetch.side_effect = TimeoutError('SECRET')
        monitor.poll_once()
        self.assertTrue(monitor.status()['targets'][0]['catching_up'])
        self.advance(1000)
        self.fetch.side_effect = None
        self.fetch.side_effect = None
        self.fetch.return_value = page(tweet(20), tweet(-10))
        monitor.poll_once()
        self.assertEqual(self.fetch.call_args.kwargs['cursor'], 'page3')
        self.assertEqual(monitor.status()['delivery']['pending'], 3)

    def test_event_and_checkpoint_roll_back_together(self):
        monitor = self.monitor()
        with monitor._transaction() as db:
            db.execute("CREATE TRIGGER fail_checkpoint BEFORE UPDATE ON targets BEGIN SELECT RAISE(ABORT, 'disk'); END")
        self.fetch.return_value = page(tweet(1))
        with self.assertRaises(XClientError) as error:
            monitor.poll_once()
        self.assertEqual(error.exception.code, 'storage_failed')
        self.assertEqual(monitor.status()['delivery']['pending'], 0)

    def test_complete_head_advances_watermark_and_compares_independent_ids(self):
        monitor = self.monitor()
        self.fetch.return_value = page(tweet(1))
        monitor.poll_once()
        self.assertEqual(monitor.status()['targets'][0]['highwater'], tweet(1)['rest_id'])
        comparison = monitor.compare_posts([
            {'user_id': '123', 'post_id': tweet(1)['rest_id']},
            {'user_id': '123', 'post_id': tweet(1)['rest_id']},
            {'user_id': '123', 'post_id': tweet(2)['rest_id']},
            {'user_id': '123', 'post_id': tweet(-1)['rest_id']},
        ])
        self.assertEqual(comparison['eligible'], 2)
        self.assertEqual(comparison['observed'], 1)
        self.assertEqual(comparison['before_start'], 1)
        self.assertEqual(comparison['observed_fraction'], 0.5)
        self.assertEqual(comparison['not_observed'], [{'user_id': '123', 'post_id': tweet(2)['rest_id']}])

    def test_cli_metrics_report_is_private_and_contains_no_post_body(self):
        from main import build_parser, cmd_watch_users
        monitor = self.monitor()
        self.client.users.monitor.return_value = monitor
        destination = Path(self.tmp.name) / 'report.json'
        args = build_parser().parse_args(['watch-users', 'alice', '--once', '--metrics-file', str(destination)])
        with patch('x_apis.XClient.from_env', return_value=self.client), redirect_stderr(io.StringIO()):
            self.assertEqual(cmd_watch_users(None, args), 0)
        report = json.loads(destination.read_text())
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        self.assertIn('metrics', report)
        self.assertNotIn('hello', destination.read_text())

    def test_explicit_source_needs_no_xclient_or_viewer(self):
        source = SimpleNamespace(scope='public-feed', resolve=lambda _: '123',
                                 fetch_page=lambda uid, cursor: parse_user_page(page(tweet(1)), uid))
        with UserPostMonitor.from_source(source, ['alice'], state_dir=self.tmp.name, clock=lambda: self.now) as monitor:
            monitor.poll_once()
            seen = []
            monitor.deliver_once(seen.append)
            self.assertEqual(seen[0]['source_id'], 'public-feed')
        self.client.users.me.assert_not_called()

    def test_cli_once_reports_status_and_does_not_emit_history(self):
        from main import build_parser, cmd_watch_users
        monitor = self.monitor()
        self.client.users.monitor.return_value = monitor
        stdout, stderr = io.StringIO(), io.StringIO()
        args = build_parser().parse_args(['watch-users', 'alice', '--once'])
        with patch('x_apis.XClient.from_env', return_value=self.client), redirect_stdout(stdout), redirect_stderr(stderr):
            code = cmd_watch_users(None, args)
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue(), '')
        self.assertEqual(json.loads(stderr.getvalue())['type'], 'monitor.status')

    def test_sdk_factory_creates_monitor(self):
        from x_apis import XClient
        client = XClient(object(), state_dir=self.tmp.name)
        with patch.object(client.users, 'me', return_value={'user_id': '999'}), \
             patch.object(client.users, 'get', return_value={'user_id': '123'}):
            with client.users.monitor('alice', clock=lambda: self.now) as monitor:
                self.assertEqual(monitor.user_ids, ['123'])

    def test_real_local_webhook_retry_keeps_event_id(self):
        deliveries = []
        class Receiver(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                deliveries.append((dict(self.headers), body))
                self.send_response(503 if len(deliveries) == 1 else 204)
                self.end_headers()
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            monitor = self.monitor()
            self.fetch.return_value = page(tweet(1))
            monitor.poll_once()
            webhook = Webhook(f'http://127.0.0.1:{server.server_port}/events', 'test-secret')
            monitor.deliver_once(webhook)
            self.assertEqual(monitor.status()['delivery']['pending'], 1)
            self.advance(20)
            monitor.deliver_once(webhook)
            self.assertEqual(monitor.status()['delivery']['delivered'], 1)
            self.assertEqual(deliveries[0][1], deliveries[1][1])
            headers, body = deliveries[1]
            expected = hmac.new(b'test-secret', headers['X-XApis-Timestamp'].encode() + b'.' + body, hashlib.sha256).hexdigest()
            self.assertEqual(headers['X-XApis-Signature'], 'sha256=' + expected)
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(2)

    def test_late_indexed_post_inside_overlap_window_is_found(self):
        monitor = self.monitor()
        self.fetch.return_value = page(tweet(40))
        monitor.poll_once()
        self.advance()
        self.fetch.return_value = page(tweet(40), tweet(20))
        monitor.poll_once()
        self.assertEqual(monitor.status()['delivery']['pending'], 2)

    def test_cursor_cycle_does_not_advance_highwater(self):
        monitor = self.monitor()
        before = monitor.status()['targets'][0]['highwater']
        self.fetch.side_effect = [page(tweet(40), cursor='a'), page(tweet(30), cursor='b'), page(tweet(20), cursor='a')]
        monitor.poll_once()
        target = monitor.status()['targets'][0]
        self.assertEqual(target['highwater'], before)
        self.assertEqual(target['backfill_error'], 'cursor_stalled')
        self.assertLessEqual(self.fetch.call_count, 3)

    def test_partial_graphql_error_is_not_empty_success(self):
        monitor = self.monitor()
        raw = page(tweet(20))
        raw['errors'] = [{'code': 999, 'message': 'SECRET'}]
        self.fetch.return_value = raw
        monitor.poll_once()
        state = monitor.status()
        self.assertEqual(state['delivery']['pending'], 0)
        self.assertIsNotNone(state['targets'][0]['last_error'])
        self.assertNotIn('SECRET', json.dumps(state))

    def test_auth_expiry_stops_without_login_or_retry(self):
        monitor = self.monitor()
        self.fetch.side_effect = GraphQLError('UserOriginalsTimeline', [{'code': 32}])
        with self.assertRaises(XClientError) as error:
            monitor.poll_once()
        self.assertEqual(error.exception.code, 'auth_expired')
        self.assertEqual(self.fetch.call_count, 1)

    def test_rate_limit_cools_down_all_targets_and_survives_restart(self):
        self.client.users.get.side_effect = [{'user_id': '123'}, {'user_id': '456'}]
        monitor = UserPostMonitor(self.client, ['alice', 'bob'], clock=lambda: self.now, max_rps=20)
        self.addCleanup(monitor.close)
        exc = RuntimeError('SECRET url')
        exc.response = SimpleNamespace(status_code=429, headers={'retry-after': '600'})
        self.fetch.side_effect = exc
        monitor.poll_once()
        self.assertEqual(self.fetch.call_count, 1)
        monitor.close()
        monitor = self.monitor()
        self.advance(100)
        monitor.poll_once()
        self.assertEqual(self.fetch.call_count, 1)

    def test_callback_failure_retry_dead_letter_and_explicit_replay(self):
        monitor = self.monitor(max_attempts=2)
        self.fetch.return_value = page(tweet(1))
        monitor.poll_once()
        handler = Mock(side_effect=RuntimeError('SECRET'))
        monitor.deliver_once(handler)
        monitor.deliver_once(handler)
        self.assertEqual(handler.call_count, 1)
        self.advance(20)
        monitor.deliver_once(handler)
        self.assertEqual(monitor.status()['delivery']['failed'], 1)
        monitor.retry_failed()
        received = []
        monitor.deliver_once(received.append)
        self.assertEqual(len(received), 1)
        self.assertNotIn('SECRET', monitor.path.read_bytes().decode(errors='ignore'))

    def test_process_lock_prevents_double_worker(self):
        self.monitor()
        with self.assertRaises(XClientError) as error:
            self.monitor()
        self.assertEqual(error.exception.code, 'monitor_busy')

    def test_files_private_and_accounts_isolated(self):
        monitor = self.monitor()
        self.assertEqual(monitor.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(monitor.path.parent.stat().st_mode & 0o777, 0o700)
        self.client.users.me.return_value = {'user_id': '888'}
        other = self.monitor()
        self.assertNotEqual(other.path, monitor.path)

    def test_delivery_does_not_block_collection_and_stop_is_graceful(self):
        monitor = self.monitor()
        self.fetch.return_value = page(tweet(1))
        monitor.poll_once()
        entered, release, stop = threading.Event(), threading.Event(), threading.Event()
        def handler(event):
            entered.set()
            release.wait(5)
        self.advance()
        self.fetch.return_value = page(tweet(2), tweet(1))
        collected = threading.Event()
        def report(state):
            if state['delivery']['pending'] == 2:
                collected.set()
        runner = threading.Thread(target=monitor.run, args=(handler,), kwargs={'stop': stop, 'on_status': report})
        runner.start()
        try:
            self.assertTrue(entered.wait(2))
            deadline = time.monotonic() + 2
            while monitor.status()['delivery']['pending'] < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(monitor.status()['delivery']['pending'], 2)
        finally:
            stop.set()
            release.set()
            runner.join(5)
        self.assertFalse(runner.is_alive())


class ParserTests(unittest.TestCase):
    def test_pin_social_context_cannot_terminate_scan(self):
        raw = page(tweet(-1000))
        raw['data']['user']['result']['timeline_v2']['timeline']['instructions'][0]['entries'][0]['content']['itemContent']['socialContext'] = {'contextType': 'Pin'}
        posts, ordered, _ = parse_user_page(raw, '123')
        self.assertEqual(len(posts), 1)
        self.assertEqual(ordered, [])

    def test_filter_foreign_reply_repost_ads_but_keep_quotes(self):
        quote = tweet(1)
        quote['quoted_status_result'] = {'result': tweet(2, '456')}
        raw = page(quote, tweet(3, '456'), tweet(4, in_reply_to_status_id_str='1'),
                   tweet(5, retweeted_status_result={'result': tweet(-5)}))
        posts, _, _ = parse_user_page(raw, '123')
        self.assertEqual([p['work_id'] for p in posts], [quote['rest_id']])
        raw['data']['user']['result']['timeline_v2']['timeline']['instructions'][0]['entries'][0]['content']['itemContent']['promotedMetadata'] = {'x': 1}
        self.assertEqual(parse_user_page(raw, '123')[0], [])

    def test_missing_timeline_is_an_error(self):
        with self.assertRaises(XClientError):
            parse_user_page({'data': {}}, '123')

    def test_modules_and_visibility_wrapper(self):
        post = tweet(1)
        raw = page()
        item = {'tweet_results': {'result': {'__typename': 'TweetWithVisibilityResults', 'tweet': post}}}
        raw['data']['user']['result']['timeline_v2']['timeline']['instructions'][0]['entries'] = [
            {'entryId': 'module', 'content': {'items': [{'item': {'itemContent': item}}]}}]
        self.assertEqual(parse_user_page(raw, '123')[0][0]['work_id'], post['rest_id'])


class WebhookTests(unittest.TestCase):
    def test_signature_no_redirect_no_cookie_no_proxy(self):
        webhook = Webhook('https://example.com/events', 'secret')
        event = {'event_id': 'stable', 'post': {'title': '你好'}}
        with patch('requests.Session') as constructor, patch('x_apis.user_monitor.time.time', return_value=123):
            session = constructor.return_value.__enter__.return_value
            session.post.return_value.status_code = 204
            webhook(event)
            kwargs = session.post.call_args.kwargs
            signature = hmac.new(b'secret', b'123.' + kwargs['data'], hashlib.sha256).hexdigest()
            self.assertEqual(kwargs['headers']['X-XApis-Signature'], 'sha256=' + signature)
            self.assertFalse(kwargs['allow_redirects'])
            self.assertFalse(session.trust_env)
            self.assertNotIn('cookies', kwargs)
            self.assertNotIn('Authorization', kwargs['headers'])

    def test_non_success_is_not_acknowledged(self):
        with patch('requests.Session') as constructor:
            constructor.return_value.__enter__.return_value.post.return_value.status_code = 302
            with self.assertRaises(XClientError):
                Webhook('https://example.com/events', 'secret')({'event_id': 'id'})


class GuestSourceTests(unittest.TestCase):
    def test_cli_guest_avoids_xclient_initialization(self):
        from main import build_parser, cmd_watch_users
        args = build_parser().parse_args(['watch-users', 'alice', '--guest', '--once'])
        state = {'source_id': 'guest', 'targets': [{'last_error': None}],
                 'delivery': {'pending': 0, 'failed': 0}}
        with patch('x_apis.XClient.from_env') as cookie_client, \
             patch('x_apis.UserPostMonitor.guest') as guest, redirect_stderr(io.StringIO()):
            guest.return_value.deliver_once.return_value = state
            guest.return_value.__enter__.return_value = guest.return_value
            self.assertEqual(cmd_watch_users(None, args), 0)
            cookie_client.assert_not_called()
            guest.assert_called_once()


if __name__ == '__main__':
    unittest.main()
