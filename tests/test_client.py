"""Public SDK acceptance: business calls, real crypto, mocked remote side effects."""
import base64
import copy
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from x_apis import XClient, XClientError
from x_apis.x_dm_api import XChatAPI
from utils.xchat_codec import encode_struct
from utils.xchat_conversation import read_conversation_events
from utils.xchat_identity import IdentityError, _save
from utils.xchat_sdk_worker import execute
from tests import test_xchat_conversation as conversation_tests
from tests.test_xchat_conversation import encoded_key_event
from tests.test_xchat_identity import registry


class MessagesClientTests(unittest.TestCase):
    setUp = conversation_tests.NewConversationTests.setUp
    submit = conversation_tests.NewConversationTests.submit

    def client(self):
        return XClient(self.auth,state_dir=self.directory/'state',identity_path=self.identity,default_pin='6382')

    def accept_message(self, auth, **kwargs):
        header=encode_struct([(1,11,str(901+len(self.events))),(2,11,kwargs['message_id']),
                             (3,11,'123'),(4,11,'123:456'),(6,11,'1234')])[:-1]
        event=header+struct.pack('>bhbh',12,7,12,1)+base64.b64decode(kwargs['encoded_message_create_event'])+b'\0'
        if kwargs['encoded_message_event_signature']:
            event+=struct.pack('>bh',12,9)+base64.b64decode(kwargs['encoded_message_event_signature'])
        event=base64.b64encode(event+b'\0').decode()
        self.events.insert(0,event)
        return {'data':{'xchat_send_create_message_event':{'encoded_message_event':event}}}

    def plaintext(self):
        self.member['chat_permissions'].update(can_dm_on_xchat=False,can_dm_reason='Allowed')

    def test_first_and_followup_use_identical_business_api(self):
        self.plaintext();client=self.client()
        with patch.object(XChatAPI,'send_message',side_effect=self.accept_message) as send:
            first=client.messages.send('recipient','你好',request_id='first')
            second=client.messages.send('recipient','第二条',request_id='second')
            self.assertEqual(first['status'],'verified');self.assertEqual(second['status'],'verified')
            self.assertEqual(send.call_count,2)
            self.assertEqual(client.messages.send('recipient','第二条',request_id='second'),second)
            self.assertEqual(send.call_count,2)
        messages=client.messages.list('recipient')
        self.assertEqual([x['text'] for x in messages['items']],['第二条','你好'])
        self.assertEqual(client.messages.list('recipient',limit=1)['next_cursor'],messages['items'][0]['sequence_id'])
        records=list((self.directory/'state'/'123'/'requests').glob('*.json'))
        self.assertEqual(len(records),2)
        self.assertTrue(all('你好' not in p.read_text() and '第二条' not in p.read_text() for p in records))

    def test_idempotency_conflict_blocks_before_send(self):
        self.plaintext();client=self.client()
        with patch.object(XChatAPI,'send_message',side_effect=self.accept_message) as send:
            client.messages.send('recipient','one',request_id='stable')
            with self.assertRaises(XClientError) as caught:client.messages.send('recipient','two',request_id='stable')
            self.assertEqual(caught.exception.code,'request_conflict');self.assertEqual(send.call_count,1)

    def test_unknown_submission_is_read_back_not_retried(self):
        self.plaintext();client=self.client()
        def ambiguous(auth,**kwargs):
            self.accept_message(auth,**kwargs)
            raise TimeoutError('SECRET body or URL')
        with patch.object(XChatAPI,'send_message',side_effect=ambiguous) as send:
            with self.assertRaises(XClientError) as caught:client.messages.send('recipient','one',request_id='stable')
            self.assertEqual(caught.exception.code,'delivery_unknown')
            self.assertNotIn('SECRET',str(caught.exception))
            self.assertEqual(client.messages.reconcile('stable')['status'],'verified')
            self.assertEqual(send.call_count,1)

    def test_unknown_without_server_event_does_not_resubmit(self):
        self.plaintext();client=self.client()
        with patch.object(XChatAPI,'send_message',side_effect=TimeoutError()) as send:
            with self.assertRaises(XClientError):client.messages.send('recipient','one',request_id='stable')
            self.assertEqual(client.messages.send('recipient','one',request_id='stable')['status'],'unconfirmed')
            self.assertEqual(send.call_count,1)

    def test_closed_inbox_has_machine_readable_error(self):
        self.member['chat_permissions'].update(can_dm=False,can_dm_reason='InboxClosed')
        with patch.object(XChatAPI,'send_message') as send:
            with self.assertRaises(XClientError) as caught:self.client().messages.send('recipient','one',request_id='id')
            self.assertEqual(caught.exception.code,'dm_not_allowed');send.assert_not_called()

    def test_reconcile_only_decodes_the_target_message(self):
        self.plaintext();client=self.client()
        with patch.object(XChatAPI,'send_message',side_effect=self.accept_message):
            client.messages.send('recipient','one',request_id='target',verify=False)
        unrelated=encode_struct([(1,11,'800'),(2,11,'unrelated'),(3,11,'456'),(4,11,'123:456'),
                                 (7,12,[(1,12,[(100,11,b'old ciphertext'),(101,11,'1')])])])
        self.events.append(base64.b64encode(unrelated).decode())
        self.assertEqual(client.messages.reconcile('target')['status'],'verified')

    def test_encrypted_creation_followup_and_verified_reading(self):
        client=self.client()
        def distribute(auth,prepared):
            self.events.append(encoded_key_event(prepared))
            return {'conversation_key_change_sequence_id':'900'}
        self.post.side_effect=distribute
        with patch.object(XChatAPI,'send_message',side_effect=self.accept_message) as send:
            result=client.messages.send('recipient','加密文字',request_id='new')
            self.assertTrue(result['encrypted']);self.assertEqual(result['status'],'verified')
            self.assertIsNotNone(send.call_args.kwargs['encoded_message_event_signature'])
            self.assertEqual(self.post.call_count,1)
            client.messages.send('recipient','再次发送',request_id='again')
            self.assertEqual(self.post.call_count,1)
        items=client.messages.list('recipient')['items']
        self.assertEqual(items[0]['text'],'再次发送');self.assertTrue(items[0]['signature_verified'])
        with self.assertRaises(XClientError) as caught:
            client.messages.send('recipient','不可降级',request_id='downgrade',mode='plaintext')
        self.assertEqual(caught.exception.code,'encryption_downgrade')

    def test_empty_sender_registry_triggers_auto_setup(self):
        self.plaintext();client=self.client()
        with patch('x_apis.messages.public_records',return_value=[]),patch.object(client.messages,'ensure_identity') as setup,\
             patch.object(XChatAPI,'send_message',side_effect=self.accept_message):
            client.messages.send('recipient','one',request_id='new')
            setup.assert_called_once_with()

    def test_new_identity_uses_default_pin(self):
        client=XClient(self.auth,state_dir=self.directory/'fresh',default_pin='6382')
        with patch('x_apis.messages.public_records',return_value=[]),\
             patch('x_apis.messages.setup_identity',return_value={'state':'ready'}) as setup:
            self.assertEqual(client.messages.ensure_identity()['state'],'ready')
            self.assertEqual(setup.call_args.args[1],'6382')
            self.assertEqual(setup.call_args.args[2],self.directory/'fresh'/'123'/'identity.json')

    def test_existing_remote_identity_never_uses_default_pin(self):
        client=XClient(self.auth,state_dir=self.directory/'fresh',default_pin='6382')
        with patch('x_apis.messages.setup_identity') as setup,patch('x_apis.messages.unlock_identity') as unlock:
            with self.assertRaises(XClientError) as caught:client.messages.ensure_identity()
            self.assertEqual(caught.exception.code,'existing_pin_required')
            setup.assert_not_called();unlock.assert_not_called()

    def test_failed_registry_cannot_create_identity(self):
        with patch('x_apis.messages.public_records',side_effect=IdentityError('registry unavailable')),\
             patch('x_apis.messages.setup_identity') as setup:
            with self.assertRaises(XClientError):self.client().messages.ensure_identity()
            setup.assert_not_called()

    def test_ready_identity_does_not_register_or_unlock(self):
        with patch('x_apis.messages.setup_identity') as setup,patch('x_apis.messages.unlock_identity') as unlock:
            self.assertEqual(self.client().messages.ensure_identity()['state'],'ready')
            setup.assert_not_called();unlock.assert_not_called()

    def test_wrong_existing_pin_is_not_retried_automatically(self):
        client=XClient(self.auth,state_dir=self.directory/'fresh',default_pin='6382',existing_pin='9871')
        with patch('x_apis.messages.unlock_identity',side_effect=IdentityError('invalid_pin')) as unlock,\
             patch('x_apis.messages.setup_identity') as setup:
            with self.assertRaises(XClientError):client.messages.ensure_identity()
            with self.assertRaises(XClientError) as caught:client.messages.ensure_identity()
            self.assertEqual(caught.exception.code,'unlock_needs_attention')
            self.assertEqual(unlock.call_count,1);setup.assert_not_called()

    def test_multiple_key_versions_require_selection_before_unlock(self):
        client=XClient(self.auth,state_dir=self.directory/'fresh',existing_pin='6382')
        with patch('x_apis.messages.public_records',return_value=[{},{}]),\
             patch('x_apis.messages.unlock_identity') as unlock:
            with self.assertRaises(XClientError) as caught:client.messages.ensure_identity()
            self.assertEqual(caught.exception.code,'key_version_required');unlock.assert_not_called()

    def test_account_switch_cannot_reuse_foreign_identity(self):
        with patch('x_apis.messages._viewer',return_value=('456','other')):
            with self.assertRaises(XClientError) as caught:self.client().messages.ensure_identity()
            self.assertEqual(caught.exception.code,'identity_mismatch')

    def test_pagination_uses_oldest_sequence_and_preserves_key_events(self):
        self.plaintext()
        with patch.object(XChatAPI,'send_message',side_effect=self.accept_message):
            self.client().messages.send('recipient','one',request_id='id')
        event=self.events[0]
        self.page.side_effect=[{'data':{'get_conversation_page':{'encoded_message_events':[event],'has_more':True}}},
                               {'data':{'get_conversation_page':{'encoded_message_events':[],'has_more':False}}}]
        self.page.reset_mock()
        self.assertEqual(read_conversation_events(self.auth,'123:456'),[event])
        self.assertEqual(self.page.call_args.kwargs['min_local_sequence_id'],'901')

    def test_pagination_no_progress_fails(self):
        self.plaintext()
        with patch.object(XChatAPI,'send_message',side_effect=self.accept_message):
            self.client().messages.send('recipient','one',request_id='id')
        self.page.side_effect=None
        self.page.return_value={'data':{'get_conversation_page':{'encoded_message_events':self.events,'has_more':True}}}
        with self.assertRaises(IdentityError):read_conversation_events(self.auth,'123:456')


class ClientFacadeTests(unittest.TestCase):
    def setUp(self):self.client=XClient(object())

    def test_env_isolation_and_configuration(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'.env';path.write_text('X_COOKIES=auth_token=fake; ct0=fake\nX_CHAT_DEFAULT_PIN=6382\nX_CHAT_STATE_DIR=local-state\n')
            before=dict(os.environ)
            with patch.dict(os.environ,{},clear=True):
                client=XClient.from_env(path)
                self.assertEqual(client.default_pin,'6382')
                self.assertEqual(client.state_dir,Path(d).resolve()/'local-state')
                self.assertIsNone(client.existing_pin)
                self.assertEqual(dict(os.environ),{})
            self.assertEqual(dict(os.environ),before)

    def test_posts_and_comments_business_arguments(self):
        with patch('x_apis.client.XWriteAPI.post_tweet',return_value=(True,'ok',{})) as create,\
             patch('x_apis.client.XWriteAPI.extract_tweet_id',return_value='99'):
            self.assertEqual(self.client.comments.create('88','reply',images=['a.png'])['post_id'],'99')
            self.assertEqual(create.call_args.kwargs['reply_to'],'88')
            self.assertEqual(create.call_args.kwargs['images'],['a.png'])

    def test_post_failure_does_not_claim_success_or_leak_error(self):
        with patch('x_apis.client.XWriteAPI.post_tweet',return_value=(False,'SECRET proxy',None)):
            with self.assertRaises(XClientError) as caught:self.client.posts.create('hello')
            self.assertNotIn('SECRET',str(caught.exception))

    def test_notification_parser_called_inside_api(self):
        from tests.test_notifications import response,tweet_entry
        with patch('x_apis.client.XAPI.get_notifications',return_value=response([tweet_entry()])):
            self.assertEqual(self.client.notifications.list()['items'][0]['text'],'@owner reply')

    def test_media_and_article_business_arguments(self):
        with patch('x_apis.client.XMediaAPI.upload',return_value='media') as upload:
            self.assertEqual(self.client.media.upload('x.jpg'),{'media_id':'media'})
            self.assertEqual(upload.call_args.args[1],'x.jpg')
        with patch('x_apis.client.XArticleAPI.post_article',return_value=(True,'ok',{'article_id':'a','edit_url':'link'})) as create:
            self.assertEqual(self.client.articles.create('# hello')['article_id'],'a')
            self.assertEqual(create.call_args.kwargs['markdown'],'# hello')

    def test_article_list_has_business_items(self):
        raw={'data':{'user':{'result':{'articles_article_mixer_slice':{'items':[
            {'article_entity_results':{'result':{'rest_id':'a','title':'Title','lifecycle_state':{'lifecycle':'Draft'}}}}]}}}}}
        with patch('x_apis.client.XArticleAPI.list_articles',return_value=raw):
            self.assertEqual(self.client.articles.list()['items'],[{'article_id':'a','title':'Title','lifecycle':'Draft'}])

    def test_cookie_factory_and_login_factory(self):
        client=XClient.from_cookies('auth_token=fake; ct0=fake',default_pin='6382')
        self.assertTrue(client.auth.is_logged_in)
        with patch('x_apis.login_api.XJetfuelLoginApi') as api:
            result=XClient.login('user','synthetic-password',profile={'profile':'synthetic'})
            self.assertIs(result.auth,api.return_value.login.return_value)
            self.assertTrue(api.call_args.kwargs['pure_required'])
            self.assertEqual(api.return_value.login.call_args.args,('user','synthetic-password'))

    def test_comment_listing_excludes_parent_and_unrelated_tweets(self):
        entries=[{'rest_id':i,'legacy':{'in_reply_to_status_id_str':parent}} for i,parent in [('88',None),('99','88'),('100','77')]]
        with patch('x_apis.client.XAPI.get_work_comments',return_value={'data':{'threaded_conversation_with_injections_v2':{'instructions':[]}}}),\
             patch('x_apis.client.extract_tweet_entries',return_value=entries),\
             patch('x_apis.client.extract_cursor',return_value='next'):
            result=self.client.comments.list('88')
            self.assertEqual([x['work_id'] for x in result['items']],['99'])
            self.assertEqual(result['next_cursor'],'next')

    def test_weak_pin_is_rejected_by_real_sdk_without_network(self):
        from chat_xdk import Chat
        chat=Chat();chat.generate_keypairs()
        with self.assertRaisesRegex(ValueError,'Weak PIN'):chat.setup('4321')
        chat.lock()


if __name__=='__main__':unittest.main()
