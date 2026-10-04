"""ezBookkeeping API Tools（复刻增强版）。

本文件是 `ebktools.sh` 的唯一实现（shell 入口只负责找解释器并透传参数）。
覆盖两类命令：

1. **官方 ebktools 的全部 12 条命令**，名字、参数名、请求形状完全一致 ——
   所以 `AGENTS.md` 里那些调用模板不用改：
   `tokens-list` / `accounts-list` / `accounts-add` /
   `transaction-categories-list` / `transaction-categories-add` /
   `transaction-tags-list` / `transaction-tags-add` /
   `transactions-list` / `transactions-list-all` / `transactions-add` /
   `exchangerates-latest` / `server-version`
2. **本项目加的增强命令**：
   `query`（筛选查账：金额区间 / 标签 / 多账户 / 自然月）、
   `modify`（改单笔，只改给到的字段）、
   `stats`（统计报表）。
   命名注意：官方 `list` = **列出所有命令**，所以筛选查账叫 `query`。

为什么用 Python 而不是 bash + jq：官方脚本把 API 定义内嵌成一大段 JSON 交给 jq 拼参数、
每个命令再手写一遍 curl 分支。改成 Python 后命令表就是一张数据结构，加命令只加一行，
而且金额换算、null 处理、统计聚合这些都能写测试（见 `tests/test_ebktools_cli.py`）。

约定：
* 只依赖标准库（容器里不用装 jq / pip 包）。
* 金额与余额一律**整数分**（接口返回 928712 = 9287.12 元）。
* 输出是给 **agent** 看的紧凑文本；面向用户的措辞由 `agent/AGENTS.md` 约束。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ranges import RangeError, resolve_range  # noqa: E402
from stats import (  # noqa: E402
    TYPE_NAMES,
    cents_to_yuan,
    format_summary,
    summarize,
)

DEFAULT_TIMEOUT = 30.0
PAGE_SIZE = 50
DEFAULT_NEAR_DAYS = 30
MAX_PAGES = 40

# ---------------------------------------------------------------- 命令表
# 每条命令：路径 / 方法 / 必填 / 可选 / 类型 / 是否需要时区头。
# 参数名与官方脚本一致（下划线命名），CLI 上也直接写 --category_ids 这种形式。
S = "string"
I = "integer"
B = "boolean"
SA = "string_array"
GEO = "geo_location"

COMMANDS: dict[str, dict] = {
    "tokens-list": {
        "path": "tokens/list.json", "method": "GET", "tz": False,
        "required": [], "optional": [], "types": {},
    },
    "accounts-list": {
        "path": "accounts/list.json", "method": "GET", "tz": False,
        "required": [], "optional": [], "types": {},
    },
    "accounts-add": {
        "path": "accounts/add.json", "method": "POST", "tz": False,
        "required": ["name", "category", "type", "icon", "iconType", "color", "currency"],
        "optional": ["balance", "balanceTime", "comment", "creditCardStatementDate"],
        "types": {
            "name": S, "category": I, "type": I, "icon": S, "iconType": I,
            "color": S, "currency": S, "balance": S, "balanceTime": I,
            "comment": S, "creditCardStatementDate": I,
        },
    },
    "transaction-categories-list": {
        "path": "transaction/categories/list.json", "method": "GET", "tz": False,
        "required": [], "optional": [], "types": {},
    },
    "transaction-categories-add": {
        "path": "transaction/categories/add.json", "method": "POST", "tz": False,
        "required": ["name", "type", "icon", "iconType", "color"],
        "optional": ["parentId", "comment"],
        "types": {
            "name": S, "type": I, "parentId": S, "icon": S, "iconType": I,
            "color": S, "comment": S,
        },
    },
    "transaction-tags-list": {
        "path": "transaction/tags/list.json", "method": "GET", "tz": False,
        "required": [], "optional": [], "types": {},
    },
    "transaction-tags-add": {
        "path": "transaction/tags/add.json", "method": "POST", "tz": False,
        "required": ["name"], "optional": ["groupId"], "types": {"name": S, "groupId": S},
    },
    "transactions-list": {
        "path": "transactions/list.json", "method": "GET", "tz": True,
        "required": ["count"],
        "optional": [
            "type", "category_ids", "account_ids", "tag_filter", "amount_filter",
            "keyword", "must_have_pictures", "max_time", "min_time", "page",
            "with_count", "with_pictures", "trim_account", "trim_category", "trim_tag",
        ],
        "types": {
            "count": I, "type": I, "category_ids": S, "account_ids": S, "tag_filter": S,
            "amount_filter": S, "keyword": S, "must_have_pictures": B, "max_time": I,
            "min_time": I, "page": I, "with_count": B, "with_pictures": B,
            "trim_account": B, "trim_category": B, "trim_tag": B,
        },
    },
    "transactions-list-all": {
        "path": "transactions/list/all.json", "method": "GET", "tz": True,
        "required": [],
        "optional": [
            "type", "category_ids", "account_ids", "tag_filter", "amount_filter",
            "keyword", "must_have_pictures", "start_time", "end_time",
            "with_pictures", "trim_account", "trim_category", "trim_tag",
        ],
        "types": {
            "type": I, "category_ids": S, "account_ids": S, "tag_filter": S,
            "amount_filter": S, "keyword": S, "must_have_pictures": B,
            "start_time": I, "end_time": I, "with_pictures": B,
            "trim_account": B, "trim_category": B, "trim_tag": B,
        },
    },
    "transactions-add": {
        "path": "transactions/add.json", "method": "POST", "tz": True,
        "required": ["type", "categoryId", "time", "utcOffset", "sourceAccountId", "sourceAmount"],
        "optional": [
            "destinationAccountId", "destinationAmount", "hideAmount",
            "tagIds", "pictureIds", "comment", "geoLocation",
        ],
        "types": {
            "type": I, "categoryId": S, "time": I, "utcOffset": I,
            "sourceAccountId": S, "sourceAmount": I, "destinationAccountId": S,
            "destinationAmount": I, "hideAmount": B, "tagIds": SA,
            "pictureIds": SA, "comment": S, "geoLocation": GEO,
        },
    },
    "exchangerates-latest": {
        "path": "exchange_rates/latest.json", "method": "GET", "tz": False,
        "required": [], "optional": [], "types": {},
    },
    "server-version": {
        "path": "systems/version.json", "method": "GET", "tz": False,
        "required": [], "optional": [], "types": {},
    },
}

#: 本项目的增强命令（不是官方 API）
EXTRA_COMMANDS = {
    "query": "查账（+ 自然月 / 金额区间 / 标签 / 多账户 / 分类筛选，输出紧凑）",
    "modify": "修改单笔交易（只改给到的字段，其余从原交易继承；--dry-run 可预演）",
    "stats": "统计报表（收支 / 分类 / 账户 / 说明词排行）",
}

RANGE_HELP = "today/yesterday/this-week/last-week/this-month/last-month/this-year/2026-09"


class EbkError(Exception):
    """面向 agent 的可读错误。"""


# --------------------------------------------------------------------- HTTP
def _base_url() -> str:
    url = os.environ.get("EBKTOOL_SERVER_BASEURL", "").strip()
    if not url:
        raise EbkError("环境变量 EBKTOOL_SERVER_BASEURL 未设置")
    return url.rstrip("/")


def _token() -> str:
    token = os.environ.get("EBKTOOL_TOKEN", "").strip()
    if not token:
        raise EbkError("环境变量 EBKTOOL_TOKEN 未设置")
    return token


def call_api(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
    tz_name: str = "",
    tz_offset: int | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> object:
    """调一次 ezBookkeeping API，返回 ``result``；失败抛 ``EbkError``。"""
    url = f"{_base_url()}/api/v1/{path.lstrip('/')}"
    if params:
        # 布尔必须序列化成 true/false：urlencode 会把 Python 的 True 写成 "True"，
        # 服务端不认（不报错、直接当成没传）。
        normalised = {
            k: ("true" if v is True else "false" if v is False else v)
            for k, v in params.items()
            if v is not None
        }
        query = urllib.parse.urlencode(normalised)
        if query:
            url = f"{url}?{query}"

    headers = {
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/json",
        "User-Agent": "ebktools/2.0",
    }
    # 需要时区的端点：给 X-Timezone-Name 或 X-Timezone-Offset（前者优先，与官方一致）
    if tz_name:
        headers["X-Timezone-Name"] = tz_name
    elif tz_offset is not None:
        headers["X-Timezone-Offset"] = str(int(tz_offset))

    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        raise EbkError(_explain_http_error(exc.code, raw)) from None
    except urllib.error.URLError as exc:
        raise EbkError(f"连不上服务器：{exc.reason}") from None

    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        raise EbkError("服务器返回的不是 JSON（可能被反向代理拦了）") from None

    if isinstance(payload, dict) and payload.get("success") is False:
        code = payload.get("errorCode")
        message = payload.get("errorMessage") or "未知错误"
        raise EbkError(f"接口报错 errorCode={code} errorMessage={message}")
    return payload.get("result") if isinstance(payload, dict) else payload


def _explain_http_error(code: int, raw: str) -> str:
    detail = ""
    try:
        payload = json.loads(raw or "{}")
        if isinstance(payload, dict) and payload.get("errorMessage"):
            detail = f"：{payload['errorMessage']}（errorCode={payload.get('errorCode')}）"
    except json.JSONDecodeError:
        pass
    if code in (401, 403):
        return f"没有权限（HTTP {code}），token 可能过期或无效{detail}"
    return f"服务器返回 HTTP {code}{detail}"


def require_timezone(command: str, tz_name: str, tz_offset: int | None) -> None:
    """需要时区的端点少给时区时，提前说清楚（否则只有服务端一句 200008）。"""
    if not tz_name and tz_offset is None:
        raise EbkError(
            f"{command} 必须给时区：加 --tz-offset <分钟>（北京时间是 480）"
            "或 --tz-name Asia/Shanghai"
        )


# ------------------------------------------------------------- 参数解析/构造
def _coerce(name: str, raw: str, ptype: str):
    """按官方类型表把命令行字符串转成 JSON 值。"""
    if ptype == I:
        try:
            return int(raw)
        except ValueError:
            raise EbkError(f"参数 --{name} 需要整数，收到 {raw!r}") from None
    if ptype == B:
        return str(raw).strip().lower() in ("1", "true", "yes")
    if ptype == SA:
        return [v.strip() for v in str(raw).split(",") if v.strip()]
    if ptype == GEO:
        try:
            lon, lat = str(raw).split(",")
            return {"latitude": float(lat), "longitude": float(lon)}
        except ValueError:
            raise EbkError(f"参数 --{name} 需要 longitude,latitude 形式，收到 {raw!r}") from None
    return raw


def build_payload(spec: dict, given: dict) -> dict:
    """把 ``{参数名: 字符串}`` 变成请求体，并校验必填。"""
    payload = {name: _coerce(name, raw, spec["types"].get(name, S)) for name, raw in given.items()}
    missing = [k for k in spec["required"] if k not in payload]
    if missing:
        raise EbkError(
            "缺少必填参数：" + "、".join(f"--{m}" for m in missing)
            + "；用 `help <command>` 看完整参数"
        )
    return payload


def parse_flags(argv: list[str], known: list[str]) -> dict:
    """解析 ``--key value`` 形式，未知参数直接报错。

    故意不接受 ``--key=value``：官方脚本也只认空格分隔，保持一致，
    免得两种写法混用后出现「参数没生效」的诡异现象。
    """
    given: dict = {}
    index = 0
    while index < len(argv):
        token = argv[index]
        if not token.startswith("--"):
            raise EbkError(f"不认识的参数：{token!r}（参数要写成 --名字 值）")
        name = token[2:]
        if name not in known:
            raise EbkError(f"不认识参数 --{name}；用 `help <command>` 看可用参数")
        if index + 1 >= len(argv):
            raise EbkError(f"参数 --{name} 缺少值")
        given[name] = argv[index + 1]
        index += 2
    return given


# ------------------------------------------------------------ 输出渲染
def _fmt_item(item: dict) -> str:
    """一行一笔，字段给全 —— agent 要拿它去回复用户。"""
    ttype = int(item.get("type") or 0)
    when = int(item.get("time") or 0)
    when_text = time.strftime("%Y-%m-%d %H:%M", time.localtime(when)) if when else "?"
    parts = [
        f"id={item.get('id')}",
        f"type={ttype}({TYPE_NAMES.get(ttype, '?')})",
        f"time={when}({when_text})",
        f"category={item.get('categoryId') or (item.get('category') or {}).get('id')}",
        f"from={item.get('sourceAccountId') or (item.get('sourceAccount') or {}).get('id')}",
        f"amount={item.get('sourceAmount')}({cents_to_yuan(int(item.get('sourceAmount') or 0))}元)",
    ]
    if item.get("destinationAccountId"):
        parts.append(f"to={item['destinationAccountId']}")
        parts.append(
            f"destAmount={item.get('destinationAmount')}"
            f"({cents_to_yuan(int(item.get('destinationAmount') or 0))}元)"
        )
    if item.get("tagIds"):
        parts.append(f"tags={','.join(str(t) for t in item['tagIds'])}")
    comment = str(item.get("comment") or "").strip()
    if comment:
        parts.append(f"comment={comment}")
    return " ".join(parts)


def _walk_accounts(nodes: list, target: str) -> dict | None:
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if str(node.get("id")) == str(target):
            return node
        found = _walk_accounts(node.get("subAccounts") or [], target)
        if found:
            return found
    return None


def _balance_note(item: dict) -> str:
    """改完之后该账户的余额（用户发现记错的唯一线索）。"""
    account_id = item.get("sourceAccountId")
    if not account_id or isinstance(account_id, dict):
        return ""
    try:
        accounts = call_api("GET", "accounts/list.json")
    except EbkError:
        return ""
    if not isinstance(accounts, list):
        return ""
    account = _walk_accounts(accounts, account_id)
    if not account:
        return ""
    balance = account.get("balance")
    try:
        # 余额也是整数分（实测 928712 = 9287.12 元），不换算 agent 会当元报
        shown = f"{cents_to_yuan(int(balance))} 元"
    except (TypeError, ValueError):
        shown = f"{balance}（单位未知，不要直接当元报）"
    return f"[余额] 账户 {account.get('name') or account_id} = {shown}"


def render_result(command: str, result: object) -> str:
    """把接口返回渲染成紧凑文本。"""
    if command == "transactions-list-all":
        items = result if isinstance(result, list) else []
        lines = [f"[交易] 共 {len(items)} 笔（金额单位：分）"]
        lines.extend(_fmt_item(i) for i in items if isinstance(i, dict))
        return "\n".join(lines)

    if command == "transactions-list":
        page = result if isinstance(result, dict) else {}
        items = [i for i in (page.get("items") or []) if isinstance(i, dict)]
        lines = [
            f"[交易] 本页 {len(items)} 笔 / 共 {page.get('totalCount', '?')} 笔；"
            f"下一页游标 max_time={page.get('nextTimeSequenceId', '?')}（金额单位：分）"
        ]
        lines.extend(_fmt_item(i) for i in items)
        return "\n".join(lines)

    if command == "accounts-list":
        accounts = result if isinstance(result, list) else []
        lines = ["[账户] 余额单位：分（子账户缩进两级）"]

        def render_accounts(nodes: list, depth: int) -> None:
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                lines.append(
                    f"{'  ' * depth}id={node.get('id')} name={node.get('name')}"
                    f" category={node.get('category')} type={node.get('type')}"
                    f" currency={node.get('currency')} balance={node.get('balance')}"
                    f"({cents_to_yuan(int(node.get('balance') or 0))}元)"
                    + (f" comment={node.get('comment')}" if node.get("comment") else "")
                )
                render_accounts(node.get("subAccounts") or [], depth + 1)

        render_accounts(accounts, 0)
        return "\n".join(lines)

    if command == "transaction-categories-list":
        cats = result if isinstance(result, list) else []
        lines = ["[分类] id → 名字（子分类缩进；type: 1收入 2支出 3转账）"]

        def render_categories(nodes: list, depth: int) -> None:
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                lines.append(
                    f"{'  ' * depth}id={node.get('id')} name={node.get('name')}"
                    f" type={node.get('type')} parentId={node.get('parentId')}"
                )
                render_categories(node.get("subCategories") or [], depth + 1)

        render_categories(cats, 0)
        return "\n".join(lines)

    if command == "transaction-tags-list":
        tags = result if isinstance(result, list) else []
        lines = [f"[标签] 共 {len(tags)} 个"]
        lines.extend(
            f"id={t.get('id')} name={t.get('name')} groupId={t.get('groupId')}"
            for t in tags if isinstance(t, dict)
        )
        return "\n".join(lines)

    if command == "tokens-list":
        tokens = result if isinstance(result, list) else []
        lines = [f"[会话] 共 {len(tokens)} 条"]
        lines.extend(
            f"tokenId={t.get('tokenId')} tokenType={t.get('tokenType')}"
            f" current={t.get('isCurrent')} lastSeen={t.get('lastSeen')}"
            for t in tokens if isinstance(t, dict)
        )
        return "\n".join(lines)

    if command == "exchangerates-latest":
        rates = result if isinstance(result, list) else []
        lines = [f"[汇率] 共 {len(rates)} 条"]
        lines.extend(
            f"{r.get('from')}→{r.get('to')} rate={r.get('rate')} updated={r.get('updatedTime')}"
            for r in rates if isinstance(r, dict)
        )
        return "\n".join(lines)

    # 其余（POST 类、server-version）：紧凑 JSON，agent 自己挑字段
    return "[结果] " + json.dumps(result, ensure_ascii=False, separators=(",", ":"))


# ------------------------------------------------------------------ 交易查询
def fetch_transactions(
    *,
    start_time: int | None = None,
    end_time: int | None = None,
    ttype: int | None = None,
    category_ids: str = "",
    account_ids: str = "",
    tag_filter: str = "",
    amount_filter: str = "",
    keyword: str = "",
    tz_name: str = "",
    tz_offset: int | None = None,
    max_items: int | None = None,
) -> list[dict]:
    """给定期望时间就走 list-all（全量），否则分页往前翻。"""
    common = {
        "type": ttype,
        "category_ids": category_ids or None,
        "account_ids": account_ids or None,
        "tag_filter": tag_filter or None,
        "amount_filter": amount_filter or None,
        "keyword": keyword or None,
    }

    if start_time is not None or end_time is not None:
        params = dict(common)
        params["start_time"] = start_time
        params["end_time"] = end_time
        result = call_api(
            "GET", "transactions/list/all.json",
            params=params, tz_name=tz_name, tz_offset=tz_offset,
        )
        return _trim(result if isinstance(result, list) else [], max_items)

    collected: list[dict] = []
    max_time = 0  # 0 = 最新
    for _ in range(MAX_PAGES):
        params = dict(common)
        params.update({"count": PAGE_SIZE, "page": 1, "max_time": max_time, "with_count": "true"})
        result = call_api(
            "GET", "transactions/list.json",
            params=params, tz_name=tz_name, tz_offset=tz_offset,
        )
        if not isinstance(result, dict):
            break
        items = [i for i in (result.get("items") or []) if isinstance(i, dict)]
        collected.extend(items)
        if max_items is not None and max_items > 0 and len(collected) >= max_items:
            break
        next_cursor = result.get("nextTimeSequenceId")
        if not items or not next_cursor or int(next_cursor) == max_time:
            break
        max_time = int(next_cursor)
    return _trim(collected, max_items)


def _trim(items: list[dict], max_items: int | None) -> list[dict]:
    """``None`` 或 ``<=0`` 都表示**不限制**（``--count 0`` 的习惯含义是「不限」）。"""
    if max_items is not None and max_items > 0:
        return items[:max_items]
    return items


def find_transaction(
    tx_id: str,
    *,
    near_time: int | None = None,
    days: int = DEFAULT_NEAR_DAYS,
    tz_name: str = "",
    tz_offset: int | None = None,
) -> dict:
    """按 ID 找一笔交易（接口没有「按 ID 查」，只能开时间窗翻页找）。

    ``tz_name`` / ``tz_offset`` 必须一路传下去：这个服务端对交易类端点会校验时区，
    漏了就是 200008「client timezone offset is invalid」。
    """
    anchor = int(near_time) if near_time else int(time.time())
    span = max(int(days), 0) * 86400
    items = fetch_transactions(
        start_time=anchor - span, end_time=anchor + span,
        tz_name=tz_name, tz_offset=tz_offset, max_items=0,
    )
    for item in items:
        if str(item.get("id")) == str(tx_id):
            return item
    raise EbkError(
        f"在 {days} 天窗口内没找到交易 id={tx_id}；"
        "用 --near-time 给出这笔交易的大致时间（unix 秒）后重试"
    )


# ------------------------------------------------------------------ 增强命令
def cmd_query(args: argparse.Namespace) -> str:
    start, end, label = _resolve_window(args)
    items = fetch_transactions(
        start_time=start, end_time=end,
        ttype=args.type, category_ids=args.category_ids or "",
        account_ids=args.account_ids or "", tag_filter=args.tag_filter or "",
        amount_filter=args.amount_filter or "", keyword=args.keyword or "",
        tz_name=args.tz_name or "", tz_offset=args.tz_offset,
        max_items=args.count,
    )
    head = f"[交易] 共 {len(items)} 笔（金额单位：分）" + (f" 区间={label}" if label else "")
    lines = [head]
    lines.extend(_fmt_item(i) for i in items)
    return "\n".join(lines)


MODIFIABLE = (
    "type", "categoryId", "time", "utcOffset", "sourceAccountId", "sourceAmount",
    "destinationAccountId", "destinationAmount", "comment", "hideAmount", "tagIds",
)


def cmd_modify(args: argparse.Namespace) -> str:
    require_timezone("modify", args.tz_name or "", args.tz_offset)

    original = find_transaction(
        args.id, near_time=args.near_time, days=args.near_days,
        tz_name=args.tz_name or "", tz_offset=args.tz_offset,
    )
    if original.get("editable") is False:
        raise EbkError("这笔交易标记为不可编辑（editable=false），改不了")

    # 以原交易为底，再覆盖用户显式给出的字段 —— modify 是「整笔替换」，
    # 只传 comment 会把金额/分类清空（必填字段直接报错）。
    # 值为 None 的字段必须丢掉：原交易里 hideAmount / tagIds 常是 null，
    # 原样提交会被服务端当成非法输入（200000 incomplete or incorrect submission）。
    body: dict = {
        key: original[key]
        for key in MODIFIABLE
        if key in original and original[key] is not None
    }
    body["id"] = args.id

    given = {
        "categoryId": args.category_id, "time": args.time, "utcOffset": args.utc_offset,
        "sourceAccountId": args.source_account_id, "sourceAmount": args.source_amount,
        "destinationAccountId": args.destination_account_id,
        "destinationAmount": args.destination_amount, "comment": args.comment,
        "hideAmount": args.hide_amount, "tagIds": args.tag_ids,
    }
    changed = [k for k, v in given.items() if v is not None]
    if not changed:
        raise EbkError("没有给任何要修改的字段（至少给一个 --comment / --source_amount / ...）")
    for key, value in given.items():
        if value is None:
            continue
        body[key] = (
            [t.strip() for t in str(value).split(",") if t.strip()] if key == "tagIds" else value
        )

    missing = [
        k for k in ("type", "time", "utcOffset", "sourceAccountId", "sourceAmount")
        if body.get(k) is None
    ]
    if missing:
        raise EbkError(
            f"原交易缺少必填字段 {','.join(missing)}，不敢提交修改（先用 query 确认这笔交易）"
        )

    if args.dry_run:
        diffs = [
            f"{key}: {original.get(key)!r} → {body.get(key)!r}"
            for key in changed
            if original.get(key) != body.get(key)
        ]
        lines = [f"[预演] 未提交。id={args.id} 将改动：{','.join(changed)}"]
        lines.extend(f"  {d}" for d in diffs)
        if not diffs:
            lines.append("  （所有给定字段与原值相同，提交也不会改变任何东西）")
        return "\n".join(lines)

    result = call_api(
        "POST", "transactions/modify.json", body=body,
        tz_name=args.tz_name or "", tz_offset=args.tz_offset,
    )
    merged = (
        {**original, **{k: v for k, v in result.items() if v is not None}}
        if isinstance(result, dict) else original
    )
    lines = [f"[已修改] id={args.id} 改动字段={','.join(changed)}", _fmt_item(merged)]
    note = _balance_note(merged)
    if note:
        lines.append(note)
    return "\n".join(lines)


def cmd_stats(args: argparse.Namespace) -> str:
    require_timezone("stats", args.tz_name or "", args.tz_offset)
    start, end, label = _resolve_window(args)
    items = fetch_transactions(
        start_time=start, end_time=end,
        ttype=args.type, category_ids=args.category_ids or "",
        account_ids=args.account_ids or "", tag_filter=args.tag_filter or "",
        keyword=args.keyword or "", tz_name=args.tz_name or "",
        tz_offset=args.tz_offset, max_items=None,
    )
    summary = summarize(items, top=args.top)
    if args.json:
        return json.dumps({
            "count": summary.count, "income": summary.income, "expense": summary.expense,
            "transfer": summary.transfer, "balance": summary.balance, "net": summary.net,
            "by_category": summary.by_category, "by_account": summary.by_account,
            "by_keyword": summary.by_keyword,
            "first_time": summary.first_time, "last_time": summary.last_time,
        }, ensure_ascii=False, indent=2)
    return format_summary(summary, label=args.label or label or "")


def _resolve_window(args: argparse.Namespace) -> tuple[int | None, int | None, str]:
    """把 ``--range`` 解析成 (start, end, label)；显式时间戳优先。"""
    expr = getattr(args, "range", "") or ""
    start = getattr(args, "start_time", None)
    end = getattr(args, "end_time", None)
    label = getattr(args, "label", "") or ""
    if not expr:
        return start, end, label
    try:
        r_start, r_end, r_label = resolve_range(expr, tz_offset=getattr(args, "tz_offset", None))
    except RangeError as exc:
        raise EbkError(str(exc)) from None
    return (
        start if start is not None else r_start,
        end if end is not None else r_end,
        label or r_label,
    )


# ------------------------------------------------------------------ 命令分发
def run_official(command: str, args: argparse.Namespace, raw: list[str]) -> str:
    spec = COMMANDS[command]
    known = list(spec["required"]) + list(spec["optional"])
    payload = build_payload(spec, parse_flags(raw, known))
    if spec["tz"]:
        require_timezone(command, args.tz_name or "", args.tz_offset)

    if spec["method"] == "GET":
        result = call_api(
            "GET", spec["path"], params=payload,
            tz_name=args.tz_name or "", tz_offset=args.tz_offset,
        )
    else:
        result = call_api(
            "POST", spec["path"], body=payload,
            tz_name=args.tz_name or "", tz_offset=args.tz_offset,
        )
    return render_result(command, result)


def print_command_list() -> str:
    lines = ["Available API Commands:", ""]
    for name, spec in COMMANDS.items():
        lines.append(f"  {name:<30} {spec['method']:<5} {spec['path']}")
        if spec["required"]:
            lines.append(f"  {'':<30} 必填: " + " ".join(f"--{p}" for p in spec["required"]))
    lines += ["", "Extra Commands (本项目加的):", ""]
    for name, desc in EXTRA_COMMANDS.items():
        lines.append(f"  {name:<30} {desc}")
    lines += ["", "Use `help <command>` to see detailed information about a command."]
    return "\n".join(lines)


def print_command_help(command: str) -> str:
    if command in COMMANDS:
        spec = COMMANDS[command]
        lines = [
            f"{command}  [{spec['method']} {spec['path']}]",
            f"  需要时区头: {'是' if spec['tz'] else '否'}",
        ]
        for label, keys in (("必填参数", spec["required"]), ("可选参数", spec["optional"])):
            if keys:
                lines.append(f"  {label}:")
                lines.extend(f"    --{p}  ({spec['types'].get(p, S)})" for p in keys)
        lines.append(f"  用法: sh ebktools.sh [--tz-offset 480] {command} --参数 值 ...")
        if command == "transactions-add":
            lines.append(
                "  提示: 金额单位是分（30 元 = 3000）；分类与账户都传 ID；"
                "支出 type=3、收入 type=2、转账 type=4"
            )
        return "\n".join(lines)

    if command == "query":
        return (
            "query  查账（比 transactions-list 好用：支持自然月 + 金额区间，输出紧凑）\n"
            f"  --range <{RANGE_HELP}>\n"
            "  --start_time / --end_time  显式 unix 秒（与 --range 同时给时以显式为准）\n"
            "  --type 1余额修改/2收入/3支出/4转账\n"
            "  --category_ids / --account_ids  逗号分隔的 ID\n"
            "  --tag_filter 如 0:tag1,tag2（0含全部 1含任意 2不含全部 3不含任意）\n"
            "  --amount_filter 如 gt:10000 / lt:5000 / bt:1000:5000（单位：分）\n"
            "  --keyword 关键词\n"
            "  --count 最多返回几笔（默认 20，0=不限）\n"
            "  需要时区：--tz-offset 480"
        )
    if command == "modify":
        return (
            "modify  修改单笔交易（只改给到的字段，其余从原交易继承）\n"
            "  --id <交易ID>          必填；先用 query / transactions-list 查到\n"
            "  --near-time <unix>     这笔交易的大致时间（缩小查找范围）\n"
            "  --near-days <天数>     查找窗口半径，默认 30\n"
            "  --dry-run              只打印「改前 → 改后」，不提交（改账前先给用户确认）\n"
            "  可改字段: --category_id --time --utc_offset --source_account_id\n"
            "            --source_amount --destination_account_id --destination_amount\n"
            "            --comment --tag_ids\n"
            "  需要时区：--tz-offset 480"
        )
    if command == "stats":
        return (
            "stats  统计报表（转账单独算、不计入收支）\n"
            f"  --range <{RANGE_HELP}> 或 --start_time/--end_time\n"
            "  --type --category_ids --account_ids --tag_filter --keyword  同 query\n"
            "  --top 明细各取前几条（默认 10）\n"
            "  --json 输出 JSON（调试用）\n"
            "  需要时区：--tz-offset 480"
        )
    raise EbkError(f"没有这个命令：{command or '(空)'}；用 `list` 看全部命令")


# --------------------------------------------------------------------- 入口
def add_global(parser: argparse.ArgumentParser) -> None:
    """给子命令挂上全局选项（时区）。写在命令名前后都认。"""
    parser.add_argument("--tz-name", default="", help="IANA 时区名，如 Asia/Shanghai")
    parser.add_argument("--tz-offset", type=int, default=None, help="时区偏移（分钟），如 480")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ebktools.sh", description="ezBookkeeping API Tools（复刻增强版）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="列出所有支持的命令")

    p_help = sub.add_parser("help", help="看某个命令的参数说明")
    p_help.add_argument("topic", nargs="?", default="")

    for name in COMMANDS:
        # 官方命令的参数（`--count 5 --type 3` …）数量不固定、名字也动态，
        # 不交给 argparse —— 用 parse_known_args 把它们原样收回来自己解析。
        # 但 --tz-name / --tz-offset 要让 argparse 认得（否则会被当成未知参数）。
        p_official = sub.add_parser(
            name, help=f"{COMMANDS[name]['method']} {COMMANDS[name]['path']}",
        )
        add_global(p_official)

    def add_filters(p: argparse.ArgumentParser) -> None:
        p.add_argument("--type", type=int, default=None, help="1余额修改 2收入 3支出 4转账")
        p.add_argument("--category_ids", default="")
        p.add_argument("--account_ids", default="")
        p.add_argument("--tag_filter", default="")
        p.add_argument("--amount_filter", default="", help="如 gt:10000（单位：分）")
        p.add_argument("--keyword", default="")

    def add_window(p: argparse.ArgumentParser) -> None:
        p.add_argument("--range", default="", help=f"时间范围：{RANGE_HELP}")
        p.add_argument("--start_time", type=int, default=None)
        p.add_argument("--end_time", type=int, default=None)
        p.add_argument("--label", default="", help="汇总标题（stats 用）")

    p_query = sub.add_parser("query", help=EXTRA_COMMANDS["query"])
    add_global(p_query)
    add_filters(p_query)
    add_window(p_query)
    p_query.add_argument("--count", type=int, default=20, help="最多返回几笔（0=不限）")

    p_mod = sub.add_parser("modify", help=EXTRA_COMMANDS["modify"])
    add_global(p_mod)
    p_mod.add_argument("--id", required=True)
    p_mod.add_argument("--near-time", type=int, default=None)
    p_mod.add_argument("--near-days", type=int, default=DEFAULT_NEAR_DAYS)
    p_mod.add_argument("--dry-run", action="store_true")
    p_mod.add_argument("--category_id", default=None)
    p_mod.add_argument("--time", type=int, default=None)
    p_mod.add_argument("--utc_offset", type=int, default=None)
    p_mod.add_argument("--source_account_id", default=None)
    p_mod.add_argument("--source_amount", type=int, default=None)
    p_mod.add_argument("--destination_account_id", default=None)
    p_mod.add_argument("--destination_amount", type=int, default=None)
    p_mod.add_argument("--comment", default=None)
    p_mod.add_argument("--tag_ids", default=None)
    p_mod.add_argument("--hide_amount", default=None)

    p_stats = sub.add_parser("stats", help=EXTRA_COMMANDS["stats"])
    add_global(p_stats)
    add_filters(p_stats)
    add_window(p_stats)
    p_stats.add_argument("--top", type=int, default=10)
    p_stats.add_argument("--json", action="store_true")

    return parser


def split_global_options(argv: list[str]) -> tuple[str, int | None, list[str]]:
    """把 ``--tz-name`` / ``--tz-offset`` 从任意位置摘出来。

    官方脚本要求全局选项写在命令名**前面**。这里两种位置都接受 ——
    顺序写错不该让人白跑一次才发现。
    """
    tz_name, tz_offset, rest = "", None, []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in ("--tz-name", "--tz-offset") and index + 1 < len(argv):
            value = argv[index + 1]
            if token == "--tz-name":
                tz_name = value
            else:
                try:
                    tz_offset = int(value)
                except ValueError:
                    raise EbkError(f"--tz-offset 需要整数（分钟），收到 {value!r}") from None
            index += 2
            continue
        rest.append(token)
        index += 1
    return tz_name, tz_offset, rest


KNOWN_COMMANDS = ("list", "help", *COMMANDS, *EXTRA_COMMANDS)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        tz_name, tz_offset, rest = split_global_options(argv)

        if not rest:
            print(print_command_list())
            return 0
        command, tail = rest[0], rest[1:]

        if command == "list":
            print(print_command_list())
            return 0
        if command == "help":
            print(print_command_help(tail[0] if tail else ""))
            return 0
        if command not in KNOWN_COMMANDS:
            # 提前拦下并给出人话，别让 argparse 打一整页 usage
            raise EbkError(f"没有这个命令：{command}；用 `list` 看全部命令")

        # 全局选项已经摘出来了，这里塞回去让 argparse 认得（写在命令名前后都行）
        if tz_name:
            tail = [*tail, "--tz-name", tz_name]
        elif tz_offset is not None:
            tail = [*tail, "--tz-offset", str(tz_offset)]

        # 官方命令的参数是 `--名字 值` 形式、数量不固定，argparse 不认识它们，
        # 所以用 parse_known_args 把「多出来」的部分原样收回来自己解析。
        args, unknown = build_parser().parse_known_args([command, *tail])

        if command == "query":
            print(cmd_query(args))
        elif command == "stats":
            print(cmd_stats(args))
        elif command == "modify":
            print(cmd_modify(args))
        else:
            print(run_official(command, args, list(unknown)))
        return 0
    except EbkError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except SystemExit as exc:  # argparse 的用法错误（缺必填选项等）
        return int(exc.code) if isinstance(exc.code, int) else 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
