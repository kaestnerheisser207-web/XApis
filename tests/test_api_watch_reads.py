"""Read scenarios exercise real validators; all transports remain offline."""
import base64
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from api_watch import read_probes as reads
from api_watch.probes import _Issue, _failure
from tests.test_api_watch_probes import entry, notifications, timeline, tweet_result, user_result
from utils.xchat_codec import encode_struct


def event(sequence='900', *, cid='101:303', kind=1):
    return base64.b64encode(encode_struct([
        (1, 11, sequence), (2, 11, 'PRIVATE_MESSAGE_ID'), (3, 11, '101'),
        (4, 11, cid), (7, 12, [(kind, 12, [])]),
    ])).decode()


def conversation(events=(), *, more=False, missing=()):
    return {'data': {'get_conversation_page': {
        'encoded_message_events': list(events), 'has_more': more,
        'missing_conversation_key_change_events': list(missing)}}}


def home(*, cursor=None):
    raw = timeline([entry()], cursor=cursor)
    return {'data': {'home': {'home_timeline_urt': raw['data']['user']['result']['timeline_v2']['timeline']}}}


def article_page(lifecycle='Draft', *, cursor=None):
    return {'data': {'user': {'result': {'articles_article_mixer_slice': {
        'items': [{'article_entity_results': {'result': {
            'rest_id': 'PRIVATE_ARTICLE', 'title': 'PRIVATE_TITLE',
            'lifecycle_state': {'lifecycle': lifecycle}}}}], 'next_cursor': cursor}}}}}


def members(ids):
    rows = []
    for uid in ids:
        user = dict(user_result(), rest_id=uid, chat_permissions={'can_dm': True, 'can_dm_on_xchat': True})
        rows.append({'id': uid, 'status': 'Found', 'member_results': {'rest_id': uid, 'result': user}})
    return {'data': {'get_member_results': {'results': rows}}}


def keys(ids, records=()):
    return {'data': {'user_results_by_rest_ids': [{'rest_id': uid, 'result': {
        '__typename': 'User', 'get_public_keys': {'__typename': 'GetPublicKeysResult',
        'public_keys_with_token_map': list(records)}}} for uid in ids]}}


def inbox(*, delta=False):
    if delta:
        page = {'__typename': 'XChatGetMessageEventsPageResponse', 'encoded_message_events': [event()],
                'cursor': {'__typename': 'XChatGetMessageEventsPageEndCursor', 'pull_finished': True}}
    else:
        page = {'__typename': 'XChatGetInboxPageResponse',
                'inboxCursor': {'__typename': 'XChatGetInboxPageEndCursor', 'inbox_exhausted': True,
                                'graph_snapshot_id': 'PRIVATE_SNAPSHOT'},
                'items': [{'__typename': 'XChatGetInboxPageConversationData',
                           'conversation_detail': {'__typename': 'XChatDirectConversationDetail',
                                                   'conversation_id': '101:303'},
                           'latest_message_events': [event()]}]}
    return {'data': {'get_initial_chat_page': page}}


class Context:
    def __init__(self, **samples):
        self.auth = object()
        self.actor_id = '101'
        self.samples = {'username': 'fixture_user', 'search_query': 'PRIVATE_QUERY', 'post_id': '202',
                        'peer_user_id': '303', 'conversation_id': '101:303', **samples}
        self.config = {'probes': {'count': 5, 'pagination': True, 'chat': True}}
        self.secrets = {}
        self.stop_event = threading.Event()
        self.results = {}

    def step(self, probe_id, action, validator=None, **kwargs):
        if probe_id in self.results:
            raise AssertionError('duplicate probe: ' + probe_id)
        try:
            value = action()
            details = validator(value) if validator else {}
            status = 'ok'
        except Exception as exc:
            status, details = _failure(exc)
            value = None
        self.results[probe_id] = {'status': status, 'details': details}
        return SimpleNamespace(ok=status == 'ok', value=value, status=status)

    def block(self, ids, reason, status='blocked'):
        for probe_id in ids:
            if probe_id in self.results:
                raise AssertionError('duplicate blocked probe: ' + probe_id)
            self.results[probe_id] = {'status': status, 'details': {'reason': reason}}


class ReadTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.contract_path = str(self.directory / 'settings-contract.json')
        Path(self.contract_path).write_text(json.dumps({'data.settings.can_receive': 'boolean'}))
        self.enterContext(patch('builder.client.get', side_effect=AssertionError('network forbidden')))
        self.enterContext(patch('builder.client.post', side_effect=AssertionError('network forbidden')))
        self.user = self.enterContext(patch.object(reads.XAPI, 'get_user_info', return_value={
            'data': {'user': {'result': user_result()}}}))
        self.user_timeline = self.enterContext(patch.object(reads.XAPI, 'get_user_post_note',
            side_effect=lambda *a, **kw: timeline([entry()], cursor=None if kw.get('cursor') else 'PRIVATE_CURSOR')))
        self.home = self.enterContext(patch.object(reads.XAPI, 'get_home_timeline',
            side_effect=lambda *a, **kw: home(cursor=None if kw.get('cursor') else 'PRIVATE_CURSOR')))
        self.search = self.enterContext(patch.object(reads.XAPI, 'search_work', side_effect=self.search_page))
        self.notification = self.enterContext(patch.object(reads.XAPI, 'get_notifications', side_effect=self.notification_page))
        self.detail = self.enterContext(patch.object(reads.XAPI, 'get_work_info', side_effect=self.detail_page))
        self.hydration = self.enterContext(patch.object(reads.XAPI, 'get_work_result', return_value={
            'data': {'tweetResult': {'result': tweet_result()}}}))
        self.comments = self.enterContext(patch.object(reads.XAPI, 'get_work_comments', side_effect=self.detail_page))
        self.articles = self.enterContext(patch.object(reads.XArticleAPI, 'list_articles',
            side_effect=lambda *a, **kw: article_page(kw['lifecycle'], cursor=None if kw.get('cursor') else 'PRIVATE_CURSOR')))
        self.settings = self.enterContext(patch.object(reads.XChatAPI, 'get_dm_settings', return_value={
            'data': {'settings': {'can_receive': True}}}))
        self.members = self.enterContext(patch.object(reads.XChatAPI, 'get_users_by_ids', side_effect=lambda auth, ids: members(ids)))
        self.keys = self.enterContext(patch.object(reads.XChatAPI, 'get_public_keys', side_effect=lambda auth, ids, **kw: keys(ids)))
        self.inbox = self.enterContext(patch.object(reads.XChatAPI, 'get_initial_chat_page', return_value=inbox()))
        self.conversation = self.enterContext(patch.object(reads.XChatAPI, 'get_conversation_page', side_effect=self.conversation_page))

    @staticmethod
    def search_page(auth, query, *, product, cursor=None, **kwargs):
        if product == 'People':
            item = {'itemType': 'TimelineUser', 'user_results': {'result': user_result()}}
            entries = [{'entryId': 'user-101', 'content': {'itemContent': item}}]
        elif product == 'Lists':
            item = {'itemType': 'TimelineTwitterList', 'list': {'id_str': '101', 'name': 'PRIVATE_LIST'}}
            entries = [{'entryId': 'list-101', 'content': {'itemContent': item}}]
        else:
            entries = [entry()]
        raw = timeline(entries, kind='search', cursor=None if cursor else 'PRIVATE_CURSOR')
        if product == 'Lists':
            root = raw['data']['search_by_raw_query']
            root['list_search_timeline'] = root.pop('search_timeline')
        return raw

    @staticmethod
    def notification_page(auth, *, cursor=None, **kwargs):
        raw = notifications()
        if not cursor:
            raw['data']['viewer_v2']['user_results']['result']['notification_timeline']['timeline']['instructions'] = [
                {'type': 'TimelineAddEntries', 'entries': [{'entryId': 'cursor-bottom',
                 'content': {'cursorType': 'Bottom', 'value': 'PRIVATE_CURSOR'}}]}]
        return raw

    @staticmethod
    def detail_page(auth, post_id, *, cursor=None):
        reply = entry('tweet-203')
        reply['content']['itemContent']['tweet_results']['result']['rest_id'] = '203'
        reply['content']['itemContent']['tweet_results']['result']['legacy']['in_reply_to_status_id_str'] = post_id
        return timeline([reply] if cursor else [entry(), reply], kind='detail', cursor=None if cursor else 'PRIVATE_CURSOR')

    @staticmethod
    def conversation_page(auth, cid, *, min_local_sequence_id, **kwargs):
        return conversation([event('899')]) if min_local_sequence_id == '900' else conversation([event()], more=True)

    def test_every_spec_has_a_record_and_all_variants_are_called(self):
        ctx = Context(chat_settings_contract_file=self.contract_path)
        reads.run(ctx)
        self.assertEqual(set(ctx.results), {spec['id'] for spec in reads.SPECS})
        self.assertEqual(len(reads.SPECS), len({spec['id'] for spec in reads.SPECS}))
        for probe_id, result in ctx.results.items():
            self.assertEqual(result['status'], 'ok', probe_id)
        self.assertEqual({c.kwargs['product'] for c in self.search.call_args_list}, {'Top', 'Latest', 'People', 'Media', 'Lists'})
        self.assertEqual({c.kwargs['type'] for c in self.notification.call_args_list}, set(reads.NOTIFICATION_TYPES))
        self.assertEqual({c.kwargs['operation'] for c in self.user_timeline.call_args_list}, {'UserOriginalsTimeline', 'UserTweets'})
        self.assertEqual(self.home.call_count, 4)
        self.assertEqual(self.search.call_count, 10)
        self.assertEqual(self.notification.call_count, 10)
        self.assertEqual(self.articles.call_count, 4)
        text = json.dumps(ctx.results)
        for private in ('PRIVATE_', 'fixture_user', '101:303'):
            self.assertNotIn(private, text)

    def test_missing_sample_is_blocked_without_request(self):
        ctx = Context(post_id='', peer_user_id='', conversation_id='')
        reads.run(ctx)
        self.detail.assert_not_called()
        self.hydration.assert_not_called()
        self.comments.assert_not_called()
        self.members.assert_not_called()
        self.keys.assert_not_called()
        self.conversation.assert_not_called()
        self.assertEqual(ctx.results['read.post.detail']['status'], 'blocked')
        self.assertEqual(ctx.results['read.home.post']['status'], 'ok')
        self.assertEqual(self.home.call_args.kwargs['seen_tweet_ids'], ['202'])

    def test_no_cursor_never_counts_as_pagination_coverage(self):
        self.user_timeline.side_effect = lambda *a, **kw: timeline([entry()])
        ctx = Context()
        reads.run(ctx)
        self.assertEqual(ctx.results['read.user.originals']['status'], 'ok')
        self.assertEqual(ctx.results['read.user.originals.page2']['status'], 'incomplete')
        self.assertEqual(self.user_timeline.call_count, 2)

    def test_repeated_cursor_is_incomplete(self):
        self.user_timeline.side_effect = lambda *a, **kw: timeline([entry()], cursor='PRIVATE_CURSOR')
        ctx = Context()
        reads.run(ctx)
        self.assertEqual(ctx.results['read.user.tweets.page2']['status'], 'incomplete')

    def test_profile_failure_blocks_dependent_reads_but_not_search(self):
        self.user.return_value = {'errors': [{'code': 144}], 'data': {}}
        ctx = Context()
        reads.run(ctx)
        self.assertEqual(ctx.results['read.user.profile']['status'], 'sample_unavailable')
        self.user_timeline.assert_not_called()
        self.assertEqual(ctx.results['read.search.top']['status'], 'ok')

    def test_partial_graphql_response_never_passes(self):
        raw = self.search_page(None, '', product='People')
        raw['errors'] = [{'message': 'PRIVATE_DIAGNOSTIC'}]
        with self.assertRaises(_Issue) as caught:
            reads._search(raw, 'People')
        self.assertEqual(caught.exception.status, 'incomplete')

    def test_search_people_requires_user_contract(self):
        raw = timeline([entry()], kind='search')
        with self.assertRaises(_Issue) as caught:
            reads._search(raw, 'People')
        self.assertEqual(caught.exception.status, 'incomplete')

    def test_search_lists_requires_list_id_and_name(self):
        raw = self.search_page(None, '', product='Lists')
        raw['data']['search_by_raw_query']['list_search_timeline']['timeline']['instructions'][0]['entries'][0]['content']['itemContent']['list']['name'] = None
        with self.assertRaises(_Issue) as caught:
            reads._search(raw, 'Lists')
        self.assertEqual(caught.exception.status, 'contract_error')

    def test_search_unknown_item_alongside_known_tweet_does_not_pass(self):
        unknown = {'entryId': 'unknown', 'content': {'itemContent': {'itemType': 'PRIVATE_NEW_TYPE'}}}
        with self.assertRaises(_Issue) as caught:
            reads._search(timeline([entry(), unknown], kind='search'), 'Latest')
        self.assertEqual(caught.exception.status, 'incomplete')

    def test_home_requires_operation_specific_path(self):
        with self.assertRaises(_Issue):
            reads._home(timeline([entry()]))

    def test_detail_first_page_requires_target_but_second_does_not(self):
        ctx = Context()
        reads.run(ctx)
        self.assertEqual(ctx.results['read.post.detail.page2']['status'], 'ok')
        self.detail.side_effect = lambda *a, **kw: timeline([], kind='detail')
        ctx = Context()
        reads.run(ctx)
        self.assertEqual(ctx.results['read.post.detail']['status'], 'sample_unavailable')

    def test_comments_without_direct_reply_are_not_parsing_evidence(self):
        with self.assertRaises(_Issue) as caught:
            reads._comments(timeline([entry()], kind='detail'), '202')
        self.assertEqual(caught.exception.status, 'incomplete')

    def test_article_lifecycle_mismatch_and_bad_cursor_fail(self):
        with self.assertRaises(_Issue):
            reads._articles(article_page('Published'), 'Draft')
        with self.assertRaises(_Issue):
            reads._articles(article_page(cursor={'token': 'PRIVATE_TOKEN'}), 'Draft')

    def test_members_require_exact_requested_accounts(self):
        with self.assertRaises(_Issue):
            reads._members(members(['101', '404']), ['101', '303'])
        raw = members(['101'])
        raw['data']['get_member_results']['results'][0]['member_results']['result']['chat_permissions'] = {}
        with self.assertRaises(_Issue) as caught:
            reads._members(raw, ['101'])
        self.assertEqual(caught.exception.status, 'incomplete')

    def test_public_keys_require_unique_versions_and_real_key_encoding(self):
        record = {'public_key_with_metadata': {'version': '1', 'public_key': {
            name: base64.b64encode(b'synthetic').decode() for name in
            ('public_key', 'signing_public_key', 'identity_public_key_signature')}}}
        self.assertEqual(reads._public_keys(keys(['101'], [record]), ['101'])['key_count'], 1)
        with self.assertRaises(_Issue):
            reads._public_keys(keys(['101'], [record, record]), ['101'])
        record['public_key_with_metadata']['public_key']['public_key'] = 'INVALID_BASE64'
        with self.assertRaises(_Issue):
            reads._public_keys(keys(['101'], [record]), ['101'])

    def test_public_keys_do_not_request_recovery_tokens(self):
        reads.run(Context())
        self.assertIs(self.keys.call_args.kwargs['include_juicebox_tokens'], False)

    def test_chat_unknown_envelope_does_not_report_coverage(self):
        for validator in (reads._chat_settings, reads._chat_inbox):
            with self.assertRaises(_Issue) as caught:
                validator({'data': {'anything': {'private': 'PRIVATE_TEXT'}}})
            self.assertIn(caught.exception.status, ('incomplete', 'contract_error'))
            self.assertNotIn('PRIVATE_TEXT', json.dumps(caught.exception.details))

    def test_settings_contract_capture_contains_paths_and_types_only(self):
        raw = {'data': {'settings': {'flag': True, 'label': 'PRIVATE_SETTING',
                'count': 3, 'ratio': 1.5, 'extra': None, 'choices': ['PRIVATE_CHOICE']}}}
        contract = reads.derive_settings_contract(raw)
        self.assertEqual(contract['data.settings.flag'], 'boolean')
        self.assertEqual(contract['data.settings.count'], 'integer')
        self.assertEqual(contract['data.settings.ratio'], 'number')
        self.assertEqual(contract['data.settings.extra'], 'null')
        self.assertEqual(contract['data.settings.choices'], 'array')
        self.assertNotIn('PRIVATE_', json.dumps(contract))
        self.assertTrue(reads._chat_settings(raw, contract)['reviewed_contract_matched'])

    def test_settings_contract_requires_specific_fields_and_detects_type_drift(self):
        for contract in ({}, {'data': 'object'}, {'data.settings': 'object'},
                         {'data.settings.enabled': 'unsupported'}, {'PRIVATE': 'string'}):
            with self.assertRaises(_Issue):
                reads._chat_settings({'data': {'settings': {}}}, contract)
        with self.assertRaises(_Issue) as caught:
            reads._chat_settings({'data': {'settings': {'enabled': 'PRIVATE_VALUE'}}},
                                 {'data.settings.enabled': 'boolean'})
        self.assertEqual(caught.exception.status, 'contract_error')
        self.assertNotIn('PRIVATE_VALUE', json.dumps(caught.exception.details))

    def test_settings_capture_is_bounded_and_rejects_dynamic_keys(self):
        for raw in ({'data': {}}, {'data': {'settings': {}}},
                    {'data': {'settings': {'123': 'PRIVATE'}}},
                    {'data': {'settings': {f'field_{n}': True for n in range(257)}}}):
            with self.assertRaises(_Issue):
                reads.derive_settings_contract(raw)
        raw = {'enabled': True}
        for _ in range(13):
            raw = {'nested': raw}
        with self.assertRaises(_Issue):
            reads.derive_settings_contract({'data': raw})

    def test_missing_settings_contract_is_blocked_before_request(self):
        ctx = Context()
        reads.run(ctx)
        self.settings.assert_not_called()
        self.assertEqual(ctx.results['read.chat.settings']['status'], 'blocked')
        ctx = Context(chat_settings_contract_file=str(self.directory / 'absent.json'))
        reads.run(ctx)
        self.settings.assert_not_called()
        self.assertEqual(ctx.results['read.chat.settings']['status'], 'blocked')

    def test_settings_contract_invalid_json_is_blocked(self):
        Path(self.contract_path).write_text('invalid JSON')
        ctx = Context(chat_settings_contract_file=self.contract_path)
        reads.run(ctx)
        self.assertEqual(ctx.results['read.chat.settings']['status'], 'blocked')
        self.settings.assert_not_called()

    def test_inbox_snapshot_and_delta_wire_shapes(self):
        self.assertEqual(reads._chat_inbox(inbox())['item_count'], 1)
        self.assertEqual(reads._chat_inbox(inbox(delta=True))['event_count'], 1)
        raw = inbox()
        raw['data']['get_initial_chat_page']['items'] = []
        self.assertEqual(reads._chat_inbox(raw)['item_count'], 0)

    def test_inbox_unknown_union_and_nested_error_are_not_success(self):
        for page in ({'__typename': 'FutureInbox', 'items': []},
                     {'__typename': 'XChatGetInboxPageResponse', 'items': [], 'error_code': 'PRIVATE_ERROR'}):
            with self.assertRaises(_Issue) as caught:
                reads._chat_inbox({'data': {'get_initial_chat_page': page}})
            self.assertEqual(caught.exception.status, 'incomplete')

    def test_inbox_cursor_requires_complete_continuation_identity(self):
        raw = inbox()
        raw['data']['get_initial_chat_page']['inboxCursor'] = {
            '__typename': 'XChatGetInboxPageContinueCursor', 'cursor_id': 'PRIVATE_CURSOR'}
        with self.assertRaises(_Issue):
            reads._chat_inbox(raw)
        raw['data']['get_initial_chat_page']['inboxCursor']['graph_snapshot_id'] = 'PRIVATE_SNAPSHOT'
        self.assertIs(reads._chat_inbox(raw)['has_cursor'], True)

    def test_cursor_unions_reject_unknown_types_and_incomplete_terminal_fields(self):
        for cursor in ({'__typename': 'FutureCursor', 'pull_finished': True},
                       {'__typename': 'XChatGetInboxPageEndCursor', 'inbox_exhausted': 'false',
                        'graph_snapshot_id': 'PRIVATE_SNAPSHOT'},
                       {'__typename': 'XChatGetInboxPageContinueCursor', 'cursor_id': 'cursor',
                        'graph_snapshot_id': 'snapshot', 'graph_snapshot_restarted': 'false'}):
            raw = inbox()
            raw['data']['get_initial_chat_page']['inboxCursor'] = cursor
            with self.subTest(cursor=cursor), self.assertRaises(_Issue):
                reads._chat_inbox(raw)

    def test_inbox_terminal_union_does_not_require_omitted_nullable_flag(self):
        raw = inbox()
        cursor = raw['data']['get_initial_chat_page']['inboxCursor']
        del cursor['inbox_exhausted']
        self.assertFalse(reads._chat_inbox(raw)['has_cursor'])
        del cursor['graph_snapshot_id']
        with self.assertRaises(_Issue):
            reads._chat_inbox(raw)

    def test_delta_continuation_requires_sequence_and_progress(self):
        raw = inbox(delta=True)
        page = raw['data']['get_initial_chat_page']
        page['cursor'] = {'__typename': 'XChatGetMessageEventsPageContinueCursor',
                          'max_local_sequence_id': '900'}
        self.assertTrue(reads._chat_inbox(raw)['has_cursor'])
        for value in (None, 'not-a-sequence', 900):
            page['cursor']['max_local_sequence_id'] = value
            with self.subTest(value=value), self.assertRaises(_Issue):
                reads._chat_inbox(raw)
        page['cursor']['max_local_sequence_id'] = '900'
        page['encoded_message_events'] = []
        with self.assertRaises(_Issue):
            reads._chat_inbox(raw)
        page['cursor'] = {'__typename': 'XChatGetMessageEventsPageEndCursor', 'pull_finished': True}
        self.assertFalse(reads._chat_inbox(raw)['has_cursor'])
        page['cursor']['pull_finished'] = False
        with self.assertRaises(_Issue):
            reads._chat_inbox(raw)

    def test_inbox_events_must_match_conversation(self):
        raw = inbox()
        raw['data']['get_initial_chat_page']['items'][0]['latest_message_events'] = [event(cid='101:404')]
        with self.assertRaises(_Issue):
            reads._chat_inbox(raw)

    def test_inbox_accepts_current_read_marker_and_delta_delete_events(self):
        raw = inbox()
        raw['data']['get_initial_chat_page']['items'][0]['latest_message_events'] = [event(kind=12)]
        self.assertEqual(reads._chat_inbox(raw)['event_count'], 1)
        raw = inbox(delta=True)
        raw['data']['get_initial_chat_page']['encoded_message_events'] = [event(kind=7)]
        self.assertEqual(reads._chat_inbox(raw)['event_count'], 1)

    def test_new_event_support_keeps_unknown_and_wrong_key_events_incomplete(self):
        for kind in (11, 999):
            raw = inbox(delta=True)
            raw['data']['get_initial_chat_page']['encoded_message_events'] = [event(kind=kind)]
            with self.subTest(kind=kind), self.assertRaises(_Issue) as caught:
                reads._chat_inbox(raw)
            self.assertEqual(caught.exception.status, 'incomplete')
        raw = inbox()
        raw['data']['get_initial_chat_page']['items'][0]['latest_conversation_key_change_events'] = [event(kind=12)]
        with self.assertRaises(_Issue) as caught:
            reads._chat_inbox(raw)
        self.assertEqual(caught.exception.status, 'contract_error')

    def test_chat_conversation_rejects_bad_encoding_and_wrong_conversation(self):
        for encoded in ('INVALID_BASE64', event(cid='101:404')):
            with self.assertRaises(_Issue):
                reads._conversation(conversation([encoded]), '101:303')

    def test_chat_conversation_unknown_type_cannot_be_silent_empty_page(self):
        with self.assertRaises(_Issue) as caught:
            reads._conversation(conversation([event(kind=999)]), '101:303')
        self.assertEqual(caught.exception.status, 'incomplete')

    def test_chat_conversation_monotonic_cursor_and_missing_key_types(self):
        with self.assertRaises(_Issue):
            reads._conversation(conversation([event('900')]), '101:303', before=900)
        with self.assertRaises(_Issue):
            reads._conversation(conversation([], more=True), '101:303')
        with self.assertRaises(_Issue):
            reads._conversation(conversation([], missing=[event()]), '101:303')
        self.assertEqual(reads._conversation_cursor(conversation([event('900'), event('899')], more=True)), '899')

    def test_pagination_and_chat_disabled_remain_visible(self):
        ctx = Context()
        ctx.config['probes'].update(pagination=False, chat=False, notifications=False)
        reads.run(ctx)
        self.assertEqual(set(ctx.results), {spec['id'] for spec in reads.SPECS})
        self.assertEqual(ctx.results['read.search.people.page2']['status'], 'incomplete')
        self.assertEqual(ctx.results['read.chat.members']['status'], 'incomplete')
        self.settings.assert_not_called()
        self.inbox.assert_not_called()


if __name__ == '__main__':
    unittest.main()
