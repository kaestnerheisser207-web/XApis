"""Operator-initiated cleanup of journalled test resources, never blind replay."""
import json
import time
import uuid

from api_watch.execution import ProbeContext, journal_report
from api_watch.suite import _authenticate
from api_watch.probes import _Issue


def journal(store):
    runs = journal_report(store)
    for run in runs:
        run['steps'] = [dict(row) for row in store.db.execute('''SELECT probe_id,state FROM probe_steps
            WHERE run_id=? AND state<>'confirmed' ''', (run['run_id'],))]
        run['resources'] = [dict(row) for row in store.db.execute('''SELECT kind,resource_id,cleaned
            FROM probe_resources WHERE run_id=? AND cleaned=0''', (run['run_id'],))]
    return runs


def acknowledge(store, run_id, note):
    if not isinstance(note, str) or not note.strip() or len(note) > 500:
        raise ValueError('需要填写人工核对说明')
    # Do not overwrite check results or pretend an unverified operation passed.
    with store.db:
        row = store.db.execute('SELECT run_id FROM probe_runs WHERE run_id=?', (run_id,)).fetchone()
        if not row:
            raise ValueError('未知巡检运行')
        store.db.execute('UPDATE probe_runs SET state=?,updated_at=? WHERE run_id=?',
                         ('acknowledged', time.time(), run_id))
        store.set_meta('acknowledged:' + run_id, {'note': note, 'at': time.time()})


def cleanup(config, secrets, store, run_id, stop_event=None):
    from api_watch.write_probes import (_cleanup_articles, _cleanup_tweets,
                                        _deleted_tweet, _following, _checked)
    from x_apis.x_write_api import XWriteAPI
    old = store.db.execute('SELECT * FROM probe_runs WHERE run_id=?', (run_id,)).fetchone()
    if not old or old['scenario'] != 'writes':
        raise ValueError('只支持清理已记录的非聊天写探针资源')
    if config['samples']['test_account_id'] != old['actor_id']:
        raise ValueError('配置测试账号与原运行不匹配')

    class RecoveryContext(ProbeContext):
        def step(self, probe_id, action, validator=None, **options):
            if options.get('cleanup'):
                probe_id = 'cleanup.' + recovery_id + '.' + probe_id
            return super().step(probe_id, action, validator, **options)

    recovery_id = uuid.uuid4().hex[:12]
    ctx = RecoveryContext(config, secrets, store, scenario='writes', run_id=run_id,
                          actor_id=old['actor_id'], stop_event=stop_event)
    try:
        if not _authenticate(ctx) or ctx.actor_id != old['actor_id']:
            return list(ctx.results.values())
        # An already absent object requires no delete. Never count that as
        # executing the DeleteTweet endpoint in the coverage report.
        for row in ctx.resources('tweet'):
            if not row['cleaned']:
                try:
                    _deleted_tweet(ctx, row['id'])
                except Exception:
                    continue
                ctx.mark_cleaned('tweet', row['id'])
        _cleanup_articles(ctx)
        _cleanup_tweets(ctx)
        for row in ctx.resources('follow'):
            if row['cleaned'] or row['metadata'].get('original_following') is not False:
                continue
            peer, username = row['id'], row['metadata'].get('peer_username')
            def restore(peer=peer, username=username):
                if _following(ctx, peer, username):
                    receipt = XWriteAPI.unfollow_user(ctx.auth, peer)
                    if (not isinstance(receipt, dict) or receipt.get('errors')
                            or str(receipt.get('id_str') or receipt.get('id')) != peer):
                        raise _Issue('contract_error', fields=('friendship.user_id',))
                if _following(ctx, peer, username):
                    raise _Issue('incomplete', fields=('following',))
                ctx.mark_cleaned('follow', peer)
                return {'restored': True}
            ctx.step('follow.' + peer, restore, mutating=True, cleanup=True)
    finally:
        ctx.finish()
    return list(ctx.results.values())
