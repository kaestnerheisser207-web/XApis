"""Run complete registered read, write or one-time lifecycle scenarios."""
from builder.auth import XAuth
from api_watch.execution import ProbeContext
from api_watch.manifest import CORE, registry
from api_watch.probes import (_GuestSource, _prepare_transaction, _user, _viewer,
                              _timeline, _Issue)
from utils.x_util import extract_cursor
from x_apis.x_api import XAPI


def _guest(ctx):
    ids = [spec['id'] for spec in CORE if spec['id'].startswith('guest.')]
    if not ctx.config['probes']['guest']:
        ctx.block(ids, 'guest_disabled', 'disabled')
        return
    source = _GuestSource(ctx.pacer, proxy=ctx.secrets.get('proxy'))
    try:
        preparation = _prepare_transaction(source.auth, 'guest', ctx.pacer)
        ctx.results[preparation['check_id']] = preparation
        if source.auth._transaction is None:
            ctx.block(ids, 'transaction_unavailable')
            return
        def activated(_):
            if not source.auth._guest_token:
                raise _Issue('contract_error', fields=('guest_token',))
            return {'token_present': True}
        if not ctx.step('guest.activate', source._refresh_guest_locked, activated).ok:
            ctx.block(ids, 'guest_activation_failed')
            return
        user = ctx.step('guest.user', lambda: source._query('UserByScreenName',
            {'screen_name': ctx.samples['username'], 'withGrokTranslatedBio': True}), _user)
        if not user.ok:
            ctx.block(ids, 'user_lookup_failed')
            return
        uid = user.value['data']['user']['result']['rest_id']
        variables = {'userId': uid, 'count': ctx.config['probes']['count'], 'includePromotedContent': True,
                     'withQuickPromoteEligibilityTweetFields': True, 'withVoice': True}
        page = ctx.step('guest.timeline', lambda: source._query('UserTweets', variables),
                        lambda raw: _timeline(raw, guest=True))
        if not ctx.config['probes']['pagination']:
            ctx.block(['guest.timeline.page2'], 'pagination_disabled', 'disabled')
        elif page.ok and extract_cursor(page.value):
            cursor = extract_cursor(page.value)
            def second(raw):
                result = _timeline(raw, guest=True)
                if extract_cursor(raw) == cursor:
                    raise _Issue('contract_error', fields=('cursor.progress',))
                return result
            ctx.step('guest.timeline.page2', lambda: source._query('UserTweets', dict(variables, cursor=cursor)), second)
        else:
            ctx.block(['guest.timeline.page2'], 'no_next_cursor', 'not_exercised')
    finally:
        source.close()


def _authenticate(ctx):
    if not ctx.secrets.get('cookies') or ctx.secrets.get('auth_paused'):
        ctx.block(['auth.viewer'], 'cookies_missing_or_paused')
        return False
    try:
        proxy = ctx.secrets.get('proxy')
        auth = XAuth(proxies={'http': proxy, 'https': proxy} if proxy else None).prepare_auth(ctx.secrets['cookies'])
        if not auth.is_logged_in:
            raise ValueError()
    except (ValueError, TypeError):
        ctx.block(['auth.viewer'], 'cookies_invalid', 'auth_expired')
        return False
    prepared = _prepare_transaction(auth, 'auth', ctx.pacer)
    ctx.results[prepared['check_id']] = prepared
    if auth._transaction is None:
        ctx.block(['auth.viewer'], 'transaction_unavailable')
        return False
    def identity_validator(raw):
        details = _viewer(raw, auth)
        observed = str(raw['data']['viewer']['user_results']['result']['rest_id'])
        expected = ctx.samples.get('test_account_id')
        # Validate both configured identity and any existing run ownership
        # before altering either the context or its durable journal record.
        if (expected and observed != expected) or (ctx.journal_actor and observed != ctx.journal_actor):
            raise _Issue('blocked', reason='test_account_mismatch', fields=('viewer.expected_account',))
        return details
    identity = ctx.step('auth.viewer', lambda: XAPI.get_viewer(auth), identity_validator)
    if not identity.ok:
        return False
    ctx.auth = auth
    ctx.actor_id = str(identity.value['data']['viewer']['user_results']['result']['rest_id'])
    ctx.journal_actor = ctx.actor_id
    with ctx.store.db:
        ctx.store.db.execute('UPDATE probe_runs SET actor_id=? WHERE run_id=?', (ctx.actor_id, ctx.run_id))
    return True


def run_suite(config, secrets, store, *, scenario='reads', stop_event=None):
    module_name = {'reads': 'read_probes', 'writes': 'write_probes', 'lifecycle': 'lifecycle_probes'}[scenario]
    import importlib
    module = importlib.import_module('api_watch.' + module_name)
    expected = module.SPECS + (CORE if scenario == 'reads' else [])
    ctx = ProbeContext(config, secrets, store, scenario=scenario, stop_event=stop_event)
    try:
        if scenario == 'reads':
            _guest(ctx)
            # A guest rate limit is not an authenticated account failure.
            ctx.blocked_status = None
        authenticated = False
        if scenario != 'reads' or config['probes']['authenticated']:
            authenticated = _authenticate(ctx)
        if scenario == 'reads' and not authenticated:
            ctx.block([spec['id'] for spec in module.SPECS], 'verified_cookie_required')
        elif scenario == 'writes' and not config['writes']['enabled']:
            ctx.block([spec['id'] for spec in module.SPECS], 'writes_disabled', 'disabled')
        elif scenario == 'writes' and not authenticated:
            ctx.block([spec['id'] for spec in module.SPECS], 'verified_test_account_required')
        elif scenario == 'lifecycle' and not config['lifecycle']['enabled']:
            ctx.block([spec['id'] for spec in module.SPECS], 'lifecycle_disabled', 'disabled')
        else:
            module.run(ctx)
    except Exception as exc:
        ctx.block([spec['id'] for spec in expected], 'scenario_interrupted:' + type(exc).__name__, 'incomplete')
    finally:
        ctx.block([spec['id'] for spec in expected], 'dependency_not_exercised', 'not_exercised')
        attention = ctx.finish()
    results = list(ctx.results.values())
    if attention:
        results.append({'check_id': 'journal.' + scenario, 'status': 'unconfirmed',
                        'summary': '存在未确认写入或待清理资源，后续写入已暂停',
                        'details': {'run_id': ctx.run_id, 'scenario': scenario}})
    return results
