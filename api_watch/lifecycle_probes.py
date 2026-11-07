"""Explicit, single-attempt login and X Chat lifecycle canaries.

These are manual scenarios, never background retries.  The business Messages
facade owns identity files, encryption and send idempotency.  Scoped forwarding
observers prove that a mutation actually happened; cached facade results cannot
claim coverage of registration or conversation-key distribution.
"""
from contextlib import contextmanager
from functools import wraps
import importlib.util
from pathlib import Path
import shutil
import threading
import uuid

from x_apis.x_dm_api import XChatAPI


def _spec(check_id, operation, source, requirements, method='POST'):
    return {'id': 'lifecycle.' + check_id, 'operation': operation, 'method': method,
            'source': source, 'requirements': requirements,
            'risk': 'lifecycle', 'scenario': 'lifecycle'}


SPECS = [
    _spec('login.jetfuel', 'JetfuelLogin', 'x_apis.login_api.XJetfuelLoginApi.login',
          ['lifecycle.login_enabled', 'secrets.login', 'samples.test_account_id']),
    _spec('login.legacy', 'LegacyOnboardingLogin', 'x_apis.login_api.XLoginApi.login',
          ['lifecycle.legacy_login_enabled', 'secrets.login', 'samples.test_account_id']),
    _spec('chat.token', 'GenerateXChatTokenMutation', 'x_apis.x_dm_api.XChatAPI.generate_chat_token',
          ['lifecycle.chat_token', 'cookies', 'samples.test_account_id']),
    _spec('chat.identity_register', 'AddXChatPublicKeyMutation', 'x_apis.x_dm_api.XChatAPI.add_public_key',
          ['lifecycle.chat_identity', 'secrets.chat_pin', 'unregistered_test_account']),
    _spec('chat.identity_backup', 'ChatSDKSetupAndUnlock', 'utils.xchat_identity.setup_identity',
          ['lifecycle.chat_identity', 'secrets.chat_pin', 'chatxdk']),
    _spec('chat.identity_recover', 'ChatSDKUnlock', 'utils.xchat_identity.unlock_identity',
          ['lifecycle.chat_identity_recover', 'secrets.chat_pin', 'chatxdk']),
    _spec('chat.new_conversation', 'AddEncryptedConversationKeysMutation',
          'x_apis.x_dm_api.XChatAPI.add_conversation_keys',
          ['lifecycle.chat_new_conversation', 'samples.peer_username', 'samples.peer_user_id', 'chat_identity']),
    _spec('chat.send_new', 'SendMessageCreateMutation', 'x_apis.x_dm_api.XChatAPI.send_message',
          ['lifecycle.chat_new_conversation', 'samples.peer_username', 'samples.peer_user_id', 'chat_identity']),
    _spec('chat.send_existing', 'SendMessageCreateMutation', 'x_apis.x_dm_api.XChatAPI.send_message',
          ['lifecycle.chat_send', 'samples.peer_username', 'samples.peer_user_id', 'chat_identity']),
    _spec('chat.readback', 'GetConversationPageQuery', 'x_apis.messages.Messages.list',
          ['samples.peer_username', 'samples.peer_user_id', 'chat_identity'], 'GET'),
]

_OBSERVER_LOCK = threading.RLock()


class LifecycleError(RuntimeError):
    """Only fixed public text/codes; never retain tokens, responses or secrets."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@contextmanager
def _observe(*, register=False, new_conversation=False, allow_send=False, conversation_id=None):
    """Forward real calls, recording successes and preventing unapproved writes.

    Lifecycle runs hold the monitor's process lock.  This additional lock keeps
    the temporary class descriptors scoped if callers use threads. No response
    is substituted and no request/response body is recorded.
    """
    rules = {'add_public_key': register, 'add_conversation_keys': new_conversation,
             'send_message': allow_send}
    succeeded = {name: 0 for name in rules}
    with _OBSERVER_LOCK:
        originals = {name: XChatAPI.__dict__[name] for name in rules}
        try:
            for name, allowed in rules.items():
                original = getattr(XChatAPI, name)

                def forwarding(*args, _name=name, _allowed=allowed,
                               _original=original, **kwargs):
                    if not _allowed:
                        raise LifecycleError('blocked', '当前场景不允许隐式注册身份、分发新会话密钥或发送消息')
                    if conversation_id and _name in ('send_message', 'add_conversation_keys'):
                        if _name == 'send_message':
                            actual = kwargs.get('conversation_id', args[1] if len(args) > 1 else None)
                        else:
                            prepared = kwargs.get('prepared', args[1] if len(args) > 1 else {})
                            actual = prepared.get('conversation_id')
                        if actual != conversation_id:
                            raise LifecycleError('blocked', '发送前会话成员变化，拒绝向未授权收件人写入')
                    result = _original(*args, **kwargs)
                    succeeded[_name] += 1
                    return result

                setattr(XChatAPI, name, staticmethod(wraps(original)(forwarding)))
            yield succeeded
        finally:
            for name, original in originals.items():
                setattr(XChatAPI, name, original)


def _ids(prefix):
    return [row['id'] for row in SPECS if row['id'].startswith(prefix)]


def _require(condition, message):
    if not condition:
        raise LifecycleError('contract_error', message)


def _sdk_available():
    return all(importlib.util.find_spec(name) is not None
               for name in ('chat_xdk', 'cryptography', 'nacl'))


def _native_profile(profile, inherited='native'):
    """Check every selected profile; never silently change compression format."""
    if not isinstance(profile, dict):
        return inherited != 'fallback'
    compression = profile.get('compression', inherited)
    by_action = profile.get('raw_by_action') or profile.get('rawByAction')
    sequence = profile.get('raw_sequence') or profile.get('rawSequence')
    if isinstance(by_action, dict):
        return any(_native_profile(value, compression) for value in by_action.values())
    if isinstance(sequence, list):
        return any(_native_profile(value, compression) for value in sequence)
    return compression != 'fallback'


def _login(ctx, legacy=False):
    check_id = 'lifecycle.login.' + ('legacy' if legacy else 'jetfuel')
    key = 'legacy_login_enabled' if legacy else 'login_enabled'
    if not ctx.config.get('lifecycle', {}).get(key):
        ctx.block([check_id], '手动登录探针未启用', status='disabled')
        return
    credentials = ctx.secrets.get('login') or {}
    expected = ctx.samples.get('test_account_id')
    if (not isinstance(credentials, dict) or not credentials.get('username')
            or not credentials.get('password') or not expected):
        ctx.block([check_id], '需要登录凭据文件及 samples.test_account_id')
        return
    profile = credentials.get('profile')
    mode = credentials.get('mode', 'pure')
    if mode not in ('pure', 'compat-node'):
        ctx.block([check_id], '登录 mode 只能为 pure 或 compat-node')
        return
    if not legacy:
        if mode == 'compat-node':
            from utils.castle_token import NODE, RUNNER
            if not shutil.which(NODE) or not Path(RUNNER).is_file():
                ctx.block([check_id], 'compat-node 登录需要 Node 和随 SDK 发布的 Castle runner')
                return
        elif profile is None:
            ctx.block([check_id], '推荐登录流需要同一前端来源的完整 profile')
            return
        elif _native_profile(profile):
            from utils.chromium_deflate import available
            if not available():
                ctx.block([check_id], '当前平台缺少 profile 所需的 Chromium native 压缩后端；未自动改用 fallback')
                return
    classifications = {}

    def attempt():
        from x_apis.login_api import XJetfuelLoginApi, XLoginApi, LoginChallenge, LoginRateLimited
        from x_apis.x_api import XAPI
        proxy = ctx.secrets.get('proxy')
        proxies = {'http': proxy, 'https': proxy} if proxy else None
        api = (XLoginApi(proxies=proxies) if legacy else
               XJetfuelLoginApi(proxies=proxies) if mode == 'compat-node' else
               XJetfuelLoginApi(proxies=proxies, pure_rl_values=profile, pure_required=True))
        try:
            auth = api.login(credentials['username'], credentials['password'])
            payload = XAPI.get_viewer(auth)
            try:
                found = payload['data']['viewer']['user_results']['result']['rest_id']
                valid = not payload.get('errors') and str(found) == str(expected)
            except (KeyError, TypeError):
                valid = False
            _require(valid, '登录返回身份未匹配指定测试账号')
            # Nothing secret crosses the context's result boundary.
            return {'identity_verified': True}
        except LoginChallenge:
            classifications['status'] = 'needs_input'
            raise LifecycleError('needs_input', '登录需要人工处理验证码或安全挑战；未自动提交') from None
        except LoginRateLimited:
            classifications['status'] = 'rate_limited'
            raise LifecycleError('rate_limited', '登录受限；未自动重试') from None
        finally:
            session = getattr(api, 'session', None)
            if session is not None:
                session.close()

    result = ctx.step(check_id, attempt, mutating=True)
    if not result.ok and classifications:
        ctx.block([check_id], '登录需要人工处理或解除限流；本轮未重试', status=classifications['status'])


def _identity(ctx):
    config = ctx.config.get('lifecycle', {})
    register_ids = ['lifecycle.chat.identity_register', 'lifecycle.chat.identity_backup']
    recover_id = 'lifecycle.chat.identity_recover'
    selected = config.get('chat_identity') or config.get('chat_identity_recover')
    if not selected:
        ctx.block(register_ids + [recover_id], '身份初始化和恢复探针未启用', status='disabled')
        return
    if not _sdk_available():
        ctx.block(register_ids + [recover_id], '缺少 chatxdk、cryptography 或 PyNaCl')
        return
    pin = ctx.secrets.get('chat_pin')
    if not pin:
        ctx.block(register_ids + [recover_id], '需要凭据文件中的明确 chat_pin；不猜测默认 PIN')
        return
    status = ctx.step('lifecycle.chat.identity_preflight', ctx.client.messages.identity_status)
    if not status.ok:
        ctx.block(register_ids + [recover_id], '身份预检未成功，不能判定是否允许注册或恢复')
        return
    value = status.value
    if value.get('sender_id') != ctx.actor_id:
        ctx.block(register_ids + [recover_id], '身份预检账号不匹配')
        return
    registered = value.get('state') == 'public_keys_registered'
    if registered:
        ctx.block(register_ids, '账号已有公钥，首次注册和 SDK 初次备份本轮未执行', status='not_exercised')
        if not config.get('chat_identity_recover'):
            ctx.block([recover_id], '已有身份恢复探针未启用', status='disabled')
        elif value.get('local_phase'):
            ctx.block([recover_id], '已有本地身份；不删除现有密钥来制造恢复场景', status='not_exercised')
        else:
            version = config.get('chat_key_version') or None

            def recover():
                # A second registry read inside the facade may disagree with
                # preflight. Recovery must never become fresh registration.
                with _observe(register=False, new_conversation=False, allow_send=False):
                    result = ctx.client.messages.ensure_identity(pin=pin, key_version=version)
                _require(result.get('recovered') is True and result.get('state') == 'ready'
                         and result.get('sender_id') == ctx.actor_id,
                         '本轮没有实际恢复并验证指定账号身份')
                return {'recovered': True, 'identity_verified': True}

            ctx.step(recover_id, recover, mutating=True)
        return
    ctx.block([recover_id], '账号尚未注册，恢复路径本轮未执行', status='not_exercised')
    if not config.get('chat_identity'):
        ctx.block(register_ids, '首次身份注册探针未启用', status='disabled')
        return
    if value.get('state') != 'needs_setup' or value.get('local_phase'):
        ctx.block(register_ids, '账号或本地身份处于未确认状态；请先人工核对恢复，不重复注册')
        return

    def setup():
        with _observe(register=True) as calls:
            result = ctx.client.messages.ensure_identity(pin=pin)
        _require(calls['add_public_key'] == 1, '本轮没有执行一次真实公钥注册')
        _require(result.get('state') == 'ready' and result.get('backup_verified') is True
                 and result.get('sender_id') == ctx.actor_id,
                 '身份注册后 SDK 备份恢复或账号回读未确认')
        return {'registration_observed': True, 'backup_verified': True}

    result = ctx.step(register_ids[0], setup, mutating=True)
    if result.ok:
        ctx.step(register_ids[1], lambda: {'sdk_setup_and_unlock_verified': True})
    else:
        ctx.block([register_ids[1]], '注册场景未确认备份恢复；保留身份状态，勿重复执行')


def _resource(row):
    metadata = row.get('metadata') or {}
    return row.get('id') or row.get('resource_id'), metadata


def _send(ctx, *, new=False):
    check_id = 'lifecycle.chat.send_new' if new else 'lifecycle.chat.send_existing'
    associated = [check_id, 'lifecycle.chat.readback']
    if new:
        associated += ['lifecycle.chat.new_conversation']
    if not _sdk_available():
        ctx.block(associated, '缺少 X Chat SDK 加密依赖')
        return
    peer = ctx.samples.get('peer_username')
    peer_id = ctx.samples.get('peer_user_id')
    if not peer or not peer_id or peer_id == ctx.actor_id:
        ctx.block(associated, '需要明确且不同于发送账号的 peer_username 和 peer_user_id')
        return

    def preflight():
        identity = ctx.client.messages.identity_status()
        _require(identity.get('sender_id') == ctx.actor_id
                 and identity.get('local_phase') == 'ready'
                 and identity.get('local_matches_registered') is True,
                 '发送前需要已恢复且匹配服务端公钥的本地身份')
        preview = ctx.client.messages.preview(peer)
        _require(preview.get('sender_id') == ctx.actor_id and preview.get('recipient_id') == peer_id,
                 '用户名未唯一匹配指定测试收件人')
        _require(preview.get('can_dm') is True, '测试收件人当前不允许私信')
        expected_cid = ':'.join(sorted((ctx.actor_id, peer_id), key=int))
        _require(preview.get('conversation_id') == expected_cid, '会话成员未匹配双方测试账号')
        configured_cid = ctx.samples.get('conversation_id')
        _require(not configured_cid or configured_cid == expected_cid, '配置会话与收件人不一致')
        return preview

    preview = ctx.step(check_id + '.preflight', preflight)
    if not preview.ok:
        ctx.block(associated, '聊天身份、目标账号或会话预检未通过')
        return
    cid = preview.value['conversation_id']
    # A reconciled prior-run resource can have a newer journal copy. Keep its
    # latest receipt rather than treating the old unknown copy as a new send.
    records = list(dict(_resource(row) for row in
                        ctx.resources('chat_request', all_runs=True)).items())
    pending = [(rid, meta) for rid, meta in records if meta.get('recipient_id') == peer_id
               and meta.get('phase') != 'not_attempted'
               and (meta.get('phase') != 'verified' or
                    (meta.get('run_id') == ctx.run_id and meta.get('scenario') == ('new' if new else 'existing')))]
    if len(pending) > 1:
        ctx.block(associated, '同一接收者存在多个待核对回执；先人工核对，不创建新 request_id')
        return
    replay = bool(pending)
    if replay:
        request_id, record = pending[0]
    else:
        if new and preview.value.get('exists') is not False:
            ctx.block(associated, '会话已经存在；新会话密钥分发及首次发送本轮未执行', status='not_exercised')
            return
        if not new and (preview.value.get('exists') is not True or preview.value.get('mode') != 'encrypted'):
            ctx.block(associated, '需要已经建立的加密测试会话；不隐式升级或新建会话')
            return
        if new and preview.value.get('can_create_encrypted_conversation') is not True:
            ctx.block(associated, '测试接收者不允许创建加密会话')
            return
        request_id = 'api-watch-' + str(uuid.uuid4())
        record = {'recipient': peer, 'recipient_id': peer_id, 'text': ctx.marker,
                  'mode': 'encrypted', 'phase': 'planned', 'run_id': ctx.run_id,
                  'scenario': 'new' if new else 'existing', 'conversation_id': cid}
        # Persist ID and exact text BEFORE the SDK can contact the server.
        ctx.save_resource('chat_request', request_id, **record)
    calls = {}
    entered = False

    def attempt():
        nonlocal calls, entered
        entered = True
        with _observe(new_conversation=new and not replay, allow_send=not replay,
                      conversation_id=cid) as calls:
            if replay:
                receipt = ctx.client.messages.reconcile(request_id)
            else:
                receipt = ctx.client.messages.send(peer, record['text'], request_id=request_id,
                                                   mode='encrypted', verify=True)
        _require(receipt.get('status') == 'verified', '消息尚未完成回读；只允许 reconcile，不重新发送')
        _require(receipt.get('sender_id') == ctx.actor_id and receipt.get('recipient_id') == peer_id
                 and receipt.get('conversation_id') == cid and receipt.get('encrypted') is True
                 and bool(receipt.get('message_id')), '已核对回执与测试账号或加密模式不匹配')
        _require(replay or calls['send_message'] == 1, '本轮没有执行一次真实发送调用')
        record['phase'] = 'verified'
        record['message_id'] = receipt['message_id']
        ctx.save_resource('chat_request', request_id, **record)
        return receipt

    result = ctx.step(check_id + '.reconcile' if replay else check_id, attempt, mutating=not replay)
    if not result.ok:
        # The context can reject a new write because another run is unresolved.
        # No SDK receipt exists if it never called us, so do not manufacture an
        # unknown delivery that could only be recovered from a nonexistent ID.
        if not replay:
            record['phase'] = 'unknown' if entered else 'not_attempted'
        ctx.save_resource('chat_request', request_id, **record)
    if new:
        if calls.get('add_conversation_keys') == 1:
            ctx.step('lifecycle.chat.new_conversation', lambda: {'key_distribution_observed': True})
        else:
            ctx.block(['lifecycle.chat.new_conversation'],
                      '本轮未观察到成功的新会话密钥分发；恢复旧回执不算新调用', status='not_exercised')
    if replay:
        ctx.block([check_id], '仅核对持久回执，本轮没有再次执行发送接口',
                  status='not_exercised' if result.ok else 'unconfirmed')
    if not result.ok:
        ctx.block(['lifecycle.chat.readback'], '发送回执未确认；保留 request_id，后续仅 reconcile')
        return

    def readback():
        page = ctx.client.messages.list(peer, limit=100, max_pages=100)
        matches = [item for item in page.get('items', []) if item.get('message_id') == record['message_id']]
        _require(page.get('conversation_id') == cid and len(matches) == 1, '读取页未唯一找到本轮消息')
        item = matches[0]
        _require(item.get('sender_id') == ctx.actor_id and item.get('text') == record['text']
                 and item.get('encrypted') is True and item.get('signature_verified') is True,
                 '消息正文、发送者、解密或签名校验未通过')
        return {'message_verified': True, 'decrypted': True, 'signature_verified': True}

    ctx.step('lifecycle.chat.readback', readback)


def run(ctx):
    """Run explicitly enabled manual lifecycle scenarios with exact prerequisites."""
    config = ctx.config.get('lifecycle', {})
    if not config.get('enabled'):
        ctx.block([row['id'] for row in SPECS], '生命周期探针未启用', status='disabled')
        return
    _login(ctx)
    _login(ctx, legacy=True)
    if getattr(ctx, 'stop_event', None) is not None and ctx.stop_event.is_set():
        ctx.block(_ids('lifecycle.chat.'), '收到停止请求；本轮未开始聊天场景')
        return
    if (not ctx.auth or not ctx.actor_id
            or ctx.actor_id != ctx.samples.get('test_account_id')):
        ctx.block(_ids('lifecycle.chat.'), '需要已验证且匹配 test_account_id 的 Cookie')
        return
    if config.get('chat_token'):
        def token():
            value = XChatAPI.generate_chat_token(ctx.auth)
            _require(isinstance(value, str) and bool(value.strip()), 'Chat token 响应无有效 token')
            return {'token_received': True}
        ctx.step('lifecycle.chat.token', token, mutating=True)
    else:
        ctx.block(['lifecycle.chat.token'], '短期 Chat token 探针未启用', status='disabled')
    _identity(ctx)
    send = config.get('chat_send')
    new = config.get('chat_new_conversation')
    if new:
        _send(ctx, new=True)
    else:
        ctx.block(['lifecycle.chat.new_conversation', 'lifecycle.chat.send_new'],
                  '新会话场景未启用', status='disabled')
    if send:
        _send(ctx)
    else:
        ctx.block(['lifecycle.chat.send_existing'], '已建会话发送场景未启用', status='disabled')
    if not send and not new:
        ctx.block(['lifecycle.chat.readback'], '没有启用聊天发送场景', status='disabled')
