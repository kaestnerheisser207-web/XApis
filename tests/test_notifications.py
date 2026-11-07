"""Notification request and parsing regressions; all requests are mocked."""

import unittest
from unittest.mock import patch
from types import SimpleNamespace
from contextlib import redirect_stdout
import io
import json

from x_apis.x_api import NOTIFICATION_TYPES, XAPI
from x_apis.errors import GraphQLError
from utils.notification_util import parse_notification_page


def response(entries=(), errors=(), timeline=True):
    value = {'instructions': [{'type': 'TimelineAddEntries', 'entries': list(entries)}]} if timeline else None
    return {'data': {'viewer_v2': {'user_results': {'result': {
        'notification_timeline': {'timeline': value}}}}}, 'errors': list(errors)}


def user(uid, name):
    return {'rest_id': uid, 'core': {'screen_name': name, 'name': name}}


def tweet_entry(entry_id='notification-reply'):
    return {'entryId': entry_id, 'sortIndex': '1000', 'content': {
        'clientEventInfo': {'element': 'user_replied_to_your_tweet'},
        'itemContent': {'itemType': 'TimelineTweet', 'tweet_results': {'result': {
            '__typename': 'Tweet', 'rest_id': '100',
            'core': {'user_results': {'result': user('2', 'reader')}},
            'legacy': {'full_text': '@owner reply', 'created_at': 'Thu Oct 01 13:00:00 +0000 2026'}}}}}}


class NotificationRequestTests(unittest.TestCase):
    def test_all_frontend_types_use_current_graphql_variables(self):
        auth = object()
        for kind in NOTIFICATION_TYPES:
            with self.subTest(kind=kind), patch('x_apis.x_api._require_subscriber_notifications'), \
                    patch('x_apis.x_api.graphql_get', return_value={'data': {}}) as get:
                XAPI.get_notifications(auth, kind, 25, 'next-page')
                get.assert_called_once_with(auth, 'NotificationsTimeline',
                    {'timeline_type': kind, 'count': 25, 'cursor': 'next-page'},
                    referer='https://x.com/notifications')

    def test_first_page_omits_cursor(self):
        with patch('x_apis.x_api.graphql_get') as get:
            XAPI.get_notifications(object())
            self.assertEqual(get.call_args.args[2], {'timeline_type': 'All', 'count': 20})

    def test_bad_types_counts_and_cursors_fail_before_network(self):
        for arguments in ({'type': 'Unknown'}, {'count': 0}, {'count': 101},
                          {'count': True}, {'count': '20'}, {'cursor': ''}, {'cursor': 42}):
            with self.subTest(arguments=arguments), patch('x_apis.x_api.graphql_get') as get:
                with self.assertRaises(ValueError):
                    XAPI.get_notifications(object(), **arguments)
                get.assert_not_called()


class NotificationParsingTests(unittest.TestCase):
    def test_reply_tweets_are_counted_even_without_notification_objects(self):
        page = parse_notification_page(response([tweet_entry()]))
        item = page['items'][0]
        self.assertEqual(item['kind'], 'user_replied_to_your_tweet')
        self.assertEqual(item['text'], '@owner reply')
        self.assertEqual(item['actors'][0]['screen_name'], 'reader')
        self.assertEqual(item['url'], 'https://x.com/reader/status/100')

    def test_aggregate_keeps_all_actors_and_iso_timestamp(self):
        item = {'itemType': 'TimelineNotification', 'id': 'group-follow',
                'notification_icon': 'person_icon',
                'rich_message': {'text': 'one and two followed you'},
                'timestamp_ms': '2026-01-01T00:00:00.000Z',
                'template': {'__typename': 'TimelineNotificationAggregateUserActions',
                    'from_users': [{'user_results': {'result': user('1', 'one')}},
                                   {'user_results': {'result': user('2', 'two')}}]}}
        entry = {'entryId': 'notification-follow', 'content': {'itemContent': item}}
        parsed = parse_notification_page(response([entry]))['items'][0]
        self.assertEqual([u['screen_name'] for u in parsed['actors']], ['one', 'two'])
        self.assertEqual(parsed['timestamp_ms'], 1767225600000)
        self.assertEqual(parsed['text'], 'one and two followed you')

    def test_cursors_excluded_and_bottom_returned(self):
        entries = [{'entryId': 'cursor-top', 'content': {'cursorType': 'Top', 'value': 'top'}},
                   tweet_entry(), {'entryId': 'cursor-bottom', 'content': {'cursorType': 'Bottom', 'value': 'bottom'}}]
        page = parse_notification_page(response(entries))
        self.assertEqual(len(page['items']), 1)
        self.assertEqual(page['next_cursor'], 'bottom')

    def test_duplicate_entry_not_counted_twice(self):
        self.assertEqual(len(parse_notification_page(response([tweet_entry(), tweet_entry()]))['items']), 1)

    def test_empty_timeline_is_success(self):
        self.assertEqual(parse_notification_page(response())['items'], [])

    def test_partial_error_with_missing_timeline_is_not_empty_success(self):
        with self.assertRaises(GraphQLError):
            parse_notification_page(response(errors=[{'name': 'DependencyError', 'message': 'Dependency'}], timeline=False))

    def test_partial_data_errors_remain_visible(self):
        page = parse_notification_page(response([tweet_entry()], errors=[{'code': 34, 'message': 'Other item unavailable'}]))
        self.assertEqual(len(page['items']), 1)
        self.assertEqual(page['error_codes'], [34])

    def test_malformed_response_is_not_empty_success(self):
        for data in ({}, [], {'data': {}}, response([{'content': {}}])):
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_notification_page(data)

    def test_visibility_wrapper_and_long_text_are_preserved(self):
        entry = tweet_entry(); result = entry['content']['itemContent']['tweet_results']['result']
        result['note_tweet'] = {'note_tweet_results': {'result': {'text': 'Complete long reply'}}}
        entry['content']['itemContent']['tweet_results']['result'] = {'__typename': 'TweetWithVisibilityResults', 'tweet': result}
        self.assertEqual(parse_notification_page(response([entry]))['items'][0]['text'], 'Complete long reply')

    def test_unavailable_tweet_not_given_a_fake_url(self):
        entry = tweet_entry(); entry['content']['itemContent']['tweet_results']['result'] = {'__typename': 'TweetUnavailable'}
        item = parse_notification_page(response([entry]))['items'][0]
        self.assertEqual(item['status'], 'tweet_unavailable')
        self.assertIsNone(item['url'])

    def test_unknown_template_retains_row(self):
        entry = {'entryId': 'notification-new', 'content': {'itemContent': {'itemType': 'NewNotification'}}}
        item = parse_notification_page(response([entry]))['items'][0]
        self.assertEqual(item['item_type'], 'NewNotification')


class NotificationCliTests(unittest.TestCase):
    def test_cli_outputs_parsed_reply_and_cursor_without_write(self):
        import main
        args = main.build_parser().parse_args(['notifications', '--type', 'Mentions', '--count', '10'])
        out = io.StringIO()
        with patch('main.XAPI.get_notifications', return_value=response([tweet_entry()])) as get, redirect_stdout(out):
            self.assertEqual(main.cmd_notifications(object(), args), 0)
        self.assertEqual(get.call_args.args[1], 'Mentions')
        self.assertEqual(json.loads(out.getvalue())['items'][0]['text'], '@owner reply')

    def test_dependency_error_is_reported_not_a_zero_count(self):
        import main
        args = SimpleNamespace(type='SuperFollowers', count=20, cursor=None)
        out = io.StringIO()
        data = response(errors=[{'name': 'DependencyError', 'message': 'Dependency'}], timeline=False)
        with patch('main.XAPI.get_notifications', return_value=data), redirect_stdout(out):
            self.assertEqual(main.cmd_notifications(object(), args), 1)
        self.assertEqual(json.loads(out.getvalue())['status'], 'error')


if __name__ == '__main__':
    unittest.main()
