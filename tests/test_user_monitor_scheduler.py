"""持续监听调度回归测试：仅使用可控时间和本地伪来源，不访问 X。"""
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from tests.test_user_monitor import START, page, tweet
from x_apis.user_monitor import UserPostMonitor, _snowflake, parse_user_page


class Clock:
    def __init__(self):
        self.value = START
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.value

    def advance(self, seconds):
        with self.lock:
            self.value += seconds


class Source:
    scope = 'scheduler-test'
    thread_safe = True

    def __init__(self, handler):
        self.handler = handler
        self.calls = []
        self.lock = threading.Lock()

    def resolve(self, name):
        return {'alice': '123', 'bob': '456'}[name]

    def fetch_page(self, uid, cursor):
        with self.lock:
            self.calls.append((uid, cursor))
        return parse_user_page(self.handler(uid, cursor), uid)

    def history(self):
        with self.lock:
            return list(self.calls)


def http_failure(code, **headers):
    error = RuntimeError('不可持久化的原始上游信息')
    error.response = SimpleNamespace(status_code=code, headers=headers)
    return error


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()

    def monitor(self, source, names=('alice',), **options):
        config = {'clock': self.clock, 'interval': 3, 'max_rps': 20, 'max_pages': 1}
        config.update(options)
        monitor = UserPostMonitor.from_source(source, names, state_dir=self.tmp.name, **config)
        self.addCleanup(monitor.close)
        return monitor

    def start(self, monitor, handler, releases=()):
        stop, errors = threading.Event(), []

        def run():
            try:
                monitor.run(handler, stop=stop)
            except BaseException as error:
                errors.append(error)

        runner = threading.Thread(target=run)
        runner.start()

        def finish():
            stop.set()
            for release in releases:
                release.set()
            runner.join(3)
            self.assertFalse(runner.is_alive(), '停止后监听线程应退出')
            self.assertEqual(errors, [], '监听线程不应隐藏运行异常')

        # 先终止线程和解除门闩，再由其他清理回调关闭状态库。
        self.addCleanup(finish)
        return stop, runner

    def test_head_refresh_preserves_unfinished_backfill_cursor(self):
        heads = iter([page(tweet(40), cursor='old-a'), page(tweet(50), cursor='new-a')])

        def fetch(uid, cursor):
            if cursor is None:
                return next(heads)
            return {
                'old-a': page(tweet(30), cursor='old-b'),
                'old-b': page(tweet(20), cursor='old-c'),
            }[cursor]

        source = Source(fetch)
        monitor = self.monitor(source)
        monitor.poll_once()
        self.assertEqual(monitor._target('123')['cursor'], 'old-b')
        self.clock.advance(3)
        monitor.poll_once()
        self.assertEqual(source.history(), [('123', None), ('123', 'old-a'),
                                            ('123', None), ('123', 'old-b')])
        self.assertEqual(monitor._target('123')['cursor'], 'old-c')
        self.assertEqual(monitor.status()['delivery']['pending'], 4)

    def test_blocked_backfill_does_not_delay_next_head_or_delivery(self):
        entered, release, detected = threading.Event(), threading.Event(), threading.Event()
        head_count = 0

        def fetch(uid, cursor):
            nonlocal head_count
            if cursor is not None:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('测试补拉未解除门闩')
                return page(tweet(-10))
            head_count += 1
            return page(tweet(1 if head_count == 1 else 5), cursor='history')

        monitor = self.monitor(Source(fetch))

        def handle(event):
            if event['post']['work_id'] == tweet(5)['rest_id']:
                detected.set()

        self.start(monitor, handle, releases=(release,))
        self.assertTrue(entered.wait(2), '应进入历史补拉')
        self.clock.advance(3)
        self.assertTrue(detected.wait(2), '历史补拉未完成时仍应发现并投递首页新推文')
        self.assertFalse(release.is_set())

    def test_slow_target_does_not_block_another_target_head(self):
        entered, release, detected = threading.Event(), threading.Event(), threading.Event()

        def fetch(uid, cursor):
            if uid == '123':
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('测试目标未解除门闩')
            return page(tweet(1, uid))

        monitor = self.monitor(Source(fetch), names=('alice', 'bob'), concurrency=3)

        def handle(event):
            if event['user_id'] == '456':
                detected.set()

        self.start(monitor, handle, releases=(release,))
        self.assertTrue(entered.wait(2))
        self.assertTrue(detected.wait(2), '另一个目标的首页采集和投递不应等待慢目标')
        self.assertFalse(release.is_set())

    def test_old_first_page_does_not_hide_new_post_on_next_page(self):
        source = Source(lambda uid, cursor: page(tweet(-100), cursor='reordered') if cursor is None
                        else page(tweet(1)))
        monitor = self.monitor(source)
        monitor.poll_once()
        delivered = []
        monitor.deliver_once(delivered.append)
        self.assertEqual(source.history(), [('123', None), ('123', 'reordered')])
        self.assertEqual([event['post']['work_id'] for event in delivered], [tweet(1)['rest_id']])
        self.assertEqual(monitor.status()['targets'][0]['coverage'], 'complete')

    def test_scan_limit_reports_bounded_without_advancing_complete_watermark(self):
        source = Source(lambda uid, cursor: page(tweet(40), cursor='a') if cursor is None
                        else page(tweet(30), cursor='b'))
        monitor = self.monitor(source, scan_page_limit=2)
        highwater = monitor.status()['targets'][0]['highwater']
        monitor.poll_once()
        target = monitor.status()['targets'][0]
        self.assertEqual(target['coverage'], 'bounded')
        self.assertEqual(target['scan_pages'], 2)
        self.assertEqual(target['highwater'], highwater)
        self.assertIsNone(target['last_scan_completed'])
        self.assertFalse(target['catching_up'])
        self.assertEqual(monitor.status()['delivery']['pending'], 2)

    def test_source_rate_limit_blocks_other_targets_and_survives_restart(self):
        def fetch(uid, cursor):
            raise http_failure(429, **{'retry-after': '600'})

        source = Source(fetch)
        monitor = self.monitor(source, names=('alice', 'bob'), concurrency=2)
        monitor.poll_once()
        cooldown = monitor.status()['cooldown_until']
        self.assertGreaterEqual(cooldown, START + 600)
        self.assertEqual(len(source.history()), 1)
        monitor.close()
        self.clock.advance(100)
        restored = self.monitor(source, names=('alice', 'bob'), concurrency=2)
        restored.poll_once()
        self.assertEqual(restored.status()['cooldown_until'], cooldown)
        self.assertEqual(len(source.history()), 1)

    def test_target_403_does_not_cool_down_other_targets(self):
        def fetch(uid, cursor):
            if uid == '123':
                raise http_failure(403)
            return page(tweet(1, uid))

        source = Source(fetch)
        monitor = self.monitor(source, names=('alice', 'bob'), concurrency=2)
        monitor.poll_once()
        targets = {target['user_id']: target for target in monitor.status()['targets']}
        self.assertEqual(targets['123']['last_error'], 'target_restricted')
        self.assertIsNone(targets['456']['last_error'])
        self.assertEqual(monitor.status()['cooldown_until'], 0)
        self.assertEqual(monitor.status()['delivery']['pending'], 1)
        self.clock.advance(3)
        monitor.poll_once()
        self.assertEqual(source.history().count(('456', None)), 2)
        self.assertEqual(source.history().count(('123', None)), 1)

    def test_old_schema_migration_retains_events_and_continues_saved_cursor(self):
        folder = Path(self.tmp.name) / Source.scope / 'user-monitor'
        folder.mkdir(parents=True)
        baseline = str(_snowflake(START))
        payload = {
            'event_id': 'legacy-event', 'type': 'post.created', 'source_id': Source.scope,
            'user_id': '123', 'detected_at': datetime.fromtimestamp(START, timezone.utc).isoformat(),
            'post': {'work_id': tweet(30)['rest_id']},
        }
        db = sqlite3.connect(folder / 'state.sqlite3')
        with db:
            db.execute('''CREATE TABLE targets (
                user_id TEXT PRIMARY KEY, username TEXT NOT NULL,
                baseline TEXT NOT NULL, highwater TEXT NOT NULL,
                cursor TEXT, scan_floor TEXT, scan_high TEXT,
                next_check REAL NOT NULL DEFAULT 0, last_success REAL,
                failures INTEGER NOT NULL DEFAULT 0, last_error TEXT)''')
            db.execute('''CREATE TABLE scan_cursors (
                user_id TEXT NOT NULL, cursor TEXT NOT NULL, PRIMARY KEY(user_id, cursor))''')
            db.execute('''CREATE TABLE events (
                event_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, payload TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT, delivered_at REAL)''')
            db.execute('''INSERT INTO targets(user_id, username, baseline, highwater,
                cursor, scan_floor, scan_high, next_check, last_success)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                       ('123', 'alice', baseline, baseline, 'legacy-next', baseline,
                        tweet(40)['rest_id'], START + 30, START))
            db.execute('INSERT INTO scan_cursors VALUES (?, ?)', ('123', 'legacy-next'))
            db.execute('INSERT INTO events(event_id, user_id, payload) VALUES (?, ?, ?)',
                       ('legacy-event', '123', json.dumps(payload)))
        db.close()

        source = Source(lambda uid, cursor: page(tweet(20)))
        monitor = self.monitor(source)
        self.assertEqual(monitor.status()['delivery']['pending'], 1)
        self.assertEqual(monitor._target('123')['cursor'], 'legacy-next')
        self.assertEqual(monitor._target('123')['baseline'], baseline)
        monitor.poll_once()
        self.assertEqual(source.history(), [('123', 'legacy-next')])
        self.assertEqual(monitor.status()['targets'][0]['coverage'], 'complete')
        delivered = []
        monitor.deliver_once(delivered.append)
        self.assertEqual({event['post']['work_id'] for event in delivered},
                         {tweet(20)['rest_id'], tweet(30)['rest_id']})
        self.assertIn('legacy-event', {event['event_id'] for event in delivered})

    def test_late_indexed_post_older_than_overlap_is_still_inserted(self):
        responses = iter([page(tweet(900)), page(tweet(900), tweet(20), tweet(-1))])
        monitor = self.monitor(Source(lambda uid, cursor: next(responses)), overlap=300)
        self.clock.advance(1000)
        monitor.poll_once()
        self.clock.advance(1000)
        monitor.poll_once()
        delivered = []
        monitor.deliver_once(delivered.append)
        self.assertEqual({event['post']['work_id'] for event in delivered},
                         {tweet(20)['rest_id'], tweet(900)['rest_id']})

    def test_already_stopped_run_sends_no_requests(self):
        source = Source(lambda uid, cursor: page(tweet(1)))
        monitor = self.monitor(source)
        stop = threading.Event()
        stop.set()
        monitor.run(lambda event: None, stop=stop)
        monitor.poll_once(stop=stop)
        self.assertEqual(source.history(), [])

    def test_duration_exits_and_stop_prevents_subsequent_requests(self):
        source = Source(lambda uid, cursor: page(tweet(1)))
        monitor = self.monitor(source)
        stop = threading.Event()
        started = time.monotonic()
        monitor.run(lambda event: None, stop=stop, duration=0.2)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.18)
        self.assertLess(elapsed, 2)
        self.assertTrue(stop.is_set())
        self.assertEqual(len(source.history()), 1)
        self.clock.advance(10)
        monitor.poll_once(stop=stop)
        self.assertEqual(len(source.history()), 1)

    def test_request_discovery_delivery_metrics_survive_restart(self):
        source = Source(lambda uid, cursor: page(tweet(20), cursor='a') if cursor is None
                        else page(tweet(10)))
        monitor = self.monitor(source)
        self.clock.advance(60)
        monitor.poll_once()
        self.clock.advance(2)
        monitor.deliver_once(lambda event: None)
        measured = monitor.status()['metrics']
        self.assertEqual(measured['counters']['requests'], 2)
        self.assertEqual(measured['counters']['requests_ok'], 2)
        self.assertEqual(measured['counters']['events_detected'], 2)
        self.assertEqual(measured['counters']['events_delivered'], 2)
        for kind in ('request_ms', 'publish_to_detect_ms', 'detect_to_delivery_ms'):
            self.assertEqual(measured['samples'][kind]['count'], 2)
            self.assertGreaterEqual(measured['samples'][kind]['p95'], 0)
        self.assertAlmostEqual(measured['samples']['publish_to_detect_ms']['p50'], 45000)
        self.assertAlmostEqual(measured['samples']['detect_to_delivery_ms']['p50'], 2000)
        monitor.close()
        restored = self.monitor(source)
        self.assertEqual(restored.status()['metrics'], measured)
        self.assertEqual(restored.status()['delivery']['delivered'], 2)


if __name__ == '__main__':
    unittest.main()
