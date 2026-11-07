"""持久化轮询用户原创及引用推文，不订阅通知或私信。

采集和投递分别保存处理进度。回调成功返回表示事件已确认；
若确认写入前进程崩溃，该事件可能在重启后再次投递。
"""
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import random
import re
import sqlite3
import threading
import time
from urllib.parse import urlparse

from utils.data_util import handle_work_info
from utils.x_util import parse_screen_name
from x_apis.client_errors import XClientError
from x_apis.errors import GraphQLError
from x_apis import monitor_metrics as metrics
from x_apis.user_monitor_sources import CookieTimelineSource, GuestTimelineSource

X_EPOCH_MS = 1288834974657


def _snowflake(timestamp):
    return max(0, int(timestamp * 1000) - X_EPOCH_MS) << 22


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _failure(exc, now):
    """仅提取安全的错误分类，不保存或打印原始网络异常、URL、请求头及 Cookie。"""
    response = getattr(exc, 'response', None)
    status = getattr(response, 'status_code', None)
    errors = exc.errors if isinstance(exc, GraphQLError) else []
    codes = {str(e.get('code')) for e in errors if isinstance(e, dict)}
    if status == 401 or codes & {'32', '89', '215'}:
        return 'auth_expired', 0
    if status == 429 or codes & {'88'}:
        headers = getattr(response, 'headers', {}) or {}
        delay = 60.0
        try:
            retry = headers.get('retry-after')
            if retry:
                try:
                    delay = max(delay, float(retry))
                except ValueError:
                    delay = max(delay, parsedate_to_datetime(retry).timestamp() - now)
            delay = max(delay, float(headers.get('x-rate-limit-reset', now)) - now)
        except (TypeError, ValueError, OverflowError):
            pass
        return 'rate_limited', delay if math.isfinite(delay) else 900
    if codes & {'64', '226', '326', '344'}:
        return 'source_restricted', 900
    if status == 403 or codes & {'50', '63', '136', '179'}:
        return 'target_restricted', 900
    if status == 404:
        return 'endpoint_unavailable', 900
    if isinstance(exc, XClientError):
        return exc.code, 0
    return 'upstream_failed', 0


def parse_user_page(raw, user_id):
    """仅解析目标用户的时间线条目，不递归提取引用内容或推荐推文。

    置顶条目可以产生事件，但不能作为终止翻页的依据。
    当前版本主动排除回复和转帖。
    """
    if not isinstance(raw, dict):
        raise XClientError('invalid_response', '用户时间线响应无效')
    if raw.get('errors'):
        raise GraphQLError('UserOriginalsTimeline', raw['errors'])
    result = ((raw.get('data') or {}).get('user') or {}).get('result') or {}
    timeline = result.get('timeline_v2') or result.get('timeline') or {}
    timeline = timeline.get('timeline', timeline)
    instructions = timeline.get('instructions')
    if not isinstance(instructions, list):
        raise XClientError('timeline_unavailable', '用户时间线不可访问或缺少必要字段')
    posts, ordered_ids, next_cursor = {}, [], None

    def consume(item, pinned=False):
        if not isinstance(item, dict) or item.get('promotedMetadata'):
            return
        pinned = pinned or (item.get('socialContext') or {}).get('contextType') == 'Pin'
        tweet = (item.get('tweet_results') or {}).get('result')
        if not isinstance(tweet, dict):
            return
        if tweet.get('__typename') == 'TweetWithVisibilityResults':
            tweet = tweet.get('tweet') or {}
        if tweet.get('__typename') in ('TweetTombstone', 'TweetUnavailable'):
            return
        legacy = tweet.get('legacy') or {}
        user = (((tweet.get('core') or {}).get('user_results') or {}).get('result') or {})
        uid = user.get('rest_id')
        post_id = tweet.get('rest_id') or legacy.get('id_str')
        if not str(post_id or '').isdigit() or not uid:
            raise XClientError('invalid_response', '时间线推文缺少标识或作者')
        if str(uid) != str(user_id):
            return
        if not pinned:
            ordered_ids.append(int(post_id))
        if legacy.get('in_reply_to_status_id_str') or legacy.get('retweeted_status_result'):
            return
        posts[str(post_id)] = handle_work_info(tweet)

    for instruction in instructions:
        if not isinstance(instruction, dict):
            raise XClientError('invalid_response', '时间线指令无效')
        entries = list(instruction.get('entries') or [])
        if instruction.get('entry'):
            entries.append(instruction['entry'])
        for entry in entries:
            content = entry.get('content') or {}
            pinned = instruction.get('type') == 'TimelinePinEntry'
            if content.get('cursorType') == 'Bottom':
                next_cursor = content.get('value')
                continue
            if content.get('promotedMetadata'):
                continue
            consume(content.get('itemContent'), pinned)
            for child in content.get('items') or []:
                consume((child.get('item') or {}).get('itemContent'), pinned)
    if next_cursor is not None and not isinstance(next_cursor, str):
        raise XClientError('invalid_response', '时间线分页游标无效')
    return list(posts.values()), ordered_ids, next_cursor or None


class Webhook:
    """可选的签名 Webhook 投递，仅访问显式配置的地址，不跟随重定向或携带 X Cookie。"""
    def __init__(self, url, secret, *, timeout=10):
        parsed = urlparse(url)
        if (parsed.scheme != 'https' and not (
                parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost', '::1'))):
            raise ValueError('Webhook 必须使用 HTTPS（本机调试允许 HTTP）')
        if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError('Webhook URL 无效')
        if not isinstance(secret, str) or not secret:
            raise ValueError('Webhook 需要签名密钥')
        if not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise ValueError('Webhook timeout 必须为 0 到 30 秒')
        self.url, self.secret, self.timeout = url, secret.encode(), timeout

    def __call__(self, event):
        import requests
        body = json.dumps(event, ensure_ascii=False, separators=(',', ':')).encode()
        timestamp = str(int(time.time()))
        signature = hmac.new(self.secret, timestamp.encode() + b'.' + body, hashlib.sha256).hexdigest()
        try:
            with requests.Session() as session:
                # 不读取 netrc 凭证，也不沿用 X 账号的代理配置。
                session.trust_env = False
                response = session.post(self.url, data=body, timeout=self.timeout,
                                        allow_redirects=False, stream=True, headers={
                                            'Content-Type': 'application/json',
                                            'X-XApis-Event-Id': event['event_id'],
                                            'X-XApis-Timestamp': timestamp,
                                            'X-XApis-Signature': 'sha256=' + signature,
                                        })
                accepted = 200 <= response.status_code < 300
                response.close()
                if not accepted:
                    raise XClientError('webhook_failed', 'Webhook 未确认接收')
        except XClientError:
            raise
        except Exception:
            raise XClientError('webhook_failed', 'Webhook 投递未完成') from None


class _Deferred(Exception):
    """停止或来源冷却时取消尚未发出的请求，不计为目标失败。"""


class UserPostMonitor:
    """首页检查与历史补拉独立调度，事件和进度持久化到来源独立的状态库。"""
    def __init__(self, client, usernames, *, interval=60, max_pages=5,
                 overlap=300, max_attempts=10, clock=time.time, source=None, state_dir=None,
                 concurrency=3, max_rps=1, scan_page_limit=20, rescan_interval=300):
        if isinstance(usernames, str):
            usernames = [usernames]
        names = list(dict.fromkeys(parse_screen_name(name) for name in usernames))
        if not names or any(not re.fullmatch(r'[a-z0-9_]{1,15}', name) for name in names):
            raise ValueError('请提供至少一个有效用户名')
        for name, value, lower, upper in (
                ('interval', interval, 3, 86400), ('max_rps', max_rps, 0.01, 20),
                ('rescan_interval', rescan_interval, 10, 86400)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lower <= value <= upper:
                raise ValueError(f'{name} 超出允许范围')
        for name, value, lower, upper in (
                ('max_pages', max_pages, 1, 100), ('overlap', overlap, 0, 86400),
                ('max_attempts', max_attempts, 1, 100), ('concurrency', concurrency, 2, 16),
                ('scan_page_limit', scan_page_limit, 2, 10000)):
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f'{name} 超出允许范围')
        self.client, self.clock = client, clock
        self.interval, self.max_pages, self.overlap = interval, max_pages, overlap
        self.max_attempts, self.max_rps = max_attempts, max_rps
        self.concurrency, self.scan_page_limit = concurrency, scan_page_limit
        self.rescan_interval = rescan_interval
        self._mutex = threading.RLock()
        self._poll_mutex, self._delivery_mutex = threading.Lock(), threading.Lock()
        self._source_mutex = threading.Lock()
        self._gate = threading.Condition()
        self._next_request = 0
        self._head_waiters = 0
        self._delivery_wake = threading.Event()
        self._db = self._lock_file = self._head_pool = self._backfill_pool = None
        self.source = source if source is not None else CookieTimelineSource(client)
        self.account_id = str(self.source.scope)
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', self.account_id):
            raise ValueError('数据来源 scope 必须为稳定且不含凭证的标识')
        folder = Path(state_dir if state_dir is not None else client.state_dir) / self.account_id / 'user-monitor'
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        folder.chmod(0o700)
        try:
            import fcntl
            fd = os.open(folder / 'worker.lock', os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            self._lock_file = os.fdopen(fd, 'a')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise XClientError('monitor_busy', '该采集来源已有监听进程') from None
            self.path = folder / 'state.sqlite3'
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            os.fchmod(fd, 0o600)
            os.close(fd)
            self._db = sqlite3.connect(self.path, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS targets (
                    user_id TEXT PRIMARY KEY, username TEXT NOT NULL,
                    baseline TEXT NOT NULL, highwater TEXT NOT NULL,
                    cursor TEXT, scan_floor TEXT, scan_high TEXT,
                    next_check REAL NOT NULL DEFAULT 0,
                    last_success REAL, failures INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT);
                CREATE TABLE IF NOT EXISTS scan_cursors (
                    user_id TEXT NOT NULL, cursor TEXT NOT NULL,
                    PRIMARY KEY (user_id, cursor));
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                    payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                    last_error TEXT, delivered_at REAL);
                CREATE INDEX IF NOT EXISTS delivery_queue ON events(state, next_attempt);
                CREATE INDEX IF NOT EXISTS target_events ON events(user_id, state);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            # 原有断点和事件原地保留；新列只补默认值，不重建历史表。
            additions = {'head_highwater': "TEXT NOT NULL DEFAULT '0'", 'backfill_next': 'REAL NOT NULL DEFAULT 0',
                         'backfill_error': 'TEXT', 'backfill_failures': 'INTEGER NOT NULL DEFAULT 0',
                         'scan_pages': 'INTEGER NOT NULL DEFAULT 0', 'coverage': "TEXT NOT NULL DEFAULT 'unverified'",
                         'last_scan_completed': 'REAL'}
            columns = {row['name'] for row in self._db.execute('PRAGMA table_info(targets)')}
            with self._db:
                for name, definition in additions.items():
                    if name not in columns:
                        self._db.execute(f'ALTER TABLE targets ADD COLUMN {name} {definition}')
                metrics.ensure_schema(self._db)
            self.user_ids = []
            for name in names:
                row = self._db.execute('SELECT user_id FROM targets WHERE username=?', (name,)).fetchone()
                uid = row['user_id'] if row else str(self.source.resolve(name))
                if not uid.isdigit():
                    raise XClientError('invalid_response', '监听用户标识无效')
                baseline = str(_snowflake(self.clock()))
                with self._db:
                    self._db.execute('INSERT OR IGNORE INTO targets(user_id,username,baseline,highwater) VALUES(?,?,?,?)',
                                     (uid, name, baseline, baseline))
                if uid not in self.user_ids:
                    self.user_ids.append(uid)
            # 留一个补拉槽，其余专门用于首页；慢历史请求不会占满首页工作线程。
            self._head_pool = ThreadPoolExecutor(max_workers=concurrency - 1, thread_name_prefix='xapis-head')
            self._backfill_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='xapis-backfill')
        except BaseException:
            self.close()
            raise

    @classmethod
    def from_source(cls, source, usernames, *, state_dir='.xapis', **options):
        """接入 scope/resolve/fetch_page 来源。并发来源需显式声明 thread_safe=True。

        未声明 chronological=True 的来源不按一页旧内容提前结束补拉。
        close() 可选，状态库关闭前会等待工作线程并释放来源连接。
        """
        return cls(None, usernames, source=source, state_dir=state_dir, **options)

    @classmethod
    def guest(cls, usernames, *, proxy=None, state_dir='.xapis', **options):
        """显式使用访客 token，不读取登录 Cookie，也不自动切换账号。"""
        return cls.from_source(GuestTimelineSource(proxy=proxy), usernames, state_dir=state_dir, **options)

    @contextmanager
    def _transaction(self):
        with self._mutex, self._db:
            yield self._db

    def close(self):
        for name in ('_head_pool', '_backfill_pool'):
            pool = getattr(self, name, None)
            if pool is not None:
                pool.shutdown(wait=True, cancel_futures=True)
                setattr(self, name, None)
        if self._db is not None:
            self._db.close()
            self._db = None
        if self._lock_file is not None:
            self._lock_file.close()
            self._lock_file = None
        source_close = getattr(self.source, 'close', None)
        if source_close:
            source_close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _target(self, uid):
        with self._transaction() as db:
            return dict(db.execute('SELECT * FROM targets WHERE user_id=?', (uid,)).fetchone())

    def _cooldown(self):
        with self._transaction() as db:
            row = db.execute("SELECT value FROM settings WHERE key='cooldown'").fetchone()
        return float(row['value']) if row else 0

    def _permit(self, kind, stop):
        """所有页读取共用速率预算，等待中的首页优先于补拉；冷却不忙等请求。"""
        with self._gate:
            self._head_waiters += kind == 'head'
            try:
                while True:
                    if stop.is_set() or self._cooldown() > self.clock():
                        raise _Deferred()
                    delay = self._next_request - time.monotonic()
                    if delay <= 0 and (kind == 'head' or self._head_waiters == 0):
                        self._next_request = time.monotonic() + 1 / self.max_rps
                        return
                    self._gate.wait(min(max(delay, 0.01), 0.1))
            finally:
                self._head_waiters -= kind == 'head'
                self._gate.notify_all()

    def _fetch(self, uid, cursor, kind, stop):
        # 先等待非线程安全来源的使用权，再领取请求预算，避免排队后集中连发。
        guard = nullcontext() if getattr(self.source, 'thread_safe', False) is True else self._source_mutex
        with guard:
            self._permit(kind, stop)
            started = time.monotonic()
            ok = False
            try:
                result = self.source.fetch_page(uid, cursor)
                ok = True
                return result
            finally:
                with self._transaction() as db:
                    metrics.increment(db, 'requests')
                    metrics.increment(db, 'requests_ok' if ok else 'requests_failed')
                    metrics.increment(db, kind + '_requests')
                    metrics.record(db, 'request_ms', (time.monotonic() - started) * 1000, self.clock())

    def _store_events(self, db, uid, posts, baseline, now, kind):
        for post in sorted(posts, key=lambda p: int(p['work_id'])):
            # 仅以首次注册起点过滤历史，避免已推进水位排除延迟出现的旧 ID。
            if int(post['work_id']) <= int(baseline):
                continue
            event_id = f"x:post.created:{self.account_id}:{uid}:{post['work_id']}"
            event = {'event_id': event_id, 'type': 'post.created', 'source_id': self.account_id,
                     'user_id': uid, 'detected_at': _iso(now), 'discovered_via': kind, 'post': post}
            inserted = db.execute('INSERT OR IGNORE INTO events(event_id,user_id,payload) VALUES(?,?,?)',
                                  (event_id, uid, json.dumps(event, ensure_ascii=False))).rowcount
            if inserted:
                metrics.increment(db, 'events_detected')
                metrics.increment(db, kind + '_events')
                published = (int(post['work_id']) >> 22) + X_EPOCH_MS
                if published <= now * 1000:
                    metrics.record(db, 'publish_to_detect_ms', now * 1000 - published, now)
                else:
                    metrics.increment(db, 'clock_skew_samples')

    def _head(self, uid, stop):
        started = self.clock()
        posts, ids, cursor = self._fetch(uid, None, 'head', stop)
        now = self.clock()
        with self._transaction() as db:
            target = dict(db.execute('SELECT * FROM targets WHERE user_id=?', (uid,)).fetchone())
            self._store_events(db, uid, posts, target['baseline'], now, 'head')
            high = max([int(target['head_highwater'])] + ids + [int(p['work_id']) for p in posts])
            db.execute('UPDATE targets SET head_highwater=?,next_check=?,last_success=?,failures=0,last_error=NULL WHERE user_id=?',
                       (str(high), max(now, started + self.interval), now, uid))
            if target['last_success'] is not None:
                metrics.record(db, 'head_check_gap_ms', max(0, now - target['last_success']) * 1000, now)
            fresh_head = high > int(target['head_highwater']) and not target['backfill_error']
            if target['cursor'] is None and (target['backfill_next'] <= now or fresh_head):
                if cursor:
                    floor = int(target['baseline'])
                    if getattr(self.source, 'chronological', False) is True:
                        floor = max(floor, int(target['highwater']) - self.overlap * 1000 * (1 << 22))
                    db.execute('DELETE FROM scan_cursors WHERE user_id=?', (uid,))
                    db.execute('INSERT INTO scan_cursors VALUES(?,?)', (uid, cursor))
                    db.execute("UPDATE targets SET cursor=?,scan_floor=?,scan_high=?,scan_pages=1,coverage='scanning',backfill_next=? WHERE user_id=?",
                               (cursor, str(floor), str(high), now, uid))
                else:
                    db.execute("UPDATE targets SET coverage='complete',highwater=?,last_scan_completed=?,backfill_next=? WHERE user_id=?",
                               (str(max(high, int(target['highwater']))), now, now + self.rescan_interval, uid))
        self._delivery_wake.set()

    def _backfill(self, uid, stop):
        for _ in range(self.max_pages):
            target = self._target(uid)
            if not target['cursor'] or stop.is_set():
                return
            posts, ids, next_cursor = self._fetch(uid, target['cursor'], 'backfill', stop)
            now = self.clock()
            pages = target['scan_pages'] + 1
            floor = int(target['scan_floor'] or target['baseline'])
            done = not next_cursor
            if getattr(self.source, 'chronological', False) is True:
                done = done or bool(ids and max(ids) <= floor)
            bounded = not done and pages >= self.scan_page_limit
            high = max([int(target['scan_high'] or target['highwater'])] + ids + [int(p['work_id']) for p in posts])
            with self._transaction() as db:
                if not done and db.execute('SELECT 1 FROM scan_cursors WHERE user_id=? AND cursor=?', (uid, next_cursor)).fetchone():
                    raise XClientError('cursor_stalled', '补拉游标循环，未完成的覆盖范围需重新检查')
                self._store_events(db, uid, posts, target['baseline'], now, 'backfill')
                if done or bounded:
                    db.execute('UPDATE targets SET cursor=NULL,scan_floor=NULL,scan_high=NULL,scan_pages=?, '
                               'backfill_next=?,backfill_failures=0,backfill_error=NULL,coverage=? WHERE user_id=?',
                               (pages, now + self.rescan_interval, 'complete' if done else 'bounded', uid))
                    if done:
                        db.execute('UPDATE targets SET highwater=?,last_scan_completed=? WHERE user_id=?', (str(high), now, uid))
                    db.execute('DELETE FROM scan_cursors WHERE user_id=?', (uid,))
                else:
                    db.execute('INSERT INTO scan_cursors VALUES(?,?)', (uid, next_cursor))
                    db.execute('UPDATE targets SET cursor=?,scan_high=?,scan_pages=?,backfill_next=?,backfill_failures=0,backfill_error=NULL WHERE user_id=?',
                               (next_cursor, str(high), pages, now + self.interval, uid))
            self._delivery_wake.set()
            if done or bounded:
                return

    def _job(self, uid, kind, stop):
        try:
            if kind == 'head':
                self._head(uid, stop)
            else:
                self._backfill(uid, stop)
        except _Deferred:
            return
        except sqlite3.Error:
            raise XClientError('storage_failed', '监听状态无法持久化，停止采集') from None
        except Exception as exc:
            code, minimum = _failure(exc, self.clock())
            target = self._target(uid)
            failures = target['failures' if kind == 'head' else 'backfill_failures']
            delay = max(minimum, min(900, self.interval * 2 ** min(failures + 1, 8))) * random.uniform(1, 1.1)
            with self._transaction() as db:
                if kind == 'head':
                    db.execute('UPDATE targets SET next_check=?,failures=failures+1,last_error=? WHERE user_id=?',
                               (self.clock() + delay, code, uid))
                else:
                    db.execute('UPDATE targets SET backfill_next=?,backfill_failures=backfill_failures+1,backfill_error=? WHERE user_id=?',
                               (self.clock() + delay, code, uid))
                metrics.increment(db, 'error_' + code)
                if code in ('rate_limited', 'source_restricted', 'endpoint_unavailable'):
                    previous = db.execute("SELECT value FROM settings WHERE key='cooldown'").fetchone()
                    cooldown = max(self.clock() + delay, float(previous['value']) if previous else 0)
                    db.execute("INSERT OR REPLACE INTO settings VALUES('cooldown',?)", (str(cooldown),))
                if kind == 'backfill' and code in ('cursor_stalled', 'invalid_response', 'timeline_unavailable'):
                    db.execute("UPDATE targets SET cursor=NULL,scan_floor=NULL,scan_high=NULL,coverage='gap' WHERE user_id=?", (uid,))
                    db.execute('DELETE FROM scan_cursors WHERE user_id=?', (uid,))
            if code in ('auth_expired', 'guest_auth_failed'):
                stop.set()
                raise XClientError(code, '采集来源鉴权失效，断点已保留') from None

    def poll_once(self, *, stop=None):
        """并发检查到期首页，然后执行每目标有限页数的补拉；持续监听请使用 run。"""
        stop = stop or threading.Event()
        with self._poll_mutex:
            if self._cooldown() > self.clock():
                return self.status()
            tails = []
            heads = [self._head_pool.submit(self._job, uid, 'head', stop) for uid in self.user_ids
                     if self._target(uid)['next_check'] <= self.clock() and not stop.is_set()]
            try:
                for future in heads:
                    future.result()
                tails = [self._backfill_pool.submit(self._job, uid, 'backfill', stop) for uid in self.user_ids
                         if self._target(uid)['cursor'] and self._target(uid)['backfill_next'] <= self.clock() and not stop.is_set()]
                for future in tails:
                    future.result()
            except BaseException:
                stop.set()
                wait(heads + tails)
                raise
        return self.status()

    def deliver_once(self, handler, *, limit=100, stop=None):
        """回调返回确认后才标记完成；失败持久化退避，接收方按 event_id 幂等。"""
        stop = stop or threading.Event()
        with self._delivery_mutex:
            placeholders = ','.join('?' for _ in self.user_ids)
            with self._transaction() as db:
                rows = db.execute(f"SELECT * FROM events WHERE state='pending' AND next_attempt<=? "
                                  f'AND user_id IN ({placeholders}) ORDER BY rowid LIMIT ?',
                                  (self.clock(), *self.user_ids, limit)).fetchall()
            for row in rows:
                if stop.is_set():
                    break
                event = json.loads(row['payload'])
                try:
                    if handler(event) is False:
                        raise RuntimeError('未确认接收')
                except Exception:
                    attempts = row['attempts'] + 1
                    state = 'failed' if attempts >= self.max_attempts else 'pending'
                    with self._transaction() as db:
                        db.execute('UPDATE events SET attempts=?,state=?,next_attempt=?,last_error=? WHERE event_id=?',
                                   (attempts, state, self.clock() + min(900, 5 * 2 ** min(attempts, 8)), 'delivery_failed', row['event_id']))
                        metrics.increment(db, 'delivery_failures')
                else:
                    with self._transaction() as db:
                        now = self.clock()
                        db.execute("UPDATE events SET state='delivered',delivered_at=?,last_error=NULL WHERE event_id=?", (now, row['event_id']))
                        metrics.increment(db, 'events_delivered')
                        detected = datetime.fromisoformat(event['detected_at']).timestamp()
                        metrics.record(db, 'detect_to_delivery_ms', max(0, now - detected) * 1000, now)
        return self.status()

    def retry_failed(self):
        """仅重放当前所选目标已耗尽重试的事件。"""
        with self._transaction() as db:
            for uid in self.user_ids:
                db.execute("UPDATE events SET state='pending',attempts=0,next_attempt=0,last_error=NULL WHERE state='failed' AND user_id=?", (uid,))
        self._delivery_wake.set()

    def compare_posts(self, expected):
        """与独立取得的已知推文清单比对；未观察到不自动等同于永久漏报。"""
        if not isinstance(expected, list):
            raise ValueError('对照清单必须为列表')
        result = {'eligible': 0, 'observed': 0, 'not_observed': [], 'before_start': 0}
        seen = set()
        with self._transaction() as db:
            for item in expected:
                if not isinstance(item, dict):
                    raise ValueError('对照条目必须含 user_id 和 post_id')
                uid, pid = item.get('user_id'), item.get('post_id')
                if (not isinstance(uid, str) or uid not in self.user_ids or
                        not isinstance(pid, str) or not re.fullmatch(r'[1-9][0-9]*', pid)):
                    raise ValueError('对照条目必须使用已选目标及有效推文 ID 字符串')
                if (uid, pid) in seen:
                    continue
                seen.add((uid, pid))
                baseline = db.execute('SELECT baseline FROM targets WHERE user_id=?', (uid,)).fetchone()['baseline']
                if int(pid) <= int(baseline):
                    result['before_start'] += 1
                    continue
                result['eligible'] += 1
                event_id = f'x:post.created:{self.account_id}:{uid}:{pid}'
                if db.execute('SELECT 1 FROM events WHERE event_id=?', (event_id,)).fetchone():
                    result['observed'] += 1
                else:
                    result['not_observed'].append({'user_id': uid, 'post_id': pid})
        result['observed_fraction'] = result['observed'] / result['eligible'] if result['eligible'] else None
        return result

    def status(self):
        with self._transaction() as db:
            targets = [dict(db.execute('SELECT user_id,username,highwater,head_highwater,cursor,next_check,last_success,failures,last_error, '
                                       'backfill_next,backfill_error,backfill_failures,scan_pages,coverage,last_scan_completed '
                                       'FROM targets WHERE user_id=?', (uid,)).fetchone()) for uid in self.user_ids]
            counts = {state: 0 for state in ('pending', 'delivered', 'failed')}
            for uid in self.user_ids:
                for row in db.execute('SELECT state,count(*) AS n FROM events WHERE user_id=? GROUP BY state', (uid,)):
                    counts[row['state']] += row['n']
            measured = metrics.snapshot(db)
        for target in targets:
            target['catching_up'] = bool(target.pop('cursor'))
        return {'source_id': self.account_id, 'targets': targets, 'delivery': counts,
                'metrics': measured, 'cooldown_until': self._cooldown(),
                'config': {'interval': self.interval, 'concurrency': self.concurrency, 'max_rps': self.max_rps,
                           'scan_page_limit': self.scan_page_limit, 'rescan_interval': self.rescan_interval}}

    def run(self, handler, *, stop=None, on_status=None, duration=None):
        """实时与补拉各有工作槽；入库即唤醒投递。duration 为可选运行秒数。

        回调必须自设 I/O 超时。退出等待当前请求和回调结束，保持事件确认可恢复。
        """
        if duration is not None and (not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0):
            raise ValueError('duration 必须为正数')
        stop = stop or threading.Event()
        deadline = time.monotonic() + duration if duration is not None else None
        errors = []
        active = {}

        def deliver():
            try:
                while not stop.is_set():
                    self._delivery_wake.clear()
                    self.deliver_once(handler, stop=stop)
                    self._delivery_wake.wait(1)
            except BaseException:
                errors.append(XClientError('delivery_worker_failed', '投递线程失败，未确认事件已保留'))
                stop.set()

        worker = threading.Thread(target=deliver, name='xapis-delivery', daemon=True)
        worker.start()
        next_status = 0
        try:
            with self._poll_mutex:
                while not stop.is_set():
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    for key, future in list(active.items()):
                        if future.done():
                            del active[key]
                            future.result()
                    if self._cooldown() <= self.clock():
                        targets = sorted((self._target(uid) for uid in self.user_ids), key=lambda t: t['next_check'])
                        head_slots = self.concurrency - 1 - sum(k[0] == 'head' for k in active)
                        for target in targets:
                            key = ('head', target['user_id'])
                            if head_slots and key not in active and target['next_check'] <= self.clock():
                                active[key] = self._head_pool.submit(self._job, target['user_id'], 'head', stop)
                                head_slots -= 1
                        if not any(k[0] == 'backfill' for k in active):
                            tails = sorted((t for t in targets if t['cursor'] and t['backfill_next'] <= self.clock()), key=lambda t: t['backfill_next'])
                            if tails:
                                uid = tails[0]['user_id']
                                active[('backfill', uid)] = self._backfill_pool.submit(self._job, uid, 'backfill', stop)
                    if on_status and self.clock() >= next_status:
                        on_status(self.status())
                        next_status = self.clock() + max(5, self.interval)
                    stop.wait(0.1)
        finally:
            stop.set()
            self._delivery_wake.set()
            for future in active.values():
                try:
                    future.result()
                except BaseException as exc:
                    errors.append(exc)
            worker.join()
        if errors:
            raise errors[0]
