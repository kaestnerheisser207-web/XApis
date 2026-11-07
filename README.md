<div align="center">
  <a href="https://github.com/cv-cat/XApis">
    <img width="180" src="./assets/xapis-logo.png" alt="XApis logo">
  </a>

  <h1>🐦 XApis</h1>
  <p>纯 Python 的 X (Twitter) 登录、搜索、读写与媒体接口库</p>

  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10+"></a>
  <a href="https://github.com/lexiforest/curl_cffi"><img src="https://img.shields.io/badge/curl__cffi-browser--like-0F172A" alt="curl_cffi"></a>
</div>

> 仅供学习、研究和经过授权的自动化使用。请遵守 X 服务条款与当地法律，勿用于隐私抓取、批量骚扰或违规发布。

本项目封装的是 X 网页内部接口。**已有 Cookie 的会话复用**与**从零开始的账密登录**是两条不同链路，不能用前者的成功证明后者可用。代码提供的能力与本轮实测范围见[验证记录](#verification-2026-10-01)。

## ✨ 能力

- 提供纯 Python 账密登录路径，Castle / XCTID 本地计算；需要完整 Castle profile，平台条件见下文。
- 登录成功后持久化 Cookie；`main.py login` 可校验并复用有效 Cookie，`--force` 强制走账密流程。
- 搜索、作品详情、评论、用户信息、时间线、发帖、回复、删除、点赞、转推、关注。
- 通知读取和分页：全部、提及、认证账号、优先、订阅者五种前端分类；可用性以当前账号实测为准。
- Premium 长推（CreateNoteTweet，按权重自动切换）、Thread、文章（Markdown 一键建草稿 / 发布）。
- 图片、多图片和视频上传；X Chat 启动、读取及原生加密文本发送已实现，发送需要已解锁的会话密钥和账号签名密钥，验证范围见下文。
- 直接调用 Python SDK，也可以使用 `main.py` CLI。

## 工程调用：统一 Python SDK

业务项目使用 `XClient`。账号、代理、PIN 和本地状态在客户端初始化时配置，
调用业务方法时无需拼 GraphQL、Thrift、签名、会话 ID、密钥文件或临时脚本。
这是可导入的 Python API；另有独立的只读 HTTP Provider，供 social 对接，见
[social 接入与候选发布](#social-接入与候选发布)。SDK 调用本身不启动服务。

```python
from x_apis import XClient, XClientError

client = XClient.from_env(".env")

# 新会话与已有会话续发使用同一入口；调用 send 即实际发送。
result = client.messages.send(
    recipient="TZh3gl",
    text="你好",
    request_id="your-business-message-id",  # 每条业务消息唯一；同一消息重试保持不变
)
# {request_id, message_id, sender_id, recipient_id, conversation_id,
#  encrypted: bool, status: "sent" | "verified" | "unconfirmed" | "sending"}

# 只核对原消息，不重新提交。
result = client.messages.reconcile("your-business-message-id")

# 只读预检/读取消息（含必要分页；加密消息会验签和解密）。
capability = client.messages.preview("TZh3gl")
page = client.messages.list("TZh3gl", limit=20)
notices = client.notifications.list(category="All", count=20)
```

`request_id` 可省略，由 SDK 生成并随结果返回；工程调用建议由业务方提供稳定 ID。
相同 ID、收件人和正文再次调用时不会再发 POST；变更正文、收件人或模式会抛
`XClientError(code="request_conflict")`。状态、回执和密钥默认放在
`.xapis/<当前账号ID>/`，目录权限 0700、文件权限 0600，已加入 Git 忽略。
`.env` 中相对状态路径按 `.env` 所在目录解析，多账号不共用密钥。

发送模式默认 `auto`：已有加密会话继续加密；对方允许加密建联时内部完成密钥分发；
只允许消息请求时沿用网页的未加密文字路径，返回 `encrypted=False`。
可指定 `mode="encrypted"` 要求必须加密；无法加密时失败，不降级。
`mode="plaintext"` 不能用于已有加密会话。发送超时不自动重试，也不自动切换模式。

`verified` 表示发送账号已从服务端回读到相同消息 ID、发送者和正文；
**不表示对方已读或已接受请求**。陌生人消息可能位于收件方“消息请求 → 其他”。
`delivery_unknown` / `readback_failed` 异常的 `result` 带原请求号和消息 ID，
应调用 `reconcile()`，不要另生成请求号重发。

### 默认 PIN 与账号初始化

```dotenv
X_CHAT_DEFAULT_PIN=6382
X_CHAT_STATE_DIR=.xapis
X_CHAT_IDENTITY_FILE=
X_CHAT_PIN=
```

默认 PIN 按用户确认设为 `6382`。官方 `chatxdk==0.5.0` 拒绝连续数字 PIN，
包括 `4321`；本地校验会在注册公钥之前拒绝这些值，避免创建不能完成备份的身份。
默认值只用于**当前登录账号**第一次注册聊天身份，不用于收件人账号。

- 发送时，只有公钥查询成功且服务端公钥列表为空，才自动创建身份并使用默认 PIN 备份。
- 已有本地身份时校验账号、公私钥和服务端注册记录，直接复用。
- 服务端已有身份但本地缺失时，不拿默认 PIN 猜测、不重新注册；
  使用 `client.messages.ensure_identity(pin="原PIN", key_version="需要时指定版本")` 恢复，
  或为该账号配置 `X_CHAT_PIN`。错误 PIN 不自动重试。
- 初始化或恢复结果不明时保留状态并报错；显式
  `ensure_identity(resume_setup=True)` 核对初始化，或确认原 PIN 后
  `ensure_identity(pin="原PIN", retry_unlock=True)` 再尝试恢复。
- Managed PIN 身份的自动恢复仍不支持，会明确失败；不会替换为默认 Custom PIN。

### 其他业务入口

| 能力 | 正式入口 |
|---|---|
| Cookie 会话 / 账密登录 | `XClient.from_cookies(cookies, proxy=...)` / `XClient.login(username, password, profile=...)` |
| 当前用户 / 用户资料 / 用户作品 | `users.me()` / `users.get(username)` / `users.posts(username, cursor=...)` |
| 关注 / 取消关注 | `users.follow(username)` / `users.unfollow(username)` |
| 通知与分页 | `notifications.list(category=..., cursor=...)` |
| 搜索 / 详情 / 首页 | `posts.search(query)` / `posts.get(post_id)` / `posts.home()` |
| 列表搜索与分页 | `lists.search(query, count=20, cursor=...)` |
| 发帖 / 串文 / 删除 | `posts.create(text, images=...)` / `posts.thread(texts)` / `posts.delete(post_id)` |
| 评论读取 / 回复 | `comments.list(post_id, cursor=...)` / `comments.create(post_id, text)` |
| 点赞 / 转帖 / 收藏及撤销 | `posts.like/unlike/repost/undo_repost/bookmark/unbookmark(post_id)` |
| 图片、视频分片上传 | `media.upload(path)` / `media.upload_many(paths)` |
| 文章建草稿 / 更新 / 发布 / 列表 / 删除 | `articles.create(markdown)` / `articles.update(id, markdown)` / `articles.publish(id)` / `articles.list()` / `articles.delete(id)` |

写接口直接执行调用，不附带 CLI 的 `--apply` 预览语义。各返回值为字典，失败统一抛
`XClientError`。`code` 区分 `auth_required`、`forbidden`、`rate_limited`、
`risk_controlled`、`timeout`、`network_error`、`invalid_response` 等，
可选 `upstream_codes`、`status_code`、`retry_after` 保留安全诊断信息；不会返回原始错误 URL。
`retryable` 默认 false，不授权自动重放写入。`result` 可保留串文/文章的部分完成结果。
串文中途失败会保留 `post_ids`、从 0 开始的 `failed_index` 与当前项 `delivery_status`；
未知回执不再丢掉已确认 ID。单帖无可确认 ID 返回 `post_unconfirmed`。
关注/取关须核对回执中的目标账号与 following 状态，空对象不算成功。
私信提供持久化请求去重；其他写接口目前不提供同样的持久化幂等键，网络结果不明时必须先核对。
底层 API 类仍可用于协议诊断，但业务项目无需使用这些低层步骤。
指定用户新原创/引用推文已有持续监听入口，支持断点、去重、回调与可选 Webhook，见下节。
私信与通知的持续监听尚未实现，实施计划见 [后续监听计划](plans/deferred-listeners.md)。
账密登录沿用已有 Castle profile 和验证码/2FA 条件；SDK 封装不改变这些前置条件。

CLI 同样复用 SDK：

```bash
python main.py message TZh3gl                  # 只读预检
python main.py message TZh3gl "你好" --request-id business-001 --apply
python main.py message-status business-001    # 只回读
python main.py messages TZh3gl --limit 20
```

2026-10-02 正式 SDK 验证：离线测试覆盖真实 SDK 加密/解密、首次请求和续发、请求去重、
超时恢复、分页、账号隔离及默认 PIN 分支。真实只读验证通过账号身份、已注册聊天身份匹配、
指定会话文字和通知读取；迁入已发送消息回执后重复调用 `send()`，确认没有新增发送调用。
这轮未对真实的未初始化账号注册 PIN；自动首次设置的远端写入使用离线替身验证，
不能据此声称所有真实账号、Managed PIN 或新加密会话均已在线验收。

## social 接入与候选发布

当前候选版本为 `xapis-internal==0.1.0rc1`。独立 HTTP 服务只开放
`profile.get`、`content.get`、`content.search`、`content.list_by_actor` 和 `reply.list`。
social 的 X `web` channel 通过一次 Provider HTTP 调用转发；既有渠道保持各自契约。
每次请求携带调用方 Cookie 和可选代理，返回一页及原始游标；不自动翻页或重试。
用户资料和作品返回 SDK 字段，不能直接套用其他渠道的结果字段。

入口为 `python -m xapis_service` / `xapis-service`，生产运行使用 Flask + Gunicorn。
服务密钥、监听地址、容器配置、错误契约及验收要求见
[HTTP Provider 运行说明](deploy/service/README.md)。`/openapi.json` 提供服务机器契约。
账号凭据不进入共享状态、日志或镜像；首版不开放 HTTP 写入、登录或 X Chat 状态管理。

SDK 读取会校验业务结构和返回类型；缺失 timeline 与合法空页明确区分。
HTTP 服务使用随版本打包的签名素材，启动时校验，不依赖公共登录页的 A/B 路由，
不读取宿主机签名素材覆盖配置。素材失效时应通过受控测试发布新版本，不自动回退。

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
python -m build --no-isolation
python scripts/check_dist.py dist/*.whl dist/*.tar.gz
```

wheel/sdist 使用显式资源白名单，排除 Cookie、`.env`、`.xapis`、live 抓取文件与 outputs。
CI 配置检查 Python 3.12/3.14、离线测试、分发包和离开源码目录的安装；只归档产物，不自动发布。
本地测试、CI 配置、已发布版本与真实 social/X 验收是不同证据，部署前须逐项确认。

## 指定用户长期监听

监听公开用户不需要**被监听者**提供 Cookie 或授权。现在提供两种明确的数据来源：

- **访客模式**：`--guest` 或 `UserPostMonitor.guest()`，使用自动获取的 guest token，完全不使用
  X 登录 Cookie，不构造已登录客户端、不调用 Viewer。即使 `.env` 已有 `X_COOKIES` 也不会使用，
  失败不会自动切换到账号采集。当前已验证公开账号资料和 UserTweets 分页读取；覆盖范围与长期稳定性仍需长跑验收。
- **采集账号模式**：`client.users.monitor()` / 不带 `--guest` 的 CLI，复用采集端统一配置的 X Cookie，
  业务调用方只提供用户名。状态与访客模式隔离。

没有接入付费供应商。第三方不要求客户提供 X Cookie，不代表其上游完全不使用账号。

无需登录 Cookie 的 SDK 用法：

```python
from x_apis import UserPostMonitor

with UserPostMonitor.guest(["OpenAI"], proxy=None, interval=60) as monitor:
    monitor.run(lambda event: print(event["post"]["work_url"], flush=True))
```

需要代理时显式传 `proxy`，或 CLI 在 `.env` 配置 `X_PROXY`；代理凭证不写入监听状态。

当前范围为来源时间线中可访问的新原创推文和引用推文（账号模式用 `UserOriginalsTimeline`，
访客模式用 `UserTweets`），主动过滤回复、转帖、
广告及其他作者条目；不监听资料变更、删帖、互动计数、通知或私信。
首次注册目标以本机 UTC 时间建立起点，之前的历史不推送；请保持采集机器时钟同步。
目标解析为稳定用户 ID 后持久化，同一名称重启继续追踪原用户，不因改名或名称被他人接管而换人。

```python
from x_apis import XClient

client = XClient.from_env(".env")  # 采集端统一配置；不是目标用户的登录态

def on_post(event):
    # 在这里完成可靠保存；正常返回表示已确认，异常或 False 表示未确认。
    # 接收方按 event_id 去重，网络操作应设置超时。
    print(event["post"]["work_url"], flush=True)

with client.users.monitor(["XDevelopers", "OpenAI"], interval=60) as monitor:
    monitor.run(on_post)  # 可传 threading.Event 为 stop，在外部 set() 后优雅退出
```

CLI 默认将推文事件逐行输出到 stdout，状态与脱敏错误输出到 stderr：

```bash
python main.py watch-users XDevelopers OpenAI
python main.py watch-users OpenAI --guest
python main.py watch-users OpenAI XDevelopers --guest --interval 5 --concurrency 3 --max-rps 1
python main.py watch-users XDevelopers --interval 60 --max-pages 5 --once
python main.py watch-users XDevelopers --status-only
python main.py watch-users XDevelopers --retry-failed
```

`--once` 并发执行到期的首页检查，再为各目标执行最多 `--max-pages` 页补拉；尊重重启前
保存的检查时间和限流退避。持续运行时首页和补拉由不同工作槽调度，补拉未完成时保留游标，
可再次运行或改为持续运行。Ctrl+C / SIGTERM 会等待当前请求和回调结束后退出。
SDK 回调在独立投递线程运行，不应永久阻塞；CLI 的 HTTP 请求有超时。
请由你现有的进程管理器托管长期进程；本仓库不会在执行一次命令后偷偷安装后台任务。

事件结构：

```json
{"event_id":"x:post.created:采集来源:用户ID:推文ID","type":"post.created","source_id":"采集来源","user_id":"目标用户ID","detected_at":"UTC时间","post":{"work_id":"推文ID","work_url":"推文链接","title":"正文"}}
```

实际 `post` 还包含现有 SDK 的作者、媒体和互动计数字段。投递保证至少一次，崩溃或响应丢失
可能重放同一 `event_id`。多目标和历史补拉不保证全局时间顺序；下游需要时按发布时间排序。

状态保存在 `.xapis/<采集账号ID或guest>/user-monitor/`（沿用 `X_CHAT_STATE_DIR` 作为默认状态根目录），
目录 0700、数据库 0600；单一来源使用进程锁，只允许一个监听实例。
目标断点、已见事件和待投递记录持久化；事件落库与分页推进在同一事务内。
数据库会兼容迁移旧版监听表，保留已有基线、事件与未完成游标。首页检查与历史补拉分别保存
到期时间、失败状态和进度；首页发现新内容会立即安排必要补拉，正在补拉时也继续检查首页。
事件以首次注册的基线过滤，不再仅因 ID 落在已推进水位之前就丢弃延迟出现的内容。

默认总并发为 3：两个首页工作槽、一个补拉工作槽；每个工作线程复用独立 HTTP Session，
连接池与 Cookie 按采集来源隔离。首页检查间隔默认 60 秒，可设置为至少 3 秒；5 秒是需要
按目标数量与网络条件实测的配置值，不是端到端延迟承诺。来源默认每秒最多启动 1 次逻辑页请求，
首页优先使用额度；初始化与访客 token 恢复的内部请求另计。目标数 / 间隔超过请求预算时，
实际检查会落后，可通过 `head_check_gap_ms` 观察，不会靠无限并发追赶。

内置两种来源都尚未证明跨页严格按时间倒序，因此不会仅因一页全是旧推文就停止补拉。
默认每次扫描最多 20 页（含首页），每个补拉任务最多 5 页，完成或达到总上限后每 300 秒
安排复扫。`coverage=complete` 只表示该次扫描走到了来源返回的末尾；`bounded` 表示达到
页数预算、仍有未覆盖部分；`gap` 表示游标异常导致扫描中断。任何状态都不证明上游返回了
该账号的所有实际推文。可用 `--scan-page-limit` 增加范围，用 `--rescan-interval` 调整复扫频率。
只有自定义来源显式保证 `chronological=True` 时才启用旧页提前结束和 `overlap` 重叠窗口。

临时失败退避；429 尊重服务端等待时间，冷却作用于整个来源。明确的来源封禁同样整体冷却，
单个目标不可访问或未分类的 403 则仅退避该目标。访客每 3 小时惰性续期；收到明确的访客
token 失效证据时只刷新并重试一次，并发请求共享刷新结果，不切换 Cookie。账号鉴权失效或
访客恢复后仍失效会退出并保留断点；不自动登录或覆盖 Cookie。

投递失败指数退避，默认 10 次后转为 `failed`，通过 `retry_failed()` 或 `--retry-failed` 显式重放。
`monitor.status()` / CLI 状态含首页与补拉进度、覆盖状态、来源冷却和投递积压。
指标包括请求成功/失败次数、发现/投递事件数，以及请求耗时、实际首页检查间隔、
发布到发现、发现到投递的延迟。时间单位为毫秒；发布时间由 Snowflake ID 估计。
计数按来源持久累计，每类分位数基于最近 1000 个样本，不能当作整个长跑期间的全量分布。
样本为空表示尚无测量证据，不是延迟为零；时钟异常计入 `clock_skew_samples`。
数据库保留推文正文及去重记录，目前不自动清理，长跑时需监控磁盘占用；省略某个目标只停止
本次进程对它的采集和投递，不删除它的历史断点。更换接收回调或 Webhook 前核对待投递事件，
重启后仍待投递的事件会交给新接收端。

可选 Webhook：在采集端本地 `.env` 配置 `X_MONITOR_WEBHOOK_URL` 与
`X_MONITOR_WEBHOOK_SECRET`，再显式传 `--webhook`。不要把密钥放入命令参数。
只接受 HTTPS（本机测试可 HTTP）；不跟随重定向，不发送 X Cookie，不继承 netrc 或环境代理。
接收方应先落库再返回 2xx，并用本地密钥验证
`HMAC-SHA256(timestamp + "." + 原始请求体)`，对应请求头为
`X-XApis-Timestamp`、`X-XApis-Signature: sha256=<hex>` 和 `X-XApis-Event-Id`。
验证时间戳窗口以防重放，并按事件 ID 做业务幂等。SDK 可将 `Webhook(url, secret)` 作为回调。

需要其他数据源时，可用 `UserPostMonitor.from_source(source, usernames, state_dir=...)`，
无需构造 `XClient` 或调用 Viewer。来源契约是 `scope`（稳定、无凭证的存储命名空间）、
`resolve(username)`（数字用户 ID）、`fetch_page(user_id, cursor)`
（规范化推文列表、非置顶时间线推文 ID 列表、下一页游标）。错误或不完整响应必须抛异常，
不能当作空页。支持并发时显式声明 `thread_safe=True`，否则兼容串行调用；来源的可选
`close()` 在工作线程结束后调用。这个扩展入口不等于已经接入供应商数据源。

### 有时限的运行与独立对照

下面的命令由操作者显式启动，使用独立状态目录，进入监听循环后运行 24 小时自动退出。
初始化不计入时长，正在执行的请求/回调退出时会等待完成；电脑休眠或退出进程会中断运行，
因此不能只依据经过 24 小时就宣称连续运行验收完成。

```bash
python main.py watch-users OpenAI XDevelopers --guest --interval 5 \
  --concurrency 3 --max-rps 1 --max-pages 1 --scan-page-limit 20 \
  --duration 86400 --state-dir .xapis/user-monitor-validation \
  --metrics-file .xapis/user-monitor-validation/report.json
```

指标 JSON 原子更新、权限 0600，不含推文正文或凭证。stdout 是事件 JSONL，stderr 是状态，
如需保存日志应放在私有状态目录。投递线程在新事件入库后即被唤醒，无需固定等一秒。
判断漏报需要独立来源的已知发布清单，监听器仅凭自身结果无法算出真实漏报率。
准备 JSON 文件 `[{"user_id":"目标数字ID","post_id":"已知推文ID"}]` 后，可在同一批目标上使用
`--status-only --reference-posts <文件>`，或 SDK 的 `monitor.compare_posts(records)`。
输出含观察到的比例、`not_observed` 清单与基线之前的条目数；未观察到可能仍在补拉，不能
直接等同于永久丢失。对照清单必须自行核对目标、发布时间和可见性，不能用本次输出自证完整。

轮询延迟取决于间隔、目标数、分页积压、上游索引和限流。无法承诺秒级推送，也无法补回
两次检查之间已经删除或不再可见的推文；接口排序和可见范围同样限制完整性。
本地回归覆盖断点、去重、分页、退避、进程锁与投递失败；长期线上可用性须另按实际账号和时长验收。

2026-10-02 验证：公开账号 OpenAI 的访客 token、用户资料、UserTweets 首屏及下一页已只读验证。
`UserPostMonitor.guest()` 在正常入口完成两轮检查和重启断点恢复，全程无登录 Cookie；
采集账号模式的 UserOriginalsTimeline 也完成一次真实检查。访客调用 UserOriginalsTimeline
返回 404，因此访客模式明确使用已读通的 UserTweets，不自动混用。
短时检查没有观测到新发布推文，不能据此声称真实新事件端到端触发或长期完整性已验收；
新增事件、重复处理与崩溃恢复由离线数据驱动测试覆盖，Webhook 503 后重投与签名在本机 HTTP 接收端验证。

同日调度升级后：全量 238 项回归通过，涵盖旧库迁移、补拉/首页隔离、慢目标隔离、乱序页面、
扫描预算、访客恢复、连接复用和指标持久化。两个公开账号 OpenAI、XDevelopers 的访客短测
在进入监听循环后运行 30 秒，14 次逻辑页读取全部成功；实际首页检查间隔 P50 为 5.01 秒、
P95 为 5.39 秒。为限制本次读量，扫描上限设为 3 页，OpenAI 明确报告 `bounded`。
本次未观察到新发布推文，因此没有真实新事件发现/投递延迟样本，也未取得独立漏报率对照；
这次短测不能替代 24 小时持续运行验收。

## 🚀 快速开始

```bash
git clone https://github.com/cv-cat/XApis.git
cd XApis
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
# source .venv/bin/activate

pip install -r requirements.txt
copy .env.example .env       # macOS / Linux 使用 cp .env.example .env
```

### 先验证已有 Cookie

只有 Cookie 和代理时，不需要填写密码或 Castle profile。在 `.env` 中填写：

```dotenv
X_COOKIES='auth_token=<自己的值>; ct0=<同一会话的值>; ...'
X_PROXY=''
```

Cookie 至少需要 `auth_token` 和与之配套的 `ct0`，应来自同一账号、同一会话。不要将真实值提交到仓库或贴进日志。变量名必须是大写 `X_COOKIES`、`X_PROXY`，`X_Proxy` 不会被代码读取。

```bash
python main.py whoami                       # 只读 Viewer 身份检查
python main.py search "python" --product Latest  # 身份确认后的可选读取
```

`whoami` 的成功条件是返回预期账号身份，不只是 HTTP 200。它不会执行账密登录或写回 Cookie。当前基线对部分错误响应仍可能抛出 `KeyError: 'user_results'`，排查方式见下文。

`X_PROXY` 用于配置 HTTP/HTTPS 请求代理；为空时会尝试探测环境/系统代理。`load_dotenv()` 默认不覆盖进程中已有的同名环境变量，因此修改 `.env` 后还需确认没有被终端环境覆盖。**传入了代理配置，不等于已核验实际出口 IP，也不保证代理出口在整个流程中固定。**

### 需要账密登录时

在 `.env` 中补充：

```dotenv
X_USERNAME=''
X_PASSWORD=''
X_PROXY=''
CASTLE_PURE_RL_FILE='castle_profile.json'
```

`castle_profile.json` 必须是同一登录页状态采集的完整 Castle Rl/profile，而不是只填写用户名、密码就能生成的配置。

当前基线的 `generate_pure()` 默认使用 `compression='native'`，随仓库提供的 Chromium-zlib 后端仅支持 Windows。macOS/Linux 不能直接按这个默认路径运行；另有显式 `fallback` 压缩实现，但本轮没有验证其真实登录成功，不能把它视为等价替代。

满足上述条件后，快速示例为：

```bash
python quickstart.py
```

`quickstart.py` 每次执行账密登录，成功后把 Cookie 写回 `.env`，再搜索“美国”并打印结果；它不是已有 Cookie 的最小验证入口。修改
`quickstart.py` 顶部的 `SEARCH_QUERY` / `SEARCH_PRODUCT` 可切换搜索内容。

CLI 登录入口：

```bash
python main.py login                         # 有效 cookie 直接复用
python main.py login --force --pure-only \
  --pure-rl castle_profile.json               # 强制纯算重登
# 没有 profile 配置、未强制纯算时，可显式选择 Node 兼容路径（需 Node.js）
python main.py login --force --compat-node
```

真正进入账密流程时，未指定 `--compat-node` 的 CLI 默认要求纯算 profile；已有有效 Cookie 的复用不需要它。若配置了 `--pure-rl` / `CASTLE_PURE_RL_FILE`，或启用了 `--pure-only` / `CASTLE_PURE_ONLY`，纯算要求仍然优先，不能靠 `--compat-node` 绕过。

当前 CLI 使用 `XJetfuelLoginApi` 网页登录流（`jf.x.com/onboarding/web/actions/*`）。仓库还保留了带 Android 身份头的 `XLoginApi` 旧实现，但它不是上述 CLI 的默认路径。

注意当前基线的文件行为：`main.py login` 判定已有 Cookie 无效后，会先从 `.env` 清除它，再尝试账密登录；后续失败并不保证原 Cookie 仍保留。开始这类操作前应自行保留可用凭据。不要仅为检查会话而运行 `login` 或 `quickstart.py`。

### ✍️ 长推 / Thread / 文章（Premium）

```bash
python main.py post --file long.txt           # 超过 280 权重自动走长推 CreateNoteTweet
python main.py thread --file thread.md        # 单独一行 --- 分隔每条，逐条回复上一条
python main.py article article.md --cover banner.png            # 只存草稿，网页上预览
python main.py article article.md --cover banner.png --publish \
  --caption "发布时附带的推文" --reply-mode Verified              # 直接发布
python main.py article-list [--published]
python main.py article-delete <文章id>
```

文章正文用 Markdown 写，第一行 `# 标题` 会被拆成文章标题；支持 `#` / `##` 标题、
`**粗体**` `*斜体*` `~~删除线~~` `[链接](url)`、有序 / 无序列表、`>` 引用、
`---` 分割线、独占一行的 `![](图片路径)`、```` ``` ```` 代码块。
封面建议 5:2。

### 通知读取

```bash
python main.py notifications --type All --count 20
python main.py notifications --type Mentions
python main.py notifications --type Priority
python main.py notifications --type All --cursor '<上一页 next_cursor>'
```

```python
from utils.common_util import load_env
from x_apis.x_api import XAPI
from utils.notification_util import parse_notification_page

auth = load_env()
raw = XAPI.get_notifications(auth, type="All", count=20)
page = parse_notification_page(raw)
# page["items"] 包含通知文本、参与用户、关联推文和服务器事件类型。
# page["next_cursor"] 可用于下一次 get_notifications(..., cursor=...)。
```

分类值为 `All`、`Mentions`、`Verified`、`Priority`、`SuperFollowers`。
使用当前网页原生 `NotificationsTimeline` GraphQL，通过既有 Cookie、代理和 XCTID 请求。
该操作 queryId 会随前端发版变化；2026-10-01 已从当前公开通知 bundle 核对并更新这一项注册表。
读取不调用通知已读写接口，也不执行返回 timeline 中清理未读标记的客户端指令。

回复和提及通常以 `TimelineTweet` 返回，关注等聚合通知以 `TimelineNotification` 返回；只数聚合对象会漏掉回复。
解析器保留未知通知类型，推文不可见时明确标记，不生成空用户名的虚假推文链接。
缺失 timeline 或服务端分类错误不会伪装成“0 条通知”；部分数据错误随 `error_codes` 保留。

2026-10-01 当前账号实测：`All` 首屏 4 条（3 条回复、1 条聚合关注），下一页为空且不再返回底部游标；
`Mentions` 1 条、`Verified` 成功但为空、`Priority` 2 条。`SuperFollowers` 返回 `DependencyError` 且 timeline 为空，
当前未验证可用，也没有证据将该错误归因为特定订阅权限。当前样本未出现点赞或转推通知，不能据此完成这些事件的在线验收。
新增 16 项通知请求、解析及 CLI 离线回归覆盖这些边界。

### X Chat 加密文字私信

`XChatAPI.send_message()` 现在调用原生 `SendMessageCreateMutation`，不再无条件抛出“需要设置密码”。
`prepare_text_message()` 负责文本的 Thrift Binary 编码、SecretBox 加密、消息校验信息和 P-256 签名。
这条路径不调用旧的 `/i/api/1.1/dm/new2.json`。

Cookie、聊天 token、passcode 与加密密钥作用不同。浏览器设置过 passcode，不会自动把密钥提供给 Python；
程序需要合法取得的最新会话密钥、已注册签名私钥及各自版本。账号级首次设置和 PIN 恢复现由下文的身份管理命令提供；
发送入口本身不自动创建、恢复或轮换身份。
密钥文件只保存在本地 Git 忽略目录，POSIX 权限必须为 `0600`；不要提交、打印或把它作为普通日志附件。
内部单聊 ID 使用按数字升序排列的 `用户ID:用户ID`，不同于网页路由的短横线形式。

```python
from utils.common_util import load_env
from utils.xchat_crypto import load_key_material, prepare_text_message
from x_apis.x_dm_api import XChatAPI

keys = load_key_material("_scratch/xchat-send-20261001/keys.local.json")
prepared = prepare_text_message("hi", keys)  # 只在本地准备，不发送
# 确认发送者、收件人和正文后，才调用：
# raw = XChatAPI.send_message(load_env(), **prepared.as_send_kwargs())
```

CLI 默认只在本地准备消息，不联网、不发送。实际发送前核对当前会话身份、收件人和 X Chat 成员接口的
`chat_permissions.can_dm`；普通用户资料响应不一定包含私信权限，不能把缺失字段当作服务端禁止。

```bash
python main.py chat-send danke_max "hi" --keys _scratch/xchat-send-20261001/keys.local.json
# 确认要新增这条私信后，使用一个尚不存在的 receipt 路径：
python main.py chat-send danke_max "hi" --keys _scratch/xchat-send-20261001/keys.local.json \
  --apply --receipt _scratch/my-new-chat-receipt.json
```

`--apply` 会在发送前独占创建权限 `0600` 的回执文件。文件已存在时拒绝重发；未知结果保留原消息 ID，
应先调用会话读取接口核对，不能靠更换回执文件名盲目重试。回执只保存身份、消息 ID 和状态，不保存私信正文或密钥。

密钥 JSON 字段：`sender_id`、`conversation_id`、`conversation_key_b64`、`conversation_key_version`、
`signing_key_b64`、`public_key_version`、`signature_version`、`contents_encryption_version`；
`signing_public_key_b64`（SPKI DER，用于核对公私钥）和 `conversation_token` 可选。
`signing_key_b64` 可编码 32 字节 P-256 私有 scalar 或 PEM/DER。所有版本均应取自当前账户状态，不要猜值。

文字发送入口支持已准备好密钥的单聊；新会话密钥分发入口见下节（在线验收状态单独说明）。不包含群成员变更、附件、回复、阅后即焚或完整收件箱解密。
消息 ID 使用 UUID。发生超时或不匹配回执时先核对原消息 ID，不自动重发。
HTTP 200 也可能携带编码后的 `MessageFailureEvent`；发送入口会解码并拒绝这种失败回执。

2026-10-01 验证：75 项协议、加密、发送请求与 CLI 离线测试通过；对网页已成功发送的消息进行只读回取，
确认解密、P-256 验签、消息校验信息及明文编码一致。随后经明确授权，使用新增 Python CLI 在一个已有单聊中
发送一条文字消息，原生 `SendMessageCreateMutation` 返回成功创建事件；再通过 `GetConversationPageQuery`
回读同一消息 ID，核对发送者、会话、解密正文、P-256 签名和消息校验信息，均通过。本次实际签名版本为 7，
`contents_encryption_version` 未设置；其他版本只有源码分支与离线测试证据，未逐一在线发送验证。

### 发起新的 X Chat 单聊

首次消息请求使用 `chat-request`。网页端区分 `can_dm`（可以发消息）与
`can_dm_on_xchat`（可以建立加密会话密钥）；`can_dm=true`、`Allowed` 且
`can_dm_on_xchat=false` 时，仍可以发起未加密的文字请求，不能把它误报为禁止私信。

```bash
# 默认仅查询发送身份和收件权限；正文不会发送
python main.py chat-request recipient "你好"
# 明确选择未加密的首次请求，发送一次并回读同一消息核对正文
python main.py chat-request recipient "你好" --apply --receipt first-request.local.json
# 发送结果不明确时只核对原消息，不重发
python main.py chat-request recipient "你好" --apply --resume --receipt first-request.local.json
```

这条路径使用网页原生 `SendMessageCreateMutation` 和未加密的 Thrift 文字事件，
签名为空；仅显式选择未加密请求时允许这种请求结构。会话已有事件时拒绝按首次请求重发，
尤其不会将已有加密会话降级。回执不保存正文；复用原回执会阻止二次提交。

双方满足加密会话条件时使用 `chat-start`：读取双方身份和私信权限，确认会话没有事件后，用官方 Chat XDK
生成新会话密钥、加密给双方并签名，通过网页原生 `AddEncryptedConversationKeysMutation`
提交，再回读同一密钥事件、验证签名和解包结果。输出可直接交给 `chat-send`。
发送者需要已经恢复的身份文件，收件人需要已注册 X Chat 公钥并允许当前账号发私信。

```bash
# 只读检查收件人身份与权限
python main.py chat-start recipient --state identity.local.json --out conversation.local.json

# 创建并回读确认密钥；回执、输出文件必须尚不存在
python main.py chat-start recipient --state identity.local.json --out conversation.local.json \
  --apply --receipt conversation-create.local.json

# 密钥创建与实际发送分开；此命令才发送文字
python main.py chat-send recipient "新会话测试消息，无需回复" --keys conversation.local.json \
  --apply --receipt first-message.local.json
```

创建发生超时或回读失败时，保留原回执和路径，在原 `chat-start` 命令后加 `--resume`。
恢复模式仅回读原事件，不再次提交密钥。存在旧会话事件时拒绝重建，使用 `chat-session`；
`InboxClosed` 会明确阻止创建，不通过密钥提交绕过收件人权限。

2026-10-02 在线验收：收件方原设置为 `No one`；经授权改为 `Everyone` 后，权限变为
`can_dm=true`、`can_dm_reason=Allowed`，但 `can_dm_on_xchat=false`。
Python 成功发起首次未加密文字请求，服务器返回 MessageCreateEvent；随后通过
`GetConversationPageQuery` 回读同一消息 ID，确认发送者、会话及正文一致，收件方网页的
“消息请求 → 其他”也显示该新会话及文字。收件设置按用户要求保留 `Everyone`。
这个结果证明新会话请求已测通；新加密会话密钥分发仍只有真实 SDK 离线回归证据，未在线提交。
X Number 联系路径未测试；资料页有 Message 按钮不等于当前账号可直接发送。

离线回归共 144 项通过，覆盖双方 SDK 密钥解包、签名绑定、请求映射、关闭收件箱、
未加密请求、已有会话保护、回读内容一致性以及超时后仅核对原事件、不自动重发。

### X Chat 账号初始化与 PIN 恢复

参考 [mautrix 的首次注册流程](https://github.com/mautrix/twitter/blob/main/pkg/connector/login.go)，
协议中的密钥生成、Juicebox 备份和恢复使用 [官方 Chat XDK](https://github.com/xdevplatform/chat-xdk) 的 Python 绑定
`chatxdk==0.5.0`。X 的公钥查询/注册继续复用本项目 Cookie、代理和 XCTID；不需要为了这条路径改用 OAuth API v2。
SDK 通过隔离子进程运行，代理只设置在该子进程中；PIN 和 realm token 仅经过内存管道，不写入身份文件或日志。

```bash
# 只读检查：公钥已注册不等于已确认 PIN 备份健康。
python main.py chat-status

# 默认只检查状态；真正首次设置时加 --apply，在终端隐藏输入并确认新 PIN。
python main.py chat-setup --state _scratch/xchat/accounts/account-a.json
python main.py chat-setup --state _scratch/xchat/accounts/account-a.json --apply

# 已有 PIN 的账号恢复自身密钥，目标文件必须尚不存在。
python main.py chat-unlock --state _scratch/xchat/accounts/account-b.json --apply
# 若服务端存在多个版本，先看 chat-status，再显式传 --key-version；不会轮流猜 PIN。
```

新 PIN 按网页的四位数字形态校验，重复数字或连续递增/递减 PIN 会在注册前拒绝。
恢复旧 PIN 不套用新的强度限制。非交互环境可使用权限 `0600` 的 `--pin-file`，不支持明文 `--pin` 参数。
不同账号使用独立身份文件，CLI 每次在线确认 Cookie 对应的用户 ID。已有账号不能通过 `chat-setup` 覆盖公钥；
公钥查询失败、响应不完整、Managed PIN 或已有不匹配身份都会明确停止，不当作“未初始化”。

首次设置的顺序是：本地生成身份/签名密钥并原子保存 → 单次注册公钥 → 使用服务端返回的版本和 realm 配置备份 →
锁定 SDK 后重新用同一 PIN 恢复并核对公钥 → 回读 X 公钥记录 → 标记就绪。身份文件包含私钥，使用 `0600` 和排他锁；
此文件管理实现目前使用 POSIX 文件锁，面向 macOS/Linux。

中断后先使用 `chat-status --state <文件>` 查看阶段。`chat-setup --resume --apply` 只复用原有身份：
若注册结果不明，会先查找同一公钥；未找到时不会自动重复注册。若备份已尝试，会只尝试恢复，不用新 PIN 覆盖备份。
恢复失败不自动更换公钥版本，也不自动重试 PIN。`pin_backup_state: unknown` 明确表示只读公钥查询无法证明备份状态。

恢复账号身份之后，可以从**已有单聊**的已签名密钥变更事件中解包会话密钥，接回现有发送入口：

```bash
python main.py chat-session '<自己的用户ID>:<对方用户ID>' \
  --state _scratch/xchat/accounts/account-a.json \
  --out _scratch/xchat/account-a-conversation.json \
  --signature-version 7 --contents-version 0
python main.py chat-send <对方用户名> '正文' --keys _scratch/xchat/account-a-conversation.json
```

上例版本 7/0 是本次账号的已验证配置，不是所有账号的固定值。`chat-session` 核对双方公钥绑定与密钥事件签名，
没有对应身份版本、无法解密或缺少密钥事件时停止，不生成假的会话密钥。`--state` 是**账号身份文件**，
`--out` 产物才是 `chat-send --keys` 使用的**会话发送配置**，两者不能混用。
本轮补的是首次 PIN 设置、恢复及已有会话接入，不包含 PIN 重置、密钥轮换和新会话密钥分发。

验证边界：当前已初始化账号的 `chat-status` 已在线读取；本地模拟代理确认 SDK 使用配置代理；
真实 SDK 的生成/导入、公钥绑定和签名会话密钥解包在合成数据上通过。
随后经账号持有人提供本地 PIN，`chat-unlock` 已完成真实 Juicebox 恢复并匹配 X 注册公钥；
再以恢复的身份执行 `chat-session`，成功解包已有单聊密钥，与先前浏览器导出的材料一致，
并正确解密比对已保存的 4 条真实历史消息。验证过程没有发送新消息，身份和会话文件均为 `0600`。
首次公钥注册和新 PIN 的 Juicebox 备份仍缺少未初始化账号的在线验收；这些首次设置分支目前由离线状态机测试覆盖。

## 🐍 SDK

```python
from utils.common_util import load_env
from x_apis.x_api import XAPI
from x_apis.x_write_api import XWriteAPI

auth = load_env()
result = XAPI.search_work(auth, "python", product="Latest")

ok, message, raw = XWriteAPI.post_tweet(auth, "hello from XApis")
print(ok, message)

# 长推：超过 280 权重自动走 CreateNoteTweet（也可 note=True 强制）
ok, message, raw = XWriteAPI.post_tweet(auth, long_text)
ok, message, tweet_ids = XWriteAPI.post_thread(auth, ["第一条", "第二条", "第三条"])

# 文章：Markdown -> 草稿（publish=True 直接发布）
from x_apis.x_article_api import XArticleAPI
ok, message, info = XArticleAPI.post_article(
    auth, open("article.md", encoding="utf-8").read(),
    cover="banner.png", base_dir=".", publish=False)
print(info["edit_url"])
```

## 📁 结构

```text
quickstart.py            纯 Python 账密登录并搜索
main.py                  CLI 入口
builder/                 请求、鉴权和 GraphQL 参数
x_apis/                  登录、读写、媒体与 X Chat 接口
utils/                   XCTID/Castle 纯算与数据处理
tests/                   通知与 X Chat 离线回归测试
static/                  GraphQL 注册表和运行时素材
assets/xapis-logo.png    项目 logo
```

## 接口变化巡检（独立 Docker 环境）

独立入口 `python -m api_watch` 提供 91 个探针：49 个读取变体、32 个受控写入场景、10 个手动登录/Chat 生命周期场景。
覆盖本 SDK 已实现的独立网络接口，包含 REST、上传、文章和 Chat；源码守卫防止新增底层接口漏登记。
`init` 生成独立配置/凭据模板及图片视频样本，`coverage` 显示参数缺口与当前配置的实际执行证据。
写入前落盘意图，超时不重试，登记并回读清理自己的测试资源；生命周期不参与后台调度。
使用 `Dockerfile.watch` / `compose.watch.yaml`、SQLite 和日志/通用 JSON Webhook，不自动覆盖业务接口注册表。

配置、启动、凭据挂载、覆盖范围与基线维护见 [巡检部署说明](deploy/api-watch/README.md)。
完整场景见 [探针清单](deploy/api-watch/COVERAGE.md)。当前 `x-web` 静态提取仍不证明动态资源图完整，
但主动探针逐项覆盖已封装接口。缺少凭据、样本或前置账号状态的项明确未通过，不以注册数量充当线上验收。

2026-10-02 全覆盖版本验证：仓库完整回归 412 项通过，监控相关 174 项在 Linux ARM64 镜像中通过；
包含真实本地 Chat SDK 加解密、写入意图日志、跨重启防重发、错误账号清理保护、回环 Webhook 投递验证。
初始化项目的配置与空凭据模板已通过容器挂载检查。首版匿名资源扫描已验证新版操作提取和不完整覆盖状态。
本轮未使用真实登录 Cookie，未执行真实发布/发送/注册/登录，也未部署长期监控；实际覆盖结果以配置后的 `coverage` 为准。

## 2026-11-07 API 同步记录

已从现有登录会话的同一份 home shell 完整读取 768 个业务 JS 资源，前端版本
`87c79ad79ded6844f4124588746090b7198a8686`。注册表现有 405 个操作：新增 63 个、
移除未被 SDK 使用的 `BakeryQuery`、更新 164 个，其中 18 个为 SDK 实际引用的操作。
同步了 queryId、featureSwitches、fieldToggles 和默认开关，以及同页 XCTID 与旧栈 Castle 素材。

Chat 元数据来自该页面明确引用的嵌入版本 `bf9d4ef96b0e920edb7a41bc02fcd5ab2413c691`
的公开模块 `dm-core-Bq9lt84w.js`：更新成员查询、收件箱初始页的 queryId，
巡检按当前事件类型及收件箱/增量页各自的游标联合类型校验，未知类型仍明确失败。

复用用户原 Google Chrome 的现有 X 标签页，确认当前账号与项目 Viewer 一致后完成在线验收。
已验证资料、作品、首页 GET/POST、Top/Latest/People/Media 搜索及可用下一页、详情、评论、
通知和 Chat 结构读取；五个 HTTP Provider 入口使用实际 SDK 和 X 响应通过验证。
修复搜索反馈提示、媒体搜索 `TimelineAddToModule`、通知未读控制指令、模块内推文计数、
收件箱结束游标可省略字段及媒体 FINALIZE 处理后大小校验；未知结构仍明确失败。

用户授权的真实发布严格为两条自然内容，图文与视频回复均回读核对作者、正文、媒体 ID 和父帖。
原 Chrome 页面已确认渲染；[图文主帖](https://x.com/mummypia/status/2119025986851274908)和
[视频回复](https://x.com/mummypia/status/2119026038466371613)按授权保留。赞、收藏、转推及关注
均已完成正反状态验证并恢复原状态。向受控接收账号只发送一条指定私信，确认加密发送、
回读解密、正文匹配及验签；已有身份匹配，无新身份注册。凭据、密钥和私信正文不进入验收报告。

Lists 已修正为 `ListSearchTimeline`，省略该操作不接受的 `querySource`，读取专用的
`list_search_timeline` 响应。`lists.search()` 与 CLI `search --product Lists` 返回列表核心字段和
原始分页游标，真实两页各返回 5 个列表且游标推进。X 的默认封面字段仍有 214/DecodeException，
仅这一明确的可选字段错误允许返回数据，并保留 `partial: true`、`error_codes` 与 `warnings`；
其他错误或核心字段缺失仍失败，巡检也不会把部分结果标为全通过。

SuperFollowers 与当前前端一致，要求账号为活跃订阅创作者且
`responsive_web_subscribers_ntab_for_creators_enabled` 开启。当前账号不满足开放条件，
SDK 明确抛出 `XClientError(code='capability_unavailable')`，CLI 返回 unavailable，
巡检保留 blocked；不再盲调该分类后暴露模糊 DependencyError，也不伪造空页或改读 All。
正常账号的其他通知请求保持原流程，开放账号仍保留实际服务端错误。

当前仍不能宣称所有分支可用：空列表和没有后续游标不计为相应分页通过。真实重新登录缺少密码和当前前端
profile；已有身份不重置来制造 PIN 首次注册或恢复场景，长推/文章写入未完成权限与实际验收。
两条发布内容保留，因此未调用 DeleteTweet。监听只验证了约 191 秒连续运行与重启断点保留，
不代表长期稳定性。Castle 完成本地令牌自检；未替换匿名新栈 UMD 或其已有信号 profile，未部署。

`refresh-graphql` 现在从同一登录态页面提取资源和默认值；下载不完整、元数据不支持、
操作冲突或空结果时保留原注册表，完整结果原子替换并保留文件读取权限。

## 测试

安装 `requirements-dev.txt` 中的依赖后，在项目根目录运行：

```bash
python -m unittest discover -s tests -v
```

单独运行一个测试模块：

```bash
python -m unittest tests.test_notifications -v
```

测试统一放在 `tests/`，使用标准库 `unittest`；X 上游由 mock/合成 SDK 替代，无需账号凭据。
HTTP、Webhook 和连接复用测试会临时监听本机回环端口，并清理自己创建的进程。
X Chat 用例会使用本地 SDK 验证密码学逻辑；Node.js 可用时还会进行 WebCrypto 签名对拍，不可用时跳过该项。

<a id="verification-2026-10-01"></a>

## 验证记录（2026-10-01）

本节总结早前登录诊断的事实与边界，不是对所有账号、地区或未来 X 前端版本的保证。起点为提交 `3fe6ea7d9700a415c14919e267d5826fc9be3e34`。诊断中曾修改运行时代码、签名计算和 SDK；**这些登录实验修改及新增测试当时已回退/移出默认路径，恢复到该基线并补充文档。** 上文的通知、X Chat 发送及身份管理是随后独立新增的功能，不属于这批已回退的登录实验。

原代码注释包含作者此前的验证描述；本轮结果不用于否定那些历史验证，也不能把过去可用等同于当前环境必然可用。

### 两套前端：必须记录会话条件

| 对照条件 | 本轮观察 | 可以确认的范围 |
|---|---|---|
| 同一 Chrome 环境，向 `/i/flow/login` 发出携带已有会话的请求 | HTTP 200，`responsive-web/client-web`，没有 HTTP 重定向 | 这份已登录会话可以取得旧栈页面 |
| 同一 Chrome 环境、同一地址，不携带会话（`credentials: omit`） | HTTP 200，`x-web/x-web`，没有 HTTP 重定向 | 本次对照中会话/Cookie 条件影响了前端分配 |
| 无痕浏览器执行 JavaScript，进入 `/i/jf/onboarding/web?mode=login` 的用户名输入界面 | 仍加载 `x-web` 入口、签名脚本和 Castle UMD | 本次新栈不只是未登录营销页，也承载了匿名登录界面 |
| 项目 HTTP 模板通过配置代理，在同一会话访问首页和登录页 | 均返回新栈；一次 UA/平台对照未改变结果 | 不能靠修改 UA 就假定已复现浏览器的旧栈环境 |

正常 Chrome 的已有会话与最初 `.env` 中用于账密尝试的账号不同。这不是“同一账号修复前后”的对照；浏览器的实际出口 IP 也没有被证明与程序代理相同。

因此，`utils/frontend.py` 原注释中的“新栈只负责营销页”不能完整描述这次现场；但这也不意味着旧栈已经失效，或可以把默认实现直接迁移到新栈。**应先确定要复现哪一份会话、哪一个前端，再核对它实际引用的整套素材。**

当时观察到的公开资源如下，文件名只用于定位这次样本，不能硬编码为永久地址：

| 场景 | 签名脚本 | Castle 脚本 |
|---|---|---|
| 已登录旧栈 `responsive-web/client-web` | `ondemand.s.24295910e9a75b62a.js` | `ondemand.castle.b45f30e9b4a2d8eba.js` |
| 匿名新栈 `x-web/x-web/assets` | `sign.o-dKm8qbBZ.js` | `castle.umd-Ceu_OdKs.js` |

匿名页面入口为 `entry-client-logged-out-BGWDYpk3.js`。回退后的仓库仍保留基线 SDK：旧栈元数据指向 `ondemand.castle.674f685cbe8c9596a.js`，另有 `castle.umd-BneRArir.js`。**本文记录的线上资源变化没有自动更新到当前代码中。**

### 已经成功验证的链路

在用户授权的一份有效 Cookie 会话上，使用独立验证脚本和当时含实验改动的工作区代码，完成了 **1 次 Viewer 身份读取**：返回预期账号，GraphQL 错误为空。最终 HTTP 验证由 Python 执行，浏览器只用于提供经授权的会话和参考证据。

该脚本的验证顺序是：

1. 在同一个 HTTP Session 中保留 X 域 Cookie，并取得旧栈页面原始 HTML。
2. 从这份 HTML 提取 key、动画帧和资源清单，再通过同一 Session 获取其引用的签名脚本与 Castle。
3. 核对 Castle 的来源 URL、公钥、模块 ID、远端内容哈希及本地 SDK 哈希一致。
4. 由同页素材构造 XCTID；同步页面响应更新的 Cookie/CSRF，避免最终请求叠加旧 Cookie 快照。
5. 使用同一 Session 调用 Viewer，核对账号身份及业务错误，而不是只看状态码。

这个结果证明了**该会话、该脚本组合下的旧栈读取可用**。它本身不是回退后原版 `main.py whoami` 的验收结果，也没有覆盖搜索分页、写接口、其他账号或长期稳定性。Viewer 请求不使用 Castle 登录令牌，所以上述 Castle 校验仅证明来源一致，不能证明令牌已通过登录风控。后续原版 CLI 的独立验证见下一节。

### 回退后原版 CLI 的实际验证

同日进一步使用上述授权账号的有效 Cookie 和配置代理，直接运行恢复至 `3fe6ea7` 的 `main.py`。运行时代码没有改动，也没有替换请求函数或注入实验签名实现；独立配置仅通过测试进程的环境变量传入，未覆盖原 `.env`。

| 完成时间（北京时间） | 原版命令 | 结果 |
|---|---|---|
| 2026-10-01 17:05:05 | `python main.py whoami` | 返回预期账号身份，退出码 0 |
| 2026-10-01 17:06:53 | `python main.py search "python" --product Latest` | 返回 20 条结果，退出码 0 |

**原基线的账号身份读取与一次搜索，已在这份 Cookie/代理配置下实测通过，不依赖前述实验修复。** 此次没有强制指定或额外观测内部签名素材的选择过程，因而不能据此断言程序没有使用原有缓存回退。结果不覆盖分页、写接口、其他账号、持续运行，也不证明匿名账密登录可用。

### 账密登录失败记录与未决原因

两次账密尝试均使用当时的原实现、配置代理和显式 Node 兼容路径，且都在 `begin_login`（提交用户名）阶段停止，尚未提交密码。以下是当时日志保留的信息，不是完整原始 HTTP 响应：

| 尝试时间（北京时间） | HTTP / 业务结果 | `x-rate-limit-reset` |
|---|---|---|
| 2026-10-01 14:48:48 | HTTP 200；“我们已临时限制你的登录。请稍后重试。” | `1790838228`，即当天 15:03:48 |
| 2026-10-01 15:26:03 | HTTP 200；相同业务限制 | `1790840463`，即当天 15:41:03 |

第二次已超过上次响应中的 reset 时间，仍然被拒绝。`retry-after` 未返回。因此，**reset 时间只是限流响应元数据，不能当作账号解锁承诺**；HTTP 200 也不代表登录成功。此后没有再提交账密登录，计划中的自动重试保持暂停。

已核对代理参数传入登录预热和 action 请求；但没有验证当时实际出口 IP、IP 是否固定或出口信誉，因而不能据此排除或认定 IP 问题。账号状态、客户端/签名适配及代理环境仍需区分，不能仅凭这句提示归因，也不能判断密码对错。

此前的 Cookie 最小测试还曾收到 GraphQL 错误码 `32`、`215`，同时响应里仍有部分 `data`。当前基线的读取封装允许部分成功，`whoami` 随后直接访问不存在的 `user_results`，会表现为 `KeyError`。这时应检查脱敏业务错误和预期身份字段，不要把 Python 异常直接解释为网络不通。后来另一份授权 Cookie 的读取成功，不证明最初那份 Cookie 有效或原账号限制已解除。

### 源码与离线观察：没有保留为默认修复

- 当时取得的新、旧前端源码都对 `jf.x.com` 使用固定 `PATCH` / `jf` 作为 XCTID 签名输入，而不是实际 GET/POST 方法和 pathname。该观察有源码依据，但没有经过本轮账密成功验收；回退后代码仍保持原实现。
- 两份签名脚本都将动画时间按 `Math.round(product / 10) * 10` 舍入。实验修正后，两组动画签名片段与真实 Chrome 计算一致；这只是计算对拍，不是完整登录验收。
- 线上旧栈 Castle 与基线文件存在版本差异。实验更新后的 Node 运行器能生成结构合法的 token，但“本地生成成功”不代表服务端接受。
- 本地曾发现一份标注为“x-web 页面 + 旧版 ondemand.s”的缓存。这是混配来源，不能因文件名含 `live` 或 token 能生成，就认定它可用于当前登录。
- 诊断中一度自动采用新栈签名素材，同时沿用旧栈 Castle 和既有登录流程；这没有得到端到端验证，相关改动已回退。未来排查也不能混用不同前端、页面状态或账号的样本。

当前基线仍会在在线 XCTID 素材加载失败后回退本地缓存，旧栈页面获取逻辑仍有最多 12 次尝试；增强诊断、默认路径约束、Cookie 失败保护及 SDK 更新等实验修改均未保留。回退是恢复可比较的基线，不是宣称这些旧行为已在当前环境重新验收。

### 测试与后续验证边界

早前登录诊断开始时，当时检出、常见测试路径及本地可见的相关历史中没有找到旧测试；`tests/` 当时还在 `.gitignore` 中。这不代表原作者或其他环境从未做过验证。当前通知与 X Chat 回归测试已统一放在 `tests/`，该目录已取消忽略。

本轮新增的回归套件最终有 **31 项离线测试通过**，它们针对实验实现，随后与实验代码一起归档，不是当前基线自带的测试，也不能据此宣称原代码通过了这些测试。回退时核对了文件与基线一致、CLI 导入及 `--help` 可运行；随后补做了上节所列的原版 `whoami` 和一次搜索，未重新执行账密登录。

后续若继续排查，应先在独立脚本或隔离 checkout 中固定账号、会话、代理条件、页面与 SDK 版本；已有 Cookie 读取与匿名账密登录分别验收。确认具体触发条件和最小差异后，再决定是否修改默认实现。保留原有凭据，原始 Cookie、密码、代理凭证和未脱敏抓包只留在受控本地，公开记录只保留必要元数据。

## 📈 Star History

<a href="https://github.com/cv-cat/XApis">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://cvcat.site/star-history/svg?repos=cv-cat/XApis&type=Date&theme=dark">
    <source media="(prefers-color-scheme: light)" srcset="https://cvcat.site/star-history/svg?repos=cv-cat/XApis&type=Date">
    <img alt="Star History Chart" src="https://cvcat.site/star-history/svg?repos=cv-cat/XApis&type=Date">
  </picture>
</a>

## 🍔 交流群

如果你对爬虫和 AI Agent 感兴趣，可以加入群聊一起讨论~

二维码可能会过期或满员，失效时请通过 Issue、微信或 QQ 提醒。第 4 个二维码为 2000 人 QQ 群。

<div align="center">
  <img width="220" src="https://cvcat.site/assets/group1.jpg" alt="交流群二维码 1">
  <img width="220" src="https://cvcat.site/assets/group2.jpg" alt="交流群二维码 2">
  <img width="220" src="https://cvcat.site/assets/group3.jpg" alt="交流群二维码 3">
  <img width="220" src="https://cvcat.site/assets/group4.jpg" alt="交流群二维码 4">
</div>

## 🤝 贡献

欢迎提交 Issue 或 PR。涉及请求字段、签名和加密链路的改动，请附上可复现的验证说明。

⭐ 如果项目对你有帮助，欢迎 Star。
