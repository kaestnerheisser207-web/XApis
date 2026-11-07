"""Locate versioned resources in a checkout or an installed wheel.

The Node runners need real neighboring files. Install the wheel normally; direct
zip imports are not supported. Missing assets fail explicitly instead of falling
back to a different checkout or downloading replacements.
"""
from importlib.resources import files
from pathlib import Path


def asset_path(*parts):
    relative = Path(*parts)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('资源路径必须位于资源目录内')
    source = Path(__file__).resolve().parent.parent
    if (source / 'pyproject.toml').is_file():
        root = source / 'static'
    else:
        root = Path(files('xapis_assets'))
    path = root / relative
    if not path.exists():
        raise FileNotFoundError(f'缺少随版本发布的资源：{relative}')
    return path
