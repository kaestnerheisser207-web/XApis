# XApis 内部读取 Provider（候选版本）

此服务供 social 的 X `web` channel 使用。首版仅开放五项读取能力，接收调用方的
完整 Cookie 和可选代理；不读取项目 `.env`，不保存 Cookie，不创建 X Chat 身份。
发送、发布、登录、长期监听不在此 HTTP 版本的能力表内。

## 契约

`POST /v1/execute`，`Authorization: Bearer <服务密钥>`，`Content-Type: application/json`。
服务密钥与 X 账号 Cookie 是两种不同凭据。不要将它们放进 URL、日志或命令行参数。

```json
{
  "capability": "content.search",
  "account": {"cookies": "auth_token=...; ct0=...", "proxy": "http://proxy-host:8080"},
  "payload": {"query": "from:OpenAI", "product": "Latest", "count": 20, "cursor": null}
}
```

`proxy` 可省略，此时明确直连，不继承宿主机的 `HTTPS_PROXY`、`ALL_PROXY` 或
`NO_PROXY`。指定代理时也不接受宿主环境绕过它。每次调用创建独立会话，响应
Set-Cookie 不进入后续调用；不同账号的 Cookie、代理及签名状态不共享。

| capability | payload | data |
|---|---|---|
| `profile.get` | `username` | SDK 用户对象 |
| `content.get` | `content_id` | SDK 作品对象 |
| `content.search` | `query`，可选 `product/count/cursor` | `{items,next_cursor}` |
| `content.list_by_actor` | `username`，可选 `count/cursor` | `{items,next_cursor}` |
| `reply.list` | `content_id`，可选 `cursor` | `{items,next_cursor}` |

成功为 `{"data": ...}`；完整字段和限制见鉴权后的 `GET /openapi.json`。
`GET /v1/capabilities` 返回当前版本实际开放的列表。搜索仅支持 Top/Latest/Media。
每次只读一页，`next_cursor` 原样交给调用方，不自动补页或重试。
用户作品列表内部需要先解析用户名，再读取一页；这是一个 Provider 业务调用内的
必要协议读取，不是 social 重复发请求。

签名使用随同一 wheel/镜像发布的 `transaction_l1.json` 公共素材，启动时校验。
不在请求中抓取公共登录页，不读取宿主 `XCTID_PROFILE_FILE`，也不自动切换素材来源。
该素材仍可能随 X 前端变化而失效；更新须通过受控读取验收、发布新版本，
不能仅根据离线签名生成成功就判定在线可用。

缺失 timeline、响应类型错误和无法确认的对象均返回错误，不伪装成空列表。
用户 followers/description 未知时为 null；旧 SDK 作品计数字段沿用既有默认值，
因此结果不是独立证明上游提供了全部指标。

失败为非 2xx 和 `{"error":{"code":"...","message":"...","retryable":false}}`。
可选 `upstream_codes` 只含数字，`retry_after` 只含秒数；429 同时返回 Retry-After。
账号失效、权限、风控、限流、超时、网络和响应不符分别分类，不包含原始响应或 URL。
`retryable=false` 表示本版本不给自动重放授权；调用方根据业务情境决定新调用。
响应头 `X-Request-ID` 用于定位本次请求，不能作为业务幂等键。

## 运行

使用 Python 3.12 或 3.14 的 Linux/macOS 环境。生产 HTTP 进程使用 Flask + Gunicorn
同步 worker；不运行 Flask 开发服务器。安装构建产物或从受审阅源码安装：

```bash
python -m pip install -r requirements-service.txt
python -m pip install --no-deps .
```

配置显式注入环境。复制 [service.env.example](service.env.example) 后填写配置，
用已有密钥管理器注入 `XAPIS_API_KEY`，或将 `XAPIS_API_KEY_FILE` 指向可读的
0600 文件；两种方式不能同时设置。密钥为 32–512 个非空白 ASCII 字符。

```bash
python -m xapis_service
```

`XAPIS_BIND` 必填；`XAPIS_WORKERS` 默认 2（1–32）；`XAPIS_REQUEST_TIMEOUT`
默认 25 秒（1–120）。每 worker 同时处理一个请求，总时限覆盖签名素材和业务读取。
所有上游请求禁用重定向，单次原始响应最多 8 MiB，归一化响应最多 2 MiB，
入站 JSON 最多 64 KiB。Gunicorn 超时比业务时限多 5 秒，作为阻塞的最终进程保护。
每个 worker 的额外并发调用返回 503，不创建无界工作线程。

`GET /healthz` 表示进程提供 HTTP；`GET /readyz` 表示本地配置与资源已加载。
**两者不代表 X 会话有效或能力在线验收通过。** X 适配漂移由独立 api_watch 检查。
api_watch 的底层探针可能使用在线签名素材，其成功不等于此服务固定素材的成功；
服务版本仍须通过下面的真实 HTTP 读取验收。监控证据指纹已包含发布的签名素材摘要，
更换该文件不能沿用旧配置的通过记录。

## 容器

[Dockerfile.service](../../Dockerfile.service) 安装同一个 wheel，使用 UID/GID 10001。
[compose.service.yaml](../../compose.service.yaml) 默认只绑定宿主回环地址；放到跨机
环境时使用已有 HTTPS 入口或私网，并由网络配置限制调用方。

```bash
docker compose --env-file deploy/service/service.env -f compose.service.yaml config --quiet
docker compose --env-file deploy/service/service.env -f compose.service.yaml up -d --build
```

镜像使用只读根文件系统，只有 `/tmp` 为 tmpfs；本版本无需账号状态卷。
服务密钥通过环境/密钥文件注入，不进入镜像。升级前保存上一镜像 digest，
回滚时重新运行上一已验证镜像和配置；不删除已有 api_watch/X Chat 状态卷。

## 与 social 对接

social 配置 `XAPIS_ORIGIN` 与 Provider 服务密钥，在 X `web` channel 的请求
payload 提供 cookies/proxy 及业务参数。connector 只向此服务发送一次业务请求，
不自动翻页、重试、切换 Provider 或缓存账号结果。安装已有数据库的 Provider
目录注册按 social 仓库的显式部署步骤执行，本仓库不操作 social 数据库。

## 验收与边界

当前候选版本已完成的证据与尚未验证的边界见 [0.1.0rc1 本地验收记录](ACCEPTANCE.md)。

本地：`python -m unittest discover -s tests -v`；构建：`python -m build`。
发布前运行 `python scripts/check_dist.py`（具体参数见 `--help`），检查产物无凭据、
无 live 抓取资料，并在源码目录以外验证安装。CI 只测试和构建，不自动发布。

上线验收须使用受控账号完成 social → 此服务 → X 的五项读取、真实下一页、
Cookie 失效、429、超时和多账号隔离，并记录确切版本。离线 fixture、容器健康和
SDK 测试不能替代这组证据。真实写入需要另行开放对应能力及完成专项验收。

实现依据：[Flask 的 Gunicorn 部署说明](https://flask.palletsprojects.com/en/stable/deploying/gunicorn/)
与 [setuptools 打包配置](https://setuptools.pypa.io/en/latest/userguide/pyproject_config.html)。
