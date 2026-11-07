"""新单聊密钥分发。恢复模式只回读原事件，绝不重发密钥变更。"""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from utils.xchat_identity import (IdentityError, _locked, _read, _same_public, _save,
                                  _uid, _viewer, export_conversation_material,
                                  public_records, sdk_call)
from utils.xchat_codec import message_event_summary
from utils.x_util import parse_screen_name
from x_apis.x_api import XAPI
from x_apis.x_dm_api import XChatAPI


def inspect_recipient(auth, username):
    name = parse_screen_name(username)
    if not re.fullmatch(r'[A-Za-z0-9_]{1,15}', name):
        raise IdentityError('收件人用户名无效')
    payload = XAPI.get_user_info(auth, name)
    try:
        if payload.get('errors'):
            raise ValueError()
        user = payload['data']['user']['result']
        uid = _uid(user['rest_id'])
        if user['core']['screen_name'].lower() != name:
            raise ValueError()
        members = XChatAPI.get_users_by_ids(auth, [uid])
        rows = members['data']['get_member_results']['results']
        if members.get('errors') or len(rows) != 1:
            raise ValueError()
        row = rows[0]
        result = row['member_results']
        member = result['result']
        if (row['id'] != uid or row['status'] != 'Found' or result['rest_id'] != uid
                or member['rest_id'] != uid or member['__typename'] != 'User'
                or member['core']['screen_name'].lower() != name):
            raise ValueError()
        permissions = member['chat_permissions']
        if not isinstance(permissions, dict):
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise IdentityError('无法从 X Chat 唯一确认收件人及权限') from None
    allowed = permissions.get('can_dm') is True
    can_create_key = allowed and permissions.get('can_dm_on_xchat') is True
    # Only expose known reason codes, never arbitrary server text.
    reason = permissions.get('can_dm_reason')
    if reason not in ('InboxClosed', 'Blocked', 'NotFollowing', 'PremiumRequired'):
        reason = 'NotAllowed' if not allowed else None
    return {'recipient': name, 'recipient_id': uid, 'can_dm': allowed, 'reason': reason,
            'can_create_encrypted_conversation': can_create_key}


def read_conversation_events(auth, cid, *, max_pages=100):
    """收齐历史和补充密钥事件；游标不推进或达到上限时明确失败。"""
    if type(max_pages) is not int or max_pages < 1:
        raise ValueError('max_pages must be positive')
    cursor, result, seen = 2**63 - 1, [], set()
    for _ in range(max_pages):
        payload = (XChatAPI.get_conversation_page(auth, cid) if cursor == 2**63 - 1 else
                   XChatAPI.get_conversation_page(auth, cid, min_local_sequence_id=str(cursor)))
        try:
            page = payload['data']['get_conversation_page']
            events = page['encoded_message_events']
            missing = page.get('missing_conversation_key_change_events', [])
            if (payload.get('errors') or not isinstance(events,list) or not isinstance(missing,list)
                    or type(page.get('has_more')) is not bool):
                raise ValueError()
            for encoded in events + missing:
                summary = message_event_summary(encoded)
                if summary['conversation_id'] != cid:
                    raise ValueError()
                if encoded not in seen:
                    result.append(encoded)
                    seen.add(encoded)
            if page['has_more'] is False:
                return result
            if not events:
                raise ValueError()
            next_cursor = min(int(message_event_summary(e)['sequence_id']) for e in events)
            if not 0 < next_cursor < cursor:
                raise ValueError()
            cursor = next_cursor
        except (KeyError, TypeError, ValueError):
            raise IdentityError('会话分页响应无效或游标未推进') from None
    raise IdentityError('会话历史超过 max_pages，不能根据不完整历史发送')


def _events(auth, cid):
    return [message_event_summary(e) for e in read_conversation_events(auth,cid)]


def _read_receipt(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd) as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 16384:
            raise IdentityError('会话创建回执必须为 0600 小文件')
        data = json.load(f)
    if data.get('format') != 'xapis-new-conversation-v1':
        raise IdentityError('会话创建回执格式无效')
    return data


def start_conversation(auth, username, identity_path, output, receipt=None, *, apply=False, resume=False,
                       upgrade_plaintext=False):
    """预览只读；apply 新建；resume 仅核对先前提交，不重新创建。"""
    if resume and not apply:
        raise IdentityError('resume 需要 apply（仅回读恢复，不重发）')
    if apply and not receipt:
        raise IdentityError('创建会话需要新的 receipt 文件')
    paths = [Path(p).absolute() for p in (identity_path, output, receipt) if p is not None]
    if len(set(paths)) != len(paths):
        raise IdentityError('身份、会话密钥和回执必须使用不同路径')
    with _locked(identity_path):
        uid, _ = _viewer(auth)
        state = _read(identity_path)
        if state['phase'] != 'ready' or state['sender_id'] != uid:
            raise IdentityError('本地身份未就绪或与当前账号不一致')
        recipient = inspect_recipient(auth, username)
        if recipient['recipient_id'] == uid:
            raise IdentityError('不能向自己创建单聊')
        cid = ':'.join(sorted([uid, recipient['recipient_id']], key=int))
        preview = {'sender_id': uid, 'conversation_id': cid, **recipient}
        if not apply:
            return {'state': ('eligible' if recipient['can_create_encrypted_conversation']
                              else 'message_request_only' if recipient['can_dm'] else 'blocked'), **preview}
        with _locked(receipt) as receipt_path:
            if resume:
                record = _read_receipt(receipt_path)
                for key in ('sender_id', 'conversation_id', 'recipient_id', 'recipient'):
                    if record.get(key) != preview[key]:
                        raise IdentityError('原创建回执与当前发送者或收件人不匹配')
                if record.get('output') != str(Path(output).absolute()):
                    raise IdentityError('恢复必须使用原会话密钥输出路径')
                if record.get('stage') not in ('submitting', 'submitted', 'ready'):
                    raise IdentityError('此回执没有已提交事件，不会自动提交')
            else:
                if receipt_path.exists() or Path(output).exists():
                    raise IdentityError('回执或会话密钥已存在；禁止重复创建，先回读核对')
                if not recipient['can_dm']:
                    raise IdentityError('收件人不允许私信：' + recipient['reason'])
                if not recipient['can_create_encrypted_conversation']:
                    raise IdentityError('允许消息请求但尚不允许创建加密会话；请使用 chat-request')
                existing = read_conversation_events(auth,cid)
                if existing:
                    from utils.xchat_codec import decode_base64_struct, field as get
                    encrypted = any(message_event_summary(e)['event_type']==3 or
                        (message_event_summary(e)['event_type']==1 and
                         101 in get(get(decode_base64_struct(e),7,12),1,12)) for e in existing)
                    if not upgrade_plaintext or encrypted:
                        raise IdentityError('会话已有事件；请用 chat-session 解包，不能作为新会话重建')
                validated = sdk_call('validate', key_blob_b64=state['key_blob_b64'], version=state['version'])
                if not _same_public(validated, state['registration']):
                    raise IdentityError('本地身份私钥与公钥不匹配')
                keys = []
                for member in (uid, recipient['recipient_id']):
                    records = public_records(auth, member, allow_managed=member != uid)
                    if not records:
                        raise IdentityError('成员未注册 X Chat 公钥，无法创建加密会话')
                    latest = records[0]['public_key_with_metadata']
                    public = latest['public_key']
                    if member == uid and (latest['version'] != state['version']
                                          or not _same_public(public, state['registration'])):
                        raise IdentityError('本地身份不是当前最新注册公钥')
                    keys.append({'user_id': member, 'key_version': latest['version'], **public})
                prepared = sdk_call('prepare_conversation', key_blob_b64=state['key_blob_b64'],
                                    version=state['version'], sender_id=uid, public_keys=keys)
                if prepared['conversation_id'] != cid:
                    raise IdentityError('SDK 返回的会话 ID 不匹配')
                record = {'format': 'xapis-new-conversation-v1', **preview, 'stage': 'submitting',
                          'output': str(Path(output).absolute()),
                          'key_version': prepared['conversation_key_version'],
                          'key_sha256': hashlib.sha256(base64.b64decode(prepared['conversation_key_b64'], validate=True)).hexdigest(),
                          'key_message_id': prepared['action_signatures'][0]['message_id']}
                # Reserve before any mutation. Even an ambiguous timeout leaves
                # a recoverable marker; resume never calls add_conversation_keys.
                fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w') as f:
                    json.dump(record, f)
                    f.flush()
                    os.fsync(f.fileno())
                result = XChatAPI.add_conversation_keys(auth, prepared)
                record.update(stage='submitted', key_sequence_id=result['conversation_key_change_sequence_id'])
                _save(receipt_path, record)
            summaries = _events(auth, cid)
            if not any(s['event_type'] == 3 and s['message_id'] == record['key_message_id']
                       and s['sender_id'] == uid for s in summaries):
                raise IdentityError('回读未确认原密钥事件；保留回执，可稍后 resume，禁止重发')
            if Path(output).exists():
                from utils.xchat_crypto import load_key_material
                material = load_key_material(output)
                if (material.sender_id != uid or material.conversation_id != cid
                        or str(material.conversation_key_version) != record['key_version']
                        or hashlib.sha256(material.conversation_key).hexdigest() != record['key_sha256']):
                    raise IdentityError('已存在的输出与原会话密钥不匹配')
            else:
                export_conversation_material(auth, identity_path, cid, output, signature_version=7,
                                             expected_key_version=record['key_version'],
                                             expected_key_sha256=record['key_sha256'])
            record['stage'] = 'ready'
            _save(receipt_path, record)
            return {'state': 'conversation_ready', **preview, 'output': str(output),
                    'key_message_id': record['key_message_id'], 'key_version': record['key_version']}


def send_first_request(auth, username, text, receipt=None, *, apply=False, resume=False):
    """明确选择未加密的首次文字请求；不会把已有加密会话降级。"""
    import uuid
    from utils.xchat_crypto import prepare_message_request, message_request_text
    from utils.xchat_codec import decode_base64_struct, field as get, encode_struct
    if not isinstance(text, str) or not text.strip():
        raise IdentityError('首次消息正文不能为空')
    if resume and not apply:
        raise IdentityError('resume 需要 apply，但只回读，不重发')
    if apply and not receipt:
        raise IdentityError('首次发送需要新的 receipt 文件')
    uid, _ = _viewer(auth)
    recipient = inspect_recipient(auth, username)
    if recipient['recipient_id'] == uid:
        raise IdentityError('不能向自己发起新会话')
    cid = ':'.join(sorted([uid, recipient['recipient_id']], key=int))
    preview = {'sender_id': uid, 'conversation_id': cid, **recipient, 'encrypted': False}
    if not apply:
        return {'stage':'eligible' if recipient['can_dm'] else 'blocked', **preview}
    digest = hashlib.sha256(text.encode()).hexdigest()
    with _locked(receipt) as path:
        if resume:
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd) as f:
                info = os.fstat(f.fileno())
                if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 16384:
                    raise IdentityError('发送回执必须为 0600 小文件')
                record = json.load(f)
            if (record.get('format') != 'xapis-first-request-v1' or record.get('text_sha256') != digest
                    or any(record.get(k) != preview[k] for k in ('sender_id','recipient_id','conversation_id','recipient'))
                    or record.get('stage') not in ('sending','sent','verified')):
                raise IdentityError('原回执与本次身份、会话或正文不匹配')
        else:
            if path.exists():
                raise IdentityError('回执已存在，禁止重复发送；使用 resume 核对')
            if not recipient['can_dm']:
                raise IdentityError('收件人不允许消息请求：' + recipient['reason'])
            if _events(auth, cid):
                raise IdentityError('会话已有事件，不能作为首次未加密请求发送')
            prepared = prepare_message_request(text, cid)
            record = {'format':'xapis-first-request-v1', **preview, 'stage':'sending',
                      'message_id':prepared.message_id, 'text_sha256':digest}
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd,'w') as f:
                json.dump(record,f); f.flush(); os.fsync(f.fileno())
            response = XChatAPI.send_message(auth, **prepared.as_send_kwargs(), allow_unencrypted=True)
            record['receipt'] = message_event_summary(response['data']['xchat_send_create_message_event']['encoded_message_event'])
            if record['receipt']['sender_id'] != uid:
                raise IdentityError('发送回执的发送者不匹配；保留回执，勿重发')
            record['stage'] = 'sent'
            _save(path,record)
        uuid.UUID(record['message_id'])
        response = XChatAPI.get_conversation_page(auth,cid)
        if response.get('errors'):
            raise IdentityError('回读失败；可 resume 核对，禁止重新发送')
        events = response['data']['get_conversation_page']['encoded_message_events']
        for encoded in events:
            summary = message_event_summary(encoded)
            if summary['message_id'] != record['message_id']:
                continue
            if summary['event_type'] != 1 or summary['sender_id'] != uid or summary['conversation_id'] != cid:
                raise IdentityError('回读消息的事件、发送者或会话不匹配')
            create = get(get(decode_base64_struct(encoded),7,12),1,12)
            if any(k in create for k in (101, 108, 112)):
                raise IdentityError('回读事件不是本次未加密消息请求')
            # Server-added nested side effects are not part of the text holder.
            actual = message_request_text(base64.b64encode(encode_struct([
                (100,11,get(create,100,11))])).decode())
            if actual != text:
                raise IdentityError('回读正文不匹配，禁止重发')
            record.update(stage='verified', readback=summary, text_matches=True)
            _save(path,record)
            return record
        raise IdentityError('回读尚未找到原消息；保留回执，可 resume，禁止重发')
