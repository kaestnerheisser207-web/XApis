"""Lifecycle coverage: real SDK/facade, synthetic accounts, no live network."""
import copy
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from api_watch import lifecycle_probes as probes
from tests import test_client as client_tests
from tests import test_xchat_conversation as conversation_tests
from tests.test_xchat_conversation import encoded_key_event
from tests.test_xchat_identity import CONFIG, registry
from x_apis import XClient
from x_apis.x_dm_api import XChatAPI


class Context:
    def __init__(self, client=None, **config):
        self.client = client
        self.auth = client.auth if client else None
        self.actor_id = '123' if client else None
        self.config = {'lifecycle': {'enabled': True, **config}}
        self.samples = {'test_account_id': '123', 'peer_username': 'recipient', 'peer_user_id': '456'}
        self.secrets = {}
        self.run_id = 'run-1'
        self.marker = 'api-watch fixture run-1'
        self.stop_event = threading.Event()
        self.results = {}
        self.saved = {}
        self.steps = []

    def step(self, check_id, action, validator=None, **options):
        self.steps.append((check_id, options))
        try:
            value = action()
            if validator:
                validator(value)
            self.results[check_id] = {'status': 'ok'}
            return SimpleNamespace(ok=True, value=value, status='ok')
        except Exception as exc:
            status = getattr(exc, 'code', 'failed')
            self.results[check_id] = {'status': status}
            return SimpleNamespace(ok=False, value=None, status=status)

    def block(self, ids, reason, status='blocked'):
        for check_id in ids:
            self.results[check_id] = {'status': status, 'reason': reason}

    def resources(self, kind, all_runs=False):
        return [copy.deepcopy(row) for (k, _), row in self.saved.items() if k == kind
                and (all_runs or row['metadata'].get('run_id') == self.run_id)]

    def save_resource(self, kind, resource_id, **metadata):
        self.saved[(kind, resource_id)] = {'id': resource_id, 'metadata': copy.deepcopy(metadata)}


class ChatLifecycleTests(unittest.TestCase):
    submit = conversation_tests.NewConversationTests.submit
    accept_message = client_tests.MessagesClientTests.accept_message

    def setUp(self):
        conversation_tests.NewConversationTests.setUp(self)
        self.enterContext(patch('builder.client.get', side_effect=AssertionError('network forbidden')))
        self.enterContext(patch('builder.client.post', side_effect=AssertionError('network forbidden')))
        self.enterContext(patch('requests.sessions.Session.request', side_effect=AssertionError('network forbidden')))
        self.client = XClient(self.auth, state_dir=self.directory / 'monitor', identity_path=self.identity)
        self.context = Context(self.client)

        def distribute(auth, prepared):
            self.events.append(encoded_key_event(prepared))
            return {'conversation_key_change_sequence_id': '900'}

        self.post.side_effect = distribute
        self.send = self.enterContext(patch.object(XChatAPI, 'send_message', side_effect=self.accept_message))

    def run_with(self, **config):
        self.context.config['lifecycle'].update(config)
        probes.run(self.context)
        return self.context.results

    def durable_context(self, run_id, **options):
        from api_watch.execution import ProbeContext
        from api_watch.state import Store
        store = Store(self.directory / 'watch-journal')
        self.addCleanup(store.close)
        config = {'samples': {'test_account_id': '123', 'peer_username': 'recipient',
                              'peer_user_id': '456'},
                  'probes': {'request_gap_seconds': 0},
                  'lifecycle': {'enabled': True, **options}}
        context = ProbeContext(config, {}, store, scenario='lifecycle', auth=self.auth,
                               actor_id='123', run_id=run_id)
        context._client = self.client
        return context

    def test_real_journal_runs_new_and_existing_send_and_keeps_only_safe_results(self):
        context = self.durable_context('durable-1', chat_new_conversation=True, chat_send=True)
        probes.run(context)
        self.assertEqual(context.results['lifecycle.chat.send_new']['status'], 'ok', context.results)
        self.assertEqual(context.results['lifecycle.chat.send_existing']['status'], 'ok')
        self.assertFalse(context.finish())
        persisted = context.store.db.execute('SELECT result FROM probe_steps').fetchall()
        self.assertNotIn(context.marker, json.dumps([row[0] for row in persisted]))

    def test_real_journal_reconciles_unknown_without_second_send(self):
        def ambiguous(*args, **kwargs):
            self.accept_message(*args, **kwargs)
            raise TimeoutError('PRIVATE_REMOTE_RESPONSE')

        self.send.side_effect = ambiguous
        first = self.durable_context('durable-1', chat_new_conversation=True)
        probes.run(first)
        self.assertEqual(first.results['lifecycle.chat.send_new']['status'], 'unconfirmed')
        self.assertTrue(first.finish())
        second = self.durable_context('durable-2', chat_new_conversation=True)
        probes.run(second)
        self.assertEqual(second.results['lifecycle.chat.send_new']['status'], 'not_exercised', second.results)
        self.assertEqual(second.results['lifecycle.chat.readback']['status'], 'ok')
        self.assertEqual(self.send.call_count, 1)

    def test_write_gate_does_not_create_an_unrecoverable_unknown_request(self):
        first = self.durable_context('durable-prior')
        # An unresolved prior lifecycle run intentionally blocks fresh writes.
        second = self.durable_context('durable-next', chat_new_conversation=True)
        probes.run(second)
        self.assertEqual(second.results['lifecycle.chat.send_new']['status'], 'blocked')
        self.assertEqual(second.resources('chat_request')[0]['metadata']['phase'], 'not_attempted')
        self.send.assert_not_called()
        self.post.assert_not_called()

    def test_new_and_existing_send_exercise_different_calls_and_decrypt(self):
        result = self.run_with(chat_new_conversation=True, chat_send=True)
        for check_id in ('new_conversation', 'send_new', 'send_existing', 'readback'):
            self.assertEqual(result['lifecycle.chat.' + check_id]['status'], 'ok', result)
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(len(self.context.resources('chat_request')), 2)
        self.assertTrue(all(row['metadata']['phase'] == 'verified'
                            for row in self.context.resources('chat_request')))
        self.assertEqual(self.client.messages.list('recipient')['items'][0]['text'], self.context.marker)

    def test_unknown_send_is_reconciled_from_prior_run_without_resubmission(self):
        def ambiguous(*args, **kwargs):
            self.accept_message(*args, **kwargs)
            raise TimeoutError('PRIVATE_REMOTE_RESPONSE')

        self.send.side_effect = ambiguous
        result = self.run_with(chat_new_conversation=True)
        self.assertEqual(result['lifecycle.chat.send_new']['status'], 'delivery_unknown')
        self.assertEqual(self.context.resources('chat_request')[0]['metadata']['phase'], 'unknown')
        self.context.run_id = 'run-2'
        result = self.run_with()
        self.assertEqual(result['lifecycle.chat.send_new']['status'], 'not_exercised')
        self.assertEqual(result['lifecycle.chat.new_conversation']['status'], 'not_exercised')
        self.assertEqual(result['lifecycle.chat.readback']['status'], 'ok')
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.post.call_count, 1)

    def test_unknown_unseen_send_never_uses_new_request_id(self):
        self.send.side_effect = TimeoutError('PRIVATE_REMOTE_RESPONSE')
        self.run_with(chat_new_conversation=True)
        first = self.context.resources('chat_request')[0]['id']
        self.context.run_id = 'run-2'
        self.run_with()
        self.assertEqual(len(self.context.resources('chat_request', all_runs=True)), 1)
        self.assertEqual(self.context.resources('chat_request', all_runs=True)[0]['id'], first)
        self.assertEqual(self.send.call_count, 1)
        self.assertNotEqual(self.context.results['lifecycle.chat.send_new']['status'], 'ok')

    def test_existing_conversation_never_claims_new_key_distribution(self):
        self.run_with(chat_new_conversation=True)
        self.context.run_id = 'run-2'
        result = self.run_with()
        self.assertEqual(result['lifecycle.chat.new_conversation']['status'], 'not_exercised')
        self.assertEqual(result['lifecycle.chat.send_new']['status'], 'not_exercised')
        self.assertEqual(self.send.call_count, 1)

    def test_existing_send_requires_existing_encrypted_conversation(self):
        result = self.run_with(chat_send=True)
        self.assertEqual(result['lifecycle.chat.send_existing']['status'], 'blocked')
        self.send.assert_not_called()
        self.post.assert_not_called()

    def test_wrong_actor_and_wrong_recipient_block_before_write(self):
        self.context.samples['test_account_id'] = '999'
        result = self.run_with(chat_new_conversation=True)
        self.assertEqual(result['lifecycle.chat.send_new']['status'], 'blocked')
        self.context.samples['test_account_id'] = '123'
        self.context.samples['peer_user_id'] = '999'
        result = self.run_with()
        self.assertEqual(result['lifecycle.chat.send_new']['status'], 'blocked')
        self.send.assert_not_called()
        self.post.assert_not_called()

    def test_existing_registered_identity_does_not_repeat_registration(self):
        self.context.secrets['chat_pin'] = '6382'
        with patch.object(XChatAPI, 'add_public_key') as register:
            result = self.run_with(chat_identity=True, chat_identity_recover=True)
        self.assertEqual(result['lifecycle.chat.identity_register']['status'], 'not_exercised')
        self.assertEqual(result['lifecycle.chat.identity_recover']['status'], 'not_exercised')
        register.assert_not_called()

    def test_identity_initialization_uses_facade_and_verifies_backup(self):
        self.identity.unlink()
        entry = {'public_key_with_metadata': {'version': '100', 'public_key': self.generated['123']['registration']},
                 'token_map': CONFIG}
        registered = []

        def register(*args):
            registered.append(entry)
            return {'version': '100', 'token_map': CONFIG}

        self.context.secrets['chat_pin'] = '6382'
        with patch.object(XChatAPI, 'get_public_keys', side_effect=lambda *a, **k: registry(registered)), \
                patch.object(XChatAPI, 'add_public_key', side_effect=register) as add, \
                patch('utils.xchat_identity.sdk_call', return_value=self.generated['123']):
            result = self.run_with(chat_identity=True)
        self.assertEqual(result['lifecycle.chat.identity_register']['status'], 'ok', result)
        self.assertEqual(result['lifecycle.chat.identity_backup']['status'], 'ok')
        self.assertEqual(add.call_count, 1)
        self.assertEqual(json.loads(self.identity.read_text())['phase'], 'ready')

    def test_recovery_runs_only_when_registered_and_local_identity_missing(self):
        self.identity.unlink()
        self.context.secrets['chat_pin'] = '6382'
        entry = {'public_key_with_metadata': {'version': '100', 'public_key': self.generated['123']['registration']},
                 'token_map': CONFIG}
        with patch.object(XChatAPI, 'get_public_keys', return_value=registry([entry])), \
                patch('utils.xchat_identity.sdk_call', return_value=self.generated['123']) as sdk:
            result = self.run_with(chat_identity_recover=True)
        self.assertEqual(result['lifecycle.chat.identity_recover']['status'], 'ok', result)
        self.assertEqual(sdk.call_args.args[0], 'unlock')

    def test_recovery_registry_drift_cannot_turn_into_registration(self):
        self.identity.unlink()
        self.context.secrets['chat_pin'] = '6382'
        entry = {'public_key_with_metadata': {'version': '100', 'public_key': self.generated['123']['registration']},
                 'token_map': CONFIG}
        # Preflight sees an existing identity. Both subsequent reads (facade
        # and setup_identity) unexpectedly return no keys, reaching add_public_key.
        with patch.object(XChatAPI, 'get_public_keys', side_effect=[
                registry([entry]), registry([]), registry([])]) as keys, \
                patch('utils.xchat_identity.sdk_call', return_value=self.generated['123']), \
                patch.object(XChatAPI, 'add_public_key') as add:
            result = self.run_with(chat_identity_recover=True)
        self.assertEqual(keys.call_count, 3)
        self.assertNotEqual(result['lifecycle.chat.identity_recover']['status'], 'ok')
        add.assert_not_called()
        self.post.assert_not_called()
        self.send.assert_not_called()

    def test_missing_pin_never_registers_or_uses_default_pin(self):
        self.client.default_pin = '6382'
        with patch.object(XChatAPI, 'add_public_key') as add:
            result = self.run_with(chat_identity=True)
        self.assertEqual(result['lifecycle.chat.identity_register']['status'], 'blocked')
        add.assert_not_called()

    def test_token_is_not_returned_or_stored(self):
        with patch.object(XChatAPI, 'generate_chat_token', return_value='PRIVATE_CHAT_TOKEN') as token:
            result = self.run_with(chat_token=True)
        self.assertEqual(result['lifecycle.chat.token']['status'], 'ok')
        self.assertNotIn('PRIVATE_CHAT_TOKEN', json.dumps(result))
        token.assert_called_once_with(self.auth)

    def test_observer_forwards_restores_and_rejects_changed_recipient(self):
        original = XChatAPI.__dict__['send_message']
        with probes._observe(new_conversation=True, allow_send=True, conversation_id='123:456'):
            with self.assertRaises(probes.LifecycleError):
                XChatAPI.send_message(self.auth, conversation_id='123:999', message_id='x')
            with self.assertRaises(probes.LifecycleError):
                XChatAPI.add_conversation_keys(self.auth, {'conversation_id': '123:999'})
        self.assertIs(XChatAPI.__dict__['send_message'], original)
        self.send.assert_not_called()
        self.post.assert_not_called()

    def test_identity_observers_disallow_send_by_default(self):
        for options in ({}, {'register': True}):
            with probes._observe(**options):
                with self.assertRaises(probes.LifecycleError):
                    XChatAPI.send_message(self.auth, conversation_id='123:456', message_id='x')
        self.send.assert_not_called()

    def test_mutation_not_observed_cannot_pass(self):
        receipt = {'status': 'verified', 'sender_id': '123', 'recipient_id': '456',
                   'conversation_id': '123:456', 'encrypted': True, 'message_id': 'fake-cache'}
        with patch.object(self.client.messages, 'send', return_value=receipt):
            result = self.run_with(chat_new_conversation=True)
        self.assertEqual(result['lifecycle.chat.send_new']['status'], 'contract_error')
        self.assertEqual(result['lifecycle.chat.new_conversation']['status'], 'not_exercised')


class LoginLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.context = Context()
        self.context.secrets['login'] = {'username': 'fixture', 'password': 'PRIVATE_PASSWORD',
                                         'profile': {'raw': [1, 2], 'compression': 'fallback'}}
        self.enterContext(patch('builder.client.get', side_effect=AssertionError('network forbidden')))
        self.enterContext(patch('builder.client.post', side_effect=AssertionError('network forbidden')))
        self.enterContext(patch('requests.sessions.Session.request', side_effect=AssertionError('network forbidden')))

    def test_login_verifies_returned_identity_and_closes_session(self):
        self.context.config['lifecycle']['login_enabled'] = True
        with patch('x_apis.login_api.XJetfuelLoginApi') as api, \
                patch('x_apis.x_api.XAPI.get_viewer', return_value={'data': {'viewer': {
                    'user_results': {'result': {'rest_id': '123'}}}}}):
            probes.run(self.context)
        self.assertEqual(self.context.results['lifecycle.login.jetfuel']['status'], 'ok')
        api.return_value.login.assert_called_once_with('fixture', 'PRIVATE_PASSWORD')
        api.return_value.session.close.assert_called_once()
        self.assertNotIn('PRIVATE_PASSWORD', json.dumps(self.context.results))

    def test_challenge_is_needs_input_and_not_retried(self):
        from x_apis.login_api import LoginChallenge
        self.context.config['lifecycle']['login_enabled'] = True
        with patch('x_apis.login_api.XJetfuelLoginApi') as api:
            api.return_value.login.side_effect = LoginChallenge('otp', 'PRIVATE_CHALLENGE', 'PRIVATE_FLOW_TOKEN')
            probes.run(self.context)
        self.assertEqual(self.context.results['lifecycle.login.jetfuel']['status'], 'needs_input')
        self.assertEqual(api.return_value.login.call_count, 1)
        self.assertNotIn('PRIVATE', json.dumps(self.context.results))

    def test_unsupported_native_profile_blocks_without_login_attempt(self):
        self.context.config['lifecycle']['login_enabled'] = True
        self.context.secrets['login']['profile'] = {'raw': [1, 2]}
        with patch('utils.chromium_deflate.available', return_value=False), \
                patch('x_apis.login_api.XJetfuelLoginApi') as api:
            probes.run(self.context)
        self.assertEqual(self.context.results['lifecycle.login.jetfuel']['status'], 'blocked')
        api.assert_not_called()

    def test_compat_node_is_explicit_and_does_not_silently_select_pure_profile(self):
        self.context.config['lifecycle']['login_enabled'] = True
        self.context.secrets['login'] = {'username': 'fixture', 'password': 'PRIVATE_PASSWORD',
                                        'mode': 'compat-node'}
        with patch('api_watch.lifecycle_probes.shutil.which', return_value='/fixture/node'), \
                patch('api_watch.lifecycle_probes.Path.is_file', return_value=True), \
                patch('x_apis.login_api.XJetfuelLoginApi') as api, \
                patch('x_apis.x_api.XAPI.get_viewer', return_value={'data': {'viewer': {
                    'user_results': {'result': {'rest_id': '123'}}}}}):
            probes.run(self.context)
        self.assertEqual(self.context.results['lifecycle.login.jetfuel']['status'], 'ok')
        api.assert_called_once_with(proxies=None)

    def test_legacy_login_is_separately_enabled_and_account_checked(self):
        self.context.config['lifecycle']['legacy_login_enabled'] = True
        with patch('x_apis.login_api.XLoginApi') as api, \
                patch('x_apis.x_api.XAPI.get_viewer', return_value={'data': {'viewer': {
                    'user_results': {'result': {'rest_id': '999'}}}}}):
            probes.run(self.context)
        self.assertEqual(self.context.results['lifecycle.login.legacy']['status'], 'contract_error')
        self.assertEqual(api.return_value.login.call_count, 1)

    def test_defaults_make_no_login_or_chat_calls(self):
        with patch('x_apis.login_api.XJetfuelLoginApi') as api:
            probes.run(self.context)
        api.assert_not_called()
        self.assertTrue(all(row['status'] != 'ok' for row in self.context.results.values()))


if __name__ == '__main__':
    unittest.main()
