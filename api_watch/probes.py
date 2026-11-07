"""Bounded, read-only canaries. Results contain metadata, never response bodies.

This module does not log in, change an account, send messages or refresh the
checked-in API registry. Cookies are supplied by the caller and kept in memory.
"""
from __future__ import annotations

import math
import threading
import time
from urllib.parse import unquote

import requests
from curl_cffi.requests.exceptions import RequestException as CurlRequestException

from builder.auth import XAuth
from utils.data_util import handle_work_info
from utils.frontend import SHELL_URL, shell_headers
from utils.notification_util import parse_notification_page
from utils.transaction import ClientTransaction, fetch_indices
from utils.x_util import extract_cursor, extract_tweet_entries
from x_apis.client_errors import XClientError
from x_apis.user_monitor_sources import GuestTimelineSource
from x_apis.x_api import XAPI
from x_apis.response_validation import validated_timeline


_SUMMARIES = {
    'ok': '只读请求及必要字段检查通过',
    'auth_expired': '会话或访客身份不可用',
    'rate_limited': '请求受到限流',
    'network_error': '网络或上游服务请求失败',
    'contract_error': '接口响应不满足现有请求或解析契约',
    'blocked': '前置检查失败或访问受限，未完成检查',
    'disabled': '此检查未启用或缺少必要配置',
    'sample_unavailable': '固定样本不存在或当前身份不可访问',
    'incomplete': '检查未能提供完整的当前接口证据',
}
_MISSING = object()


class _Issue(Exception):
    def __init__(self, status, *, fields=(), **details):
        self.status = status
        self.details = details
        if fields:
            self.details['fields'] = list(fields)


def _record(check_id, status, details=None, summary=None):
    return {'check_id': check_id, 'status': status,
            'summary': summary or _SUMMARIES[status], 'details': details or {}}


def _error_codes(errors):
    """Upstream messages/extensions may contain private data; retain numeric codes."""
    if not isinstance(errors, list):
        return []
    values = set()
    for error in errors:
        code = error.get('code') if isinstance(error, dict) else None
        if not isinstance(code, bool) and isinstance(code, (int, str)):
            value = str(code)
            if value.isascii() and value.isdigit() and len(value) <= 6:
                values.add(value)
    return sorted(values)


def _classification(status, codes, *, partial=False):
    codes = set(codes)
    if status == 401 or codes & {'32', '89', '215', '239'}:
        return 'auth_expired'
    if status == 429 or codes & {'88'}:
        return 'rate_limited'
    if codes & {'50', '63', '144', '179', '136'}:
        return 'sample_unavailable'
    if status == 403 or codes & {'64', '226', '326', '344'}:
        return 'blocked'
    if status is not None and status >= 500:
        return 'network_error'
    if partial:
        return 'incomplete'
    return 'contract_error'


def _failure(exc):
    if isinstance(exc, _Issue):
        return exc.status, exc.details
    response = getattr(exc, 'response', None)
    status = getattr(response, 'status_code', None)
    status = status if type(status) is int and 100 <= status <= 599 else None
    errors = getattr(exc, 'errors', None)
    raw = getattr(exc, 'res_json', None)
    if response is not None:
        try:
            raw = response.json()
            if isinstance(raw, dict):
                errors = raw.get('errors', errors)
        except Exception:
            pass
    codes = _error_codes(errors)
    details = {}
    if status is not None:
        details['http_status'] = status
    if codes:
        details['error_codes'] = codes
    if isinstance(exc, XClientError) and exc.code == 'guest_auth_failed':
        return 'auth_expired', details
    if isinstance(exc, XClientError) and exc.code == 'capability_unavailable':
        details['reason'] = 'capability_unavailable'
        if isinstance(exc.result, dict):
            for key in ('category', 'reason'):
                if exc.result.get(key) in ('SuperFollowers', 'feature_disabled', 'creator_required'):
                    details['availability_' + key] = exc.result[key]
        return 'blocked', details
    if status is not None or errors:
        return _classification(status, codes,
                               partial=isinstance(raw, dict) and bool(raw.get('data'))), details
    if isinstance(exc, (ValueError, TypeError, KeyError, AttributeError)):
        return 'contract_error', details
    if isinstance(exc, (requests.RequestException, CurlRequestException, TimeoutError, ConnectionError, OSError)):
        return 'network_error', details
    return 'contract_error', details


def _checked(raw):
    if not isinstance(raw, dict):
        raise _Issue('contract_error', fields=('response',))
    if raw.get('errors'):
        codes = _error_codes(raw['errors'])
        raise _Issue(_classification(None, codes, partial=bool(raw.get('data'))),
                     error_codes=codes, partial_data=bool(raw.get('data')))
    if not isinstance(raw.get('data'), dict):
        raise _Issue('contract_error', fields=('data',))
    return raw


def _path(raw, path):
    value = raw
    for key in path.split('.'):
        if not isinstance(value, dict) or key not in value:
            raise _Issue('contract_error', fields=(path,))
        value = value[key]
    return value


def _user(raw, *, viewer=False):
    _checked(raw)
    path = 'data.viewer.user_results.result' if viewer else 'data.user.result'
    user = _path(raw, path)
    if user is None or isinstance(user, dict) and user.get('__typename') in ('UserUnavailable', 'UserTombstone'):
        raise _Issue('auth_expired' if viewer else 'sample_unavailable')
    if not isinstance(user, dict) or not str(user.get('rest_id', '')).isdigit():
        raise _Issue('contract_error', fields=(path + '.rest_id',))
    core = user.get('core') or user.get('legacy')
    if not isinstance(core, dict) or not isinstance(core.get('screen_name'), str) or not core['screen_name']:
        raise _Issue('contract_error', fields=(path + '.core.screen_name',))
    return {'identity_fields_valid': True}


def _viewer(raw, auth):
    details = _user(raw, viewer=True)
    # twid is an untrusted hint, never the authority for account identity.
    # Compare only a parseable hint and never include either ID in diagnostics.
    expected_id = unquote(auth.cookie.get('twid', '')).strip('"')
    if expected_id.startswith('u='):
        expected_id = expected_id[2:]
    if expected_id.isascii() and expected_id.isdigit():
        observed_id = str(_path(raw, 'data.viewer.user_results.result.rest_id'))
        if observed_id != expected_id:
            raise _Issue('incomplete', fields=('viewer.identity_match',), identity_mismatch=True)
        details['cookie_identity_matches'] = True
    return details


def _tweet(result):
    if not isinstance(result, dict):
        raise _Issue('contract_error', fields=('tweet_results.result',))
    if result.get('__typename') == 'TweetWithVisibilityResults':
        result = result.get('tweet')
    if not isinstance(result, dict):
        raise _Issue('contract_error', fields=('tweet_results.result.tweet',))
    if result.get('__typename') in ('TweetUnavailable', 'TweetTombstone'):
        return False
    legacy = result.get('legacy')
    if not isinstance(legacy, dict):
        raise _Issue('contract_error', fields=('tweet.legacy',))
    if not str(result.get('rest_id') or legacy.get('id_str') or '').isdigit():
        raise _Issue('contract_error', fields=('tweet.rest_id',))
    user = _path(result, 'core.user_results.result')
    if not isinstance(user, dict) or not str(user.get('rest_id', '')).isdigit():
        raise _Issue('contract_error', fields=('tweet.core.user_results.result.rest_id',))
    core = user.get('core')
    if not isinstance(core, dict) or not isinstance(core.get('screen_name'), str) or not core['screen_name']:
        raise _Issue('contract_error', fields=('tweet.author.core.screen_name',))
    # Exercise the actual flattening path too; no text/ID is retained in results.
    work = handle_work_info(result)
    if not isinstance(legacy.get('full_text'), str) or not isinstance(work.get('title'), str):
        raise _Issue('contract_error', fields=('tweet.legacy.full_text',))
    if not isinstance(legacy.get('created_at'), str) or not legacy['created_at']:
        raise _Issue('contract_error', fields=('tweet.legacy.created_at',))
    return True


def _instructions(raw, kind):
    _checked(raw)
    if kind == 'user':
        user = _path(raw, 'data.user.result')
        if user is None or isinstance(user, dict) and user.get('__typename') in ('UserUnavailable', 'UserTombstone'):
            raise _Issue('sample_unavailable')
        if not isinstance(user, dict):
            raise _Issue('contract_error', fields=('data.user.result',))
        timeline = user.get('timeline_v2', user.get('timeline', _MISSING))
        if isinstance(timeline, dict):
            timeline = timeline.get('timeline', timeline)
        if not isinstance(timeline, dict):
            raise _Issue('contract_error', fields=('user.timeline.instructions',))
        instructions = timeline.get('instructions')
    else:
        path = {'search': 'data.search_by_raw_query.search_timeline.timeline.instructions',
                'detail': 'data.threaded_conversation_with_injections_v2.instructions'}[kind]
        instructions = _path(raw, path)
    if not isinstance(instructions, list):
        raise _Issue('contract_error', fields=('timeline.instructions',))
    _instruction_shapes(instructions)
    return instructions


def _instruction_shapes(instructions):
    """Distinguish a real empty page from a newly renamed instruction format."""
    if not isinstance(instructions, list):
        raise _Issue('contract_error', fields=('timeline.instructions',))
    for instruction in instructions:
        if not isinstance(instruction, dict):
            raise _Issue('contract_error', fields=('timeline.instructions[]',))
        kind = instruction.get('type')
        if kind == 'TimelineAddEntries':
            if not isinstance(instruction.get('entries'), list):
                raise _Issue('contract_error', fields=('timeline.instructions[].entries',))
        elif kind in ('TimelinePinEntry', 'TimelineReplaceEntry'):
            if not isinstance(instruction.get('entry'), dict):
                raise _Issue('contract_error', fields=('timeline.instructions[].entry',))
        elif kind == 'TimelineAddToModule':
            if (not isinstance(instruction.get('moduleEntryId'), str) or not instruction['moduleEntryId']
                    or not isinstance(instruction.get('moduleItems'), list)):
                raise _Issue('contract_error', fields=('timeline.instructions[].moduleItems',))
        elif kind in ('TimelineClearCache', 'TimelineTerminateTimeline', 'TimelineShowAlert'):
            # These control instructions carry no tweet/notification entries.
            continue
        elif kind == 'TimelineClearEntriesUnreadState':
            continue
        elif kind == 'TimelineMarkEntriesUnreadGreaterThanSortIndex':
            index = instruction.get('sort_index')
            if not isinstance(index, str) or not index.isascii() or not index.isdigit():
                raise _Issue('contract_error', fields=('timeline.instructions[].sort_index',))
        else:
            # Includes module-only instructions the current SDK cannot flatten.
            # Do not place the server-supplied type string in diagnostic output.
            raise _Issue('incomplete', fields=('timeline.instructions[].type',))


def _timeline(raw, kind='user', *, expected_post_id=None, guest=False):
    instructions = _instructions(raw, kind)
    count, unavailable, entries_count, unknown = 0, 0, 0, 0
    observed_ids = set()

    def item(content):
        nonlocal count, unavailable, unknown
        if not isinstance(content, dict):
            raise _Issue('contract_error', fields=('timeline.itemContent',))
        if content.get('promotedMetadata'):
            return
        if 'tweet_results' in content or content.get('itemType') == 'TimelineTweet':
            result = _path(content, 'tweet_results.result')
            if _tweet(result):
                count += 1
                tweet = result.get('tweet', result)
                observed_ids.add(str(tweet.get('rest_id') or tweet['legacy'].get('id_str')))
            else:
                unavailable += 1
        elif content:
            unknown += 1

    for instruction in instructions:
        if not isinstance(instruction, dict):
            raise _Issue('contract_error', fields=('timeline.instructions[]',))
        entries = instruction.get('entries', [])
        if instruction.get('type') == 'TimelineAddToModule':
            entries = [{'entryId': instruction['moduleEntryId'],
                        'content': {'items': instruction['moduleItems']}}]
        if not isinstance(entries, list):
            raise _Issue('contract_error', fields=('timeline.entries',))
        if 'entry' in instruction:
            entries = [*entries, instruction['entry']]
        for entry in entries:
            entries_count += 1
            if not isinstance(entry, dict) or not isinstance(entry.get('entryId'), str):
                raise _Issue('contract_error', fields=('timeline.entryId',))
            content = entry.get('content')
            if not isinstance(content, dict):
                raise _Issue('contract_error', fields=('timeline.entry.content',))
            if content.get('cursorType'):
                if not isinstance(content.get('value'), str):
                    raise _Issue('contract_error', fields=('timeline.cursor.value',))
                continue
            if content.get('promotedMetadata'):
                continue
            if 'itemContent' in content:
                item(content['itemContent'])
            elif entry['entryId'].startswith('tweet-'):
                raise _Issue('contract_error', fields=('timeline.itemContent',))
            elif 'items' not in content:
                unknown += 1
            children = content.get('items', [])
            if not isinstance(children, list):
                raise _Issue('contract_error', fields=('timeline.items',))
            for child in children:
                item(_path(child, 'item.itemContent'))
    if expected_post_id and str(expected_post_id) not in observed_ids:
        raise _Issue('sample_unavailable', item_count=count, unavailable_count=unavailable)
    details = {'item_count': count, 'entry_count': entries_count,
               'unavailable_count': unavailable, 'has_cursor': bool(extract_cursor(raw))}
    if not count and unknown:
        raise _Issue('incomplete', **details, unrecognized_count=unknown)
    # Exercise the SDK's actual canonicalization, including module tweets.
    # Applying the legacy flat helper to raw payloads falsely reports loss for
    # conversation modules even though the business facade preserves them.
    parsed = raw if guest else validated_timeline(raw, kind)
    if not guest and len(extract_tweet_entries(parsed)) < count:
        raise _Issue('contract_error', fields=('timeline.entryId',), parsed_item_count=count)
    return details


def _post_result(raw, post_id):
    _checked(raw)
    result = _path(raw, 'data.tweetResult.result')
    if result is None or not _tweet(result):
        raise _Issue('sample_unavailable')
    tweet = result.get('tweet', result)
    if str(tweet.get('rest_id') or tweet['legacy'].get('id_str')) != str(post_id):
        raise _Issue('contract_error', fields=('tweetResult.result.rest_id',))
    return {'item_count': 1}


def _notifications(raw):
    _checked(raw)
    _instruction_shapes(_path(raw, 'data.viewer_v2.user_results.result.notification_timeline.timeline.instructions'))
    page = parse_notification_page(raw)
    # Empty notifications are valid. Unknown formats cannot establish parsing.
    if any(row.get('item_type') not in ('TimelineTweet', 'TimelineNotification') for row in page['items']):
        raise _Issue('incomplete', item_count=len(page['items']))
    for row in page['items']:
        if row.get('item_type') == 'TimelineNotification':
            if not isinstance(row.get('notification_id'), str) or not row['notification_id']:
                raise _Issue('incomplete', fields=('notification.id',), item_count=len(page['items']))
            # A known type tag alone is not evidence that notification content
            # still parses. Some templates use actors/targets instead of text.
            if not (isinstance(row.get('text'), str) and row['text'].strip()
                    or row['actors'] or row['tweets']):
                raise _Issue('incomplete', fields=('notification.content',), item_count=len(page['items']))
        for actor in row['actors']:
            if (not str(actor.get('user_id') or '').isdigit()
                    or not isinstance(actor.get('screen_name'), str) or not actor['screen_name']):
                raise _Issue('incomplete', fields=('notification.actors',), item_count=len(page['items']))
        if row.get('item_type') == 'TimelineTweet' and not row['tweets'] and row.get('status') != 'tweet_unavailable':
            raise _Issue('contract_error', fields=('notification.tweet_results.result',))
        for tweet in row['tweets']:
            if (not str(tweet.get('work_id') or '').isdigit()
                    or not str(tweet.get('user_id') or '').isdigit()
                    or not isinstance(tweet.get('screen_name'), str) or not tweet['screen_name']
                    or not isinstance(tweet.get('title'), str)):
                raise _Issue('contract_error', fields=('notification.tweet',))
    return {'item_count': len(page['items']), 'has_cursor': bool(page['next_cursor'])}


class _Pacer:
    def __init__(self, gap, stop_event):
        self.gap = float(gap)
        if not math.isfinite(self.gap) or self.gap < 0:
            raise ValueError('request_gap_seconds must be finite and non-negative')
        self.stop_event = stop_event or threading.Event()
        self.last_request = None

    def wait(self):
        if self.stop_event.is_set():
            raise _Issue('incomplete', interrupted=True)
        delay = 0 if self.last_request is None else max(0, self.gap - (time.monotonic() - self.last_request))
        if delay and self.stop_event.wait(delay):
            raise _Issue('incomplete', interrupted=True)
        if self.stop_event.is_set():
            raise _Issue('incomplete', interrupted=True)
        self.last_request = time.monotonic()


class _GuestSource(GuestTimelineSource):
    def __init__(self, pacer, **kwargs):
        self.pacer = pacer
        super().__init__(**kwargs)

    def _request(self, method, url, **kwargs):
        self.pacer.wait()
        return super()._request(method, url, **kwargs)


def _network_transaction(auth, scope, pacer):
    """One shell per identity; no 12-attempt shell loop or implicit fallback.

    Authenticated shell cookies are sent only to the fixed x.com URL. Redirects
    are reported as incomplete, and the subsequent CDN fetch has no cookies.
    """
    pacer.wait()
    kwargs = {'headers': shell_headers(), 'proxies': auth.proxies,
              'timeout': 30, 'allow_redirects': False}
    if scope == 'auth':
        kwargs['cookies'] = dict(auth.cookie)
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(SHELL_URL, **kwargs)
        response.raise_for_status()
        if 300 <= response.status_code < 400:
            raise _Issue('incomplete', http_status=response.status_code, redirected=True)
        html = response.text
        if 'responsive-web/client-web' not in html:
            raise _Issue('incomplete', fields=('client_web_shell',), frontend_family='unrecognized')

        class PublicAssets:
            @staticmethod
            def get(url, **options):
                # fetch_indices builds this URL from the fixed CDN asset base.
                if not url.startswith('https://abs.twimg.com/responsive-web/client-web/'):
                    raise _Issue('contract_error', fields=('transaction.asset_url',))
                pacer.wait()
                # Discard any Set-Cookie from the shell before the CDN request.
                session.cookies.clear()
                options['allow_redirects'] = False
                result = session.get(url, **options)
                if 300 <= result.status_code < 400:
                    raise _Issue('incomplete', http_status=result.status_code, redirected=True)
                return result

        indices = fetch_indices(html, session=PublicAssets(), proxies=auth.proxies)
        return ClientTransaction.from_html(html, indices)
    finally:
        session.cookies.clear()
        session.close()


def _prepare_transaction(auth, scope, pacer):
    started = time.monotonic()
    try:
        auth._transaction = _network_transaction(auth, scope, pacer)
        result = _record(scope + '.transaction', 'ok',
                         {'source': 'network', 'current_source_verified': True})
    except Exception as exc:
        failure, details = _failure(exc)
        details.update(source='unavailable', current_source_verified=False, failure_status=failure)
        auth._transaction = None
        # A throttle or expired identity is an instruction to stop this scope,
        # not a reason to bypass the failed preflight with old local material.
        if failure not in ('rate_limited', 'auth_expired') and not details.get('interrupted'):
            try:
                auth._transaction = ClientTransaction.from_cached()
                details['source'] = 'cache'
            except Exception:
                auth._transaction = None
        result = _record(scope + '.transaction', 'incomplete' if auth._transaction else failure,
                         details, '当前签名素材未验证；使用本地缓存' if auth._transaction else None)
    result['details']['duration_ms'] = round((time.monotonic() - started) * 1000)
    return result


def run_probes(config: dict, *, cookies: str = '', proxy: str | None = None, stop_event=None) -> list[dict]:
    """Run one canary round, with no login, mutation or credential persistence.

    The optional pagination probe requests only the next user timeline page.
    HTTP 404 is a contract failure; explicit missing/inaccessible sample data is
    reported separately. A failed Viewer prevents all remaining account reads.
    """
    pacer = _Pacer(config.get('request_gap_seconds', 1), stop_event)
    results = []
    blocked_scopes = {}
    username = config.get('username', 'OpenAI')
    post_id = str(config.get('post_id') or '')
    count = config.get('count', 5)
    if type(count) is not int or not 1 <= count <= 20:
        raise ValueError('probe count must be between 1 and 20')

    def add(check_id, status, **details):
        results.append(_record(check_id, status, details))

    def check(check_id, action, validate, *, pace=True):
        started = time.monotonic()
        raw = None
        scope = check_id.split('.', 1)[0]
        if scope in blocked_scopes:
            result = _record(check_id, 'blocked', {'prerequisite': blocked_scopes[scope]})
            results.append(result)
            return result, None
        try:
            if pacer.stop_event.is_set():
                raise _Issue('incomplete', interrupted=True)
            if pace:
                pacer.wait()
            raw = action()
            details = validate(raw)
            result = _record(check_id, 'ok', details)
        except Exception as exc:
            status, details = _failure(exc)
            result = _record(check_id, status, details)
        result['details']['duration_ms'] = round((time.monotonic() - started) * 1000)
        results.append(result)
        if result['status'] in ('auth_expired', 'rate_limited'):
            blocked_scopes[scope] = check_id
        return result, raw

    def timeline(scope, request, *, guest=False):
        result, raw = check(scope + '.timeline', lambda: request(None),
                            lambda value: _timeline(value, guest=guest), pace=not guest)
        if config.get('pagination', False):
            cursor = extract_cursor(raw) if result['status'] == 'ok' else None
            if cursor:
                def validate_page(value):
                    details = _timeline(value, guest=guest)
                    if extract_cursor(value) == cursor:
                        raise _Issue('contract_error', fields=('timeline.cursor.progress',))
                    return details
                check(scope + '.timeline.page2', lambda: request(cursor), validate_page, pace=not guest)
            else:
                add(scope + '.timeline.page2', 'incomplete' if result['status'] == 'ok' else 'blocked',
                    has_cursor=False)

    guest_ids = ['guest.transaction', 'guest.user', 'guest.timeline']
    if config.get('pagination', False):
        guest_ids.append('guest.timeline.page2')
    if not config.get('guest', True):
        for check_id in guest_ids:
            add(check_id, 'disabled')
    else:
        source = _GuestSource(pacer, proxy=proxy)
        try:
            results.append(_prepare_transaction(source.auth, 'guest', pacer))
            if source.auth._transaction is None:
                for check_id in guest_ids[1:]:
                    add(check_id, 'blocked', prerequisite='guest.transaction')
            else:
                result, raw = check('guest.user', lambda: source._query('UserByScreenName', {
                    'screen_name': username, 'withGrokTranslatedBio': True}), _user, pace=False)
                if result['status'] == 'ok':
                    uid = raw['data']['user']['result']['rest_id']

                    def request_guest(cursor):
                        variables = {'userId': uid, 'count': count, 'includePromotedContent': True,
                                     'withQuickPromoteEligibilityTweetFields': True, 'withVoice': True}
                        if cursor:
                            variables['cursor'] = cursor
                        return source._query('UserTweets', variables)

                    timeline('guest', request_guest, guest=True)
                else:
                    for check_id in guest_ids[2:]:
                        add(check_id, 'blocked', prerequisite='guest.user')
        finally:
            source.close()

    auth_ids = ['auth.transaction', 'auth.viewer', 'auth.user', 'auth.timeline', 'auth.search',
                'auth.post_detail', 'auth.post_result', 'auth.notifications', 'auth.chat_settings']
    if config.get('pagination', False):
        auth_ids.insert(4, 'auth.timeline.page2')
    if not config.get('authenticated', True) or not cookies:
        for check_id in auth_ids:
            add(check_id, 'disabled', reason='disabled_or_missing_cookies')
        return results
    try:
        auth = XAuth(proxies={'http': proxy, 'https': proxy} if proxy else None).prepare_auth(cookies)
        if not auth.is_logged_in:
            raise ValueError('missing auth token')
    except (ValueError, TypeError):
        add('auth.transaction', 'disabled')
        add('auth.viewer', 'auth_expired', reason='incomplete_cookies')
        for check_id in auth_ids[2:]:
            add(check_id, 'blocked', prerequisite='auth.viewer')
        return results
    results.append(_prepare_transaction(auth, 'auth', pacer))
    if auth._transaction is None:
        for check_id in auth_ids[1:]:
            add(check_id, 'blocked', prerequisite='auth.transaction')
        return results
    result, _ = check('auth.viewer', lambda: XAPI.get_viewer(auth), lambda raw: _viewer(raw, auth))
    if result['status'] != 'ok':
        for check_id in auth_ids[2:]:
            add(check_id, 'blocked', prerequisite='auth.viewer')
        return results
    result, raw = check('auth.user', lambda: XAPI.get_user_info(auth, username), _user)
    if result['status'] == 'ok':
        uid = raw['data']['user']['result']['rest_id']
        timeline('auth', lambda cursor: XAPI.get_user_post_note(auth, uid, count=count, cursor=cursor))
    else:
        add('auth.timeline', 'blocked', prerequisite='auth.user')
        if config.get('pagination', False):
            add('auth.timeline.page2', 'blocked', prerequisite='auth.user')
    check('auth.search', lambda: XAPI.search_work(auth, config.get('search_query', 'from:OpenAI'),
                                                 product='Latest', count=count),
          lambda raw: _timeline(raw, 'search'))
    if post_id:
        check('auth.post_detail', lambda: XAPI.get_work_info(auth, post_id),
              lambda raw: _timeline(raw, 'detail', expected_post_id=post_id))
        check('auth.post_result', lambda: XAPI.get_work_result(auth, post_id),
              lambda raw: _post_result(raw, post_id))
    else:
        add('auth.post_detail', 'disabled', reason='missing_post_id')
        add('auth.post_result', 'disabled', reason='missing_post_id')
    if config.get('notifications', True):
        check('auth.notifications', lambda: XAPI.get_notifications(auth, count=count), _notifications)
    else:
        add('auth.notifications', 'disabled')
    if config.get('chat', False):
        # Import only when explicitly requested; never call bootstrap/token/send.
        from x_apis.x_dm_api import XChatAPI

        def chat_shape(raw):
            _checked(raw)
            raise _Issue('incomplete', fields=('chat_settings_field_baseline',), response_container_valid=True)

        check('auth.chat_settings', lambda: XChatAPI.get_dm_settings(auth), chat_shape)
    else:
        add('auth.chat_settings', 'disabled')
    return results
