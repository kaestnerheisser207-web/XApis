"""Explicit service configuration. Never reads a user's SDK .env file."""
from dataclasses import dataclass
import math
import os
from pathlib import Path
import stat


@dataclass(frozen=True)
class Settings:
    api_key: str
    timeout: float = 25
    max_request_bytes: int = 65536
    max_response_bytes: int = 2 * 1024 * 1024

    def __post_init__(self):
        if (not isinstance(self.api_key, str) or not 32 <= len(self.api_key) <= 512
                or not self.api_key.isascii() or any(c.isspace() for c in self.api_key)):
            raise ValueError('XAPIS_API_KEY must be 32-512 non-whitespace ASCII characters')
        if not math.isfinite(self.timeout) or not 1 <= self.timeout <= 120:
            raise ValueError('XAPIS_REQUEST_TIMEOUT must be between 1 and 120 seconds')

    @classmethod
    def from_env(cls):
        key, filename = os.environ.get('XAPIS_API_KEY'), os.environ.get('XAPIS_API_KEY_FILE')
        if key and filename:
            raise ValueError('Configure only one of XAPIS_API_KEY and XAPIS_API_KEY_FILE')
        if filename:
            try:
                path = Path(filename)
                info = path.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 1024:
                    raise ValueError()
                key = path.read_text().strip()
            except (OSError, UnicodeError, ValueError):
                raise ValueError('XAPIS_API_KEY_FILE must be a readable private file (0600)') from None
        try:
            timeout = float(os.environ.get('XAPIS_REQUEST_TIMEOUT', '25'))
        except ValueError:
            raise ValueError('Invalid XAPIS_REQUEST_TIMEOUT') from None
        return cls(api_key=key or '', timeout=timeout)
