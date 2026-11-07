# -*- coding: utf-8 -*-
"""Castle token generation for the jetfuel login flow.

`$castle_token` 是 Castle.io 反自动化 SDK 的产物。登录页实际加载的是
`responsive-web/ondemand.castle.*.js` webpack chunk；无 profile 时默认 runner 使用当前保存的
`castle.js`，而传入完整 749 槽浏览器 profile 时自动切换到并回放当前 x-web UMD。
默认路径仍是不反混淆、直接把 SDK 塞进 Node 跑
`configure({pk}) → createRequestToken()`，浏览器实抓的 token 结构已对齐到同一 UMD
版本。完整的纯算组装入口是 :func:`generate_pure`；它要求调用方先提供同一浏览器状态下
完整的 `Rl` 向量（旧版 632 槽、当前 x-web 版 749 槽均可），并默认使用 Python 直接调用的 Chromium-zlib profile，使 native
`CompressionStream` 字节与浏览器一致。Castle 自带 raw-DEFLATE fallback 仍可通过
`compression='fallback'` 显式选择。
没有完整 profile 时 `generate()` 不会偷偷伪造 UMD 信号；只有显式提供 749 槽向量才启用 UMD
回放。

与 `ui_metrics.py` 同一设计：把尚未拆完的浏览器采集隔离在 Node 侧
（`static/castle/`：`run.js` 补环境 + `env_core.js` 代理引擎 + `castle.js` 打过补丁的 SDK），
Python 侧只负责调它、取 token。每次登录的每一步都要一枚**新鲜** token。

公钥（`pk_...`）随前端发版可能轮换，存在 `static/castle/castle_meta.json`（由
`utils.castle_refresh` 刷新时写入），运行时读它并经 `CASTLE_PK` 传给 run.js；
缺省回落到 run.js 内置值。SDK 漂移检测/刷新见 `utils/castle_refresh.py`。
"""

import base64
import json
import os
import re
import subprocess
import tempfile
import time

from utils.castle_crypto import (
    CURRENT_TOKEN_PREFIX,
    LIVE_UMD_TOKEN_PREFIX,
    build_current_token_pure,
    build_current_token_raw_pure,
    parse_payload,
)
from utils.resource_paths import asset_path

CASTLE_DIR = str(asset_path('castle'))
RUNNER = os.path.join(CASTLE_DIR, 'run.js')
META_PATH = os.path.join(CASTLE_DIR, 'castle_meta.json')
DEFAULT_CASTLE_FILE = 'castle.js'
DEFAULT_UMD = 'castle.umd-BneRArir.js'
DEFAULT_UMD_RUNTIME = 'rolldown-runtime-XXLRXQBO.js'
NODE = os.environ.get('NODE_BIN', 'node')
_TOKEN_RE = re.compile(r'__CASTLE_TOKEN__=(\S+)')


def read_public_key() -> str:
    """从 castle_meta.json 读当前公钥；没有就返回空串（run.js 用内置默认）。"""
    try:
        with open(META_PATH, encoding='utf-8') as fp:
            return json.load(fp).get('public_key', '') or ''
    except (OSError, ValueError):
        return ''


def read_token_prefix() -> str:
    """Return the prefix recorded with the current Castle bundle baseline."""
    try:
        with open(META_PATH, encoding='utf-8') as fp:
            value = json.load(fp).get('token_prefix', '') or ''
            if isinstance(value, str) and value and '|' not in value:
                return value
    except (OSError, ValueError):
        pass
    return CURRENT_TOKEN_PREFIX


def generate(timeout: int = 60, castle_file: str = None, pk: str = None,
             module_id: int = None, cookie: str = None,
             local_storage: dict = None, session_storage: dict = None,
             snapshot: dict = None, rl_overrides=None) -> str:
    """跑一次现有 Castle 运行器，返回一枚新鲜 castle_token（形如 `<prefix>|JAAx…`）。

    :param castle_file: static/castle/ 下要加载的 SDK 文件；省略时使用登录页当前
        x-web UMD 包。刷新旧 webpack 候选时可显式传入 `castle.candidate.js`。
    :param pk: 覆盖公钥；不传则读 castle_meta.json，再回落 run.js 内置默认。
    :param module_id: webpack module id；前端发版可能变化，不传则由 run.js 自动探测。
    :param cookie: 当前 x.com 页面可见的 cookie 串，传给 Castle 的 document.cookie。
    :param local_storage: 当前页面 localStorage 快照（至少包含 __cuid）。
    :param session_storage: 当前页面 sessionStorage 快照；登录页通常为空。
    :param snapshot: 可选的同一页面 Castle 环境快照。支持
        ``own_keys``/``enum_keys``、``query_collection``、``all_elements``、
        ``scripts``、``style_counts``、``computed_keys`` 和 ``performance_marks``；值会按浏览器
        的 JSON 顺序直接传给运行器，不会在 Python 侧补齐或截断。这样调用方
        可以复用真实页面的 cookie/localStorage，同时把采集字段逐项带入本地
         计算链。``all_elements`` 也可传 ``{'meta': [...], 'count': 474}``。
    :param rl_overrides: 可选的浏览器采集向量。旧版 ``castle.js`` 接受
        ``{index: value}``；当前 x-web UMD 接受完整的 749 槽 list，或
        ``{'rl': [...]}`` profile。传入后 runner 只在本次临时 Node 进程中
        替换对应采集槽位，不会发出任何网络请求。未传入时仍走本地补环境路径。
    """
    env = dict(os.environ, TOKEN_TIMEOUT='9000')
    _snapshot_temp_files = []
    castle_file_explicit = castle_file is not None
    if castle_file is None:
        castle_file = DEFAULT_CASTLE_FILE
    if castle_file.startswith('castle.umd-'):
        env.pop('CASTLE_FILE', None)
        env['CASTLE_UMD_FILE'] = castle_file
        env['CASTLE_UMD_RUNTIME'] = env.get('CASTLE_UMD_RUNTIME', DEFAULT_UMD_RUNTIME)
    else:
        env['CASTLE_FILE'] = castle_file
    if module_id is not None:
        env['CASTLE_MODULE_ID'] = str(module_id)
    # Castle 会把页面状态纳入设备采集。显式传入时优先于进程环境，避免把
    # 上一次登录/别的浏览器的 cookie 混入本次 token；未传入则保留调试环境覆盖。
    if cookie is not None:
        env.pop('CASTLE_COOKIE', None)
        env['CASTLE_COOKIE_B64'] = base64.b64encode(
            cookie.encode('utf-8')).decode('ascii')
    if local_storage is not None:
        env['CASTLE_LOCAL_STORAGE_B64'] = base64.b64encode(
            json.dumps(local_storage, ensure_ascii=False, separators=(',', ':'))
            .encode('utf-8')).decode('ascii')
    if session_storage is not None:
        env['CASTLE_SESSION_STORAGE_B64'] = base64.b64encode(
            json.dumps(session_storage, ensure_ascii=False, separators=(',', ':'))
            .encode('utf-8')).decode('ascii')
    # 真实浏览器快照是可选的；不传时保留 runner 的版本化默认值。
    # 只接受已知的 Castle 输入，避免把任意对象误塞进子进程环境。
    snapshot = snapshot or {}
    def _put_json(name, value):
        env[name] = base64.b64encode(
            json.dumps(value, ensure_ascii=False, separators=(',', ':'))
            .encode('utf-8')).decode('ascii')
    def _put_json_file(name, value):
        fd, path = tempfile.mkstemp(prefix='castle-snapshot-', suffix='.json')
        with os.fdopen(fd, 'w', encoding='utf-8') as fp:
            json.dump(value, fp, ensure_ascii=False, separators=(',', ':'))
        _snapshot_temp_files.append(path)
        env[name] = path
    if snapshot.get('own_keys') is not None:
        _put_json_file('CASTLE_OWN_KEYS_FILE', snapshot['own_keys'])
        env.pop('CASTLE_OWN_KEYS_JSON_B64', None)
    if snapshot.get('enum_keys') is not None:
        _put_json_file('CASTLE_ENUM_KEYS_FILE', snapshot['enum_keys'])
        env.pop('CASTLE_ENUM_KEYS_JSON_B64', None)
    if snapshot.get('query_collection') is not None:
        _put_json('CASTLE_QUERY_COLLECTION_JSON_B64', snapshot['query_collection'])
    if snapshot.get('all_elements') is not None:
        all_elements = snapshot['all_elements']
        if isinstance(all_elements, dict) and ('meta' in all_elements or 'all' in all_elements):
            if all_elements.get('count') is not None:
                env['CASTLE_ALL_ELEMENTS_COUNT'] = str(all_elements['count'])
            all_elements = all_elements.get('meta', all_elements.get('all'))
        _put_json('CASTLE_ALL_ELEMENTS_JSON_B64', all_elements)
        env['CASTLE_ALL_ELEMENTS'] = '1'
    if snapshot.get('all_elements_count') is not None:
        env['CASTLE_ALL_ELEMENTS_COUNT'] = str(snapshot['all_elements_count'])
    if snapshot.get('scripts') is not None:
        _put_json('CASTLE_SCRIPTS_JSON_B64', snapshot['scripts'])
    if snapshot.get('style_counts') is not None:
        _put_json('CASTLE_STYLE_COUNTS_B64', snapshot['style_counts'])
    if snapshot.get('computed_keys') is not None:
        _put_json_file('CASTLE_COMPUTED_KEYS_FILE', snapshot['computed_keys'])
        env.pop('CASTLE_COMPUTED_KEYS_JSON_B64', None)
    if snapshot.get('performance_marks') is not None:
        marks = snapshot['performance_marks']
        env['CASTLE_PERFORMANCE_MARKS'] = ','.join(str(x) for x in marks) \
            if not isinstance(marks, str) else marks
    # 允许调用方通过环境变量指定一次浏览器采集向量文件；不设置时完全走
    # 本地补环境，不会隐式读取工作区中的样例文件。
    if rl_overrides is None:
        profile_path = os.environ.get('CASTLE_PROFILE_FILE', '').strip()
        if profile_path:
            try:
                with open(profile_path, encoding='utf-8') as fp:
                    profile = json.load(fp)
                # DevTools evaluate_script serializes a returned JSON string
                # one level deeper; accept that export shape directly.
                if isinstance(profile, str):
                    try:
                        profile = json.loads(profile)
                    except (TypeError, ValueError):
                        pass
                if isinstance(profile, dict) and profile.get('rl') is not None:
                    rl_overrides = profile['rl']
                elif isinstance(profile, list):
                    # A direct JSON export from the browser is the signal
                    # vector itself rather than a ``{"rl": ...}`` wrapper.
                    rl_overrides = profile
            except (OSError, ValueError):
                rl_overrides = None

    def _umd_signal_vector(value):
        """Normalize a captured current-UMD signal profile to a list."""
        if isinstance(value, dict):
            for key in ('rl', 'signals', 'profile'):
                nested = value.get(key)
                if isinstance(nested, (list, tuple)):
                    value = nested
                    break
            else:
                numeric = {}
                for key, item in value.items():
                    try:
                        index = int(key)
                    except (TypeError, ValueError):
                        continue
                    if index >= 0:
                        numeric[index] = item
                if numeric and max(numeric) + 1 == len(numeric):
                    value = [numeric[index] for index in range(len(numeric))]
        if isinstance(value, (list, tuple)):
            values = list(value)
            if len(values) == 749:
                return values
        return None

    umd_values = _umd_signal_vector(rl_overrides)
    # A complete live vector is unambiguous: when the caller did not pin a
    # bundle, select the same UMD revision loaded by the x-web login page.
    if umd_values is not None and not castle_file_explicit:
        castle_file = DEFAULT_UMD
        env.pop('CASTLE_FILE', None)
        env['CASTLE_UMD_FILE'] = castle_file
        env['CASTLE_UMD_RUNTIME'] = env.get('CASTLE_UMD_RUNTIME', DEFAULT_UMD_RUNTIME)

    # UMD replay applies the complete signal vector immediately before its
    # serializer creates the Uint8Array.  Legacy replay still uses the Rl
    # setter proxy.  Both paths are opt-in and never emit network traffic.
    if castle_file.startswith('castle.umd-') and umd_values is not None:
        _put_json_file('CASTLE_UMD_SIGNAL_OVERRIDES_FILE', umd_values)
    elif rl_overrides:
        if not isinstance(rl_overrides, dict):
            raise ValueError('legacy rl_overrides must be a numeric-key mapping')
        env['CASTLE_RL_OVERRIDES_JSON'] = json.dumps(
            {str(k): v for k, v in rl_overrides.items()},
            ensure_ascii=False, separators=(',', ':'))
        fd, dump_path = tempfile.mkstemp(prefix='castle-rl-', suffix='.json')
        os.close(fd)
        fd, trace_path = tempfile.mkstemp(prefix='castle-rl-trace-', suffix='.log')
        os.close(fd)
        _snapshot_temp_files.extend([dump_path, trace_path])
        env['CASTLE_RL_DUMP'] = dump_path
        env['CASTLE_RL_SET_TRACE'] = trace_path
    pk = pk or read_public_key()
    if pk:
        env['CASTLE_PK'] = pk
    try:
        try:
            result = subprocess.run([NODE, RUNNER], cwd=CASTLE_DIR, env=env,
                                    capture_output=True, timeout=timeout)
        except FileNotFoundError as exc:
            raise RuntimeError(
                f'找不到 node 可执行文件（{NODE}），生成 castle_token 需要它；'
                f'可用环境变量 NODE_BIN 指定路径') from exc
    finally:
        for path in _snapshot_temp_files:
            try:
                os.unlink(path)
            except OSError:
                pass
    stdout = result.stdout.decode('utf-8', 'replace')
    match = _TOKEN_RE.search(stdout)
    if not match:
        stderr = result.stderr.decode('utf-8', 'replace')
        raise RuntimeError(
            f'castle_token 生成失败（exit={result.returncode}）。'
            f'stderr 末尾：{stderr[-400:]}')
    token = match.group(1)
    # Validate the wire framing in Python before handing the value to the
    # login request.  This is intentionally only a structural check: the
    # remaining seed/KDF and signal builder are not silently reimplemented
    # here until they have their own byte-for-byte oracle vectors.
    try:
        parts = token.split('|', 1)
        if len(parts) != 2:
            raise ValueError('missing token separator')
        parse_payload(base64.b64decode(parts[1], validate=True))
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f'castle_token 生成了无效 payload 结构: {exc}') from exc
    return token


def generate_pure(rl_values, *, timestamp_ms: int = None,
                  prefix: str = None, compression: str = 'native') -> str:
    """Build a current Castle token from a complete captured ``Rl`` vector.

    This is intentionally separate from :func:`generate`: the latter keeps
    the legacy runner for callers that have not yet supplied a real browser
    vector, while this function performs no Node calls, no DOM emulation, and
    no network I/O.  ``rl_values`` may be the full profile object
    (``{"rl": ..., "prefix": ...}``), a serialized browser capture
    (``{"raw": [...], "prefix": ...}`` or base64 ``rawB64``), the JSON
    object captured by the diagnostics (numeric string keys), or an already
    ordered list/tuple.  The
    legacy baseline has 632 slots; the live x-web UMD currently has 749.
    Partial input is rejected so a superficially plausible but server-invalid
    token is never emitted.

    ``compression='native'`` (the default) uses the bundled Chromium-zlib
    profile; ``compression='fallback'`` uses Castle's own fallback stream.
    Native mode raises if its local backend is unavailable instead of
    silently changing token length.  If ``timestamp_ms`` is omitted, the
    current wall clock is used.  The timestamp is generated independently by
    the bundle and is not an ``Rl`` slot; for browser byte comparison, pass an
    explicit timestamp from the same token capture.  Profile objects may carry
    ``timestamp_ms``/``timestamp`` for this purpose.
    """

    profile_prefix = None
    profile_compression = None
    profile_inner_variant = None
    profile_timestamp = None
    if isinstance(rl_values, dict) and 'rl' in rl_values:
        profile_prefix = rl_values.get('prefix')
        profile_compression = rl_values.get('compression')
        profile_inner_variant = rl_values.get('inner_variant')
        profile_timestamp = rl_values.get('timestamp_ms',
                                          rl_values.get('timestamp'))
        rl_values = rl_values['rl']
    elif isinstance(rl_values, dict) and ('raw' in rl_values or
                                          'rawB64' in rl_values):
        profile_prefix = rl_values.get('prefix')
        # A browser-exported profile may carry the exact compression and
        # timestamp framing variant used by that UMD revision.  Preserve
        # those fields instead of silently reverting to the defaults.
        profile_compression = rl_values.get('compression')
        profile_inner_variant = rl_values.get('inner_variant')
        profile_timestamp = rl_values.get('timestamp_ms',
                                          rl_values.get('timestamp'))
        raw = rl_values.get('raw')
        if raw is None:
            try:
                raw = base64.b64decode(rl_values['rawB64'], validate=True)
            except (TypeError, ValueError) as exc:
                raise ValueError("rawB64 must be valid base64") from exc
        if timestamp_ms is None:
            timestamp_ms = profile_timestamp
        if timestamp_ms is None:
            timestamp_ms = int(time.time() * 1000)
        if compression == 'native' and profile_compression is not None:
            compression = profile_compression
        return build_current_token_raw_pure(
            raw, timestamp_ms=int(timestamp_ms), prefix=prefix or profile_prefix,
            compression=compression, inner_variant=profile_inner_variant,
            validate=True)
    if prefix is None:
        if profile_prefix:
            prefix = profile_prefix
        else:
            # A bare 749-slot vector is unambiguously the live x-web UMD
            # profile; retain the historical marker for 632-slot vectors.
            vector_len = (len(rl_values) if isinstance(rl_values, (dict, list, tuple))
                          else 0)
            prefix = (LIVE_UMD_TOKEN_PREFIX if vector_len == 749
                      else read_token_prefix())
    if isinstance(rl_values, dict):
        try:
            values = [rl_values[str(index)] if str(index) in rl_values
                      else rl_values[index] for index in range(len(rl_values))]
        except (KeyError, TypeError):
            raise ValueError("Rl mapping must contain contiguous 0..N-1 keys")
    elif isinstance(rl_values, (list, tuple)):
        values = list(rl_values)
    else:
        raise TypeError("rl_values must be a list, tuple, or numeric-key mapping")
    if len(values) not in (632, 749):
        raise ValueError(
            f"current Castle Rl requires a complete 632-slot (legacy) or "
            f"749-slot (live) vector, got {len(values)}")
    if timestamp_ms is None:
        timestamp_ms = profile_timestamp
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)
    if compression == 'native' and profile_compression is not None:
        compression = profile_compression
    return build_current_token_pure(values, timestamp_ms=int(timestamp_ms),
                                    prefix=prefix, compression=compression,
                                    inner_variant=profile_inner_variant)
