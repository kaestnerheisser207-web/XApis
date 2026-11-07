"""Current list-search routing and account-scoped subscriber notification gates."""
from contextlib import redirect_stdout
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from api_watch.probes import _failure, _Issue
from api_watch.read_probes import _search
from x_apis import XClient, XClientError
from x_apis.errors import GraphQLError
from x_apis.x_api import XAPI


def list_page(*, warning=False):
    listing = {'id_str': '101', 'name': 'Science', 'description': 'Public research',
               'member_count': 3, 'subscriber_count': 7, 'default_banner_media_results': {'result': None}}
    data = {'data': {'search_by_raw_query': {'list_search_timeline': {'timeline': {'instructions': [
        {'type': 'TimelineAddEntries', 'entries': [
            {'entryId': 'list-module', 'content': {'items': [
                {'entryId': 'list-101', 'item': {'itemContent': {'itemType': 'TimelineTwitterList', 'list': listing}}}]}},
            {'entryId': 'cursor-bottom', 'content': {'cursorType': 'Bottom', 'value': 'opaque:+/%='}},
        ]},
    ]}}}}}
    if warning:
        data['errors'] = [{'code': 214, 'name': 'BadRequestError', 'path': [
            'search_by_raw_query', 'list_search_timeline', 'timeline', 'instructions', 0,
            'entries', 0, 'content', 'items', 0, 'item', 'itemContent', 'list',
            'default_banner_media_results', 'result']}]
    return data


def shell(*, enabled=True, active=True, uid='123', override=None):
    feature = {'defaultConfig': {'responsive_web_subscribers_ntab_for_creators_enabled': {'value': enabled}}}
    if override is not None:
        feature['user'] = {'config': {'responsive_web_subscribers_ntab_for_creators_enabled': {'value': override}}}
    return 'window.__INITIAL_STATE__=' + json.dumps({
        'session': {'user_id': uid, 'isActiveCreator': active}, 'featureSwitch': feature,
    }, separators=(',', ':')) + ';'


class ListSearchTests(unittest.TestCase):
    def test_dedicated_get_omits_unsupported_generic_variables_and_forwards_cursor(self):
        auth = object()
        with patch('x_apis.x_api.graphql_get', return_value=list_page()) as get, \
                patch('x_apis.x_api.graphql_post_query') as post:
            XAPI.search_work(auth, 'OpenAI', product='Lists', count=5, cursor='opaque:+/%=')
        get.assert_called_once_with(auth, 'ListSearchTimeline',
            {'rawQuery': 'OpenAI', 'count': 5, 'cursor': 'opaque:+/%='},
            referer='https://x.com/i/lists/search?q=OpenAI')
        post.assert_not_called()

    def test_other_search_products_keep_the_existing_transport(self):
        with patch('x_apis.x_api.graphql_get') as get, patch('x_apis.x_api.graphql_post_query') as post:
            XAPI.search_work(object(), 'OpenAI', product='Latest')
        get.assert_not_called()
        self.assertEqual(post.call_args.args[1], 'SearchTimeline')
        self.assertEqual(post.call_args.args[2]['querySource'], 'typed_query')

    def test_sdk_returns_lists_and_preserves_explicit_optional_banner_warning(self):
        client = XClient(object())
        for warning in (False, True):
            with self.subTest(warning=warning), patch('x_apis.client.XAPI.search_work', return_value=list_page(warning=warning)):
                page = client.lists.search('OpenAI')
            self.assertEqual(page['items'][0]['list_id'], '101')
            self.assertEqual(page['items'][0]['member_count'], 3)
            self.assertEqual(page['next_cursor'], 'opaque:+/%=')
            self.assertEqual(page['partial'], warning)
            self.assertEqual(page['error_codes'], [214] if warning else [])

    def test_monitor_does_not_turn_partial_banner_failures_into_full_success(self):
        with self.assertRaises(_Issue) as caught:
            _search(list_page(warning=True), 'Lists')
        self.assertEqual(caught.exception.status, 'incomplete')
        self.assertEqual(caught.exception.details['item_count'], 1)
        self.assertEqual(caught.exception.details['reason'], 'optional_list_banner_errors')

    def test_observed_empty_banner_results_are_optional_but_missing_parents_are_not(self):
        raw = list_page(warning=True)
        listing = raw['data']['search_by_raw_query']['list_search_timeline']['timeline']['instructions'][0]['entries'][0]['content']['items'][0]['item']['itemContent']['list']
        listing['default_banner_media_results'] = {}
        client = XClient(object())
        with patch('x_apis.client.XAPI.search_work', return_value=raw):
            self.assertTrue(client.lists.search('OpenAI')['partial'])
        del listing['default_banner_media_results']
        with patch('x_apis.client.XAPI.search_work', return_value=raw):
            with self.assertRaises(XClientError) as caught:
                client.lists.search('OpenAI')
        self.assertEqual(caught.exception.code, 'invalid_response')

    def test_non_banner_errors_and_malformed_core_records_are_not_downgraded(self):
        client = XClient(object())
        for mutation, expected in (
            (lambda d: d['errors'][0].update(code=32), 'auth_required'),
            (lambda d: d['errors'][0]['path'].__setitem__(-1, 'name'), 'upstream_error'),
            (lambda d: d['data']['search_by_raw_query']['list_search_timeline']['timeline']['instructions'][0]['entries'][0]['content']['items'][0]['item']['itemContent']['list'].update(name=None), 'invalid_response'),
            (lambda d: d['errors'][0]['path'].__setitem__(9, 99), 'invalid_response'),
        ):
            raw = list_page(warning=True)
            mutation(raw)
            with self.subTest(expected=expected), patch('x_apis.client.XAPI.search_work', return_value=raw):
                with self.assertRaises(XClientError) as caught:
                    client.lists.search('OpenAI')
            self.assertEqual(caught.exception.code, expected)

    def test_cli_displays_list_results_and_warning_instead_of_zero_tweets(self):
        import main
        output = io.StringIO()
        with patch('main.XAPI.search_work', return_value=list_page(warning=True)), redirect_stdout(output):
            self.assertEqual(main.cmd_search(object(), SimpleNamespace(query='OpenAI', product='Lists')), 0)
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed['items'][0]['name'], 'Science')
        self.assertTrue(parsed['partial'])


class SubscriberAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.auth = SimpleNamespace(is_logged_in=True, proxies=None, user_id='123')

    def test_unavailable_accounts_fail_before_graphql_with_an_explicit_reason(self):
        for enabled, active, reason in ((False, True, 'feature_disabled'), (True, False, 'creator_required')):
            with self.subTest(reason=reason), patch('utils.graphql_registry._fetch_home_shell', return_value=shell(enabled=enabled, active=active)), \
                    patch('x_apis.x_api.graphql_get') as get:
                with self.assertRaises(XClientError) as caught:
                    XClient(self.auth).notifications.list(category='SuperFollowers')
            self.assertEqual(caught.exception.code, 'capability_unavailable')
            self.assertEqual(caught.exception.result['reason'], reason)
            self.assertEqual(_failure(caught.exception)[0], 'blocked')
            get.assert_not_called()

    def test_enabled_creator_uses_the_original_request_and_does_not_hide_server_errors(self):
        error = GraphQLError('NotificationsTimeline', [{'name': 'DependencyError'}])
        with patch('utils.graphql_registry._fetch_home_shell', return_value=shell()), \
                patch('x_apis.x_api.graphql_get', side_effect=error) as get:
            with self.assertRaises(GraphQLError) as caught:
                XAPI.get_notifications(self.auth, 'SuperFollowers', count=5, cursor='next')
        self.assertIs(caught.exception, error)
        self.assertEqual(get.call_args.args[2], {'timeline_type': 'SuperFollowers', 'count': 5, 'cursor': 'next'})

    def test_resolved_user_flag_overrides_default_and_is_rechecked(self):
        with patch('utils.graphql_registry._fetch_home_shell', side_effect=[shell(override=False), shell(override=True)]) as home, \
                patch('x_apis.x_api.graphql_get', return_value={'data': {}}) as get:
            with self.assertRaises(XClientError):
                XAPI.get_notifications(self.auth, 'SuperFollowers')
            XAPI.get_notifications(self.auth, 'SuperFollowers')
        self.assertEqual(home.call_count, 2)
        get.assert_called_once()

    def test_unknown_state_and_expired_identity_are_not_mislabeled_unavailable(self):
        for html, code in ((shell(enabled=None), 'invalid_response'),
                           (shell(uid=None), 'auth_required'),
                           (shell(uid='456'), 'invalid_response'),
                           ('window.__INITIAL_STATE__={};', 'invalid_response')):
            with self.subTest(code=code), patch('utils.graphql_registry._fetch_home_shell', return_value=html):
                with self.assertRaises(XClientError) as caught:
                    XAPI.get_notifications(self.auth, 'SuperFollowers')
            self.assertEqual(caught.exception.code, code)

    def test_normal_categories_do_not_fetch_capability_state(self):
        with patch('utils.graphql_registry._fetch_home_shell') as home, patch('x_apis.x_api.graphql_get'):
            XAPI.get_notifications(self.auth, 'All')
        home.assert_not_called()

    def test_cli_exposes_unavailable_without_claiming_an_empty_success(self):
        import main
        error = XClientError('capability_unavailable', '当前账号未开放订阅者通知',
                             result={'category': 'SuperFollowers', 'reason': 'creator_required', 'available': False})
        output = io.StringIO()
        with patch('main.XAPI.get_notifications', side_effect=error), redirect_stdout(output):
            code = main.cmd_notifications(object(), SimpleNamespace(type='SuperFollowers', count=5, cursor=None))
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())['status'], 'unavailable')
