# XApis API Watch

独立运行的接口监控项目，复用本仓库 SDK。覆盖范围是 **XApis 已封装的独立网络接口与登记的请求变体**，不是 X 全站所有内部操作，也不把内部辅助函数算作不同接口。

现有 **91 个探针：49 个读取、32 个写入、10 个生命周期场景**。逐项清单见 [COVERAGE.md](COVERAGE.md)。源码覆盖测试会检查公开底层网络方法，新增接口未登记探针会使测试失败。登记完整不等于线上已通过；`coverage` 分别报告已实现、已配置、最近实际结果。

## 初始化与提供参数

```bash
python -m pip install -r requirements-watch.txt
python -m api_watch init --directory watch-project
```

初始化拒绝覆盖非空目录，创建独立项目：

```text
watch-project/
  config.json                 测试对象、场景开关和频率
  monitor.env                 凭据文件位置、代理、通知地址
  secrets/account.json        Cookie、PIN、可选登录凭据（0600）
  fixtures/probe.png          内置 320×180 测试图片
  fixtures/probe.mp4          内置 1 秒测试视频
```

该目录已被 Git 和 Docker 构建上下文忽略。路径参数相对于配置文件所在目录解析，不需要在命令行传 Cookie、密码或 PIN。

| 位置 | 参数 | 用途 |
|---|---|---|
| secrets/account.json | cookies | 测试账号完整 Cookie，至少含有效 auth_token、ct0 |
| config.json → samples | test_account_id | 同一个测试账号数字 ID；写入前须与 Viewer 完全匹配 |
| 同上 | username / search_query | 资料/时间线及搜索样本，默认 OpenAI / from:OpenAI |
| 同上 | post_id | 可访问且最好有评论的固定推文数字 ID |
| 同上 | peer_username / peer_user_id | 明确受控的第二个测试账号；关注/取关和聊天发送会核对二者 |
| 同上 | conversation_id | 已有测试单聊的 较小数字ID:较大数字ID，用于历史和分页 |
| 同上 | image_file / video_file | 初始化已提供默认素材，也可自行替换 |
| 同上 | chat_settings_contract_file | 审阅过的 Chat 设置字段/类型契约，生成方式见下文 |
| secrets/account.json | chat_pin | 首次身份备份或已有身份恢复使用；不猜默认 PIN |
| 同上 | identity_file | 可选现有身份文件，省略则使用状态卷中独立身份存储 |
| 同上 login | username / password / mode / profile_file | 仅手动登录场景需要，普通 Cookie 巡检不需要账密 |
| monitor.env | API_WATCH_PROXY | X 访问代理；空表示直连，不继承系统代理变量 |
| 同上 | API_WATCH_WEBHOOK_URL | 通用 JSON 告警接收地址；留空输出日志 |

`secrets/account.json` 模板：

```json
{
  "cookies": "",
  "chat_pin": "",
  "identity_file": "",
  "login": {"username": "", "password": "", "mode": "pure", "profile_file": ""}
}
```

profile_file、identity_file 相对路径以 account.json 所在目录为准。完整登录 profile 属于敏感材料，放在 secrets 目录中。`pure` 尊重 profile 声明的压缩方式；要求 Windows native 而平台不支持时明确阻断，不自动换算法。Linux 镜像含 Node 和原 Castle runner，可显式选择 `mode: "compat-node"`；执行兼容路径不等于一定通过 X 风控。验证码、安全挑战和限流会停止本次尝试。

原 `API_WATCH_COOKIES_FILE` 仍可用，并优先于 JSON 中的 cookies。每轮重新读取凭据，更新 Cookie 后恢复检查。身份文件与账号绑定，不能跨账号复用。

## 查看缺口与运行读取

以下命令统一使用同一配置、凭据和状态目录，所有全局参数放在子命令之前：

```bash
python -m api_watch --config watch-project/config.json --env-file watch-project/monitor.env --state-dir watch-project/state coverage
python -m api_watch --config watch-project/config.json --env-file watch-project/monitor.env --state-dir watch-project/state once --only probes
python -m api_watch --config watch-project/config.json --env-file watch-project/monitor.env --state-dir watch-project/state coverage --strict
```

每行包含 id、operation、method、source、requirements、missing、last_status。`--strict` 在任何项未配置、未运行或未通过时退出 2。报告只采用当前代码、参数、凭据和测试文件指纹对应的证据，换账号、PIN、profile 或样本后不能沿用旧绿灯。

先只填 Cookie、暂留 test_account_id 为空时，可只读查询本账号与受控 peer 的数字 ID，再填入 config.json：

```bash
python -m api_watch --config watch-project/config.json --env-file watch-project/monitor.env --state-dir watch-project/state inspect-account --peer <测试接收账号用户名>
```

输出不含 Cookie；给出的 conversation_id_format 只说明 ID 格式，不证明会话已经建立。

读取覆盖访客激活/资料/时间线、Viewer、两种用户时间线、首页 GET/POST、五种搜索产品、五类通知、详情/补水/评论、文章 Draft/Published、Chat 设置/成员/公钥/收件箱/会话历史。分页检查真实下一页及游标推进，没有下一页就明确未实际覆盖。某些分类需要账号权限或足够历史数据，不能造游标换取绿色。

当前 SDK 未实现的独立 Chat inbox 后续页接口不纳入“已有接口”分母，不重复 initial 请求伪装翻页。

Chat 设置没有可靠内置字段基线，先用配置的 Cookie 做一次只读采集：

```bash
python -m api_watch --config watch-project/config.json --env-file watch-project/monitor.env --state-dir watch-project/state capture-settings-contract --out watch-project/chat-settings.candidate.json
```

文件只含字段路径和类型，不含值。审阅必要字段后把 samples.chat_settings_contract_file 指向它；不会自动覆盖已有文件或接受当天新结构为基线。

## 写接口

config.json → writes 的配置对应真实副作用：

```json
{
  "enabled": true,
  "interval_seconds": 86400,
  "allow_publish": true,
  "allow_follow": true,
  "premium": true,
  "articles": true
}
```

```bash
python -m api_watch --config watch-project/config.json --env-file watch-project/monitor.env --state-dir watch-project/state once --only writes
```

会创建带唯一运行标记的临时推文，验证回复/引用/串文、赞/转推/收藏及撤销、图片视频上传、指定 peer 的关注/取关、文章创建修改发布删除。长推及文章需账号实际具备权限，配置开关不会授予权限。只做草稿可关闭 allow_publish，但发布和对应推文场景不计通过。

互动使用本轮创建的帖子；串文逐条回读确认作者、正文和父帖后才发下一条。删除前再次核对归属和标记。原本已关注的关系不会被取关；需要最初未关注的测试 peer 才能执行正反验证。

每次写入前持久保存意图；超时不重试，后续新写入暂停，重启也不能绕过。APPEND/metadata 标注为协议回执，媒体 roundtrip 另外核对帖内媒体 ID/type，不声称回读了下载权限。媒体无删除 API，按服务端规则到期。

清理失败或进程崩溃后：

```bash
python -m api_watch --state-dir watch-project/state journal
python -m api_watch --config watch-project/config.json --env-file watch-project/monitor.env --state-dir watch-project/state cleanup --run-id <运行ID>
```

cleanup 只处理该运行登记并能确认归属的对象，不重放创建动作。cleanup 与 acknowledge-run 需要先停止同状态目录的常驻进程；journal 和 coverage 可在运行时查看。仍未知的提交须先在 X 核对，再明确记录处理结论：

```bash
python -m api_watch --state-dir watch-project/state acknowledge-run --run-id <运行ID> --note "已核对未知提交并处理本次测试残留"
```

该操作解除运行暂停，但不把历史失败改成成功，不能替代实际核对。

## 手动登录与聊天生命周期

这些场景只由 `once` 手动执行，持续 `run` 不调度。总开关 lifecycle.enabled，子开关 login_enabled、legacy_login_enabled、chat_token、chat_identity、chat_identity_recover、chat_new_conversation、chat_send 默认关闭。多版本恢复可配 chat_key_version。

```bash
python -m api_watch --config watch-project/config.json --env-file watch-project/monitor.env --state-dir watch-project/state once --only lifecycle
```

- 首次注册需要未注册 Chat 公钥的测试账号，执行注册、SDK 备份和恢复校验；已有身份则明确未执行，不重复注册。
- 恢复需要服务端已有身份、正确 PIN，以及本监控状态目录尚无该身份。可用独立恢复验证 state-dir，保留原密钥；恢复分支禁止隐式注册。
- 新会话分发需要尚未建立加密会话的受控 peer；已有会话不会冒充首次分发成功。
- 发送只面向配置的 peer，正文为固定运行标记，使用持久 request_id；未知结果只 reconcile，回读时解密验签。
- 同时开启新/已有会话发送会发送两条测试消息。当前 SDK 无消息删除接口，这些消息会保留在明确配置的测试单聊。

首次注册、恢复、新会话、已有会话是不同前置状态，不能要求同一账号在一轮中同时满足。报告保留真实状态，不把“已有所以跳过”当通过。

## Docker 常驻环境

```bash
docker compose -f compose.watch.yaml config --quiet
docker compose -f compose.watch.yaml up -d --build
docker compose -f compose.watch.yaml logs -f --tail=50 api-watch
docker compose -f compose.watch.yaml exec api-watch python -m api_watch --config /project/config.json --env-file /project/monitor.env coverage
```

Compose 将 watch-project 只读挂到 /project，状态放命名卷 /state。镜像含 Python、Chat XDK、加密依赖、Node、原 SDK 与测试素材；UID/GID 为 10001，根文件系统只读，不开放端口。

Linux 宿主机需确保该 UID 可遍历项目目录，凭据目录/文件分别为 0700/0600 且属于该 UID。由管理员调整这个专用目录所有者，不把 Cookie 改成全局可读。Docker Desktop 文件权限映射需现场核对。

默认每小时扫描前端、每 15 分钟读取；开启 writes 后按其周期执行，默认每日一次，最短一小时。生命周期不自动运行。容器手动场景先 stop api-watch，再用 `docker compose -f compose.watch.yaml run --rm api-watch` 后接相同全局参数和子命令，最后 start。

同一状态卷有进程锁，只允许一个执行者。升级用 up -d --build，停止 down 不加 -v。SQLite 放宿主机本地卷，不用多机共享 NFS/SMB。

## 告警和证据边界

Webhook 接收通用 JSON POST（event_id/kind/check_id/status/summary/details），2xx 确认，持久退避重试、至少一次投递，接收方按 event_id 去重。它不是飞书/Telegram 原生消息体，可接通知网关；留空则打印日志。

部分数据伴 GraphQL errors、未知结构、账号失效、限流、清理失败不会假装成功。Cookie 失效后暂停账号检查，匿名继续；凭据、正文、原始响应不进入告警。外部主机监控需独立检查心跳，本进程停止后不能自行发送停机通知。

前端扫描保持账号/新旧栈隔离；client-web 完整快照才推断增删，x-web 只比较明确观察到的同名操作和资源版本。无法证明动态资源图完整，也不自动做浏览器方法/参数差分；主动探针发现失效后仍需定位修复。候选不会覆盖 static/graphql.json，accept-baseline/accept-resources/accept-operations 仍可维护比较基线。

未确认写入记录保留待处理，正常完成运行和缓存按保留期清理。离线测试、镜像可运行和清单完整不替代真实账号验收，只有实际执行并满足契约的那条探针才算通过。
