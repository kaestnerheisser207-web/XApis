"""Offline canaries: real read wrappers/validators, no X or credential files."""
import copy
import json
import threading
import unittest
from unittest.mock import Mock, patch

import requests

from api_watch import probes
from builder.auth import XAuth
from x_apis.errors import GraphQLError


_REAL_NETWORK_TRANSACTION = probes._network_transaction
COOKIES = 'auth_token=PRIVATE_COOKIE; ct0=PRIVATE_CSRF'


def user_result():
    return {'__typename': 'User', 'rest_id': '101',
            'core': {'screen_name': 'fixture_user', 'name': 'PRIVATE_NAME'}}


def tweet_result():
    return {'__typename': 'Tweet', 'rest_id': '202',
            'core': {'user_results': {'result': user_result()}},
            'legacy': {'full_text': 'PRIVATE_POST', 'created_at': 'Thu Oct 01 12:00:00 +0000 2026'}}


def entry(entry_id='tweet-202'):
    return {'entryId': entry_id, 'content': {'itemContent': {
        'itemType': 'TimelineTweet', 'tweet_results': {'result': tweet_result()}}}}


def timeline(entries=(), *, cursor=None, kind='user'):
    values = list(entries)
    if cursor:
        values.append({'entryId': 'cursor-bottom', 'content': {'cursorType': 'Bottom', 'value': cursor}})
    body = {'instructions': [{'type': 'TimelineAddEntries', 'entries': values}]}
    if kind == 'search':
        return {'data': {'search_by_raw_query': {'search_timeline': {'timeline': body}}}}
    if kind == 'detail':
        return {'data': {'threaded_conversation_with_injections_v2': body}}
    return {'data': {'user': {'result': {'timeline_v2': {'timeline': body}}}}}


def notifications():
    return {'data': {'viewer_v2': {'user_results': {'result': {
        'notification_timeline': {'timeline': {'instructions': []}}}}}}}


def response(raw, status=200):
    result = Mock(status_code=status)
    result.json.return_value = copy.deepcopy(raw)
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError(
            'PRIVATE_PROXY https://x.com/authenticated?token=PRIVATE_TOKEN', response=result)
    return result


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.material = Mock()
        self.material.generate.return_value = 'fixture-transaction'
        self.network_material = self.enterContext(patch(
            'api_watch.probes._network_transaction', return_value=self.material))
        self.no_raw_network = self.enterContext(patch(
            'api_watch.probes.requests.get', side_effect=AssertionError('network prohibited')))
        self.no_session_network = self.enterContext(patch(
            'api_watch.probes.requests.sessions.Session.request', side_effect=AssertionError('network prohibited')))
        self.payloads = {
            'Viewer': {'data': {'viewer': {'user_results': {'result': user_result()}}}},
            'UserByScreenName': {'data': {'user': {'result': user_result()}}},
            'UserOriginalsTimeline': timeline([entry()]),
            'SearchTimeline': timeline([], kind='search'),
            'TweetDetail': timeline([entry()], kind='detail'),
            'TweetResultByRestId': {'data': {'tweetResult': {'result': tweet_result()}}},
            'NotificationsTimeline': notifications(),
        }
        self.transport_get = self.enterContext(patch('builder.client.get', side_effect=self.send))
        self.transport_post = self.enterContext(patch('builder.client.post', side_effect=self.send))
        self.guest_query = self.enterContext(patch('api_watch.probes._GuestSource._query', side_effect=self.guest))

    def send(self, url, **kwargs):
        value = self.payloads[url.rsplit('/', 1)[-1]]
        if isinstance(value, Exception):
            raise value
        return value if hasattr(value, 'raise_for_status') else response(value)

    def guest(self, operation, variables):
        if operation == 'UserByScreenName':
            return copy.deepcopy(self.payloads[operation])
        return copy.deepcopy(self.payloads['UserOriginalsTimeline'])

    def run_round(self, **config):
        value = {'guest': False, 'authenticated': True, 'request_gap_seconds': 0, **config}
        result = probes.run_probes(value, cookies=COOKIES)
        self.no_raw_network.assert_not_called()
        self.no_session_network.assert_not_called()
        self.assert_private(result)
        return {item['check_id']: item for item in result}

    def assert_private(self, result):
        text = json.dumps(result)
        for value in ('PRIVATE_COOKIE', 'PRIVATE_CSRF', 'PRIVATE_NAME', 'PRIVATE_POST',
                      'PRIVATE_PROXY', 'PRIVATE_TOKEN', 'fixture_user', 'private-error-code',
                      'PRIVATE_CURSOR'):
            self.assertNotIn(value, text)

    def test_read_only_wrappers_and_post_search_work_without_login(self):
        result = self.run_round(guest=True, post_id='202')
        for check_id in ('guest.user', 'guest.timeline', 'auth.viewer', 'auth.user',
                         'auth.timeline', 'auth.search', 'auth.notifications',
                         'auth.post_detail', 'auth.post_result'):
            self.assertEqual(result[check_id]['status'], 'ok', check_id)
        self.assertEqual(self.transport_post.call_count, 1)
        self.assertTrue(self.transport_post.call_args.args[0].endswith('/SearchTimeline'))
        self.assertEqual([call.args[1] for call in self.network_material.call_args_list], ['guest', 'auth'])
        self.assertNotIn('auth_token', self.network_material.call_args_list[0].args[0].cookie)
        self.assertIn('auth_token', self.network_material.call_args_list[1].args[0].cookie)

    def test_missing_cookies_disables_account_without_network(self):
        result = probes.run_probes({'guest': False}, cookies='')
        self.assertTrue(all(item['status'] == 'disabled' for item in result))
        self.network_material.assert_not_called()
        self.transport_get.assert_not_called()
        self.transport_post.assert_not_called()

    def test_incomplete_cookie_is_not_replaced_or_auto_logged_in(self):
        result = probes.run_probes({'guest': False}, cookies='auth_token=PRIVATE_COOKIE')
        self.assertEqual(next(r for r in result if r['check_id'] == 'auth.viewer')['status'], 'auth_expired')
        self.network_material.assert_not_called()
        self.transport_get.assert_not_called()
        self.assert_private(result)

    def test_http_200_partial_auth_error_blocks_every_account_read(self):
        self.payloads['Viewer'] = {'data': {'viewer': {}}, 'errors': [
            {'code': 32, 'message': 'PRIVATE_COOKIE PRIVATE_PROXY'}]}
        result = self.run_round()
        self.assertEqual(result['auth.viewer']['status'], 'auth_expired')
        self.assertEqual(result['auth.viewer']['details']['error_codes'], ['32'])
        self.assertEqual(result['auth.search']['status'], 'blocked')
        self.assertEqual(self.transport_get.call_count, 1)
        self.transport_post.assert_not_called()

    def test_viewer_missing_identity_is_contract_error_not_authenticated_success(self):
        self.payloads['Viewer'] = {'data': {'viewer': {}}}
        result = self.run_round()
        self.assertEqual(result['auth.viewer']['status'], 'contract_error')
        self.assertEqual(result['auth.user']['status'], 'blocked')

    def test_viewer_identity_mismatch_blocks_account_without_persisting_ids(self):
        result = probes.run_probes({'guest': False, 'request_gap_seconds': 0},
                                  cookies=COOKIES + '; twid=u%3D909090')
        indexed = {item['check_id']: item for item in result}
        self.assertEqual(indexed['auth.viewer']['status'], 'incomplete')
        self.assertTrue(indexed['auth.viewer']['details']['identity_mismatch'])
        self.assertEqual(indexed['auth.user']['status'], 'blocked')
        self.assertEqual(indexed['auth.search']['status'], 'blocked')
        self.assertEqual(self.transport_get.call_count, 1)
        self.transport_post.assert_not_called()
        self.assertNotIn('909090', json.dumps(result))
        self.assert_private(result)

    def test_matching_or_unparseable_twid_does_not_override_viewer(self):
        for twid in ('u%3D101', 'u%3d101', 'u=101', '"u%3D101"', 'not-an-identity'):
            with self.subTest(twid=twid):
                result = probes.run_probes({'guest': False, 'request_gap_seconds': 0},
                                          cookies=COOKIES + '; twid=' + twid)
                viewer = next(item for item in result if item['check_id'] == 'auth.viewer')
                self.assertEqual(viewer['status'], 'ok')

    def test_graphql_partial_data_is_never_silently_successful(self):
        self.payloads['SearchTimeline']['errors'] = [
            {'code': 'private-error-code', 'message': 'PRIVATE_TOKEN'}, {'code': 999}]
        result = self.run_round()
        self.assertEqual(result['auth.search']['status'], 'incomplete')
        self.assertEqual(result['auth.search']['details']['error_codes'], ['999'])

    def test_http_rate_limit_blocks_later_account_requests(self):
        self.payloads['SearchTimeline'] = response({'errors': [{'code': 88}]}, 429)
        result = self.run_round(post_id='202')
        self.assertEqual(result['auth.search']['status'], 'rate_limited')
        self.assertEqual(result['auth.search']['details']['http_status'], 429)
        self.assertEqual(result['auth.post_detail']['status'], 'blocked')
        self.assertEqual(result['auth.notifications']['status'], 'blocked')

    def test_user_or_timeline_auth_and_rate_failures_stop_same_identity(self):
        for operation, status, check_id in (
                ('UserByScreenName', 401, 'auth.user'),
                ('UserOriginalsTimeline', 429, 'auth.timeline')):
            with self.subTest(operation=operation):
                original = self.payloads[operation]
                self.payloads[operation] = response({}, status)
                self.transport_post.reset_mock()
                result = self.run_round(post_id='202', pagination=True)
                self.assertEqual(result[check_id]['status'], 'auth_expired' if status == 401 else 'rate_limited')
                self.assertEqual(result['auth.search']['status'], 'blocked')
                self.assertEqual(result['auth.notifications']['status'], 'blocked')
                self.transport_post.assert_not_called()
                self.payloads[operation] = original

    def test_http_401_and_403_have_distinct_classifications(self):
        for status, expected in ((401, 'auth_expired'), (403, 'blocked')):
            with self.subTest(status=status):
                self.payloads['Viewer'] = response({}, status)
                result = self.run_round()
                self.assertEqual(result['auth.viewer']['status'], expected)

    def test_endpoint_404_is_contract_error_not_disappeared_sample(self):
        self.payloads['SearchTimeline'] = response({}, 404)
        result = self.run_round()
        self.assertEqual(result['auth.search']['status'], 'contract_error')

    def test_deleted_fixed_sample_is_not_classified_as_protocol_change(self):
        self.payloads['UserByScreenName'] = {'data': {'user': {'result': None}}}
        self.payloads['TweetResultByRestId'] = {'data': {'tweetResult': {'result': {'__typename': 'TweetTombstone'}}}}
        self.payloads['TweetDetail'] = timeline([], kind='detail')
        result = self.run_round(post_id='202')
        self.assertEqual(result['auth.user']['status'], 'sample_unavailable')
        self.assertEqual(result['auth.timeline']['status'], 'blocked')
        self.assertEqual(result['auth.post_result']['status'], 'sample_unavailable')
        self.assertEqual(result['auth.post_detail']['status'], 'sample_unavailable')
        self.assertEqual(result['auth.search']['status'], 'ok')

    def test_network_error_does_not_retain_exception_message(self):
        self.payloads['SearchTimeline'] = requests.ConnectionError('PRIVATE_PROXY PRIVATE_TOKEN')
        result = self.run_round()
        self.assertEqual(result['auth.search']['status'], 'network_error')

    def test_invalid_json_is_a_contract_error(self):
        malformed = response({})
        malformed.json.side_effect = requests.exceptions.JSONDecodeError('PRIVATE_TOKEN', '', 0)
        self.payloads['SearchTimeline'] = malformed
        result = self.run_round()
        self.assertEqual(result['auth.search']['status'], 'contract_error')

    def test_missing_timeline_is_not_accepted_as_empty(self):
        self.payloads['UserOriginalsTimeline'] = {'data': {'user': {'result': {}}}}
        result = self.run_round()
        self.assertEqual(result['auth.timeline']['status'], 'contract_error')
        self.assertEqual(result['auth.search']['status'], 'ok')
        self.assertEqual(result['auth.notifications']['status'], 'ok')

    def test_changed_tweet_fields_cannot_silently_flatten_to_empty_strings(self):
        changed = entry()
        del changed['content']['itemContent']['tweet_results']['result']['core']['user_results']['result']['core']['screen_name']
        self.payloads['UserOriginalsTimeline'] = timeline([changed])
        result = self.run_round()
        self.assertEqual(result['auth.timeline']['status'], 'contract_error')
        self.assertIn('tweet.author.core.screen_name', result['auth.timeline']['details']['fields'])

    def test_changed_entry_prefix_is_normalized_by_the_actual_sdk_parser(self):
        self.payloads['UserOriginalsTimeline'] = timeline([entry('new-entry-202')])
        result = self.run_round()
        self.assertEqual(result['auth.timeline']['status'], 'ok')
        self.assertEqual(result['auth.timeline']['details']['item_count'], 1)

    def test_module_tweets_are_counted_after_sdk_canonicalization(self):
        item = entry()['content']['itemContent']
        self.payloads['UserOriginalsTimeline'] = timeline([
            {'entryId': 'conversation-module', 'content': {'items': [{'item': {'itemContent': item}}]}}])
        result = self.run_round()
        self.assertEqual(result['auth.timeline']['status'], 'ok')
        self.assertEqual(result['auth.timeline']['details']['item_count'], 1)

    def test_notification_unread_controls_are_typed_and_not_executed(self):
        probes._instruction_shapes([
            {'type': 'TimelineClearEntriesUnreadState'},
            {'type': 'TimelineMarkEntriesUnreadGreaterThanSortIndex', 'sort_index': '123'},
        ])
        with self.assertRaises(probes._Issue):
            probes._instruction_shapes([
                {'type': 'TimelineMarkEntriesUnreadGreaterThanSortIndex', 'sort_index': None}])

    def test_unknown_nonempty_timeline_is_incomplete(self):
        self.payloads['UserOriginalsTimeline'] = timeline([
            {'entryId': 'future', 'content': {'futureItem': {}}}])
        result = self.run_round()
        self.assertEqual(result['auth.timeline']['status'], 'incomplete')

    def test_unknown_instruction_is_not_accepted_as_empty_timeline(self):
        self.payloads['UserOriginalsTimeline']['data']['user']['result']['timeline_v2']['timeline']['instructions'] = [
            {'type': 'FutureInstructionV2', 'newEntries': [entry()]}]
        result = self.run_round()
        self.assertEqual(result['auth.timeline']['status'], 'incomplete')

    def test_missing_entries_on_known_instruction_is_contract_error(self):
        self.payloads['UserOriginalsTimeline']['data']['user']['result']['timeline_v2']['timeline']['instructions'] = [
            {'type': 'TimelineAddEntries'}]
        result = self.run_round()
        self.assertEqual(result['auth.timeline']['status'], 'contract_error')

    def test_known_control_instructions_can_describe_empty_timeline(self):
        self.payloads['UserOriginalsTimeline']['data']['user']['result']['timeline_v2']['timeline']['instructions'] = [
            {'type': 'TimelineClearCache'}, {'type': 'TimelineAddEntries', 'entries': []},
            {'type': 'TimelineTerminateTimeline', 'direction': 'Bottom'}]
        result = self.run_round()
        self.assertEqual(result['auth.timeline']['status'], 'ok')
        self.assertEqual(result['auth.timeline']['details']['item_count'], 0)

    def test_unknown_notification_type_is_not_accepted_as_parsed_item(self):
        self.payloads['NotificationsTimeline']['data']['viewer_v2']['user_results']['result']['notification_timeline']['timeline']['instructions'] = [
            {'type': 'TimelineAddEntries', 'entries': [
                {'entryId': 'notification-1', 'content': {
                    'itemContent': {'itemType': 'FutureNotificationV2', 'secret': 'PRIVATE_TOKEN'}}}]}]
        result = self.run_round()
        self.assertEqual(result['auth.notifications']['status'], 'incomplete')

    def test_known_notification_type_without_usable_content_is_incomplete(self):
        for item in ({'itemType': 'TimelineNotification'},
                     {'itemType': 'TimelineNotification', 'id': 'fixture-notice'},
                     {'itemType': 'TimelineNotification', 'rich_message': {'text': 'PRIVATE_POST'}},
                     {'itemType': 'TimelineNotification', 'id': 'fixture-notice',
                      'template': {'from_users': [{'user_results': {'result': {}}}]}}):
            with self.subTest(fields=sorted(item)):
                self.payloads['NotificationsTimeline']['data']['viewer_v2']['user_results']['result']['notification_timeline']['timeline']['instructions'] = [
                    {'type': 'TimelineAddEntries', 'entries': [
                        {'entryId': 'notification-1', 'content': {'itemContent': item}}]}]
                result = self.run_round()
                self.assertEqual(result['auth.notifications']['status'], 'incomplete')

    def test_valid_notification_text_or_actor_template_still_passes(self):
        for content in ({'rich_message': {'text': 'PRIVATE_POST'}},
                        {'template': {'__typename': 'TimelineNotificationAggregateUserActions',
                                      'from_users': [{'user_results': {'result': user_result()}}]}}):
            with self.subTest(fields=sorted(content)):
                item = {'itemType': 'TimelineNotification', 'id': 'fixture-notice', **content}
                self.payloads['NotificationsTimeline']['data']['viewer_v2']['user_results']['result']['notification_timeline']['timeline']['instructions'] = [
                    {'type': 'TimelineAddEntries', 'entries': [
                        {'entryId': 'notification-1', 'content': {'itemContent': item}}]}]
                result = self.run_round()
                self.assertEqual(result['auth.notifications']['status'], 'ok')
                self.assertEqual(result['auth.notifications']['details']['item_count'], 1)

    def test_unknown_notification_instruction_is_not_accepted_as_empty(self):
        self.payloads['NotificationsTimeline']['data']['viewer_v2']['user_results']['result']['notification_timeline']['timeline']['instructions'] = [
            {'type': 'FutureInstructionV2', 'newEntries': [entry()]}]
        result = self.run_round()
        self.assertEqual(result['auth.notifications']['status'], 'incomplete')

    def test_pagination_uses_bounded_second_page_and_detects_no_progress(self):
        self.payloads['UserOriginalsTimeline'] = timeline([entry()], cursor='PRIVATE_CURSOR')
        result = self.run_round(pagination=True)
        self.assertEqual(result['auth.timeline.page2']['status'], 'contract_error')
        requests_for_timeline = [call for call in self.transport_get.call_args_list
                                 if call.args[0].endswith('/UserOriginalsTimeline')]
        self.assertEqual(len(requests_for_timeline), 2)
        params = requests_for_timeline[1].kwargs['params']
        self.assertEqual(json.loads(params['variables'])['cursor'], 'PRIVATE_CURSOR')

    def test_without_cursor_pagination_does_not_claim_coverage(self):
        result = self.run_round(pagination=True)
        self.assertEqual(result['auth.timeline.page2']['status'], 'incomplete')

    def test_failed_network_material_uses_cache_but_records_unverified_origin(self):
        self.network_material.side_effect = requests.ConnectionError('PRIVATE_PROXY')
        with patch('api_watch.probes.ClientTransaction.from_cached', return_value=self.material) as cached:
            result = self.run_round()
        cached.assert_called_once_with()
        self.assertEqual(result['auth.transaction']['status'], 'incomplete')
        self.assertEqual(result['auth.transaction']['details']['source'], 'cache')
        self.assertFalse(result['auth.transaction']['details']['current_source_verified'])
        self.assertEqual(result['auth.viewer']['status'], 'ok')

    def test_unavailable_material_blocks_api_without_hidden_transaction_retry(self):
        self.network_material.side_effect = requests.ConnectionError('PRIVATE_PROXY')
        with patch('api_watch.probes.ClientTransaction.from_cached', side_effect=ValueError('PRIVATE_TOKEN')):
            result = self.run_round()
        self.assertEqual(result['auth.transaction']['status'], 'network_error')
        self.assertEqual(result['auth.viewer']['status'], 'blocked')
        self.transport_get.assert_not_called()
        self.transport_post.assert_not_called()

    def test_throttled_or_expired_material_never_falls_back_or_probes(self):
        for http_status, expected in ((429, 'rate_limited'), (401, 'auth_expired')):
            with self.subTest(http_status=http_status):
                self.network_material.side_effect = requests.HTTPError(
                    'PRIVATE_TOKEN', response=response({}, http_status))
                with patch('api_watch.probes.ClientTransaction.from_cached', return_value=self.material) as cached, \
                     patch('api_watch.probes._GuestSource._refresh_guest_locked') as activate:
                    result = self.run_round(guest=True)
                cached.assert_not_called()
                activate.assert_not_called()
                self.guest_query.assert_not_called()
                self.transport_get.assert_not_called()
                self.transport_post.assert_not_called()
                for scope in ('guest', 'auth'):
                    self.assertEqual(result[scope + '.transaction']['status'], expected)
                    self.assertEqual(result[scope + '.transaction']['details']['source'], 'unavailable')
                    self.assertEqual(result[scope + '.user']['status'], 'blocked')
                self.assertEqual(result['auth.viewer']['status'], 'blocked')

    def test_stop_signal_prevents_initial_material_or_api_network(self):
        event = threading.Event()
        event.set()
        # The real helper checks stop before issuing its single shell request.
        self.network_material.side_effect = _REAL_NETWORK_TRANSACTION
        result = probes.run_probes({'guest': True, 'request_gap_seconds': 10}, cookies=COOKIES, stop_event=event)
        self.no_raw_network.assert_not_called()
        self.transport_get.assert_not_called()
        self.transport_post.assert_not_called()
        self.assertTrue(any(row['details'].get('interrupted') for row in result))

    def test_wait_is_interruptible(self):
        event = Mock()
        event.is_set.return_value = False
        event.wait.return_value = True
        pacer = probes._Pacer(60, event)
        pacer.wait()
        with self.assertRaises(probes._Issue) as raised:
            pacer.wait()
        self.assertEqual(raised.exception.status, 'incomplete')
        self.assertTrue(raised.exception.details['interrupted'])
        event.wait.assert_called_once()

    def test_guest_error_does_not_fall_back_to_account_cookie(self):
        self.guest_query.side_effect = GraphQLError('UserByScreenName', [{'code': 239, 'message': 'PRIVATE_TOKEN'}])
        result = probes.run_probes({'authenticated': False, 'request_gap_seconds': 0}, cookies=COOKIES)
        indexed = {row['check_id']: row for row in result}
        self.assertEqual(indexed['guest.user']['status'], 'auth_expired')
        self.assertEqual(indexed['guest.timeline']['status'], 'blocked')
        self.transport_get.assert_not_called()
        self.transport_post.assert_not_called()
        self.assert_private(result)

    def test_optional_chat_checks_only_settings_and_reports_missing_schema_baseline(self):
        with patch('x_apis.x_dm_api.XChatAPI.get_dm_settings', return_value={'data': {'settings': {}}}) as settings, \
             patch('x_apis.x_dm_api.XChatAPI.generate_chat_token') as token, \
             patch('x_apis.x_dm_api.XChatAPI.send_message') as send, \
             patch('x_apis.x_dm_api.XChatAPI.add_public_key') as key:
            result = self.run_round(chat=True)
        settings.assert_called_once()
        token.assert_not_called()
        send.assert_not_called()
        key.assert_not_called()
        self.assertEqual(result['auth.chat_settings']['status'], 'incomplete')

    def test_single_pass_material_fetch_scopes_cookies_to_shell_not_cdn(self):
        auth = XAuth().prepare_auth(COOKIES)
        shell = Mock(status_code=200, text='responsive-web/client-web fixture')
        asset = Mock(status_code=200, text='fixture-indices')

        def indices(html, session, proxies):
            fetched = session.get('https://abs.twimg.com/responsive-web/client-web/ondemand.s.hash.js',
                                  headers={'accept': '*/*'}, proxies=proxies, timeout=60)
            fetched.raise_for_status()
            return [1, 2, 3, 4]

        session = Mock()
        session.cookies = {'auth_token': 'PRIVATE_COOKIE'}
        def get_response(url, **kwargs):
            if 'abs.twimg.com' in url:
                self.assertEqual(session.cookies, {})
                return asset
            return shell
        session.get.side_effect = get_response
        with patch('api_watch.probes.requests.Session', return_value=session), \
             patch('api_watch.probes.fetch_indices', side_effect=indices), \
             patch('api_watch.probes.ClientTransaction.from_html', return_value=self.material):
            result = _REAL_NETWORK_TRANSACTION(auth, 'auth', probes._Pacer(0, None))
        get = session.get
        self.assertIs(result, self.material)
        self.assertFalse(session.trust_env)
        session.close.assert_called_once()
        self.assertEqual(get.call_count, 2)
        self.assertIn('auth_token', get.call_args_list[0].kwargs['cookies'])
        self.assertNotIn('cookies', get.call_args_list[1].kwargs)
        self.assertFalse(get.call_args_list[0].kwargs['allow_redirects'])
        self.assertFalse(get.call_args_list[1].kwargs['allow_redirects'])

    def test_unrecognized_shell_is_not_retried_twelve_times(self):
        shell = Mock(status_code=200, text='new unrelated shell PRIVATE_TOKEN')
        session = Mock()
        session.get.return_value = shell
        with patch('api_watch.probes.requests.Session', return_value=session):
            with self.assertRaises(probes._Issue) as raised:
                _REAL_NETWORK_TRANSACTION(XAuth(), 'guest', probes._Pacer(0, None))
        get = session.get
        self.assertEqual(raised.exception.status, 'incomplete')
        get.assert_called_once()
        self.assertNotIn('cookies', get.call_args.kwargs)
        self.assertFalse(session.trust_env)
        session.close.assert_called_once()

    def test_shell_redirect_is_reported_without_forwarding_cookies(self):
        shell = Mock(status_code=302)
        session = Mock()
        session.get.return_value = shell
        with patch('api_watch.probes.requests.Session', return_value=session):
            with self.assertRaises(probes._Issue) as raised:
                _REAL_NETWORK_TRANSACTION(XAuth().prepare_auth(COOKIES), 'auth', probes._Pacer(0, None))
        get = session.get
        self.assertEqual(raised.exception.status, 'incomplete')
        get.assert_called_once()
        self.assertFalse(get.call_args.kwargs['allow_redirects'])
        session.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
