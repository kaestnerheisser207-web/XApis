"""X Chat 账号初始化/恢复：参考公开注册流程，Juicebox 使用官方 Chat XDK。

这里不重置既有账号密钥，也不把“本地没文件”当成“账号尚未注册”。
私钥先持久化再注册；注册/备份结果不明时保留状态，后续只核对或恢复。
"""

import base64
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time

from x_apis.x_api import XAPI
from x_apis.x_dm_api import XChatAPI
from utils.xchat_codec import message_event_summary


class IdentityError(RuntimeError):
    """可公开的状态错误；消息中不带 PIN、token 或私钥。"""


def sdk_call(action, *, proxies=None, **arguments):
    """在独立进程中配置 SDK 的 reqwest 代理，不改变主进程环境。"""
    environment = os.environ.copy()
    for name in list(environment):
        if name.lower() in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy') or name.startswith('X_'):
            environment.pop(name, None)
    proxy = (proxies or {}).get('https') or (proxies or {}).get('http')
    if proxy:
        environment.update(HTTPS_PROXY=proxy, HTTP_PROXY=proxy, ALL_PROXY=proxy,
                           https_proxy=proxy, http_proxy=proxy, all_proxy=proxy,
                           NO_PROXY='', no_proxy='')
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name('xchat_sdk_worker.py'))],
            input=json.dumps({'action': action, **arguments}), text=True,
            capture_output=True, env=environment, timeout=120)
        payload = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        raise IdentityError('SDK 未返回可确认结果；保留本地状态，勿自动重试') from None
    if result.returncode or not payload.get('ok'):
        code = payload.get('error')
        if code not in ('invalid_pin', 'not_registered', 'rate_limited', 'invalid_auth', 'upgrade_required'):
            code = 'sdk_error'
        remaining = payload.get('guesses_remaining')
        suffix = f'，剩余尝试次数 {remaining}' if type(remaining) is int else ''
        raise IdentityError(f'Juicebox {code}{suffix}；未自动重试')
    return payload['result']


def _uid(value):
    if not isinstance(value, str) or not value.isascii() or not value.isdigit() or int(value) <= 0:
        raise IdentityError('账号 ID 无效')
    return value


def _viewer(auth):
    payload = XAPI.get_viewer(auth)
    if payload.get('errors'):
        raise IdentityError('无法确认当前登录账号')
    try:
        user = payload['data']['viewer']['user_results']['result']
        return _uid(user['rest_id']), user['core']['screen_name']
    except (KeyError, TypeError):
        raise IdentityError('无法确认当前登录账号') from None


def public_records(auth, uid, *, tokens=False, allow_managed=False):
    payload = XChatAPI.get_public_keys(auth, [uid], include_juicebox_tokens=tokens)
    try:
        if payload.get('errors'):
            raise IdentityError('公钥查询失败，不能据此判断账号未初始化')
        rows = payload['data']['user_results_by_rest_ids']
        if len(rows) != 1 or rows[0]['rest_id'] != uid or rows[0]['result']['__typename'] != 'User':
            raise IdentityError('公钥响应账号不匹配')
        keys = rows[0]['result']['get_public_keys']
        if keys['__typename'] != 'GetPublicKeysResult':
            raise IdentityError('公钥服务返回错误，不能进入首次注册')
        if keys.get('is_managed_pin_user') and not allow_managed:
            raise IdentityError('该账号使用 Managed PIN，当前只支持 Custom PIN')
        entries = keys['public_keys_with_token_map']
        if not isinstance(entries, list):
            raise IdentityError('公钥响应格式无效')
        versions = set()
        for entry in entries:
            record = entry['public_key_with_metadata']
            version = _uid(record['version'])
            if version in versions:
                raise IdentityError('公钥响应版本重复')
            versions.add(version)
            for name in ('public_key', 'signing_public_key', 'identity_public_key_signature'):
                if not isinstance(record['public_key'][name], str) or not record['public_key'][name]:
                    raise IdentityError('公钥记录不完整')
        return sorted(entries, key=lambda e: int(e['public_key_with_metadata']['version']), reverse=True)
    except (KeyError, TypeError, AttributeError):
        raise IdentityError('公钥响应不完整，不能据此进入首次注册') from None


def _read(path):
    path = Path(path)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, encoding='utf-8') as f:
        info = os.fstat(f.fileno())
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > 131072):
            raise IdentityError('身份文件必须为权限 0600 的小型普通文件')
        try:
            data = json.load(f)
            if data['format'] != 'xapis-xchat-identity-v1':
                raise ValueError()
            _uid(data['sender_id'])
            if data['phase'] not in ('prepared', 'registration_attempted', 'backup_attempted', 'backup_verified', 'ready'):
                raise ValueError()
            if not isinstance(data['registration'], dict):
                raise ValueError()
            base64.b64decode(data['key_blob_b64'], validate=True)
            return data
        except (ValueError, KeyError, TypeError):
            raise IdentityError('身份文件无效') from None


def _save(path, data):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.xchat-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _locked(path):
    path = Path(path)
    if path.is_symlink():
        raise IdentityError('身份或会话文件不能是符号链接')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path) + '.lock', os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise IdentityError('该身份文件正在被另一个进程使用') from None
        try:
            yield path
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _same_public(left, right):
    try:
        for name in ('public_key', 'signing_public_key'):
            a, b = left[name], right[name]
            if not isinstance(a, str) or not isinstance(b, str) or not a or not b:
                return False
            if base64.b64decode(a + '=' * (-len(a) % 4), validate=True) != base64.b64decode(b + '=' * (-len(b) % 4), validate=True):
                return False
        return True
    except (KeyError, ValueError, TypeError):
        return False


def _config(entry):
    config = entry.get('token_map')
    try:
        embedded = json.loads(config['key_store_token_map_json'])
        realms = embedded['realms']
        tokens = config['token_map']
        if (not realms or not tokens or not all(v['value']['token'] for v in tokens)
                or {r['id'].lower() for r in realms} != {v['key'].lower() for v in tokens}):
            raise ValueError()
        if not 1 <= embedded['recover_threshold'] <= embedded['register_threshold'] <= len(realms):
            raise ValueError()
        if type(config['max_guess_count']) is not int or not 1 <= config['max_guess_count'] <= 65535:
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise IdentityError('缺少完整 Juicebox 配置，拒绝猜测 realm 或重新注册') from None
    return config


def inspect_identity(auth, path=None):
    uid, name = _viewer(auth)
    records = public_records(auth, uid)
    result = {'sender_id': uid, 'screen_name': name,
              'state': 'public_keys_registered' if records else 'needs_setup',
              'key_versions': [r['public_key_with_metadata']['version'] for r in records],
              'pin_backup_state': 'unknown'}
    if path and Path(path).exists():
        local = _read(path)
        if local['sender_id'] != uid:
            raise IdentityError('身份文件属于另一个账号')
        result['local_phase'] = local['phase']
        result['local_version'] = local.get('version')
        result['local_matches_registered'] = any(
            r['public_key_with_metadata']['version'] == local.get('version')
            and _same_public(local['registration'], r['public_key_with_metadata']['public_key'])
            for r in records)
    return result


def _validate_new_pin(pin):
    # Match the web/mautrix four-digit UI and reject SDK-forbidden weak PINs
    # before making the public-key registration request.
    if (not isinstance(pin, str) or len(pin) != 4 or not pin.isascii() or not pin.isdigit()
            or len(set(pin)) == 1 or pin in '0123456789' or pin in '9876543210'):
        raise IdentityError('新 PIN 必须为四位数字，且不能重复同一数字或连续递增/递减')


def setup_identity(auth, pin, path, *, resume=False):
    _validate_new_pin(pin)
    uid, name = _viewer(auth)
    with _locked(path) as path:
        records = public_records(auth, uid, tokens=True)
        if path.exists():
            if not resume:
                raise IdentityError('身份文件已存在；先检查状态，仅中断恢复使用 --resume')
            state = _read(path)
            if state['sender_id'] != uid:
                raise IdentityError('身份文件属于另一个账号')
            sdk = sdk_call('validate', key_blob_b64=state['key_blob_b64'], version=state.get('version'))
            if not _same_public(sdk, state['registration']):
                raise IdentityError('本地私钥与注册记录不匹配')
        else:
            if records:
                raise IdentityError('账号已有注册公钥，请使用 chat-unlock；不会替换既有密钥')
            generated = sdk_call('generate')
            state = {'format': 'xapis-xchat-identity-v1', 'sender_id': uid, 'screen_name': name,
                     'phase': 'prepared', 'request_version': str(time.time_ns() // 1000000),
                     'key_blob_b64': generated['key_blob_b64'], 'registration': generated['registration']}
            _save(path, state)
        matches = [r for r in records if _same_public(r['public_key_with_metadata']['public_key'], state['registration'])]
        if len(matches) > 1:
            raise IdentityError('同一公钥存在多个版本，需先人工核对')
        if matches:
            entry = matches[0]
            if (state['phase'] in ('backup_attempted', 'backup_verified', 'ready')
                    and state.get('version') != entry['public_key_with_metadata']['version']):
                raise IdentityError('已开始备份的身份版本发生变化，请先核对版本，不能自动切换')
            state['version'] = entry['public_key_with_metadata']['version']
        else:
            if records:
                raise IdentityError('服务端已有另一套公钥，禁止覆盖')
            if state['phase'] != 'prepared':
                raise IdentityError('上次注册结果未确认，当前未查到匹配公钥；不会自动重复注册')
            state['phase'] = 'registration_attempted'
            _save(path, state)
            result = XChatAPI.add_public_key(auth, state['registration'], state['request_version'])
            state['version'] = result['version']
            entry = {'token_map': result['token_map']}
        config = _config(entry)
        if state['phase'] == 'ready':
            return {'state': 'ready', 'sender_id': uid, 'version': state['version'], 'already_ready': True}
        # An interrupted backup may already exist. Only recover it; never
        # register again with a possibly different PIN after an unknown result.
        recovery_only = state['phase'] in ('backup_attempted', 'backup_verified')
        state['phase'] = 'backup_attempted'
        _save(path, state)
        sdk = sdk_call('unlock' if recovery_only else 'setup', proxies=auth.proxies,
                       pin=pin, token_map=config, key_blob_b64=state['key_blob_b64'], version=state['version'])
        if not _same_public(sdk, state['registration']):
            raise IdentityError('恢复所得密钥与本次注册身份不匹配')
        # Persist confirmed recovery before the final server-side readback.
        state['phase'] = 'backup_verified'
        _save(path, state)
        actual = public_records(auth, uid)
        if not any(r['public_key_with_metadata']['version'] == state['version']
                   and _same_public(r['public_key_with_metadata']['public_key'], state['registration']) for r in actual):
            raise IdentityError('备份已核对，但服务端公钥回读未匹配；保留状态待核对')
        state['phase'] = 'ready'
        _save(path, state)
        return {'state': 'ready', 'sender_id': uid, 'version': state['version'], 'backup_verified': True}


def unlock_identity(auth, pin, path, *, version=None):
    if not isinstance(pin, str) or not pin:
        raise IdentityError('PIN 不能为空')
    uid, name = _viewer(auth)
    with _locked(path) as path:
        if path.exists():
            raise IdentityError('目标身份文件已存在，请指定新文件；不会覆盖现有私钥')
        records = public_records(auth, uid, tokens=True)
        if not records:
            raise IdentityError('账号尚未注册，请使用 chat-setup')
        candidates = [r for r in records if version is None or r['public_key_with_metadata']['version'] == version]
        if not candidates:
            raise IdentityError('未找到指定公钥版本')
        if version is None and len(candidates) != 1:
            raise IdentityError('存在多个公钥版本，请用 --key-version 明确选择；不逐个猜 PIN')
        entry = candidates[0]
        metadata = entry['public_key_with_metadata']
        sdk = sdk_call('unlock', proxies=auth.proxies, pin=pin, token_map=_config(entry), version=metadata['version'])
        if not _same_public(sdk, metadata['public_key']):
            raise IdentityError('恢复所得密钥与服务端公钥不匹配，不保存')
        state = {'format': 'xapis-xchat-identity-v1', 'sender_id': uid, 'screen_name': name,
                 'phase': 'ready', 'version': metadata['version'], 'key_blob_b64': sdk['key_blob_b64'],
                 'registration': metadata['public_key']}
        _save(path, state)
        return {'state': 'ready', 'sender_id': uid, 'version': state['version'], 'recovered': True}


def export_conversation_material(auth, path, conversation_id, output, *,
                                 signature_version, contents_encryption_version=None,
                                 expected_key_version=None, expected_key_sha256=None, events=None):
    """从已恢复的账号身份解包已有单聊的会话密钥，输出 chat-send --keys 所需配置。"""
    from utils.xchat_crypto import XChatKeyMaterial, prepare_text_message
    uid, _ = _viewer(auth)
    state = _read(path)
    if state['phase'] != 'ready' or state['sender_id'] != uid:
        raise IdentityError('身份未就绪或不属于当前账号')
    try:
        ids = conversation_id.replace('-', ':').split(':')
        if len(ids) != 2 or uid not in ids:
            raise IdentityError('仅支持包含当前账号的已有单聊')
        conversation_id = ':'.join(sorted((_uid(x) for x in ids), key=int))
    except (AttributeError, ValueError):
        raise IdentityError('会话 ID 无效') from None
    validated = sdk_call('validate', key_blob_b64=state['key_blob_b64'], version=state['version'])
    if not _same_public(validated, state['registration']):
        raise IdentityError('本地私钥与公钥不匹配')
    signing_keys = []
    for member in set(ids):
        records = public_records(auth, member, allow_managed=member != uid)
        if member == uid and not any(r['public_key_with_metadata']['version'] == state['version']
                and _same_public(r['public_key_with_metadata']['public_key'], state['registration']) for r in records):
            raise IdentityError('当前账号的本地身份已不匹配服务端公钥')
        for record in records:
            metadata = record['public_key_with_metadata']
            public = metadata['public_key']
            signing_keys.append({'user_id': member, 'public_key_version': metadata['version'],
                                 'public_key': public['signing_public_key'],
                                 'identity_public_key': public['public_key'],
                                 'identity_public_key_signature': public['identity_public_key_signature']})
    if events is None:
        response = XChatAPI.get_conversation_page(auth, conversation_id)
        if response.get('errors'):
            raise IdentityError('会话读取失败')
        page = (response.get('data') or {}).get('get_conversation_page') or {}
        events = page.get('encoded_message_events', []) + page.get('missing_conversation_key_change_events', [])
    try:
        selected, seen = [], set()
        for event in events:
            summary = message_event_summary(event)
            if summary['conversation_id'] == conversation_id and summary['event_type'] == 3 and event not in seen:
                selected.append(event)
                seen.add(event)
    except (KeyError, TypeError, ValueError):
        raise IdentityError('会话密钥事件无效') from None
    if not selected:
        raise IdentityError('未取得该会话的密钥事件；此命令不创建新会话')
    recovered = sdk_call('conversation_keys', key_blob_b64=state['key_blob_b64'],
                         version=state['version'], sender_id=uid, signing_keys=signing_keys, events=selected)
    version = recovered['latest_version']
    if version is None or str(version) not in recovered['keys']:
        raise IdentityError('当前账号密钥无法解开该会话；需要正确的历史身份版本')
    key = base64.b64decode(recovered['keys'][str(version)], validate=True)
    if expected_key_version is not None and str(version) != expected_key_version:
        raise IdentityError('回读的会话密钥版本与本次创建不一致')
    if expected_key_sha256 is not None:
        import hashlib
        import hmac
        if not hmac.compare_digest(hashlib.sha256(key).hexdigest(), expected_key_sha256):
            raise IdentityError('回读的会话密钥与本次生成不一致')
    signing = base64.b64decode(recovered['signing_key_b64'], validate=True)
    material = XChatKeyMaterial(uid, conversation_id, key, str(version), signing,
                               state['version'], signature_version, contents_encryption_version)
    # Exercise the same validation used before sending, entirely offline.
    prepare_text_message('validation', material)
    data = {'sender_id': uid, 'conversation_id': conversation_id,
            'conversation_key_b64': recovered['keys'][str(version)], 'conversation_key_version': str(version),
            'signing_key_b64': recovered['signing_key_b64'], 'public_key_version': state['version'],
            'signing_public_key_b64': state['registration']['signing_public_key'],
            'signature_version': signature_version, 'contents_encryption_version': contents_encryption_version,
            'conversation_token': None}
    with _locked(output) as destination:
        if destination.exists():
            raise IdentityError('目标会话密钥文件已存在，不覆盖')
        _save(destination, data)
    return {'state': 'conversation_ready', 'sender_id': uid, 'conversation_id': conversation_id,
            'key_version': str(version), 'output': str(output)}
