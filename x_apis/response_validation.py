"""Validate read receipts before the permissive legacy flattening helpers run."""

from x_apis.client_errors import XClientError, graphql_error
from utils.data_util import handle_work_info


def invalid_response():
    return XClientError('invalid_response', 'X 响应缺少有效的业务数据结构')


def required_path(value, *keys):
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            raise invalid_response()
        value = value[key]
    return value


def numeric_id(value):
    return isinstance(value, str) and value.isascii() and value.isdigit()


def validated_list_search_page(payload):
    """Parse the dedicated list result and preserve optional banner warnings.

    X currently returns code 214 on nullable default-banner results. Only this
    exact observed field failure can accompany usable list records; identity,
    name, cursor or unrelated GraphQL errors still fail explicitly.
    """
    if not isinstance(payload, dict):
        raise invalid_response()
    errors = payload.get('errors') or []
    if not isinstance(errors, list):
        raise invalid_response()
    for error in errors:
        path = error.get('path') if isinstance(error, dict) else None
        if (not isinstance(error, dict) or error.get('code') not in (214, '214')
                or error.get('name') != 'BadRequestError' or not isinstance(path, list)
                or path[:4] != ['search_by_raw_query', 'list_search_timeline', 'timeline', 'instructions']
                or path[-3:] != ['list', 'default_banner_media_results', 'result']):
            raise graphql_error(errors)
        value = payload.get('data')
        try:
            for index, key in enumerate(path):
                if isinstance(value, list) and type(key) is int and key >= 0:
                    value = value[key]
                elif isinstance(value, dict) and isinstance(key, str):
                    # The observed error leaves an empty banner-results object
                    # rather than materializing its nullable result field.
                    if index == len(path) - 1 and key == 'result':
                        value = value.get(key)
                    else:
                        value = value[key]
                else:
                    raise ValueError()
            if value is not None:
                raise ValueError()
        except (KeyError, IndexError, TypeError, ValueError):
            raise invalid_response() from None
    instructions = required_path(payload, 'data', 'search_by_raw_query', 'list_search_timeline', 'timeline', 'instructions')
    if not isinstance(instructions, list):
        raise invalid_response()
    items, seen, cursor = [], set(), None

    def consume(item):
        nonlocal cursor
        if not isinstance(item, dict):
            raise invalid_response()
        if item.get('cursorType'):
            if not isinstance(item.get('value'), str) or not item['value']:
                raise invalid_response()
            if item['cursorType'] == 'Bottom':
                cursor = item['value']
            return
        if item.get('itemType') != 'TimelineTwitterList':
            raise invalid_response()
        listing = required_path(item, 'list')
        if not isinstance(listing, dict):
            raise invalid_response()
        uid = listing.get('id_str') or listing.get('id')
        if not numeric_id(uid) or not isinstance(listing.get('name'), str) or not listing['name']:
            raise invalid_response()
        row = {'list_id': uid, 'name': listing['name'], 'url': 'https://x.com/i/lists/' + uid}
        for key in ('description', 'member_count', 'subscriber_count'):
            if key in listing:
                value = listing[key]
                if (key == 'description' and not isinstance(value, str)
                        or key != 'description' and (type(value) is not int or value < 0)):
                    raise invalid_response()
                row[key] = value
        if uid not in seen:
            seen.add(uid)
            items.append(row)

    for instruction in instructions:
        if not isinstance(instruction, dict):
            raise invalid_response()
        kind = instruction.get('type')
        if kind == 'TimelineAddEntries':
            rows = required_path(instruction, 'entries')
        elif kind in ('TimelineReplaceEntry', 'TimelinePinEntry'):
            rows = [required_path(instruction, 'entry')]
        elif kind == 'TimelineAddToModule':
            rows = [{'content': {'items': required_path(instruction, 'moduleItems')}}]
        elif kind in ('TimelineClearCache', 'TimelineTerminateTimeline'):
            continue
        else:
            raise invalid_response()
        if not isinstance(rows, list):
            raise invalid_response()
        for row in rows:
            content = required_path(row, 'content')
            if not isinstance(content, dict):
                raise invalid_response()
            if content.get('cursorType'):
                consume(content)
            elif 'itemContent' in content:
                consume(content['itemContent'])
            elif not isinstance(content.get('items'), list):
                raise invalid_response()
            if 'items' in content:
                if not isinstance(content['items'], list):
                    raise invalid_response()
                for child in content['items']:
                    consume(required_path(child, 'item', 'itemContent'))
    if errors and not items:
        raise graphql_error(errors)
    return {'items': items, 'next_cursor': cursor, 'partial': bool(errors),
            'error_codes': [214] if errors else [],
            'warnings': [{'field': 'default_banner_media', 'code': 214}] if errors else []}


def validate_user(user, *, viewer=False):
    if user is None or isinstance(user, dict) and user.get('__typename') in (
            'UserUnavailable', 'UserTombstone'):
        if viewer:
            raise XClientError('auth_required', '当前会话身份不可用，请重新认证')
        raise XClientError('user_unavailable', '用户不存在或不可访问')
    if not isinstance(user, dict) or not numeric_id(user.get('rest_id')):
        raise invalid_response()
    core = user.get('core') or user.get('legacy')
    if (not isinstance(core, dict) or not isinstance(core.get('screen_name'), str)
            or not core['screen_name'].strip()):
        raise invalid_response()
    for key in ('profile_bio', 'relationship_counts'):
        if key in user and user[key] is not None and not isinstance(user[key], dict):
            raise invalid_response()
    return user


def validated_profile_record(profile):
    """Preserve optional nulls, but never pass an upstream type change through."""
    for field in ('user_id', 'username'):
        if not isinstance(profile.get(field), str):
            raise invalid_response()
    for field in ('name', 'description'):
        if profile.get(field) is not None and not isinstance(profile[field], str):
            raise invalid_response()
    followers = profile.get('followers')
    if followers is not None and (type(followers) is not int or followers < 0):
        raise invalid_response()
    return profile


def normalized_post(result):
    """Check the public record after applying the existing parser's defaults."""
    try:
        post = handle_work_info(result)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise invalid_response() from None
    for field in ('work_id', 'work_url', 'work_type', 'title', 'create_time', 'lang',
                  'user_id', 'nickname', 'screen_name', 'user_url'):
        if not isinstance(post.get(field), str):
            raise invalid_response()
    for field in ('like_count', 'retweet_count', 'reply_count', 'quote_count', 'bookmark_count'):
        value = post.get(field)
        if type(value) is not int or value < 0:
            raise invalid_response()
    if type(post.get('view_count')) not in (str, int):
        raise invalid_response()
    for field in ('image_urls', 'video_urls'):
        urls = post.get(field)
        if not isinstance(urls, list) or any(not isinstance(url, str) for url in urls):
            raise invalid_response()
    return post


def _tweet(result):
    if not isinstance(result, dict):
        raise invalid_response()
    if result.get('__typename') == 'TweetWithVisibilityResults':
        result = required_path(result, 'tweet')
    if not isinstance(result, dict):
        raise invalid_response()
    if result.get('__typename') in ('TweetUnavailable', 'TweetTombstone'):
        return None
    legacy = result.get('legacy')
    if (not isinstance(legacy, dict)
            or not numeric_id(result.get('rest_id') or legacy.get('id_str'))
            or not isinstance(legacy.get('full_text'), str)
            or not isinstance(legacy.get('created_at'), str)
            or not legacy['created_at']):
        raise invalid_response()
    user = required_path(result, 'core', 'user_results', 'result')
    # A tweet that cannot be normalized must not become a blank author record.
    try:
        validate_user(user)
    except XClientError:
        raise invalid_response() from None
    if not user.get('core'):
        user = {**user, 'core': user['legacy']}
        result = {**result, 'core': {**result['core'], 'user_results': {'result': user}}}
    return result


def is_relevance_prompt(content):
    """Recognize the search feedback row without following its callback URLs."""
    if not isinstance(content, dict) or content.get('itemType') != 'TimelinePrompt':
        return False
    prompt = required_path(content, 'content')
    if (not isinstance(prompt, dict) or prompt.get('contentType') != 'TimelineRelevancePrompt'
            or not isinstance(prompt.get('title'), str) or not prompt['title']):
        raise invalid_response()
    return True


def validated_timeline(payload, kind):
    """Return only this operation's validated tweets/cursors, including modules.

    The canonical entries keep the existing flattening helpers usable without
    scanning unrelated response metadata or silently dropping conversation rows.
    """
    if kind == 'user':
        user = required_path(payload, 'data', 'user', 'result')
        if user is None or isinstance(user, dict) and user.get('__typename') in (
                'UserUnavailable', 'UserTombstone'):
            raise XClientError('user_unavailable', '用户不存在或不可访问')
        if not isinstance(user, dict):
            raise invalid_response()
        timeline = user.get('timeline_v2', user.get('timeline'))
        if isinstance(timeline, dict):
            timeline = timeline.get('timeline', timeline)
    else:
        paths = {
            'search': ('data', 'search_by_raw_query', 'search_timeline', 'timeline'),
            'detail': ('data', 'threaded_conversation_with_injections_v2'),
            'home': ('data', 'home', 'home_timeline_urt'),
        }
        timeline = required_path(payload, *paths[kind])
    instructions = required_path(timeline, 'instructions')
    if not isinstance(instructions, list):
        raise invalid_response()
    entries = []

    def item(content):
        if not isinstance(content, dict):
            raise invalid_response()
        if content.get('promotedMetadata'):
            return
        if kind == 'search' and is_relevance_prompt(content):
            return
        if kind == 'user' and content.get('itemType') == 'TimelineUser':
            # User timelines include a typed "who to follow" module alongside
            # tweet/conversation rows. Validate this known non-post shape before
            # omitting it; unknown item types still fail below.
            validate_user(required_path(content, 'user_results', 'result'))
            return
        if content.get('cursorType'):
            if not isinstance(content.get('value'), str) or not content['value']:
                raise invalid_response()
            entries.append({'entryId': 'cursor-' + str(len(entries)), 'content': content})
            return
        if 'tweet_results' not in content and content.get('itemType') != 'TimelineTweet':
            raise invalid_response()
        tweet = _tweet(required_path(content, 'tweet_results', 'result'))
        if tweet is not None:
            tweet_id = tweet.get('rest_id') or tweet['legacy']['id_str']
            entries.append({'entryId': 'tweet-' + tweet_id,
                            'content': {'itemContent': {'tweet_results': {'result': tweet}}}})

    for instruction in instructions:
        if not isinstance(instruction, dict):
            raise invalid_response()
        instruction_type = instruction.get('type')
        if instruction_type == 'TimelineAddEntries':
            rows = required_path(instruction, 'entries')
        elif instruction_type in ('TimelinePinEntry', 'TimelineReplaceEntry'):
            rows = [required_path(instruction, 'entry')]
        elif instruction_type == 'TimelineAddToModule':
            rows = [{'entryId': required_path(instruction, 'moduleEntryId'),
                     'content': {'items': required_path(instruction, 'moduleItems')}}]
        elif instruction_type in ('TimelineClearCache', 'TimelineTerminateTimeline', 'TimelineShowAlert'):
            continue
        else:
            raise invalid_response()
        if not isinstance(rows, list):
            raise invalid_response()
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('entryId'), str):
                raise invalid_response()
            content = required_path(row, 'content')
            if not isinstance(content, dict):
                raise invalid_response()
            if content.get('promotedMetadata'):
                continue
            if content.get('cursorType'):
                item(content)
                continue
            if 'itemContent' in content:
                item(content['itemContent'])
            elif 'items' not in content:
                raise invalid_response()
            children = content.get('items', [])
            if not isinstance(children, list):
                raise invalid_response()
            for child in children:
                item(required_path(child, 'item', 'itemContent'))
    return {'instructions': [{'type': 'TimelineAddEntries', 'entries': entries}]}
