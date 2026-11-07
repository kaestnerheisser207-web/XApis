"""Thrift wire-format and defensive-decoding tests with synthetic public data."""

import base64
import struct
import unittest

from utils.xchat_codec import (
    decode_base64_struct, decode_struct, encode_struct, field,
    message_event_summary,
)


def encoded(fields):
    return base64.b64encode(encode_struct(fields)).decode('ascii')


class XChatCodecTests(unittest.TestCase):
    def test_string_encoding_matches_thrift_binary_bytes(self):
        # STRING type 11, field id 1, byte length 3, UTF-8 bytes, STOP.
        expected = b'\x0b\x00\x01\x00\x00\x00\x03\xe4\xb8\xad\x00'
        self.assertEqual(encode_struct([(1, 11, '中')]), expected)
        self.assertEqual(decode_struct(expected), {1: (11, b'\xe4\xb8\xad')})

    def test_numeric_types_and_binary_values_roundtrip(self):
        fields = [
            (1, 2, True), (2, 2, False), (3, 3, -128),
            (4, 4, 0.125), (5, 6, -32768),
            (6, 8, -2147483648), (7, 10, 9223372036854775807),
            (8, 11, b'\x00\xff\x80'),
        ]
        self.assertEqual(decode_struct(encode_struct(fields)), {
            number: (kind, value) for number, kind, value in fields
        })

    def test_nested_struct_and_collections_roundtrip(self):
        payload = encode_struct([
            (1, 12, [(1, 11, 'nested')]),
            (2, 15, (8, [1, -2])),
            (3, 14, (11, ['one', 'two'])),
            (4, 13, (11, 12, [('key', [(1, 2, True)])])),
        ])
        self.assertEqual(decode_struct(payload), {
            1: (12, {1: (11, b'nested')}),
            2: (15, (8, [1, -2])),
            3: (14, (11, [b'one', b'two'])),
            4: (13, (11, 12, [(b'key', {1: (2, True)})])),
        })

    def test_every_truncated_prefix_is_rejected(self):
        payload = encode_struct([
            (1, 11, 'example'), (2, 12, [(1, 10, 123)]),
            (3, 15, (11, ['first', 'second'])),
        ])
        for length in range(len(payload)):
            with self.subTest(length=length):
                with self.assertRaises(ValueError):
                    decode_struct(payload[:length])

    def test_trailing_bytes_are_rejected(self):
        with self.assertRaisesRegex(ValueError, '尾部'):
            decode_struct(encode_struct([]) + b'\0')

    def test_duplicate_fields_at_any_struct_depth_are_rejected(self):
        duplicates = [(1, 11, 'first'), (1, 11, 'second')]
        for fields in (duplicates, [(2, 12, duplicates)]):
            with self.subTest(fields=fields):
                with self.assertRaisesRegex(ValueError, '重复字段'):
                    decode_struct(encode_struct(fields))

    def test_negative_string_length_is_rejected(self):
        with self.assertRaises(ValueError):
            decode_struct(b'\x0b\x00\x01' + struct.pack('>i', -1) + b'\0')

    def test_negative_or_excessive_collection_lengths_are_rejected(self):
        for kind in (14, 15):
            for size in (-1, 10001):
                with self.subTest(kind=kind, size=size):
                    payload = struct.pack('>bhbi', kind, 1, 8, size) + b'\0'
                    with self.assertRaisesRegex(ValueError, '长度'):
                        decode_struct(payload)
        for size in (-1, 10001):
            with self.subTest(map_size=size):
                payload = struct.pack('>bhbbi', 13, 1, 11, 8, size) + b'\0'
                with self.assertRaisesRegex(ValueError, '长度'):
                    decode_struct(payload)

    def test_total_item_budget_applies_across_nested_collections(self):
        payload = encode_struct([
            (1, 15, (3, [0] * 6000)),
            (2, 15, (3, [0] * 6000)),
        ])
        with self.assertRaisesRegex(ValueError, '结构超出限制'):
            decode_struct(payload)

    def test_deep_structures_are_rejected_before_python_recursion_limit(self):
        # 33 nested field-1 structs, each terminated by STOP, plus root STOP.
        payload = b'\x0c\x00\x01' * 33 + b'\0' * 34
        with self.assertRaisesRegex(ValueError, '结构超出限制'):
            decode_struct(payload)

    def test_byte_size_limit_applies_before_parsing(self):
        with self.assertRaisesRegex(ValueError, '大小限制'):
            decode_struct(b'\0' * (4 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, '有界'):
            decode_base64_struct('A' * (8 * 1024 * 1024 + 1))

    def test_invalid_base64_and_wrong_argument_types_are_rejected(self):
        for value in (None, b'AA==', 1, '', 'AA==\n', '中文', '!!!!'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    decode_base64_struct(value)

    def test_unsupported_types_and_invalid_booleans_are_rejected(self):
        for kind in (1, 5, 7, 9, 16, 99, -1):
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError):
                    decode_struct(struct.pack('>bh', kind, 1) + b'\0')
        for value in (-1, 2, 127):
            with self.subTest(boolean=value):
                with self.assertRaisesRegex(ValueError, '布尔'):
                    decode_struct(struct.pack('>bhb', 2, 1, value) + b'\0')
        with self.assertRaises(ValueError):
            encode_struct([(1, 99, 0)])

    def test_empty_collections_still_require_valid_member_types(self):
        for kind in (14, 15):
            for element_type in (0, 1, 99):
                with self.subTest(kind=kind, element_type=element_type):
                    payload = struct.pack('>bhbi', kind, 1, element_type, 0) + b'\0'
                    with self.assertRaises(ValueError):
                        decode_struct(payload)
        for key_type, value_type in ((0, 11), (11, 0), (99, 11), (11, 99)):
            with self.subTest(key_type=key_type, value_type=value_type):
                payload = struct.pack('>bhbbi', 13, 1, key_type, value_type, 0) + b'\0'
                with self.assertRaises(ValueError):
                    decode_struct(payload)

    def test_field_type_is_checked_and_absent_fields_use_default(self):
        fields = decode_struct(encode_struct([(1, 11, 'value')]))
        self.assertEqual(field(fields, 1, 11), b'value')
        self.assertEqual(field(fields, 2, 8, 42), 42)
        with self.assertRaisesRegex(ValueError, '类型不匹配'):
            field(fields, 1, 8)

    def test_receipt_summary_returns_metadata_without_message_contents(self):
        summary = message_event_summary(encoded([
            (1, 11, '99'), (2, 11, 'message'), (3, 11, 'sender'),
            (4, 11, 'conversation'),
            (7, 12, [(1, 12, [(1, 11, b'ciphertext-must-not-be-returned')])]),
        ]))
        self.assertEqual(summary, {
            'sequence_id': '99', 'message_id': 'message', 'sender_id': 'sender',
            'conversation_id': 'conversation', 'event_type': 1,
        })

    def test_receipt_summary_identifies_business_failure(self):
        summary = message_event_summary(encoded([(7, 12, [(5, 12, [(1, 8, 7)])])]))
        self.assertEqual(summary['event_type'], 5)
        self.assertEqual(summary['failure_type'], 7)

    def test_receipt_requires_one_struct_typed_detail(self):
        invalid_fields = (
            [], [(7, 12, [])],
            [(7, 12, [(1, 12, []), (5, 12, [])])],
            [(7, 11, b'not-a-detail')],
            [(7, 12, [(1, 11, b'not-a-create-event')])],
            [(7, 12, [(5, 11, b'not-a-failure-event')])],
            [(7, 12, [(5, 12, [(1, 11, b'not-an-enum')])])],
        )
        for fields in invalid_fields:
            with self.subTest(fields=fields):
                with self.assertRaises(ValueError):
                    message_event_summary(encoded(fields))


if __name__ == '__main__':
    unittest.main()
