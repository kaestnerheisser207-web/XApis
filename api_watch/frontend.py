"""Read-only frontend observations; never refresh the SDK's live registry.

Only a complete legacy webpack manifest produces a comparable snapshot. The
newer x-web ES module graph can be observed, but its generated/dynamic imports
cannot yet be exhaustively enumerated here, so it remains explicitly incomplete.
The on-disk cache contains derived public metadata, never HTML or JS source.
"""
import hashlib
import heapq
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urljoin, urlsplit

import requests

from utils.frontend import (SHELL_URL, parse_all_assets, parse_animation_frames,
                            parse_site_verification, shell_headers)
from utils.graphql_registry import (_OPERATION_RE, _extract_operations,
                                    _parse_feature_defaults)

_PREFIXES = {'client-web': '/responsive-web/client-web/',
             'x-web': '/x-web/x-web/'}
_CACHE_VERSION = 2
_SAFE_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,199}$')
_SAFE_ID = re.compile(r'^[A-Za-z0-9_-]{1,200}$')
_HEX = re.compile(r'^[0-9a-f]{64}$')
_HASHED_ASSET = re.compile(
    r'(?:\.[0-9a-f]{8,65}|-(?=[A-Za-z0-9_-]*[A-Z0-9])[A-Za-z0-9_-]{8,})\.js$')
_JS_LITERAL = re.compile(r'''["'`]([^"'`\s<>\\${}]+\.js)["'`]''')
_RELAY_PARAMS = re.compile(r'params\s*:\s*\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}')
_RELAY_FIELD = re.compile(
    r'''\b(id|name|operationKind)\s*:\s*(["'`])([\w-]+)\2''')
_QUERY_MARKER = re.compile(r'queryId\s*:\s*["\'][\w-]+["\']')
_MAX_BODY = 24 * 1024 * 1024
_MAX_DISCOVERED_ASSETS = 10000


class _StopScan(Exception):
    def __init__(self, status, reason, operation_names=()):
        self.status, self.reason = status, reason
        self.operation_names = operation_names


class _AssetLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and attrs.get('src'):
            self.urls.append(attrs['src'])
        if tag == 'link' and attrs.get('rel') in ('modulepreload', 'preload'):
            if attrs.get('href', '').endswith('.js'):
                self.urls.append(attrs['href'])


def _asset_url(url, family=None):
    """Allow only credential-free immutable JS paths on the public asset CDN."""
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.netloc != 'abs.twimg.com'
            or parsed.query or parsed.fragment
            or not re.fullmatch(r'/[A-Za-z0-9_./~-]+\.js', parsed.path)
            or any(part in ('.', '..') for part in parsed.path.split('/'))):
        return None
    families = [family] if family else _PREFIXES
    return url if any(parsed.path.startswith(_PREFIXES[f]) for f in families) else None


def _remaining(deadline, stop_event):
    if stop_event is not None and stop_event.is_set():
        raise _StopScan('incomplete', 'cancelled')
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _StopScan('incomplete', 'deadline_exceeded')
    return remaining


def _fetch(session, url, headers, proxy, timeout, deadline, stop_event, *, shell=False, auth=False):
    remaining = _remaining(deadline, stop_event)
    session.cookies.clear()
    response = None
    try:
        response = session.get(
            url, headers=headers, proxies={'http': proxy, 'https': proxy} if proxy else {},
            timeout=min(timeout, remaining), allow_redirects=False, stream=True)
        status = response.status_code
        if status == 429:
            raise _StopScan('rate_limited', 'shell_rate_limited' if shell else 'asset_rate_limited')
        if shell and auth and status == 401:
            raise _StopScan('auth_expired', 'shell_unauthorized')
        if 300 <= status < 400:
            location = response.headers.get('location', '')
            target = urlsplit(urljoin(url, location))
            if shell and auth and target.hostname == 'x.com' and target.path in ('/login', '/i/flow/login'):
                raise _StopScan('auth_expired', 'shell_login_redirect')
            # No redirect is followed, so neither Cookie nor proxy credentials
            # can be forwarded to a URL supplied by a response.
            raise _StopScan('incomplete', 'redirect_rejected')
        if status != 200:
            raise _StopScan('network_error' if status >= 500 else 'incomplete',
                            'shell_http_error' if shell else 'asset_http_error')
        parts, size = [], 0
        for chunk in response.iter_content(chunk_size=65536):
            _remaining(deadline, stop_event)
            size += len(chunk)
            if size > _MAX_BODY:
                raise _StopScan('incomplete', 'body_limit_exceeded')
            parts.append(chunk)
        raw = b''.join(parts)
        if not shell and ('text/html' in response.headers.get('content-type', '').lower()
                          or raw.lstrip().lower().startswith((b'<!doctype html', b'<html'))):
            raise _StopScan('incomplete', 'asset_not_javascript')
        return raw.decode('utf-8', errors='replace'), hashlib.sha256(raw).hexdigest()
    except requests.RequestException:
        # Exception text may contain the full proxy URL, headers, or Cookie.
        raise _StopScan('network_error', 'request_failed') from None
    finally:
        if response is not None:
            response.close()


def _operation_valid(name, operation):
    return (isinstance(name, str) and _SAFE_NAME.fullmatch(name)
            and isinstance(operation, dict)
            and isinstance(operation.get('queryId'), str)
            and _SAFE_ID.fullmatch(operation['queryId'])
            and operation.get('operationType') in ('query', 'mutation', 'subscription')
            and all(key in operation and (operation[key] is None
                    or (isinstance(operation[key], list)
                        and all(isinstance(item, str) and _SAFE_NAME.fullmatch(item)
                                for item in operation[key])))
                    for key in ('featureSwitches', 'fieldToggles')))


def _normalized(operation):
    return {'queryId': operation['queryId'], 'operationType': operation['operationType'],
            **{key: sorted(set(operation[key])) if operation[key] is not None else None
               for key in ('featureSwitches', 'fieldToggles')}}


def _merge(target, found):
    for name, operation in found.items():
        if not _operation_valid(name, operation):
            raise _StopScan('incomplete', 'unsupported_operation_metadata')
        operation = _normalized(operation)
        if name in target and target[name] != operation:
            raise _StopScan('incomplete', 'operation_conflict', (name,))
        target[name] = operation


def _derive(text, url, family, digest):
    operations = {}
    matches = list(_OPERATION_RE.finditer(text))
    for match in matches:
        # Per-match extraction preserves duplicate-name conflicts which the
        # existing bulk extractor otherwise resolves by last assignment.
        _merge(operations, _extract_operations(match.group(0)))
    if family == 'client-web' and len(_QUERY_MARKER.findall(text)) > len(matches):
        raise _StopScan('incomplete', 'unsupported_operation_metadata')
    if family == 'x-web':
        for match in _RELAY_PARAMS.finditer(text):
            body = match.group(1)
            fields = {field: value for field, _, value in _RELAY_FIELD.findall(body)}
            if fields.keys() >= {'id', 'name', 'operationKind'}:
                _merge(operations, {fields['name']: {
                    'queryId': fields['id'], 'operationType': fields['operationKind'],
                    # Relay providers do not establish the legacy switches
                    # contract. Unknown is distinct from a known empty list.
                    'featureSwitches': None, 'fieldToggles': None}})
    imports = set()
    for literal in _JS_LITERAL.findall(text):
        candidate = urljoin(url, literal)
        safe = _asset_url(candidate)
        if safe and not _asset_url(candidate, family):
            raise _StopScan('incomplete', 'mixed_frontend_assets')
        if safe:
            imports.add(safe)
    return {'version': _CACHE_VERSION, 'family': family, 'sha256': digest,
            'operations': operations, 'imports': sorted(imports)}


def _cache_path(root, family, url):
    return root / 'frontend-cache' / family / (hashlib.sha256(url.encode()).hexdigest() + '.json')


def _load_cached(root, family, url):
    if not _HASHED_ASSET.search(urlsplit(url).path):
        return None
    try:
        path = _cache_path(root, family, url)
        if path.stat().st_size > 4 * 1024 * 1024:
            return None
        data = json.loads(path.read_text(encoding='utf-8'))
        if (data.get('version') != _CACHE_VERSION or data.get('family') != family
                or not isinstance(data.get('sha256'), str) or not _HEX.fullmatch(data['sha256'])
                or not isinstance(data.get('operations'), dict)
                or not all(_operation_valid(k, v) for k, v in data['operations'].items())
                or not isinstance(data.get('imports'), list)
                or not all(isinstance(u, str) and _asset_url(u, family) for u in data['imports'])):
            return None
        # Last-use time lets pruning retain the active release even when its
        # immutable assets have not needed another download for several weeks.
        path.touch()
        return data
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _save_cached(root, family, url, data):
    if not _HASHED_ASSET.search(urlsplit(url).path):
        return
    path = _cache_path(root, family, url)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.partial-', delete=False) as fp:
            name = fp.name
            json.dump(data, fp, ensure_ascii=True, sort_keys=True)
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def prune_frontend_cache(state_dir: Path, *, max_age_days: float = 30,
                         max_entries: int = 6000) -> int:
    """Remove only this module's derived cache files, oldest/inactive first."""
    if max_age_days < 0 or max_entries < 0:
        raise ValueError('cache limits must be nonnegative')
    cutoff = time.time() - max_age_days * 86400
    entries = []
    for family in _PREFIXES:
        directory = Path(state_dir) / 'frontend-cache' / family
        if directory.is_symlink():
            continue
        for path in directory.glob('*.json'):
            if not _HEX.fullmatch(path.stem) or path.is_symlink():
                continue
            try:
                if path.is_file():
                    entries.append((path.stat().st_mtime, path))
            except OSError:
                continue
    entries.sort(key=lambda item: item[0])
    removed = 0
    for index, (modified, path) in enumerate(entries):
        if modified >= cutoff and index >= len(entries) - max_entries:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _asset_priority(url):
    name = urlsplit(url).path.rsplit('/', 1)[-1].lower()
    # Observe explicit operation modules before expanding broad UI route maps.
    return (0 if any(word in name for word in ('query', 'mutation', 'graphql')) else 1, url)


def _material_hashes(html, defaults):
    materials = {}
    for name, parser in [('site_verification', parse_site_verification),
                         ('animation_frames', parse_animation_frames)]:
        try:
            value = parser(html)
            materials[name] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
        except (ValueError, RuntimeError, KeyError):
            pass
    key = defaults.get('responsive_web_castle_public_key')
    if isinstance(key, str):
        materials['castle_public_key'] = hashlib.sha256(key.encode()).hexdigest()
    return materials


def scan_frontend(state_dir: Path, *, cookies: str = '', proxy: str | None = None,
                  timeout: float = 20, max_assets: int = 600,
                  max_seconds: float = 300, stop_event=None) -> dict:
    """Collect one shell and its assets, without mutating any SDK state.

    An auth observation means a Cookie was supplied, not that authentication
    succeeded; the independent session probe is responsible for that proof.
    """
    scope = 'auth' if cookies else 'guest'
    result = {'check_id': f'frontend.{scope}', 'status': 'incomplete',
              'summary': '前端采集不完整，保留已有观测基线', 'details': {
                  'scope': scope, 'assets_total': 0, 'assets_downloaded': 0,
                  'assets_cached': 0, 'operations': 0}, 'snapshot': None, 'observation': None}
    details = result['details']
    shell_session = asset_session = None
    deadline = time.monotonic() + max_seconds
    try:
        if timeout <= 0 or max_assets < 1 or max_seconds <= 0:
            raise _StopScan('incomplete', 'invalid_scan_limits')
        shell_session, asset_session = requests.Session(), requests.Session()
        shell_session.trust_env = asset_session.trust_env = False
        headers = shell_headers()
        if cookies:
            headers['cookie'] = cookies
        html, _ = _fetch(shell_session, 'https://x.com/home' if cookies else SHELL_URL,
                         headers, proxy, timeout, deadline, stop_event, shell=True, auth=bool(cookies))
        links = _AssetLinks()
        links.feed(html)
        families = {family for family, prefix in _PREFIXES.items()
                    if any(prefix in url for url in links.urls)}
        if len(families) != 1:
            raise _StopScan('incomplete', 'mixed_frontend_assets' if families else 'unknown_frontend')
        family = families.pop()
        details['family'] = family
        result['observation'] = {'family': family, 'scope': scope,
                                 'operations': {}, 'complete': False}
        # This is a complete observation of *entry URLs in this one page*, not
        # evidence that all GraphQL operations or dynamic imports were found.
        entries = sorted({url for url in links.urls if _asset_url(url, family)})
        details['observed_fingerprint'] = _fingerprint({'family': family, 'scope': scope,
                                                       'entry_assets': entries})
        details['entry_assets'] = len(entries)
        details['fingerprint_coverage'] = 'shell-entry-urls'
        defaults = {}
        if family == 'client-web':
            try:
                assets = set(parse_all_assets(html).values())
                defaults = _parse_feature_defaults(html)
            except (ValueError, TypeError, KeyError, RuntimeError):
                raise _StopScan('incomplete', 'manifest_or_defaults_unsupported') from None
            details['coverage'] = 'client-web-manifest'
        else:
            assets = {url for url in links.urls if _asset_url(url, family)}
            details['coverage'] = 'x-web-static-imports-only'
        assets.update(url for url in links.urls if _asset_url(url, family))
        if not assets or any(not _asset_url(url, family) for url in assets):
            raise _StopScan('incomplete', 'invalid_asset_manifest')
        operations, hashes = {}, {}
        queued = set(assets)
        if len(queued) > _MAX_DISCOVERED_ASSETS:
            raise _StopScan('incomplete', 'discovery_limit_exceeded')
        queue = [_asset_priority(url) for url in assets]
        heapq.heapify(queue)
        asset_headers = {'user-agent': headers['user-agent'], 'accept': '*/*'}
        while queue:
            _remaining(deadline, stop_event)
            details['assets_total'] = len(queued)
            if len(hashes) >= max_assets:
                raise _StopScan('incomplete', 'asset_limit_exceeded')
            _, url = heapq.heappop(queue)
            data = _load_cached(Path(state_dir), family, url)
            if data is None:
                text, digest = _fetch(asset_session, url, asset_headers, proxy, timeout,
                                      deadline, stop_event)
                data = _derive(text, url, family, digest)
                _save_cached(Path(state_dir), family, url, data)
                details['assets_downloaded'] += 1
            else:
                details['assets_cached'] += 1
            _merge(operations, data['operations'])
            details['operations'] = len(operations)
            result['observation']['operations'].update({name: {
                'queryId': op['queryId'], 'operationType': op['operationType']}
                for name, op in data['operations'].items()})
            hashes[url] = data['sha256']
            for imported in data['imports']:
                if imported not in queued:
                    if len(queued) >= _MAX_DISCOVERED_ASSETS:
                        raise _StopScan('incomplete', 'discovery_limit_exceeded')
                    queued.add(imported)
                    heapq.heappush(queue, _asset_priority(imported))
        _remaining(deadline, stop_event)
        details['observed_assets_fingerprint'] = _fingerprint(hashes)
        details['observed_operations_fingerprint'] = _fingerprint(operations)
        if family == 'x-web':
            raise _StopScan('incomplete', 'x_web_coverage_unverified')
        if not operations:
            raise _StopScan('incomplete', 'no_operations')
        referenced = {name for op in operations.values()
                      for key in ('featureSwitches', 'fieldToggles') for name in op[key]}
        materials = _material_hashes(html, defaults)
        for url, digest in hashes.items():
            if re.search(r'/(?:ondemand\.(?:s|castle)\.|castle[.-])', url):
                materials['asset:' + urlsplit(url).path.rsplit('/', 1)[-1]] = digest
        result['snapshot'] = {
            'family': family, 'scope': scope, 'complete': True,
            'coverage': 'client-web-manifest', 'operations': dict(sorted(operations.items())),
            'featureDefaults': {name: defaults[name] for name in sorted(referenced)
                                if isinstance(defaults.get(name), bool)},
            'materials': materials, 'assets': dict(sorted(hashes.items()))}
        details['feature_defaults_missing'] = sum(not isinstance(defaults.get(name), bool)
                                                  for name in referenced)
        details['materials'] = len(materials)
        result.update(status='ok', summary='前端观测完成，已生成候选快照')
    except _StopScan as exc:
        if result['observation'] is not None:
            for name in exc.operation_names:
                result['observation']['operations'].pop(name, None)
            details['operations'] = len(result['observation']['operations'])
        result['status'] = exc.status
        details['reason'] = exc.reason
        result['summary'] = {
            'incomplete': '前端采集不完整，保留已有观测基线',
            'network_error': '前端采集网络失败，保留已有观测基线',
            'rate_limited': '前端采集受到限流，本轮停止请求',
            'auth_expired': '前端会话未通过鉴权，需要更新监测会话',
        }[exc.status]
    except Exception:
        # Parser, filesystem and unexpected transport failures have the same
        # safe boundary: never expose exception text or produce deletions.
        details['reason'] = 'scan_failed'
    finally:
        for session in (shell_session, asset_session):
            if session is not None:
                session.close()
    return result


def _mapping_diff(before, after):
    return {'added': {k: after[k] for k in sorted(after.keys() - before.keys())},
            'removed': {k: before[k] for k in sorted(before.keys() - after.keys())},
            'changed': {k: {'before': before[k], 'after': after[k]}
                        for k in sorted(before.keys() & after.keys()) if before[k] != after[k]}}


def diff_snapshots(baseline, candidate) -> dict:
    """Compare complete observations only, ignoring timestamp/list-order noise."""
    result = {'comparable': False, 'changed': False, 'reason': 'incomplete_snapshot',
              'operations': {'added': [], 'removed': [], 'changed': {}},
              'assets': {'added': [], 'removed': [], 'changed': []},
              'featureDefaults': {'added': {}, 'removed': {}, 'changed': {}},
              'materials': {'added': {}, 'removed': {}, 'changed': {}}}
    if not all(isinstance(s, dict) and s.get('complete') is True for s in (baseline, candidate)):
        return result
    if any(baseline.get(key) != candidate.get(key) for key in ('family', 'scope', 'coverage')):
        result['reason'] = 'observation_scope_changed'
        return result
    before = {k: _normalized(v) for k, v in baseline['operations'].items()}
    after = {k: _normalized(v) for k, v in candidate['operations'].items()}
    changes = _mapping_diff(before, after)
    result['operations'] = {
        'added': list(changes['added']), 'removed': list(changes['removed']),
        'changed': {k: {field: {'before': values['before'][field], 'after': values['after'][field]}
                        for field in values['before'] if values['before'][field] != values['after'][field]}
                    for k, values in changes['changed'].items()}}
    assets = _mapping_diff(baseline.get('assets', {}), candidate.get('assets', {}))
    result['assets'] = {key: list(value) for key, value in assets.items()}
    for key in ('featureDefaults', 'materials'):
        result[key] = _mapping_diff(baseline.get(key, {}), candidate.get(key, {}))
    result.update(comparable=True, reason='compared')
    result['changed'] = any(any(value.values()) for key, value in result.items()
                            if key in ('operations', 'assets', 'featureDefaults', 'materials'))
    return result
