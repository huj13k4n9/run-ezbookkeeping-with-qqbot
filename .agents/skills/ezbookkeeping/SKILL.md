---
name: ezbookkeeping
description: Use the ezBookkeeping API Tools script to record new transactions, query transactions, retrieve account information, categories, tags, and exchange rates, modify a single transaction, and compute spending statistics in the self hosted personal finance application ezBookkeeping.
---

# ezBookkeeping API Tools（复刻增强版）

本 skill 是 ezBookkeeping 官方 API Tools 的**增强复刻版**：命令名、参数名、
请求形状与官方一致，另加了三条本项目需要的命令（`query` / `modify` / `stats`）。

实现是 Python（只依赖标准库，不需要 `jq`），由同目录的 `ebktools.sh` 拉起。

> 实现文件：`scripts/ebktools.py`（CLI）、`scripts/stats.py`（统计聚合）、
> `scripts/ranges.py`（自然月等时间范围）。
> 命令表在 `ebktools.py` 顶部的 `COMMANDS`；加命令只需加一行。

## Usage

```bash
sh scripts/ebktools.sh list                       # 列出所有命令
sh scripts/ebktools.sh help <command>             # 看某个命令的参数
sh scripts/ebktools.sh [--tz-offset 480] <command> [--参数 值 ...]
```

`--tz-name` / `--tz-offset` 可以写在命令名前面或后面（两种位置都接受）。
**需要时区的命令**（`transactions-add` / `transactions-list` /
`transactions-list-all` / `query` / `modify` / `stats`）少给时区会直接报错，
不会等到服务端回一句含糊的失败。

## 命令一览

### 官方命令（与上游一致）

| 命令 | 方法 | 说明 |
| --- | --- | --- |
| `tokens-list` | GET | 当前用户的会话/令牌 |
| `accounts-list` | GET | 账户（含 `subAccounts` 子账户） |
| `accounts-add` | POST | 新建账户 |
| `transaction-categories-list` | GET | 分类（按 type 分组，子分类在 `subCategories`） |
| `transaction-categories-add` | POST | 新建分类 |
| `transaction-tags-list` | GET | 标签 |
| `transaction-tags-add` | POST | 新建标签 |
| `transactions-list` | GET | 分页查交易（游标 `max_time` 是 `timeSequenceId`，**不是 unix 秒**） |
| `transactions-list-all` | GET | 按 `start_time` / `end_time`（unix 秒）全量查 |
| `transactions-add` | POST | 记一笔 |
| `exchangerates-latest` | GET | 最新汇率 |
| `server-version` | GET | 服务端版本 |

### 增强命令

```bash
# 筛选查账：自然月 / 金额区间 / 标签 / 多账户
sh scripts/ebktools.sh --tz-offset 480 query --range this-month --type 3 --amount-filter gt:10000
sh scripts/ebktools.sh --tz-offset 480 query --range 2026-09 --keyword 打车 --count 20

# 统计报表：收支 + 分类/账户/说明词排行（转账单独算，不计入收支）
sh scripts/ebktools.sh --tz-offset 480 stats --range this-month

# 改单笔：只改给到的字段，其余从原交易继承
sh scripts/ebktools.sh --tz-offset 480 modify --id <交易ID> --comment "改成晚饭"
sh scripts/ebktools.sh --tz-offset 480 modify --id <交易ID> --comment "改成晚饭" --dry-run
```

* `--range`：`today` / `yesterday` / `this-week` / `last-week` / `this-month` /
  `last-month` / `this-year` / `2026-09`。**能用它就别自己算 unix 秒。**
* `--amount-filter`：`gt:` / `lt:` / `eq:` / `ne:` / `bt:min:max`，单位**分**。
* `--tag-filter`：`0:id1,id2`（0=含全部、1=含任意、2=不含全部、3=不含任意）。
* `modify` 是**整笔替换**语义：工具会先把原交易拉回来、只覆盖你给的字段，
  并丢掉值为 `null` 的可选字段。改账前先 `--dry-run` 看一眼。
* `stats` 里分类/账户是 **ID**，名字由调用方自己翻译。

## 三个容易踩的点

1. **金额与余额都是整数分**：`sourceAmount: 3000` = 30.00 元；
   `balance: 928712` = 9287.12 元。工具在输出里已经换算并标注单位。
2. **账户与分类都要传 ID**，不是名字；分类要用**二级分类** ID。
3. **`transactions-list` 的 `--min_time` / `--max_time` 是游标**（`timeSequenceId`），
   传 unix 秒不会报错、只会返回空表。按时间查用 `transactions-list-all`
   的 `--start_time` / `--end_time`，或者直接用 `query`。

## Troubleshooting

If the script reports that the environment variable `EBKTOOL_SERVER_BASEURL` or
`EBKTOOL_TOKEN` is not set, define them as system environment variables, or create a
`.env` file in the user home directory containing these two variables.

| Variable | Required | Description |
| --- | --- | --- |
| `EBKTOOL_SERVER_BASEURL` | Required | ezBookkeeping server base URL (e.g., `http://localhost:8080`) |
| `EBKTOOL_TOKEN` | Required | ezBookkeeping API token |

## 来源与许可

命令名、参数名、请求形状与错误语义参照上游 **ezBookkeeping** 的
[官方 API Tools 脚本](https://github.com/mayswind/ezbookkeeping)（`skills/ezbookkeeping/`，
作者 MaysWind，MIT 许可，见同目录 `LICENSE`）。

本目录下的实现**已由本项目重写为 Python 增强版**，不再与上游文件同步；
升级上游时请对照 `ebktools.py` 里的 `COMMANDS` 表核对参数是否变化，
并同步 `agent/AGENTS.md` 里的调用模板。

## Reference

ezBookkeeping: [https://ezbookkeeping.mayswind.net](https://ezbookkeeping.mayswind.net)
