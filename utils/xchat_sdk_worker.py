"""官方 SDK 的隔离执行入口：秘密只经过父子进程管道，stdout 不用于日志。"""

import base64
import json
import re
import sys

from chat_xdk import Chat
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def _public_from_scalar(raw):
    key = ec.derive_private_key(int.from_bytes(raw, 'big'), ec.SECP256R1())
    return key.public_key().public_bytes(serialization.Encoding.DER,
                                         serialization.PublicFormat.SubjectPublicKeyInfo)


def _export(chat):
    blob = bytes(chat.export_keys())
    # chatxdk 0.5.0 exports identity scalar || signing scalar. Validate against
    # the SDK public keys, so a future serialization change fails closed.
    if len(blob) != 64:
        raise ValueError('Unsupported SDK key format')
    pubs = chat.get_public_keys()
    identity, signing = _public_from_scalar(blob[:32]), _public_from_scalar(blob[32:])
    for der, raw in ((identity, pubs.identity), (signing, pubs.signing)):
        key = serialization.load_der_public_key(der)
        sec1 = key.public_bytes(serialization.Encoding.X962,
                                serialization.PublicFormat.UncompressedPoint)
        if base64.b64decode(raw) != sec1:
            raise ValueError('SDK key format mismatch')
    return {'key_blob_b64': base64.b64encode(blob).decode(),
            'public_key': base64.b64encode(identity).decode(),
            'signing_public_key': base64.b64encode(signing).decode()}


def execute(request):
    action = request['action']
    chat = Chat()
    try:
        if action == 'generate':
            reg = chat.generate_keypairs()
            result = _export(chat)
            result['registration'] = {name: getattr(reg.public_key, name) for name in (
                'public_key', 'signing_public_key', 'identity_public_key_signature', 'registration_method')}
            if not chat.verify_key_binding(result['public_key'], result['signing_public_key'],
                                           result['registration']['identity_public_key_signature']):
                raise ValueError('Invalid generated key binding')
            return result
        if action == 'validate':
            chat.import_keys(base64.b64decode(request['key_blob_b64'], validate=True), request.get('version'))
            return _export(chat)
        if action == 'prepare_conversation':
            chat.import_keys(base64.b64decode(request['key_blob_b64'], validate=True), request['version'])
            chat.set_identity(request['sender_id'], request['version'])
            public_keys = request['public_keys']
            for key in public_keys:
                if not chat.verify_key_binding(key['public_key'], key['signing_public_key'],
                                               key['identity_public_key_signature']):
                    raise ValueError('Invalid participant key binding')
            prepared = chat.prepare_conversation_key_change([
                {name: key[name] for name in ('user_id', 'public_key', 'key_version')}
                for key in public_keys])
            prepared['conversation_key_b64'] = base64.b64encode(prepared.pop('conversation_key')).decode()
            return prepared
        if action in ('conversation_keys', 'decrypt_messages'):
            chat.import_keys(base64.b64decode(request['key_blob_b64'], validate=True), request['version'])
            chat.set_identity(request['sender_id'], request['version'])
            chat.set_signing_keys(request['signing_keys'])
            chat.set_cache_keys(True)
            decoded = chat.decrypt_events(request['events'])
            if action == 'decrypt_messages':
                # Never return extracted raw conversation keys to the reader.
                return {'messages': [item['event'] for item in decoded['messages']
                                     if item['event']['type'] == 'Message'],
                        'error_indices': list(decoded['errors'])}
            keys = decoded['conversation_keys']
            # Only verified KeyChange events are supplied; never expose message
            # plaintext or silently use an unverified fallback key.
            if decoded['errors']:
                raise ValueError('Key change verification failed')
            return {'keys': {str(k): base64.b64encode(bytes(v)).decode() for k, v in keys['keys'].items()},
                    'latest_version': keys['latest_version'],
                    'signing_key_b64': base64.b64encode(bytes(chat.export_keys())[32:]).decode()}
        if action not in ('setup', 'unlock'):
            raise ValueError('Unsupported SDK action')
        chat.update_config(json.dumps(request['token_map']))
        if action == 'setup':
            chat.import_keys(base64.b64decode(request['key_blob_b64'], validate=True), request['version'])
            before = _export(chat)
            chat.setup(request['pin'])
            if _export(chat) != before:
                raise ValueError('Setup unexpectedly changed keys')
            # A fresh SDK instance must recover the same identity; do not mark
            # setup complete just because the registration call returned.
            chat.lock()
        chat.unlock(request['pin'])
        return _export(chat)
    finally:
        chat.lock()


def main():
    try:
        request = json.load(sys.stdin)
        result = execute(request)
        print(json.dumps({'ok': True, 'result': result}))
    except Exception as exc:
        # Do not return exception bodies: native/HTTP errors may contain realm
        # credentials. Only expose a small, recognized error classification.
        message = str(exc).lower()
        kind = 'sdk_error'
        for fragment, label in (('invalid pin', 'invalid_pin'), ('invalidpin', 'invalid_pin'),
                                ('not registered', 'not_registered'), ('notregistered', 'not_registered'),
                                ('rate limit', 'rate_limited'), ('ratelimit', 'rate_limited'),
                                ('invalid auth', 'invalid_auth'), ('upgrade', 'upgrade_required')):
            if fragment in message:
                kind = label
                break
        match = re.search(r'guesses_remaining[^0-9]{0,20}(\d+)', message)
        print(json.dumps({'ok': False, 'error': kind,
                          'guesses_remaining': int(match.group(1)) if match else None}))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
