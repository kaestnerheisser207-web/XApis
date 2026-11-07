# -*- coding: utf-8 -*-
"""XAPI：读接口。

风格对齐 ../DouYin_Spider/dy_apis/douyin_api.py —— 全部静态方法，首参恒为 `auth`，
凭据只从 auth 取，方法体里不出现 cookie / token。

queryId 与 features 来自 static/graphql.json（`GraphQLOperation`），不写死。
每个请求都带 XCTID。
"""

from builder import client
from builder.header import HeaderBuilder, HeaderType
from builder.params import GraphQLOperation
from utils.x_util import extract_cursor, parse_screen_name, parse_tweet_id
from x_apis.errors import GraphQLError
from x_apis.client_errors import XClientError

try:
    from urllib.parse import quote
except ImportError:  # pragma: no cover
    from urllib import quote

X_HOST = 'https://x.com'
NOTIFICATION_TYPES = ('All', 'Mentions', 'Verified', 'Priority', 'SuperFollowers')


def _require_subscriber_notifications(auth):
    """Match the current web tab's active-creator AND feature-switch gate."""
    from utils.frontend import parse_initial_state
    from utils.graphql_registry import _fetch_home_shell, _parse_feature_defaults
    if not auth.is_logged_in:
        raise XClientError('auth_required', '订阅者通知需要有效登录会话')
    try:
        html = _fetch_home_shell(auth, auth.proxies)
        session = parse_initial_state(html)['session']
        uid = session.get('user_id')
        enabled = _parse_feature_defaults(html).get('responsive_web_subscribers_ntab_for_creators_enabled')
        if not isinstance(uid, str) or not uid.isascii() or not uid.isdigit():
            raise XClientError('auth_required', '登录会话未通过身份检查')
        if auth.user_id and auth.user_id != uid:
            raise ValueError('identity mismatch')
        if type(enabled) is not bool:
            raise ValueError('unknown feature state')
        if enabled:
            active = session.get('isActiveCreator')
            if type(active) is not bool:
                raise ValueError('unknown creator state')
            if active:
                return
        reason = 'feature_disabled' if not enabled else 'creator_required'
    except XClientError:
        raise
    except (RuntimeError, ValueError, KeyError, TypeError, AttributeError):
        raise XClientError('invalid_response', '无法确认订阅者通知的账号开放条件') from None
    raise XClientError('capability_unavailable', '当前账号未开放订阅者通知（SuperFollowers）',
                       result={'category': 'SuperFollowers', 'reason': reason, 'available': False})


def graphql_get(auth, operation_name: str, variables: dict,
                referer: str = f'{X_HOST}/home', feature_overrides: dict = None,
                toggle_overrides: dict = None) -> dict:
    """所有 query 型 GraphQL 读接口的统一发送路径。

    注意 X 的 GraphQL 失败**不一定用 HTTP 状态码表达**：会话过期（code 32）、
    限流、无权限都可能是 200 + `{"errors": [...]}`。
    但读接口存在「部分成功」——比如时间线里某条推文不可见时会带 errors 却仍有 data，
    所以只在**完全没有 data** 时才抛。
    """
    operation = GraphQLOperation(operation_name)
    headers = (HeaderBuilder.build(HeaderType.POST)
               .with_bearer(auth)
               .with_csrf(auth)
               .with_auth_type(auth)
               .with_client_language(auth)
               .with_xctid(auth, 'GET', operation.path())
               .set_referer(referer)
               .get())
    resp = client.get(operation.url(), headers=headers, cookies=auth.cookie,
                      params=operation.query_params(variables, feature_overrides,
                                                    toggle_overrides),
                      proxies=auth.proxies, timeout=30)
    resp.raise_for_status()
    res_json = resp.json()
    if res_json.get('errors') and not res_json.get('data'):
        raise GraphQLError(operation_name, res_json['errors'], res_json)
    return res_json


def graphql_post_query(auth, operation_name: str, variables: dict,
                       referer: str = f'{X_HOST}/home', feature_overrides: dict = None,
                       toggle_overrides: dict = None) -> dict:
    """POST 发送的 **query** 型操作。

    不是所有读接口都走 GET —— `HomeTimeline` 这种变量里可能塞很长
    `seenTweetIds` 数组的，浏览器用 POST + JSON body 发（URL 会超长）。
    body 结构与 mutation 相同：variables + features + queryId。
    """
    operation = GraphQLOperation(operation_name)
    headers = (HeaderBuilder.build(HeaderType.POST)
               .with_bearer(auth)
               .with_csrf(auth)
               .with_auth_type(auth)
               .with_client_language(auth)
               .with_xctid(auth, 'POST', operation.path())
               .set_referer(referer)
               .get())
    resp = client.post(operation.url(), headers=headers, cookies=auth.cookie,
                       json=operation.json_body(variables, feature_overrides,
                                                toggle_overrides),
                       proxies=auth.proxies, timeout=30)
    resp.raise_for_status()
    res_json = resp.json()
    if res_json.get('errors') and not res_json.get('data'):
        raise GraphQLError(operation_name, res_json['errors'], res_json)
    return res_json


class XAPI:
    """X 读接口集合。

    每个方法的 variables / fieldToggles 都**逐字段对齐浏览器实抓**。
    少一个字段服务端可能静默降级或直接 400，所以这里不做"看起来没用就省掉"的裁剪。
    """

    # ---- 通知 ------------------------------------------------------------ #

    @staticmethod
    def get_notifications(auth, type: str = 'All', count: int = 20,
                          cursor: str = None) -> dict:
        """读取网页端通知列表，不调用通知已读更新接口。

        分类来自 2026-10-01 bundle.Notifications：All/Mentions/Verified/
        Priority/SuperFollowers。分类是否对某个账号可用，以实际响应为准。
        返回原始 GraphQL 响应；分页 cursor 取通知页底部游标。
        """
        if type not in NOTIFICATION_TYPES:
            raise ValueError(f'通知分类必须为 {NOTIFICATION_TYPES}')
        if (not isinstance(count, int) or isinstance(count, bool)
                or not 1 <= count <= 100):
            raise ValueError('count 必须为 1 到 100 的整数')
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            raise ValueError('cursor 必须为非空字符串或 None')
        if type == 'SuperFollowers':
            _require_subscriber_notifications(auth)
        variables = {'timeline_type': type, 'count': count}
        if cursor is not None:
            variables['cursor'] = cursor
        return graphql_get(auth, 'NotificationsTimeline', variables,
                           referer=f'{X_HOST}/notifications')

    # ---- 作品 ------------------------------------------------------------ #

    @staticmethod
    def get_work_info(auth, work_id: str, cursor: str = None,
                      referrer: str = None, **kwargs) -> dict:
        """作品详情（含评论区首屏）。

        :param auth: XAuth 对象。
        :param work_id: 推文 id 或完整推文链接。
        :param cursor: 评论翻页游标。
        :param referrer: 来源页面标记（如 'home' / 'tweet'）；
                         直接按 id 取详情时不传（与真实浏览器一致）。
        """
        tweet_id = parse_tweet_id(work_id)
        # variables 逐字段对齐浏览器实抓（2026-08-16 Chrome DevTools）。
        # 注意**没有 `referrer`**：直接打开推文详情页（等价于我们按 id 取详情）时
        # 前端不发这个字段，只有从时间线点进去才带 `referrer: "home"` 之类的来源标记。
        # 早期版本硬写 `referrer: "tweet"`，属于凭空多出来的字段。
        variables = {
            'focalTweetId': tweet_id,
            'with_rux_injections': False,
            'rankingMode': 'Relevance',
            'includePromotedContent': True,
            'withCommunity': True,
            'withQuickPromoteEligibilityTweetFields': True,
            'withBirdwatchNotes': True,
            'withVoice': True,
        }
        if referrer:
            # 模拟「从某个页面点进详情」的场景，位置与前端一致（focalTweetId 之后）
            variables = {'focalTweetId': tweet_id, 'referrer': referrer,
                         **{k: v for k, v in variables.items() if k != 'focalTweetId'}}
        if cursor:
            variables['cursor'] = cursor
        return graphql_get(auth, 'TweetDetail', variables,
                           referer=f'{X_HOST}/i/status/{tweet_id}')

    @staticmethod
    def get_work_comments(auth, work_id: str, cursor: str = None, **kwargs) -> dict:
        """作品评论。与详情同一个 GraphQL 操作，靠游标翻页。"""
        return XAPI.get_work_info(auth, work_id, cursor=cursor, **kwargs)

    @staticmethod
    def get_work_result(auth, work_id: str, **kwargs) -> dict:
        """详情页伴随的 TweetResultByRestId 补水请求。

        Chrome 打开公开详情时会在 TweetDetail 之外再发一次这个 query；它的
        variables 与 fieldToggles 形状不同，不能把 TweetDetail 的请求复用过来。
        """
        tweet_id = parse_tweet_id(work_id)
        variables = {
            'tweetId': tweet_id,
            'includePromotedContent': True,
            'withBirdwatchNotes': True,
            'withVoice': True,
            'withCommunity': True,
        }
        return graphql_get(auth, 'TweetResultByRestId', variables,
                           referer=f'{X_HOST}/i/status/{tweet_id}')

    @staticmethod
    def get_all_work_comments(auth, work_id: str, max_pages: int = 20, **kwargs) -> list:
        """翻完评论区，返回每一页的原始响应。"""
        pages, cursor = [], None
        for _ in range(max_pages):
            res_json = XAPI.get_work_comments(auth, work_id, cursor=cursor)
            pages.append(res_json)
            cursor = extract_cursor(res_json, 'Bottom')
            if not cursor:
                break
        return pages

    # ---- 搜索 ------------------------------------------------------------ #

    @staticmethod
    def search_work(auth, query: str, cursor: str = None, product: str = 'Top',
                    count: int = 20, **kwargs) -> dict:
        """搜索作品。

        :param product: Top / Latest / People / Media / Lists。
        """
        if product == 'Lists':
            # bundle.UserLists -> fetchListSearch: querySource is omitted.
            # Its response lives under list_search_timeline, not search_timeline.
            if not isinstance(query, str) or not query.strip():
                raise ValueError('列表搜索关键词不能为空')
            if type(count) is not int or not 1 <= count <= 100:
                raise ValueError('count 必须为 1 到 100 的整数')
            if cursor is not None and (not isinstance(cursor, str) or not cursor):
                raise ValueError('cursor 必须为非空字符串或 None')
            variables = {'rawQuery': query, 'count': count}
            if cursor is not None:
                variables['cursor'] = cursor
            return graphql_get(auth, 'ListSearchTimeline', variables,
                               referer=f'{X_HOST}/i/lists/search?q={quote(query)}')
        variables = {
            'rawQuery': query,
            'count': count,
            'querySource': 'typed_query',
            'product': product,
            # Chrome 153 实抓（2026-09-25，已登录搜索页）为 true；这会让
            # 响应包含 grok_translated_bio_with_availability，不能按旧的
            # 未登录/旧前端样本写成 false。
            'withGrokTranslatedBio': True,
            'withQuickPromoteEligibilityTweetFields': False,
        }
        if cursor:
            # 浏览器把 cursor 放在 count 之后、querySource 之前
            variables = {
                'rawQuery': query, 'count': count, 'cursor': cursor,
                'querySource': 'typed_query', 'product': product,
                'withGrokTranslatedBio': True,
                'withQuickPromoteEligibilityTweetFields': False,
            }
        # The current x-web client sends SearchTimeline through its POST
        # GraphQL transport.  The persisted operation remains a query, but
        # the GET route now returns 404 while the browser-compatible POST
        # route returns the timeline normally.
        return graphql_post_query(auth, 'SearchTimeline', variables,
                                  referer=f'{X_HOST}/search?q={quote(query)}&src=typed_query')

    # ---- 用户 ------------------------------------------------------------ #

    @staticmethod
    def get_user_info(auth, user_name: str, **kwargs) -> dict:
        """按 screen_name 取用户信息。接受主页链接、@handle 或裸用户名。"""
        screen_name = parse_screen_name(user_name)
        return graphql_get(auth, 'UserByScreenName',
                           {'screen_name': screen_name, 'withGrokTranslatedBio': True},
                           referer=f'{X_HOST}/{screen_name}')

    @staticmethod
    def get_user_id(auth, user_name: str) -> str:
        res_json = XAPI.get_user_info(auth, user_name)
        return res_json['data']['user']['result']['rest_id']

    @staticmethod
    def get_user_post_note(auth, user_id: str, cursor: str = None,
                           count: int = 20, operation: str = 'UserOriginalsTimeline',
                           **kwargs) -> dict:
        """用户发布的作品列表。user_id 是数字 rest_id，不是 screen_name。

        **默认走 `UserOriginalsTimeline`**：2026-08-16 实抓确认，浏览器打开个人主页
        「Posts」标签发的是这个操作，不再是老的 `UserTweets`（两者 variables 与
        fieldToggles 形状完全一致，只有 queryId 不同）。
        需要老行为时传 `operation='UserTweets'`。
        """
        variables = {
            'userId': user_id,
            'count': count,
            'includePromotedContent': True,
            'withQuickPromoteEligibilityTweetFields': True,
            'withVoice': True,
        }
        if cursor:
            # 实抓：cursor 插在 count 之后
            variables = {'userId': user_id, 'count': count, 'cursor': cursor,
                         'includePromotedContent': True,
                         'withQuickPromoteEligibilityTweetFields': True,
                         'withVoice': True}
        return graphql_get(auth, operation, variables)

    @staticmethod
    def get_user_all_post_note(auth, user_id: str, max_pages: int = 20, **kwargs) -> list:
        pages, cursor = [], None
        for _ in range(max_pages):
            res_json = XAPI.get_user_post_note(auth, user_id, cursor=cursor)
            pages.append(res_json)
            next_cursor = extract_cursor(res_json, 'Bottom')
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return pages

    # ---- 自己 ------------------------------------------------------------ #

    @staticmethod
    def get_home_timeline(auth, cursor: str = None, count: int = 20,
                          seen_tweet_ids=None, **kwargs) -> dict:
        """自己的主页时间线。

        **两种形态，取决于有没有已读记录**（2026-08-16 实抓确认，两种都真实存在）：

        - 冷启动（没有 seenTweetIds）→ **GET**，variables/features 走 query string，
          且 variables 里**根本没有 `seenTweetIds` 这个键**；
        - 已有已读记录 → **POST + JSON body**，variables 末尾带 `seenTweetIds` 数组
          （数组会很长，URL 放不下，所以前端改用 POST）。

        早期版本无论如何都发 POST + `seenTweetIds: []`，两种形态都不匹配：
        冷启动时凭空多一个空数组字段，正是会被风控盯上的那类差异。
        """
        seen = list(seen_tweet_ids or [])
        variables = {'count': count}
        if cursor:
            variables['cursor'] = cursor
        variables['includePromotedContent'] = True
        if not cursor:
            variables['requestContext'] = 'launch'
        variables['withCommunity'] = True

        if not seen:
            # 冷启动：GET，且不带 seenTweetIds 键
            return graphql_get(auth, 'HomeTimeline', variables)
        variables['seenTweetIds'] = seen
        return graphql_post_query(auth, 'HomeTimeline', variables)

    @staticmethod
    def get_viewer(auth, **kwargs) -> dict:
        """当前会话身份，用来判断 cookie 是否还活着。"""
        return graphql_get(auth, 'Viewer', {'withCommunitiesMemberships': True})

    @staticmethod
    def get_my_user_id(auth) -> str:
        """优先用 cookie 里的 twid，省一次请求。"""
        if auth.user_id:
            return auth.user_id
        return XAPI.get_viewer(auth)['data']['viewer']['user_results']['result']['rest_id']
