# QQ 机器人接入层（run-bookkeeping）

用官方 QQ 机器人 OpenAPI v2 写的接入层，包含 **API 鉴权** 与 **WebSocket 事件订阅** 的完整逻辑，
可直接用来验证「机器人能不能连上、能不能收发消息」，后续记账 Skill 直接挂事件处理器即可。

参考文档：
- 获取访问凭证 <https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html>
- API 调用指南 <https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/api-call-guide.html>
- WebSocket 方式 <https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/websocket.html>
- opcode <https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/opcode.html>

## 目录

```
qqbot/
  config.py     配置（环境变量 / .env）
  auth.py       access_token 获取 + 缓存 + 到期自动刷新
  api.py        OpenAPI 客户端（自动注入 Authorization、token 失效自动重试）
  intents.py    Intents 位标记（事件订阅）
  ws.py         网关状态机：Hello/Identify/Heartbeat/Resume/重连
  media.py      附件解析与下载
  refindex.py   引用索引（对标官方 JsonlRefIndexStore）
  quote.py      引用解析（两级合并 + 来源标记）
  bot.py        高层封装：@bot.on("事件") + bot.reply() + event.quote
  errors.py     异常定义
  agent.py      pi agent 调用层（会话 ID / 去重 / prompt 组装 / 子进程）
scripts/
  qqbot_test.py 命令行测试工具（鉴权 / 网关 / 订阅 / 发消息）
  run_bot.py    常驻服务入口（systemd 拉起）
agent/
  AGENTS.md     交给 pi 的约束（分流、记账规则、回复格式、禁止泄露）
tests/
  test_gateway_local.py   网关状态机离线自测（假网关，不需要真实机器人）
  test_event_media.py     附件解析与下载自测（含真实抓包回归）
  test_refindex_quote.py  引用索引与引用解析自测
  test_agent.py           agent 层自测（会话/去重/prompt/子进程/调度）
```

## 一、鉴权逻辑

```
POST https://api.bot.qq.com/app/getAppAccessToken
Content-Type: application/json
{"appId": "...", "clientSecret": "..."}

成功 -> {"access_token": "...", "expires_in": "7200"}
失败 -> {"code": 100016, "message": "invalid appid or secret"}   # HTTP 仍是 200！
```

实现要点（`qqbot/auth.py`）：

| 点 | 处理方式 |
| --- | --- |
| 判断成败 | **看响应体 `code`，不看 HTTP 状态码**（文档明确说明失败时 HTTP 仍为 200） |
| 缓存 | 进程内缓存 token，有效期内不重复请求（重复请求返回同一个 token） |
| 刷新 | 距过期 `token_refresh_margin`（默认 60s）内自动重新获取 |
| 失效重试 | 业务接口返回 401 或 `err_code ∈ {11241,11242,11243,11244}` 时，强制刷新 token 后重试一次 |
| 调用鉴权 | 所有业务接口统一 `Authorization: QQBot {access_token}` |

## 二、WebSocket 订阅逻辑

`qqbot/ws.py` 实现的状态机：

```
create_connection(wss://api.bot.qq.com/websocket/)
        │
        ├─ op 10 Hello  ──► 读取 heartbeat_interval
        │
        ├─ 有 session ──► op 6 Resume  {token, session_id, seq}
        └─ 无 session ──► op 2 Identify {token, intents, shard, properties}
                │
                ├─ READY   (op 0 / t=READY)   -> 保存 session_id、user，回调 on_ready
                ├─ RESUMED (op 0 / t=RESUMED) -> 回调 on_resumed
                ├─ op 1 心跳（周期 = heartbeat_interval，d = 最新 seq）
                ├─ op 11 Heartbeat ACK -> 清空未确认计数
                ├─ op 0 Dispatch(t/d)  -> 回调 on_event（业务事件）
                ├─ op 7 Reconnect       -> 重连（保留 session，走 Resume）
                └─ op 9 Invalid Session -> d=true 走 Resume；d=false 清空会话重新 Identify
```

token 格式：`QQBot {access_token}`（注意不是旧版的 `Bot {appid}.{token}`）。

关闭码处理（官方「WebSocket 错误码」表）：

| 关闭码 | 含义 | 行为 |
| --- | --- | --- |
| 4001 / 4002 / 4010~4014 | 协议或权限问题 | **抛 `FatalWebSocketError`，停止重连**（intents 无权限通常是 4013/4014） |
| 4914 / 4915 | 机器人下架 / 封禁 | 停止重连，联系官方 |
| 4006 / 4007 | session 无效 / seq 错误 | 清空会话，重新 Identify |
| 4009 | 会话过期 | 保留会话，走 Resume |
| 其它 / 网络中断 | — | 指数退避重连（1s → 60s），优先 Resume |

另外做了两件防「假死」的事：

1. 心跳连续 2 次没收到 ACK → 判定连接失效，主动断开重连；
2. `seq` 在每条 Dispatch 后更新，断线时用它 Resume，网关会补发漏掉的事件。

## 三、配置

```bash
cp .env.example .env
# 填入 AppID / AppSecret（QQ 开放平台 -> 开发设置）
```

| 变量 | 说明 |
| --- | --- |
| `QQ_BOT_APP_ID` / `QQ_BOT_CLIENT_SECRET` | 机器人凭证 |
| `QQ_BOT_SANDBOX=1` | 使用沙箱环境 |
| `QQ_BOT_INTENTS` | 事件位图，默认 `33554432` = `GROUP_AND_C2C_EVENT(1<<25)`，**不含公域频道事件** |
| `QQ_BOT_SHARD_ID` / `QQ_BOT_SHARD_TOTAL` | 分片，不分片保持 `0` / `1` |
| `QQ_BOT_LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` |

> 默认值故意把公域频道事件 `PUBLIC_GUILD_MESSAGES(1<<30)` 排除了（它是基础权限，能订但不必要）。
> 重点：**平台没有「只订阅单聊」的 intent 位** —— `GROUP_AND_C2C_EVENT(1<<25)` 同时承载
> `C2C_MESSAGE_CREATE` 和 `GROUP_AT_MESSAGE_CREATE`。所以单聊机器人也得订这一位，
> 群聊事件会一并推送过来，不注册对应处理器即可。
>
> 另外，若订阅了没有权限的事件（如未申请就订 `INTERACTION`），网关会以 4013/4014 关闭连接。

## 四、测试步骤

> 项目已带 `.venv`（Python 3.12），依赖已安装。激活后直接用 `python` 即可：
>
> ```bat
> .venv\Scripts\activate
> ```
>
> 未激活时用 `.venv\Scripts\python.exe` 代替下文的 `python`。

```bash
pip install -r requirements.txt   # 仅首次 / 换环境时需要
cp .env.example .env              # 填入 AppID / AppSecret

# 1. 只验证鉴权
python scripts/qqbot_test.py token

# 2. 验证鉴权 + 网关接入点 + 机器人身份
python scripts/qqbot_test.py gateway

# 3. 验证单聊订阅（用手机 QQ 私聊机器人发消息）
#    每条消息都会详细打印：message_type、文本、附件、图片、语音（ASR）、引用内容
#    默认会自动回复，等于同时验证「事件订阅 + 被动回复」
python scripts/qqbot_test.py listen
python scripts/qqbot_test.py listen --no-reply --duration 60   # 只收不回，60s 后退出
python scripts/qqbot_test.py listen --raw                     # 额外打印完整原始 JSON
python scripts/qqbot_test.py listen --download-dir data/inbox  # 附件落盘并打印路径

# 4. 验证主动发送（openid 从第 3 步日志里拿）
python scripts/qqbot_test.py send-c2c <user_openid> "记账机器人测试"
```

排错：`--debug` 打开详细日志。

`listen` 的打印示例：

```
------------------------------------------------------------------------
[事件] C2C_MESSAGE_CREATE   seq=11   event_id=...
  message_type : 0 (纯文本)
  msg_id       : ROBOT1.0_xxxx   (被动回复用)
  msg_idx      : REFIDX_img==
  发送者        : 小明   openid=U1
  时间          : 2026-07-21T10:05:00+08:00
  文本内容      : ' '
  附件 (1):
      [0] content_type=image/jpeg  filename=photo.jpg  size=250.0 KB
          width/height: 1920x1080
          url         : https://multimedia.nt.qq.com.cn/download?appid=..&rkey=..
  分类统计      : 图片=1  语音=0  其它文件=0  引用图片=0

[事件] C2C_MESSAGE_CREATE   seq=101   event_id=...
  message_type : 0 (纯文本)
  文本内容      : '记 30 午饭'
  附件 (1):
      [0] content_type=voice  filename=voice.silk  size=4.5 KB
          url         : https://...
          voice_wav   : https://.../voice.wav
          asr_text    : '记三十块午饭'
  分类统计      : 图片=0  语音=1  其它文件=0  引用图片=0
  语音内容      :
      asr_text    : '记三十块午饭'
      voice_wav   : https://.../voice.wav
```

> 默认只有注册了处理器的消息会打印。代码里额外注册了 `"*"` 处理器，
> 所以没预料到的事件（如群聊事件）也会原样打出来，方便排查订阅范围。

离线自测网关状态机（不需要真实机器人）：

```bash
python tests/test_gateway_local.py     # 26 项断言，覆盖 Identify/Resume/心跳/致命关闭码
```

## 五、记账 Skill 怎么接

`qqbot/bot.py` 已经把连接、重连、鉴权、事件分发都处理好了，
记账逻辑只需要注册处理器 —— 示例见 `scripts/qqbot_test.py` 的 `listen` 分支：

```python
from qqbot import QQBot, Event

bot = QQBot()  # 读 .env，默认 intents = 33554432（单聊/群聊事件）

@bot.on("C2C_MESSAGE_CREATE")          # 单聊消息
def on_c2c(event: Event):
    reply = handle_bookkeeping(event.content, user_id=event.user_openid)
    bot.reply(event, reply)

@bot.on("FRIEND_ADD")                 # 用户添加机器人，可发欢迎语
def on_friend_add(event: Event):
    bot.send_c2c(event.user_openid, "我是记账机器人，发「记 30 午饭」开始记账")

bot.run()
```

`qqbot.C2C_EVENTS` 列出了全部单聊相关事件名，可作为处理器注册的参考。

注意事项：

- **被动回复必须带 `msg_id`**（即事件的 `d.id`），单聊 60 分钟内有效、最多回 4 次。
  同一 `msg_id` 下 `msg_seq` 必须不同，否则报 `40054005`。
- 超过窗口期就只能发主动消息，受主动消息频控限制（单关系维度 20/qpm，每天最多 1000 条）。
- 每条消息可能被重复推送，业务侧建议用 `msg_id` 去重后再记账。
- 单聊的 `user_openid` 是**每个 AppID 独立的**，不能跨机器人通用，存库时以它为主键即可。

## 六、接收图片 / 语音

机器人的回复仍然只有文本，但**接收侧**已能解析附件并落盘（`qqbot/media.py`）。

### 事件字段

`C2C_MESSAGE_CREATE` / `GROUP_AT_MESSAGE_CREATE` 的附件结构：

| 字段 | 说明 |
| --- | --- |
| `content_type` | `image/jpeg` `image/png` `image/gif` `image/webp` `video/mp4` `voice`(语音) `file`(群文件) |
| `url` | 下载地址，带 `rkey` 等签名参数 |
| `filename` `size` `width` `height` | 文件名 / 大小 / 图片尺寸 |
| `voice_wav_url` | 仅语音：SILK 转换后的 WAV |
| `asr_refer_text` | 仅语音：**ASR 识别文本** |

### `Event` 上新增的属性

```python
event.attachments        # 当前消息附件（list[dict]）
event.images             # 当前消息的图片
event.voice              # 语音附件，无则 None
event.asr_text           # 语音 ASR 文本（语音记账可直接用）
event.files              # 非图片非语音的其余附件
event.message_type       # 0=文本 3=ARK卡片 101=并行 102=聊天记录 103=引用消息
event.is_quoted          # message_type == 103
event.is_text_only       # 无附件且无引用附件

event.msg_elements       # 引用消息里被引用的内容元素
event.quoted_attachments  # 被引用消息的附件（实测为空，见下）
event.quoted_images      # 被引用消息的图片（实测为空）
event.quoted_text        # 被引用消息的文本
event.all_images         # 本消息 + 被引用消息的全部图片

event.parallel_message   # parallel_message（引用场景描述被引用内容）
event.parallel_nodes     # parallel_message.msg_nodes
event.quoted_summary     # 被引用内容摘要，如 '[图片]'
event.quoted_kinds       # {'image'} / {'video'} ...（按占位符/附件推断）
event.quoted_is_image    # 被引用的是否为图片

event.scene_ext          # {"msg_idx":..., "ref_msg_idx":..., "auth_token":...}
event.msg_idx / ref_msg_idx / auth_token
```

### 落盘

```python
@bot.on("C2C_MESSAGE_CREATE")
def on_c2c(event: Event):
    if event.images and not event.content.strip():
        # 单独一张图：只记录，不回复
        paths = bot.download_event_attachments(event, "data/images", only="images", max_bytes=20 * 1024 * 1024)
        db.save_pending_image(event.user_openid, paths[0], event.msg_id, event.msg_idx)
        return                      # 不回复

    if event.content.strip():
        image = db.take_pending_image(event.user_openid)   # 取最近一张未消费的图
        bot.reply(event, do_bookkeeping(event.content, image))
```

> **重要**：附件 `url` 带 `rkey` 签名参数，**有时效**。所以「只做事件记录」必须
> **收到即下载落盘**，不能只存 URL —— 否则等用户发文本时图可能已经取不到了。

### 引用图片（实测：可以拿到，见第七章）

引用消息的处理机制在**第七章**，这里只放结论：

* **能拿到**：`msg_elements[0].attachments` 会带回被引用图片的 URL。
  实测两次里成功一次，`source=both`。
* **失败模式**：引用了**机器人自己发的消息**时必然拿不到
  （机器人发出的消息不入站，永远不在本地索引里）。
* `msg_elements[0].attachments` 与本地 ref-index 会**合并**使用：
  本地有落盘文件（`local_path`），`msg_elements` 有带签名的原始 URL。

图片事件本身的字段（不变）：

```jsonc
{ "content": "", "message_type": 0,
  "attachments": [{ "content_type": "image/jpeg", "url": "https://...&rkey=..",
                    "content": "" /* 未文档化字段 */ }] }
```

### 自测

```bash
python tests/test_event_media.py   # 62 项断言：图片/引用/语音解析 + 真实下载 + 实测抓包回归
```

## 七、引用消息处理（ref-index）

对标官方 Node SDK `@tencent-connect/qqbot-nodejs` 的 `quoteRef` 中间件与
`JsonlRefIndexStore`，并做了改进。

### 机制

```
用户发图 / 发文本
   └─ 归档：按 msg_idx（回退 message_id）写入 ref-index
           附件摘要里带 local_path（落盘后的本地文件）

用户引用某条消息发文本（message_type=103）
   └─ 解析：ref_key = msg_elements[0].msg_idx（官方规则）或 ext 的 ref_msg_idx
            ┌ store 命中 ────────┐
            └ msg_elements 有内容 ┘ → 合并，标出来源
```

**只处理引用消息**：没有 `ref_msg_idx` 的消息直接返回 `None`，
不会用「最近一张图」之类的时间窗口去猜，纯文本消息永远不会被误配。

### 官方规则里的一条暗坑

`message_scene.ext` 和 `msg_elements` 都可能带引用索引，官方 SDK 明确规定：

> `message_type == 103` 时，`msg_elements[0].msg_idx` **优先于**
> `message_scene.ext` 里的 `ref_msg_idx` —— 元素级索引更权威。

`Event.ref_msg_idx` 已按这个规则实现（`qqbot.refindex.parse_ref_indices`）。

### 解析来源（`event.quote.source`）

| source | 含义 | 拿到什么 |
| --- | --- | --- |
| `both` | store 命中且 msg_elements 有内容 | 文本 + 附件本地路径（最全） |
| `store` | 只有 store 命中 | 文本 + 附件本地路径 |
| `msg_elements` | 只有事件自带内容 | 文本 + 附件 URL（无本地路径） |
| `none` | **两边都没有** | 只有平台给的 `hint`（如 `[图片]`） |

### 用法

```python
@bot.on("C2C_MESSAGE_CREATE")
def on_c2c(event: Event):
    quote = event.quote                     # 非引用消息时为 None

    if quote is None:
        # 普通消息
        ...
        return

    if not quote.resolved:
        # 是引用消息，但内容回查不到（source == "none"）
        bot.reply(event, f"抱歉，引用的内容我没能取到（{quote.hint or '未知类型'}）")
        return

    print(quote.text)                       # '[image: photo.jpg]'
    print(quote.local_paths)                # ['/abs/path/data/media/photo.jpg']
    bot.reply(event, f"收到引用：{quote.text}")
```

### 配置

| 变量 | 说明 |
| --- | --- |
| `QQ_BOT_REF_INDEX_PATH` | 索引文件（JSONL），默认 `data/ref-index.jsonl` |
| `QQ_BOT_REF_INDEX_TTL_DAYS` | 条目有效期，默认 7 天（对齐官方） |
| `QQ_BOT_REF_INDEX_MAX_ENTRIES` | 最大条数，默认 50000（对齐官方） |
| `QQ_BOT_AUTO_DOWNLOAD_DIR` | 附件落盘目录；**留空则引用回查拿不到图片本体** |
| `QQ_BOT_AUTO_DOWNLOAD_KINDS` | 落盘类型，默认 `image,voice` |
| `QQ_BOT_AUTO_DOWNLOAD_MAX_MB` | 单附件大小上限，默认 20MB |

> **必须落盘的原因**：附件 URL 带 `rkey` 签名参数，有时间限制。
> 收到消息时不下，等用户引用时基本就 403 了。

### REFIDX 的结构（重要，别靠“像不像”判断）

实测三次样本，结构非常稳定：

```
REFIDX_ + 22 字符（消息相关） + 106 字符（会话级常量） = 135 字符

run1 图片  cTK6518fnUR7Qx+Z9T8qca │ XpYGtMWU/sb/Gyjx…PKH
run1 引用  a5EjxGcDTrjdxl5/FqS+OK │ XpYGtMWU/sb/Gyjx…PKH
run2 图片  BsxqpIE3rSaKEDkl+WMVM6 │ XpYGtMWU/sb/Gyjx…PKH
                                   └─ 三次完全相同（跨 33 分钟、跨两张不同图片）
```

**那 106 字符尾巴是会话级常量，不是消息指纹。** 任意两条 REFIDX 天然有
78% 字符相同，所以**绝不能**用「字符串很像」推断是不是同一条消息 ——
只能做精确相等比较。

### 实测结论：机器人「被动回复过」的消息，引用会失效

三次实测、四个数据点，完全相关：

| 图片 | 机器人是否被动回复过 | 引用时 ref vs 图片 msg_idx | 结果 |
| --- | --- | --- | --- |
| run1 23:12 | ✅ 回复了 | 不同 | `none` |
| run2 23:45 | ❌ 没回复 | 相同（11s 后） | `both` |
| run3 23:51 | ✅ 回复了（`--reply-images`） | 不同（8s 后） | `none` |
| run2 那张图，在 run3 里被引用 | ❌ 没回复 | 相同（隔 6 分 19 秒） | `both` |

三条推论：

1. **REFIDX 与时间无关** —— run2 的图在 6 分 19 秒后引用，REFIDX 仍然完全一致
   （这条推翻了「REFIDX 按时间桶轮换」的猜想）。
2. **机器人被动回复会让原消息的 REFIDX 重新生成** —— 我们收到的事件里那个
   `msg_idx` 作废，用户引用时给的是新 REFIDX，本地缓存必然查不到。
3. `msg_elements[0].message_type == 103` 只是**当前消息类型的回显**，
   不代表「被引用元素是引用消息」（命中的那个数据点它同样是 103）。

### 因此：不要对图片消息做被动回复

```
x  发图 -> 机器人回复 -> 引用图 -> source=none
√  发图 -> 机器人不回复 -> 引用图 -> source=both
```

这不是 UX 偏好，而是**引用功能可用的前提**。记账流程不受影响：
用户发图不回复 → 引用图 + 发文本 → 这时回复的是**文本**消息，
图片本身从未被回复过，引用有效。

测试脚本的 `--reply-images` 会打印警告 —— 它会让后续引用失效。

### 其他会导致 `source=none` 的情况

* 引用了**机器人自己发的消息** —— 机器人发出的消息不作为入站事件回来，
  永远不会被归档。
* 引用的消息已被 TTL（默认 7 天）淘汰，或超出 `max_entries` 被挤出。
* 引用发生在机器人启动之前（本地索引里还没有）。

### 诊断

```bash
python scripts/qqbot_test.py listen --raw
```

会打印每次引用的 `ref 前缀` / `ref 会话尾` / `source` / `归档时间`，
以及 `msg_elements[0].message_type`。

miss 时日志会列出最近 5 条归档记录，并额外做**疑似候选检测**：

```
WARNING [qqbot.quote]   疑似 REFIDX 已变: key=REFIDX_9vPNNRvw68WgzM6cYf6cV…  age=8s
        （类型匹配 [图片]；常见原因是该消息被机器人被动回复过，QQ 会重新生成 REFIDX）
```

候选只在 `ResolvedQuote.candidates` 里作为**提示**提供，
`text` / `attachments` / `local_paths` 保持为空 —— **不参与自动解析**，
避免把不相关的媒体消息混淆进来。要不要把它提升成自动匹配，由业务侧决定。

### 自测

```bash
python tests/test_refindex_quote.py   # 102 项：索引读写/回放/TTL/淘汰/compact + 两级解析 + 实测回归
```


## 八、常驻服务：接 pi agent 记账

装配方案与决策依据见第八章开头的说明。这里只讲怎么跑。

### 数据流

```
QQ 单聊消息
   │
   ├─ 归档 + 引用解析 + 附件落盘          （qqbot，见第三~七章）
   │
   ├─ 纯图片消息 ──► 只归档，**不启 agent、不回复**
   │                  （图片不被回复 → REFIDX 不变 → 之后引用它必能解析到）
   │
   └─ 有文本的消息（含引用）──► 线程池 ──► pi --print
                                            ├─ 读 agent/AGENTS.md 的约束
                                            ├─ 调 ebktools.sh → ezBookkeeping
                                            └─ 输出一段纯文本
                                        ──► bot.reply(清洗+截断后)
```

**agent 必须跑在后台线程**：网关事件回调在接收循环里，阻塞超过 2 个心跳周期
（约 90s）会被判定为僵尸连接而重连。

### 工具脚本：官方 ebktools.sh

执行层用 ezBookkeeping 官方自带的 `ebktools.sh`（REST API），不使用 MCP。
它已放在 **`.agents/skills/ezbookkeeping/`**（pi 会自动发现这个 skill 目录）。

**`transactions-add` 的真实入参**（从 `ebktools.sh` 内嵌的 `API_CONFIGS` JSON 读出来的，不是猜的）：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `--type` | 整数 | **1**=余额修改 **2**=收入 **3**=支出 **4**=转账 |
| `--categoryId` | 字符串 | 分类 **ID**（不是名字） |
| `--time` | 整数 | **unix 秒** |
| `--utcOffset` | 整数 | 时区偏移**分钟** |
| `--sourceAccountId` | 字符串 | 账户 **ID** |
| `--sourceAmount` | 整数 | 金额，**单位是分**：`1234` = `12.34` |
| `--destinationAccountId` | 字符串 | 转账时必填 |
| `--destinationAmount` | 整数 | 转账时必填 |
| `--tagIds` | 逗号分隔 | 标签 ID |
| `--comment` | 字符串 | 备注 |

三个容易致命的点，已完整写进 `agent/AGENTS.md`：

1. **金额单位是分** —— 用户说 30 元必须传 `3000`，传 `30` 会被记成 0.30 元。
2. **账户和分类要传 ID** —— 必须先 `accounts-list` / `transaction-categories-list` 取 `id`。
3. **`--type` 是整数不是字符串**。

其他确认到的事实：

* **没有 `--dry-run`** → 改用「先查 ID + 金额换算明示 + 金额确认阈值」三重防错。
* 全局选项 `--tz-name` / `--tz-offset` / `--raw-response` **必须写在命令名之前**
  （脚本的参数循环遇到第一个非选项就当作命令名）。
* `transaction-categories-list` 的返回**按 type 分组**（1=收入 2=支出 3=转账），
  二级分类在 `subCategories` 里。
* `RequiresTimezone` 的命令会带 `X-Timezone-Name` / `X-Timezone-Offset` 头。

因为 `--time` 要 unix 秒而消息里是 RFC3339，bot 直接把结果算好注入 prompt，不让模型换算：

```
[时间] unix=1790178684 utcOffset=480
[工具] /opt/qq-bookkeeping/.agents/skills/ezbookkeeping/scripts/ebktools.sh
```

完整命令集：`tokens-list` / `accounts-list` / `accounts-add` /
`transaction-categories-list` / `transaction-categories-add` /
`transaction-tags-list` / `transaction-tags-add` / `transactions-list` /
`transactions-list-all` / `transactions-add` / `exchangerates-latest` /
`server-version`。

### 为什么纯图片不启 agent

实测结论（第七章）：机器人**被动回复过**的消息，QQ 会重新生成它的 REFIDX。
所以「图片来了就 OCR 并回复」会让这张图之后引用不到。
改成「图片静默归档 → 用户引用它并说明用途 → 才启 agent」，
引用机制就成了可靠的主路径，OCR 也在这一轮顺带完成。

### 运行

```bash
# 依赖见 requirements.txt；另需本机装好 pi，并 clone 官方 ebktools
cp .env.example .env          # 填 AppID / Secret
python scripts/run_bot.py
```

启动时会打印一份自检摘要，并**主动检查** agent 环境：
`QQ_BOT_AGENT_CWD` 是否存在、里面有没有 `AGENTS.md`、`ebktools.sh` 路径对不对。
配错会直接打成 `[警告]`，不用等到发消息才发现。

### 配置

全部走 `.env`，完整清单见 `.env.example`。几个关键的：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `QQ_BOT_AGENT_ENABLED` | `1` | 关闭后只归档，不处理 |
| `QQ_BOT_AGENT_CWD` | `agent` | **必须指向含 AGENTS.md 的目录**；支持绝对路径 |
| `QQ_BOT_EBKTOOLS_PATH` | `.agents/skills/…/ebktools.sh` | 注入 prompt 的 `[工具]` 行 |
| `QQ_BOT_AGENT_MODEL` | 空 | 多模态模型填这里（OCR 靠它） |
| `QQ_BOT_AGENT_TIMEOUT` | `120` | 秒 |
| `QQ_BOT_AGENT_MAX_CONCURRENCY` | `2` | 同时跑几个 agent |
| `QQ_BOT_SESSION_ROTATION` | `day` | `day`/`week`/`none` |
| `QQ_BOT_CONFIRM_AMOUNT_THRESHOLD` | `1000` | ≥ 此金额先确认再入账；`0`=关闭 |
| `QQ_BOT_DEDUP_TTL` | `600` | msg_id 去重窗口（秒） |
| `QQ_BOT_REPLY_MAX_CHARS` | `500` | 回复硬截断 |
| `QQ_BOT_ALLOWED_OPENIDS` | 空 | 空 = 不限制 |

`EBKTOOL_SERVER_BASEURL` / `EBKTOOL_TOKEN` 由 systemd `EnvironmentFile` 注入，
**只在进程环境里**，不进 agent 上下文（AGENTS.md 也禁止回显）。

### 约束写在 agent/AGENTS.md

pi 从 **cwd** 向上发现 `AGENTS.md`，所以 `QQ_BOT_AGENT_CWD` 决定它读哪一份。
放在 `agent/`（而不是仓库根目录）有两个原因：

1. 避免你自己在这个项目里跑 pi 时被记账约束污染；
2. 顺带把 agent 的文件视野限制在一个近乎空的目录里。

`AGENTS.md` 只写**静态规则**；数值型策略（金额确认阈值）由 bot 注入 prompt，
所以改配置不用改提示词：

```
[当前时间] 2026-09-23T23:51:24+08:00
[时间] unix=1790178684 utcOffset=480      ← bot 直接算好，模型不用换算
[工具] /opt/qq-bookkeeping/.agents/skills/ezbookkeeping/scripts/ebktools.sh
[策略] 确认阈值 1000
[用户] 593CBC92…
[消息] 午饭
[引用] [image: 9932F906…jpg]
[附件] /opt/qq-bookkeeping/data/media/9932F906…jpg
```

### 自测

```bash
python tests/test_agent.py   # 108 项：会话轮换/去重/prompt/子进程/超时/并发锁/输出清洗
```

## 九、Docker 部署

### 为什么 bot 和 agent 在同一个容器

`qqbot` 是用 `subprocess` 直接拉起 `pi` 的，两者本来就共享 `agent/` 和 `data/media/`。
拆成两个容器需要引入 IPC（挂 `docker.sock` 是安全隐患，或者给 pi 写个 RPC 客户端），
而收益很小 —— **那个 agent 容器里同样得有 `EBKTOOL_TOKEN` 才能记账**，
等于在两个已经互信的东西之间划线。

真正要防的是「agent 执行任意 bash」，而那是它的功能。同容器下已经做了：
非 root（uid 10001）、`no-new-privileges`、`--tools bash,read`、
`agent` 与 `.agents` 只读挂载、资源与 pid 上限。

> 如果哪天真需要强隔离，正确做法是 pi 官方的 **Gondolin 扩展**
> （把 `bash`/`read` 路由进本地 micro-VM），而不是把 pi 拆到另一个容器 ——
> 那样代码一行都不用改，隔离边界还更实。

### 为什么不用 Alpine

pi 官方容器化文档（`docs/containerization.md`）用的是 `node:24-bookworm-slim` + **ripgrep**。
Alpine 有两个坑：musl libc 对 Node 生态的兼容风险，以及 `jq`/`ripgrep`/`tzdata` 都得另装。
省下的体积不值这个风险。

### 快速开始

```bash
cp .env.example .env
#  填 QQ_BOT_APP_ID / QQ_BOT_CLIENT_SECRET
#  填 EBKTOOL_SERVER_BASEURL / EBKTOOL_TOKEN
#  填一个模型 key（ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY …）
#  设 QQ_BOT_AGENT_MODEL（多模态模型，看图入账要用）

mkdir -p data && sudo chown -R 10001:10001 data   # 见下方「权限坑」
docker compose up -d --build
docker compose logs -f
```

启动日志会先打一份自检摘要，并检查 `agent/AGENTS.md` 与 `ebktools.sh` 是否就位。

### 卷

| 宿主机 | 容器内 | 说明 |
| --- | --- | --- |
| `./data` | `/app/data` | 图片落盘 + 引用索引。**必须持久化** —— 引用回查依赖已落盘的图片 |
| `pi-home`（named volume） | `/home/qqbot/.pi` | pi 的会话与凭证，重启不丢会话 |
| `$AGENT_DIR` | `/app/agent`（只读） | `AGENTS.md` 约束文件 |
| `$AGENTS_SKILLS_DIR` | `/app/.agents`（只读） | 官方 `ebktools.sh` |

后两个目录不在本仓库下时（比如 agent 目录另有安排），在 `.env` 里设
`AGENT_DIR` / `AGENTS_SKILLS_DIR`。容器内的路径是固定的
（`QQ_BOT_AGENT_CWD` / `QQ_BOT_EBKTOOLS_PATH` 由 compose 写死为 `/app/...`）。

### 权限坑（第一次必踩）

绑定挂载时**宿主机目录的属主说了算**。容器里跑的是 **uid 10001**，
而 `docker compose` 会自动创建 `./data` 并归 `root:root`，于是附件落盘会失败。

```bash
mkdir -p data && sudo chown -R 10001:10001 data
```

（named volume `pi-home` 没这个问题 —— 镜像里已经建好 `/home/qqbot/.pi` 并 chown 给 10001，
Docker 会把这份属主信息带进新卷。）

### 在容器里手动验证

```bash
# 官方脚本
docker compose run --rm --entrypoint sh qqbot -c \
  'sh /app/.agents/skills/ezbookkeeping/scripts/ebktools.sh accounts-list'

# 手动跑一次 agent
docker compose run --rm --entrypoint pi qqbot --print --tools bash,read -- "记 30 午饭"

# 跑测试（tests/ 已经打进镜像）
docker compose run --rm --entrypoint python qqbot tests/test_agent.py
```

### 如果 ezBookkeeping 不在同一台机器

`EBKTOOL_SERVER_BASEURL` 的取值：

| ezBookkeeping 在哪 | 填什么 |
| --- | --- |
| 宿主机 | `http://host.docker.internal:8080`（compose 已加 `extra_hosts`） |
| 另一个容器（同一 compose 网络） | `http://<服务名>:8080` |
| 远端 | `https://你的域名` |

### 安全提示

* **`docker compose config` 会把 `.env` 的内容明文打印出来**（包括 AppSecret 和 Token）。
  排查问题时别把它贴到 issue / 聊天里。
* `.env` 已在 `.gitignore` 和 `.dockerignore` 里，不会进 git，也不会烤进镜像。
* compose 里默认**没有**开启只读根文件系统（`read_only: true`），
  因为没在真实环境实测过。想加固就把那三行注释打开；
  若启动报 `Read-only file system`，说明 pi 或 Python 需要写别的路径，把 tmpfs 补上即可。
