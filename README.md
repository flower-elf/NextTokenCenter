# NextTokenCenter 多账号网关

<p align="center">
  <a href="https://github.com/flower-elf/NextTokenCenter/releases"><img src="https://img.shields.io/github/v/release/flower-elf/NextTokenCenter?style=flat-square&color=2496ED&label=Release" alt="Release"></a>
  <img src="https://img.shields.io/badge/Python-3.12+-blue.svg?style=flat-square" alt="Python">
  <img src="https://img.shields.io/badge/API-OpenAI_Compatible-412991?style=flat-square" alt="OpenAI API">
  <img src="https://img.shields.io/badge/License-MIT-green.svg?style=flat-square" alt="License">
  <img src="https://img.shields.io/badge/Vibe_Coding-100%25-ff69b4?style=flat-square" alt="Vibe Coding">
</p>

本项目把多个上游 AI 服务账号聚合成一个兼容 OpenAI 接口。客户端只需填 Base URL 和 API Key，即可使用平台提供的模型；所有账号的用量都可以在 Web 看板管理。

> 本项目由 **吃白饭的蓝色大肥鱼（Deepseek-V4.1-Flash-1017）** 配合开发。人类开发者提供架构和需求，大肥鱼完成界面编写和后端改进。

> 项目的上游来源与贡献者见[来源与贡献者](#来源与贡献者)。

## 功能

- **统一接口**：同时支持 Chat Completions、Responses 与 Anthropic Messages 协议，支持流式输出、工具调用和图片输入。
- **账号管理**：每个账号可以单独设置优先级、权重、并发、代理与模型排除，也可以批量启停。
- **会话粘性**：同一段对话固定使用同一个账号；累计消耗超过阈值后更换，并支持设置冷却时间。
- **限额与密钥**：可在账号级设置积分、每日 Token、每日积分与单模型 Token 限额；Key 可以限制渠道、模型、有效期、配额与来源 IP。
- **用量与费用**：按账号、模型与 Key 统计请求、Token、积分和缓存命中，并按官方价格估算费用。
- **媒体生成**：上游支持图片生成、图片编辑与视频任务时，可以通过统一接口调用。
- **看板与部署**：设置都在面板里；支持本机、局域网与 Docker。

## 快速开始

需要 Python 3.12 或更新版本。你也可以直接使用 releases 里的绿色包，解压后运行 `start-wb-proxy.bat（Windows）`、`start-wb-proxy.sh（Linux）` 或 `start-wb-proxy.command（macOS）`。

```bash
python -m pip install -r requirements.txt
python app/wb_proxy.py
```

启动后访问：

- **API**：`http://127.0.0.1:8788/v1`
- **看板**：`http://127.0.0.1:8788/`，默认密码为 `admin`

登录后请在“设置 → 面板密码”里修改默认密码。首次启动可在“账号”页点击“添加账号”，按提示完成 OAuth 授权。

局域网共享**强制要求** API Key：

```bash
python app/wb_proxy.py --lan --api-key 你的Key
```

如果你已经在面板设置过 Key，可以直接用以下命令启动。

```bash
python app/wb_proxy.py --lan
```

Docker 使用根目录的 `docker-compose.yml`：

```bash
docker compose up -d
docker compose pull && docker compose up -d
docker compose logs -f
```

`./accounts` 保存账号凭证和设置，`./usage` 保存用量记录，两个目录都要挂载。容器通过 `API_KEY` 环境变量设置 Key；没有设置时，网关自动生成并打印在日志里。

## 客户端接入

- **Base URL**：`http://127.0.0.1:8788/v1`
- **API Key**：本机没有配置 Key 时可以留空；配置过 Key 或开启局域网模式后，使用“密钥”页里的 Key。
- **模型**：填写 `/v1/models` 里的任意模型 ID。

所有 `/v1/...` 接口都可以去掉 `/v1` 前缀访问。

**Responses 客户端**

```bash
export OPENAI_BASE_URL="http://127.0.0.1:8788/v1"
export OPENAI_API_KEY="你的 API Key"
```

**Messages 客户端**

这个入口默认关闭，先在“设置 → 高级”里打开“Anthropic Messages 入口”。

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8788"
export ANTHROPIC_AUTH_TOKEN="你的 API Key"
export ANTHROPIC_MODEL="deepseek-v4.1-flash"
```

**curl**

```bash
curl http://127.0.0.1:8788/v1/chat/completions \
  -H "Authorization: Bearer 你的Key" \
  -H "Content-Type: application/json" \
  -d '{"model": "deepseek-v4.1-flash", "messages": [{"role": "user", "content": "你好"}]}'
```

## 看板

| 页面 | 内容 |
|---|---|
| 仪表盘 | 账号数量、可用账号、积分余额、今日请求与 Token、调用趋势、默认渠道 |
| 账号 | 添加账号、优先级、权重、并发、分组、代理、模型排除、导入与导出 |
| 任务 | 调度器状态、签到与活跃任务、积分变动记录 |
| 积分 | 每个账号的套餐、已用、剩余与到期时间 |
| 密钥 | Key 的渠道、模型限制、有效期、配额、IP 白名单与用量 |
| 模型 | 模型目录、能力规格、账号覆盖率与被排除的模型 |
| 测试台 | 用面板会话直接调试模型，可以指定账号 |
| 用量 | 按时间范围统计请求、Token、积分与缓存命中 |
| 费用估算 | 按官方价格折算费用，再对比积分折算支出 |
| 请求日志 | 每条请求的模型、账号、Token、积分、状态与来源 IP |
| 运行日志 | 网关控制台输出 |
| 设置 | 网关行为、账号限额、费用估算、代理槽、媒体生成、密码与高级选项 |

## 设置

| 设置组 | 内容 |
|---|---|
| 网关行为 | 定时任务时段、任务随机间隔、会话粘性、单会话轮换阈值（默认 100 M）、轮换冷却（默认 10 分钟）、积分刷新间隔、Messages 接口翻译、本地网络工具 |
| 账号 | 优先级、权重、并发上限、分组、代理槽、模型排除、出站身份 |
| 密钥 | 渠道绑定、模型限制、有效期、Token 与积分配额、IP 白名单 |
| 限额 | 保留积分、每日 Token、每日积分、单模型每日 Token、临期积分窗口 |
| 费用估算 | 每个模型的输入、缓存命中、输出价格与积分折算单价 |
| 渠道专属 | 默认出站身份、流式超时、模型目录缓存、签到与自动任务 |
| 代理槽 | 固定代理地址、自动发现、绑定与测试 |
| 媒体生成 | 开关、渠道、出站身份、生图频率、视频时长、Key 积分额度、视频并发 |
| 面板密码 | 修改看板登录密码 |
| 高级 | Anthropic Messages 入口、亲和调试日志、并发与超时、本地网络工具参数、代理自动发现、用量聚合缓存、日与小时分桶、最近请求索引 |
| 运维 | 定价刷新与变体继承、积分刷新、日志数据库与保留上限、上游超时、可信反向代理、提示词、更新检查 |

单会话轮换阈值按“同一段对话、同一账号、同一模型”累计，单位为 M（百万 Token）。填写 0 表示不限制；换号后计数清零。轮换冷却表示两次换号之间的最小间隔，填写 0 表示不设冷却。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /v1/chat/completions | Chat Completions，支持流式 |
| POST | /v1/completions | 旧版补全 |
| POST | /v1/responses | Responses，支持流式与自定义工具 |
| POST | /v1/messages | Anthropic Messages，默认关闭 |
| GET | /v1/models | 模型列表 |
| POST | /v1/images/generations | 图片生成 |
| POST | /v1/images/edits | 图片编辑 |
| POST | /v1/videos | 视频任务提交 |
| GET | /v1/media/tasks | 媒体任务查询 |
| GET | /v1/media/content/<id> | 媒体结果读取 |
| GET | /v1/media/models | 媒体模型列表 |
| GET | /health | 健康检查，不需要凭据 |

对外接口使用 `Authorization: Bearer` 或 `x-api-key` 鉴权。看板接口需要面板 session。

## 命令行参数

| 参数 | 说明 |
|---|---|
| `--host` | 监听地址，默认 `127.0.0.1`；对应环境变量 `HOST` |
| `--port` | 监听端口，默认 `8788`；对应环境变量 `PORT` |
| `--lan` | 监听全部网卡并强制要求 API Key |
| `--api-key` | 固定 API Key；在看板保存过 Key 后不再生效；对应环境变量 `API_KEY` |
| `--panel-password` | 启动时设置面板密码 |
| `--accounts-dir` | 账号与设置目录，默认 `./accounts`；对应环境变量 `ACCOUNTS_DIR` |
| `--usage-dir` | 用量记录目录，默认 `./usage` |
| `--system-prompt` | 请求没有 system 消息时注入的内容 |
| `--user-agent` | 覆盖出站 User-Agent |

其余选项以面板设置为准。

### 环境变量

同名环境变量优先于面板设置。下面列出全部可识别的变量。
| 分组 | 变量 | 默认值 | 说明 |
|---|---|---|---|
| 运行与目录 | `HOST` | `127.0.0.1` | 监听地址 |
| | `PORT` | `8788` | 监听端口 |
| | `API_KEY` / `WB_PROXY_KEY` | 空 | 固定 API Key |
| | `WB_PROXY_API_KEY` | 空 | 代理工具调用网关使用的 Key |
| | `ACCOUNTS_DIR` | `./accounts` | 账号与设置目录 |
| | `WB_PROXY_USAGE_DIR` | `./usage` | 用量记录目录 |
| | `WB_PROXY_CATALOG_DIR` | `./catalog` | 模型目录缓存 |
| | `WB_PROXY_DEFAULT_REALM` | `intl` | 启动时的默认渠道 |
| | `WB_MEDIA_DIR` | `app/media` | 媒体产物目录 |
| | `WB_PRICING_DIR` | 用量目录 | 定价策略数据目录 |
| | `WB_PRICING_FILE` | `pricing/pricing.json` | 定价快照文件 |
| | `WB_ATREST_KEYID` | 从密钥文件读取 | 桌面端静态密钥的 16 位十六进制 id |
| 并发与超时 | `WB_MAX_CONCURRENT_CHAT` | `32`，可填 `auto` | 对话并发上限 |
| | `WB_CHAT_SLOT_WAIT` | 30 秒 | 并发已满时的排队等待 |
| | `WB_CLIENT_TIMEOUT` | 120 秒 | 客户端连接读写超时 |
| | `WB_MAX_PAYLOAD_BYTES` | 16 MiB | 请求体上限 |
| | `WB_WEB_TURN_TIMEOUT` | 120 秒 | 网页任务单轮等待上限 |
| 本地网络工具 | `WB_MAX_WEB_ROUNDS` | 3，最大 8 | 工具调用最多代跑几轮 |
| | `WB_WEB_ENGINES` | 内置引擎链 | 搜索引擎顺序与子集 |
| | `WB_WEB_COOLDOWN` | 600 秒 | 被限流引擎的冷却 |
| | `WB_WEB_COOLDOWN_ERROR` | 60 秒 | 出错引擎的冷却 |
| | `WB_WEB_CACHE_TTL` | 600 秒 | 搜索与抓取缓存 |
| | `WB_WEB_MIN_INTERVAL` | 0.5 秒 | 对外请求最小间隔 |
| | `WB_WEB_BUDGET` | 25 秒 | 单次搜索总预算 |
| | `WB_WEB_TOOLS_PROXY` | 空 | 搜索请求使用的代理 |
| 代理发现 | `WB_PROXY_DISCOVER_HOST` | `cli-proxy-mihomo` | 自动发现的主机 |
| | `WB_PROXY_DISCOVER_PORTS` | `17901-17910` | 自动发现的端口段 |
| 缓存与统计 | `WB_STATS_TTL` | 15 秒 | 用量统计缓存 |
| | `WB_COUNT_TTL` | 30 秒 | 计数缓存 |
| | `WB_REMAINING_TTL` | 15 秒 | 剩余用量估算缓存 |
| | `WB_TASKS_CACHE_TTL` | 20 秒 | 任务状态缓存，0 关闭 |
| | `WB_USAGE_CACHE_MIN_BYTES` | 1 MiB | 用量聚合写盘字节阈值 |
| | `WB_USAGE_CACHE_MIN_SECONDS` | 900 秒 | 用量聚合写盘时间阈值 |
| | `WB_RECENT_INDEX_MIN_BYTES` | 256 KiB | 请求索引写盘字节阈值 |
| | `WB_RECENT_INDEX_MIN_SECONDS` | 900 秒 | 请求索引写盘时间阈值 |
| | `WB_ROW_MEMO_MAX` | 65,536 | 行级记忆表上限，0 关闭 |
| | `WB_ROW_COST_MEMO_MAX` | 16,384 | 行级定价记忆表上限，0 关闭 |
| | `WB_USAGE_HOUR_KEEP_DAYS` | 2 | 小时桶保留天数，`WB_USAGE_HOUR_KEEP_HOURS` 未设置时生效 |
| 连接与失败 | `WB_UPSTREAM_POOL_SIZE` | 2 条/键 | 上游连接复用池大小 |
| | `WB_UPSTREAM_POOL_IDLE` | 90 秒 | 空闲连接回收时间 |
| | `WB_FAIL_WINDOW` | 600 秒 | 账号失败计数的重置窗口 |
| | `WB_MODEL_SCOPED_ERRORS_PENALIZE` | 关闭 | 所有 5xx 都记账号故障 |
| | `WB_CACHE_HTTP_ERROR_BODY` | 关闭 | 缓存上游 HTTP 错误正文 |
| | `WB_BACKGROUND_MODEL_NAMES` | 内置名单 | 哪些模型名算后台请求 |
| | `WB_BACKGROUND_MODEL_TARGET` | 内置模型 | 后台请求改写到哪个模型 |
| 面板开关 | `WB_ENABLE_ANTHROPIC_MESSAGES` | 关闭 | Anthropic Messages 入口 |
| | `WB_AFFINITY_DEBUG` | 关闭 | 亲和调试日志 |
| | `WB_USAGE_CACHE` | 开启 | 用量聚合缓存 |
| | `WB_USAGE_DAY_BUCKETS` | 开启 | 日与小时分桶 |
| | `WB_RECENT_INDEX` | 关闭 | 最近请求索引 |
| | `WB_DSH_TOOL_NEUTRALIZATION` | 关闭 | 工具调用中性化 |
| | `WB_ENABLE_FREE_MODEL_CREDIT_FALLBACK` | 关闭 | 免费模型积分回退 |
| | `WB_PROMPT_CACHE_KEY` | 关闭 | 向上游附带 `prompt_cache_key` |
| | `WB_UPSTREAM_KEEPALIVE` | 开启 | 上游连接复用 |
| | `WB_TRUSTED_PROXIES` | 空 | 可信反向代理列表 |
| | `WB_UPDATE_CHECK_ENABLED` | 关闭 | 自动检查更新 |
| | `WB_UPDATE_CHECK_INTERVAL_HOURS` | 24 | 自动检查间隔 |
| | `WB_USAGE_HOUR_KEEP_HOURS` | 48 | 小时层保留小时数 |
| | `WB_USAGE_DAY_KEEP_DAYS` | 32 | 日桶保留天数 |
| 渠道接口覆盖 | `WB_CODEARTS_SNAP_HOST` | 官方地址 | 聊天服务地址 |
| | `WB_CODEARTS_SNAP_MANAGER` | 官方地址 | 会话管理地址 |
| | `WB_CODEARTS_STS_HOST` | 官方地址 | 临时凭证地址 |
| | `WB_CODEARTS_BENEFIT_HOST` | 官方地址 | 福利接口地址 |
| | `WB_CODEARTS_PORTAL_HOST` | 官方地址 | 门户地址 |
| 本地凭据扫描 | `CLAUDE_CONFIG_DIR` | 用户目录 | Claude 客户端配置目录 |
| | `CODEX_HOME` | 用户目录 | Codex 客户端配置目录 |
| | `WORKBUDDY_CONFIG_DIR` | 用户目录 | 渠道客户端配置目录 |
| | `CODEBUDDY_CONFIG_DIR` | 用户目录 | 渠道客户端配置目录 |
| | `LOCALAPPDATA` | 系统默认 | Windows 本地应用数据目录 |

渠道接口覆盖与本地凭据扫描只用于私有部署、测试和导入本机凭据，一般来说你不需要它们。

## 数据与安全

- `accounts/` 保存账号凭证与设置，包含可登录的凭据，不要提交到公开仓库。
- `usage/` 保存请求记录、积分变动与统计快照；删除后历史统计会丢失。
- `catalog/` 保存模型目录缓存。
- 建议定期备份 `accounts/` 与 `usage/`。局域网模式必须设置 API Key。

## 测试

```bash
python tests/run_all.py
python tests/_test_doc_style.py
```

测试共有 182 个套件：165 个 Python 套件 + 17 个 JavaScript 套件。JavaScript 套件需要 Node.js 22 或更新版本。

提交前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)，文档写作遵守 [project-rule.md](project-rule.md)。本项目使用 MIT 许可证，详见 [LICENSE](LICENSE)。

## 来源与贡献者

本项目基于 [ardeyouxipianyi/workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub) 开发

看板视觉参考 [ithtelab/workbuddy-manager](https://github.com/ithtelab/workbuddy-manager) 使用原生 h5/js 重实现

部分交互参考 [linux-do/cdk](https://github.com/linux-do/cdk) 实现

完整的贡献者名单见 [GitHub 贡献者图表](https://github.com/flower-elf/NextTokenCenter/graphs/contributors)。

## 更新日志

见 [GitHub Releases](https://github.com/flower-elf/NextTokenCenter/releases)

## 免责声明

1. 本项目是非官方的自托管网关，仅供技术研究和学习，以及在私有环境中使用个人合法授权的账号进行测试。
2. 本项目不提供任何账号和额度。请遵守上游服务条款，仅限个人和家庭使用，禁止恶意多并发和违规滥用。
