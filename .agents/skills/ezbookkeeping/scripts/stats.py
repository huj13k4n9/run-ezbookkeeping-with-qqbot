"""账单统计：把 ezBookkeeping 的交易列表聚合成人话。

设计要点：

* **纯函数**：只吃 ``transactions/list/all.json`` 的原始返回，不碰网络、不碰时间，
  方便单测（见 ``tests/test_ebktools_cli.py``）。
* **金额一律用整数分**：接口返回的 ``sourceAmount`` 就是分（``1234`` = 12.34 元），
  累加时绝不用浮点，最后一步才转成元显示 —— 浮点累加会攒出 0.01 的误差。
* **转账不计入收支**：``type 4`` 的两个账户是一进一出，计进收支会双算。
  它单独统计，用来回答「这个月往信用卡还了多少」。
* **余额修改（type 1）同样不计入收支**：那是校正，不是真实收付。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

TYPE_INCOME = 2
TYPE_EXPENSE = 3
TYPE_TRANSFER = 4
TYPE_BALANCE = 1

TYPE_NAMES = {
    TYPE_BALANCE: "余额修改",
    TYPE_INCOME: "收入",
    TYPE_EXPENSE: "支出",
    TYPE_TRANSFER: "转账",
}


def cents_to_yuan(amount: int) -> str:
    """分 → 元的显示字符串（保留两位，负数带上负号）。"""
    sign = "-" if amount < 0 else ""
    value = abs(int(amount))
    return f"{sign}{value // 100}.{value % 100:02d}"


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _text(value: Any) -> str:
    return str(value or "").strip()


def _account_id_of(item: dict, key: str) -> str:
    """取账户 ID：接口可能给 ID 字符串，也可能给嵌套对象（取决于 trim_* 参数）。"""
    raw = item.get(key)
    if isinstance(raw, dict):
        return _text(raw.get("id"))
    return _text(raw)


@dataclass
class Summary:
    """一段时间的汇总。金额字段全部是**分**。"""

    count: int = 0
    income: int = 0
    expense: int = 0
    transfer: int = 0
    balance: int = 0
    #: (分类 ID, 金额分, 笔数)，按金额从大到小
    by_category: list[tuple[str, int, int]] = field(default_factory=list)
    #: (账户 ID, 支出分, 收入分)，按支出从大到小
    by_account: list[tuple[str, int, int]] = field(default_factory=list)
    #: (关键词, 金额分, 笔数)，按金额从大到小
    by_keyword: list[tuple[str, int, int]] = field(default_factory=list)
    first_time: int = 0
    last_time: int = 0

    @property
    def net(self) -> int:
        """净额 = 收入 - 支出（转账/余额修改不计）。"""
        return self.income - self.expense

    def is_empty(self) -> bool:
        return self.count == 0


def summarize(items: Iterable[dict], *, top: int = 10, with_keyword: bool = True) -> Summary:
    """把交易列表聚合成 ``Summary``。

    ``top`` 控制明细条数（分类/账户/关键词各取前 N 条），避免回复被塞爆。
    """
    result = Summary()
    category: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    account: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    keyword: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    times: list[int] = []

    for item in items:
        if not isinstance(item, dict):
            continue
        result.count += 1

        ttype = _as_int(item.get("type"))
        amount = _as_int(item.get("sourceAmount"))
        when = _as_int(item.get("time"))
        if when:
            times.append(when)

        if ttype == TYPE_INCOME:
            result.income += amount
        elif ttype == TYPE_EXPENSE:
            result.expense += amount
        elif ttype == TYPE_TRANSFER:
            result.transfer += amount
        elif ttype == TYPE_BALANCE:
            result.balance += amount

        # 转账不参与「分类/账户消费榜」：它会同时污染转出和转入两边
        if ttype in (TYPE_INCOME, TYPE_EXPENSE):
            cid = _text(item.get("categoryId")) or _text(
                (item.get("category") or {}).get("id") if isinstance(item.get("category"), dict) else ""
            )
            if cid:
                bucket = category[cid]
                bucket[0] += amount
                bucket[1] += 1

            aid = _account_id_of(item, "sourceAccountId")
            if aid:
                bucket = account[aid]
                bucket[1] += amount if ttype == TYPE_INCOME else 0
                bucket[0] += amount if ttype == TYPE_EXPENSE else 0

        if with_keyword:
            kw = _text(item.get("comment"))
            if kw:
                bucket = keyword[kw]
                bucket[0] += amount
                bucket[1] += 1

    result.by_category = [
        (k, v[0], v[1]) for k, v in sorted(category.items(), key=lambda kv: -kv[1][0])[:top]
    ]
    result.by_account = [
        (k, v[0], v[1]) for k, v in sorted(account.items(), key=lambda kv: -kv[1][0])[:top]
    ]
    result.by_keyword = [
        (k, v[0], v[1]) for k, v in sorted(keyword.items(), key=lambda kv: -kv[1][0])[:top]
    ]
    if times:
        result.first_time = min(times)
        result.last_time = max(times)
    return result


def format_summary(summary: Summary, *, label: str = "") -> str:
    """把汇总渲染成给 agent 看的纯文本（agent 再翻译成人话发给用户）。

    刻意输出**分类 ID / 账户 ID**：ID → 名字的映射由 agent 用
    ``transaction-categories-list`` / ``accounts-list`` 完成，这里不再多发请求。
    """
    lines: list[str] = []
    head = f"[汇总] {label}".rstrip()
    lines.append(head)

    if summary.is_empty():
        lines.append("区间内没有任何交易。")
        return "\n".join(lines)

    lines.append(
        f"总笔数 {summary.count}；支出 {cents_to_yuan(summary.expense)} 元"
        f"（{summary.expense} 分）；收入 {cents_to_yuan(summary.income)} 元"
        f"；净额 {cents_to_yuan(summary.net)} 元"
    )
    if summary.transfer:
        lines.append(f"转账 {cents_to_yuan(summary.transfer)} 元（不计入收支）")
    if summary.balance:
        lines.append(f"余额修改 {cents_to_yuan(summary.balance)} 元（不计入收支）")

    if summary.by_category:
        lines.append("[分类] 金额单位：分")
        for cid, amount, count in summary.by_category:
            lines.append(f"  {cid}\t{amount}\t{count} 笔")
    if summary.by_account:
        lines.append("[账户支出] 金额单位：分")
        for aid, expense, income in summary.by_account:
            lines.append(f"  {aid}\t支出 {expense}\t收入 {income}")
    if summary.by_keyword:
        lines.append("[说明词频] 金额单位：分")
        for kw, amount, count in summary.by_keyword:
            lines.append(f"  {kw}\t{amount}\t{count} 笔")
    if summary.first_time:
        lines.append(f"[时间范围] unix {summary.first_time} ~ {summary.last_time}")
    return "\n".join(lines)
