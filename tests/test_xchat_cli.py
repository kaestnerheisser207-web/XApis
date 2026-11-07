"""Offline chat-send CLI checks; every account lookup and send is mocked."""

import base64
from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from loguru import logger

import main
from utils.xchat_codec import encode_struct


MESSAGE_ID = '12345678-1234-4234-8234-123456789abc'
SECRET_TEXT = 'TEST_PRIVATE_TEXT_MUST_NOT_APPEAR'
SECRET_EVENT = 'TEST_PRIVATE_ENCRYPTED_EVENT_MUST_NOT_APPEAR'
SECRET_SIGNATURE = 'TEST_PRIVATE_SIGNATURE_MUST_NOT_APPEAR'


class XChatCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.receipt_path = Path(self.temp) / 'receipt.json'
        self.auth = object()
        self.keys = SimpleNamespace(sender_id='10', conversation_id='2:10')
        self.send_arguments = {
            'conversation_id': '2:10', 'message_id': MESSAGE_ID,
            'encoded_message_create_event': SECRET_EVENT,
            'encoded_message_event_signature': SECRET_SIGNATURE,
            'conversation_token': 'TEST_PRIVATE_CONVERSATION_TOKEN',
        }
        self.prepared = SimpleNamespace(
            conversation_id='2:10', message_id=MESSAGE_ID,
            as_send_kwargs=Mock(return_value=self.send_arguments))
        self.load_keys = self.enterContext(patch(
            'utils.xchat_crypto.load_key_material', return_value=self.keys))
        self.prepare = self.enterContext(patch(
            'utils.xchat_crypto.prepare_text_message', return_value=self.prepared))
        self.viewer = self.enterContext(patch('main.XAPI.get_viewer', return_value={
            'data': {'viewer': {'user_results': {'result': {'rest_id': '10'}}}},
        }))
        self.user = self.enterContext(patch('main.XAPI.get_user_info', return_value={
            'data': {'user': {'result': {
                'rest_id': '2', 'core': {'screen_name': 'recipient'},
            }}},
        }))
        self.members = self.enterContext(patch(
            'x_apis.x_dm_api.XChatAPI.get_users_by_ids', return_value={
                'data': {'get_member_results': {'results': [{
                    'id': '2', 'status': 'Found', 'member_results': {
                        'rest_id': '2', 'result': {
                            '__typename': 'User', 'rest_id': '2',
                            'core': {'screen_name': 'recipient'},
                            'chat_permissions': {
                                '__typename': 'UserDMPermissions', 'can_dm': True,
                            },
                        },
                    },
                }]}},
            }))
        response_event = base64.b64encode(encode_struct([
            (1, 11, '100'), (2, 11, MESSAGE_ID), (3, 11, '10'),
            (4, 11, '2:10'), (7, 12, [(1, 12, [(100, 11, b'ciphertext')])]),
        ])).decode('ascii')
        self.send = self.enterContext(patch(
            'x_apis.x_dm_api.XChatAPI.send_message', return_value={
                'data': {'xchat_send_create_message_event': {
                    'encoded_message_event': response_event,
                }},
            }))
        self.no_get = self.enterContext(patch(
            'builder.client.get', side_effect=AssertionError('Unexpected HTTP GET')))
        self.no_post = self.enterContext(patch(
            'builder.client.post', side_effect=AssertionError('Unexpected HTTP POST')))
        self.output = io.StringIO()
        self.logs = io.StringIO()
        self.log_sink = logger.add(self.logs, format='{message}')
        self.addCleanup(logger.remove, self.log_sink)

    def arguments(self, *, apply=False, receipt=True, extra=()):
        command = ['chat-send', '@Recipient', SECRET_TEXT, '--keys', 'local-test-keys.json']
        if apply:
            command.append('--apply')
        if receipt:
            command.extend(['--receipt', str(self.receipt_path)])
        command.extend(extra)
        return main.build_parser().parse_args(command)

    def run_command(self, args):
        with redirect_stdout(self.output), redirect_stderr(self.output):
            return main.cmd_chat_send(self.auth, args)

    def read_receipt(self):
        return json.loads(self.receipt_path.read_text())

    def assert_no_api_calls(self):
        self.viewer.assert_not_called()
        self.user.assert_not_called()
        self.members.assert_not_called()
        self.send.assert_not_called()
        self.no_get.assert_not_called()
        self.no_post.assert_not_called()

    def assert_no_sensitive_output(self):
        exposed = self.output.getvalue() + self.logs.getvalue()
        if self.receipt_path.exists():
            exposed += self.receipt_path.read_text()
        for secret in (SECRET_TEXT, SECRET_EVENT, SECRET_SIGNATURE,
                       'TEST_PRIVATE_CONVERSATION_TOKEN', 'TEST_PROXY_PASSWORD'):
            self.assertNotIn(secret, exposed)

    def test_default_preview_skips_auth_initialization_and_all_network(self):
        command = ['main.py', 'chat-send', '@Recipient', SECRET_TEXT,
                   '--keys', 'local-test-keys.json']
        with patch('sys.argv', command), patch('main.init') as init:
            with redirect_stdout(self.output), redirect_stderr(self.output):
                result = main.main()
        self.assertEqual(result, 0)
        init.assert_not_called()
        self.assert_no_api_calls()
        self.assertFalse(self.receipt_path.exists())
        summary = json.loads(self.output.getvalue())
        self.assertEqual(summary, {
            'recipient': 'recipient', 'sender_id': '10',
            'conversation_id': '2:10', 'message_id': MESSAGE_ID, 'stage': 'prepared',
        })
        self.assert_no_sensitive_output()

    def test_preview_reads_file_and_does_not_modify_receipt(self):
        text_file = Path(self.temp) / 'message.txt'
        text_file.write_text('  ' + SECRET_TEXT + '\n')
        self.receipt_path.write_text('existing receipt')
        args = self.arguments(extra=['--file', str(text_file)])
        self.assertEqual(self.run_command(args), 0)
        self.prepare.assert_called_once_with(SECRET_TEXT, self.keys)
        self.assertEqual(self.receipt_path.read_text(), 'existing receipt')
        self.assert_no_api_calls()
        self.assert_no_sensitive_output()

    def test_apply_requires_receipt_before_lookups_or_sending(self):
        self.assertEqual(self.run_command(self.arguments(apply=True, receipt=False)), 1)
        self.assert_no_api_calls()
        self.load_keys.assert_not_called()

    def test_empty_text_or_invalid_username_never_calls_api(self):
        for field, value in (('text', ''), ('username', 'invalid user!')):
            with self.subTest(field=field):
                args = self.arguments(apply=True)
                setattr(args, field, value)
                self.assertEqual(self.run_command(args), 1)
        self.assert_no_api_calls()
        self.assertFalse(self.receipt_path.exists())

    def test_wrong_sender_blocks_recipient_lookup_and_sending(self):
        self.viewer.return_value['data']['viewer']['user_results']['result']['rest_id'] = '11'
        self.assertEqual(self.run_command(self.arguments(apply=True)), 1)
        self.viewer.assert_called_once_with(self.auth)
        self.user.assert_not_called()
        self.send.assert_not_called()
        self.assertEqual(self.read_receipt()['stage'], 'blocked_sender_mismatch')

    def test_wrong_recipient_conversation_blocks_sending(self):
        self.user.return_value['data']['user']['result']['rest_id'] = '3'
        self.assertEqual(self.run_command(self.arguments(apply=True)), 1)
        self.send.assert_not_called()
        record = self.read_receipt()
        self.assertEqual(record['stage'], 'blocked_recipient_mismatch')
        self.assertEqual(record['recipient_id'], '3')

    def test_wrong_recipient_handle_blocks_even_with_matching_numeric_id(self):
        self.user.return_value['data']['user']['result']['core']['screen_name'] = 'someone_else'
        self.assertEqual(self.run_command(self.arguments(apply=True)), 1)
        self.send.assert_not_called()
        self.assertEqual(self.read_receipt()['stage'], 'blocked_handle_mismatch')

    def test_non_numeric_recipient_id_blocks_sending(self):
        self.user.return_value['data']['user']['result']['rest_id'] = 'not-a-user-id'
        self.assertEqual(self.run_command(self.arguments(apply=True)), 1)
        self.send.assert_not_called()
        self.assertEqual(self.read_receipt()['stage'], 'blocked_invalid_recipient')

    def test_false_unknown_or_nonboolean_permission_blocks_sending(self):
        user = self.members.return_value['data']['get_member_results']['results'][0][
            'member_results']['result']
        for value in (False, None, 'true', 1):
            with self.subTest(value=value):
                self.receipt_path = Path(self.temp) / f'permission-{type(value).__name__}.json'
                user['chat_permissions'] = {'can_dm': value}
                self.assertEqual(self.run_command(self.arguments(apply=True)), 1)
                self.assertEqual(self.read_receipt()['stage'], 'blocked_dm_permission')
        self.receipt_path = Path(self.temp) / 'permission-missing.json'
        user.pop('chat_permissions')
        self.assertEqual(self.run_command(self.arguments(apply=True)), 1)
        self.send.assert_not_called()

    def test_profile_permission_does_not_override_authoritative_chat_permission(self):
        user = self.user.return_value['data']['user']['result']
        user['legacy'] = {'can_dm': True}
        user['dm_permissions'] = {'can_dm': True}
        self.members.return_value['data']['get_member_results']['results'][0][
            'member_results']['result']['chat_permissions']['can_dm'] = False
        self.assertEqual(self.run_command(self.arguments(apply=True)), 1)
        self.send.assert_not_called()

    def test_chat_member_must_uniquely_match_ids_status_type_and_handle(self):
        original = self.members.return_value
        changes = (
            (('id',), '3'), (('status',), 'NotFound'),
            (('member_results', 'rest_id'), '3'),
            (('member_results', 'result', 'rest_id'), '3'),
            (('member_results', 'result', '__typename'), 'UserUnavailable'),
            (('member_results', 'result', 'core', 'screen_name'), 'someone_else'),
        )
        for index, (path, value) in enumerate(changes):
            with self.subTest(path=path):
                self.receipt_path = Path(self.temp) / f'member-{index}.json'
                payload = deepcopy(original)
                current = payload['data']['get_member_results']['results'][0]
                for key in path[:-1]:
                    current = current[key]
                current[path[-1]] = value
                self.members.return_value = payload
                self.assertEqual(self.run_command(self.arguments(apply=True)), 1)
                self.assertEqual(self.read_receipt()['stage'], 'blocked_chat_member_mismatch')
        for count in (0, 2):
            with self.subTest(count=count):
                self.receipt_path = Path(self.temp) / f'member-count-{count}.json'
                payload = deepcopy(original)
                entries = payload['data']['get_member_results']['results']
                payload['data']['get_member_results']['results'] = entries * count
                self.members.return_value = payload
                self.assertEqual(self.run_command(self.arguments(apply=True)), 1)
        self.send.assert_not_called()

    def test_chat_member_partial_graphql_errors_prevent_sending(self):
        self.members.return_value['errors'] = [{'message': 'TEST_PROXY_PASSWORD'}]
        self.assertEqual(self.run_command(self.arguments(apply=True)), 1)
        self.send.assert_not_called()
        self.assertEqual(self.read_receipt()['stage'], 'failed_before_send')
        self.assert_no_sensitive_output()

    def test_existing_receipt_blocks_all_api_calls_and_is_preserved(self):
        self.receipt_path.write_text('previous attempt marker')
        self.assertEqual(self.run_command(self.arguments(apply=True)), 1)
        self.assert_no_api_calls()
        self.assertEqual(self.receipt_path.read_text(), 'previous attempt marker')

    def test_success_sends_once_after_persisting_marker_and_records_only_metadata(self):
        def assert_marker_before_send(*args, **kwargs):
            record = self.read_receipt()
            self.assertEqual(record['stage'], 'sending')
            self.assertEqual(record['message_id'], MESSAGE_ID)
            self.assertEqual(record['recipient_id'], '2')
            return self.send.return_value

        self.send.side_effect = assert_marker_before_send
        self.assertEqual(self.run_command(self.arguments(apply=True)), 0)
        self.viewer.assert_called_once_with(self.auth)
        self.user.assert_called_once_with(self.auth, 'recipient')
        self.members.assert_called_once_with(self.auth, ['2'])
        self.send.assert_called_once_with(self.auth, **self.send_arguments)
        record = self.read_receipt()
        self.assertEqual(record['stage'], 'confirmed')
        self.assertEqual(record['receipt']['event_type'], 1)
        self.assertEqual(record['receipt']['message_id'], MESSAGE_ID)
        self.assertEqual(stat.S_IMODE(self.receipt_path.stat().st_mode), 0o600)
        self.assert_no_sensitive_output()

    def test_timeout_keeps_unknown_marker_and_reuse_cannot_send_again(self):
        self.send.side_effect = TimeoutError('proxy://user:TEST_PROXY_PASSWORD@host')
        args = self.arguments(apply=True)
        self.assertEqual(self.run_command(args), 1)
        record = self.read_receipt()
        self.assertEqual(record['stage'], 'unconfirmed')
        self.assertEqual(record['message_id'], MESSAGE_ID)
        self.assertEqual(record['error'], {'type': 'TimeoutError'})
        self.assertEqual(self.run_command(args), 1)
        self.send.assert_called_once()
        self.viewer.assert_called_once()
        self.user.assert_called_once()
        self.members.assert_called_once()
        self.assertEqual(self.read_receipt(), record)
        self.assert_no_sensitive_output()

    def test_preflight_failure_keeps_marker_and_only_safe_error_metadata(self):
        failure = RuntimeError('proxy://user:TEST_PROXY_PASSWORD@host')
        failure.response = SimpleNamespace(status_code=403)
        self.viewer.side_effect = failure
        self.assertEqual(self.run_command(self.arguments(apply=True)), 1)
        record = self.read_receipt()
        self.assertEqual(record['stage'], 'failed_before_send')
        self.assertEqual(record['error'], {'type': 'RuntimeError', 'status_code': 403})
        self.send.assert_not_called()
        self.assert_no_sensitive_output()

    def test_failed_marker_persistence_prevents_send(self):
        with patch('main._write_chat_receipt', side_effect=OSError('TEST_PROXY_PASSWORD')):
            self.assertEqual(self.run_command(self.arguments(apply=True)), 1)
        self.assert_no_api_calls()
        self.assertTrue(self.receipt_path.exists())
        self.assert_no_sensitive_output()


if __name__ == '__main__':
    unittest.main()
