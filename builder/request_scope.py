"""An opt-in, request-local transport budget for the read-only HTTP provider."""
from contextlib import contextmanager
from contextvars import ContextVar
import time
from urllib.parse import urlsplit

from curl_cffi import CurlOpt, requests

from x_apis.client_errors import XClientError


_current = ContextVar('xapis_request_transport', default=None)
_HOSTS = frozenset(('x.com', 'api.x.com', 'abs.twimg.com'))


def current_transport():
    return _current.get()


class BoundedTransport:
    """One session per invocation; no ambient proxies, redirects or cookie jar."""

    def __init__(self, seconds, *, max_bytes=8 * 1024 * 1024, session_factory=None):
        self.deadline = time.monotonic() + seconds
        self.max_bytes = max_bytes
        self.session = (session_factory or requests.Session)(
            trust_env=False, curl_options={CurlOpt.NOPROXY: ''})

    def remaining(self):
        seconds = self.deadline - time.monotonic()
        if seconds <= 0:
            raise XClientError('timeout', '请求总时限已到，未继续调用上游', status_code=504)
        return seconds

    def request(self, method, url, **kwargs):
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.hostname not in _HOSTS
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise XClientError('invalid_response', '上游地址不在允许范围内', status_code=502)
        body = bytearray()

        def receive(chunk):
            self.remaining()
            if len(body) + len(chunk) > self.max_bytes:
                raise XClientError('invalid_response', '上游响应超过大小限制', status_code=502)
            body.extend(chunk)
            return len(chunk)

        kwargs.update(timeout=min(float(kwargs.get('timeout') or 30), self.remaining()),
                      allow_redirects=False, discard_cookies=True, content_callback=receive)
        # An absent explicit proxy means direct egress, even when the host has
        # HTTP_PROXY/HTTPS_PROXY set. Caller account routing is never inherited.
        if not kwargs.get('proxies') and not kwargs.get('proxy'):
            kwargs.pop('proxy', None)
            kwargs['proxies'] = {'all': ''}
        response = self.session.request(method, url, **kwargs)
        self.remaining()
        response.content = bytes(body)
        if 300 <= response.status_code < 400:
            raise XClientError('invalid_response', '上游重定向未执行', status_code=502)
        return response

    def get(self, url, **kwargs):
        from builder.client import _apply_fetch_headers, IMPERSONATE
        kwargs.setdefault('impersonate', IMPERSONATE)
        _apply_fetch_headers(url, kwargs)
        return self.request('GET', url, **kwargs)

    def close(self):
        self.session.close()


@contextmanager
def request_scope(seconds, *, session_factory=None):
    transport = BoundedTransport(seconds, session_factory=session_factory)
    token = _current.set(transport)
    try:
        yield transport
    finally:
        _current.reset(token)
        transport.close()
