"""Offline checks for the public X Chat send contract; never sends messages."""

import base64
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from utils.xchat_codec import encode_struct
from x_apis.x_dm_api import XChatAPI


SEND_PATH = '/graphql/TWRPP7gnKwV_R8-tE-Dd3Q/SendMessageCreateMutation'
MESSAGE_ID = '12345678-1234-4234-8234-123456789abc'
EVENT = base64.b64encode(encode_struct([
    (1, 11, b'test-nonce-and-ciphertext'), (2, 11, '1'),
])).decode('ascii')
SIGNATURE = base64.b64encode(encode_struct([
    (1, 11, 'test-signature'), (2, 11, '1'), (3, 11, '1'),
])).decode('ascii')


def receipt(*, message_id=MESSAGE_ID, conversation_id='1-2',
            detail=None, include_detail=True):
    fields = [(1, 11, '100'), (2, 11, message_id), (3, 11, '1'),
              (4, 11, conversation_id)]
    if include_detail:
        if detail is None:
            detail = [(1, 12, [(1, 11, b'test-nonce-and-ciphertext')])]
        fields.append((7, 12, detail))
    return base64.b64encode(encode_struct(fields)).decode('ascii')


RESPONSE_EVENT = receipt()


class XChatSendTests(unittest.TestCase):
    def setUp(self):
        self.auth = SimpleNamespace(
            bearer='Bearer FAKE_BEARER_FOR_OFFLINE_TEST',
            ct0='FAKE_CSRF_FOR_OFFLINE_TEST',
            cookie={'auth_token': 'FAKE_COOKIE_FOR_OFFLINE_TEST'},
            proxies={'https': 'http://proxy.invalid:8080'},
            is_logged_in=True,
            user_id='1',
            client_transaction_id=Mock(return_value='test-transaction-id'),
        )
        self.response = Mock()
        self.response.json.return_value = {
            'data': {
                'xchat_send_create_message_event': {
                    '__typename': 'SendMessageCreateResponse',
                    'encoded_message_event': RESPONSE_EVENT,
                },
            },
        }
        self.post = self.enterContext(patch(
            'x_apis.x_dm_api.client.post', return_value=self.response))
        self.get = self.enterContext(patch(
            'x_apis.x_dm_api.client.get',
            side_effect=AssertionError('Unexpected GET in offline send test')))

    def send(self, **overrides):
        arguments = {
            'conversation_id': '1-2',
            'message_id': MESSAGE_ID,
            'encoded_message_create_event': EVENT,
            'encoded_message_event_signature': SIGNATURE,
        }
        arguments.update(overrides)
        return XChatAPI.send_message(self.auth, **arguments)

    def test_post_uses_new_xchat_contract_and_existing_auth(self):
        result = self.send(conversation_token='test-conversation-token')

        self.assertIs(result, self.response.json.return_value)
        self.post.assert_called_once()
        args, kwargs = self.post.call_args
        self.assertEqual(args, ('https://api.x.com' + SEND_PATH,))
        self.assertEqual(set(kwargs['json']), {'variables'})
        variables = kwargs['json']['variables']
        self.assertIsInstance(variables, str)
        self.assertEqual(json.loads(variables), {
            'conversation_id': '1-2',
            'message_id': MESSAGE_ID,
            'conversation_token': 'test-conversation-token',
            'encoded_message_create_event': EVENT,
            'encoded_message_event_signature': SIGNATURE,
        })
        self.assertIs(kwargs['cookies'], self.auth.cookie)
        self.assertIs(kwargs['proxies'], self.auth.proxies)
        self.assertEqual(kwargs['timeout'], 30)
        self.assertEqual(kwargs['headers']['authorization'], self.auth.bearer)
        self.assertEqual(kwargs['headers']['x-csrf-token'], self.auth.ct0)
        self.assertEqual(kwargs['headers']['content-type'], 'application/json')
        self.assertEqual(kwargs['headers']['origin'], 'https://x.com')
        self.assertEqual(kwargs['headers']['x-client-transaction-id'],
                         'test-transaction-id')
        self.auth.client_transaction_id.assert_called_once_with('POST', SEND_PATH)
        self.response.raise_for_status.assert_called_once_with()
        self.get.assert_not_called()

    def test_conversation_token_is_nullable_without_passcode_check(self):
        self.send()
        variables = json.loads(self.post.call_args.kwargs['json']['variables'])
        self.assertIsNone(variables['conversation_token'])
        self.post.assert_called_once()

    def test_invalid_identifiers_fail_before_auth_or_network(self):
        for field in ('conversation_id', 'message_id'):
            for value in (None, '', ' \t', 123):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ValueError, field):
                        self.send(**{field: value})
        self.post.assert_not_called()
        self.auth.client_transaction_id.assert_not_called()

    def test_invalid_optional_token_fails_before_network(self):
        for value in ('', ' ', 123, []):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, 'conversation_token'):
                    self.send(conversation_token=value)
        self.post.assert_not_called()

    def test_invalid_base64_fails_before_network(self):
        for field in ('encoded_message_create_event',
                      'encoded_message_event_signature'):
            for value in (None, '', '!!!!', 'YQ', 'YQ==\n', '文本', '====', 123):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ValueError, field):
                        self.send(**{field: value})
        self.post.assert_not_called()
        self.auth.client_transaction_id.assert_not_called()

    def test_http_failure_is_propagated_without_parsing_or_retry(self):
        failure = RuntimeError('HTTP 403')
        self.response.raise_for_status.side_effect = failure
        with self.assertRaises(RuntimeError) as caught:
            self.send()
        self.assertIs(caught.exception, failure)
        self.post.assert_called_once()
        self.response.json.assert_not_called()

    def test_unknown_network_result_is_not_retried(self):
        failure = TimeoutError('Connection timed out after POST')
        self.post.side_effect = failure
        with self.assertRaises(TimeoutError) as caught:
            self.send()
        self.assertIs(caught.exception, failure)
        self.post.assert_called_once()

    def test_graphql_error_fails_even_with_partial_data(self):
        self.response.json.return_value['errors'] = [{
            'message': 'server error with FAKE_RESPONSE_SECRET',
        }]
        with self.assertRaisesRegex(RuntimeError, 'GraphQL') as caught:
            self.send()
        self.assertNotIn('FAKE_RESPONSE_SECRET', str(caught.exception))
        self.post.assert_called_once()

    def test_missing_response_event_cannot_report_success(self):
        payloads = (
            {}, {'data': None}, {'data': []},
            {'data': {'xchat_send_create_message_event': None}},
            {'data': {'xchat_send_create_message_event': []}},
            {'data': {'xchat_send_create_message_event': {}}},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                self.post.reset_mock()
                self.response.json.return_value = payload
                with self.assertRaisesRegex(RuntimeError, 'encoded_message_event'):
                    self.send()
                self.post.assert_called_once()
        for event in (None, '', ' \n', 42):
            with self.subTest(event=event):
                self.post.reset_mock()
                self.response.json.return_value = {
                    'data': {'xchat_send_create_message_event': {
                        'encoded_message_event': event,
                    }},
                }
                with self.assertRaisesRegex(RuntimeError, 'encoded_message_event'):
                    self.send()
                self.post.assert_called_once()

    def test_message_failure_receipt_is_not_success_or_retried(self):
        self.response.json.return_value['data']['xchat_send_create_message_event'][
            'encoded_message_event'] = receipt(detail=[(5, 12, [(1, 8, 7)])])
        with self.assertRaisesRegex(RuntimeError, 'MessageFailureEvent'):
            self.send()
        self.post.assert_called_once()

    def test_receipt_must_match_message_and_conversation(self):
        for encoded in (
                receipt(message_id='other-message-id'),
                receipt(conversation_id='1-3'),
                receipt(detail=[(6, 12, [])])):
            with self.subTest(encoded=encoded):
                self.post.reset_mock()
                self.response.json.return_value['data']['xchat_send_create_message_event'][
                    'encoded_message_event'] = encoded
                with self.assertRaisesRegex(RuntimeError, '不匹配'):
                    self.send()
                self.post.assert_called_once()

    def test_missing_ambiguous_or_wrong_type_detail_is_not_success(self):
        for encoded in (
                receipt(include_detail=False),
                receipt(detail=[]),
                receipt(detail=[(1, 12, []), (5, 12, [(1, 8, 7)])]),
                receipt(detail=[(1, 11, b'not-a-struct')]),
                receipt(detail=[(5, 11, b'not-a-struct')])):
            with self.subTest(encoded=encoded):
                self.post.reset_mock()
                self.response.json.return_value['data']['xchat_send_create_message_event'][
                    'encoded_message_event'] = encoded
                with self.assertRaisesRegex(RuntimeError, '无法解码'):
                    self.send()
                self.post.assert_called_once()

    def test_malformed_receipt_is_not_success_or_retried(self):
        for encoded in (
                '!!!!', base64.b64encode(b'not-thrift').decode('ascii'),
                base64.b64encode(encode_struct([(2, 11, b'\xff')])).decode('ascii'),
                base64.b64encode(encode_struct([(2, 8, 1)])).decode('ascii'),
                base64.b64encode(base64.b64decode(RESPONSE_EVENT)[:-1]).decode('ascii')):
            with self.subTest(encoded=encoded):
                self.post.reset_mock()
                self.response.json.return_value['data']['xchat_send_create_message_event'][
                    'encoded_message_event'] = encoded
                with self.assertRaisesRegex(RuntimeError, '无法解码'):
                    self.send()
                self.post.assert_called_once()

    def test_invalid_json_and_nonobject_payloads_fail(self):
        self.response.json.side_effect = ValueError('FAKE_JSON_SECRET')
        with self.assertRaisesRegex(RuntimeError, 'JSON') as caught:
            self.send()
        self.assertNotIn('FAKE_JSON_SECRET', str(caught.exception))
        self.post.assert_called_once()
        self.response.json.side_effect = None
        for payload in (None, [], 'unexpected string', 42):
            with self.subTest(payload=payload):
                self.post.reset_mock()
                self.response.json.return_value = payload
                with self.assertRaisesRegex(RuntimeError, 'GraphQL'):
                    self.send()
                self.post.assert_called_once()

    def test_send_does_not_print_or_log_request_secrets(self):
        output = io.StringIO()
        log_output = io.StringIO()
        handler = logging.StreamHandler(log_output)
        logger = logging.getLogger()
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        with redirect_stdout(output), redirect_stderr(output):
            self.send(conversation_token='FAKE_CONVERSATION_SECRET')
            self.response.json.return_value = {'errors': [{
                'message': 'FAKE_RESPONSE_SECRET',
                'extensions': {'request': self.post.call_args.kwargs['json']},
            }]}
            with self.assertRaises(RuntimeError) as caught:
                self.send(conversation_token='FAKE_CONVERSATION_SECRET')
        exposed = output.getvalue() + log_output.getvalue() + str(caught.exception)
        for secret in (
                self.auth.bearer, self.auth.ct0, self.auth.cookie['auth_token'],
                EVENT, SIGNATURE, 'FAKE_CONVERSATION_SECRET', 'FAKE_RESPONSE_SECRET'):
            self.assertNotIn(secret, exposed)

    def test_bootstrap_does_not_infer_passcode_state_from_reads(self):
        with ExitStack() as stack:
            for method in (
                    'generate_chat_token', 'get_dm_settings',
                    'get_initial_chat_page', 'get_users_by_ids',
                    'get_public_keys', 'get_conversation_page'):
                stack.enter_context(patch.object(XChatAPI, method,
                                                return_value={'test': method}))
            result = XChatAPI.bootstrap(self.auth, conversation_id='1-2')
        self.assertIn('passcode_required', result)
        self.assertIsNone(result['passcode_required'])
        self.assertIn('conversation', result)
        self.post.assert_not_called()
        self.get.assert_not_called()


if __name__ == '__main__':
    unittest.main()
