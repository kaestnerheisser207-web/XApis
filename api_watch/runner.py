"""Scheduling, explicit scenario execution and durable reporting."""
import json
import os
from pathlib import Path
import threading
import time

from api_watch.catalog import compare_installed
from api_watch.state import Store, dumps, process_lock, secure_dir


def emit(payload):
    print(dumps(payload), flush=True)


class Heartbeat:
    def __init__(self, state_dir, phase_limit):
        self.path = secure_dir(state_dir) / 'heartbeat.json'
        self.phase_limit = phase_limit
        self.phase = 'starting'
        self.phase_started = time.time()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.write()
        self.thread.start()

    def set_phase(self, name):
        with self.lock:
            self.phase, self.phase_started = name, time.time()
        self.write()

    def write(self):
        with self.lock:
            data = {'heartbeat_at': time.time(), 'phase': self.phase,
                    'phase_started': self.phase_started, 'phase_limit': self.phase_limit,
                    'pid': os.getpid()}
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(dumps(data))
            temporary.chmod(0o600)
            temporary.replace(self.path)

    def _run(self):
        while not self.stop_event.wait(15):
            self.write()

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=20)
        self.set_phase('stopped')


def healthy(state_dir, now=None):
    try:
        data = json.loads((Path(state_dir) / 'heartbeat.json').read_text())
        now = time.time() if now is None else now
        if not 0 <= now - data['heartbeat_at'] < 90 or data['phase'] == 'stopped':
            return False
        if data['phase'] not in ('idle', 'starting'):
            return 0 <= now - data['phase_started'] <= data['phase_limit']
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def flush_alerts(store, webhook=None, stop_event=None):
    """At-least-once delivery; receivers deduplicate event_id. No raw exceptions."""
    import requests
    failures = 0
    for event_id, payload, attempts in store.pending():
        if stop_event and stop_event.is_set():
            break
        if not webhook:
            emit({'type': 'alert', **payload})
            store.delivery(event_id, True, attempts)
            continue
        try:
            # Never follow redirects with an incident payload or token-bearing webhook URL.
            with requests.Session() as session:
                session.trust_env = False
                response = session.post(webhook, json=payload, timeout=10, allow_redirects=False)
                success = 200 <= response.status_code < 300
        except requests.RequestException:
            success = False
        store.delivery(event_id, success, attempts)
        if not success:
            failures += 1
    if failures:
        emit({'type': 'notification_error', 'summary': '告警投递未成功，已保留并安排退避重试',
              'count': failures})
    return failures


def _account_scope(cookies):
    import hashlib
    from utils.x_util import trans_cookies
    parsed = trans_cookies(cookies)
    # A Cookie's twid is not verified identity; key by the actual session token.
    identity = parsed.get('auth_token') or cookies
    return 'auth-' + hashlib.sha256(identity.encode()).hexdigest()[:12]


def observe_operations(scope, observation, store):
    """Positive evidence only: absent operations never resolve or create changes."""
    from api_watch.state import digest
    family = observation.get('family')
    operations = observation.get('operations') or {}
    if family not in ('client-web', 'x-web') or not operations:
        return None
    key = scope + '/' + family
    baseline = store.get_meta('operations_baseline:' + key, {})
    changes = store.get_meta('operations_changes:' + key, {})
    for name, current in operations.items():
        if name in baseline and current != baseline[name]:
            changes[name] = {'before': baseline[name], 'after': current}
        else:
            changes.pop(name, None)
            baseline.setdefault(name, current)
    latest = dict(store.get_meta('operations_latest:' + key, {}).get('operations', {}))
    latest.update(operations)
    store.set_meta('operations_baseline:' + key, baseline)
    store.set_meta('operations_changes:' + key, changes)
    fingerprint = digest(latest)
    store.set_meta('operations_latest:' + key, {'operations': latest, 'fingerprint': fingerprint})
    # Keep a positively observed local incompatibility until that operation is
    # observed again or the installed implementation changes. Missing != fixed.
    local = compare_installed({'operations': latest})
    return {'check_id': 'operations.' + scope + '.' + family,
            'status': 'changed' if changes or local else 'ok',
            'summary': '已观察到同名操作定义变化；不推断缺失接口被删除' if changes or local
                       else '已记录明确观察到的操作定义；完整覆盖状态另行报告',
            'details': {'scope': key, 'observed_count': len(operations), 'fingerprint': fingerprint,
                        'diff': changes, 'local_diff': local}}


def scan_results(config, state_dir, secrets, store, stop_event):
    from api_watch.frontend import diff_snapshots, scan_frontend, prune_frontend_cache
    options = config['frontend']
    scopes = [('guest', '')]
    if options['authenticated'] and secrets['cookies'] and not secrets.get('auth_paused'):
        scopes.append((_account_scope(secrets['cookies']), secrets['cookies']))
    for scope, cookies in scopes:
        if stop_event.is_set():
            return
        result = scan_frontend(Path(state_dir) / 'resources' / scope, cookies=cookies,
                               proxy=secrets['proxy'], timeout=options['timeout'],
                               max_assets=int(options['max_assets']),
                               max_seconds=options['max_seconds'], stop_event=stop_event)
        result['check_id'] = 'frontend.' + scope
        observation = result.pop('observation', None)
        if observation:
            operation_result = observe_operations(scope, observation, store)
            if operation_result:
                yield operation_result
        observed_fingerprint = result.get('details', {}).get('observed_fingerprint')
        family = result.get('details', {}).get('family')
        if family in ('client-web', 'x-web'):
            result['check_id'] += '.' + family
        else:
            result['check_id'] += '.acquisition'
        if observed_fingerprint and family in ('client-web', 'x-web'):
            key = 'resource_baseline:' + scope + '/' + family
            resource_baseline = store.get_meta(key)
            if resource_baseline is None:
                store.set_meta(key, observed_fingerprint)
            resource_changed = resource_baseline is not None and resource_baseline != observed_fingerprint
            store.set_meta('resource_latest:' + scope + '/' + family, observed_fingerprint)
            yield {'check_id': 'resources.' + scope + '.' + family, 'status': 'changed' if resource_changed else 'ok',
                   'summary': '入口资源版本与首次观测不同；不代表接口已失效' if resource_changed else '入口资源版本已观测',
                   'details': {'family': family, 'diff': {'baseline': resource_baseline or observed_fingerprint,
                                                       'candidate': observed_fingerprint}}}
        snapshot = result.pop('snapshot', None)
        if result['status'] == 'ok' and snapshot and snapshot.get('complete') is True:
            key = scope + '/' + snapshot['family']
            # Remove timestamps before hashing content-addressed observations.
            snapshot.pop('generatedAt', None)
            fingerprint = store.save_snapshot(key, snapshot)
            baseline = store.baseline(key)
            result.setdefault('details', {}).update(scope=key, candidate_digest=fingerprint)
            if baseline is None:
                store.accept_baseline(key, fingerprint)
                result['details']['baseline_created'] = True
                result['summary'] = '首次完整观测已保存；不代表业务接口已验收'
            else:
                changes = diff_snapshots(baseline, snapshot)
                if changes.get('changed'):
                    result['status'] = 'changed'
                    result['summary'] = '上游定义或资源与观测基线不同，请结合接口探针判断影响'
                    result['details']['diff'] = changes
            local = compare_installed(snapshot)
            if local:
                result['status'] = 'changed'
                result['summary'] = '上游部分操作定义与当前 XApis 不同，尚未证明现用接口失效'
                result['details']['local_diff'] = local
        yield result
        if family in ('client-web', 'x-web') and store.has_check('frontend.' + scope + '.acquisition'):
            yield {'check_id': 'frontend.' + scope + '.acquisition', 'status': 'ok',
                   'summary': '前端页面获取与类型识别恢复；接口覆盖另行报告', 'details': {}}
    if options['authenticated'] and not secrets['cookies']:
        yield {'check_id': 'frontend.auth', 'status': 'disabled',
               'summary': '未配置监测账号 Cookie，未扫描登录态前端', 'details': {}}
    resources = Path(state_dir) / 'resources'
    if resources.exists():
        import re
        for path in resources.iterdir():
            if path.is_dir() and re.fullmatch(r'guest|auth-[0-9a-f]{12}', path.name):
                prune_frontend_cache(path, max_age_days=config['retention_days'])


def run(config, state_dir, secret_loader, *, once=False, only='all', stop_event=None):
    from api_watch.suite import run_suite
    from api_watch.manifest import plan_fingerprint
    from api_watch.config import ConfigError
    stop_event = stop_event or threading.Event()
    # A phase includes both guest and auth scans and a bounded probe suite.
    phase_limit = max(3600, config['frontend']['max_seconds'] * 2 + 120)
    with process_lock(state_dir):
        store = Store(state_dir)
        heartbeat = Heartbeat(state_dir, phase_limit)
        heartbeat.start()
        observed = []
        try:
            while not stop_event.is_set():
                now = time.time()
                try:
                    secrets = secret_loader()  # Pick up rotated file contents without login.
                except ConfigError:
                    result = {'check_id': 'monitor.credentials', 'status': 'incomplete',
                              'summary': '无法读取巡检凭据配置；本轮暂停，请修复挂载或配置', 'details': {}}
                    store.record(result, config['failure_threshold'])
                    emit({'type': 'check', **result})
                    observed.append(result)
                    if once:
                        break
                    observed.clear()
                    heartbeat.set_phase('idle')
                    stop_event.wait(30)
                    continue
                if secrets.get('credentials_error'):
                    result = {'check_id': 'monitor.credentials', 'status': 'incomplete',
                              'summary': 'Cookie 文件缺失或无效；登录态检查暂停，匿名检查继续', 'details': {}}
                    store.record(result, config['failure_threshold'])
                    emit({'type': 'check', **result})
                    observed.append(result)
                elif store.has_check('monitor.credentials'):
                    store.record({'check_id': 'monitor.credentials', 'status': 'ok',
                                  'summary': '巡检凭据配置可读取', 'details': {}}, config['failure_threshold'])
                from api_watch.state import digest
                credential_fingerprint = digest(secrets['cookies']) if secrets['cookies'] else None
                secrets['auth_paused'] = bool(credential_fingerprint and
                    store.get_meta('expired_credentials') == credential_fingerprint)
                current_plan = plan_fingerprint(config, secrets)
                phases = []
                if config['frontend']['enabled'] and only in ('all', 'scan'):
                    phases.append(('scan', config['scan_interval_seconds']))
                if config['probes']['enabled'] and only in ('all', 'probes'):
                    phases.append(('probes', config['probe_interval_seconds']))
                if (only == 'writes' or only == 'all' and config['writes']['enabled']):
                    phases.append(('writes', config['writes']['interval_seconds']))
                if once and (only == 'lifecycle' or only == 'all' and config['lifecycle']['enabled']):
                    phases.append(('lifecycle', 86400))
                for phase, interval in phases:
                    if stop_event.is_set():
                        break
                    due = store.get_meta('next_' + phase, 0)
                    if not once and now < due:
                        continue
                    heartbeat.set_phase(phase)
                    rate_limited = False
                    try:
                        if phase == 'scan':
                            results = scan_results(config, state_dir, secrets, store, stop_event)
                        else:
                            scenario = {'probes': 'reads', 'writes': 'writes', 'lifecycle': 'lifecycle'}[phase]
                            results = run_suite(config, secrets, store, scenario=scenario, stop_event=stop_event)
                        for result in results:
                            if stop_event.is_set():
                                break
                            if (secrets['auth_paused'] and result['check_id'].startswith('auth.')
                                    and result['status'] == 'disabled'):
                                result.update(status='blocked', summary='监测会话已失效，等待更新 Cookie 文件',
                                              details={'reason': 'session_paused'})
                            if result['status'] == 'auth_expired' and (result['check_id'].startswith('auth.')
                                    or result['check_id'].startswith('frontend.auth-')):
                                store.set_meta('expired_credentials', credential_fingerprint)
                                secrets['auth_paused'] = True
                            if result['status'] == 'rate_limited':
                                rate_limited = True
                            if phase != 'scan':
                                result.setdefault('details', {})['plan_fingerprint'] = current_plan
                                scope = 'guest' if result['check_id'].startswith('guest.') else (
                                    _account_scope(secrets['cookies']) if secrets['cookies'] else 'unconfigured')
                                result['check_id'] = 'endpoint.' + scope + '.' + result['check_id']
                            store.record(result, config['failure_threshold'])
                            emit({'type': 'check', **result})
                            observed.append(result)
                        if not stop_event.is_set() and store.has_check('monitor.' + phase):
                            store.record({'check_id': 'monitor.' + phase, 'status': 'ok',
                                          'summary': '巡检阶段执行恢复', 'details': {}}, config['failure_threshold'])
                    except Exception as exc:
                        # Dependencies sometimes attach full request URLs to errors.
                        result = {'check_id': 'monitor.' + phase, 'status': 'incomplete',
                                  'summary': '巡检阶段发生内部错误，本轮证据不完整',
                                  'details': {'error_type': type(exc).__name__}}
                        store.record(result, config['failure_threshold'])
                        emit({'type': 'check', **result})
                        observed.append(result)
                    if not stop_event.is_set():
                        store.set_meta('next_' + phase, time.time() + (max(1800, interval) if rate_limited else interval))
                heartbeat.set_phase('notifications')
                flush_alerts(store, secrets['webhook'], stop_event)
                store.prune(config['retention_days'])
                heartbeat.set_phase('idle')
                if once:
                    break
                observed.clear()
                stop_event.wait(15)
        finally:
            heartbeat.close()
            store.close()
    if any(result['status'] in ('incomplete', 'auth_expired', 'rate_limited', 'network_error',
                                'contract_error', 'sample_unavailable', 'blocked', 'not_exercised',
                                'unconfirmed', 'cleanup_failed', 'needs_input') for result in observed):
        return 2
    return 0
