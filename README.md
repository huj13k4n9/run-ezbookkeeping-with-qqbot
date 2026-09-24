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

mkdir -p data pi-config && sudo chown -R 10001:10001 data pi-config
#  ↑ 容器以 uid 10001 运行，绑定挂载的目录必须先改属主
docker compose up -d --build
docker compose logs -f
```

启动时会打印一份自检摘要，并检查 `agent/AGENTS.md` 与 `ebktools.sh` 是否就位 ——
配错会直接打 `[警告]`，不用等到发消息才发现。

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
cp pi-config/langfuse.json.example pi-config/langfuse.json
# 填 publicKey / secretKey，并按 key 的区域改 baseUrl
chmod 600 pi-config/langfuse.json      # 里面有 secretKey
docker compose up -d
```

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

扩展由容器入口脚本幂等安装（`PI_EXTENSIONS`），装在挂载出来的 `pi-config/` 里。

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

## 自定义 LLM 端点（base URL）

pi **没有** `OPENAI_BASE_URL` 这类通用环境变量（只有 Azure 有 `AZURE_OPENAI_BASE_URL`），
自定义端点必须走 `<agent-dir>/models.json`。这个部署里 agent dir 是挂载出来的
`pi-config/`，所以直接改宿主机上的文件就行，不用重建镜像：

```bash
cp pi-config/models.json.example pi-config/models.json
```

```json
{
  "providers": {
    "my-proxy": {
      "baseUrl": "https://your-llm-proxy.example.com/v1",
      "api": "openai-completions",
      "apiKey": "$MY_PROXY_API_KEY",
      "models": [
        { "id": "gpt-4o", "name": "GPT-4o (via proxy)", "input": ["text", "image"] }
      ]
    }
  }
}
```

`.env` 里给它配上 key 和默认模型：

```ini
MY_PROXY_API_KEY=sk-...
QQ_BOT_AGENT_MODEL=my-proxy/gpt-4o
```

要点：

* `apiKey` 支持 `$NAME` / `${NAME}` 环境变量插值，也可以写 `!command` 从命令取 ——
  所以密钥不用写进 `models.json`。
* **自定义的 key 变量名要进白名单**，否则不会到 pi 进程：
  `QQ_BOT_AGENT_PASSTHROUGH_ENV=PATH,HOME,TZ,EBKTOOL_*,MY_PROXY_*`
  （注意这样会覆盖内置默认值，`EBKTOOL_*` 千万别漏）。
* **看图入账必须声明 `"input": ["text", "image"]`**，否则模型不会被标记为支持图片，
  OCR 流程会失效。
* `api` 取值：`openai-completions`、`openai-responses`、`anthropic-messages`、
  `google-generative-ai`、`mistral-conversations`、`azure-openai-responses`、
  `bedrock-converse-stream`、`openai-codex-responses`、`pi-messages`。
* 改完 `models.json` **不用重启容器**（pi 每次运行都重新读）。

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

pi-config/                pi 的配置目录（挂载出来）
  models.json.example       自定义 LLM 端点模板
  langfuse.json.example     Langfuse 凭证模板
  models.json               你自己的（gitignore）
  langfuse.json             你自己的，含 secretKey（gitignore）
  settings.json             pi install 写的扩展声明（gitignore）

tests/                    309 项离线测试
docs/QQBOT.md             完整技术文档
docker/entrypoint.sh      容器入口：幂等安装 pi 扩展
Dockerfile
docker-compose.yml
```

---

## 测试

全部离线（共 309 项），不需要真实机器人、不需要 ezBookkeeping、不需要装 pi。

```bash
python tests/test_gateway_local.py    #  26  假网关驱动状态机
python tests/test_event_media.py      #  62  附件解析/真实下载 + 真实抓包回归
python tests/test_refindex_quote.py   # 102  引用索引/解析/实测相关性
python tests/test_agent.py            # 119  会话/去重/prompt/子进程/并发/env 透传/清洗
```

几个「固化了实测事实」的用例值得一提 —— 哪天平台行为变了，测试会立刻失败：

* `test_reply_breaks_refidx_correlation` —— 机器人回复导致 REFIDX 失效（4/4 相关）
* `test_real_capture_regression` / `test_real_capture_success` —— 真实抓包的正反两面
* `test_refidx_structure` —— REFIDX 的 22+106 结构，防止有人靠「字符串像」做匹配

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
