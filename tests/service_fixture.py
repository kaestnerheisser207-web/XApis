"""Offline Gunicorn fixture shared by Python HTTP and social connector tests.

Run from the repository root with:
    .venv/bin/python -m gunicorn tests.service_fixture:application --bind 127.0.0.1:18081

All accounts and posts are synthetic. This factory performs no network calls,
reads no .env file, and creates a new account-scoped SDK substitute per request.
"""
from types import SimpleNamespace

from x_apis.client_errors import XClientError
from xapis_service.app import create_app
from xapis_service.config import Settings


KEY = 'synthetic-service-key-only-for-tests-1234'
COOKIES = 'auth_token=account-one; ct0=synthetic'
SECOND_COOKIES = 'auth_token=account-two; ct0=synthetic'
NEXT_CURSOR = 'opaque:+/%=下一页'


def profile(user_id='123', username='reader'):
    return {'user_id': user_id, 'username': username.lstrip('@'), 'name': 'Synthetic Reader',
            'description': 'Offline HTTP fixture', 'followers': 7}


def post(post_id='123', *, user_id='123', title='Synthetic post'):
    return {'work_id': post_id, 'work_url': 'https://x.com/reader/status/' + post_id,
            'work_type': 'text', 'title': title, 'create_time': 'Thu Oct 01 12:00:00 +0000 2026',
            'lang': 'en', 'user_id': user_id, 'nickname': 'Synthetic Reader',
            'screen_name': 'reader', 'user_url': 'https://x.com/reader',
            'like_count': 1, 'retweet_count': 2, 'reply_count': 3, 'quote_count': 4,
            'bookmark_count': 5, 'view_count': '11', 'image_urls': [], 'video_urls': []}


def synthetic_sdk(account, transport):
    fields = dict(part.strip().split('=', 1) for part in account['cookies'].split(';') if '=' in part)
    user_id = '456' if fields.get('auth_token') == 'account-two' else '123'

    def get_user(username):
        if username == 'timeout':
            raise TimeoutError('synthetic-private-transport-detail')
        if username == 'auth_error':
            raise XClientError('auth_required', '登录会话无效')
        if username == 'rate_limit':
            raise XClientError('rate_limited', '请求受到限流', retry_after=3, upstream_codes=(88,))
        if username == 'unavailable':
            raise XClientError('user_unavailable', '用户不可访问')
        return profile(user_id, username)

    def page(*, cursor=None, title='Synthetic page'):
        if cursor is not None and cursor != NEXT_CURSOR:
            raise XClientError('invalid_argument', '不匹配的 synthetic cursor')
        return {'items': [post('124' if cursor else '123', user_id=user_id, title=title)],
                'next_cursor': None if cursor else NEXT_CURSOR}

    return SimpleNamespace(
        users=SimpleNamespace(get=get_user,
            posts=lambda username, count=20, cursor=None: page(cursor=cursor, title='Actor ' + username)),
        posts=SimpleNamespace(
            get=lambda content_id: post(content_id, user_id=user_id),
            search=lambda query, product='Latest', count=20, cursor=None: page(cursor=cursor, title=query)),
        comments=SimpleNamespace(list=lambda content_id, cursor=None: page(cursor=cursor, title='Reply ' + content_id)),
    )


application = create_app(Settings(KEY, timeout=5), client_factory=synthetic_sdk)
