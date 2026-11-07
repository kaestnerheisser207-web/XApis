"""用户时间线监听来源：按线程复用连接，并隔离访客与账号凭证。"""
from contextlib import contextmanager
import re
import threading
import time

from curl_cffi import requests

from builder import client as transport
from builder.auth import GUEST_ACTIVATE_URL, XAuth, _GUEST_TTL
from builder.header import HeaderBuilder, HeaderType
from builder.params import GraphQLOperation
from x_apis.client_errors import XClientError
from x_apis.errors import GraphQLError


def _guest_token_invalid(exc):
    """只根据明确的访客 token 失效证据恢复，普通 401、403、限流不换身份。"""
    if not isinstance(exc, GraphQLError):
        return False
    for error in exc.errors:
        if not isinstance(error, dict):
            continue
        if str(error.get('code')) == '239':
            return True
        message = str(error.get('message') or '').lower()
        if re.search(r'(?:bad|invalid|expired)\s+(?:x[-_ ]?)?guest[-_ ]+token\b', message):
            return True
        if re.search(r'\bguest[-_ ]+token\s+(?:is\s+|has\s+)?(?:invalid|expired)\b', message):
            return True
    return False


def _response_json(response, operation):
    """保留 HTTP 响应和 GraphQL 错误，供监听调度器区分限流与认证失败。"""
    try:
        raw = response.json()
    except (ValueError, TypeError):
        response.raise_for_status()
        raise XClientError('invalid_response', '时间线接口响应无效') from None
    if isinstance(raw, dict) and raw.get('errors'):
        error = GraphQLError(operation, raw['errors'], raw)
        error.response = response
        # 非 2xx 也保留 GraphQL 错误码，区分来源受限与单个目标不可访问。
        # 部分 data 伴随 errors 也不接受，防止推进到未完整读取的位置。
        raise error
    response.raise_for_status()
    if not isinstance(raw, dict):
        raise XClientError('invalid_response', '时间线接口响应无效')
    return raw


class _TimelineSource:
    """每个工作线程拥有一个连接池；关闭时先等待正在执行的请求结束。"""
    thread_safe = True
    chronological = False

    def __init__(self, auth):
        self.auth = auth
        self._auth_lock = threading.RLock()
        self._local = threading.local()
        self._sessions = []
        self._lifecycle = threading.Condition()
        self._active_requests = 0
        self._closed = False

    def _assert_open(self):
        with self._lifecycle:
            if self._closed:
                raise XClientError('source_closed', '监听数据来源已关闭')

    @contextmanager
    def _session(self):
        with self._lifecycle:
            self._assert_open()
            session = getattr(self._local, 'session', None)
            if session is None:
                # 已在来源层按线程隔离，关闭内部线程局部句柄才能由主线程正确释放。
                session = requests.Session(impersonate=transport.IMPERSONATE,
                                           use_thread_local_curl=False)
                self._local.session = session
                self._sessions.append(session)
            self._active_requests += 1
        try:
            yield session
        finally:
            with self._lifecycle:
                self._active_requests -= 1
                self._lifecycle.notify_all()

    def _request(self, method, url, **kwargs):
        kwargs.update(proxies=self.auth.proxies, timeout=30, discard_cookies=True)
        kwargs.setdefault('impersonate', transport.IMPERSONATE)
        transport._apply_fetch_headers(url, kwargs)
        with self._session() as session:
            # 不接收 Set-Cookie，也不让偶然留在连接池的 Cookie 影响下一次请求。
            session.cookies.clear()
            try:
                return getattr(session, method.lower())(url, **kwargs)
            finally:
                session.cookies.clear()

    def close(self):
        """释放所有线程的连接；重复关闭安全，不关闭调用方的 SDK。"""
        with self._lifecycle:
            self._closed = True
            while self._active_requests:
                self._lifecycle.wait()
            sessions, self._sessions = self._sessions, []
        for session in sessions:
            session.close()

    def resolve(self, username):
        raw = self._query('UserByScreenName', {
            'screen_name': username, 'withGrokTranslatedBio': True,
        }, referer=f'https://x.com/{username}')
        uid = (((raw.get('data') or {}).get('user') or {}).get('result') or {}).get('rest_id')
        if not str(uid or '').isdigit():
            raise XClientError('user_unavailable', '无法读取目标用户')
        return str(uid)

    def fetch_page(self, user_id, cursor):
        # 局部导入避免来源模块与监听状态机循环依赖。
        from x_apis.user_monitor import parse_user_page
        variables = {'userId': user_id, 'count': 40}
        if cursor:
            variables['cursor'] = cursor
        variables.update(includePromotedContent=True,
                         withQuickPromoteEligibilityTweetFields=True, withVoice=True)
        return parse_user_page(self._query(self.operation, variables), user_id)


class CookieTimelineSource(_TimelineSource):
    """使用采集账号的固定 Cookie 快照，只读数据，不登录或改写 SDK 会话。"""
    operation = 'UserOriginalsTimeline'

    def __init__(self, client):
        self.client = client
        account_id = str(client.users.me()['user_id'])
        if not account_id.isdigit():
            raise XClientError('invalid_account', '当前采集账号标识无效')
        self.scope = account_id
        auth = XAuth(bearer=client.auth.bearer,
                     proxies=dict(client.auth.proxies) if client.auth.proxies else None)
        auth.cookie = dict(client.auth.cookie)
        auth.cookie_str = client.auth.cookie_str
        auth._transaction = client.auth._transaction
        super().__init__(auth)

    def _query(self, operation, variables, *, referer='https://x.com/home'):
        self._assert_open()
        op = GraphQLOperation(operation)
        with self._auth_lock:
            headers = (HeaderBuilder.build(HeaderType.GET).with_bearer(self.auth)
                       .with_csrf(self.auth).with_auth_type(self.auth)
                       .with_client_language(self.auth).with_xctid(self.auth, 'GET', op.path())
                       .set_referer(referer).get())
            cookies = dict(self.auth.cookie)
        response = self._request('GET', op.url(), headers=headers, cookies=cookies,
                                 params=op.query_params(variables))
        return _response_json(response, operation)


class GuestTimelineSource(_TimelineSource):
    """通过访客 token 读取公开时间线；绝不发送或自动回退到登录 Cookie。"""
    scope = 'guest'
    operation = 'UserTweets'

    def __init__(self, *, proxy=None):
        super().__init__(XAuth(proxies={'http': proxy, 'https': proxy} if proxy else None)
                         .prepare_auth(''))
        self._refresh_failure = None
        self._guest_generation = 0

    def _refresh_guest_locked(self):
        """调用方持有认证锁；一次激活失败在短暂窗口内由并发请求共享。"""
        previous = self.auth._guest_token
        if self._refresh_failure is not None:
            failed_token, until, error = self._refresh_failure
            if failed_token == previous and time.monotonic() < until:
                raise error
        try:
            headers = HeaderBuilder.build(HeaderType.FORM).with_bearer(self.auth).get()
            response = self._request('POST', GUEST_ACTIVATE_URL, headers=headers)
            raw = _response_json(response, 'GuestActivate')
            token = raw.get('guest_token')
            if not isinstance(token, str) or not token:
                raise XClientError('invalid_response', '访客激活响应缺少 token')
            self.auth.guest_token = token
            self._guest_generation += 1
            self._refresh_failure = None
        except Exception as exc:
            self._refresh_failure = (previous, time.monotonic() + 60, exc)
            raise

    def _headers(self, operation):
        with self._auth_lock:
            if not self.auth._guest_token or time.time() - self.auth._guest_ts >= _GUEST_TTL:
                self._refresh_guest_locked()
            # token 获取和事务素材初始化共用锁；网络查询不占用此锁。
            headers = (HeaderBuilder.build(HeaderType.GET).with_bearer(self.auth)
                       .set_header('x-guest-token', self.auth._guest_token)
                       .with_xctid(self.auth, 'GET', operation.path())
                       .set_referer('https://x.com/').get())
            return headers, self._guest_generation

    def _query(self, operation, variables, *, referer=None):
        self._assert_open()
        op = GraphQLOperation(operation)
        for attempt in range(2):
            headers, generation = self._headers(op)
            response = self._request('GET', op.url(), headers=headers,
                                     params=op.query_params(variables))
            try:
                return _response_json(response, operation)
            except GraphQLError as exc:
                if not _guest_token_invalid(exc):
                    raise
                if attempt:
                    raise XClientError('guest_auth_failed', '访客身份刷新后仍不可用') from None
                with self._auth_lock:
                    # 其他线程已激活过时复用结果，即使服务端返回了相同 token。
                    if self._guest_generation == generation:
                        self._refresh_guest_locked()
