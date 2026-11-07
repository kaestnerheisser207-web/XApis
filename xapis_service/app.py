"""Flask application factory; Gunicorn supplies the production HTTP runtime."""
import hmac
import json
import logging
import threading
import time
import uuid

from flask import Flask, g, request
from werkzeug.exceptions import HTTPException

from builder.request_scope import request_scope
from utils.resource_paths import asset_path
from utils.transaction import ClientTransaction
from x_apis import XClient
from x_apis.client_errors import XClientError, normalize_error
from xapis_service import __version__
from xapis_service.config import Settings
from xapis_service.contract import CAPABILITIES, execute, validate_request


_STATUS = {
    'invalid_argument': 400, 'unsupported_capability': 400,
    'service_unauthorized': 401, 'auth_required': 401, 'forbidden': 403,
    'risk_controlled': 403, 'rate_limited': 429, 'timeout': 504,
    'user_unavailable': 404, 'post_unavailable': 404,
    'service_busy': 503, 'upstream_unavailable': 503,
    'network_error': 502, 'invalid_response': 502,
}


def sdk_client(account, transport):
    client = XClient.from_cookies(account['cookies'], proxy=account.get('proxy'))
    # These public signature materials are part of the tested release artifact.
    # Do not follow an A/B marketing/login page, consult a host override, or
    # silently switch provenance when an upstream request fails.
    client.auth._transaction = ClientTransaction.from_cached(str(asset_path('transaction_l1.json')))
    return client


def create_app(settings=None, *, client_factory=None):
    settings = settings or Settings.from_env()
    from utils.graphql_registry import get_operation
    for operation in ('UserByScreenName', 'UserOriginalsTimeline', 'TweetDetail', 'SearchTimeline'):
        get_operation(operation)
    ClientTransaction.from_cached(str(asset_path('transaction_l1.json')))
    app = Flask(__name__, static_folder=None)
    app.config.update(MAX_CONTENT_LENGTH=settings.max_request_bytes, PROPAGATE_EXCEPTIONS=False)
    factory = client_factory or sdk_client
    slot = threading.BoundedSemaphore(1)
    logger = logging.getLogger('xapis_service.requests')

    def respond(body, status=200):
        raw = json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
        if len(raw) > settings.max_response_bytes:
            raise XClientError('invalid_response', '业务响应超过大小限制')
        return app.response_class(raw, status=status, mimetype='application/json')

    @app.before_request
    def authenticate():
        g.request_id = str(uuid.uuid4())
        g.started = time.monotonic()
        g.capability = 'unknown'
        if request.path in ('/healthz', '/readyz'):
            return None
        expected = ('Bearer ' + settings.api_key).encode('ascii')
        actual = request.headers.get('Authorization', '').encode('utf-8')
        if not hmac.compare_digest(expected, actual):
            raise XClientError('service_unauthorized', '服务鉴权失败')
        if request.query_string:
            raise XClientError('invalid_argument', '该服务不接受 URL 查询参数')

    @app.after_request
    def finish(response):
        response.headers['X-Request-ID'] = g.request_id
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        logger.info('request_id=%s capability=%s status=%s elapsed_ms=%d',
                    g.request_id, g.capability, response.status_code,
                    int((time.monotonic() - g.started) * 1000))
        return response

    @app.errorhandler(Exception)
    def failure(exc):
        if isinstance(exc, HTTPException):
            status = exc.code or 500
            error = {'code': 'invalid_argument' if status < 500 else 'operation_failed',
                     'message': '请求路径、方法、格式或大小无效', 'retryable': False}
        else:
            safe = normalize_error(exc)
            status = _STATUS.get(safe.code, 502)
            error = {'code': safe.code, 'message': str(safe), 'retryable': False}
            if safe.retry_after is not None:
                error['retry_after'] = safe.retry_after
            if safe.upstream_codes:
                error['upstream_codes'] = list(safe.upstream_codes)
        response = respond({'error': error}, status)
        if status == 429 and 'retry_after' in error:
            response.headers['Retry-After'] = str(error['retry_after'])
        return response

    @app.get('/healthz')
    def health():
        return respond({'status': 'ok', 'version': __version__})

    @app.get('/readyz')
    def ready():
        return respond({'status': 'ready', 'version': __version__, 'upstream_checked': False})

    @app.get('/v1/capabilities')
    def capabilities():
        return respond({'version': __version__, 'capabilities': list(CAPABILITIES)})

    @app.get('/openapi.json')
    def openapi():
        from xapis_service.schema import document
        return respond(document())

    @app.post('/v1/execute')
    def invoke():
        if request.mimetype != 'application/json':
            raise XClientError('invalid_argument', '请求必须使用 application/json')
        def reject_constant(value):
            raise ValueError('non-JSON number')

        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate key')
                result[key] = value
            return result

        try:
            body = json.loads(request.get_data(), parse_constant=reject_constant,
                              object_pairs_hook=unique_object)
        except (ValueError, UnicodeError):
            raise XClientError('invalid_argument', '请求不是有效 JSON') from None
        capability, payload, account = validate_request(body)
        g.capability = capability
        if not slot.acquire(blocking=False):
            raise XClientError('service_busy', '当前服务工作进程正在处理请求')
        try:
            with request_scope(settings.timeout) as transport:
                client = factory(account, transport)
                transport.remaining()
                result = execute(client, capability, payload)
                transport.remaining()
                return respond({'data': result})
        finally:
            slot.release()

    return app
