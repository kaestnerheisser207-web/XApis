"""Create an explicit monitor project with no live credentials or enabled writes."""
import json
from pathlib import Path
import shutil

from api_watch.config import DEFAULTS


def initialize(directory):
    root = Path(directory).expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError('初始化目录必须为空，避免覆盖配置或凭据')
    root.mkdir(parents=True, exist_ok=True)
    secrets = root / 'secrets'
    secrets.mkdir(mode=0o700)
    fixture_dir = root / 'fixtures'
    fixture_dir.mkdir()
    for path in (Path(__file__).parent / 'fixtures').iterdir():
        if path.is_file() and path.name in ('probe.png', 'probe.mp4'):
            shutil.copyfile(path, fixture_dir / path.name)
    config = json.loads(json.dumps(DEFAULTS))
    config['samples'].update(image_file='fixtures/probe.png', video_file='fixtures/probe.mp4')
    (root / 'config.json').write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
    (root / 'monitor.env').write_text('API_WATCH_SECRETS_FILE=secrets/account.json\nAPI_WATCH_PROXY=\nAPI_WATCH_WEBHOOK_URL=\n')
    (root / 'monitor.env').chmod(0o600)
    account = {'cookies': '', 'chat_pin': '', 'identity_file': '',
               'login': {'username': '', 'password': '', 'mode': 'pure', 'profile_file': ''}}
    target = secrets / 'account.json'
    target.write_text(json.dumps(account, ensure_ascii=False, indent=2) + '\n')
    target.chmod(0o600)
    return {'created': True, 'directory': str(root), 'next': '填写 config.json 与 secrets/account.json，然后运行 coverage'}
