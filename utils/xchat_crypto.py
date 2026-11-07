"""Prepare native X Chat text events without performing any network request.

Protocol source: X's public xchat/embed/3cee38291561ed77df6a5425a853d724f1576695
bundle, inspected 2026-10-01. ``graph-provider-C9zc_lbU.js`` defines the Thrift
structs, HKDF inputs, franking, and signature payload. Its imported
``attachment-preview-text-C4-0baN0.js`` encodes signatures/contents using
*standard* Base64 without padding; the outer Thrift envelopes retain padding.

Callers must supply the latest, legitimately recovered conversation key and
registered signing key. This module does not recover keys, infer protocol
versions, log secrets, register devices, or send messages.
"""

import base64
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import uuid

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from nacl.secret import SecretBox

from utils.xchat_codec import encode_struct


_BOOL, _I16, _I32, _BINARY, _STRUCT, _LIST = 2, 6, 8, 11, 12, 15
_SUPPORTED_SIGNATURE_VERSIONS = frozenset(range(1, 12))


@dataclass(frozen=True)
class XChatKeyMaterial:
    """Explicit key/version state for one sender and one conversation.

    ``signing_key`` is an unencrypted PKCS8/SEC1 PEM or DER private key for
    P-256, or the browser cache's 32-byte big-endian private scalar. Both
    private fields are excluded from the generated repr. The conversation ID
    is the SDK ID (for example ``sender:recipient``), not its route spelling.
    ``contents_encryption_version=None`` means the observed original cKey
    scheme; ``1`` uses the observed per-message HKDF scheme.
    """

    sender_id: str
    conversation_id: str
    conversation_key: bytes = field(repr=False)
    conversation_key_version: str | int
    signing_key: bytes | str = field(repr=False)
    public_key_version: str | int
    signature_version: int
    contents_encryption_version: int | None = None
    conversation_token: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class PreparedXChatMessage:
    """An immutable encrypted request; preserve its message_id for reconciliation."""

    conversation_id: str
    message_id: str
    encoded_message_create_event: str = field(repr=False)
    encoded_message_event_signature: str | None = field(repr=False)
    conversation_token: str | None = field(default=None, repr=False)

    def as_send_kwargs(self) -> dict:
        """Arguments accepted by ``XChatAPI.send_message(auth, **result)``."""
        return {
            'conversation_id': self.conversation_id,
            'message_id': self.message_id,
            'conversation_token': self.conversation_token,
            'encoded_message_create_event': self.encoded_message_create_event,
            'encoded_message_event_signature': self.encoded_message_event_signature,
        }


def prepare_message_request(text: str, conversation_id: str, *, message_id=None):
    """网页 sendUnencrypted 首次文字请求；没有密钥版本、franking 或签名。"""
    if not isinstance(text, str) or not text.strip():
        raise ValueError('text must be a nonempty string')
    if not re.fullmatch(r'[1-9][0-9]*:[1-9][0-9]*', conversation_id):
        raise ValueError('Invalid one-to-one conversation ID')
    ids = conversation_id.split(':')
    if len(set(ids)) != 2 or ids != sorted(ids, key=int):
        raise ValueError('Conversation must contain two numerically sorted IDs')
    message_id = str(uuid.UUID(message_id)) if message_id else str(uuid.uuid4())
    contents = [(1, _BINARY, text), (2, _LIST, (_STRUCT, [])),
                (3, _LIST, (_STRUCT, [])), (7, _I32, 1)]
    plaintext = encode_struct([(1, _STRUCT, [(1, _STRUCT, contents)])])
    event = encode_struct([(100, _BINARY, plaintext), (102, _BOOL, True),
                           (105, _BOOL, False), (106, _I32, 1)])
    return PreparedXChatMessage(conversation_id, message_id, _base64(event), None)


def message_request_text(encoded_create):
    """只接受未加密文字事件，防止加密事件无签名发送或静默降级。"""
    from utils.xchat_codec import decode_base64_struct, decode_struct, field as get
    event = decode_base64_struct(encoded_create)
    if any(k in event for k in (101, 108, 112)):
        raise ValueError('Encrypted messages cannot use the unsigned request path')
    holder = decode_struct(get(event, 100, _BINARY))
    text = get(get(get(holder, 1, _STRUCT), 1, _STRUCT), 1, _BINARY).decode('utf-8')
    if not text.strip():
        raise ValueError('Empty message request')
    return text


def load_key_material(path) -> XChatKeyMaterial:
    """读取本地权限 600 的账户/会话密钥文件，不从浏览器自动导出。"""
    path = Path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
        raise ValueError('X Chat key file must be a regular file below 64 KiB')
    if os.name == 'posix' and stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError('X Chat key file permissions must be 0600')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        conversation_key = base64.b64decode(data['conversation_key_b64'], validate=True)
        signing_key = base64.b64decode(data['signing_key_b64'], validate=True)
        keys = XChatKeyMaterial(
            sender_id=data['sender_id'], conversation_id=data['conversation_id'],
            conversation_key=conversation_key,
            conversation_key_version=data['conversation_key_version'],
            signing_key=signing_key, public_key_version=data['public_key_version'],
            signature_version=data['signature_version'],
            contents_encryption_version=data.get('contents_encryption_version'),
            conversation_token=data.get('conversation_token'))
        if data.get('signing_public_key_b64'):
            public_der = base64.b64decode(data['signing_public_key_b64'], validate=True)
            expected = serialization.load_der_public_key(public_der)
            actual = _load_signing_key(signing_key).public_key()
            if (not isinstance(expected, ec.EllipticCurvePublicKey)
                    or actual.public_numbers() != expected.public_numbers()):
                raise ValueError('Signing public/private key mismatch')
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError('Invalid X Chat key file or signing public/private key mismatch') from None
    return keys


def _identifier(name: str, value: str) -> str:
    # The frontend rejects commas in signed components before signing.
    if (not isinstance(value, str) or not value.strip()
            or ',' in value or any(ord(char) < 32 for char in value)):
        raise ValueError(f'{name} must be a nonempty string without commas or controls')
    return value


def _version(name: str, value: str | int) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f'{name} must be a nonnegative signed 64-bit integer')
    encoded = str(value)
    if not re.fullmatch(r'0|[1-9][0-9]*', encoded) or int(encoded) > 2**63 - 1:
        raise ValueError(f'{name} must be a nonnegative signed 64-bit integer')
    return encoded


def _load_signing_key(value):
    if isinstance(value, str):
        try:
            value = value.encode('ascii')
        except UnicodeEncodeError:
            raise ValueError('signing_key must be PEM, DER, or a 32-byte P-256 scalar') from None
    if not isinstance(value, bytes) or not value:
        raise ValueError('signing_key must be PEM, DER, or a 32-byte P-256 scalar')
    try:
        if len(value) == 32:
            key = ec.derive_private_key(int.from_bytes(value, 'big'), ec.SECP256R1())
        elif value.lstrip().startswith(b'-----BEGIN'):
            key = serialization.load_pem_private_key(value, password=None)
        else:
            key = serialization.load_der_private_key(value, password=None)
    except (ValueError, TypeError):
        raise ValueError('signing_key must be unencrypted PEM/DER or a valid 32-byte P-256 scalar') from None
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError('signing_key must use ECDSA P-256')
    return key


def _base64(data: bytes, *, unpadded=False) -> str:
    result = base64.b64encode(data).decode('ascii')
    return result.rstrip('=') if unpadded else result


def _encrypt(key: bytes, plaintext: bytes) -> bytes:
    # PyNaCl's bytes(EncryptedMessage) is nonce || MAC || ciphertext, matching
    # the frontend's nonce || crypto_secretbox_easy(message, nonce, key).
    return bytes(SecretBox(key).encrypt(plaintext, os.urandom(SecretBox.NONCE_SIZE)))


def prepare_text_message(text: str, keys: XChatKeyMaterial, *,
                         message_id: str | None = None) -> PreparedXChatMessage:
    """Serialize, encrypt, frank, and sign one text message locally.

    Supported signature versions 1..11 follow the currently inspected frontend
    branches; newer or unknown versions fail before generating an envelope.
    There are no attachments, rich-text entities, reply previews, or TTL here.
    Calling this function again creates fresh nonces; for an ambiguous send
    retain this returned object and reconcile the original message_id first.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError('text must be a nonempty string')
    if not isinstance(keys, XChatKeyMaterial):
        raise TypeError('keys must be XChatKeyMaterial')
    sender = _identifier('sender_id', keys.sender_id)
    conversation = _identifier('conversation_id', keys.conversation_id)
    message = _identifier('message_id', message_id) if message_id is not None else None
    key_version = _version('conversation_key_version', keys.conversation_key_version)
    public_key_version = _version('public_key_version', keys.public_key_version)
    signature_version = keys.signature_version
    if (type(signature_version) is not int
            or signature_version not in _SUPPORTED_SIGNATURE_VERSIONS):
        raise ValueError('signature_version must be one of the verified versions 1..11')
    contents_version = keys.contents_encryption_version
    if contents_version is not None and (type(contents_version) is not int or contents_version != 1):
        raise ValueError('contents_encryption_version must be None or the verified version 1')
    if not isinstance(keys.conversation_key, bytes) or len(keys.conversation_key) != SecretBox.KEY_SIZE:
        raise ValueError('conversation_key must be exactly 32 bytes')
    if keys.conversation_token is not None and (
            not isinstance(keys.conversation_token, str) or not keys.conversation_token.strip()):
        raise ValueError('conversation_token must be a nonempty string or None')
    signing_key = _load_signing_key(keys.signing_key)
    if message is None:
        message = str(uuid.uuid4())

    # MessageEntryHolder.contents -> MessageEntryContents.message ->
    # MessageContents. Browser plain-text composition supplies empty lists and
    # sent_from=CONVERSATION_SCREEN_COMPOSER (1).
    contents = [
        (1, _BINARY, text),
        (2, _LIST, (_STRUCT, [])),
        (3, _LIST, (_STRUCT, [])),
        (7, _I32, 1),
    ]
    plaintext = encode_struct([(1, _STRUCT, [(1, _STRUCT, contents)])])
    conversation_key = keys.conversation_key
    message_key = conversation_key
    if contents_version == 1:
        message_key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=b'xchat-message-key',
            info=f'{sender}:{message}'.encode('utf-8'),
        ).derive(conversation_key)
    encrypted = _encrypt(message_key, plaintext)

    franking_nonce = os.urandom(32)
    conversation_bytes = conversation.encode('utf-8')
    franking_tag = hashlib.sha256(
        b'xchat-franking' + hashlib.sha256(plaintext).digest()
        + struct.pack('>i', 0)  # number of media hashes
        + franking_nonce + message.encode('utf-8')
        + struct.pack('>i', len(conversation_bytes)) + conversation_bytes,
    ).digest()
    franking = [
        (1, _BINARY, franking_tag),
        (2, _BINARY, _encrypt(conversation_key, franking_nonce)),
    ]
    event = [
        (100, _BINARY, encrypted),
        (101, _BINARY, key_version),
        (102, _BOOL, True),
        (105, _BOOL, False),
        (106, _I32, 1),
        (108, _STRUCT, franking),
    ]
    if contents_version is not None:
        event.append((112, _I16, contents_version))

    encoded_contents = _base64(encrypted, unpadded=True)
    if signature_version == 1:
        components = [message, sender, conversation, encoded_contents]
    else:
        components = ['MessageCreateEvent', message, sender, conversation,
                      key_version, encoded_contents]
        if signature_version >= 10:
            # Kotlin toString(nullable) deliberately contributes literal null.
            components.append('null' if contents_version is None else str(contents_version))
        if signature_version >= 9:
            components.append(f'signature_version={signature_version}')
    payload = ','.join(components).encode('utf-8')
    der_signature = signing_key.sign(payload, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_signature)
    raw_signature = r.to_bytes(32, 'big') + s.to_bytes(32, 'big')
    signature = [
        (1, _BINARY, _base64(raw_signature, unpadded=True)),
        (2, _BINARY, public_key_version),
        (3, _BINARY, str(signature_version)),
        (5, _LIST, (_STRUCT, [[
            (1, _BINARY, sender),
            (2, _BINARY, public_key_version),
        ]])),
    ]
    return PreparedXChatMessage(
        conversation_id=conversation,
        message_id=message,
        encoded_message_create_event=_base64(encode_struct(event)),
        encoded_message_event_signature=_base64(encode_struct(signature)),
        conversation_token=keys.conversation_token,
    )
