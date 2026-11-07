"""Bounded authenticated read scenarios and their explicit coverage inventory.

Only the caller owns credentials, pacing, failure isolation and persistence.
Response bodies and cursor values are returned to the caller in memory; validator
summaries contain counts/booleans and fixed field names only. A missing second
page is never represented as a successful pagination check.
"""
from __future__ import annotations

import base64
import json
import math
from pathlib import Path
import re

from api_watch.probes import (
    _Issue, _checked, _instruction_shapes, _instructions, _notifications,
    _path, _post_result, _timeline, _user,
)
from utils.data_util import handle_work_info
from utils.notification_util import parse_notification_page
from utils.x_util import extract_cursor, extract_tweet_entries
from utils.xchat_codec import message_event_summary
from x_apis.x_api import NOTIFICATION_TYPES, XAPI
from x_apis.x_article_api import XArticleAPI
from x_apis.x_dm_api import XChatAPI
from x_apis.response_validation import is_relevance_prompt, validated_list_search_page
from x_apis.client_errors import XClientError


SPECS = []


def _spec(probe_id, operation, method, source, *requirements, pages=False):
    for suffix in ('', '.page2') if pages else ('',):
        SPECS.append({'id': probe_id + suffix, 'operation': operation,
                      'method': method, 'source': source,
                      'requirements': ['cookies', *requirements],
                      'risk': 'read', 'scenario': 'reads'})


_spec('read.user.profile', 'UserByScreenName', 'GET', 'x_apis.x_api.XAPI.get_user_info', 'samples.username')
for _variant, _operation in (('originals', 'UserOriginalsTimeline'), ('tweets', 'UserTweets')):
    _spec('read.user.' + _variant, _operation, 'GET', 'x_apis.x_api.XAPI.get_user_post_note',
          'samples.username', pages=True)
for _method in ('get', 'post'):
    _spec('read.home.' + _method, 'HomeTimeline', _method.upper(),
          'x_apis.x_api.XAPI.get_home_timeline',
          *(['samples.post_id|home.get.item'] if _method == 'post' else []), pages=True)
for _product in ('Top', 'Latest', 'People', 'Media', 'Lists'):
    _spec('read.search.' + _product.lower(), 'ListSearchTimeline' if _product == 'Lists' else 'SearchTimeline',
          'GET' if _product == 'Lists' else 'POST',
          'x_apis.x_api.XAPI.search_work', 'samples.search_query', pages=True)
for _kind in NOTIFICATION_TYPES:
    _spec('read.notifications.' + _kind.lower(), 'NotificationsTimeline', 'GET',
          'x_apis.x_api.XAPI.get_notifications', pages=True)
_spec('read.post.detail', 'TweetDetail', 'GET', 'x_apis.x_api.XAPI.get_work_info', 'samples.post_id', pages=True)
_spec('read.post.hydration', 'TweetResultByRestId', 'GET', 'x_apis.x_api.XAPI.get_work_result', 'samples.post_id')
_spec('read.post.comments', 'TweetDetail', 'GET', 'x_apis.x_api.XAPI.get_work_comments', 'samples.post_id', pages=True)
for _lifecycle in ('Draft', 'Published'):
    _spec('read.articles.' + _lifecycle.lower(), 'ArticleEntitiesSlice', 'GET',
          'x_apis.x_article_api.XArticleAPI.list_articles', pages=True)
_spec('read.chat.settings', 'xChatDmSettingsQuery', 'GET', 'x_apis.x_dm_api.XChatAPI.get_dm_settings',
      'samples.chat_settings_contract_file')
_spec('read.chat.members', 'GetUsersByIdsForXChat', 'GET', 'x_apis.x_dm_api.XChatAPI.get_users_by_ids', 'samples.peer_user_id')
_spec('read.chat.public_keys', 'GetPublicKeysQuery', 'GET', 'x_apis.x_dm_api.XChatAPI.get_public_keys', 'samples.peer_user_id')
_spec('read.chat.inbox', 'GetInitialXChatPageQuery', 'GET', 'x_apis.x_dm_api.XChatAPI.get_initial_chat_page')
_spec('read.chat.conversation', 'GetConversationPageQuery', 'GET', 'x_apis.x_dm_api.XChatAPI.get_conversation_page',
      'samples.conversation_id', pages=True)


def _pages(ctx, probe_id, request, validator, *, cursor=extract_cursor, first_validator=None):
    """Run two actual requests only when page one supplied a usable cursor."""
    first = ctx.step(probe_id, lambda: request(None), first_validator or validator)
    second_id = probe_id + '.page2'
    if not first.ok:
        ctx.block([second_id], 'first_page_failed')
    elif not ctx.config['probes'].get('pagination', True):
        ctx.block([second_id], 'pagination_disabled', status='incomplete')
    else:
        next_cursor = cursor(first.value)
        if next_cursor is None or next_cursor == '':
            ctx.block([second_id], 'pagination_no_cursor', status='incomplete')
        else:
            def second(value):
                details = validator(value)
                returned = cursor(value)
                if returned is not None and returned == next_cursor:
                    raise _Issue('incomplete', fields=('pagination.cursor_progress',))
                return dict(details, page_count=2)
            ctx.step(second_id, lambda: request(next_cursor), second)
    return first


def _home(raw):
    _checked(raw)
    instructions = _path(raw, 'data.home.home_timeline_urt.instructions')
    # Reuse the production tweet/parser checks at this operation's real path.
    return _timeline({'data': {'threaded_conversation_with_injections_v2': {
        'instructions': instructions}}}, 'detail')


def _timeline_items(instructions):
    """Validate timeline item/module containers before yielding their contents."""
    _instruction_shapes(instructions)
    for instruction in instructions:
        entries = instruction.get('entries', [])
        if instruction.get('type') == 'TimelineAddToModule':
            entries = [{'entryId': instruction['moduleEntryId'],
                        'content': {'items': instruction['moduleItems']}}]
        if 'entry' in instruction:
            entries = [*entries, instruction['entry']]
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get('entryId'), str):
                raise _Issue('contract_error', fields=('timeline.entryId',))
            content = _path(entry, 'content')
            if not isinstance(content, dict):
                raise _Issue('contract_error', fields=('timeline.entry.content',))
            if content.get('cursorType'):
                if not isinstance(content.get('value'), str):
                    raise _Issue('contract_error', fields=('timeline.cursor.value',))
                continue
            if content.get('promotedMetadata'):
                continue
            if 'itemContent' in content:
                item = content['itemContent']
                if not isinstance(item, dict):
                    raise _Issue('contract_error', fields=('timeline.itemContent',))
                yield item
            elif 'items' not in content:
                raise _Issue('incomplete', fields=('timeline.entry.content',))
            children = content.get('items', [])
            if not isinstance(children, list):
                raise _Issue('contract_error', fields=('timeline.items',))
            for child in children:
                item = _path(child, 'item.itemContent')
                if not isinstance(item, dict):
                    raise _Issue('contract_error', fields=('timeline.itemContent',))
                yield item


def _search(raw, product):
    if product == 'Lists':
        try:
            page = validated_list_search_page(raw)
        except XClientError as exc:
            if exc.code == 'invalid_response':
                raise _Issue('contract_error', fields=('search.lists.response',)) from None
            _checked(raw)
            raise
        details = {'item_count': len(page['items']), 'has_cursor': bool(page['next_cursor']),
                   'parser_exercised': bool(page['items']), 'partial': page['partial'],
                   'error_codes': page['error_codes']}
        if page['partial']:
            raise _Issue('incomplete', reason='optional_list_banner_errors', **details)
        return details
    instructions = _instructions(raw, 'search')
    if product != 'People':
        # Check every unknown item, even if recognized tweets were also present.
        for item in _timeline_items(instructions):
            if is_relevance_prompt(item):
                continue
            if not item.get('promotedMetadata') and not (
                    'tweet_results' in item or item.get('itemType') == 'TimelineTweet'):
                raise _Issue('incomplete', fields=('search.item_type',))
        return _timeline(raw, 'search')
    count, unavailable = 0, 0
    for item in _timeline_items(instructions):
        if item.get('promotedMetadata'):
            continue
        if item.get('itemType') != 'TimelineUser':
            raise _Issue('incomplete', fields=('search.people.item_type',))
        result = _path(item, 'user_results.result')
        if isinstance(result, dict) and result.get('__typename') in ('UserUnavailable', 'UserTombstone'):
            unavailable += 1
            continue
        _user({'data': {'user': {'result': result}}})
        count += 1
    return {'item_count': count, 'unavailable_count': unavailable,
            'has_cursor': bool(extract_cursor(raw)), 'parser_exercised': bool(count)}


def _comments(raw, post_id):
    details = _timeline(raw, 'detail')
    count = 0
    # This is the same direct-reply selection as XClient.comments.list; details
    # can also contain the parent, ancestors and unrelated recommendations.
    for entry in extract_tweet_entries(raw):
        tweet = entry.get('tweet', entry)
        if (tweet.get('legacy') or {}).get('in_reply_to_status_id_str') == post_id:
            parsed = handle_work_info(tweet)
            if str(parsed.get('work_id')) == post_id:
                raise _Issue('contract_error', fields=('comments.parent_excluded',))
            count += 1
    if not count:
        raise _Issue('incomplete', fields=('comments.direct_reply_sample',), item_count=0)
    return dict(details, direct_reply_count=count, parser_exercised=True)


def _article_page(raw):
    _checked(raw)
    page = _path(raw, 'data.user.result.articles_article_mixer_slice')
    if not isinstance(page, dict) or not isinstance(page.get('items'), list):
        raise _Issue('contract_error', fields=('articles_article_mixer_slice.items',))
    return page


def _article_cursor(raw):
    page = _article_page(raw)
    # Do not recursively find a random item's cursor and use it for the slice.
    cursor = page.get('next_cursor', page.get('cursor'))
    if cursor is not None and not isinstance(cursor, str):
        raise _Issue('contract_error', fields=('articles.pagination.cursor',))
    return cursor or None


def _articles(raw, lifecycle):
    page = _article_page(raw)
    for item in page['items']:
        article = _path(item, 'article_entity_results.result')
        if (not isinstance(article, dict) or not isinstance(article.get('rest_id'), str)
                or not article['rest_id'] or not isinstance(article.get('title'), str)):
            raise _Issue('contract_error', fields=('articles.item',))
        if _path(article, 'lifecycle_state.lifecycle') != lifecycle:
            raise _Issue('contract_error', fields=('articles.lifecycle',))
    return {'item_count': len(page['items']), 'has_cursor': bool(_article_cursor(raw)),
            'parser_exercised': bool(page['items'])}


def _members(raw, user_ids):
    _checked(raw)
    rows = _path(raw, 'data.get_member_results.results')
    if not isinstance(rows, list) or len(rows) != len(user_ids):
        raise _Issue('contract_error', fields=('chat.members.results',))
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise _Issue('contract_error', fields=('chat.members.result',))
        uid = row.get('id')
        if not isinstance(uid, str) or uid not in user_ids or uid in seen:
            raise _Issue('contract_error', fields=('chat.members.identity_match',))
        seen.add(uid)
        if row.get('status') != 'Found':
            raise _Issue('sample_unavailable', fields=('chat.members.status',))
        member = _path(row, 'member_results.result')
        if _path(row, 'member_results.rest_id') != uid or not isinstance(member, dict) or member.get('rest_id') != uid:
            raise _Issue('contract_error', fields=('chat.members.identity_match',))
        _user({'data': {'user': {'result': member}}})
        permissions = member.get('chat_permissions')
        if not isinstance(permissions, dict) or any(type(permissions.get(k)) is not bool for k in ('can_dm', 'can_dm_on_xchat')):
            raise _Issue('incomplete', fields=('chat.members.chat_permissions',))
    return {'item_count': len(rows), 'identity_fields_valid': True}


def _public_keys(raw, user_ids):
    _checked(raw)
    rows = _path(raw, 'data.user_results_by_rest_ids')
    if not isinstance(rows, list) or len(rows) != len(user_ids):
        raise _Issue('contract_error', fields=('chat.public_keys.results',))
    seen, count = set(), 0
    for row in rows:
        if not isinstance(row, dict):
            raise _Issue('contract_error', fields=('chat.public_keys.result',))
        uid = row.get('rest_id')
        if not isinstance(uid, str) or uid not in user_ids or uid in seen:
            raise _Issue('contract_error', fields=('chat.public_keys.identity_match',))
        seen.add(uid)
        result = _path(row, 'result')
        if not isinstance(result, dict) or result.get('__typename') != 'User':
            raise _Issue('sample_unavailable', fields=('chat.public_keys.user',))
        keys = _path(result, 'get_public_keys')
        if not isinstance(keys, dict) or keys.get('__typename') != 'GetPublicKeysResult':
            raise _Issue('incomplete', fields=('chat.public_keys.result_type',))
        records = keys.get('public_keys_with_token_map')
        if not isinstance(records, list):
            raise _Issue('contract_error', fields=('chat.public_keys.records',))
        versions = set()
        for record in records:
            metadata = _path(record, 'public_key_with_metadata')
            version = _path(metadata, 'version')
            if (not isinstance(version, str) or not version.isascii() or not version.isdigit()
                    or int(version) <= 0 or version in versions):
                raise _Issue('contract_error', fields=('chat.public_keys.version',))
            versions.add(version)
            for key in ('public_key', 'signing_public_key', 'identity_public_key_signature'):
                value = _path(metadata, 'public_key.' + key)
                try:
                    if not isinstance(value, str) or not base64.b64decode(value, validate=True):
                        raise ValueError()
                except ValueError:
                    raise _Issue('contract_error', fields=('chat.public_keys.key_material',)) from None
            count += 1
    return {'item_count': len(rows), 'key_count': count, 'identity_fields_valid': True}


_SETTINGS_TYPES = {'boolean', 'string', 'integer', 'number', 'object', 'array', 'null'}
_SETTINGS_PATH = re.compile(r'data(?:\.[A-Za-z_][A-Za-z0-9_]*){1,11}\Z', re.ASCII)


def _value_type(value):
    if value is None:
        return 'null'
    if type(value) is bool:
        return 'boolean'
    if type(value) is int:
        return 'integer'
    if type(value) is float and math.isfinite(value):
        return 'number'
    if isinstance(value, str):
        return 'string'
    if isinstance(value, dict):
        return 'object'
    if isinstance(value, list):
        return 'array'
    raise _Issue('contract_error', fields=('chat.settings.value_type',))


def _validate_settings_contract(contract):
    if not isinstance(contract, dict) or not 1 <= len(contract) <= 256:
        raise _Issue('blocked', fields=('chat.settings.contract',), reason='invalid_settings_contract')
    if any(not isinstance(path, str) or len(path) > 500 or not _SETTINGS_PATH.fullmatch(path)
           or not isinstance(kind, str) or kind not in _SETTINGS_TYPES for path, kind in contract.items()):
        raise _Issue('blocked', fields=('chat.settings.contract',), reason='invalid_settings_contract')
    # data/object/list containers alone recreate the old false-success check.
    if not any(kind in ('boolean', 'string', 'integer', 'number') for kind in contract.values()):
        raise _Issue('incomplete', fields=('chat.settings.concrete_fields',))
    return contract


def derive_settings_contract(raw):
    """Derive a bounded candidate of field paths/types; never accept it here.

    Arrays are recorded as arrays, without following data-dependent indices.
    This intentionally exports neither account values nor opaque array values.
    The operator must review the candidate before configuring it as a contract.
    """
    _checked(raw)
    contract = {}

    def visit(value, path, depth):
        if depth > 12 or len(contract) >= 256:
            raise _Issue('incomplete', fields=('chat.settings.contract_limits',))
        if path != 'data':
            if not _SETTINGS_PATH.fullmatch(path) or len(path) > 500:
                raise _Issue('incomplete', fields=('chat.settings.field_name',))
            contract[path] = _value_type(value)
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key, re.ASCII):
                    raise _Issue('incomplete', fields=('chat.settings.field_name',))
                visit(child, path + '.' + key, depth + 1)

    visit(raw['data'], 'data', 1)
    return _validate_settings_contract(contract)


def _load_settings_contract(path):
    try:
        target = Path(path)
        if not target.is_file() or target.stat().st_size > 65536:
            raise ValueError()
        contract = json.loads(target.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError):
        raise _Issue('blocked', fields=('samples.chat_settings_contract_file',),
                     reason='settings_contract_unreadable') from None
    return _validate_settings_contract(contract)


def _chat_settings(raw, contract=None):
    _checked(raw)
    if contract is None:
        raise _Issue('incomplete', fields=('chat.settings.field_baseline',))
    _validate_settings_contract(contract)
    for path, expected in contract.items():
        actual = _value_type(_path(raw, path))
        if actual != expected and not (expected == 'number' and actual == 'integer'):
            raise _Issue('contract_error', fields=(path,))
    return {'checked_field_count': len(contract), 'reviewed_contract_matched': True}


# MessageEventDetailAdapter in the authenticated xchat embed build
# bf9d4ef96b0e920edb7a41bc02fcd5ab2413c691, dm-core-Bq9lt84w.js (2026-11-07).
# These are wire-envelope types, not claims that their payloads were decrypted.
# Type 2 remains accepted for legacy observations; 11 is not a defined variant.
_KNOWN_CHAT_EVENT_TYPES = frozenset((
    1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 13, 14, 15, 16, 17, 18, 19,
))


def _event(encoded, *, conversation_id=None):
    try:
        summary = message_event_summary(encoded)
    except (ValueError, TypeError, UnicodeError):
        raise _Issue('contract_error', fields=('chat.event.encoding',)) from None
    sequence = summary.get('sequence_id')
    if (not isinstance(sequence, str) or not sequence.isascii() or not sequence.isdigit()
            or not 0 < int(sequence) < 2**63
            or not isinstance(summary.get('message_id'), str) or not summary['message_id']
            or not isinstance(summary.get('conversation_id'), str) or not summary['conversation_id']
            or not isinstance(summary.get('sender_id'), str) or not summary['sender_id'].isascii()
            or not summary['sender_id'].isdigit()):
        raise _Issue('contract_error', fields=('chat.event.identity',))
    if conversation_id is not None and summary['conversation_id'] != conversation_id:
        raise _Issue('contract_error', fields=('chat.event.conversation_match',))
    if summary['event_type'] not in _KNOWN_CHAT_EVENT_TYPES:
        raise _Issue('incomplete', fields=('chat.event.type',))
    return summary


def _conversation_page(raw):
    _checked(raw)
    page = _path(raw, 'data.get_conversation_page')
    if (not isinstance(page, dict) or not isinstance(page.get('encoded_message_events'), list)
            or not isinstance(page.get('missing_conversation_key_change_events', []), list)
            or type(page.get('has_more')) is not bool):
        raise _Issue('contract_error', fields=('chat.conversation.page',))
    return page


def _conversation(raw, conversation_id, *, before=2**63 - 1):
    page = _conversation_page(raw)
    events = page['encoded_message_events']
    missing = page.get('missing_conversation_key_change_events', [])
    seen = set()
    for index, encoded in enumerate(events + missing):
        event = _event(encoded, conversation_id=conversation_id)
        if index < len(events):
            if int(event['sequence_id']) >= before or event['sequence_id'] in seen:
                raise _Issue('contract_error', fields=('chat.conversation.sequence_progress',))
            seen.add(event['sequence_id'])
        elif event['event_type'] != 3:
            raise _Issue('contract_error', fields=('chat.conversation.missing_key_event_type',))
    if page['has_more'] and not events:
        raise _Issue('contract_error', fields=('chat.conversation.pagination',))
    return {'item_count': len(events), 'key_event_count': len(missing),
            'has_cursor': page['has_more'], 'parser_exercised': bool(events or missing)}


def _conversation_cursor(raw):
    page = _conversation_page(raw)
    if not page['has_more']:
        return None
    return str(min(int(_event(event)['sequence_id']) for event in page['encoded_message_events']))


def _chat_inbox(raw):
    _checked(raw)
    page = _path(raw, 'data.get_initial_chat_page')
    if not isinstance(page, dict):
        raise _Issue('contract_error', fields=('chat.inbox.page',))
    if page.get('error_code') or page.get('errors'):
        raise _Issue('incomplete', fields=('chat.inbox.errors',))
    # Cursor unions match the authenticated embed's GetInitialXChatPageQuery
    # in dm-core-Bq9lt84w.js; inbox and delta pages use distinct cursor types.
    # This is structural evidence, not proof that local E2E keys can decrypt.
    kind = page.get('__typename')
    events, items = [], []
    if kind == 'XChatGetInboxPageResponse':
        items = page.get('items')
        cursor = page.get('inboxCursor')
        if not isinstance(items, list):
            raise _Issue('contract_error', fields=('chat.inbox.items',))
        for item in items:
            if not isinstance(item, dict) or item.get('__typename') != 'XChatGetInboxPageConversationData':
                raise _Issue('incomplete', fields=('chat.inbox.item_type',))
            detail = _path(item, 'conversation_detail')
            if (not isinstance(detail, dict) or detail.get('__typename') not in (
                    'XChatDirectConversationDetail', 'XChatGroupConversationDetail')
                    or not isinstance(detail.get('conversation_id'), str) or not detail['conversation_id']):
                raise _Issue('incomplete', fields=('chat.inbox.conversation_identity',))
            latest = item.get('latest_message_events')
            key_events = item.get('latest_conversation_key_change_events', [])
            if not isinstance(latest, list) or not isinstance(key_events, list):
                raise _Issue('contract_error', fields=('chat.inbox.latest_message_events',))
            for encoded in latest + key_events:
                summary = _event(encoded, conversation_id=detail['conversation_id'])
                if encoded in key_events and summary['event_type'] != 3:
                    raise _Issue('contract_error', fields=('chat.inbox.key_event_type',))
                events.append(encoded)
        if not isinstance(cursor, dict):
            raise _Issue('contract_error', fields=('chat.inbox.cursor',))
        cursor_type = cursor.get('__typename')
        if cursor_type == 'XChatGetInboxPageContinueCursor':
            if any(not isinstance(cursor.get(name), str) or not cursor[name]
                   for name in ('cursor_id', 'graph_snapshot_id')):
                raise _Issue('contract_error', fields=('chat.inbox.cursor_identity',))
            restarted = cursor.get('graph_snapshot_restarted')
            if restarted is not None and type(restarted) is not bool:
                raise _Issue('contract_error', fields=('chat.inbox.cursor_restart',))
            has_cursor = True
        elif cursor_type == 'XChatGetInboxPageEndCursor':
            exhausted = cursor.get('inbox_exhausted')
            if exhausted is not None and type(exhausted) is not bool:
                raise _Issue('contract_error', fields=('chat.inbox.cursor_end',))
            # The current persisted query may omit the nullable flag. The
            # explicit terminal union and snapshot ID establish no next page.
            snapshot = cursor.get('graph_snapshot_id')
            if not isinstance(snapshot, str) or not snapshot:
                raise _Issue('contract_error', fields=('chat.inbox.cursor_snapshot',))
            has_cursor = False
        else:
            raise _Issue('incomplete', fields=('chat.inbox.cursor_type',))
    elif kind == 'XChatGetMessageEventsPageResponse':
        events = page.get('encoded_message_events')
        cursor = page.get('cursor')
        if not isinstance(events, list) or not isinstance(cursor, dict):
            raise _Issue('contract_error', fields=('chat.inbox.delta',))
        cursor_type = cursor.get('__typename')
        if cursor_type == 'XChatGetMessageEventsPageContinueCursor':
            sequence = cursor.get('max_local_sequence_id')
            if not isinstance(sequence, str) or not sequence.isascii() or not sequence.isdigit():
                raise _Issue('contract_error', fields=('chat.inbox.max_local_sequence_id',))
            has_cursor = True
        elif cursor_type == 'XChatGetMessageEventsPageEndCursor':
            if type(cursor.get('pull_finished')) is not bool:
                raise _Issue('contract_error', fields=('chat.inbox.delta_end',))
            if not cursor['pull_finished']:
                raise _Issue('incomplete', fields=('chat.inbox.delta_end_unfinished',))
            has_cursor = False
        else:
            raise _Issue('incomplete', fields=('chat.inbox.cursor_type',))
        for encoded in events:
            _event(encoded)
        if not events and has_cursor:
            raise _Issue('incomplete', fields=('chat.inbox.delta_progress',))
    else:
        raise _Issue('incomplete', fields=('chat.inbox.page_type',))
    return {'item_count': len(items), 'event_count': len(events),
            'has_cursor': has_cursor, 'parser_exercised': bool(items or events)}


def run(ctx):
    """Execute every declared read scenario, recording explicit unmet cases."""
    auth, samples = ctx.auth, ctx.samples
    settings = ctx.config['probes']
    count = settings.get('count', 5)

    def block(prefix, reason, status='blocked'):
        ctx.block([s['id'] for s in SPECS if s['id'] == prefix or s['id'].startswith(prefix + '.')], reason, status=status)

    username = samples.get('username')
    if username:
        profile = ctx.step('read.user.profile', lambda: XAPI.get_user_info(auth, username), _user)
        if profile.ok:
            user_id = _path(profile.value, 'data.user.result.rest_id')
            for variant, operation in (('originals', 'UserOriginalsTimeline'), ('tweets', 'UserTweets')):
                _pages(ctx, 'read.user.' + variant,
                       lambda cursor, op=operation: XAPI.get_user_post_note(auth, user_id, cursor=cursor, count=count, operation=op),
                       _timeline)
        else:
            block('read.user.originals', 'profile_failed')
            block('read.user.tweets', 'profile_failed')
    else:
        block('read.user', 'missing_username')

    home = _pages(ctx, 'read.home.get', lambda cursor: XAPI.get_home_timeline(auth, cursor=cursor, count=count), _home)
    seen_id = samples.get('post_id')
    if not seen_id and home.ok:
        for item in extract_tweet_entries(home.value):
            tweet = item.get('tweet', item)
            if str(tweet.get('rest_id') or '').isdigit():
                seen_id = str(tweet['rest_id'])
                break
    if seen_id:
        _pages(ctx, 'read.home.post',
               lambda cursor: XAPI.get_home_timeline(auth, cursor=cursor, count=count, seen_tweet_ids=[seen_id]), _home)
    else:
        block('read.home.post', 'missing_seen_post_sample')

    query = samples.get('search_query')
    if query:
        for product in ('Top', 'Latest', 'People', 'Media', 'Lists'):
            _pages(ctx, 'read.search.' + product.lower(),
                   lambda cursor, p=product: XAPI.search_work(auth, query, cursor=cursor, count=count, product=p),
                   lambda raw, p=product: _search(raw, p))
    else:
        block('read.search', 'missing_search_query')

    if settings.get('notifications', True):
        for kind in NOTIFICATION_TYPES:
            _pages(ctx, 'read.notifications.' + kind.lower(),
                   lambda cursor, k=kind: XAPI.get_notifications(auth, type=k, count=count, cursor=cursor),
                   _notifications, cursor=lambda raw: parse_notification_page(raw)['next_cursor'])
    else:
        block('read.notifications', 'notifications_disabled', 'incomplete')

    post_id = samples.get('post_id')
    if post_id:
        # Only the initial detail page must contain the focal post. Page two
        # generally contains older replies and should not require it again.
        _pages(ctx, 'read.post.detail', lambda cursor: XAPI.get_work_info(auth, post_id, cursor=cursor),
               lambda raw: _timeline(raw, 'detail'),
               first_validator=lambda raw: _timeline(raw, 'detail', expected_post_id=post_id))
        ctx.step('read.post.hydration', lambda: XAPI.get_work_result(auth, post_id), lambda raw: _post_result(raw, post_id))
        _pages(ctx, 'read.post.comments', lambda cursor: XAPI.get_work_comments(auth, post_id, cursor=cursor),
               lambda raw: _comments(raw, post_id))
    else:
        block('read.post', 'missing_post_id')

    for lifecycle in ('Draft', 'Published'):
        _pages(ctx, 'read.articles.' + lifecycle.lower(),
               lambda cursor, life=lifecycle: XArticleAPI.list_articles(auth, lifecycle=life, user_id=ctx.actor_id, count=count, cursor=cursor),
               lambda raw, life=lifecycle: _articles(raw, life), cursor=_article_cursor)

    if not settings.get('chat', True):
        block('read.chat', 'chat_disabled', 'incomplete')
        return
    settings_contract_path = samples.get('chat_settings_contract_file')
    if settings_contract_path:
        contract = {}
        def settings_request():
            contract.update(_load_settings_contract(settings_contract_path))
            return XChatAPI.get_dm_settings(auth)
        ctx.step('read.chat.settings', settings_request, lambda raw: _chat_settings(raw, contract))
    else:
        block('read.chat.settings', 'missing_chat_settings_contract_file')
    peer_id = samples.get('peer_user_id')
    if peer_id:
        ids = list(dict.fromkeys([ctx.actor_id, peer_id]))
        ctx.step('read.chat.members', lambda: XChatAPI.get_users_by_ids(auth, ids), lambda raw: _members(raw, ids))
        ctx.step('read.chat.public_keys', lambda: XChatAPI.get_public_keys(auth, ids, include_juicebox_tokens=False),
                 lambda raw: _public_keys(raw, ids))
    else:
        block('read.chat.members', 'missing_peer_user_id')
        block('read.chat.public_keys', 'missing_peer_user_id')
    ctx.step('read.chat.inbox', lambda: XChatAPI.get_initial_chat_page(auth, query_settings={
        'conversation_event_limit': count, 'inbox_conversation_event_limit': count,
        'inbox_conversation_limit': count, 'user_event_limit': count}), _chat_inbox)
    conversation_id = samples.get('conversation_id')
    if conversation_id:
        before = [2**63 - 1]
        def request(cursor):
            before[0] = int(cursor) if cursor else 2**63 - 1
            return XChatAPI.get_conversation_page(auth, conversation_id, min_local_sequence_id=str(before[0]),
                                                  query_settings={'conversation_event_limit': count,
                                                                  'inbox_conversation_event_limit': count,
                                                                  'inbox_conversation_limit': count,
                                                                  'user_event_limit': count})
        _pages(ctx, 'read.chat.conversation', request,
               lambda raw: _conversation(raw, conversation_id, before=before[0]), cursor=_conversation_cursor)
    else:
        block('read.chat.conversation', 'missing_conversation_id')
