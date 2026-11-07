"""PIN 生命周期离线测试：真实 SDK 密码学，模拟所有账户/realm 写请求。"""

import base64
from contextlib import redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import stat
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from chat_xdk import Chat

from utils import xchat_identity as identity
from utils.xchat_identity import sdk_call as real_sdk_call
from utils.xchat_codec import encode_struct
from utils.xchat_sdk_worker import execute
from x_apis.x_dm_api import XChatAPI, ADD_PUBLIC_KEY_QID


CONFIG = {'key_store_token_map_json': json.dumps({
    'realms': [{'id': 'ab' * 16, 'address': 'https://realm.invalid'}],
    'register_threshold': 1, 'recover_threshold': 1, 'pin_hashing_mode': 'Standard2019'}),
    'max_guess_count': 20,
    'token_map': [{'key': 'ab' * 16, 'value': {'token': 'SYNTHETIC_REALM_TOKEN'}}]}


def registry(entries, uid='123'):
    return {'data': {'user_results_by_rest_ids': [{'rest_id': uid, 'result': {
        '__typename': 'User', 'get_public_keys': {'__typename': 'GetPublicKeysResult',
        'is_managed_pin_user': False, 'public_keys_with_token_map': entries}}}]}}


class IdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.generated = execute({'action': 'generate'})

    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.directory) / 'identity.json'
        self.auth = SimpleNamespace(proxies={'https': 'http://user:TEST_PROXY_SECRET@proxy.invalid:8080'})
        self.entry = {'public_key_with_metadata': {'version': '100', 'public_key': self.generated['registration']},
                      'token_map': CONFIG}
        self.viewer = self.enterContext(patch('utils.xchat_identity.XAPI.get_viewer', return_value={
            'data': {'viewer': {'user_results': {'result': {'rest_id': '123', 'core': {'screen_name': 'test_user'}}}}}}))
        self.public = self.enterContext(patch('utils.xchat_identity.XChatAPI.get_public_keys', return_value=registry([])))
        self.add = self.enterContext(patch('utils.xchat_identity.XChatAPI.add_public_key', return_value={
            'version': '100', 'token_map': CONFIG}))
        self.sdk = self.enterContext(patch('utils.xchat_identity.sdk_call', return_value=copy.deepcopy(self.generated)))

    def write_state(self, phase='prepared', uid='123'):
        value = {'format': 'xapis-xchat-identity-v1', 'sender_id': uid, 'screen_name': 'test_user',
                 'phase': phase, 'request_version': '90', 'version': '100',
                 'registration': self.generated['registration'], 'key_blob_b64': self.generated['key_blob_b64']}
        identity._save(self.path, value)

    def test_status_is_read_only_and_no_pin_backup_assumption(self):
        result = identity.inspect_identity(self.auth)
        self.assertEqual(result['state'], 'needs_setup')
        self.assertEqual(result['pin_backup_state'], 'unknown')
        self.public.assert_called_once_with(self.auth, ['123'], include_juicebox_tokens=False)
        self.add.assert_not_called(); self.sdk.assert_not_called()

    def test_status_existing_public_key_is_not_missing_local_identity(self):
        self.public.return_value = registry([self.entry])
        self.assertEqual(identity.inspect_identity(self.auth)['state'], 'public_keys_registered')

    def test_wrong_viewer_response_never_registers(self):
        self.viewer.return_value = {'errors': [{'message': 'secret'}]}
        with self.assertRaises(identity.IdentityError):identity.setup_identity(self.auth, '6382', self.path)
        self.add.assert_not_called(); self.sdk.assert_not_called()

    def test_existing_remote_identity_never_replaced(self):
        self.public.return_value = registry([self.entry])
        with self.assertRaises(identity.IdentityError):identity.setup_identity(self.auth, '6382', self.path)
        self.add.assert_not_called(); self.sdk.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_bad_or_partial_registry_never_means_unregistered(self):
        for data in ({}, {'errors': [{'code': 32}]}, registry([], uid='999'),
                     registry([{'public_key_with_metadata': {'version': 'x'}}])):
            with self.subTest(data=data):
                self.public.return_value = data
                with self.assertRaises(identity.IdentityError):identity.setup_identity(self.auth, '6382', self.path)
        self.add.assert_not_called(); self.sdk.assert_not_called()

    def test_weak_pin_rejected_before_any_api(self):
        for pin in ('', '123', '1234', '4321', '0000', '１２３４'):
            with self.subTest(pin=pin), self.assertRaises(identity.IdentityError):
                identity.setup_identity(self.auth, pin, self.path)
        self.viewer.assert_not_called(); self.add.assert_not_called()

    def test_private_identity_saved_before_public_registration(self):
        def register(*args):
            current = identity._read(self.path)
            self.assertEqual(current['phase'], 'registration_attempted')
            self.assertEqual(current['key_blob_b64'], self.generated['key_blob_b64'])
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
            return {'version': '100', 'token_map': CONFIG}
        self.add.side_effect = register
        self.public.side_effect = [registry([]), registry([self.entry])]
        result = identity.setup_identity(self.auth, '6382', self.path)
        self.assertTrue(result['backup_verified'])
        self.add.assert_called_once()
        state = identity._read(self.path)
        self.assertEqual(state['phase'], 'ready')
        self.assertNotIn('pin', state); self.assertNotIn('token_map', state)
        self.assertNotIn('SYNTHETIC_REALM_TOKEN', self.path.read_text())
        self.assertEqual([c.args[0] for c in self.sdk.call_args_list], ['generate', 'setup'])
        self.assertEqual(self.sdk.call_args_list[-1].kwargs['proxies'], self.auth.proxies)

    def test_ambiguous_registration_persists_and_does_not_retry(self):
        self.add.side_effect = TimeoutError('unsafe credentials in transport error')
        with self.assertRaises(TimeoutError):identity.setup_identity(self.auth, '6382', self.path)
        self.assertEqual(identity._read(self.path)['phase'], 'registration_attempted')
        self.add.reset_mock()
        with self.assertRaises(identity.IdentityError):identity.setup_identity(self.auth, '6382', self.path, resume=True)
        self.add.assert_not_called()

    def test_resume_matches_remote_public_key_without_registering_again(self):
        self.write_state('registration_attempted')
        self.public.return_value = registry([self.entry])
        self.assertEqual(identity.setup_identity(self.auth, '6382', self.path, resume=True)['state'], 'ready')
        self.add.assert_not_called()

    def test_ambiguous_backup_only_recovers_never_resets_pin(self):
        self.write_state('backup_attempted')
        self.public.return_value = registry([self.entry])
        identity.setup_identity(self.auth, '6382', self.path, resume=True)
        self.assertEqual([c.args[0] for c in self.sdk.call_args_list], ['validate', 'unlock'])
        self.add.assert_not_called()

    def test_failed_backup_remains_reconcilable(self):
        def sdk(action, **kwargs):
            if action == 'setup':raise identity.IdentityError('sdk_error')
            return self.generated
        self.sdk.side_effect = sdk
        with self.assertRaises(identity.IdentityError):identity.setup_identity(self.auth, '6382', self.path)
        self.assertEqual(identity._read(self.path)['phase'], 'backup_attempted')
        self.assertEqual(self.add.call_count, 1)

    def test_resume_different_account_blocks_before_sdk(self):
        self.write_state(uid='999')
        with self.assertRaises(identity.IdentityError):identity.setup_identity(self.auth, '6382', self.path, resume=True)
        self.sdk.assert_not_called(); self.add.assert_not_called()

    def test_wrong_recovered_key_not_ready(self):
        self.public.side_effect = [registry([]), registry([self.entry])]
        def sdk(action, **kwargs):
            if action == 'setup':return {**self.generated, 'public_key': 'wrong'}
            return self.generated
        self.sdk.side_effect = sdk
        with self.assertRaises(identity.IdentityError):identity.setup_identity(self.auth, '6382', self.path)
        self.assertNotEqual(identity._read(self.path)['phase'], 'ready')

    def test_unlock_requires_explicit_version_when_ambiguous(self):
        older = copy.deepcopy(self.entry); older['public_key_with_metadata']['version'] = '99'
        self.public.return_value = registry([self.entry, older])
        with self.assertRaises(identity.IdentityError):identity.unlock_identity(self.auth, '6382', self.path)
        self.sdk.assert_not_called()
        identity.unlock_identity(self.auth, '6382', self.path, version='99')
        self.assertEqual(identity._read(self.path)['version'], '99')
        self.add.assert_not_called()

    def test_unlock_failure_does_not_create_identity_or_retry(self):
        self.public.return_value = registry([self.entry])
        self.sdk.side_effect = identity.IdentityError('invalid_pin remaining 2')
        with self.assertRaises(identity.IdentityError):identity.unlock_identity(self.auth, '6382', self.path)
        self.sdk.assert_called_once(); self.add.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_unlock_does_not_overwrite_existing_private_file(self):
        self.write_state();before=self.path.read_bytes()
        with self.assertRaises(identity.IdentityError):identity.unlock_identity(self.auth, '6382', self.path)
        self.assertEqual(before, self.path.read_bytes());self.sdk.assert_not_called()

    def test_lock_blocks_competing_mutation(self):
        with identity._locked(self.path):
            with self.assertRaises(identity.IdentityError):identity.setup_identity(self.auth, '6382', self.path)
        self.add.assert_not_called();self.sdk.assert_not_called()

    def test_state_file_permissions_and_symlink_rejected(self):
        self.write_state(); self.path.chmod(0o644)
        with self.assertRaises(identity.IdentityError):identity._read(self.path)
        self.path.chmod(0o600);link=Path(self.directory)/'link';link.symlink_to(self.path)
        with self.assertRaises(OSError):identity._read(link)

    def test_bad_realm_config_not_sent_to_sdk(self):
        self.add.return_value = {'version': '100', 'token_map': {'token_map': []}}
        with self.assertRaises(identity.IdentityError):identity.setup_identity(self.auth, '6382', self.path)
        self.assertEqual([c.args[0] for c in self.sdk.call_args_list], ['generate'])

    def test_unknown_journal_phase_cannot_trigger_backup(self):
        self.write_state('unexpected')
        self.public.return_value=registry([self.entry])
        with self.assertRaises(identity.IdentityError):identity.setup_identity(self.auth,'6382',self.path,resume=True)
        self.sdk.assert_not_called();self.add.assert_not_called()

    def test_version_drift_after_backup_attempt_never_tries_another_pin_slot(self):
        self.write_state('backup_attempted')
        changed=copy.deepcopy(self.entry);changed['public_key_with_metadata']['version']='101'
        self.public.return_value=registry([changed])
        with self.assertRaises(identity.IdentityError):identity.setup_identity(self.auth,'6382',self.path,resume=True)
        self.assertEqual([c.args[0] for c in self.sdk.call_args_list],['validate'])

    def test_end_to_end_existing_conversation_to_send_key_file(self):
        self.write_state('ready')
        chat=Chat();chat.import_keys(base64.b64decode(self.generated['key_blob_b64']),'100');chat.set_identity('123','100')
        prepared=chat.prepare_conversation_key_change([
            {'user_id':'123','public_key':self.generated['registration']['public_key'],'key_version':'100'}],conversation_id='123:123')
        sig=prepared['action_signatures'][0]
        signature=encode_struct([(1,11,sig['signature']),(2,11,sig['public_key_version']),(3,11,sig['signature_version'])])
        event=(encode_struct([(1,11,'123'),(2,11,sig['message_id']),(3,11,'123'),(4,11,'123:123'),(6,11,'1234')])[:-1]
               +struct.pack('>bh',12,7)+base64.b64decode(sig['encoded_message_event_detail'])
               +struct.pack('>bh',12,9)+signature+b'\0')
        payload={'data':{'get_conversation_page':{'encoded_message_events':[base64.b64encode(event).decode()],
                                                'missing_conversation_key_change_events':[]}}}
        self.public.return_value=registry([self.entry])
        self.sdk.side_effect=real_sdk_call
        output=Path(self.directory)/'conversation.json'
        with patch('utils.xchat_identity.XChatAPI.get_conversation_page',return_value=payload):
            result=identity.export_conversation_material(self.auth,self.path,'123:123',output,signature_version=7)
        self.assertEqual(result['state'],'conversation_ready')
        from utils.xchat_crypto import load_key_material,prepare_text_message
        material=load_key_material(output)
        self.assertEqual(material.conversation_key,prepared['conversation_key'])
        self.assertEqual(stat.S_IMODE(output.stat().st_mode),0o600)
        self.assertTrue(prepare_text_message('local test',material).encoded_message_create_event)
        self.add.assert_not_called()


class RealSdkTests(unittest.TestCase):
    def test_generate_import_and_binding_with_real_sdk_subprocess(self):
        generated = identity.sdk_call('generate')
        imported = identity.sdk_call('validate', key_blob_b64=generated['key_blob_b64'], version='100')
        self.assertEqual(generated['public_key'], imported['public_key'])
        self.assertEqual(generated['signing_public_key'], imported['signing_public_key'])
        self.assertTrue(Chat().verify_key_binding(generated['public_key'], generated['signing_public_key'],
                                                generated['registration']['identity_public_key_signature']))

    def test_proxy_isolated_in_child_and_secrets_not_in_argv(self):
        response = SimpleNamespace(returncode=0, stdout='{"ok":true,"result":{}}')
        before = dict(os.environ)
        with patch('utils.xchat_identity.subprocess.run', return_value=response) as run:
            identity.sdk_call('unlock', proxies={'https':'http://user:SECRET@proxy.invalid:8080'}, pin='6382')
        self.assertEqual(os.environ, before)
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs['env']['HTTPS_PROXY'], 'http://user:SECRET@proxy.invalid:8080')
        self.assertEqual(kwargs['env']['NO_PROXY'], '')
        self.assertNotIn('6382', str(run.call_args.args))
        self.assertNotIn('SECRET', str(run.call_args.args))

    def test_worker_error_redacted(self):
        response = SimpleNamespace(returncode=1, stdout='{"ok":false,"error":"secret-token-here"}')
        with patch('utils.xchat_identity.subprocess.run', return_value=response):
            with self.assertRaises(identity.IdentityError) as caught:identity.sdk_call('unlock', pin='6382')
        self.assertNotIn('secret-token-here', str(caught.exception))
        self.assertNotIn('6382', str(caught.exception))

    def test_real_sdk_unwraps_signed_conversation_key(self):
        chat = Chat(); reg = chat.generate_keypairs(); chat.set_identity('1', '100')
        prepared = chat.prepare_conversation_key_change([
            {'user_id':'1','public_key':reg.public_key.public_key,'key_version':'100'}],conversation_id='1:1')
        signature = prepared['action_signatures'][0]
        sig = encode_struct([(1,11,signature['signature']),(2,11,signature['public_key_version']),
                             (3,11,signature['signature_version'])])
        event = (encode_struct([(1,11,'123'),(2,11,signature['message_id']),(3,11,'1'),(4,11,'1:1'),(6,11,'1234')])[:-1]
                 + struct.pack('>bh',12,7) + base64.b64decode(signature['encoded_message_event_detail'])
                 + struct.pack('>bh',12,9) + sig + b'\0')
        signing_keys=[{'user_id':'1','public_key_version':'100','public_key':reg.public_key.signing_public_key,
                       'identity_public_key':reg.public_key.public_key,
                       'identity_public_key_signature':reg.public_key.identity_public_key_signature}]
        result = identity.sdk_call('conversation_keys', key_blob_b64=base64.b64encode(bytes(chat.export_keys())).decode(),
            version='100',sender_id='1',signing_keys=signing_keys,events=[base64.b64encode(event).decode()])
        self.assertEqual(base64.b64decode(result['keys'][result['latest_version']]), prepared['conversation_key'])
        with self.assertRaises(identity.IdentityError):
            identity.sdk_call('conversation_keys',key_blob_b64=base64.b64encode(bytes(chat.export_keys())).decode(),
                version='100',sender_id='1',signing_keys=[],events=[base64.b64encode(event).decode()])


class RegistrationTransportTests(unittest.TestCase):
    def test_native_graphql_contract_and_no_retry(self):
        generated = execute({'action':'generate'})
        auth=SimpleNamespace(bearer='test',ct0='test',cookie={},proxies={'https':'http://proxy.invalid'},
                             is_logged_in=True, client_transaction_id=Mock(return_value='id'))
        response=Mock();response.json.return_value={'data':{'user_add_public_key':{'version':'100','token_map':CONFIG}}}
        with patch('x_apis.x_dm_api.client.post',return_value=response) as post:
            result=XChatAPI.add_public_key(auth,generated['registration'],'90')
        self.assertEqual(result['version'],'100')
        self.assertIn(ADD_PUBLIC_KEY_QID,post.call_args.args[0])
        body=json.loads(post.call_args.kwargs['json']['variables'])
        self.assertEqual(body['public_key']['registration_method'],'CustomPin')
        self.assertTrue(body['generate_version'])
        self.assertEqual(post.call_count,1)

    def test_graphql_or_business_failure_never_accepted(self):
        generated=execute({'action':'generate'})
        auth=SimpleNamespace(bearer='test',ct0='test',cookie={},proxies=None,is_logged_in=True,
                             client_transaction_id=Mock(return_value='id'))
        for payload in ({'errors':[{'message':'PRIVATE_TOKEN'}]},
                        {'data':{'user_add_public_key':{'error_code':'RateLimit','version':'100','token_map':CONFIG}}},
                        {'data':{'user_add_public_key':{'version':'100'}}}):
            response=Mock();response.json.return_value=payload
            with patch('x_apis.x_dm_api.client.post',return_value=response) as post:
                with self.assertRaises(RuntimeError) as caught:XChatAPI.add_public_key(auth,generated['registration'],'90')
            self.assertNotIn('PRIVATE_TOKEN',str(caught.exception));self.assertEqual(post.call_count,1)
        with patch('x_apis.x_dm_api.client.post',side_effect=TimeoutError) as post:
            with self.assertRaises(TimeoutError):XChatAPI.add_public_key(auth,generated['registration'],'90')
        self.assertEqual(post.call_count,1)


class IdentityCliTests(unittest.TestCase):
    def test_setup_preview_does_not_read_pin_or_register(self):
        import main
        args=main.build_parser().parse_args(['chat-setup','--state','private.json'])
        with patch('utils.xchat_identity.inspect_identity',return_value={'state':'needs_setup'}), \
             patch('main._chat_pin') as pin, patch('utils.xchat_identity.setup_identity') as setup, redirect_stdout(io.StringIO()):
            self.assertEqual(main.cmd_chat_identity(object(),args),0)
        pin.assert_not_called();setup.assert_not_called()

    def test_registered_account_rejected_before_pin_input(self):
        import main
        args=main.build_parser().parse_args(['chat-setup','--state','private.json','--apply'])
        with patch('utils.xchat_identity.inspect_identity',return_value={'state':'public_keys_registered'}), \
             patch('main._chat_pin') as pin, patch('utils.xchat_identity.setup_identity') as setup:
            self.assertEqual(main.cmd_chat_identity(object(),args),1)
        pin.assert_not_called();setup.assert_not_called()

    def test_no_pin_cli_argument(self):
        import main
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), patch('sys.stderr',io.StringIO()):
            main.build_parser().parse_args(['chat-setup','--state','private.json','--pin','6382'])


if __name__ == '__main__':
    unittest.main()
