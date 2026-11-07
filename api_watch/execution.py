"""Endpoint execution with durable mutation intent and owned-resource receipts."""
from dataclasses import dataclass
import json
from pathlib import Path
import time
import uuid

from api_watch.probes import _failure, _Issue, _Pacer, _record
from api_watch.state import dumps, secure_dir


@dataclass
class Outcome:
    ok: bool
    value: object = None
    status: str = 'blocked'


class ProbeContext:
    def __init__(self, config, secrets, store, *, scenario='reads', auth=None,
                 actor_id='', stop_event=None, run_id=None):
        self.config, self.secrets, self.store = config, secrets, store
        self.samples = config['samples']
        self.scenario, self.auth, self.actor_id = scenario, auth, actor_id
        self.stop_event = stop_event
        self.state_dir = secure_dir(store.root / 'suite')
        self.run_id = run_id or uuid.uuid4().hex
        self.marker = '[xapis-watch:' + self.run_id + ']'
        self.results, self.outcomes = {}, {}
        self.pacer = _Pacer(config['probes']['request_gap_seconds'], stop_event)
        self.blocked_status = None
        self.mutation_uncertain = False
        self._client = None
        self._schema()
        self.journal_actor = actor_id or self.samples.get('test_account_id', '')
        old = self.store.db.execute('SELECT actor_id,scenario FROM probe_runs WHERE run_id=?', (self.run_id,)).fetchone()
        if old and (old['actor_id'] != self.journal_actor or old['scenario'] != scenario):
            raise ValueError('运行记录的账号或场景不匹配')
        if old:
            pending = self.store.db.execute('''SELECT 1 FROM probe_steps WHERE run_id=?
                AND mutation=1 AND state<>'confirmed' LIMIT 1''', (self.run_id,)).fetchone()
            self.mutation_uncertain = bool(pending)
            for row in self.store.db.execute('SELECT kind,metadata FROM probe_resources WHERE run_id=? AND cleaned=0',
                                              (self.run_id,)):
                metadata = json.loads(row['metadata'])
                if metadata.get('cleanup_required', row['kind'] in ('tweet', 'post', 'article', 'retweet', 'follow')):
                    self.mutation_uncertain = True
        with self.store.db:
            self.store.db.execute('INSERT OR IGNORE INTO probe_runs VALUES(?,?,?,?,?,?)',
                (self.run_id, scenario, self.journal_actor, 'running', time.time(), time.time()))

    def _schema(self):
        self.store.db.executescript('''
            CREATE TABLE IF NOT EXISTS probe_runs(
                run_id TEXT PRIMARY KEY, scenario TEXT NOT NULL, actor_id TEXT NOT NULL,
                state TEXT NOT NULL, started_at REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS probe_steps(
                run_id TEXT NOT NULL, probe_id TEXT NOT NULL, mutation INTEGER NOT NULL,
                state TEXT NOT NULL, result TEXT, PRIMARY KEY(run_id,probe_id));
            CREATE TABLE IF NOT EXISTS probe_resources(
                run_id TEXT NOT NULL, kind TEXT NOT NULL, resource_id TEXT NOT NULL,
                metadata TEXT NOT NULL, cleaned INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(run_id,kind,resource_id));
        ''')
        self.store.db.commit()

    @property
    def client(self):
        if self._client is None:
            if self.auth is None or not self.actor_id:
                raise _Issue('blocked', reason='verified_account_required')
            from x_apis.client import XClient
            self._client = XClient(self.auth, state_dir=self.state_dir / 'chat',
                                  identity_path=self.secrets.get('identity_file') or None,
                                  existing_pin=self.secrets.get('chat_pin') or None,
                                  default_pin=self.secrets.get('chat_pin') or '')
        return self._client

    def block(self, ids, reason, status='blocked'):
        for probe_id in ids:
            if probe_id not in self.results:
                self._save(probe_id, status, {'reason': reason})

    def _save(self, probe_id, status, details=None, summary=None):
        labels = {'needs_input': '此步骤需要人工完成验证',
                  'not_exercised': '本轮未实际执行此接口，不能计为通过',
                  'unconfirmed': '写入结果不明确，已暂停后续写入，请先核对',
                  'cleanup_failed': '测试资源清理未确认，已保留恢复记录'}
        result = _record(probe_id, status, details or {}, summary or labels.get(status))
        result['details']['probe_id'] = probe_id
        result['details']['scenario'] = self.scenario
        self.results[probe_id] = result
        return result

    def _mutation_allowed(self, probe_id, cleanup):
        expected = self.samples.get('test_account_id')
        if not expected:
            return False
        if probe_id.startswith('lifecycle.login.'):
            return bool(self.config['lifecycle']['enabled'] and expected)
        if self.actor_id != expected or self.auth is None:
            return False
        if cleanup:
            return True
        if self.scenario == 'writes':
            return self.config['writes']['enabled']
        return self.scenario == 'lifecycle' and self.config['lifecycle']['enabled']

    def unresolved_prior(self):
        return bool(self.store.db.execute('''SELECT 1 FROM probe_runs
            WHERE actor_id=? AND scenario IN ('writes','lifecycle') AND run_id<>?
              AND state IN ('running','attention') LIMIT 1''', (self.journal_actor, self.run_id)).fetchone())

    def step(self, probe_id, action, validator=None, *, mutating=False, cleanup=False):
        # An accidental second call in one run returns its original outcome,
        # and a restarted run never resubmits an existing mutation intent.
        if probe_id in self.outcomes:
            return self.outcomes[probe_id]
        if self.blocked_status or (self.stop_event and self.stop_event.is_set() and not cleanup):
            self._save(probe_id, 'blocked', {'reason': self.blocked_status or 'stopping'})
            return Outcome(False)
        if mutating and (not self._mutation_allowed(probe_id, cleanup)
                         or self.mutation_uncertain and not cleanup
                         or not cleanup and self.unresolved_prior()):
            self._save(probe_id, 'blocked', {'reason': 'write_gate_or_unresolved_run'})
            return Outcome(False)
        existing = self.store.db.execute('SELECT state FROM probe_steps WHERE run_id=? AND probe_id=?',
                                         (self.run_id, probe_id)).fetchone()
        if existing and mutating:
            self._save(probe_id, 'unconfirmed', {'reason': 'existing_mutation_intent'})
            return Outcome(False, status='unconfirmed')
        started = time.monotonic()
        attempted = False
        try:
            if not cleanup:
                self.pacer.wait()
            # Commit the intent before control can reach any external mutation.
            with self.store.db:
                self.store.db.execute('INSERT OR REPLACE INTO probe_steps VALUES(?,?,?,?,?)',
                    (self.run_id, probe_id, int(mutating), 'attempted', None))
            attempted = True
            value = action()
            details = validator(value) if validator else {}
            if details is None:
                details = {}
            if not isinstance(details, dict):
                raise _Issue('contract_error', reason='invalid_validator')
            details.update(duration_ms=round((time.monotonic() - started) * 1000))
            result = self._save(probe_id, 'ok', details)
            outcome = Outcome(True, value, 'ok')
            state = 'confirmed'
        except Exception as exc:
            code = getattr(exc, 'code', None)
            custom = {'needs_input': 'needs_input', 'rate_limited': 'rate_limited',
                      'delivery_unknown': 'unconfirmed', 'readback_failed': 'unconfirmed',
                      'not_exercised': 'not_exercised', 'contract_error': 'contract_error',
                      'blocked': 'blocked'}
            status, details = _failure(exc)
            if code in custom:
                status = custom[code]
            if status in ('auth_expired', 'rate_limited'):
                self.blocked_status = status
            if mutating and attempted:
                details['failure_status'] = status
                status = ('cleanup_failed' if cleanup else status if status in
                          ('needs_input', 'rate_limited', 'auth_expired') else 'unconfirmed')
                state = 'unconfirmed'
                self.mutation_uncertain = True
            else:
                state = 'failed'
            result = self._save(probe_id, status, details)
            outcome = Outcome(False, status=status)
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO probe_steps VALUES(?,?,?,?,?)',
                (self.run_id, probe_id, int(mutating), state, dumps(result)))
        self.outcomes[probe_id] = outcome
        return outcome

    def save_resource(self, kind, resource_id, **metadata):
        if not isinstance(resource_id, str) or not resource_id or len(resource_id) > 200:
            raise ValueError('invalid resource identifier')
        # Private journal data stays on the protected state volume, not in alerts.
        with self.store.db:
            self.store.db.execute('''INSERT INTO probe_resources VALUES(?,?,?,?,0)
                ON CONFLICT(run_id,kind,resource_id) DO UPDATE SET metadata=excluded.metadata''',
                (self.run_id, kind, resource_id, dumps(metadata)))
        return resource_id

    def resources(self, kind, all_runs=False):
        if all_runs:
            rows = self.store.db.execute('''SELECT r.* FROM probe_resources r JOIN probe_runs p
                ON r.run_id=p.run_id WHERE p.actor_id=? AND r.kind=? ORDER BY p.started_at''',
                (self.journal_actor, kind)).fetchall()
        else:
            rows = self.store.db.execute('SELECT * FROM probe_resources WHERE run_id=? AND kind=?',
                                         (self.run_id, kind)).fetchall()
        return [{'id': row['resource_id'], 'metadata': json.loads(row['metadata']),
                 'cleaned': bool(row['cleaned']), 'run_id': row['run_id']} for row in rows]

    def mark_cleaned(self, kind, resource_id):
        with self.store.db:
            self.store.db.execute('UPDATE probe_resources SET cleaned=1 WHERE run_id=? AND kind=? AND resource_id=?',
                                  (self.run_id, kind, resource_id))

    def resolve_resource(self, kind, resource_id, **metadata):
        """Reconcile a recorded request for this verified actor without resubmitting."""
        with self.store.db:
            rows = self.store.db.execute('''SELECT r.run_id,r.metadata FROM probe_resources r
                JOIN probe_runs p ON p.run_id=r.run_id
                WHERE p.actor_id=? AND r.kind=? AND r.resource_id=?''',
                (self.actor_id, kind, resource_id)).fetchall()
            for row in rows:
                merged = dict(json.loads(row['metadata']), **metadata)
                self.store.db.execute('UPDATE probe_resources SET metadata=? WHERE run_id=? AND kind=? AND resource_id=?',
                                     (dumps(merged), row['run_id'], kind, resource_id))

    def finish(self):
        rows = self.store.db.execute('SELECT state FROM probe_steps WHERE run_id=? AND mutation=1',
                                    (self.run_id,)).fetchall()
        pending = any(row[0] != 'confirmed' for row in rows)
        resources = self.store.db.execute('SELECT kind,metadata FROM probe_resources WHERE run_id=? AND cleaned=0',
                                         (self.run_id,)).fetchall()
        for row in resources:
            metadata = json.loads(row['metadata'])
            if metadata.get('cleanup_required', row['kind'] in ('tweet', 'post', 'article', 'retweet', 'follow')):
                pending = True
        with self.store.db:
            self.store.db.execute('UPDATE probe_runs SET state=?,updated_at=? WHERE run_id=?',
                ('attention' if pending else 'complete', time.time(), self.run_id))
        return pending


def journal_report(store):
    exists = store.db.execute("SELECT 1 FROM sqlite_master WHERE name='probe_runs'").fetchone()
    if not exists:
        return []
    return [dict(row) for row in store.db.execute('''SELECT run_id,scenario,state,started_at,updated_at
        FROM probe_runs WHERE state IN ('running','attention') ORDER BY started_at''')]
