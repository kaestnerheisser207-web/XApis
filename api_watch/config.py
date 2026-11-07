"""Explicit configuration; never discover the business project's .env."""
import json
import math
import os
from pathlib import Path
from urllib.parse import urlsplit


DEFAULTS = {
    'scan_interval_seconds': 3600,
    'probe_interval_seconds': 900,
    'failure_threshold': 2,
    'retention_days': 30,
    'frontend': {'enabled': True, 'authenticated': True, 'timeout': 20,
                 'max_assets': 600, 'max_seconds': 300},
    'probes': {'enabled': True, 'username': 'OpenAI', 'search_query': 'from:OpenAI',
               'post_id': '', 'guest': True, 'authenticated': True,
               'notifications': True, 'chat': True, 'pagination': True,
               'count': 5, 'request_gap_seconds': 1},
    'samples': {'test_account_id': '', 'username': 'OpenAI', 'search_query': 'from:OpenAI',
                'post_id': '', 'peer_user_id': '', 'peer_username': '',
                'conversation_id': '', 'media_id': '', 'image_file': '', 'video_file': '',
                'chat_settings_contract_file': ''},
    'writes': {'enabled': False, 'interval_seconds': 86400, 'allow_publish': False,
               'allow_follow': False, 'premium': False, 'articles': False},
    'lifecycle': {'enabled': False, 'login_enabled': False, 'legacy_login_enabled': False,
                  'chat_token': False, 'chat_identity': False, 'chat_identity_recover': False,
                  'chat_new_conversation': False, 'chat_send': False, 'chat_key_version': ''},
}
SECRET_KEYS = ('API_WATCH_COOKIES_FILE', 'API_WATCH_PROXY', 'API_WATCH_WEBHOOK_URL', 'API_WATCH_SECRETS_FILE')


class ConfigError(ValueError):
    """Only fixed, non-secret messages may be emitted by the CLI."""


def _merge(defaults, supplied):
    if not isinstance(supplied, dict) or set(supplied) - set(defaults):
        raise ConfigError('配置包含未知键或不是 JSON 对象')
    result = {}
    for key, value in defaults.items():
        current = supplied.get(key, value)
        if isinstance(value, dict):
            result[key] = _merge(value, current)
        elif isinstance(value, bool):
            if not isinstance(current, bool):
                raise ConfigError('开关配置必须是布尔值')
            result[key] = current
        elif isinstance(value, (int, float)):
            if (isinstance(current, bool) or not isinstance(current, (int, float))
                    or not math.isfinite(current) or current <= 0):
                raise ConfigError('数值配置必须是有限正数')
            result[key] = current
        else:
            if not isinstance(current, str) or len(current) > 500:
                raise ConfigError('文本配置格式无效')
            result[key] = current
    return result


def load_config(path=None):
    try:
        supplied = json.loads(Path(path).read_text()) if path else {}
    except (OSError, ValueError):
        raise ConfigError('无法读取巡检 JSON 配置') from None
    config = _merge(DEFAULTS, supplied)
    # Old configs remain readable; samples is the single source for new suites.
    for key in ('username', 'search_query', 'post_id'):
        if key not in supplied.get('samples', {}):
            config['samples'][key] = config['probes'][key]
    for key in ('scan_interval_seconds', 'probe_interval_seconds'):
        if config[key] < 60:
            raise ConfigError('巡检周期不得小于 60 秒')
    for value in (config['failure_threshold'], config['retention_days'],
                  config['frontend']['max_assets']):
        if int(value) != value:
            raise ConfigError('阈值、保留天数和资源数量必须是整数')
    if config['frontend']['max_assets'] > 2000 or config['frontend']['timeout'] > 60:
        raise ConfigError('资源数量或请求超时超过允许上限')
    if config['frontend']['max_seconds'] > 1800:
        raise ConfigError('单轮前端扫描不得超过 1800 秒')
    if config['probes']['request_gap_seconds'] < 1:
        raise ConfigError('探针请求间隔不得小于 1 秒')
    import re
    if not re.fullmatch(r'[A-Za-z0-9_]{1,15}', config['samples']['username']):
        raise ConfigError('测试用户名格式无效')
    post_id = config['samples']['post_id']
    if post_id and not re.fullmatch(r'[0-9]{1,30}', post_id):
        raise ConfigError('测试帖必须配置数字 ID')
    for key in ('test_account_id', 'peer_user_id', 'media_id'):
        value = config['samples'][key]
        if value and not re.fullmatch(r'[1-9][0-9]{0,29}', value):
            raise ConfigError('账号和资源 ID 必须是正十进制整数')
    if config['samples']['conversation_id'] and not re.fullmatch(r'[1-9][0-9]*:[1-9][0-9]*', config['samples']['conversation_id']):
        raise ConfigError('测试会话 ID 必须为两个数字 ID 以冒号分隔')
    if config['samples']['peer_username'] and not re.fullmatch(r'[A-Za-z0-9_]{1,15}', config['samples']['peer_username']):
        raise ConfigError('测试接收账号用户名无效')
    if config['writes']['interval_seconds'] < 3600:
        raise ConfigError('写探针周期不得小于一小时')
    if not 1 <= config['probes']['count'] <= 20 or int(config['probes']['count']) != config['probes']['count']:
        raise ConfigError('探针每页数量必须是 1 到 20 的整数')
    for key in ('image_file', 'video_file', 'chat_settings_contract_file'):
        if config['samples'][key]:
            target = Path(config['samples'][key]).expanduser()
            if not target.is_absolute():
                target = (Path(path).resolve().parent if path else Path.cwd()) / target
            config['samples'][key] = str(target.resolve())
    return config


def load_secrets(env_file=None):
    """Explicit env file plus environment overrides; credentials are not persisted."""
    values = {}
    base = Path.cwd()
    if env_file:
        from dotenv import dotenv_values
        path = Path(env_file).resolve()
        if not path.is_file():
            raise ConfigError('巡检凭据配置文件不存在')
        base = path.parent
        values.update(dotenv_values(path, interpolate=False))
    values.update({key: os.environ[key] for key in SECRET_KEYS if key in os.environ})
    account = {}
    if values.get('API_WATCH_SECRETS_FILE'):
        account_path = Path(values['API_WATCH_SECRETS_FILE']).expanduser()
        if not account_path.is_absolute():
            account_path = base / account_path
        try:
            import stat
            info = account_path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 262144:
                raise ValueError()
            account = json.loads(account_path.read_text())
            if not isinstance(account, dict) or set(account) - {'cookies', 'chat_pin', 'identity_file', 'login'}:
                raise ValueError()
            for key in ('cookies', 'chat_pin', 'identity_file'):
                if key in account and not isinstance(account[key], str):
                    raise ValueError()
            login = account.get('login') or {}
            if not isinstance(login, dict) or set(login) - {'username', 'password', 'profile_file', 'compression', 'mode'}:
                raise ValueError()
            if any(not isinstance(value, str) for value in login.values()):
                raise ValueError()
            if login.get('mode', 'pure') not in ('pure', 'compat-node'):
                raise ValueError()
            if login.get('profile_file'):
                profile_path = Path(login.pop('profile_file')).expanduser()
                if not profile_path.is_absolute():
                    profile_path = account_path.parent / profile_path
                if profile_path.stat().st_size > 2097152:
                    raise ValueError()
                login['profile'] = json.loads(profile_path.read_text())
                if not isinstance(login['profile'], (dict, list)):
                    raise ValueError()
            if account.get('identity_file'):
                identity = Path(account['identity_file']).expanduser()
                account['identity_file'] = str(identity if identity.is_absolute() else account_path.parent / identity)
            account['login'] = login
        except (OSError, ValueError, TypeError, UnicodeError):
            raise ConfigError('无法读取账号凭据 JSON；检查路径、0600 权限和字段格式') from None
    cookies = account.get('cookies', '')
    credentials_error = False
    cookie_file = values.get('API_WATCH_COOKIES_FILE')
    if cookie_file:
        path = Path(cookie_file).expanduser()
        if not path.is_absolute():
            path = base / path
        try:
            cookies = path.read_text(encoding='utf-8').strip()
        except (OSError, UnicodeError):
            credentials_error = True
        if not cookies or len(cookies) > 65536 or '\n' in cookies or '\r' in cookies:
            credentials_error = True
            cookies = ''
    if cookies and (len(cookies) > 65536 or '\n' in cookies or '\r' in cookies):
        cookies = ''
        credentials_error = True
    proxy = values.get('API_WATCH_PROXY') or None
    webhook = values.get('API_WATCH_WEBHOOK_URL') or None
    for url, schemes in ((proxy, ('http', 'https', 'socks5', 'socks5h')),
                         (webhook, ('https', 'http'))):
        if url:
            try:
                parsed = urlsplit(url)
                if parsed.scheme not in schemes or not parsed.hostname:
                    raise ValueError()
                if url == webhook and (parsed.username or parsed.password):
                    raise ValueError()
            except ValueError:
                raise ConfigError('代理或 Webhook URL 格式无效') from None
    return {'cookies': cookies, 'proxy': proxy, 'webhook': webhook,
            'credentials_error': credentials_error, 'chat_pin': account.get('chat_pin', ''),
            'identity_file': account.get('identity_file', ''), 'login': account.get('login', {})}
