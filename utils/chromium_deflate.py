# -*- coding: utf-8 -*-
"""Python binding for the Chromium zlib raw-deflate profile used by Castle.

The current Castle bundle uses the browser's ``CompressionStream('deflate-raw')``.
CPython's zlib is not byte-identical because Chromium changes zlib's dictionary
hash (Castagnoli rather than the canonical Rabin--Karp rolling hash).  The
small DLL shipped in ``static/castle/chromium_zlib.dll`` is built from the
matching Chromium-zlib sources and is called directly from Python; it does not
create a browser/DOM environment and performs no I/O beyond the supplied bytes.

This module is intentionally isolated so callers can distinguish an exact
native-compatible compressor from Castle's larger JavaScript fallback.
"""

from __future__ import annotations

import ctypes
import os
from functools import lru_cache

from utils.resource_paths import asset_path


_Z_OK = 0
_Z_STREAM_END = 1
_Z_BUF_ERROR = -5
_Z_DEFLATED = 8
_Z_FINISH = 4
_DEFAULT_LEVEL = 6
_DEFAULT_MEM_LEVEL = 8
_DEFAULT_STRATEGY = 0


class _ZStream(ctypes.Structure):
    """Windows zlib 1.3 ABI (uLong is 32-bit on Win64/LLP64)."""

    _fields_ = [
        ('next_in', ctypes.c_void_p),
        ('avail_in', ctypes.c_uint32),
        ('total_in', ctypes.c_uint32),
        ('next_out', ctypes.c_void_p),
        ('avail_out', ctypes.c_uint32),
        ('total_out', ctypes.c_uint32),
        ('msg', ctypes.c_char_p),
        ('state', ctypes.c_void_p),
        ('zalloc', ctypes.c_void_p),
        ('zfree', ctypes.c_void_p),
        ('opaque', ctypes.c_void_p),
        ('data_type', ctypes.c_int),
        ('adler', ctypes.c_uint32),
        ('reserved', ctypes.c_uint32),
    ]


def _default_library() -> str:
    override = os.environ.get('CASTLE_CHROMIUM_ZLIB', '').strip()
    if override:
        return override
    return str(asset_path('castle', 'chromium_zlib.dll'))


@lru_cache(maxsize=4)
def _load(path: str):
    if os.name != 'nt':
        raise OSError('the bundled Chromium zlib backend is currently Windows-only')
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    lib = ctypes.CDLL(path)
    lib.zlibVersion.argtypes = []
    lib.zlibVersion.restype = ctypes.c_char_p
    lib.deflateInit2_.argtypes = [
        ctypes.POINTER(_ZStream), ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
    ]
    lib.deflateInit2_.restype = ctypes.c_int
    lib.deflate.argtypes = [ctypes.POINTER(_ZStream), ctypes.c_int]
    lib.deflate.restype = ctypes.c_int
    lib.deflateEnd.argtypes = [ctypes.POINTER(_ZStream)]
    lib.deflateEnd.restype = ctypes.c_int
    return lib


def available() -> bool:
    """Return whether the exact local Chromium backend can be loaded."""

    try:
        _load(_default_library())
        return True
    except (OSError, AttributeError):
        return False


def deflate_raw(data: bytes, *, level: int = _DEFAULT_LEVEL,
                mem_level: int = _DEFAULT_MEM_LEVEL,
                strategy: int = _DEFAULT_STRATEGY) -> bytes:
    """Compress *data* as raw DEFLATE using Chromium's zlib implementation.

    The parameters mirror the current browser/Node ``CompressionStream``
    defaults.  This function is deterministic and never launches Node or
    touches a browser.
    """

    source = bytes(data)
    lib = _load(_default_library())
    input_buf = ctypes.create_string_buffer(source or b'\0')
    # Raw DEFLATE cannot expand by more than a small constant for this use,
    # but leave ample room and grow safely if an unusual input trips the bound.
    capacity = max(1024, len(source) * 2 + 1024)
    output_buf = ctypes.create_string_buffer(capacity)
    stream = _ZStream()
    stream.next_in = ctypes.addressof(input_buf)
    stream.avail_in = len(source)
    stream.next_out = ctypes.addressof(output_buf)
    stream.avail_out = capacity
    version = lib.zlibVersion()
    rc = lib.deflateInit2_(
        ctypes.byref(stream), int(level), _Z_DEFLATED, -15,
        int(mem_level), int(strategy), version, ctypes.sizeof(_ZStream),
    )
    if rc != _Z_OK:
        raise RuntimeError(f'Chromium zlib deflateInit2_ failed: {rc}')
    try:
        while True:
            rc = lib.deflate(ctypes.byref(stream), _Z_FINISH)
            if rc == _Z_STREAM_END:
                return bytes(output_buf)[:int(stream.total_out)]
            if rc not in (_Z_OK, _Z_BUF_ERROR):
                raise RuntimeError(f'Chromium zlib deflate failed: {rc}')
            if stream.avail_out:
                # Z_BUF_ERROR with output space left is not recoverable by
                # changing buffers; preserve the diagnostic rather than loop.
                raise RuntimeError(
                    f'Chromium zlib made no progress (avail_in={stream.avail_in}, '
                    f'avail_out={stream.avail_out})')
            used = int(stream.total_out)
            capacity *= 2
            grown = ctypes.create_string_buffer(capacity)
            ctypes.memmove(grown, output_buf, used)
            output_buf = grown
            stream.next_out = ctypes.addressof(output_buf) + used
            stream.avail_out = capacity - used
    finally:
        lib.deflateEnd(ctypes.byref(stream))


__all__ = ['available', 'deflate_raw']
