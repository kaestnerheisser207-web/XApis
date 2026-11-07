"""Production entry point: fixed synchronous workers with a hard timeout."""
import os
from gunicorn.app.base import BaseApplication

from xapis_service.app import create_app
from xapis_service.config import Settings


def main():
    settings = Settings.from_env()
    bind = os.environ.get('XAPIS_BIND', '').strip()
    if not bind or any(c.isspace() for c in bind):
        raise ValueError('XAPIS_BIND must explicitly configure the listening address')
    try:
        workers = int(os.environ.get('XAPIS_WORKERS', '2'))
    except ValueError:
        raise ValueError('XAPIS_WORKERS must be an integer') from None
    if not 1 <= workers <= 32:
        raise ValueError('XAPIS_WORKERS must be between 1 and 32')

    class Application(BaseApplication):
        def load_config(self):
            for key, value in {'bind': bind, 'workers': workers, 'worker_class': 'sync',
                               'timeout': int(settings.timeout) + 5, 'graceful_timeout': int(settings.timeout) + 5,
                               'max_requests': 1000, 'max_requests_jitter': 50,
                               'accesslog': None, 'errorlog': '-', 'loglevel': 'warning',
                               'limit_request_line': 4094, 'limit_request_fields': 32,
                               'limit_request_field_size': 4096}.items():
                self.cfg.set(key, value)

        def load(self):
            return create_app(settings)

    Application().run()


if __name__ == '__main__':
    main()
