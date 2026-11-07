"""新会话协议与失败恢复：真实 SDK，所有网络写入由测试替身承接。"""
import base64
import copy
import json
from pathlib import Path
import stat
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from chat_xdk import Chat
from utils import xchat_conversation as conversation
from utils.xchat_identity import IdentityError, _save
from utils.xchat_sdk_worker import execute
from utils.xchat_codec import encode_struct
from utils.xchat_crypto import load_key_material, prepare_message_request, message_request_text
from x_apis.x_dm_api import XChatAPI, ADD_CONVERSATION_KEYS_QID
from tests.test_xchat_identity import registry


def encoded_key_event(prepared):
    sig = prepared['action_signatures'][0]
    signature = encode_struct([(1,11,sig['signature']), (2,11,sig['public_key_version']),
                               (3,11,sig['signature_version'])])
    header = encode_struct([(1,11,'900'), (2,11,sig['message_id']), (3,11,'123'),
                            (4,11,'123:456'), (6,11,'1234')])[:-1]
    return base64.b64encode(header + struct.pack('>bh',12,7)
                           + base64.b64decode(sig['encoded_message_event_detail'])
                           + struct.pack('>bh',12,9) + signature + b'\0').decode()


class NewConversationTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.identity = self.directory / 'identity.json'
        self.output = self.directory / 'keys.json'
        self.receipt = self.directory / 'receipt.json'
        self.generated = {uid: execute({'action':'generate'}) for uid in ('123','456')}
        own = self.generated['123']
        _save(self.identity, {'format':'xapis-xchat-identity-v1', 'phase':'ready',
                             'sender_id':'123', 'version':'100', 'registration':own['registration'],
                             'key_blob_b64':own['key_blob_b64']})
        self.auth = SimpleNamespace(proxies=None, cookie={})
        self.enterContext(patch.object(XChatAPI, 'get_public_keys', side_effect=lambda auth, ids, **kw:
            registry([{'public_key_with_metadata': {'version':'100',
                       'public_key':self.generated[ids[0]]['registration']}}], ids[0])))
        self.enterContext(patch('utils.xchat_identity.XAPI.get_viewer', return_value={'data':{
            'viewer':{'user_results':{'result':{'rest_id':'123','core':{'screen_name':'sender'}}}}}}))
        self.enterContext(patch('utils.xchat_conversation.XAPI.get_user_info', return_value={'data':{
            'user':{'result':{'rest_id':'456','core':{'screen_name':'recipient'}}}}}))
        self.member = {'rest_id':'456','__typename':'User','core':{'screen_name':'recipient'},
                       'chat_permissions':{'can_dm':True,'can_dm_on_xchat':True}}
        self.members = self.enterContext(patch.object(XChatAPI, 'get_users_by_ids', return_value={'data':{
            'get_member_results':{'results':[{'id':'456','status':'Found','member_results':{
                'rest_id':'456','result':self.member}}]}}}))
        self.events = []
        self.page = self.enterContext(patch.object(XChatAPI, 'get_conversation_page', side_effect=lambda *args:
            {'data':{'get_conversation_page':{'encoded_message_events':list(self.events),
                                             'has_more':False}}}))
        self.post = self.enterContext(patch.object(XChatAPI, 'add_conversation_keys', side_effect=self.submit))

    def submit(self, auth, prepared):
        self.prepared = prepared
        # Prove persistence precedes mutation and recipient can unwrap its copy.
        record = json.loads(self.receipt.read_text())
        self.assertEqual(record['stage'], 'submitting')
        other = Chat()
        other.import_keys(base64.b64decode(self.generated['456']['key_blob_b64']), '100')
        recipient_key = next(k for k in prepared['participant_keys'] if k['user_id']=='456')
        self.assertEqual(other.decrypt_conversation_key(recipient_key['encrypted_key']),
                         base64.b64decode(prepared['conversation_key_b64']))
        self.events.append(encoded_key_event(prepared))
        return {'conversation_key_change_sequence_id':'900','error_code':None}

    def start(self, **kwargs):
        return conversation.start_conversation(self.auth, 'recipient', self.identity, self.output,
                                               self.receipt, **kwargs)

    def test_real_crypto_create_readback_export_and_resume_without_second_write(self):
        self.assertEqual(self.start(apply=True)['state'], 'conversation_ready')
        material = load_key_material(self.output)
        self.assertEqual(material.conversation_key, base64.b64decode(self.prepared['conversation_key_b64']))
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertEqual(self.start(apply=True, resume=True)['state'], 'conversation_ready')
        self.assertEqual(self.post.call_count, 1)
        self.assertNotIn(self.prepared['conversation_key_b64'], self.receipt.read_text())

    def test_preview_does_not_prepare_or_create(self):
        self.assertEqual(self.start()['state'], 'eligible')
        self.post.assert_not_called()
        self.assertFalse(self.receipt.exists())

    def test_closed_inbox_blocked_before_key_creation(self):
        self.member['chat_permissions'].update(can_dm=False, can_dm_reason='InboxClosed')
        self.assertEqual(self.start()['reason'], 'InboxClosed')
        with self.assertRaisesRegex(IdentityError, 'InboxClosed'): self.start(apply=True)
        self.post.assert_not_called()
        self.assertFalse(self.receipt.exists())

    def test_xchat_disallowed_even_if_legacy_dm_allowed(self):
        self.member['chat_permissions']['can_dm_on_xchat'] = False
        self.member['chat_permissions']['can_dm_reason'] = 'Allowed'
        self.assertEqual(self.start()['state'], 'message_request_only')
        with self.assertRaises(IdentityError): self.start(apply=True)
        self.post.assert_not_called()

    def test_bad_member_and_missing_permission_block(self):
        for change in ({'rest_id':'999'}, {'core':{'screen_name':'wrong'}}, {'chat_permissions':{}}):
            original = copy.deepcopy(self.member)
            self.member.update(change)
            with self.assertRaises(IdentityError): self.start(apply=True)
            self.member.clear(); self.member.update(original)
        self.post.assert_not_called()

    def test_partial_read_never_interpreted_as_empty_conversation(self):
        self.page.side_effect = None
        for payload in ({}, {'errors':[{}]}, {'data':{'get_conversation_page':{
                'encoded_message_events':[], 'has_more':True}}}):
            self.page.return_value = payload
            with self.assertRaises(IdentityError): self.start(apply=True)
        self.post.assert_not_called()

    def test_existing_conversation_is_not_rotated(self):
        self.start(apply=True)
        self.post.reset_mock()
        with self.assertRaisesRegex(IdentityError, '已有事件'):
            conversation.start_conversation(self.auth, 'recipient', self.identity,
                self.directory/'other-keys.json', self.directory/'other-receipt.json', apply=True)
        self.post.assert_not_called()

    def test_timeout_after_acceptance_reconciles_original_event(self):
        def timeout(auth, prepared):
            self.submit(auth, prepared)
            raise TimeoutError()
        self.post.side_effect = timeout
        with self.assertRaises(TimeoutError): self.start(apply=True)
        self.assertEqual(json.loads(self.receipt.read_text())['stage'], 'submitting')
        self.assertEqual(self.start(apply=True, resume=True)['state'], 'conversation_ready')
        self.assertEqual(self.post.call_count, 1)

    def test_unknown_timeout_without_event_never_retries(self):
        self.post.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError): self.start(apply=True)
        with self.assertRaisesRegex(IdentityError, '未确认'): self.start(apply=True, resume=True)
        with self.assertRaisesRegex(IdentityError, '禁止重复'): self.start(apply=True)
        self.assertEqual(self.post.call_count, 1)
        self.assertFalse(self.output.exists())

    def test_changed_key_hash_prevents_export(self):
        def changed(auth, prepared):
            response = self.submit(auth, prepared)
            record = json.loads(self.receipt.read_text())
            record['key_sha256'] = '0'*64
            _save(self.receipt, record)
            raise TimeoutError()
        self.post.side_effect = changed
        with self.assertRaises(TimeoutError): self.start(apply=True)
        with self.assertRaisesRegex(IdentityError, '不一致'): self.start(apply=True, resume=True)
        self.assertFalse(self.output.exists())

    def test_path_collision_preserves_identity(self):
        before = self.identity.read_bytes()
        with self.assertRaises(IdentityError):
            conversation.start_conversation(self.auth,'recipient',self.identity,self.identity,self.receipt,apply=True)
        self.assertEqual(self.identity.read_bytes(), before)
        self.post.assert_not_called()

    def test_invalid_public_key_binding_blocks_submission(self):
        self.generated['456']['registration']['identity_public_key_signature'] = 'AA=='
        with self.assertRaises(IdentityError): self.start(apply=True)
        self.post.assert_not_called()


class NewConversationTransportTests(unittest.TestCase):
    def setUp(self):
        own, other = execute({'action':'generate'}), execute({'action':'generate'})
        self.prepared = execute({'action':'prepare_conversation', 'key_blob_b64':own['key_blob_b64'],
            'version':'100','sender_id':'123','public_keys':[
                {'user_id':uid,'key_version':'100',**key['registration']}
                for uid,key in [('123',own),('456',other)]]})
        self.enterContext(patch('x_apis.x_dm_api._external_headers',return_value={}))
        self.post = self.enterContext(patch('x_apis.x_dm_api.client.post'))
        self.response = self.post.return_value
        self.auth = SimpleNamespace(cookie={},proxies=None)

    def test_native_graphql_contract(self):
        self.response.json.return_value={'data':{'xchat_add_encrypted_conversation_key':{
            'error_code':None,'conversation_key_change_sequence_id':'900'}}}
        XChatAPI.add_conversation_keys(self.auth,self.prepared)
        self.assertIn(ADD_CONVERSATION_KEYS_QID,self.post.call_args.args[0])
        variables=json.loads(self.post.call_args.kwargs['json']['variables'])
        self.assertEqual(variables['conversation_id'],'123:456')
        self.assertEqual(set(variables['conversation_participant_keys'][0]),
                         {'user_id','public_key_version','encrypted_conversation_key'})
        self.assertIn('message_event_signature',variables['action_signatures'][0])
        self.assertNotIn('conversation_key_b64',str(variables))

    def test_bad_receipts_never_succeed_or_retry(self):
        for payload in ({}, {'errors':[{}]}, {'data':{'xchat_add_encrypted_conversation_key':{
                'error_code':'Denied','conversation_key_change_sequence_id':'900'}}},
                {'data':{'xchat_add_encrypted_conversation_key':{'error_code':None}}}):
            self.post.reset_mock()
            self.response.json.return_value=payload
            with self.assertRaises(RuntimeError): XChatAPI.add_conversation_keys(self.auth,self.prepared)
            self.assertEqual(self.post.call_count,1)

    def test_wrong_participants_rejected_before_network(self):
        self.prepared['participant_keys'][1]['user_id']='789'
        with self.assertRaises(ValueError): XChatAPI.add_conversation_keys(self.auth,self.prepared)
        self.post.assert_not_called()


class FirstRequestTests(unittest.TestCase):
    setUp = NewConversationTests.setUp
    submit = NewConversationTests.submit

    def request(self, **kwargs):
        return conversation.send_first_request(self.auth, 'recipient', '新会话测试', self.receipt, **kwargs)

    def accept(self, auth, **kwargs):
        self.assertTrue(kwargs['allow_unencrypted'])
        self.assertIsNone(kwargs['encoded_message_event_signature'])
        self.assertEqual(message_request_text(kwargs['encoded_message_create_event']), '新会话测试')
        record = json.loads(self.receipt.read_text())
        self.assertEqual(record['message_id'], kwargs['message_id'])
        self.assertEqual(record['stage'], 'sending')
        header = encode_struct([(1,11,'901'),(2,11,kwargs['message_id']),
                                (3,11,'123'),(4,11,'123:456')])[:-1]
        create = base64.b64decode(kwargs['encoded_message_create_event'])
        side_effects = encode_struct([(111,15,(12,[[(1,11,'server metadata')]]))])[:-1]
        event = base64.b64encode(header + struct.pack('>bhbh',12,7,12,1)
            + create[:-1] + side_effects + b'\0\0\0').decode()
        self.events.append(event)
        return {'data':{'xchat_send_create_message_event':{'encoded_message_event':event}}}

    def test_allowed_request_without_encrypted_permission_roundtrips(self):
        self.member['chat_permissions'].update(can_dm_on_xchat=False, can_dm_reason='Allowed')
        with patch.object(XChatAPI,'send_message',side_effect=self.accept) as send:
            result = self.request(apply=True)
            self.assertEqual(result['stage'],'verified')
            self.assertFalse(result['encrypted'])
            self.assertNotIn('新会话测试',self.receipt.read_text())
            self.assertEqual(self.request(apply=True,resume=True)['stage'],'verified')
            self.assertEqual(send.call_count,1)
        self.post.assert_not_called()

    def test_ambiguous_send_only_readbacks(self):
        def timeout(auth,**kwargs):
            self.accept(auth,**kwargs)
            raise TimeoutError()
        with patch.object(XChatAPI,'send_message',side_effect=timeout) as send:
            with self.assertRaises(TimeoutError): self.request(apply=True)
            with self.assertRaises(IdentityError): self.request(apply=True)
            self.assertEqual(self.request(apply=True,resume=True)['stage'],'verified')
            self.assertEqual(send.call_count,1)

    def test_unknown_send_does_not_retry(self):
        with patch.object(XChatAPI,'send_message',side_effect=TimeoutError()) as send:
            with self.assertRaises(TimeoutError): self.request(apply=True)
            with self.assertRaises(IdentityError): self.request(apply=True,resume=True)
            self.assertEqual(send.call_count,1)

    def test_preexisting_conversation_blocks_plaintext(self):
        self.events.append(encoded_key_event(execute({'action':'prepare_conversation',
            'key_blob_b64':self.generated['123']['key_blob_b64'],'version':'100','sender_id':'123',
            'public_keys':[{'user_id':uid,'key_version':'100',**self.generated[uid]['registration']}
                           for uid in ('123','456')]})))
        with patch.object(XChatAPI,'send_message') as send:
            with self.assertRaisesRegex(IdentityError,'已有事件'): self.request(apply=True)
            send.assert_not_called()

    def test_closed_inbox_still_blocks_plaintext(self):
        self.member['chat_permissions'].update(can_dm=False,can_dm_reason='InboxClosed')
        with patch.object(XChatAPI,'send_message') as send:
            with self.assertRaisesRegex(IdentityError,'InboxClosed'): self.request(apply=True)
            send.assert_not_called()

    def test_unsigned_requires_explicit_plaintext_choice(self):
        prepared=prepare_message_request('新会话测试','123:456')
        with patch('x_apis.x_dm_api.client.post') as post:
            with self.assertRaises(ValueError): XChatAPI.send_message(self.auth,**prepared.as_send_kwargs())
            post.assert_not_called()
            encrypted=base64.b64encode(encode_struct([(100,11,b'cipher'),(101,11,'100')])).decode()
            arguments=prepared.as_send_kwargs()
            arguments['encoded_message_create_event']=encrypted
            with self.assertRaises(ValueError): XChatAPI.send_message(self.auth,**arguments,allow_unencrypted=True)
            post.assert_not_called()

    def test_native_unsigned_transport(self):
        prepared=prepare_message_request('新会话测试','123:456')
        with patch('x_apis.x_dm_api._external_headers',return_value={}), patch('x_apis.x_dm_api.client.post') as post:
            header=encode_struct([(1,11,'901'),(2,11,prepared.message_id),(3,11,'123'),(4,11,'123:456')])[:-1]
            event=base64.b64encode(header+struct.pack('>bhbh',12,7,12,1)
                +base64.b64decode(prepared.encoded_message_create_event)+b'\0\0').decode()
            post.return_value.json.return_value={'data':{'xchat_send_create_message_event':{'encoded_message_event':event}}}
            XChatAPI.send_message(self.auth,**prepared.as_send_kwargs(),allow_unencrypted=True)
            variables=json.loads(post.call_args.kwargs['json']['variables'])
            self.assertIsNone(variables['encoded_message_event_signature'])
            self.assertEqual(message_request_text(variables['encoded_message_create_event']),'新会话测试')


if __name__ == '__main__':
    unittest.main()
