# -*- coding: utf-8 -*-
"""castle SDK 抗漂移：纯算刷新 + 漂移检测。

`static/castle/castle.js` 是打过补丁的 Castle SDK（`ondemand.castle.js`）。它随前端发版会变：
文件内容变、公钥（`responsive_web_castle_public_key`）可能轮换、被 patch 命中的**压缩变量名**
也会重命名。本模块把「拉线上 SDK → 打补丁 → 落盘」做成纯算流程（同 `refresh-graphql`）：

- **`refresh()`**：从线上 app shell 定位并下载 `ondemand.castle.js`（原始未打补丁），
  用**对变量名重命名容忍**的正则重打补丁（`make_patch` 逻辑的产品化），并顺带抓当前公钥；
  写盘前**先真生成一枚 token 自检**，通过才原子替换 `castle.js`，绝不静默落一个坏 SDK。
- **`check()`**：只读比对——线上原始 SDK 的 sha / 公钥 / patch 命中数是否相对本地漂移，
  不改任何文件。用来在生成器失效**之前**预警（同 `verify_registry --online` 的定位）。

补丁只做「触发/兜底」，不碰密码学：
  1. 强制 `NE` 直接调 crypto builder，绕过 VM 的门控；
  2. 给 5 个 deferred 工厂注入幂等兜底 timeout，解锁无头环境永不 resolve 的行为/传感器信号。

**局限（诚实标注）**：补丁按当前压缩结构定位。若 X 只是重命名变量，容忍正则能自动重打；
若改了代码结构（工厂增减/触发链改写），自检会失败并明确报出哪条没打上——此时补丁需人工
重新推导，不是纯算能力问题，而是「反自动化 SDK 换了实现」。
"""

import hashlib
import json
import os
import re
import time

import requests

from utils import castle_token
from utils.frontend import fetch_chunk, fetch_shell_html, parse_all_assets
from utils.resource_paths import asset_path

CASTLE_DIR = str(asset_path('castle'))
CASTLE_JS = os.path.join(CASTLE_DIR, 'castle.js')
CANDIDATE_JS = os.path.join(CASTLE_DIR, 'castle.candidate.js')
META_PATH = os.path.join(CASTLE_DIR, 'castle_meta.json')

# 公钥来自 __INITIAL_STATE__.featureSwitch.defaultConfig 的这条开关
_PK_RE = re.compile(r'"responsive_web_castle_public_key":\{"value":"([^"]+)"')
# The module id is emitted as the key of the Castle webpack chunk.  It has
# changed between client-web releases (321128 -> 855881), so do not bake it
# into the runner.
_MODULE_ID_RE = re.compile(
    r'webpackChunk_twitter_responsive_web\s*=.*?\.push\(\[\[[^\]]+\],\{(\d+)\(',
    re.S)

# --- 补丁定位（对压缩变量名重命名容忍）--- #
# 强制触发 crypto builder：n[2]=(function(){})[pD][pD](n[16],n[15],n[17]),n[2]},NP=
#   → n[2]=n[17](),n[2]},NP=  （pD / 各 n[..] 下标 / 尾随 NP 都允许改名）
_FORCE_RE = re.compile(
    r'n\[2\]=\(function\(\)\{\}\)\[(\w+)\]\[\1\]\('
    r'n\[(\d+)\],n\[(\d+)\],n\[(\d+)\]\),n\[2\]\},(\w+)=')
# 5 个 deferred 工厂：function oK(n){return new wV(function(t,r){ …（wV 允许改名，工厂名暂固定）
_FACTORY_NAMES = ('oK', 'oj', 'el', 'eI', 'uI')
_FACTORY_INJECT = r'\1setTimeout(function(){try{t(void 0)}catch(e){}},700);'


def _force_repl(match: re.Match) -> str:
    # match.group(4) = crypto builder 的下标（原 17）；group(5) = 尾随变量（原 NP）
    return f'n[2]=n[{match.group(4)}](),n[2]}},{match.group(5)}='


def _factory_re(name: str) -> re.Pattern:
    return re.compile(r'(function %s\(n\)\{return new \w+\(function\(t,r\)\{)' % re.escape(name))


def apply_patch(orig: str) -> tuple:
    """对原始 SDK 文本打补丁。返回 (patched_text, counts)。counts 记录每条命中数。"""
    counts = {}
    patched, counts['force'] = _FORCE_RE.subn(_force_repl, orig)
    for name in _FACTORY_NAMES:
        patched, counts[name] = _factory_re(name).subn(_FACTORY_INJECT, patched)
    return patched, counts


def _patch_ok(counts: dict) -> bool:
    """每条补丁应恰好命中一次。"""
    return counts.get('force') == 1 and all(counts.get(n) == 1 for n in _FACTORY_NAMES)


def _locate_castle_url(html: str) -> str:
    assets = parse_all_assets(html)
    hits = [url for name, url in assets.items() if 'castle' in name.lower()]
    if not hits:
        raise RuntimeError('前端分片清单里找不到 castle 分片（ondemand.castle）')
    return hits[0]


def _parse_module_id(orig: str) -> int:
    # A Castle chunk starts with a webpack module map entry such as
    # ``{855881(n,r){``.  Restrict the match to a numeric property followed
    # by the module function; this avoids accidentally selecting constants
    # elsewhere in the minified source.
    match = re.search(r'\{(\d+)\([^{}]{0,80}\)\{', orig)
    if not match:
        raise RuntimeError('Castle 分片里找不到 webpack 模块 id')
    return int(match.group(1))


def _parse_public_key(html: str) -> str:
    match = _PK_RE.search(html)
    if not match:
        raise RuntimeError('app shell 里找不到 responsive_web_castle_public_key')
    return match.group(1)


def _fetch_live(proxies: dict = None) -> dict:
    """拉线上原始 SDK + 公钥 + 来源信息（不打补丁、不落盘）。"""
    session = requests.Session()
    html = fetch_shell_html(session, proxies)
    url = _locate_castle_url(html)
    orig = fetch_chunk(url, session, proxies)
    return {
        'url': url,
        'orig': orig,
        'orig_sha256': hashlib.sha256(orig.encode('utf-8', 'replace')).hexdigest(),
        'public_key': _parse_public_key(html),
        'module_id': _parse_module_id(orig),
    }


def load_meta() -> dict:
    try:
        with open(META_PATH, encoding='utf-8') as fp:
            return json.load(fp)
    except (OSError, ValueError):
        return {}


def _valid_token(tok: str) -> bool:
    return bool(re.match(r'^[A-Za-z0-9_-]{6,16}\|.+', tok, re.S)) and len(tok) > 4000


def refresh(proxies: dict = None, verbose: bool = True) -> dict:
    """拉线上 SDK → 打补丁 → 自检生成 token → 原子替换 castle.js，并写 meta。"""
    live = _fetch_live(proxies)
    if verbose:
        print(f'castle 分片：{live["url"]}')
        print(f'原始 sha256={live["orig_sha256"][:16]}… 公钥={live["public_key"]}')

    # Newer Castle chunks may already run correctly in the maintained Node
    # environment and no longer match the legacy trigger patches.  Try the
    # pristine chunk first; use the old patch recipe only as a compatibility
    # fallback for older releases.
    patched, counts = live['orig'], {}
    patch_mode = 'unpatched'
    try:
        with open(CANDIDATE_JS, 'w', encoding='utf-8') as fp:
            fp.write(live['orig'])
        tok = castle_token.generate(castle_file=os.path.basename(CANDIDATE_JS),
                                    pk=live['public_key'], module_id=live['module_id'])
        if not _valid_token(tok):
            raise RuntimeError('pristine token validation failed')
    except Exception:
        patched, counts = apply_patch(live['orig'])
        patch_mode = 'patched'
        if verbose:
            print('原始 Castle 自检失败，回退到兼容补丁模式')
        if not _patch_ok(counts):
            raise RuntimeError(
                f'补丁未按预期命中（{counts}）——线上 SDK 结构可能变了，补丁需重新推导。'
                f'castle.js 未改动。')
        with open(CANDIDATE_JS, 'w', encoding='utf-8') as fp:
            fp.write(patched)
        tok = castle_token.generate(castle_file=os.path.basename(CANDIDATE_JS),
                                    pk=live['public_key'], module_id=live['module_id'])
        if not _valid_token(tok):
            raise RuntimeError(f'自检 token 结构不对：{tok[:24]!r}')
    if verbose:
        print(f'模式：{patch_mode}  模块 id：{live["module_id"]}  补丁命中：{counts or "不需要"}')
    try:
        if verbose:
            print(f'自检 token：len={len(tok)} prefix={tok.split("|", 1)[0]}')
        os.replace(CANDIDATE_JS, CASTLE_JS)
    finally:
        if os.path.exists(CANDIDATE_JS):
            os.remove(CANDIDATE_JS)

    meta = {
        'generatedAt': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'source_url': live['url'],
        'orig_sha256': live['orig_sha256'],
        'public_key': live['public_key'],
        'token_prefix': tok.split('|', 1)[0],
        'patch_counts': counts,
        'patch_mode': patch_mode,
        'module_id': live['module_id'],
    }
    with open(META_PATH, 'w', encoding='utf-8') as fp:
        json.dump(meta, fp, ensure_ascii=False, indent=2)
    if verbose:
        print(f'已更新 {CASTLE_JS} 与 castle_meta.json')
    return meta


def check(proxies: dict = None, verbose: bool = True) -> dict:
    """只读漂移检测：线上原始 SDK 的 sha / 公钥 / patch 命中数是否相对本地漂移。"""
    live = _fetch_live(proxies)
    meta = load_meta()
    _, counts = apply_patch(live['orig'])

    report = {
        'sdk_changed': bool(meta.get('orig_sha256')) and meta['orig_sha256'] != live['orig_sha256'],
        'pk_changed': bool(meta.get('public_key')) and meta['public_key'] != live['public_key'],
        'patch_ok': _patch_ok(counts),
        'online_pk': live['public_key'],
        'local_pk': meta.get('public_key'),
        'online_sha256': live['orig_sha256'],
        'local_sha256': meta.get('orig_sha256'),
        'patch_counts': counts,
        'module_id': live['module_id'],
        'patch_mode': meta.get('patch_mode', 'patched'),
        'has_meta': bool(meta),
    }
    # A pristine-mode release is valid even when the legacy patch anchors no
    # longer exist.  Only require patch hits when the local baseline used them.
    report['module_changed'] = bool(meta.get('module_id')) and meta.get('module_id') != live['module_id']
    needs_patch = meta.get('patch_mode', 'patched') != 'unpatched'
    report['drifted'] = (report['sdk_changed'] or report['pk_changed'] or
                         report['module_changed'] or (needs_patch and not report['patch_ok']))

    if verbose:
        if not meta:
            print('本地无 castle_meta.json（还没跑过 refresh）；先跑一次 refresh 建立基线。')
        print(f'SDK 内容漂移：{report["sdk_changed"]}  '
              f'（线上 {report["online_sha256"][:12]}… / 本地 {str(report["local_sha256"])[:12]}…）')
        print(f'公钥漂移：{report["pk_changed"]}  '
              f'（线上 {report["online_pk"]} / 本地 {report["local_pk"]}）')
        print(f'补丁仍可命中：{report["patch_ok"]}  命中数={report["patch_counts"]}')
        # 注意：不要用 ✓ / ⚠️ 之类字符——Windows 控制台默认 GBK，打这些会 UnicodeEncodeError
        if report['drifted'] and not report['patch_ok']:
            print('  [!] 补丁命中数不对——直接 refresh 会失败，补丁需人工重新推导。')
        elif report['drifted']:
            print('  [-] 有漂移但补丁仍可命中：跑 `python main.py refresh-castle` 即可自动重打。')
        else:
            print('  [ok] 无漂移，生成器与线上一致。')
    return report
