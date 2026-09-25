# run-ezbookkeeping-with-qqbot

在 QQ 里私聊记账：发一句「记 30 午饭」，或者引用一张收据截图说「午饭」，
机器人把这条消息交给一个 [pi](https://github.com/earendil-works/pi) agent，
由它识别意图、调用 [ezBookkeeping](https://github.com/mayswind/ezbookkeeping)
的 API 写入你自部署的账本，再把结果回给你。

* **不需要** ezBookkeeping 暴露到公网
* **不需要** 额外的 NLU / 规则引擎 —— 理解自然语言交给 agent
* **不需要** 自己写 ezBookkeeping 的 API 封装 —— 直接用官方自带的脚本

```
QQ 单聊消息
   │
   ├─ 归档 + 引用解析 + 图片落盘              ← qqbot（Python，常驻）
   │
   ├─ 纯图片消息 ──► 只归档，不启 agent、不回复
   │
   └─ 有文本的消息（含引用）──► 线程池 ──► pi --print
                                            ├─ 读 agent/AGENTS.md 里的约束
                                            ├─ bash → ebktools.sh → ezBookkeeping
                                            └─ 输出一段纯文本
                                        ──► 清洗 + 截断后回给 QQ
```

---

## 几个不显然的设计决策

这个仓库比较有价值的部分在这几条 —— 它们都是实测或读源码得出的，不是拍脑袋。

### 1. 机器人**回复过**的消息，用户就引用不到了

QQ 的引用消息靠 `REFIDX_xxx` 索引回查原文。我们实测发现（4/4 相关）：

* `REFIDX` = 22 字符消息相关前缀 + 106 字符**会话级常量**
  （跨消息、跨 33 分钟都相同，所以**不能用「字符串很像」判断是否同一条**）
* 机器人一旦对某条消息做**被动回复**，QQ 会**重新生成该消息的 REFIDX**；
  用户之后引用它 → 索引对不上 → 解析失败

所以设计成：**图片消息只归档，不回复、不启 agent**。等用户引用那张图并给出说明时
才处理 —— 引用因此成了可靠的主路径，OCR 也在那一轮顺带完成。

> 细节与实测数据见 [docs/QQBOT.md](docs/QQBOT.md) 第七章。

### 2. bot 和 agent 放**同一个容器**

`qqbot` 是用 `subprocess` 直接拉起 `pi` 的，两者共享 `agent/` 与 `data/media/`。
拆成两个容器需要引入 IPC（挂 `docker.sock` 是安全隐患，或者给 pi 写 RPC 客户端），
而收益很小 —— **那个 agent 容器里同样得有 `EBKTOOL_TOKEN` 才能记账**，
等于在两个已经互信的东西之间划线。

真正要防的是「agent 执行任意 bash」，而那是它的功能。同容器下已经做了：
非 root、`no-new-privileges`、`--tools bash,read`、约束目录只读挂载、资源与 pid 上限。

### 3. 会话按用户分配，但**按天轮换**

用 pi 自己的 session（`--session-id qq-<openid哈希>-<日期>`），会话状态不需要自己维护。
但**不长期复用同一个 session**：pi 的 compaction 触发后仍会保留约 2 万 token 历史，
而且它的摘要格式（Goal / Progress / Key Decisions）会把「支付宝」压成「某个账户」——
而记账接口要求名字/ID 逐字匹配。

对应的缓解措施写进了 `agent/AGENTS.md`：**每次入账前必须重新查一次账户和分类列表**，
不许复用记忆里的名字。多两次工具调用，换掉一整类错账。

### 4. 金额单位是「分」

读 `ebktools.sh` 内嵌的 `API_CONFIGS` 才发现的：`transactions-add` 的
`--sourceAmount` 单位是**分**，`1234` 表示 `12.34` 元。用户说「30 元」必须传 `3000`。

同理还有：账户和分类要传 **ID** 不是名字、`--type` 是**整数**、**没有 `--dry-run`**。
这些全部写死在 `agent/AGENTS.md` 里。完整参数表见
[docs/QQBOT.md](docs/QQBOT.md)。

---

## 快速开始

### Docker（推荐）

```bash
git clone https://github.com/huj13k4n9/run-ezbookkeeping-with-qqbot.git
cd run-ezbookkeeping-with-qqbot

cp .env.example .env
#  ├─ QQ_BOT_APP_ID / QQ_BOT_CLIENT_SECRET     （QQ 开放平台 → 开发设置）
#  ├─ EBKTOOL_SERVER_BASEURL / EBKTOOL_TOKEN   （ezBookkeeping → 设置 → 令牌）
#  └─ 一个模型 key + QQ_BOT_AGENT_MODEL         （看图入账需要多模态模型）

sh scripts/init_config.sh                     # 生成 pi-config/ 下的两个配置文件
mkdir -p data && sudo chown -R 10001:10001 data
#  ↑ 容器以 uid 10001 运行，绑定挂载的 data/ 必须先改属主
docker compose up -d --build
docker compose logs -f
```

启动时会打印一份自检摘要，并检查 `agent/AGENTS.md` 与 `ebktools.sh` 是否就位 ——
配错会直接打 `[警告]`，不用等到发消息才发现。

想先干跑一遍（**不连 QQ、不花 token**）：

```bash
python scripts/run_bot.py --check     # 只自检，通过返回 0，有问题返回 1
python scripts/run_bot.py --help
```

### 本地开发

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env

python tests/test_agent.py          # 不需要网络、不需要真的装 pi
python scripts/qqbot_test.py token   # 验证 QQ 鉴权链路
python scripts/run_bot.py
```

---

## 可观测性：Langfuse

官方有现成的 pi 扩展 [`@langfuse/pi-observability-plugin`](https://github.com/langfuse/pi-observability-plugin)，
不用自己写。每次「一条 QQ 消息 → 一次 agent 运行」会成为一条 trace，
里面的 model 调用、token（含 cache / reasoning 拆分）、成本、工具调用都在。

**配置走文件，不走环境变量** —— 和 pi 的 `models.json` 一样，放进 pi 的配置目录：

```bash
# scripts/init_config.sh 已经生成过一个空配置，直接改它
vi pi-config/langfuse.json      # 填 publicKey / secretKey，按 key 区域改 baseUrl
chmod 600 pi-config/langfuse.json
docker compose restart qqbot    # 改配置只需重启，不用重建
```

完整的字段说明见 `pi-config/langfuse.json.example`。

`baseUrl` 的区域必须和 key 对应（省略则默认 EU）：

| 区域 | baseUrl |
| --- | --- |
| EU | `https://cloud.langfuse.com` |
| US | `https://us.cloud.langfuse.com` |
| JP | `https://jp.cloud.langfuse.com` |
| HIPAA | `https://hipaa.cloud.langfuse.com` |

可选字段：`userId`、`environment`、`release`（用来在 Langfuse 里筛选）。

**为什么不走环境变量**：密钥要跟着配置文件走，而不是散在 `.env` 里再层层透传。
插件本身两种都支持（环境变量优先），但这里刻意只留文件这条路径 ——
qqbot 的白名单里**只保留 `PI_LANGFUSE_*`**（`PI_LANGFUSE_DEBUG` 调试开关、
`PI_LANGFUSE_MAX_CHARS` 截断长度），`LANGFUSE_*` 一律不透传。

**为什么逐文件挂载而不是挂整个目录**：

* `langfuse.json` 含 secretKey —— 按文件挂载，**密钥不会留在镜像层**
* 改完只需 `docker compose restart`，不用重建镜像
* `settings.json` / `auth.json` / 预装的扩展包留在容器与镜像里，不污染仓库

**唯一的坑**：宿主机上这两个文件必须**先存在**。否则 Docker 会建一个同名
*目录*挂进来，pi 解析 JSON 失败。`scripts/init_config.sh` 负责生成它们，
容器入口脚本也会在启动时检测并报出明确错误。

扩展由容器入口脚本幂等安装（`PI_EXTENSIONS`），装在镜像内的 `pi-config/` 里。

**想关掉追踪**：把 `pi-config/langfuse.json` 删掉或改名即可。
（插件还有个只认环境变量的 kill switch `LANGFUSE_TRACING_ENABLED=false`，
但既然配置已经走文件，用文件开关就够了。）

排查「没有 trace」：

```bash
# 跑一次真实记账，同时打开插件调试日志（走 stderr）
docker compose run --rm -e PI_LANGFUSE_DEBUG=true   --entrypoint pi qqbot --print --tools bash,read -- "记 1 元 连通性测试"
```

调试日志会说明它有没有读到 `langfuse.json`、有没有成功上传。

> 官方文档：<https://langfuse.com/integrations/developer-tools/pi-agent>

## 模型凭证与端点（全在 pi-config 里）

**推荐：端点、密钥、模型名都写在 `pi-config/models.json` 一个文件里。**

```jsonc
// pi-config/models.json
{
  "defaultModel": "anthropic/claude-sonnet-4-5",
  "providers": {
    "anthropic": {
      "baseUrl": "https://your-relay.example.com",
      "apiKey": "sk-...",
    },
  },
}
```

```bash
chmod 600 pi-config/models.json # 含密钥
docker compose restart qqbot    # 只需重启，不用重建
```

`models.json` 是 gitignore 的，密钥不会进仓库。`.env` 里对应的
`QQ_BOT_AGENT_MODEL` / `*_API_KEY` **全部留空即可**。

### 三个字段分别怎么来的

| 字段 | pi 认不认 | 说明 |
|---|---|---|
| `baseUrl` | ✅ 原生 | provider 级覆盖，见下节 |
| `apiKey` | ✅ 原生 | 支持字面值、`"$ENV_VAR"` 插值、`"!command"` 取 |
| `defaultModel` | ❌ **本项目约定** | pi 的默认模型在 `settings.json` 里，不在 `models.json` |

第三个是本项目加的：pi 的 `models.json` 只管端点，模型*选择*归
`<agent-dir>/settings.json` 的 `defaultModel` 管 —— 于是「apiKey 在 .env、
baseUrl 在 models.json、模型名在 settings.json」要三处填。qqbot 读取
`models.json` 里 Pi 会忽略的 `defaultModel` 键，作为 `--model` 传给 pi，
这样三件事就收拢到一个文件。pi 对未知顶层键不作限制（已实测）。

优先级：`QQ_BOT_AGENT_MODEL`（环境变量）> `models.json: defaultModel` > 不传，
让 pi 自己决定。想临时换个模型、不想改文件时用环境变量。

### 想用环境变量放密钥也行

pi 直接读 provider 的标准环境变量（`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` /
`GEMINI_API_KEY` / …）。但**填进 `.env` 并不等于 pi 拿得到**：bot 拉起 pi 时会按
白名单过滤环境变量（防止 QQ 的 `client_secret` 泄给 agent），内置白名单见
`qqbot/config.py` 的 `DEFAULT_AGENT_PASSTHROUGH_ENV`。

| 类别 | 是否透传 | 原因 |
|---|---|---|
| 常见 provider 的 `*_API_KEY` | ✅ | 不给 pi 就没法调模型 |
| `EBKTOOL_SERVER_BASEURL` / `EBKTOOL_TOKEN` | ✅ | `ebktools.sh` 必需 |
| `PI_*`（配置 / 会话目录 / `PI_LANGFUSE_*`） | ✅ | pi 自身与外挂 |
| `QQ_BOT_*`（含 `client_secret`） | ❌ | 与 agent 无关，绝不外泄 |

在 `models.json` 里写 `"apiKey": "$MY_RELAY_KEY"` 时，这个变量名也要在名单内，
否则报错是 `No API key found`（而不是「变量不存在」），不太好认。自定义名字就
整套覆盖白名单（**填了就会替换默认值**，`EBKTOOL_*` 千万别漏）：

```ini
QQ_BOT_AGENT_PASSTHROUGH_ENV=PATH,HOME,TZ,PI_CODING_AGENT_DIR,PI_CODING_AGENT_SESSION_DIR,EBKTOOL_SERVER_BASEURL,EBKTOOL_TOKEN,MISTRAL_*
```

### 自定义 LLM 端点（base URL）

pi **没有** `OPENAI_BASE_URL` 这类通用环境变量（只有 Azure 有 `AZURE_OPENAI_BASE_URL`），
自定义端点必须走 `<agent-dir>/models.json`，也就是 `pi-config/models.json`。

#### 推荐写法：只覆盖内置 provider 的 baseUrl

最省事的方式 —— 只写 `baseUrl`，**模型 id、上下文长度、是否支持图片全部沿用
pi 内置目录**，不用一个个重写：

```jsonc
{
  "providers": {
    "anthropic": { "baseUrl": "https://your-relay.example.com" },
  },
}
```

配套 `.env`：

```ini
QQ_BOT_AGENT_MODEL=anthropic/claude-sonnet-4-5
ANTHROPIC_API_KEY=sk-...        # 中转站给的 key
```

这一点是读 pi 源码确认的（`dist/core/model-config.js` 的 `applyModelsJson`）：

```js
baseUrl: config.oauth === "radius" ? model.baseUrl : (config.baseUrl ?? model.baseUrl)
```

即该 provider 下**所有内置模型**都改走这个地址，而**协议不变** ——
anthropic 仍然是 `POST <baseUrl>/v1/messages` + `x-api-key` 头。
实测（本地起假服务器拦截）：

```
baseUrl = http://127.0.0.1:9999/relay/anthropic
→ POST /relay/anthropic/v1/messages?beta=true   x-api-key=sk-...
```

路径前缀会被正确拼接；末尾别自己加 `/v1`。覆盖是**按 provider 隔离**的，
改了 `anthropic` 不会影响 `openai`（已实测）。

### 另一种写法：整套自定义 provider

pi 不认识的端点（Ollama / LM Studio / vLLM，或协议不标准的中转）用这种：

```jsonc
{
  "providers": {
    "my-proxy": {
      "baseUrl": "https://your-llm-proxy.example.com/v1",
      "api": "openai-completions",
      "apiKey": "$MY_PROXY_API_KEY",
      "models": [
        { "id": "gpt-4o", "input": ["text", "image"] },
      ],
    },
  },
}
```

```ini
MY_PROXY_API_KEY=sk-...
QQ_BOT_AGENT_MODEL=my-proxy/gpt-4o
```

### 几个坑（都是实测出来的）

* **只支持 `//` 行注释和尾随逗号，不支持 `/* */` 块注释。**
  pi 用的是 `stripJsonComments`，写块注释会让 JSON 解析失败 ——
  而 pi 对解析失败的处理是**静默忽略整个 models.json**，一个错都不报，
  表现就是「配置没生效」。容器入口脚本会用相同规则提前验一遍并报出警告。
* **`apiKey` 里的 `$VAR` 必须真的到得了 pi 进程**：既要进容器（写在 `.env` 里），
  又要**在 agent 白名单内**。内置白名单已含常见 provider 的 `*_API_KEY`，
  自定义变量名要加进 `QQ_BOT_AGENT_PASSTHROUGH_ENV`（这套会**覆盖**默认值，
  `EBKTOOL_*` 和模型凭证都别漏）。漏了的话报错是 `No API key found`。
* **`apiKey` 支持 `$NAME` / `${NAME}` 插值，也可以写 `!command`** 从命令/密钥库里取，
  所以密钥不一定要写进 `models.json`。
* **看图入账必须声明 `"input": ["text", "image"]`**（只有自定义 provider 需要；
  覆盖内置 provider 时 pi 目录里已经有了）。否则模型不会被标记为支持图片，
  OCR 流程会失效。
* **provider 条目不能是空对象** `{}` —— pi 会直接报错，至少要有个 `baseUrl`。
* `api` 取值：`openai-completions`、`openai-responses`、`anthropic-messages`、
  `google-generative-ai`、`mistral-conversations`、`azure-openai-responses`、
  `bedrock-converse-stream`、`openai-codex-responses`、`pi-messages`。
* 改完 `models.json` / `langfuse.json` 只需 `docker compose restart qqbot`。

完整模板见 `pi-config/models.json.example`。

## 目录结构

```
qqbot/                   QQ 官方 API v2 接入层
  auth.py                  access_token 获取/缓存/刷新
  api.py                   OpenAPI 客户端（token 失效自动重试）
  ws.py                    网关状态机（Identify/心跳/Resume/关闭码分流）
  media.py                 附件解析与落盘
  refindex.py              引用索引（对标官方 Node SDK 的 JsonlRefIndexStore）
  quote.py                 引用解析（两级合并 + 来源标记）
  agent.py                 pi 调用层（会话轮换/去重/prompt 组装/子进程）
  bot.py                   事件分发 + 后台线程调度 + 输出清洗
  config.py                全部配置项

agent/
  AGENTS.md               交给 pi 的约束（分流规则、记账规则、回复格式、禁止泄露）

.agents/skills/ezbookkeeping/   官方 ebktools.sh（vendored，见同目录 SOURCE.md）

scripts/
  run_bot.py              常驻服务入口
  qqbot_test.py           命令行测试工具（鉴权/网关/订阅/发消息）

pi-config/                pi 的配置目录（只挂两个 json 文件）
  models.json.example       自定义 LLM 端点模板
  langfuse.json.example     Langfuse 凭证模板
  models.json               你自己的（gitignore，由 init_config.sh 生成）
  langfuse.json             你自己的，含 secretKey（gitignore，同上）

tests/                    357 项离线测试
docs/QQBOT.md             完整技术文档
docker/
  entrypoint.sh           容器入口：配置检查 + 兜底安装 pi 扩展
  check_pi_json.js        用 pi 的规则预验 models.json / langfuse.json
scripts/init_config.sh    生成 pi-config/ 下的两个配置文件
Dockerfile
docker-compose.yml
```

---

## 测试

全部离线（共 357 项），不需要真实机器人、不需要 ezBookkeeping、不需要装 pi。

```bash
python tests/test_gateway_local.py    #  26  假网关驱动状态机
python tests/test_event_media.py      #  62  附件解析/真实下载 + 真实抓包回归
python tests/test_refindex_quote.py   # 102  引用索引/解析/实测相关性
python tests/test_agent.py            # 148  会话/去重/prompt/子进程/并发/env 透传/models.json/清洗
python tests/test_run_bot_cli.py      #  19  命令行入口（--check 不连网关、未知参数报错）
```

几个「固化了实测事实」的用例值得一提 —— 哪天平台行为变了，测试会立刻失败：

* `test_reply_breaks_refidx_correlation` —— 机器人回复导致 REFIDX 失效（4/4 相关）
* `test_real_capture_regression` / `test_real_capture_success` —— 真实抓包的正反两面
* `test_refidx_structure` —— REFIDX 的 22+106 结构，防止有人靠「字符串像」做匹配
* `test_cli / test_check_mode` —— 入口脚本曾经**没有参数解析**，敲错 `--check`
  会被静默忽略并把机器人真的拉起来连 QQ
* `test_models_json` 里的 `strip_json_comments` 用例 —— 与 pi 的 JS 实现逐例比对
  （注释/尾随逗号/字符串里的 `//`/转义引号），两边行为必须一致

---

## 状态

**已验证**

* QQ 鉴权、网关订阅、消息收发、引用解析、图片落盘 —— 真机跑通
* 全部业务逻辑 —— 298 项离线测试

**未验证**

* Docker 镜像未实际构建过（开发机 Docker daemon 没启动，只做了 compose 静态校验）
* `ebktools.sh` 与你的 ezBookkeeping 实例的连通性（阶段 0）
* pi 端到端记账（阶段 1）

---

## 第三方与致谢

| 组件 | 用途 | 许可 |
| --- | --- | --- |
| [ezBookkeeping](https://github.com/mayswind/ezbookkeeping) 的 `skills/ezbookkeeping/` | 记账执行层。**vendored** 在 `.agents/skills/ezbookkeeping/`，未做修改 | MIT，版权归 MaysWind —— 见 [LICENSE](.agents/skills/ezbookkeeping/LICENSE) 与 [SOURCE.md](.agents/skills/ezbookkeeping/SOURCE.md) |
| [pi](https://github.com/earendil-works/pi) (`@earendil-works/pi-coding-agent`) | 约束执行与工具调用 | 见上游 |
| [`@langfuse/pi-observability-plugin`](https://github.com/langfuse/pi-observability-plugin) | 可观测性。**不 vendored**，由容器入口脚本按需安装 | MIT |

本仓库的 QQ 协议实现参考了腾讯官方文档
（[bot.q.qq.com/wiki](https://bot.q.qq.com/wiki/)），
引用机制部分参考了官方 Node SDK `@tencent-connect/qqbot-nodejs` 的设计。

## 许可

本仓库自身尚未选择许可证。注意 vendored 的 `.agents/skills/ezbookkeeping/`
是独立的 MIT 作品，不受本仓库许可影响。
