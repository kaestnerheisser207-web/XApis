"""Durable observations, incident deduplication and a small notification outbox."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid


ISSUES = {'changed', 'incomplete', 'auth_expired', 'rate_limited', 'network_error',
          'contract_error', 'sample_unavailable', 'not_exercised', 'needs_input',
          'unconfirmed', 'cleanup_failed'}


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def secure_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


@contextmanager
def process_lock(state_dir):
    # flock is released by the OS after crashes; never delete another worker's lock.
    import fcntl
    root = secure_dir(state_dir)
    with (root / 'worker.lock').open('a') as stream:
        os.chmod(stream.name, 0o600)
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('此状态目录已有巡检进程运行') from None
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class Store:
    def __init__(self, state_dir):
        self.root = secure_dir(state_dir)
        self.db = sqlite3.connect(self.root / 'state.sqlite3', timeout=10)
        os.chmod(self.root / 'state.sqlite3', 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS checks(
                check_id TEXT PRIMARY KEY, result TEXT NOT NULL, checked_at REAL NOT NULL,
                fingerprint TEXT NOT NULL, streak INTEGER NOT NULL,
                announced TEXT, incident INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS snapshots(
                scope TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL,
                created_at REAL NOT NULL, PRIMARY KEY(scope,digest));
            CREATE TABLE IF NOT EXISTS baselines(
                scope TEXT PRIMARY KEY, digest TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(
                event_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at REAL NOT NULL,
                delivered_at REAL, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0);
        ''')
        self.db.commit()

    def close(self):
        self.db.close()

    def get_meta(self, key, default=None):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, dumps(value)))

    def baseline(self, scope):
        row = self.db.execute('''SELECT s.payload FROM snapshots s JOIN baselines b
            ON s.scope=b.scope AND s.digest=b.digest WHERE b.scope=?''', (scope,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_snapshot(self, scope, snapshot, now=None):
        fingerprint = digest(snapshot)
        with self.db:
            self.db.execute('''INSERT INTO snapshots VALUES(?,?,?,?)
                ON CONFLICT(scope,digest) DO UPDATE SET created_at=excluded.created_at''',
                            (scope, fingerprint, dumps(snapshot), time.time() if now is None else now))
        return fingerprint

    def accept_baseline(self, scope, fingerprint):
        row = self.db.execute('SELECT payload FROM snapshots WHERE scope=? AND digest=?',
                              (scope, fingerprint)).fetchone()
        if not row:
            raise ValueError('找不到该范围的候选快照')
        if json.loads(row[0]).get('complete') is not True:
            raise ValueError('不完整观测不能设为接口定义基线')
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO baselines VALUES(?,?)', (scope, fingerprint))

    def record(self, result, threshold=2, now=None):
        now = time.time() if now is None else now
        check_id, status = result['check_id'], result['status']
        if status not in ISSUES | {'ok', 'disabled', 'blocked'}:
            raise ValueError('未知巡检状态')
        # Counts, timings and dynamic data never create a new incident.
        detail = result.get('details', {})
        stable = {key: detail[key] for key in ('http_status', 'error_codes', 'diff', 'local_diff')
                  if key in detail}
        fingerprint = digest({'status': status, 'summary': result['summary'], 'details': stable})
        old = self.db.execute('SELECT * FROM checks WHERE check_id=?', (check_id,)).fetchone()
        previous_issue = old and json.loads(old['result'])['status'] in ISSUES
        streak = old['streak'] + 1 if previous_issue and status in ISSUES else 1
        announced = old['announced'] if old else None
        incident = old['incident'] if old else 0
        event = None
        if status in ISSUES and (status in ('changed', 'auth_expired') or streak >= threshold):
            if announced != fingerprint:
                event = dict(result, event_id=str(uuid.uuid4()), kind='issue', detected_at=now)
                announced, incident = fingerprint, 1
        elif status == 'ok' and incident:
            event = dict(result, event_id=str(uuid.uuid4()), kind='recovered', detected_at=now)
            announced, incident = fingerprint, 0
        # blocked / disabled / unconfirmed failures do not resolve an open incident.
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO checks VALUES(?,?,?,?,?,?,?)',
                            (check_id, dumps(result), now, fingerprint, streak, announced, incident))
            if event:
                self.db.execute('INSERT INTO events(event_id,payload,created_at) VALUES(?,?,?)',
                                (event['event_id'], dumps(event), now))
        return event

    def pending(self, now=None):
        rows = self.db.execute('''SELECT event_id,payload,attempts FROM events
            WHERE delivered_at IS NULL AND next_attempt_at<=? ORDER BY created_at LIMIT 20''',
                               (time.time() if now is None else now,)).fetchall()
        return [(row[0], json.loads(row[1]), row[2]) for row in rows]

    def has_check(self, check_id):
        return bool(self.db.execute('SELECT 1 FROM checks WHERE check_id=?', (check_id,)).fetchone())

    def delivery(self, event_id, success, attempts, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('''UPDATE events SET delivered_at=?,attempts=?,next_attempt_at=?
                WHERE event_id=?''', (now if success else None, attempts + 1,
                                      now + min(3600, 30 * 2 ** min(attempts, 7)), event_id))

    def report(self):
        checks = [dict(json.loads(row['result']), checked_at=row['checked_at'],
                       failure_streak=row['streak'], incident_open=bool(row['incident']))
                  for row in self.db.execute('SELECT * FROM checks ORDER BY check_id')]
        snapshots = [dict(row) for row in self.db.execute('''SELECT s.scope,s.digest,s.created_at,
            CASE WHEN b.digest=s.digest THEN 1 ELSE 0 END AS is_baseline FROM snapshots s
            LEFT JOIN baselines b ON s.scope=b.scope ORDER BY s.created_at DESC LIMIT 40''')]
        pending = self.db.execute('SELECT count(*) FROM events WHERE delivered_at IS NULL').fetchone()[0]
        return {'checks': checks, 'snapshots': snapshots, 'pending_alerts': pending}

    def prune(self, days, now=None):
        before = (time.time() if now is None else now) - days * 86400
        with self.db:
            self.db.execute('DELETE FROM events WHERE delivered_at IS NOT NULL AND created_at<?', (before,))
            self.db.execute('''DELETE FROM snapshots WHERE created_at<? AND NOT EXISTS
                (SELECT 1 FROM baselines b WHERE b.scope=snapshots.scope AND b.digest=snapshots.digest)''',
                            (before,))
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='probe_runs'").fetchone():
                targets = [row[0] for row in self.db.execute(
                    "SELECT run_id FROM probe_runs WHERE state='complete' AND updated_at<?", (before,))]
                for run_id in targets:
                    self.db.execute('DELETE FROM probe_steps WHERE run_id=?', (run_id,))
                    self.db.execute('DELETE FROM probe_resources WHERE run_id=?', (run_id,))
                    self.db.execute('DELETE FROM probe_runs WHERE run_id=?', (run_id,))
