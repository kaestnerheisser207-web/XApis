# 0.1.0rc1 本地验收记录

日期：2026-10-02（Asia/Shanghai）。这是本地候选产物的证据，不是已部署或正式发布的声明。

## 交付范围

- SDK：安全错误分类、严格读取结构与结果类型校验、关注回执核对、单帖未知结果和串文部分结果保护。
- 独立 HTTP Provider：五项读取、显式分页、服务鉴权、请求级账号/代理隔离、总时限和响应大小上限。
- social：X `web` channel 的五项能力、connector、Go/TypeScript 契约、OpenAPI、Flow SDK、关闭状态的 Provider 注册 SQL。
- wheel/sdist、服务与巡检镜像、只构建和检查的 CI 配置。

## 已完成检查

| 边界 | 结果 |
|---|---|
| XApis 本机 Python 3.14 全量回归 | 477 项通过；X 上游均为合成数据，包含真实回环 HTTP/Gunicorn/Webhook |
| social Go | 全量 tests、vet、全部命令 build 通过 |
| social TypeScript | typecheck、31 文件共 207 项测试通过；最后生成后 58 项契约检查再次通过 |
| social → Gunicorn → Flask → 合成 SDK | 12 个显式调用对应 12 次 HTTP dispatch，五能力、分页、账号隔离与错误路径通过 |
| Python wheel / sdist | 显式文件白名单及完整性检查通过，不含 `.env`、`.xapis`、live 抓取或 outputs |
| 离开源码的安装 | 新临时安装目录中 SDK/service/资源加载、XCTID、Node ui_metrics、watch scaffold 检查通过 |
| Linux ARM64 服务镜像 | 构建通过；Python 3.12.15、UID 10001、只读根文件系统、无外网下实际 Gunicorn 启动和能力表通过 |
| 巡检镜像 | 构建通过；无外网条件下资源定位、目录和包含签名素材摘要的证据指纹通过 |

真实 X 读取使用已有有效会话及其代理，通过新服务应用的 `/v1/execute` 路由调用；
不包含真实登录、发布、发送或身份注册。公开样本为 OpenAI：

| 调用 | 实际结果 |
|---|---|
| `profile.get` | 200，目标用户名与数字 ID 有效 |
| `content.search` | 200，首屏 5 条；显式第二页 200、5 条 |
| `content.get` | 200，返回搜索所得目标作品 |
| `reply.list` | 200，36 条直接回复，存在下一页游标；本轮未继续评论第二页 |
| `content.list_by_actor` | 修复推荐关注模块解析后 200，首屏 26 条、显式第二页 29 条 |

`count=5` 是传给上游的页大小提示，实际数量可能更多或更少，不是返回数量上限。
列表与搜索的后续游标仅在调用方显式提供后读取，没有自动补页。

真实验证发现并修复了两个问题：公共登录页的 A/B 路由已返回 x-web，不能继续作为
每次业务读取的 client-web 素材入口；服务改为显式使用随版本发布且在线验证过的
签名素材。用户作品页含已知的 `TimelineUser` 推荐模块；校验该结构后跳过，未知类型
仍报错。这两个行为都有离线回归保护。

## 产物标识

本地 `dist/`（未上传）：

- `xapis_internal-0.1.0rc1-py3-none-any.whl`，83 个文件，SHA-256：
  `ee767b691e1fb2717463c9414f68f186c6f08419c4e25bb72e1b851a6e03f131`
- `xapis_internal-0.1.0rc1.tar.gz`，89 个文件，SHA-256：
  `5cc5bbfe3ccded2974f071dba9190c818028b0039b54fd3ea004e7abe73eaf88`
- 服务镜像 `xapis-internal:0.1.0rc1`，本地 image ID：
  `sha256:17b547991ccc3099dc2efdeaa57706ef371b0cbdc1cbce7ff6c8a5d6b1134dc8`

## 仍需部署阶段验证

- 两个仓库均未提交或推送，保留了原有工作区改动。GitHub CI 尚未实际运行。
- 没有执行 social 数据库注册 SQL、配置 Provider 密钥、打开 gates/routes 或部署服务器。
- 已验证真实 X 读取与本机跨进程 connector 两个边界；尚未验证已部署 social 的
  `/v1/execute` → XApis → X 整条线上链路。
- 真实失效会话、429、超时与多账号反例由本地合成测试覆盖，未在真实账号上刻意触发。
- 私信、发布、登录及长期监听均不在此 HTTP 首版开放范围。
- social 全仓格式检查被原有、Git 忽略的 `.claude/settings.local.json` 阻挡；未改该本地配置。
  本次修改文件的格式检查通过。
- 独立 api_watch 的绿灯不等同于该服务固定签名素材的可用性。`readyz` 也不访问 X。
  升级或更新素材后须重新执行真实读取验收；签名摘要变化会使旧探针证据失效。

部署与回滚步骤见 [运行说明](README.md)。
