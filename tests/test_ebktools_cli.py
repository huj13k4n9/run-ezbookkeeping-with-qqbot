#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ebktools（复刻增强版）自测 —— 用本地 mock 服务端，不需要真实账本 / 外网。

覆盖：

    官方命令   12 条都能打到正确的路径/方法，参数名与类型正确
    query      筛选参数真的透传（且客户端不做二次过滤）
    modify     只改给到的字段，其余从原交易继承；null 字段必须丢掉；dry-run 不提交
    stats      聚合正确性：转账不计入收支、分转元、排行、空区间
    ranges     自然月/周/年边界的 unix 区间（含跨年、闰月）
    参数校验   必填缺失、未知参数、交易类命令缺时区都要提前报错

运行： python tests/test_ebktools_cli.py
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".agents" / "skills" / "ezbookkeeping" / "scripts"))

from ranges import RangeError, resolve_range  # noqa: E402
from stats import cents_to_yuan, summarize  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}" + (f"  -> {detail}" if detail and not ok else ""))


# --------------------------------------------------------------------------- 样本
#: 4 笔：支出 12.34/56.78（同分类）、收入 100.00、转账 500.00
SAMPLE_TX = [
    {
        "id": "T1", "type": 3, "time": 1790000000, "utcOffset": 480, "categoryId": "C1",
        "sourceAccountId": "A1", "sourceAmount": 1234, "comment": "午饭", "editable": True,
    },
    {
        "id": "T2", "type": 3, "time": 1790000600, "utcOffset": 480, "categoryId": "C1",
        "sourceAccountId": "A1", "sourceAmount": 5678, "comment": "午饭",
        "editable": True, "hideAmount": None, "tagIds": None,
    },
    {
        "id": "T3", "type": 2, "time": 1790001200, "utcOffset": 480, "categoryId": "C2",
        "sourceAccountId": "A1", "sourceAmount": 10000, "comment": "工资", "editable": True,
    },
    {
        "id": "T4", "type": 4, "time": 1790001800, "utcOffset": 480, "categoryId": "C3",
        "sourceAccountId": "A1", "sourceAmount": 50000,
        "destinationAccountId": "A2", "destinationAmount": 50000,
        "comment": "还花呗", "editable": True,
    },
]

ACCOUNTS = [
    # balance 是**整数分**（真实接口实测：928712 = 9287.12 元）
    {"id": "A1", "name": "支付宝", "balance": 123456, "subAccounts": []},
    {"id": "A2", "name": "花呗", "balance": -51800, "subAccounts": []},
]

CATEGORIES = [
    {"id": "C0", "name": "餐饮", "type": 2, "parentId": "0",
     "subCategories": [{"id": "C1", "name": "午饭", "type": 2, "parentId": "C0"}]},
    {"id": "C2", "name": "工资", "type": 1, "parentId": "0", "subCategories": []},
]
TAGS = [{"id": "TG1", "name": "报销", "groupId": "G1"}]
TOKENS = [{"tokenId": "TK1", "tokenType": 8, "isCurrent": True, "lastSeen": 1790000000}]
RATES = [{"from": "CNY", "to": "USD", "rate": "0.14", "updatedTime": 1790000000}]


# --------------------------------------------------------------------------- mock
class MockHandler(BaseHTTPRequestHandler):
    server_version = "mock-ebk/1.0"

    def log_message(self, *args):  # 静音
        pass

    def _record(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""
        entry = {
            "path": self.path,
            "method": self.command,
            "body": json.loads(body) if body else None,
            "auth": self.headers.get("Authorization") or "",
            "tz_name": self.headers.get("X-Timezone-Name") or "",
            "tz_offset": self.headers.get("X-Timezone-Offset") or "",
        }
        self.server.requests.append(entry)  # type: ignore[attr-defined]
        return entry

    def _send(self, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        entry = self._record()
        path = urllib.parse.urlparse(self.path).path
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

        if path.endswith("transactions/list/all.json"):
            # 像真服务端那样按时间窗过滤（客户端不该依赖本地过滤）
            start = int((query.get("start_time") or ["0"])[0] or 0)
            end = int((query.get("end_time") or ["0"])[0] or 0)
            items = [t for t in SAMPLE_TX if start <= int(t["time"]) <= end]
            self._send({"success": True, "result": items})
        elif path.endswith("transactions/list.json"):
            self._send({"success": True, "result": {
                "items": SAMPLE_TX, "nextTimeSequenceId": 0, "totalCount": len(SAMPLE_TX),
            }})
        elif path.endswith("accounts/list.json"):
            self._send({"success": True, "result": ACCOUNTS})
        elif path.endswith("transaction/categories/list.json"):
            self._send({"success": True, "result": CATEGORIES})
        elif path.endswith("transaction/tags/list.json"):
            self._send({"success": True, "result": TAGS})
        elif path.endswith("tokens/list.json"):
            self._send({"success": True, "result": TOKENS})
        elif path.endswith("exchange_rates/latest.json"):
            self._send({"success": True, "result": RATES})
        elif path.endswith("systems/version.json"):
            self._send({"success": True, "result": "v1.2.3"})
        else:
            self._send({"success": False, "errorCode": 404, "errorMessage": f"mock 没有: {entry['path']}"})

    def do_POST(self) -> None:  # noqa: N802
        entry = self._record()
        path = urllib.parse.urlparse(self.path).path
        sent = entry["body"] or {}
        if path.endswith("transactions/modify.json"):
            self._send({"success": True, "result": {"id": sent.get("id"), **sent}})
        elif path.endswith("transactions/add.json"):
            self._send({"success": True, "result": {"id": "NEW1", **sent}})
        else:
            self._send({"success": True, "result": {"id": "NEW1", **sent}})


def start_server() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    server.requests = []  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


class CliEnv:
    """把 CLI 指向 mock 服务端。"""

    def __enter__(self):
        import os
        self._saved = {k: os.environ.get(k) for k in ("EBKTOOL_SERVER_BASEURL", "EBKTOOL_TOKEN")}
        os.environ["EBKTOOL_SERVER_BASEURL"] = self.base
        os.environ["EBKTOOL_TOKEN"] = "test-token"
        return self

    def __init__(self, base: str):
        self.base = base

    def __exit__(self, *exc):
        import os
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run_cli(argv: list[str]) -> tuple[int, str]:
    """直接调 main()，避免起子进程（Windows 上更稳）。

    argparse 遇到未知命令会 ``sys.exit(2)`` —— 必须接住，否则会把整个测试进程带走。
    """
    import contextlib
    import io
    import importlib

    module = importlib.import_module("ebktools")
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = module.main(argv)
        except SystemExit as exc:  # argparse 的用法错误
            code = int(exc.code) if isinstance(exc.code, int) else 1
    return code, (out.getvalue() + err.getvalue())


# --------------------------------------------------------------------------- 用例
def test_stats_pure() -> None:
    print("\n[用例1] stats 聚合（纯函数）")
    s = summarize(SAMPLE_TX)

    check("总笔数 4", s.count == 4, str(s.count))
    check("支出 = 1234+5678 = 6912 分", s.expense == 6912, str(s.expense))
    check("收入 = 10000 分", s.income == 10000, str(s.income))
    check("转账单独算、不计入收支", s.transfer == 50000 and s.expense == 6912, str(s.transfer))
    check("净额 = 收入-支出 = 3088", s.net == 3088, str(s.net))
    check("6912 分显示成 69.12 元", cents_to_yuan(6912) == "69.12", cents_to_yuan(6912))
    check("0 分显示成 0.00", cents_to_yuan(0) == "0.00", cents_to_yuan(0))
    check("负数（负债）显示带负号", cents_to_yuan(-51800) == "-518.00", cents_to_yuan(-51800))

    cats = {cid: (amount, count) for cid, amount, count in s.by_category}
    check("分类 C1 = 6912 分 / 2 笔", cats.get("C1") == (6912, 2), repr(cats))
    check("分类 C2（收入）也在榜上", cats.get("C2") == (10000, 1), repr(cats))
    check("转账的分类不进榜", "C3" not in cats, repr(cats))
    accounts = {aid: (exp, inc) for aid, exp, inc in s.by_account}
    check("账户 A1 支出 6912 / 收入 10000", accounts.get("A1") == (6912, 10000), repr(accounts))
    check("空区间不炸", summarize([]).is_empty() and summarize([]).count == 0)

    # 回归：--count 0 的习惯含义是「不限」，曾把 modify 的查找截成空列表
    from ebktools import _trim

    check("max_items=0 表示不限（回归）", len(_trim(SAMPLE_TX, 0)) == len(SAMPLE_TX))
    check("max_items=None 表示不限", len(_trim(SAMPLE_TX, None)) == len(SAMPLE_TX))
    check("max_items=2 真的截断", len(_trim(SAMPLE_TX, 2)) == 2)

    many = [dict(SAMPLE_TX[0], id=f"X{i}", comment=f"c{i}") for i in range(30)]
    check("top 限制明细条数", len(summarize(many, top=5).by_keyword) <= 5)


def test_ranges() -> None:
    print("\n[用例2] 时间范围解析")
    now = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc).timestamp()

    start, end, label = resolve_range("this-month", tz_offset=480, now=now)
    first = datetime.fromtimestamp(start, tz=timezone.utc) + timedelta(hours=8)
    last = datetime.fromtimestamp(end, tz=timezone.utc) + timedelta(hours=8)
    check("本月起点 = 1 号 0 点", first.strftime("%Y-%m-%d %H:%M") == "2026-03-01 00:00", str(first))
    check("本月终点 = 31 号 23:59:59", last.strftime("%Y-%m-%d %H:%M:%S") == "2026-03-31 23:59:59", str(last))
    check("本月 label = 2026-03", label == "2026-03", label)

    jan = datetime(2026, 1, 10, 10, 0, tzinfo=timezone.utc).timestamp()
    check("跨年：1 月的上个月是去年 12 月",
          resolve_range("last-month", tz_offset=480, now=jan)[2] == "2025-12")

    span_start, span_end, _ = resolve_range("2024-02", tz_offset=480, now=now)
    check("闰年 2 月区间跨 28 天差 = 29 天月", (span_end - span_start) // 86400 == 28)
    check("今天 / 昨天可解析",
          bool(resolve_range("today", tz_offset=480, now=now)[2])
          and bool(resolve_range("yesterday", tz_offset=480, now=now)[2]))
    check("本周从周一开始",
          resolve_range("this-week", tz_offset=480, now=now)[2].startswith("2026-03-09"))
    try:
        resolve_range("benyue", tz_offset=480, now=now)
        check("非法范围报错", False, "没有报错")
    except RangeError as exc:
        check("非法范围报错（并提示可用值）", "this-month" in str(exc), str(exc))


def test_official_commands(server: ThreadingHTTPServer, base: str) -> None:
    print("\n[用例3] 官方命令复刻（路径 / 方法 / 参数）")

    with CliEnv(base):
        # --- 无参数的 GET ---
        for command, endpoint in (
            ("accounts-list", "accounts/list.json"),
            ("transaction-categories-list", "transaction/categories/list.json"),
            ("transaction-tags-list", "transaction/tags/list.json"),
            ("tokens-list", "tokens/list.json"),
            ("exchangerates-latest", "exchange_rates/latest.json"),
            ("server-version", "systems/version.json"),
        ):
            server.requests.clear()  # type: ignore[attr-defined]
            code, out = run_cli([command])
            req = server.requests[-1]  # type: ignore[attr-defined]
            check(f"{command} 打到 {endpoint}", code == 0 and endpoint in req["path"], f"{code} {req['path']}")
            check(f"{command} 带上 token", req["auth"].startswith("Bearer "), req["auth"][:12])

        # --- 账户列表要能渲染出子账户与余额（分 → 元）---
        code, out = run_cli(["accounts-list"])
        check("accounts-list 渲染余额（分转元）", "1234.56" in out, out)
        check("accounts-list 渲染账户名", "支付宝" in out, out)

        # --- 分类列表要能渲染二级分类 ---
        code, out = run_cli(["transaction-categories-list"])
        check("分类渲染出二级分类", "C1" in out and "午饭" in out, out)

        # --- transactions-list：count 必填 + 时区 ---
        server.requests.clear()  # type: ignore[attr-defined]
        code, out = run_cli(["--tz-offset", "480", "transactions-list", "--count", "5", "--type", "3"])
        req = server.requests[-1]  # type: ignore[attr-defined]
        check("transactions-list 路径正确", "transactions/list.json" in req["path"], req["path"])
        check("transactions-list 透传 count 与 type", "count=5" in req["path"] and "type=3" in req["path"], req["path"])
        check("transactions-list 带时区头", req["tz_offset"] == "480", req["tz_offset"])

        code, out = run_cli(["transactions-list", "--count", "5"])
        check("transactions-list 缺时区时前置报错", code != 0 and "必须给时区" in out, out)
        code, out = run_cli(["--tz-offset", "480", "transactions-list"])
        check("transactions-list 缺 count 时报错", code != 0 and "count" in out, out)

        # --- transactions-list-all：按时间窗全量 ---
        server.requests.clear()  # type: ignore[attr-defined]
        code, out = run_cli([
            "--tz-offset", "480", "transactions-list-all",
            "--start_time", "1789999000", "--end_time", "1790009999",
        ])
        req = server.requests[-1]  # type: ignore[attr-defined]
        check("list-all 路径正确", "transactions/list/all.json" in req["path"], req["path"])
        check("list-all 透传时间窗",
              "start_time=1789999000" in req["path"] and "end_time=1790009999" in req["path"], req["path"])
        check("list-all 输出笔数与元金额", "共 4 笔" in out and "12.34元" in out, out[:200])

        # --- transactions-add：金额/类型转换 + 请求体 ---
        server.requests.clear()  # type: ignore[attr-defined]
        code, out = run_cli([
            "--tz-offset", "480", "transactions-add",
            "--type", "3", "--categoryId", "C1", "--time", "1790000000", "--utcOffset", "480",
            "--sourceAccountId", "A1", "--sourceAmount", "3000", "--comment", "午饭",
        ])
        posted = [r for r in server.requests if r["method"] == "POST"]  # type: ignore[attr-defined]
        check("transactions-add 是 POST", len(posted) == 1, repr([r["method"] for r in server.requests]))  # type: ignore[attr-defined]
        body = posted[0]["body"] if posted else {}
        check("add 的 type 是整数", body.get("type") == 3, repr(body.get("type")))
        check("add 的 sourceAmount 是整数（分）", body.get("sourceAmount") == 3000, repr(body.get("sourceAmount")))
        check("add 的 comment 保留", body.get("comment") == "午饭", repr(body.get("comment")))
        check("add 的 time / utcOffset 是整数",
              body.get("time") == 1790000000 and body.get("utcOffset") == 480, repr(body))
        check("add 带时区头", posted[0]["tz_offset"] == "480" if posted else False)

        # 必填缺失要提前拦
        code, out = run_cli(["--tz-offset", "480", "transactions-add", "--type", "3"])
        check("add 缺必填时报错并点名参数",
              code != 0 and "categoryId" in out and "sourceAmount" in out, out)

        # --- 未知参数要拦（否则会静默丢参数）---
        code, out = run_cli(["--tz-offset", "480", "transactions-list", "--count", "5", "--nope", "1"])
        check("未知参数被拦下", code != 0 and "nope" in out, out)
        code, out = run_cli(["--tz-offset", "480", "transactions-list", "--count=5"])
        check("--key=value 形式被拦下（提示用空格）", code != 0, out)

        # --- 布尔与数组类型的转换 ---
        server.requests.clear()  # type: ignore[attr-defined]
        code, out = run_cli([
            "--tz-offset", "480", "transactions-list", "--count", "5",
            "--with_count", "true", "--trim_account", "1",
        ])
        check("布尔 true 转成 true", "with_count=true" in server.requests[-1]["path"], server.requests[-1]["path"])  # type: ignore[attr-defined]
        check("布尔 1 也转成 true", "trim_account=true" in server.requests[-1]["path"], server.requests[-1]["path"])  # type: ignore[attr-defined]

        # --- 全局选项写在命令名后面也要认 ---
        server.requests.clear()  # type: ignore[attr-defined]
        code, out = run_cli(["transactions-list", "--count", "5", "--tz-offset", "480"])
        check("全局选项写在命令后也认（回归）",
              code == 0 and server.requests[-1]["tz_offset"] == "480",  # type: ignore[attr-defined]
              f"{code} {server.requests[-1]['tz_offset']}")  # type: ignore[attr-defined]


def test_extra_commands(server: ThreadingHTTPServer, base: str) -> None:
    print("\n[用例4] 增强命令：query / stats / modify")

    window_start = min(t["time"] for t in SAMPLE_TX) - 3600
    window_end = max(t["time"] for t in SAMPLE_TX) + 3600
    window = ["--start_time", str(window_start), "--end_time", str(window_end)]

    with CliEnv(base):
        # --- query：筛选参数必须真的发给服务端 ---
        server.requests.clear()  # type: ignore[attr-defined]
        code, out = run_cli([
            "--tz-offset", "480", "query", *window,
            "--amount_filter", "gt:10000", "--account_ids", "A1",
            "--tag_filter", "0:TG1", "--keyword", "午饭", "--type", "3", "--count", "5",
        ])
        req = server.requests[-1]  # type: ignore[attr-defined]
        check("query 退出码 0", code == 0, out)
        check("query 走 list-all（给了时间窗就不翻页）", "list/all.json" in req["path"], req["path"])
        check("query 透传 amount_filter", "amount_filter=gt%3A10000" in req["path"], req["path"])
        check("query 透传 account_ids", "account_ids=A1" in req["path"], req["path"])
        check("query 透传 tag_filter", "tag_filter=0%3ATG1" in req["path"], req["path"])
        check("query 透传 type", "type=3" in req["path"], req["path"])
        check("query 只打一次接口", len(server.requests) == 1, str(len(server.requests)))  # type: ignore[attr-defined]
        check("query 输出分与元都给", "6912" in out or "1234" in out, out[:200])
        check("query 不泄露 token", "test-token" not in out, out[:200])

        # --- query --range：范围表达式要真的算出区间 ---
        server.requests.clear()  # type: ignore[attr-defined]
        code, out = run_cli(["--tz-offset", "480", "query", "--range", "2026-09", "--count", "5"])
        query = urllib.parse.parse_qs(urllib.parse.urlparse(server.requests[-1]["path"]).query)  # type: ignore[attr-defined]
        start = int((query.get("start_time") or ["0"])[0])
        end = int((query.get("end_time") or ["0"])[0])
        check("--range 算出的是 2026-09 的区间", start < end and end - start >= 28 * 86400, f"{start}~{end}")
        check("--range 区间覆盖样本数据", start <= SAMPLE_TX[0]["time"] <= end, f"{start}~{end}")

        # --- stats：转账不计入收支 ---
        code, out = run_cli(["--tz-offset", "480", "stats", *window, "--label", "本月"])
        check("stats 退出码 0", code == 0, out)
        check("stats 标题带 label", "本月" in out, out[:120])
        check("stats 支出 69.12 元", "69.12" in out, out)
        check("stats 转账单独列且不计入收支", "转账" in out and "不计入收支" in out, out)

        code, out = run_cli(["--tz-offset", "480", "stats", *window, "--json"])
        payload = json.loads(out)
        check("stats --json 支出 = 6912 分", payload.get("expense") == 6912, repr(payload.get("expense")))
        check("stats --json 转账 = 50000 分", payload.get("transfer") == 50000, repr(payload.get("transfer")))

        # --- modify：只给 comment，其余字段必须继承 ---
        server.requests.clear()  # type: ignore[attr-defined]
        code, out = run_cli([
            "--tz-offset", "480", "modify", "--id", "T2", "--comment", "改成晚饭",
            "--near-time", "1790000600",
        ])
        body = None
        for r in server.requests:  # type: ignore[attr-defined]
            if r["body"] and r["body"].get("id") == "T2":
                body = r["body"]
        check("modify 退出码 0", code == 0, out)
        check("modify 找到并提交了 T2", body is not None, repr([r["path"] for r in server.requests]))  # type: ignore[attr-defined]
        if body:
            check("只改说明", body.get("comment") == "改成晚饭", repr(body.get("comment")))
            check("金额继承原值（不会被清空）", body.get("sourceAmount") == 5678, repr(body.get("sourceAmount")))
            check("分类继承原值", body.get("categoryId") == "C1", repr(body.get("categoryId")))
            check("时间继承原值", body.get("time") == 1790000600, repr(body.get("time")))
            check("账户继承原值", body.get("sourceAccountId") == "A1", repr(body.get("sourceAccountId")))
            check("null 字段被丢掉（否则服务端 200000）",
                  "hideAmount" not in body and "tagIds" not in body, repr(body))
        check("modify 回显余额", "余额" in out, out)
        check("余额按元显示而不是裸分", "1234.56 元" in out, out)

        # 交易相关请求都必须带时区（回归：find 路径曾把时区参数丢了）
        without_tz = [
            r["path"] for r in server.requests  # type: ignore[attr-defined]
            if "/transactions/" in r["path"] and not r["tz_offset"] and not r["tz_name"]
        ]
        check("modify 的交易请求都带时区（回归）", without_tz == [], repr(without_tz))

        code, out = run_cli(["modify", "--id", "T2", "--comment", "x", "--near-time", "1790000600"])
        check("modify 缺时区时前置报错", code != 0 and "必须给时区" in out, out)

        # --- dry-run 绝不提交 ---
        server.requests.clear()  # type: ignore[attr-defined]
        code, out = run_cli([
            "--tz-offset", "480", "modify", "--id", "T2", "--comment", "改成晚饭",
            "--near-time", "1790000600", "--dry-run",
        ])
        check("dry-run 退出码 0", code == 0, out)
        check("dry-run 标注未提交", "未提交" in out, out)
        check("dry-run 打印前后差异", "改成晚饭" in out and "午饭" in out, out)
        check("dry-run 不提交任何修改",
              [r for r in server.requests if r["path"].endswith("modify.json")] == [])  # type: ignore[attr-defined]

        code, out = run_cli(["--tz-offset", "480", "modify", "--id", "T1", "--near-time", "1790000000"])
        check("不给字段时报错", code != 0 and "没有给任何要修改的字段" in out, out)

        code, out = run_cli(["--tz-offset", "480", "modify", "--id", "NOPE", "--near-time", "1790000000"])
        check("找不到交易时报错", code != 0 and "没找到" in out, out)

        # --- 不可编辑的交易要如实拒绝 ---
        server.requests.clear()  # type: ignore[attr-defined]
        import ebktools
        original_fetch = ebktools.fetch_transactions
        ebktools.fetch_transactions = lambda **kw: [dict(SAMPLE_TX[1], editable=False)]  # type: ignore[assignment]
        try:
            code, out = run_cli([
                "--tz-offset", "480", "modify", "--id", "T2", "--comment", "x", "--near-time", "1790000600",
            ])
            check("不可编辑的交易被拒绝", code != 0 and "不可编辑" in out, out)
        finally:
            ebktools.fetch_transactions = original_fetch  # type: ignore[assignment]

    # --- 环境变量缺失 ---
    import os
    saved = os.environ.pop("EBKTOOL_SERVER_BASEURL", None)
    try:
        code, out = run_cli(["stats", *window, "--tz-offset", "480"])
        check("缺服务端地址时报可读错误", code != 0 and "EBKTOOL_SERVER_BASEURL" in out, out)
    finally:
        if saved is not None:
            os.environ["EBKTOOL_SERVER_BASEURL"] = saved


def test_help_and_list() -> None:
    print("\n[用例5] list / help 自身")
    code, out = run_cli(["list"])
    check("list 退出码 0", code == 0, out)
    check("list 列出官方命令", "transactions-add" in out and "accounts-list" in out, out[:200])
    check("list 列出增强命令", "query" in out and "modify" in out and "stats" in out, out[:400])
    check("list 说明官方 list 的用途", "Available API Commands" in out, out[:80])

    code, out = run_cli(["help", "transactions-add"])
    check("help 给出必填参数", "categoryId" in out and "sourceAmount" in out, out)
    check("help 提示金额单位是分", "分" in out, out)

    code, out = run_cli(["help", "query"])
    check("help query 解释 --range", "--range" in out and "this-month" in out, out)
    code, out = run_cli(["help", "modify"])
    check("help modify 提到 dry-run", "--dry-run" in out, out)

    code, out = run_cli([])
    check("不带参数时列出命令", code == 0 and "transactions-add" in out, out[:120])

    code, out = run_cli(["不存在的命令"])
    check("未知命令报错", code != 0 and "没有这个命令" in out, out)


def main() -> int:
    print("=" * 62)
    print("  ebktools（复刻增强版）自测")
    print("=" * 62)

    test_stats_pure()
    test_ranges()
    test_help_and_list()

    server, base = start_server()
    try:
        test_official_commands(server, base)
        test_extra_commands(server, base)
    finally:
        server.shutdown()

    failed = [c for c in CHECKS if not c[1]]
    print("\n" + "=" * 62)
    print(f"  通过 {len(CHECKS) - len(failed)}/{len(CHECKS)}")
    if failed:
        print("  失败项:")
        for name, _ok, detail in failed:
            print(f"    - {name}  {detail}")
        return 1
    print("  全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
