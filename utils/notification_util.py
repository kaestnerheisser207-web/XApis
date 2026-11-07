"""解析当前网页 NotificationsTimeline 的通知、推文条目和分页游标。"""

from datetime import datetime

from utils.data_util import handle_work_info
from x_apis.errors import GraphQLError


def _user_summary(user):
    core = user.get('core') or user.get('legacy') or user
    return {'user_id': user.get('rest_id') or user.get('id_str'),
            'screen_name': core.get('screen_name'), 'name': core.get('name')}


def _tweet_summary(result):
    if result.get('__typename') == 'TweetWithVisibilityResults':
        result = result.get('tweet') or {}
    if result.get('__typename') != 'Tweet':
        return None
    work = handle_work_info(result)
    return {key: work[key] for key in ('work_id', 'work_url', 'title', 'create_time',
                                      'screen_name', 'user_id')}


def _timestamp(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int) or isinstance(value, str) and value.isdigit():
        return int(value)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() * 1000)
        except ValueError:
            return None
    return None


def _entries(instructions):
    for instruction in instructions:
        if not isinstance(instruction, dict):
            raise ValueError('通知 timeline instruction 不是对象')
        for entry in instruction.get('entries') or []:
            yield entry
        if isinstance(instruction.get('entry'), dict):
            yield instruction['entry']


def parse_notification_page(response: dict) -> dict:
    """保留通知条目顺序，推文型回复/@通知不能只从 notification 对象中统计。

    不请求用户或推文详情，不更新已读状态。未知通知格式以 item_type 留在结果中，
    字段缺失或服务端分类错误不能静默转换成空列表。
    """
    if not isinstance(response, dict):
        raise ValueError('通知响应不是对象')
    data = response.get('data') or {}
    viewer = data.get('viewer_v2') or {}
    results = viewer.get('user_results') or {}
    user = results.get('result') or {}
    notification_timeline = user.get('notification_timeline') or {}
    timeline = notification_timeline.get('timeline')
    if not isinstance(timeline, dict) or not isinstance(timeline.get('instructions'), list):
        if response.get('errors'):
            raise GraphQLError('NotificationsTimeline', response['errors'], response)
        raise ValueError('通知响应缺少 notification_timeline.timeline.instructions')

    items, seen, cursor = [], set(), None
    for entry in _entries(timeline['instructions']):
        if not isinstance(entry, dict):
            raise ValueError('通知 timeline entry 不是对象')
        entry_id = entry.get('entryId')
        if not isinstance(entry_id, str) or not entry_id:
            raise ValueError('通知 entry 缺少 entryId')
        content = entry.get('content') or {}
        if not isinstance(content, dict):
            raise ValueError('通知 entry.content 不是对象')
        if content.get('cursorType') == 'Bottom':
            cursor = content.get('value')
        if content.get('cursorType') or str(entry_id).startswith('cursor-'):
            continue
        if entry_id in seen:
            continue
        seen.add(entry_id)
        item = content.get('itemContent') or {}
        if not isinstance(item, dict):
            raise ValueError('通知 itemContent 不是对象')
        event = content.get('clientEventInfo') or {}
        item_type = item.get('itemType') or item.get('__typename') or 'Unknown'
        row = {'entry_id': entry_id, 'kind': event.get('element') or item_type,
               'item_type': item_type,
               'timestamp_ms': _timestamp(item.get('timestamp_ms') or entry.get('sortIndex')),
               'text': None, 'actors': [], 'tweets': [], 'url': None}
        if item_type == 'TimelineTweet':
            tweet = (item.get('tweet_results') or {}).get('result')
            if isinstance(tweet, dict):
                summary = _tweet_summary(tweet)
                if summary is None:
                    row['status'] = 'tweet_unavailable'
                    row['unavailable_type'] = tweet.get('__typename')
                else:
                    row['text'], row['url'] = summary['title'], summary['work_url']
                    row['tweets'].append(summary)
                result = tweet.get('tweet', tweet)
                author = ((result.get('core') or {}).get('user_results') or {}).get('result')
                if isinstance(author, dict):
                    row['actors'].append(_user_summary(author))
        elif item_type == 'TimelineNotification':
            row['notification_id'] = item.get('id')
            row['icon'] = item.get('notification_icon')
            row['text'] = (item.get('rich_message') or {}).get('text')
            row['url'] = (item.get('notification_url') or {}).get('url')
            template = item.get('template') or {}
            row['template_type'] = template.get('__typename')
            for actor in template.get('from_users') or []:
                user = (actor.get('user_results') or {}).get('result')
                if isinstance(user, dict):
                    row['actors'].append(_user_summary(user))
            for target in template.get('target_objects') or []:
                tweet = (target.get('tweet_results') or {}).get('result')
                if isinstance(tweet, dict):
                    summary = _tweet_summary(tweet)
                    if summary is not None:
                        row['tweets'].append(summary)
        items.append(row)
    return {'items': items, 'next_cursor': cursor,
            'error_codes': [error.get('code') for error in response.get('errors', [])]}
