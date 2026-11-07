"""Business receipts cannot turn missing structure into success or empty data."""
import unittest
from unittest.mock import patch

from tests.test_api_watch_probes import entry, timeline, tweet_result, user_result
from x_apis import XClient, XClientError
from x_apis.x_write_api import XWriteAPI


def posted(tweet_id='301'):
    return {'data': {'create_tweet': {'tweet_results': {'result': {'rest_id': tweet_id}}}}}


class ReadResultContractTests(unittest.TestCase):
    def setUp(self):
        self.client = XClient(object())
        self.profile = self.enterContext(patch('x_apis.client.XAPI.get_user_info',
            return_value={'data': {'user': {'result': user_result()}}}))

    def assert_error(self, code, call):
        with self.assertRaises(XClientError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_missing_operation_structure_is_not_an_empty_page(self):
        operations = (
            ('search_work', lambda: self.client.posts.search('query')),
            ('get_home_timeline', self.client.posts.home),
            ('get_user_post_note', lambda: self.client.users.posts('fixture_user')),
            ('get_work_info', lambda: self.client.posts.get('202')),
            ('get_work_comments', lambda: self.client.comments.list('202')),
        )
        for method, call in operations:
            for raw in ({}, {'data': {}}, {'data': None}):
                with self.subTest(method=method, raw=raw), patch('x_apis.client.XAPI.' + method, return_value=raw):
                    self.assert_error('invalid_response', call)

    def test_legitimate_empty_timelines_are_valid_pages(self):
        home = {'data': {'home': {'home_timeline_urt': {'instructions': []}}}}
        operations = (
            ('search_work', timeline([], kind='search'), lambda: self.client.posts.search('query')),
            ('get_home_timeline', home, self.client.posts.home),
            ('get_user_post_note', timeline([]), lambda: self.client.users.posts('fixture_user')),
            ('get_work_comments', timeline([], kind='detail'), lambda: self.client.comments.list('202')),
        )
        for method, raw, call in operations:
            with self.subTest(method=method), patch('x_apis.client.XAPI.' + method, return_value=raw):
                self.assertEqual(call(), {'items': [], 'next_cursor': None})

    def test_search_rejects_other_operation_timeline(self):
        with patch('x_apis.client.XAPI.search_work', return_value=timeline([entry()])):
            self.assert_error('invalid_response', lambda: self.client.posts.search('query'))

    def test_search_relevance_prompt_preserves_posts_and_cursor_without_feedback(self):
        prompt = {'entryId': 'prompt', 'content': {'itemContent': {
            'itemType': 'TimelinePrompt', 'content': {
                'contentType': 'TimelineRelevancePrompt', 'title': 'Is this relevant?',
                'isRelevantCallback': {'endpoint': 'https://must-not-be-called.invalid/feedback'}}}}}
        raw = timeline([entry(), prompt], kind='search', cursor='next-page')
        with patch('x_apis.client.XAPI.search_work', return_value=raw):
            result = self.client.posts.search('query', product='Top')
        self.assertEqual([p['work_id'] for p in result['items']], ['202'])
        self.assertEqual(result['next_cursor'], 'next-page')
        prompt['content']['itemContent']['content']['contentType'] = 'FuturePrompt'
        with patch('x_apis.client.XAPI.search_work', return_value=raw):
            self.assert_error('invalid_response', lambda: self.client.posts.search('query', product='Top'))

    def test_media_search_continuation_flattens_added_module_items(self):
        raw = timeline([], kind='search')
        instructions = raw['data']['search_by_raw_query']['search_timeline']['timeline']['instructions']
        instructions.extend([
            {'type': 'TimelineAddToModule', 'moduleEntryId': 'media-grid', 'moduleItems': [
                {'entryId': 'media-202', 'item': {'itemContent': entry()['content']['itemContent']}}]},
            {'type': 'TimelineReplaceEntry', 'entry': {'entryId': 'cursor-bottom',
                'content': {'cursorType': 'Bottom', 'value': 'page-three'}}},
        ])
        with patch('x_apis.client.XAPI.search_work', return_value=raw):
            page = self.client.posts.search('query', product='Media', cursor='page-two')
        self.assertEqual([p['work_id'] for p in page['items']], ['202'])
        self.assertEqual(page['next_cursor'], 'page-three')
        instructions[1]['moduleItems'] = None
        with patch('x_apis.client.XAPI.search_work', return_value=raw):
            self.assert_error('invalid_response', lambda: self.client.posts.search('query', product='Media'))

    def test_search_only_reads_cursor_from_operation_timeline(self):
        raw = timeline([entry()], kind='search', cursor='next-page')
        raw['unrelated'] = {'entryId': 'cursor-wrong', 'content': {'cursorType': 'Bottom', 'value': 'wrong'}}
        with patch('x_apis.client.XAPI.search_work', return_value=raw):
            result = self.client.posts.search('query')
        self.assertEqual(result['next_cursor'], 'next-page')
        self.assertEqual(result['items'][0]['work_id'], '202')

    def test_malformed_or_unknown_instructions_do_not_disappear(self):
        for instructions in ({}, [{}], [{'type': 'RenamedTimeline'}],
                             [{'type': 'TimelineAddEntries', 'entries': None}]):
            raw = timeline([], kind='search')
            raw['data']['search_by_raw_query']['search_timeline']['timeline']['instructions'] = instructions
            with self.subTest(instructions=instructions), patch('x_apis.client.XAPI.search_work', return_value=raw):
                self.assert_error('invalid_response', lambda: self.client.posts.search('query'))

    def test_malformed_tweets_and_unknown_items_do_not_become_empty_results(self):
        malformed = [None, {}, {'__typename': 'Tweet'}, {'rest_id': '202', 'legacy': {}}]
        for result in malformed:
            item = entry()
            item['content']['itemContent']['tweet_results']['result'] = result
            with self.subTest(result=result), patch('x_apis.client.XAPI.search_work',
                    return_value=timeline([item], kind='search')):
                self.assert_error('invalid_response', lambda: self.client.posts.search('query'))
        unknown = {'entryId': 'unknown-1', 'content': {'itemContent': {'itemType': 'UnknownItem'}}}
        with patch('x_apis.client.XAPI.search_work', return_value=timeline([entry(), unknown], kind='search')):
            self.assert_error('invalid_response', lambda: self.client.posts.search('query'))

    def test_tombstone_is_unavailable_while_valid_empty_detail_is_not_a_post(self):
        item = entry()
        item['content']['itemContent']['tweet_results']['result'] = {'__typename': 'TweetTombstone'}
        for raw in (timeline([item], kind='detail'), timeline([], kind='detail')):
            with patch('x_apis.client.XAPI.get_work_info', return_value=raw):
                self.assert_error('post_unavailable', lambda: self.client.posts.get('202'))

    def test_user_timeline_skips_typed_recommendations_but_rejects_broken_ones(self):
        recommendation = {'entryId': 'who-to-follow-1', 'content': {'items': [
            {'item': {'itemContent': {'itemType': 'TimelineUser',
                                     'user_results': {'result': user_result()}}}}
        ]}}
        raw = timeline([entry(), recommendation], kind='user', cursor='next-user-page')
        with patch('x_apis.client.XAPI.get_user_post_note', return_value=raw):
            result = self.client.users.posts('fixture_user')
            self.assertEqual([item['work_id'] for item in result['items']], ['202'])
            self.assertEqual(result['next_cursor'], 'next-user-page')
        recommendation['content']['items'][0]['item']['itemContent']['user_results'] = {}
        with patch('x_apis.client.XAPI.get_user_post_note', return_value=raw):
            self.assert_error('invalid_response', lambda: self.client.users.posts('fixture_user'))

    def test_conversation_module_returns_direct_replies_and_preserves_cursor(self):
        reply = tweet_result()
        reply['rest_id'] = '203'
        reply['legacy']['in_reply_to_status_id_str'] = '202'
        module = {'entryId': 'conversationthread-202', 'content': {'items': [
            {'entryId': 'conversationthread-202-tweet-203',
             'item': {'itemContent': {'tweet_results': {'result': reply}}}}
        ]}}
        raw = timeline([entry(), module], kind='detail', cursor='next-comments')
        with patch('x_apis.client.XAPI.get_work_comments', return_value=raw):
            result = self.client.comments.list('202')
        self.assertEqual([item['work_id'] for item in result['items']], ['203'])
        self.assertEqual(result['next_cursor'], 'next-comments')

    def test_user_profile_missing_fields_and_explicit_unavailable_are_distinct(self):
        for result in ({}, {'rest_id': '101'}, {'rest_id': '101', 'core': []},
                       {'rest_id': '101', 'core': {'screen_name': ''}}):
            self.profile.return_value = {'data': {'user': {'result': result}}}
            self.assert_error('invalid_response', lambda: self.client.users.get('fixture_user'))
        for result in (None, {'__typename': 'UserUnavailable'}):
            self.profile.return_value = {'data': {'user': {'result': result}}}
            self.assert_error('user_unavailable', lambda: self.client.users.get('fixture_user'))

    def test_missing_viewer_is_not_an_authenticated_profile(self):
        with patch('x_apis.client.XAPI.get_viewer', return_value={'data': {}}):
            self.assert_error('invalid_response', self.client.users.me)
        with patch('x_apis.client.XAPI.get_viewer',
                   return_value={'data': {'viewer': {'user_results': {'result': None}}}}):
            self.assert_error('auth_required', self.client.users.me)

    def test_profile_optional_values_preserve_nulls_but_reject_wrong_types(self):
        self.assertIsNone(self.client.users.get('fixture_user')['followers'])
        for field, value in (('name', []), ('description', {}), ('followers', '100'),
                             ('followers', True), ('followers', -1)):
            user = user_result()
            if field == 'name':
                user['core']['name'] = value
            elif field == 'description':
                user['profile_bio'] = {'description': value}
            else:
                user['relationship_counts'] = {'followers': value}
            self.profile.return_value = {'data': {'user': {'result': user}}}
            with self.subTest(field=field, value=value):
                self.assert_error('invalid_response', lambda: self.client.users.get('fixture_user'))

    def test_post_counts_preserve_absent_defaults_but_reject_null_and_wrong_types(self):
        with patch('x_apis.client.XAPI.search_work', return_value=timeline([entry()], kind='search')):
            post = self.client.posts.search('query')['items'][0]
            self.assertEqual(post['like_count'], 0)
            self.assertEqual(post['view_count'], '')
        for field in ('favorite_count', 'retweet_count', 'reply_count', 'quote_count', 'bookmark_count'):
            for value in (None, '1', True, -1):
                item = entry()
                item['content']['itemContent']['tweet_results']['result']['legacy'][field] = value
                with self.subTest(field=field, value=value), patch('x_apis.client.XAPI.search_work',
                        return_value=timeline([item], kind='search')):
                    self.assert_error('invalid_response', lambda: self.client.posts.search('query'))

    def test_post_optional_text_views_and_media_fail_on_invalid_output_types(self):
        variants = [
            {'legacy': {'lang': None}},
            {'views': {'count': None}},
            {'views': {'count': True}},
            {'legacy': {'extended_entities': {'media': [{'type': 'photo'}]}}},
            {'legacy': {'extended_entities': {'media': [{'type': 'video', 'video_info': {'variants': [
                {'content_type': 'video/mp4', 'url': None}]}}]}}},
        ]
        for patch_values in variants:
            item = entry()
            tweet = item['content']['itemContent']['tweet_results']['result']
            for key, value in patch_values.items():
                tweet.setdefault(key, {}).update(value)
            with self.subTest(patch=patch_values), patch('x_apis.client.XAPI.search_work',
                    return_value=timeline([item], kind='search')):
                self.assert_error('invalid_response', lambda: self.client.posts.search('query'))

    def test_post_search_does_not_silently_discard_people_or_list_results(self):
        with patch('x_apis.client.XAPI.search_work') as search:
            for product in ('People', 'Lists'):
                self.assert_error('invalid_argument', lambda: self.client.posts.search('query', product=product))
            search.assert_not_called()

    def test_graphql_errors_use_classification_without_private_payload(self):
        raw = {'errors': [{'code': 89, 'message': 'PRIVATE_TOKEN'}], 'data': {}}
        with patch('x_apis.client.XAPI.search_work', return_value=raw):
            error = self.assert_error('auth_required', lambda: self.client.posts.search('query'))
        self.assertNotIn('PRIVATE_TOKEN', str(error))


class WriteResultContractTests(unittest.TestCase):
    def setUp(self):
        self.client = XClient(object())
        self.enterContext(patch('x_apis.client.XAPI.get_user_info',
            return_value={'data': {'user': {'result': user_result()}}}))

    def test_follow_and_unfollow_require_matching_identity_and_boolean_state(self):
        for expected in (True, False):
            method = 'follow' if expected else 'unfollow'
            operation = getattr(self.client.users, method)
            for raw in ({}, {'id_str': '101'}, {'id_str': '999', 'following': expected},
                        {'id_str': '101', 'following': not expected},
                        {'id_str': '101', 'following': int(expected)}):
                with self.subTest(method=method, raw=raw), patch(
                        'x_apis.client.XWriteAPI.' + method + '_user', return_value=raw):
                    with self.assertRaises(XClientError) as caught:
                        operation('fixture_user')
                    self.assertEqual(caught.exception.code, method + '_unconfirmed')
                    self.assertEqual(caught.exception.result['status'], 'unconfirmed')
            with patch('x_apis.client.XWriteAPI.' + method + '_user',
                       return_value={'id_str': '101', 'following': expected}):
                self.assertIs(operation('fixture_user')['following'], expected)

    def test_single_post_missing_or_malformed_id_is_unconfirmed(self):
        for raw in (None, {}, {'data': {}}, posted(''), posted('PRIVATE_NON_ID'), posted(301)):
            with self.subTest(raw=raw), patch.object(XWriteAPI, 'post_tweet', return_value=(True, 'ok', raw)):
                with self.assertRaises(XClientError) as caught:
                    self.client.posts.create('hello')
                self.assertEqual(caught.exception.code, 'post_unconfirmed')
                self.assertEqual(caught.exception.result['delivery_status'], 'unknown')
                self.assertNotIn('PRIVATE_NON_ID', str(caught.exception))

    def test_single_post_success_requires_confirmed_id(self):
        with patch.object(XWriteAPI, 'post_tweet', return_value=(True, 'ok', posted())):
            self.assertEqual(self.client.posts.create('hello')['post_id'], '301')

    def test_thread_missing_second_receipt_retains_first_and_stops(self):
        with patch.object(XWriteAPI, 'post_tweet', side_effect=[
                (True, 'ok', posted()), (True, 'ok', {'data': {}})]) as post:
            with self.assertRaises(XClientError) as caught:
                self.client.posts.thread(['first', 'second', 'third'], interval=0)
        self.assertEqual(caught.exception.code, 'thread_incomplete')
        self.assertEqual(caught.exception.result, {
            'post_ids': ['301'], 'status': 'partial', 'failed_index': 1, 'delivery_status': 'unknown'})
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args.kwargs['reply_to'], '301')

    def test_thread_transport_failure_and_rejection_preserve_confirmed_ids(self):
        for raw, delivery in ((None, 'unknown'), ({'errors': [{'code': 88}]}, 'rejected')):
            with self.subTest(delivery=delivery), patch.object(XWriteAPI, 'post_tweet', side_effect=[
                    (True, 'ok', posted()), (False, 'PRIVATE_PROXY', raw)]):
                with self.assertRaises(XClientError) as caught:
                    self.client.posts.thread(['first', 'second'], interval=0)
                self.assertEqual(caught.exception.result['post_ids'], ['301'])
                self.assertEqual(caught.exception.result['delivery_status'], delivery)
                self.assertNotIn('PRIVATE_PROXY', str(caught.exception))

    def test_legacy_thread_tuple_and_id_list_stay_compatible(self):
        with patch.object(XWriteAPI, 'post_tweet', side_effect=[
                (True, 'ok', posted()), (True, 'ok', posted('302'))]) as post:
            ok, message, ids = XWriteAPI.post_thread(object(), ['one', 'two'], interval=0)
        self.assertTrue(ok)
        self.assertIsInstance(ids, list)
        self.assertEqual(ids, ['301', '302'])
        self.assertEqual(post.call_args.kwargs['reply_to'], '301')

    def test_thread_validates_all_inputs_before_any_send(self):
        with patch.object(XWriteAPI, 'post_tweet') as post:
            for texts, interval in ((['first', ''], 0), ('text', 0), ([], 0),
                                    (['first'], -1), (['first'], float('nan'))):
                with self.subTest(texts=texts, interval=interval), self.assertRaises(XClientError) as caught:
                    self.client.posts.thread(texts, interval=interval)
                self.assertEqual(caught.exception.code, 'invalid_argument')
            post.assert_not_called()

    def test_receipt_extractor_does_not_embed_payload_in_exception(self):
        with self.assertRaises(KeyError) as caught:
            XWriteAPI.extract_tweet_id({'PRIVATE_BODY': 'PRIVATE_TOKEN'})
        self.assertNotIn('PRIVATE', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
