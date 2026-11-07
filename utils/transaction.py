# -*- coding: utf-8 -*-
"""x-client-transaction-id（XCTID）纯算实现。

X 的前端对**每一个** API 请求现算一个一次性签名头 `x-client-transaction-id`。
写接口（发推 / 互动）和登录流缺它会被直接拒。

素材全部来自服务端可直接 GET 的静态资源，**不需要浏览器**：

1. `GET https://x.com/i/flow/login` 返回 app shell HTML，内含
   - `<meta name="twitter-site-verification" content="...">` —— base64 解出 48 字节 key；
   - 4 个 `<svg id="loading-x-anim-{0..3}">` —— 每个第 2 个 `<g>` 里 `<path d="...">` 是动画关键帧；
   - 内联 webpack runtime —— chunkId → 文件名映射。
2. 从映射里解析出 `ondemand.s.<hash>.js`，正则 `(\\w[NN], 16)` 取出 4 个下标 `[row, i1, i2, i3]`。

算法骨架（对应线上 `ondemand.s` 里 chunk 59924 / module 208932 的默认导出）::

    row_index   = key_bytes[idx[0]] % 16
    frame_time  = ∏ (key_bytes[i] % 16 for i in idx[1:])
    frame_row   = frames[key_bytes[5] % 4] 的 path 数据按 'C' 切出的第 row_index 行
    anim_key    = 在 frame_row 上做三次贝塞尔插值 + 颜色/旋转矩阵 → 十六进制串
    t           = floor((now_ms - 1682924400000) / 1000)
    digest      = sha256(f"{method}!{path}!{t}{KEYWORD}{anim_key}")
    payload     = key_bytes + t 的 4 字节小端 + digest[:16] + [3]
    XCTID       = base64(rnd + [b ^ rnd for b in payload])

因为末尾混入随机字节，**同输入不同输出**；固定随机源时可逐字节对拍，线上以服务端接受为准：
L1 固定随机源逐字节对拍，L2 服务端接受。
"""

import base64
import hashlib
import json
import math
import os
import random
import re
import time

import requests

from utils.frontend import (fetch_chunk, fetch_shell_html, parse_animation_frames,
                            parse_chunk_manifest, parse_site_verification)
from utils.resource_paths import asset_path

# 2023-05-01T00:00:00Z，前端写死的时间原点
EPOCH_SECONDS = 1682924400
# 参与 sha256 的固定盐
DEFAULT_KEYWORD = 'obfiowerehiring'
# payload 末尾固定追加的常量字节
ADDITIONAL_RANDOM_NUMBER = 3
TOTAL_TIME = 4096

_INDICES_RE = re.compile(r'\(\w\[(\d{1,2})\],\s*16\)')


# --------------------------------------------------------------------------- #
# 数学部分：三次贝塞尔 + 插值 + 旋转矩阵
# --------------------------------------------------------------------------- #

def _bezier(a: float, b: float, m: float) -> float:
    return 3.0 * a * (1 - m) * (1 - m) * m + 3.0 * b * (1 - m) * m * m + m * m * m


class Cubic:
    """前端 `cubic-bezier(c0, c1, c2, c3)` 的求值器。"""

    def __init__(self, curves):
        self.curves = curves

    def get_value(self, target: float) -> float:
        start_gradient = end_gradient = 0.0
        start, end, mid = 0.0, 1.0, 0.0
        c = self.curves

        if target <= 0.0:
            if c[0] > 0.0:
                start_gradient = c[1] / c[0]
            elif not c[1] and c[2] > 0.0:
                start_gradient = c[3] / c[2]
            return start_gradient * target

        if target >= 1.0:
            if c[2] < 1.0:
                end_gradient = (c[3] - 1.0) / (c[2] - 1.0)
            elif c[2] == 1.0 and c[0] < 1.0:
                end_gradient = (c[1] - 1.0) / (c[0] - 1.0)
            return 1.0 + end_gradient * (target - 1.0)

        while start < end:
            mid = (start + end) / 2
            estimate = _bezier(c[0], c[2], mid)
            if abs(target - estimate) < 0.00001:
                return _bezier(c[1], c[3], mid)
            if estimate < target:
                start = mid
            else:
                end = mid
        return _bezier(c[1], c[3], mid)


def _interpolate(from_list, to_list, f):
    return [a * (1 - f) + b * f for a, b in zip(from_list, to_list)]


def _rotation_matrix(degrees: float):
    rad = math.radians(degrees)
    return [math.cos(rad), -math.sin(rad), math.sin(rad), math.cos(rad)]


def _float_to_hex(x: float) -> str:
    """复刻前端把浮点转十六进制的写法（含小数部分，逐位展开）。"""
    result = []
    quotient = int(x)
    fraction = x - quotient

    while quotient > 0:
        quotient = int(x / 16)
        remainder = int(x - (quotient * 16))
        result.insert(0, chr(remainder + 55) if remainder > 9 else str(remainder))
        x = float(quotient)

    if fraction == 0:
        return ''.join(result)

    result.append('.')
    while fraction > 0:
        fraction *= 16
        integer = int(fraction)
        fraction -= integer
        result.append(chr(integer + 55) if integer > 9 else str(integer))
    return ''.join(result)


def _solve(value: float, min_val: float, max_val: float, rounding: bool):
    result = value * (max_val - min_val) / 255 + min_val
    return math.floor(result) if rounding else round(result, 2)


def _is_odd(num: int) -> float:
    return -1.0 if num % 2 else 0.0


# --------------------------------------------------------------------------- #
# 素材抓取
# --------------------------------------------------------------------------- #

def fetch_indices(html: str, session: requests.Session = None, proxies: dict = None) -> list:
    """从 app shell 解析出 ondemand.s 的地址，下载后取出 4 个 key 下标。"""
    url = parse_chunk_manifest(html).get('ondemand.s')
    if not url:
        raise RuntimeError('webpack chunk 清单里找不到 ondemand.s')
    indices = [int(x) for x in _INDICES_RE.findall(fetch_chunk(url, session, proxies))]
    if len(indices) < 4:
        raise RuntimeError(f'ondemand.s 里只取到 {len(indices)} 个下标，期望 ≥4：{url}')
    return indices[:4]


# --------------------------------------------------------------------------- #
# 生成器
# --------------------------------------------------------------------------- #

class ClientTransaction:
    """一次装配、多次生成。素材（key / frames / indices）在会话内是稳定的。

    典型用法::

        ct = ClientTransaction.from_network()
        ct.generate('POST', '/i/api/graphql/xxx/CreateTweet')
    """

    def __init__(self, key: str, frames: list, indices: list):
        self.key = key
        self.frames = frames
        self.indices = list(indices)
        self.key_bytes = list(base64.b64decode(key))
        self.animation_key = self._build_animation_key()

    # ---- 构造 ----------------------------------------------------------- #

    @classmethod
    def from_html(cls, html: str, indices: list):
        return cls(parse_site_verification(html), parse_animation_frames(html), indices)

    @classmethod
    def from_network(cls, session: requests.Session = None, proxies: dict = None):
        html = fetch_shell_html(session, proxies)
        return cls.from_html(html, fetch_indices(html, session, proxies))

    @classmethod
    def from_cached(cls, path: str = None):
        """Load a previously captured XCTID profile without network I/O.

        The signature itself is pure Python, but the first construction normally
        downloads the public key/SVG/chunk materials.  Login must not become
        dependent on a second, unrelated TLS fetch (the jetfuel request has its
        own transport and may be reachable when the shell fetch is reset), so a
        verified local capture is an explicit offline fallback.  Callers can
        override the path with ``XCTID_PROFILE_FILE``.
        """
        candidate = path or os.environ.get('XCTID_PROFILE_FILE', '').strip()
        if not candidate:
            candidate = asset_path('transaction_l1.json')
        with open(candidate, encoding='utf-8') as fp:
            profile = json.load(fp)
        key = profile.get('key')
        frames = profile.get('frames')
        indices = profile.get('indices')
        if not isinstance(key, str) or not isinstance(frames, list) \
                or not isinstance(indices, list):
            raise ValueError(f'无效 XCTID profile: {candidate}')
        return cls(key, frames, indices)

    # ---- 动画 key ------------------------------------------------------- #

    def _frame_rows(self) -> list:
        """选中一个动画帧，把它的 path 数据切成若干行数字。"""
        frame = self.frames[self.key_bytes[5] % 4]
        # path 形如 "M 1 2 C 3 4 5 6 7 8 C ..."，前 9 个字符是 "M " 起手指令
        return [
            [int(x) for x in re.sub(r'[^\d]+', ' ', segment).strip().split()]
            for segment in frame[9:].split('C')
        ]

    def _build_animation_key(self) -> str:
        row_index = self.key_bytes[self.indices[0]] % 16
        frame_time = 1
        for index in self.indices[1:]:
            frame_time *= self.key_bytes[index] % 16
        rows = self._frame_rows()
        return self._animate(rows[row_index], frame_time / TOTAL_TIME)

    @staticmethod
    def _animate(frame_row: list, target_time: float) -> str:
        from_color = [float(v) for v in (*frame_row[:3], 1)]
        to_color = [float(v) for v in (*frame_row[3:6], 1)]
        from_rotation = [0.0]
        to_rotation = [_solve(float(frame_row[6]), 60.0, 360.0, True)]

        curves = [_solve(float(v), _is_odd(i), 1.0, False)
                  for i, v in enumerate(frame_row[7:])]
        progress = Cubic(curves).get_value(target_time)

        color = [max(v, 0) for v in _interpolate(from_color, to_color, progress)]
        rotation = _interpolate(from_rotation, to_rotation, progress)
        matrix = _rotation_matrix(rotation[0])

        parts = [format(round(v), 'x') for v in color[:-1]]
        for value in matrix:
            rounded = abs(round(value, 2))
            hex_value = _float_to_hex(rounded)
            if hex_value.startswith('.'):
                parts.append(f'0{hex_value}'.lower())
            else:
                parts.append(hex_value or '0')
        parts.extend(['0', '0'])
        return re.sub(r'[.-]', '', ''.join(parts))

    # ---- 生成 ----------------------------------------------------------- #

    def generate(self, method: str, path: str, time_now: int = None,
                 random_byte: int = None) -> str:
        """算一个 XCTID。

        :param method: HTTP 方法，大写。
        :param path: 请求 pathname，**不含 query**，如 `/i/api/graphql/xx/TweetDetail`。
        :param time_now: 覆盖时间戳（仅测试对拍用）。
        :param random_byte: 覆盖随机字节（仅测试对拍用）。
        """
        if time_now is None:
            time_now = math.floor(time.time()) - EPOCH_SECONDS
        time_bytes = [(time_now >> (i * 8)) & 0xFF for i in range(4)]

        message = f'{method}!{path}!{time_now}{DEFAULT_KEYWORD}{self.animation_key}'
        digest = hashlib.sha256(message.encode()).digest()

        payload = [*self.key_bytes, *time_bytes, *digest[:16], ADDITIONAL_RANDOM_NUMBER]
        rnd = random.randrange(256) if random_byte is None else random_byte
        out = bytes([rnd, *[b ^ rnd for b in payload]])
        return base64.b64encode(out).decode().rstrip('=')
