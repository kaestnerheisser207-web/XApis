"""Stable, secret-free errors for the business SDK."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import wraps
import json
import math

from curl_cffi.requests import exceptions as curl_errors
from requests import exceptions as request_errors


class XClientError(RuntimeError):
    def __init__(self, code, message, *, result=None, status_code=None,
                 retryable=False, retry_after=None, upstream_codes=()):
        super().__init__(message)
        self.code = code
        self.result = result
        self.status_code = status_code
        # A transient failure does not establish that a write was not applied.
        self.retryable = retryable
        self.retry_after = retry_after
        self.upstream_codes = tuple(upstream_codes)


def _numeric_codes(errors):
    codes = set()
    for error in errors:
        value = error.get('code')
        if type(value) is int and 0 <= value <= 999999:
            codes.add(value)
        elif (isinstance(value, str) and value.isascii() and value.isdigit()
              and len(value) <= 6):
            codes.add(int(value))
    return tuple(sorted(codes))


def graphql_error(errors):
    """Normalize a GraphQL errors array without retaining messages or payloads."""
    if (not isinstance(errors, list) or not errors
            or any(not isinstance(error, dict) for error in errors)):
        return XClientError('invalid_response', '服务端错误响应格式无效')
    codes = _numeric_codes(errors)
    if set(codes) & {32, 89, 135}:
        code, message = 'auth_required', '登录会话无效，请更新账号会话'
    elif 226 in codes:
        code, message = 'risk_controlled', '账号操作受到限制，请检查账号状态'
    elif 88 in codes:
        code, message = 'rate_limited', '请求受到限流，请稍后核对操作状态'
    else:
        code, message = 'upstream_error', 'X 返回接口错误'
    return XClientError(code, message, upstream_codes=codes)


def _retry_after(response):
    """Return bounded seconds only; never expose an arbitrary header value."""
    headers = getattr(response, 'headers', None)
    if not hasattr(headers, 'get'):
        return None
    value = headers.get('Retry-After')
    if value is None:
        value = headers.get('retry-after')
    if not isinstance(value, str) or len(value) > 128:
        return None
    value = value.strip()
    try:
        if value.isascii() and value.isdigit():
            seconds = int(value)
        else:
            moment = parsedate_to_datetime(value)
            if moment.tzinfo is None:
                return None
            seconds = max(0, math.ceil(
                (moment - datetime.now(timezone.utc)).total_seconds()))
        return seconds if 0 <= seconds <= 2147483647 else None
    except (TypeError, ValueError, OverflowError):
        return None


def normalize_error(exc):
    """Map failures to stable categories; classification never permits replay.

    Response validators should raise invalid_response explicitly. Missing keys,
    JSON decoding and attribute access failures are also response errors; ordinary
    ValueError/TypeError retain the public API's argument-validation convention.
    """
    from utils.xchat_identity import IdentityError
    from x_apis.errors import GraphQLError

    if isinstance(exc, XClientError):
        return exc
    if isinstance(exc, GraphQLError):
        return graphql_error(exc.errors)
    if isinstance(exc, IdentityError):
        return XClientError('identity_error', '聊天身份操作未完成，请检查身份状态和 PIN 配置')

    response = getattr(exc, 'response', None)
    status = getattr(response, 'status_code', None)
    status = status if type(status) is int and 100 <= status <= 599 else None
    if status is not None and status >= 400:
        if status == 401:
            code, message = 'auth_required', '登录会话无效，请更新账号会话'
        elif status == 403:
            code, message = 'forbidden', '账号无权执行此操作'
        elif status == 429:
            code, message = 'rate_limited', '请求受到限流，请稍后核对操作状态'
        elif status >= 500:
            code, message = 'upstream_unavailable', 'X 服务暂时不可用，请核对操作状态'
        else:
            code, message = 'upstream_error', 'X 返回接口错误'
        return XClientError(code, message, status_code=status,
                            retry_after=_retry_after(response))
    if isinstance(exc, (TimeoutError, request_errors.Timeout, curl_errors.Timeout)):
        return XClientError('timeout', '接口请求超时，请核对操作状态后再决定后续处理',
                            status_code=status)
    if isinstance(exc, (ConnectionError, request_errors.ConnectionError,
                        request_errors.ChunkedEncodingError, curl_errors.ConnectionError,
                        curl_errors.ProxyError)):
        return XClientError('network_error', '网络请求未完成，请核对操作状态', status_code=status)
    if isinstance(exc, (json.JSONDecodeError, request_errors.JSONDecodeError,
                        KeyError, AttributeError)):
        return XClientError('invalid_response', '服务端响应格式无效', status_code=status)
    if isinstance(exc, (ValueError, TypeError)):
        return XClientError('invalid_argument', '接口参数无效')
    return XClientError('operation_failed', '接口操作未完成；请核对会话状态和网络')


def api_method(function):
    """Keep raw transport errors (which may contain authenticated URLs) private."""
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except XClientError:
            raise
        except Exception as exc:
            raise normalize_error(exc) from None
    return call
