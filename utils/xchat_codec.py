"""X Chat 使用的 Thrift Binary struct 编解码；不含账户或密钥状态。"""

import base64
import binascii
import struct

_MAX_BYTES = 4 * 1024 * 1024
_MAX_ITEMS = 10000
_NUMBERS = {2: '>b', 3: '>b', 4: '>d', 6: '>h', 8: '>i', 10: '>q'}
_TYPES = frozenset(_NUMBERS) | {11, 12, 13, 14, 15}


def encode_struct(fields):
    """fields 为 (field_id, type, value)；嵌套 STRUCT 同形，LIST=(type, values)。"""
    parts = []
    for field_id, kind, value in fields:
        parts.append(struct.pack('>bh', kind, field_id))
        parts.append(_encode_value(kind, value))
    return b''.join(parts) + b'\0'


def _encode_value(kind, value):
    if kind in _NUMBERS:
        return struct.pack(_NUMBERS[kind], value)
    if kind == 11:
        value = value.encode('utf-8') if isinstance(value, str) else bytes(value)
        return struct.pack('>i', len(value)) + value
    if kind == 12:
        return encode_struct(value)
    if kind in (14, 15):
        element_type, items = value
        return struct.pack('>bi', element_type, len(items)) + b''.join(
            _encode_value(element_type, item) for item in items)
    if kind == 13:
        key_type, value_type, items = value
        return struct.pack('>bbi', key_type, value_type, len(items)) + b''.join(
            _encode_value(key_type, key) + _encode_value(value_type, item)
            for key, item in items)
    raise ValueError('不支持的 X Chat Thrift 类型')


class _Reader:
    def __init__(self, data):
        if len(data) > _MAX_BYTES:
            raise ValueError('X Chat 事件超出大小限制')
        self.data, self.position, self.items = data, 0, 0

    def take(self, length):
        if length < 0 or self.position + length > len(self.data):
            raise ValueError('X Chat 事件数据不完整')
        start = self.position
        self.position += length
        return self.data[start:self.position]

    def number(self, fmt):
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]

    def count(self):
        value = self.number('>i')
        if value < 0 or value > _MAX_ITEMS:
            raise ValueError('X Chat 事件集合长度无效')
        return value

    def value(self, kind, depth=0):
        self.items += 1
        if depth > 32 or self.items > _MAX_ITEMS:
            raise ValueError('X Chat 事件结构超出限制')
        if kind in _NUMBERS:
            value = self.number(_NUMBERS[kind])
            if kind == 2 and value not in (0, 1):
                raise ValueError('X Chat 布尔字段无效')
            return bool(value) if kind == 2 else value
        if kind == 11:
            return self.take(self.number('>i'))
        if kind == 12:
            fields = {}
            while True:
                item_type = self.number('>b')
                if item_type == 0:
                    return fields
                field_id = self.number('>h')
                if field_id in fields:
                    raise ValueError('X Chat 事件含重复字段')
                fields[field_id] = (item_type, self.value(item_type, depth + 1))
        if kind in (14, 15):
            element_type = self.number('>b')
            if element_type not in _TYPES:
                raise ValueError('X Chat 集合元素类型无效')
            return (element_type, [self.value(element_type, depth + 1)
                                   for _ in range(self.count())])
        if kind == 13:
            key_type, value_type = self.number('>b'), self.number('>b')
            if key_type not in _TYPES or value_type not in _TYPES:
                raise ValueError('X Chat 映射元素类型无效')
            return (key_type, value_type, [
                (self.value(key_type, depth + 1), self.value(value_type, depth + 1))
                for _ in range(self.count())])
        raise ValueError('不支持的 X Chat Thrift 类型')


def decode_struct(data):
    reader = _Reader(data)
    result = reader.value(12)
    if reader.position != len(data):
        raise ValueError('X Chat 事件含多余尾部数据')
    return result


def decode_base64_struct(value):
    if not isinstance(value, str) or not value or len(value) > _MAX_BYTES * 2:
        raise ValueError('X Chat 事件必须为有界非空 Base64 字符串')
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError('X Chat 事件 Base64 无效') from None
    return decode_struct(decoded)


def field(fields, field_id, kind, default=None):
    if field_id not in fields:
        return default
    actual_kind, value = fields[field_id]
    if actual_kind != kind:
        raise ValueError('X Chat 事件字段类型不匹配')
    return value


def message_event_summary(encoded):
    """仅返回发送回执元数据；失败事件不应视为已发送。"""
    event = decode_base64_struct(encoded)
    detail = field(event, 7, 12, {})
    result = {}
    for field_id, name in ((1, 'sequence_id'), (2, 'message_id'),
                           (3, 'sender_id'), (4, 'conversation_id')):
        raw = field(event, field_id, 11)
        result[name] = raw.decode('utf-8') if raw is not None else None
    if len(detail) != 1:
        raise ValueError('X Chat 回执缺少唯一事件类型')
    result['event_type'] = next(iter(detail))
    field(detail, result['event_type'], 12)
    if result['event_type'] == 5:
        result['failure_type'] = field(field(detail, 5, 12), 1, 8)
    return result
