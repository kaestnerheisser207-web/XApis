# -*- coding: utf-8 -*-
"""X Chat / 私信读取和已加密消息发送。

当前网页端的私信已经切到 ``X Chat``，不是旧的 ``/1.1/dm/*`` 接口：

* ``GenerateXChatTokenMutation`` 在 ``api.x.com`` 下发短期 chat token；
* ``xChatDmSettingsQuery`` 读取 DM 权限；
* ``GetUsersByIdsForXChat`` 读取聊天成员资料。
* ``SendMessageCreateMutation`` 发送已编码、加密和签名的消息事件。

读取请求成功不能证明 passcode 或本地密钥已准备好。发送层接收调用方生成的
X Chat 事件和签名，不负责从 passcode 恢复密钥，也不把 token 持久化到磁盘。
"""

import base64
import binascii

from builder import client
from builder.header import Header, HeaderBuilder, HeaderType
from utils.xchat_codec import message_event_summary


XCHAT_ORIGIN = 'https://api.x.com'
XCHAT_GRAPHQL = f'{XCHAT_ORIGIN}/graphql'
XWEB_ORIGIN = 'https://x.com'
XWEB_GRAPHQL = f'{XWEB_ORIGIN}/i/api/graphql'

GENERATE_TOKEN_QID = 'Qh3fZRjPPtPoHYR_2sCZsA'
DM_SETTINGS_QID = 'mRmm_3aCzCcbpzBkhyhCDg'
# Authenticated home embeds xchat bf9d4ef96b0e920edb7a41bc02fcd5ab2413c691
# (dm-core-Bq9lt84w.js), observed 2026-11-07.
MEMBERS_QID = '6U-79eoYEaV8RDKC39k_RA'
PUBLIC_KEYS_QID = 'C-YphxeIcPLOGtFNGhkuOw'
INITIAL_PAGE_QID = 'VAUff2_Z24tK6gMAUmt1RA'
CONVERSATION_PAGE_QID = 'GX9ZijkxG8AqRMQVD7hMnQ'
SEND_MESSAGE_CREATE_QID = 'TWRPP7gnKwV_R8-tE-Dd3Q'
ADD_PUBLIC_KEY_QID = '3K4FppkWWmDPQgdqCn0NJw'
ADD_CONVERSATION_KEYS_QID = 'hEhvYw2gxbE4YlBjr6JlXA'

DEFAULT_QUERY_SETTINGS = {
    'conversation_event_limit': 200,
    'inbox_conversation_event_limit': 5,
    'inbox_conversation_limit': 20,
    'user_event_limit': 500,
}


def _external_headers(auth, path: str, *, accept='application/json',
                      content_type=None, apollo=False):
    """api.x.com X Chat 请求的显式头，按 Chrome 实抓排列。"""
    order = [
        'authorization', 'x-csrf-token', 'referer',
        'apollo-require-preflight', 'x-client-transaction-id',
        'x-twitter-auth-type', 'accept', 'content-type', 'origin',
    ]
    h = Header(order=order)
    h.with_bearer(auth).with_csrf(auth).set_referer('https://x.com/')
    h.with_xctid(auth, 'POST' if content_type else 'GET', path)
    h.with_auth_type(auth)
    if apollo:
        h.set_header('apollo-require-preflight', 'true')
    if accept:
        h.set_header('accept', accept)
    if content_type:
        h.set_header('content-type', content_type)
    h.set_origin('https://x.com')
    return h.get()


class XChatAPI:

    @staticmethod
    def add_conversation_keys(auth, prepared: dict) -> dict:
        """提交 SDK 准备的单聊密钥分发；只发一次，超时后须回读核对。"""
        import re
        import uuid
        cid = prepared.get('conversation_id')
        if not isinstance(cid, str) or not re.fullmatch(r'[1-9][0-9]*:[1-9][0-9]*', cid):
            raise ValueError('仅支持规范单聊 ID')
        ids = cid.split(':')
        if len(set(ids)) != 2 or ids != sorted(ids, key=int):
            raise ValueError('单聊 ID 必须按数字排序且包含两个不同账号')
        version = prepared.get('conversation_key_version')
        if not isinstance(version, str) or not re.fullmatch(r'[1-9][0-9]*', version):
            raise ValueError('会话密钥版本无效')
        participants, signatures = [], []
        for key in prepared.get('participant_keys', []):
            if not isinstance(key, dict) or key.get('user_id') not in ids:
                raise ValueError('密钥分发成员不匹配')
            if not re.fullmatch(r'[1-9][0-9]*', str(key.get('public_key_version', ''))):
                raise ValueError('成员公钥版本无效')
            if not base64.b64decode(key['encrypted_key'], validate=True):
                raise ValueError('成员加密密钥为空')
            participants.append({'user_id': key['user_id'],
                                 'public_key_version': key['public_key_version'],
                                 'encrypted_conversation_key': key['encrypted_key']})
        if sorted(k['user_id'] for k in participants) != sorted(ids):
            raise ValueError('密钥必须恰好分发给双方')
        for signature in prepared.get('action_signatures', []):
            uuid.UUID(signature['message_id'])
            for name in ('encoded_message_event_detail', 'signature'):
                value = signature[name]
                if not isinstance(value, str) or not base64.b64decode(value + '=' * (-len(value) % 4), validate=True):
                    raise ValueError('密钥事件签名材料无效')
            if signature.get('signature_version') != '7':
                raise ValueError('仅支持 SDK 当前密钥事件签名版本 7')
            if not re.fullmatch(r'[1-9][0-9]*', str(signature.get('public_key_version', ''))):
                raise ValueError('签名公钥版本无效')
            signatures.append({'message_id': signature['message_id'],
                               'encoded_message_event_detail': signature['encoded_message_event_detail'],
                               'message_event_signature': {name: signature[name] for name in (
                                   'signature', 'signature_version', 'public_key_version')}})
        if len(signatures) != 1:
            raise ValueError('新单聊需要一个密钥变更签名')
        variables = {'conversation_id': cid, 'conversation_key_version': version,
                     'conversation_participant_keys': participants, 'action_signatures': signatures,
                     'base64_encoded_key_rotation': None, 'ttl_msec': None, 'rotated_pmk_bundle': None}
        path = f'/graphql/{ADD_CONVERSATION_KEYS_QID}/AddEncryptedConversationKeysMutation'
        response = client.post(XCHAT_ORIGIN + path,
                               headers=_external_headers(auth, path, content_type='application/json'),
                               cookies=auth.cookie, json={'variables': _compact(variables)},
                               proxies=auth.proxies, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get('errors'):
            raise RuntimeError('新会话密钥提交失败；先回读，勿直接重试')
        data = payload.get('data')
        result = data.get('xchat_add_encrypted_conversation_key') if isinstance(data, dict) else None
        if (not isinstance(result, dict) or result.get('error_code') is not None
                or not isinstance(result.get('conversation_key_change_sequence_id'), str)
                or not result['conversation_key_change_sequence_id'].isdigit()):
            raise RuntimeError('新会话密钥回执失败或不完整；先回读，勿直接重试')
        return result

    @staticmethod
    def generate_chat_token(auth) -> str:
        """请求当前会话的 X Chat 短期 token（不落盘）。"""
        path = f'/graphql/{GENERATE_TOKEN_QID}/GenerateXChatTokenMutation'
        headers = _external_headers(auth, path, content_type='application/json')
        resp = client.post(
            f'{XCHAT_ORIGIN}{path}', headers=headers, cookies=auth.cookie,
            json={'variables': '{}'}, proxies=auth.proxies, timeout=30)
        resp.raise_for_status()
        data = resp.json().get('data') or {}
        result = data.get('user_get_x_chat_auth_token') or {}
        token = result.get('token')
        if not token:
            raise RuntimeError(f'X Chat token 响应缺 token：{resp.text[:300]}')
        return token

    @staticmethod
    def get_dm_settings(auth) -> dict:
        """读取当前账号的 DM / X Chat 权限设置。"""
        path = f'/i/api/graphql/{DM_SETTINGS_QID}/xChatDmSettingsQuery'
        headers = (HeaderBuilder.build(HeaderType.POST)
                   .with_bearer(auth).with_csrf(auth).with_auth_type(auth)
                   .with_client_language(auth)
                   .with_xctid(auth, 'GET', path)
                   .set_referer('https://x.com/i/chat').get())
        resp = client.get(
            f'{XWEB_ORIGIN}{path}', headers=headers, cookies=auth.cookie,
            params={'variables': '{}'}, proxies=auth.proxies, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get('errors'):
            raise RuntimeError(f'X Chat settings 错误：{payload["errors"]}')
        return payload

    @staticmethod
    def get_users_by_ids(auth, user_ids) -> dict:
        """读取 X Chat 成员信息（Apollo GraphQL）。"""
        path = f'/graphql/{MEMBERS_QID}/GetUsersByIdsForXChat'
        variables = {'ids': [str(uid) for uid in user_ids],
                     'with_social_proof': False}
        headers = _external_headers(auth, path, apollo=True)
        resp = client.get(
            f'{XCHAT_ORIGIN}{path}', headers=headers, cookies=auth.cookie,
            params={'variables': _compact(variables)},
            proxies=auth.proxies, timeout=30)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def get_public_keys(auth, user_ids, *, include_juicebox_tokens=True) -> dict:
        """读取 X Chat 公钥查询（进入会话页时浏览器会发）。"""
        path = f'/graphql/{PUBLIC_KEYS_QID}/GetPublicKeysQuery'
        variables = {
            'ids': [str(uid) for uid in user_ids],
            'include_juicebox_tokens': include_juicebox_tokens,
        }
        headers = _external_headers(auth, path, apollo=True)
        resp = client.get(
            f'{XCHAT_ORIGIN}{path}', headers=headers, cookies=auth.cookie,
            params={'variables': _compact(variables)},
            proxies=auth.proxies, timeout=30)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def add_public_key(auth, public_key: dict, version: str) -> dict:
        """首次身份注册。调用方须先保存私钥并确认账号未初始化；不自动重试。"""
        if not isinstance(version, str) or not version.isascii() or not version.isdigit():
            raise ValueError('version 必须为十进制版本号')
        required = ('public_key', 'signing_public_key', 'identity_public_key_signature')
        if not isinstance(public_key, dict) or public_key.get('registration_method') != 'CustomPin':
            raise ValueError('此入口仅支持 CustomPin 首次注册')
        for name in required:
            try:
                value = public_key[name]
                if not isinstance(value, str) or not base64.b64decode(value, validate=True):
                    raise ValueError()
            except (KeyError, ValueError, binascii.Error):
                raise ValueError('公钥注册材料无效') from None
        path = f'/graphql/{ADD_PUBLIC_KEY_QID}/AddXChatPublicKeyMutation'
        headers = _external_headers(auth, path, content_type='application/json')
        response = client.post(
            XCHAT_ORIGIN + path, headers=headers, cookies=auth.cookie,
            json={'variables': _compact({'version': version, 'generate_version': True,
                                        'public_key': {k: public_key[k] for k in (*required, 'registration_method')}})},
            proxies=auth.proxies, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get('errors'):
            raise RuntimeError('公钥注册 GraphQL 失败；先核对服务端，勿直接重试')
        result = (payload.get('data') or {}).get('user_add_public_key')
        if (not isinstance(result, dict) or result.get('error_code') is not None
                or not isinstance(result.get('version'), str) or not result['version'].isdigit()
                or not isinstance(result.get('token_map'), dict)):
            raise RuntimeError('公钥注册回执不完整或失败；先核对服务端，勿直接重试')
        return result

    @staticmethod
    def get_initial_chat_page(auth, *, max_local_sequence_id=None,
                              message_pull_version=None,
                              query_settings=None) -> dict:
        """读取 X Chat 收件箱初始页（真实 GET GraphQL 请求）。"""
        path = f'/graphql/{INITIAL_PAGE_QID}/GetInitialXChatPageQuery'
        variables = {
            'max_local_sequence_id': max_local_sequence_id,
            'query_settings': dict(query_settings or DEFAULT_QUERY_SETTINGS),
            'message_pull_version': message_pull_version,
        }
        headers = _external_headers(auth, path, apollo=True)
        resp = client.get(
            f'{XCHAT_ORIGIN}{path}', headers=headers, cookies=auth.cookie,
            params={'variables': _compact(variables)},
            proxies=auth.proxies, timeout=30)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def get_conversation_page(auth, conversation_id: str, *,
                              min_local_sequence_id=9223372036854775807,
                              min_conversation_key_version=9223372036854775807,
                              query_settings=None) -> dict:
        """读取单个 X Chat 会话页；不发送消息。"""
        path = f'/graphql/{CONVERSATION_PAGE_QID}/GetConversationPageQuery'
        variables = {
            'conversation_id': str(conversation_id),
            'min_local_sequence_id': str(min_local_sequence_id),
            'min_conversation_key_version': str(min_conversation_key_version),
            'query_settings': dict(query_settings or DEFAULT_QUERY_SETTINGS),
        }
        headers = _external_headers(auth, path, apollo=True)
        resp = client.get(
            f'{XCHAT_ORIGIN}{path}', headers=headers, cookies=auth.cookie,
            params={'variables': _compact(variables)},
            proxies=auth.proxies, timeout=30)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def bootstrap(auth, user_ids=None, conversation_id=None) -> dict:
        """完成网页进入私信页时的可复现读取请求（不发送消息）。"""
        ids = list(user_ids or [auth.user_id])
        result = {
            'chat_token': XChatAPI.generate_chat_token(auth),
            'settings': XChatAPI.get_dm_settings(auth),
            'initial_page': XChatAPI.get_initial_chat_page(auth),
            'members': XChatAPI.get_users_by_ids(auth, ids),
            'public_keys': XChatAPI.get_public_keys(auth, ids),
            # 这些读取响应不能判断本地密钥或 passcode 状态。
            'passcode_required': None,
        }
        if conversation_id:
            result['conversation'] = XChatAPI.get_conversation_page(
                auth, conversation_id)
        return result

    @staticmethod
    def send_message(auth, conversation_id: str, message_id: str, *,
                     encoded_message_create_event: str,
                     encoded_message_event_signature: str | None,
                     conversation_token=None, allow_unencrypted=False) -> dict:
        """通过新 X Chat GraphQL 发送消息事件，默认要求已签名。

        两个 encoded 参数分别是序列化后的 MessageCreateEvent 和
        MessageEventSignature 的标准 Base64；不能直接传纯文本。
        仅显式 allow_unencrypted=True 的未加密文字请求可省略签名，
        此时仍验证事件结构，禁止已有加密事件降级为无签名发送。
        密钥恢复、消息加密及签名由调用方完成。message_id 应由调用方保留，
        网络结果不明时本方法不会自动重试。返回事件须为匹配本次消息和会话的
        MessageCreateEvent；失败事件或畸形回执不能视为成功。
        """
        for name, value in (
                ('conversation_id', conversation_id),
                ('message_id', message_id)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f'{name} 必须为非空字符串')
        if conversation_token is not None and (
                not isinstance(conversation_token, str)
                or not conversation_token.strip()):
            raise ValueError('conversation_token 必须为非空字符串或 None')
        for name, value in (
                ('encoded_message_create_event', encoded_message_create_event),
                ('encoded_message_event_signature', encoded_message_event_signature)):
            if name == 'encoded_message_event_signature' and value is None and allow_unencrypted is True:
                from utils.xchat_crypto import message_request_text
                message_request_text(encoded_message_create_event)
                continue
            if not isinstance(value, str) or not value:
                raise ValueError(f'{name} 必须为非空标准 Base64 字符串')
            try:
                decoded = base64.b64decode(value, validate=True)
            except (binascii.Error, ValueError):
                raise ValueError(f'{name} 必须为非空标准 Base64 字符串') from None
            if not decoded:
                raise ValueError(f'{name} 必须为非空标准 Base64 字符串')

        path = f'/graphql/{SEND_MESSAGE_CREATE_QID}/SendMessageCreateMutation'
        variables = {
            'conversation_id': conversation_id,
            'message_id': message_id,
            'conversation_token': conversation_token,
            'encoded_message_create_event': encoded_message_create_event,
            'encoded_message_event_signature': encoded_message_event_signature,
        }
        headers = _external_headers(auth, path, content_type='application/json')
        resp = client.post(
            f'{XCHAT_ORIGIN}{path}', headers=headers, cookies=auth.cookie,
            json={'variables': _compact(variables)},
            proxies=auth.proxies, timeout=30)
        resp.raise_for_status()
        try:
            payload = resp.json()
        except ValueError:
            raise RuntimeError('X Chat 发送响应不是有效 JSON') from None
        if not isinstance(payload, dict):
            raise RuntimeError('X Chat 发送响应不是 GraphQL 对象')
        if payload.get('errors'):
            raise RuntimeError('X Chat SendMessageCreateMutation 返回 GraphQL errors')
        data = payload.get('data')
        result = data.get('xchat_send_create_message_event') if isinstance(data, dict) else None
        event = result.get('encoded_message_event') if isinstance(result, dict) else None
        if not isinstance(event, str) or not event.strip():
            raise RuntimeError('X Chat 发送响应缺 encoded_message_event，无法确认发送成功')
        try:
            receipt = message_event_summary(event)
        except (ValueError, UnicodeError):
            raise RuntimeError('X Chat 回执无法解码，发送结果未确认；请先回读，勿直接重发') from None
        if receipt['event_type'] == 5:
            raise RuntimeError(f'X Chat 返回 MessageFailureEvent（类型 {receipt.get("failure_type")}）')
        if (receipt['event_type'] != 1 or receipt['message_id'] != message_id
                or receipt['conversation_id'] != conversation_id):
            raise RuntimeError('X Chat 回执事件、消息或会话不匹配；请先回读，勿直接重发')
        return payload


def _compact(payload):
    import json
    return json.dumps(payload, separators=(',', ':'), ensure_ascii=False)
