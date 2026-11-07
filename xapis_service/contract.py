"""The provider's deliberately small v1 capability and request contract."""
import re
from urllib.parse import urlsplit

from x_apis.client_errors import XClientError


CAPABILITIES = {
    'profile.get': ('username',),
    'content.get': ('content_id',),
    'content.search': ('query', 'product', 'count', 'cursor'),
    'content.list_by_actor': ('username', 'count', 'cursor'),
    'reply.list': ('content_id', 'cursor'),
}


def invalid():
    raise XClientError('invalid_argument', '请求字段或参数格式无效', status_code=400)


def validate_request(body):
    if not isinstance(body, dict) or set(body) != {'capability', 'payload', 'account'}:
        invalid()
    capability = body['capability']
    if not isinstance(capability, str) or capability not in CAPABILITIES:
        raise XClientError('unsupported_capability', '该能力未在当前服务版本开放', status_code=400)
    payload, account = body['payload'], body['account']
    if (not isinstance(payload, dict) or set(payload) - set(CAPABILITIES[capability])
            or not isinstance(account, dict) or set(account) - {'cookies', 'proxy'}):
        invalid()
    cookies = account.get('cookies')
    if (not isinstance(cookies, str) or not 1 <= len(cookies) <= 32768
            or any(ord(c) < 32 or ord(c) == 127 for c in cookies)):
        invalid()
    # Require the caller's complete session; never create a guest identity or
    # guess ct0 when account material is missing.
    fields = dict(part.strip().split('=', 1) for part in cookies.split(';') if '=' in part)
    if not fields.get('auth_token') or not fields.get('ct0'):
        invalid()
    proxy = account.get('proxy')
    if proxy is not None:
        if not isinstance(proxy, str) or len(proxy) > 4096 or any(c.isspace() for c in proxy):
            invalid()
        try:
            address = urlsplit(proxy)
            if (address.scheme not in ('http', 'https', 'socks5', 'socks5h') or not address.hostname
                    or address.port is None or address.path not in ('', '/') or address.query or address.fragment):
                invalid()
        except ValueError:
            invalid()
    required = CAPABILITIES[capability][0]
    value = payload.get(required)
    if not isinstance(value, str):
        invalid()
    if required == 'username' and not re.fullmatch(r'@?[A-Za-z0-9_]{1,15}', value):
        invalid()
    if required == 'content_id' and not re.fullmatch(r'[1-9][0-9]{0,29}', value):
        invalid()
    if required == 'query' and (not value.strip() or len(value) > 2048 or '\x00' in value):
        invalid()
    if 'count' in payload and (type(payload['count']) is not int or not 1 <= payload['count'] <= 100):
        invalid()
    if 'product' in payload and payload['product'] not in ('Top', 'Latest', 'Media'):
        invalid()
    if 'cursor' in payload and payload['cursor'] is not None:
        cursor = payload['cursor']
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 8192 or any(ord(c) < 32 for c in cursor):
            invalid()
    return capability, dict(payload), {'cookies': cookies, 'proxy': proxy}


def execute(client, capability, payload):
    if capability == 'profile.get':
        return client.users.get(payload['username'])
    if capability == 'content.get':
        return client.posts.get(payload['content_id'])
    if capability == 'content.search':
        return client.posts.search(**payload)
    if capability == 'content.list_by_actor':
        return client.users.posts(**payload)
    return client.comments.list(payload['content_id'], cursor=payload.get('cursor'))
