"""Check release contents, or smoke-test an installation without credentials."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile


VERSION = '0.1.0rc1'
PACKAGES = {'x_apis', 'builder', 'utils', 'api_watch', 'xapis_service'}
ASSETS = {
    '__init__.py', 'graphql.json', 'transaction_l1.json', 'ui_metrics.js',
    'castle/castle.js', 'castle/castle.umd-BneRArir.js', 'castle/castle_meta.json',
    'castle/chromium_zlib.dll', 'castle/chromium_zlib.meta.json', 'castle/env_core.js',
    'castle/rolldown-runtime-XXLRXQBO.js', 'castle/run.js',
}
FIXTURES = {'api_watch/fixtures/probe.png', 'api_watch/fixtures/probe.mp4'}
SOURCE_FILES = {
    'pyproject.toml', 'MANIFEST.in', 'PKG-INFO', 'setup.cfg',
    'requirements.txt', 'requirements-watch.txt', 'requirements-service.txt',
    'requirements-dev.txt', 'scripts/check_dist.py', '.github/workflows/ci.yml',
}
EGG_METADATA = {'PKG-INFO', 'SOURCES.txt', 'dependency_links.txt', 'entry_points.txt',
                'requires.txt', 'top_level.txt'}
WHEEL_METADATA = {'METADATA', 'WHEEL', 'RECORD', 'entry_points.txt', 'top_level.txt'}


def _allowed(name, *, wheel):
    parts = PurePosixPath(name).parts
    if not parts or name.startswith('/') or '..' in parts:
        return False
    if len(parts) == 2 and parts[0] in PACKAGES and parts[1].endswith('.py'):
        return True
    if name in FIXTURES:
        return True
    asset_prefix = 'xapis_assets/' if wheel else 'static/'
    if name.startswith(asset_prefix):
        return name[len(asset_prefix):] in ASSETS
    if wheel:
        return (len(parts) == 2 and parts[0] == f'xapis_internal-{VERSION}.dist-info'
                and parts[1] in WHEEL_METADATA)
    return (name in SOURCE_FILES or
            (len(parts) == 2 and parts[0] == 'xapis_internal.egg-info'
             and parts[1] in EGG_METADATA))


def check_archive(path):
    path = Path(path)
    wheel = path.suffix == '.whl'
    if wheel:
        with zipfile.ZipFile(path) as archive:
            members = [member for member in archive.infolist() if not member.is_dir()]
            if any(stat.S_ISLNK(member.external_attr >> 16) for member in members):
                raise ValueError('分发包不能包含符号链接')
            names = [member.filename for member in members]
    elif path.name.endswith('.tar.gz'):
        prefix = f'xapis_internal-{VERSION}/'
        with tarfile.open(path, 'r:gz') as archive:
            members = [member for member in archive.getmembers() if not member.isdir()]
            if any(not member.isfile() or not member.name.startswith(prefix) for member in members):
                raise ValueError('源码分发包包含非普通文件或错误根目录')
            names = [member.name[len(prefix):] for member in members]
    else:
        raise ValueError('只支持 wheel 和 tar.gz 源码分发包')
    unexpected = sorted(name for name in names if not _allowed(name, wheel=wheel))
    if unexpected:
        raise ValueError('分发包包含白名单外文件：' + ', '.join(unexpected))
    prefix = 'xapis_assets/' if wheel else 'static/'
    required = {prefix + asset for asset in ASSETS} | FIXTURES
    required |= {package + '/__init__.py' for package in PACKAGES}
    missing = sorted(required - set(names))
    if missing:
        raise ValueError('分发包缺少必需文件：' + ', '.join(missing))
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    print(f'{path.name}: {len(names)} files; sha256={checksum}')


def installed_smoke(expected_root=None):
    # Every operation below is local. Socket access is denied even if an import
    # accidentally introduces a network request; no account is loaded.
    def deny_network(event, args):
        if event in ('socket.connect', 'socket.getaddrinfo', 'socket.bind'):
            raise RuntimeError('安装烟测禁止网络访问')
    sys.addaudithook(deny_network)

    from importlib.metadata import version
    import api_watch
    import builder
    import utils
    import x_apis
    import xapis_service
    from api_watch.catalog import inventory
    from api_watch.config import load_config
    from api_watch.manifest import plan_fingerprint
    from api_watch.scaffold import initialize
    from utils import castle_refresh, castle_token, chromium_deflate, graphql_registry, ui_metrics
    from utils.resource_paths import asset_path
    from utils.transaction import ClientTransaction
    from xapis_service.app import create_app

    assert version('xapis-internal') == VERSION
    assert callable(create_app) and callable(x_apis.XClient.from_cookies)
    root = Path(expected_root).resolve() if expected_root else Path(x_apis.__file__).resolve().parent.parent
    for module in (api_watch, builder, utils, x_apis, xapis_service):
        assert Path(module.__file__).resolve().is_relative_to(root), module.__name__
    assert asset_path().name == 'xapis_assets', 'smoke must use an installation, not the checkout'
    assert asset_path().resolve().is_relative_to(root)
    for asset in ASSETS:
        assert asset_path(asset).is_file(), asset
    assert graphql_registry.get_query_id('TweetDetail')
    assert Path(graphql_registry.REGISTRY_PATH) == asset_path('graphql.json')
    assert ClientTransaction.from_cached().generate('GET', '/i/api/graphql/synthetic')
    assert castle_token.read_public_key()
    assert castle_refresh.load_meta()['public_key']
    assert Path(castle_token.RUNNER).parent == asset_path('castle')
    assert Path(castle_refresh.CASTLE_DIR) == asset_path('castle')
    assert Path(chromium_deflate._default_library()) == asset_path('castle/chromium_zlib.dll')
    for script in ('castle/run.js', 'castle/env_core.js', 'ui_metrics.js'):
        subprocess.run([ui_metrics.NODE, '--check', str(asset_path(script))], check=True,
                       capture_output=True, timeout=30)
    solved = ui_metrics.solve('document.getElementsByName("ui_metrics")[0].value = \'{"rf":{},"s":"package-smoke"}\';')
    assert json.loads(solved) == {'rf': {}, 's': 'package-smoke'}
    assert inventory()
    with tempfile.TemporaryDirectory(prefix='xapis-scaffold-smoke-') as directory:
        initialize(directory)
        config = load_config(Path(directory) / 'config.json')
        assert config['writes']['enabled'] is False
        assert config['lifecycle']['enabled'] is False
        assert len(plan_fingerprint(config, {})) == 64
        for name in ('probe.png', 'probe.mp4'):
            assert (Path(directory) / 'fixtures' / name).stat().st_size > 0
    print('installed smoke: SDK, service, all resources, watch scaffold, ui_metrics OK')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archives', nargs='*')
    parser.add_argument('--installed', action='store_true')
    parser.add_argument('--installed-root')
    args = parser.parse_args()
    if args.installed:
        installed_smoke(args.installed_root)
    elif args.archives:
        for path in args.archives:
            check_archive(path)
    else:
        parser.error('请提供分发包路径或 --installed')


if __name__ == '__main__':
    main()
