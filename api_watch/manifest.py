"""Executable coverage registry: endpoint variants, prerequisites and evidence."""
from collections import Counter
import importlib
from pathlib import Path

CORE = [
    {'id': 'guest.activate', 'operation': 'GuestActivate', 'method': 'POST',
     'source': 'builder.auth.XAuth.refresh_guest_token', 'requirements': [], 'risk': 'read', 'scenario': 'reads'},
    {'id': 'guest.user', 'operation': 'UserByScreenName', 'method': 'GET',
     'source': 'x_apis.x_api.XAPI.get_user_info', 'requirements': ['samples.username'], 'risk': 'read', 'scenario': 'reads'},
    {'id': 'guest.timeline', 'operation': 'UserTweets', 'method': 'GET',
     'source': 'x_apis.x_api.XAPI.get_user_post_note', 'requirements': ['samples.username'], 'risk': 'read', 'scenario': 'reads'},
    {'id': 'guest.timeline.page2', 'operation': 'UserTweets', 'method': 'GET',
     'source': 'x_apis.x_api.XAPI.get_user_post_note', 'requirements': ['samples.username', 'probes.pagination'], 'risk': 'read', 'scenario': 'reads'},
    {'id': 'auth.viewer', 'operation': 'Viewer', 'method': 'GET',
     'source': 'x_apis.x_api.XAPI.get_viewer', 'requirements': ['cookies'], 'risk': 'read', 'scenario': 'reads'},
]


def registry():
    specs = list(CORE)
    for name in ('read_probes', 'write_probes', 'lifecycle_probes'):
        specs.extend(getattr(importlib.import_module('api_watch.' + name), 'SPECS'))
    seen = set()
    for spec in specs:
        if spec['id'] in seen:
            raise ValueError('duplicate probe id')
        seen.add(spec['id'])
        if not spec.get('operation') or not spec.get('source'):
            raise ValueError('incomplete probe registration')
    return specs


def uncovered_sources(specs=None):
    """Fail CI when an exposed low-level SDK network method lacks a probe.

    Convenience wrappers are not independent HTTP interfaces. Login state
    machines are registered at their public login entry, including challenges.
    """
    import ast
    from api_watch.catalog import ROOT
    specs = registry() if specs is None else specs
    covered = {row['source'] for row in specs}
    leaves = set()
    modules = ('x_api', 'x_write_api', 'x_media_api', 'x_article_api', 'x_dm_api')
    for name in modules:
        tree = ast.parse((ROOT / 'x_apis' / (name + '.py')).read_text())
        for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
            for method in cls.body:
                if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) or method.name.startswith('_'):
                    continue
                direct = False
                for call in ast.walk(method):
                    if not isinstance(call, ast.Call):
                        continue
                    fn = call.func
                    if isinstance(fn, ast.Name) and fn.id in ('graphql_get', 'graphql_post', 'graphql_post_query', '_rest_post'):
                        direct = True
                    if (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                            and fn.value.id in ('client', 'requests') and fn.attr in ('get', 'post', 'request')):
                        direct = True
                if direct:
                    leaves.add('x_apis.' + name + '.' + cls.name + '.' + method.name)
    leaves.update(('x_apis.login_api.XJetfuelLoginApi.login', 'x_apis.login_api.XLoginApi.login',
                   'builder.auth.XAuth.refresh_guest_token'))
    return sorted(leaves - covered)


def requirement_ready(name, config, secrets):
    aliases = {'writes_enabled': 'writes.enabled', 'test_account_id': 'samples.test_account_id',
               'peer_user_id': 'samples.peer_user_id', 'image_file': 'samples.image_file',
               'video_file': 'samples.video_file', 'premium': 'writes.premium', 'articles': 'writes.articles',
               'chat_pin': 'secrets.chat_pin', 'identity_file': 'secrets.identity_file',
               'allow_publish': 'writes.allow_publish', 'allow_follow': 'writes.allow_follow',
               'peer_username': 'samples.peer_username'}
    name = aliases.get(name, name)
    if '|' in name:
        return any(requirement_ready(part, config, secrets) for part in name.split('|'))
    if name == 'cookies':
        return bool(secrets.get('cookies'))
    if name in ('home.get.item', 'cursor', 'first_page_cursor', 'fresh_identity', 'new_conversation',
                'unregistered_test_account', 'chat_identity'):
        return None  # Runtime evidence, never assert readiness from config alone.
    if name == 'chatxdk':
        import importlib.util
        return bool(importlib.util.find_spec('chat_xdk'))
    if name.startswith('login.'):
        value = secrets.get('login', {})
        parts = name.split('.')[1:]
    elif name.startswith('secrets.'):
        value, parts = secrets, name.split('.')[1:]
    else:
        value, parts = config, name.split('.')
    for part in parts:
        if not isinstance(value, dict) or part not in value:
            return False
        value = value[part]
    if parts[-1].endswith('_file'):
        return bool(value and Path(value).is_file())
    return bool(value)


def plan_fingerprint(config, secrets):
    from api_watch.state import digest
    import hashlib
    def file_hash(value):
        if not value:
            return None
        try:
            path = Path(value)
            if not path.is_file() or path.stat().st_size > 25 * 1024 * 1024:
                return 'missing_or_oversize'
            h = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(65536), b''):
                    h.update(chunk)
            return h.hexdigest()
        except OSError:
            return 'unreadable'
    artifacts = {name: file_hash(config['samples'].get(name))
                 for name in ('image_file', 'video_file', 'chat_settings_contract_file')}
    artifacts['identity_file'] = file_hash(secrets.get('identity_file'))
    root = Path(__file__).resolve().parent.parent
    sources = {}
    for directory in ('api_watch', 'x_apis', 'builder', 'utils'):
        for path in (root / directory).glob('*.py'):
            sources[str(path.relative_to(root))] = file_hash(path)
    from utils.resource_paths import asset_path
    sources['graphql'] = file_hash(asset_path('graphql.json'))
    sources['transaction_profile'] = file_hash(asset_path('transaction_l1.json'))
    return digest({'samples': config['samples'], 'probes': config['probes'], 'writes': config['writes'],
                   'lifecycle': config['lifecycle'], 'artifacts': artifacts, 'sources': sources,
                   'credentials': digest({key: secrets.get(key) for key in
                      ('cookies', 'proxy', 'chat_pin', 'identity_file', 'login')})})


def coverage(config, secrets, store=None):
    latest = {}
    fingerprint = plan_fingerprint(config, secrets)
    if store:
        for result in store.report()['checks']:
            key = result.get('details', {}).get('probe_id')
            if result.get('details', {}).get('plan_fingerprint') != fingerprint:
                continue
            if key and (key not in latest or latest[key]['checked_at'] < result['checked_at']):
                latest[key] = result
    rows = []
    for spec in registry():
        requirements = list(spec.get('requirements', []))
        if spec['scenario'] == 'reads':
            requirements.append('probes.enabled')
            requirements.append('probes.guest' if spec['id'].startswith('guest.') else 'probes.authenticated')
            if spec['id'].endswith('.page2'):
                requirements.append('probes.pagination')
            if spec['id'].startswith('read.chat.'):
                requirements.append('probes.chat')
            if spec['id'].startswith('read.notifications.'):
                requirements.append('probes.notifications')
        if spec['scenario'] == 'writes':
            requirements += ['writes.enabled', 'samples.test_account_id', 'cookies']
        elif spec['scenario'] == 'lifecycle':
            requirements += ['lifecycle.enabled', 'samples.test_account_id']
            if not spec['id'].startswith('lifecycle.login.'):
                requirements.append('cookies')
            else:
                requirements += ['login.username', 'login.password']
                if spec['id'] == 'lifecycle.login.jetfuel' and secrets.get('login', {}).get('mode', 'pure') == 'pure':
                    requirements.append('login.profile')
        missing = [key for key in sorted(set(requirements)) if requirement_ready(key, config, secrets) is False]
        runtime = [key for key in sorted(set(requirements)) if requirement_ready(key, config, secrets) is None]
        old = latest.get(spec['id'])
        rows.append(dict(spec, configured=not missing, missing=missing, runtime_requirements=runtime,
                         last_status=old['status'] if old else 'not_run',
                         checked_at=old.get('checked_at') if old else None,
                         last_reason=(old or {}).get('details', {}).get('reason'),
                         verification=(old or {}).get('details', {}).get('verification_level')))
    counts = dict(Counter(row['last_status'] for row in rows))
    return {'implemented': len(rows), 'configured': sum(row['configured'] for row in rows),
            'last_result_counts': counts, 'all_passed': bool(rows) and all(row['configured'] and row['last_status'] == 'ok' for row in rows),
            'scope': 'XApis exposed endpoint operations and registered request variants; not all X internal APIs',
            'probes': rows}
