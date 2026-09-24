#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""agent 层自测：会话 ID、去重、prompt 组装、子进程调用、调度与清洗。

全部离线，不需要真的装 pi —— 用临时脚本冒充可执行文件。

运行： python tests/test_agent.py
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import textwrap
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qqbot.agent import (  # noqa: E402
    AgentResult,
    AgentRunner,
    Deduplicator,
    SessionManager,
    build_agent_env,
    build_prompt,
    parse_event_time,
)
from qqbot.bot import Event, QQBot, sanitize_reply  # noqa: E402
from qqbot.config import BotConfig  # noqa: E402
from qqbot.quote import ResolvedQuote  # noqa: E402
from qqbot.refindex import RefAttachment  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}" + (f"  -> {detail}" if detail and not ok else ""))


# --------------------------------------------------------------------------- 工具
def make_config(tmp: Path, **overrides) -> BotConfig:
    params = dict(
        app_id="1",
        client_secret="s",
        log_level="WARNING",
        ref_index_path=str(tmp / "ref-index.jsonl"),
        auto_download_dir=str(tmp / "media"),
        agent_cwd=str(tmp / "ws"),
    )
    params.update(overrides)
    ws = tmp / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    # 真实的 agent cwd 里应该有 AGENTS.md，否则 validate() 会报问题
    (ws / "AGENTS.md").write_text("# test\n", encoding="utf-8")
    return BotConfig(**params)


_PRELUDE = '''import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
'''


def fake_executable(tmp: Path, body: str, name: str = "fake_pi") -> str:
    """生成一个忽略参数、按 body 行为的可执行文件（Windows 用 .bat 包装）。"""
    script = tmp / f"{name}.py"
    script.write_text(_PRELUDE + textwrap.dedent(body), encoding="utf-8")
    if os.name == "nt":
        bat = tmp / f"{name}.bat"
        bat.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return str(bat)
    sh = tmp / name
    sh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
    sh.chmod(sh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(sh)


def msg_event(content="记 30 午饭", **kwargs) -> Event:
    data = {
        "id": kwargs.pop("msg_id", "MSG-1"),
        "author": {"user_openid": "OPENID-A", "bot": False},
        "content": content,
        "message_type": 0,
        "timestamp": "2026-09-23T23:51:24+08:00",
        "message_scene": {"ext": ["msg_idx=REFIDX_T=="]},
    }
    data.update(kwargs)
    return Event(name="C2C_MESSAGE_CREATE", id="EV", seq=1, data=data)


def image_only_event(tmp: Path, name="pic.jpg") -> Event:
    path = tmp / "media"
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_bytes(b"fake")
    ev = msg_event(content="")
    ev.data["attachments"] = [{
        "content_type": "image/jpeg", "filename": name,
        "url": "https://example.com/x", "size": 4,
    }]
    ev.local_attachments = {0: str(path / name)}
    return ev


# --------------------------------------------------------------------------- 用例
def test_session_manager() -> None:
    print("\n[用例1] SessionManager")
    sm = SessionManager("qq", "day")
    d1 = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
    d2 = d1 + timedelta(days=1)
    sid1 = sm.session_id("OPENID-A", d1)
    sid2 = sm.session_id("OPENID-A", d2)
    sid_other = sm.session_id("OPENID-B", d1)

    check("同用户同一天 ID 稳定", sid1 == sm.session_id("OPENID-A", d1))
    check("同用户跨天换新会话", sid1 != sid2, f"{sid1} vs {sid2}")
    check("不同用户不串话", sid1 != sid_other, f"{sid1} vs {sid_other}")
    check("ID 只含合法字符", all(c.isalnum() or c in "-._" for c in sid1), sid1)
    check("ID 不含原始 openid", "OPENID" not in sid1.upper(), sid1)
    check("day 桶格式正确", sid1.endswith("20260923"), sid1)

    week = SessionManager("qq", "week")
    w1 = week.session_id("A", datetime(2026, 9, 21, tzinfo=timezone.utc))
    w2 = week.session_id("A", datetime(2026, 9, 28, tzinfo=timezone.utc))
    check("week 桶跨周换会话", w1 != w2, f"{w1} vs {w2}")
    check("week 桶同周不变",
          w1 == week.session_id("A", datetime(2026, 9, 24, tzinfo=timezone.utc)))
    check("week 桶合法字符", all(c.isalnum() or c in "-._" for c in w1), w1)

    none_sm = SessionManager("qq", "none")
    check("none 不轮换", none_sm.session_id("A", d1) == none_sm.session_id("A", d2))
    check("自定义前缀生效", SessionManager("ebk", "none").session_id("A").startswith("ebk-"))

    try:
        SessionManager("qq", "hour")
    except ValueError:
        check("非法 rotation 抛错", True)
    else:
        check("非法 rotation 抛错", False)


def test_deduplicator() -> None:
    print("\n[用例2] Deduplicator")
    dd = Deduplicator(ttl=1.0)
    check("首次不算重复", dd.is_duplicate("m1") is False)
    check("第二次算重复", dd.is_duplicate("m1") is True)
    check("不同 key 不影响", dd.is_duplicate("m2") is False)
    check("空 key 永远不算重复", dd.is_duplicate("") is False and dd.is_duplicate("") is False)
    check("过 TTL 后不再算重复", dd.is_duplicate("m1", now=time.time() + 5) is False)
    dd.forget("m2")
    check("forget 后可再次处理", dd.is_duplicate("m2") is False)

    small = Deduplicator(ttl=9999, max_entries=8)
    for i in range(40):
        small.is_duplicate(f"k{i}")
    check("容量上限生效", len(small) <= 8, str(len(small)))


def test_build_prompt(tmp: Path) -> None:
    print("\n[用例3] build_prompt")
    cfg = make_config(tmp)

    # 纯文本
    prompt, images = build_prompt(msg_event("记 30 午饭"), cfg)
    check("含当前时间", "[当前时间] 2026-09-23T23:51:24+08:00" in prompt, prompt)
    check("含确认阈值", "[策略] 确认阈值 1000" in prompt, prompt)
    check("含用户 openid", "[用户] OPENID-A" in prompt, prompt)
    check("含消息正文", "[消息] 记 30 午饭" in prompt, prompt)
    check("纯文本无图片", images == [], repr(images))
    # transactions-add 要 unix 秒 + utcOffset 分钟，由 bot 算好注入，不让模型换算
    check("注入 unix 与 utcOffset", "[时间] unix=1790178684 utcOffset=480" in prompt, prompt)
    check("注入 ebktools 绝对路径",
          "[工具] " in prompt
          and prompt.split("[工具] ", 1)[1].splitlines()[0].endswith("ebktools.sh"),
          prompt)

    # 阈值 0 -> 不注入
    cfg0 = make_config(tmp, confirm_amount_threshold=0)
    p0, _ = build_prompt(msg_event("x"), cfg0)
    check("阈值 0 时不注入策略行", "[策略]" not in p0, p0)

    # 引用 + 已落盘图片
    img = tmp / "media"
    img.mkdir(parents=True, exist_ok=True)
    real = img / "receipt.jpg"
    real.write_bytes(b"x")
    ev = msg_event("午饭")
    ev.quote = ResolvedQuote(
        ref_key="REFIDX_X==", source="both",
        text="[image: receipt.jpg]",
        attachments=[RefAttachment(type="image", filename="receipt.jpg", local_path=str(real))],
    )
    prompt, images = build_prompt(ev, cfg)
    check("引用文本进 prompt", "[引用] [image: receipt.jpg]" in prompt, prompt)
    check("图片进 images", images == [str(real.resolve())], repr(images))
    check("图片列进 [附件]", f"[附件] {real.resolve()}" in prompt, prompt)

    # 引用解析失败
    ev2 = msg_event("午饭")
    ev2.quote = ResolvedQuote(ref_key="REFIDX_Y==", source="none", text="", hint="[图片]")
    prompt2, images2 = build_prompt(ev2, cfg)
    check("解析失败会说明", "未能解析" in prompt2 and "[图片]" in prompt2, prompt2)
    check("解析失败无图片", images2 == [])
    check("解析失败提示不要猜", "不要猜" in prompt2, prompt2)

    # 图片文件不存在 -> 跳过
    ev3 = msg_event("午饭")
    ev3.quote = ResolvedQuote(
        ref_key="R", source="store", text="x",
        attachments=[RefAttachment(type="image", filename="gone.jpg", local_path=str(img / "gone.jpg"))],
    )
    _, images3 = build_prompt(ev3, cfg)
    check("不存在的附件被跳过", images3 == [], repr(images3))

    # 相对路径 -> 绝对路径（相对于 bot 的 cwd）
    rel = Path("data/media/rel.jpg")
    abs_rel = (Path.cwd() / rel).resolve()
    abs_rel.parent.mkdir(parents=True, exist_ok=True)
    abs_rel.write_bytes(b"x")
    try:
        ev4 = msg_event("午饭")
        ev4.quote = ResolvedQuote(ref_key="R", source="store", text="x",
                                  attachments=[RefAttachment(type="image", local_path=str(rel))])
        _, images4 = build_prompt(ev4, cfg)
        check("相对路径转成绝对路径", images4 == [str(abs_rel)], repr(images4))
    finally:
        abs_rel.unlink(missing_ok=True)


def test_parse_event_time() -> None:
    print("\n[用例3b] parse_event_time（把 RFC3339 变成 unix + 偏移，避免模型换算）")
    check("带 +08:00", parse_event_time("2026-09-23T23:51:24+08:00") == (1790178684, 480),
          repr(parse_event_time("2026-09-23T23:51:24+08:00")))
    check("UTC 的 Z 后缀", parse_event_time("2026-09-23T15:51:24Z") == (1790178684, 0),
          repr(parse_event_time("2026-09-23T15:51:24Z")))
    check("负偏移", parse_event_time("2026-09-23T08:51:24-07:00")[1] == -420,
          repr(parse_event_time("2026-09-23T08:51:24-07:00")))
    check("无时区返回 None", parse_event_time("2026-09-23T23:51:24") is None)
    check("乱字符串返回 None", parse_event_time("not-a-time") is None)
    check("空值返回 None", parse_event_time("") is None and parse_event_time(None) is None)


def test_ebktools_path_and_validate(tmp: Path) -> None:
    print("\n[用例3c] ebktools 路径注入与启动自检")
    tools = tmp / "ebk" / "ebktools.sh"
    tools.parent.mkdir(parents=True, exist_ok=True)
    tools.write_text("#!/bin/sh\n", encoding="utf-8")

    cfg = make_config(tmp, ebktools_path=str(tools))
    prompt, _ = build_prompt(msg_event("x"), cfg)
    check("绝对路径原样注入", f"[工具] {tools.resolve()}" in prompt, prompt)

    # 默认相对路径按 bot 的 cwd 解析
    cfg2 = make_config(tmp, ebktools_path=".agents/skills/ezbookkeeping/scripts/ebktools.sh")
    prompt2, _ = build_prompt(msg_event("x"), cfg2)
    expected = (Path.cwd() / ".agents/skills/ezbookkeeping/scripts/ebktools.sh").resolve()
    check("相对路径按 bot cwd 解析", f"[工具] {expected}" in prompt2, prompt2)

    # validate：cwd 与 ebktools 都就绪时无问题
    runner = AgentRunner(make_config(tmp, ebktools_path=str(tools)))
    check("一切就绪时自检无问题", runner.validate() == [], repr(runner.validate()))

    # cwd 不存在
    bad = AgentRunner(make_config(tmp, agent_cwd=str(tmp / "nope"), ebktools_path=str(tools)))
    check("cwd 不存在能被检出", any("cwd 不存在" in p for p in bad.validate()), repr(bad.validate()))

    # cwd 存在但没有 AGENTS.md
    empty = tmp / "empty_ws"
    empty.mkdir(exist_ok=True)
    no_agents = AgentRunner(make_config(tmp, agent_cwd=str(empty), ebktools_path=str(tools)))
    check("缺 AGENTS.md 能被检出",
          any("AGENTS.md" in p for p in no_agents.validate()), repr(no_agents.validate()))

    # ebktools 不存在
    no_tools = AgentRunner(make_config(tmp, ebktools_path=str(tmp / "missing.sh")))
    check("ebktools 缺失能被检出",
          any("ebktools" in p for p in no_tools.validate()), repr(no_tools.validate()))

    # 真实仓库里的路径应该存在（回归：别把默认值写错）
    real = BotConfig(app_id="1", client_secret="s")
    from qqbot.agent import resolve_passthrough_path
    check("默认 ebktools_path 指向真实文件",
          resolve_passthrough_path(real.ebktools_path).is_file(),
          str(resolve_passthrough_path(real.ebktools_path)))


def test_agent_env(tmp: Path) -> None:
    print("\n[用例4] build_agent_env")
    cfg = make_config(tmp)
    base = {
        "PATH": "/usr/bin", "HOME": "/home/x",
        "QQ_BOT_CLIENT_SECRET": "SHOULD-NOT-LEAK",
        "EBKTOOL_TOKEN": "tok", "EBKTOOL_SERVER_BASEURL": "http://e",
        "RANDOM_VAR": "nope",
    }
    env = build_agent_env(cfg, base)
    check("透传 PATH", env.get("PATH") == "/usr/bin")
    check("QQ secret 不透传", "QQ_BOT_CLIENT_SECRET" not in env, repr(env))
    check("无关变量不透传", "RANDOM_VAR" not in env, repr(env))
    check("EBKTOOL_TOKEN 强制透传", env.get("EBKTOOL_TOKEN") == "tok")
    check("EBKTOOL_SERVER_BASEURL 强制透传", env.get("EBKTOOL_SERVER_BASEURL") == "http://e")
    check("注入 TZ", env.get("TZ") == cfg.timezone, repr(env.get("TZ")))

    # --- 前缀通配：第三方集成（Langfuse）会加新变量，逐个列举不现实 ---
    lf = {
        "PATH": "/usr/bin",
        "LANGFUSE_PUBLIC_KEY": "pk-lf-1",
        "LANGFUSE_SECRET_KEY": "sk-lf-1",
        "LANGFUSE_BASE_URL": "https://cloud.langfuse.com",
        "LANGFUSE_TRACING_ENVIRONMENT": "production",
        "LANGFUSE_SOME_FUTURE_VAR": "future",   # 以后新增的也要能透传
        "PI_LANGFUSE_DEBUG": "true",
        "EBKTOOL_TOKEN": "tok",
        "QQ_BOT_CLIENT_SECRET": "SHOULD-NOT-LEAK",
    }
    env2 = build_agent_env(cfg, lf)
    check("LANGFUSE_* 前缀透传（已列举的）", env2.get("LANGFUSE_PUBLIC_KEY") == "pk-lf-1")
    check("LANGFUSE_* 前缀透传（密钥）", env2.get("LANGFUSE_SECRET_KEY") == "sk-lf-1")
    check("LANGFUSE_* 前缀透传（新增的未列举变量）", env2.get("LANGFUSE_SOME_FUTURE_VAR") == "future")
    check("PI_LANGFUSE_DEBUG 也能透传", env2.get("PI_LANGFUSE_DEBUG") == "true")
    check("前缀通配不影响 secret 隔离", "QQ_BOT_CLIENT_SECRET" not in env2, repr(env2))
    check("前缀不会误拉到相似名", "QQ_BOT_LANGFUSE_X" not in env2)

    # 自己配白名单时，包括前缀写法
    cfg3 = make_config(tmp, agent_passthrough_env=("PATH", "MY_*"))
    env3 = build_agent_env(cfg3, {"PATH": "/p", "MY_A": "1", "MY_B": "2", "OTHER": "3"})
    check("自定义前缀生效", env3.get("MY_A") == "1" and env3.get("MY_B") == "2", repr(env3))
    check("自定义白名单外不泄", "OTHER" not in env3, repr(env3))
    check("EBKTOOL 仍被强保证", build_agent_env(cfg3, {"EBKTOOL_TOKEN": "t"}).get("EBKTOOL_TOKEN") == "t")

    # 默认配置应当涵盖我们依赖的两组变量
    default = BotConfig(app_id="1", client_secret="s")
    check("默认含 LANGFUSE_* 前缀", "LANGFUSE_*" in default.agent_passthrough_env,
          repr(default.agent_passthrough_env))
    check("默认含 PI_CODING_AGENT_DIR", "PI_CODING_AGENT_DIR" in default.agent_passthrough_env)


def test_build_argv(tmp: Path) -> None:
    print("\n[用例5] AgentRunner.build_argv")
    cfg = make_config(tmp, agent_model="multimodal:high", agent_extra_args=("--thinking", "high"))
    runner = AgentRunner(cfg)
    argv = runner.build_argv("qq-abc-20260923", "hello", ["/a.jpg", "/b.png"])
    check("以命令开头", argv[0] == cfg.agent_command, argv[0])
    check("含 --print", "--print" in argv)
    check("含 --session-id", argv[argv.index("--session-id") + 1] == "qq-abc-20260923")
    check("含 --tools", argv[argv.index("--tools") + 1] == "bash,read")
    check("含 --model", argv[argv.index("--model") + 1] == "multimodal:high")
    check("含额外参数", "--thinking" in argv and "high" in argv)
    check("-- 在 @文件 之前", argv.index("--") < argv.index("@/a.jpg"), repr(argv))
    check("@ 文件排在消息之前", argv.index("@/b.png") < argv.index("hello"), repr(argv))
    check("消息是最后一个参数", argv[-1] == "hello")

    no_session = AgentRunner(make_config(tmp, agent_model=None)).build_argv("", "hi", [])
    check("无 session 时不带该参数", "--session-id" not in no_session, repr(no_session))
    check("无 model 时不带该参数", "--model" not in no_session, repr(no_session))


def test_runner_run(tmp: Path) -> None:
    print("\n[用例6] AgentRunner.run（用假可执行文件）")
    ok_cmd = fake_executable(tmp, """
        import sys
        print("记好了：30 元 / 餐饮 / 支付宝，余额 1234.56")
    """)
    cfg = make_config(tmp, agent_command=ok_cmd)
    res = AgentRunner(cfg).run("记 30 午饭", session_id="s1", images=[])
    check("成功时 ok=True", res.ok is True, res.error)
    check("拿到文本", "记好了" in res.text, repr(res.text))
    check("usable=True", res.usable is True)
    check("记录了耗时", res.duration > 0)
    check("记录了 session", res.session_id == "s1")

    empty_cmd = fake_executable(tmp, "pass", name="empty_pi")
    res2 = AgentRunner(make_config(tmp, agent_command=empty_cmd)).run("x")
    check("无输出时 ok=False", res2.ok is False, repr(res2))
    check("无输出时 usable=False", res2.usable is False)

    fail_cmd = fake_executable(tmp, """
        import sys
        print("boom", file=sys.stderr)
        sys.exit(3)
    """, name="fail_pi")
    res3 = AgentRunner(make_config(tmp, agent_command=fail_cmd)).run("x")
    check("非零退出码 -> ok=False", res3.ok is False)
    check("保留 returncode", res3.returncode == 3, repr(res3.returncode))
    check("保留 stderr 尾部", "boom" in res3.stderr_tail, repr(res3.stderr_tail))

    slow_cmd = fake_executable(tmp, """
        import time
        time.sleep(5)
        print("too late")
    """, name="slow_pi")
    res4 = AgentRunner(make_config(tmp, agent_command=slow_cmd, agent_timeout=0.6)).run("x")
    check("超时标记", res4.timed_out is True, repr(res4))
    check("超时时 ok=False", res4.ok is False)
    check("超时有人话错误", "超时" in res4.error, repr(res4.error))

    res5 = AgentRunner(make_config(tmp, agent_command=str(tmp / "nope_binary"))).run("x")
    check("找不到可执行文件有明确错误", "找不到" in res5.error, repr(res5.error))
    check("找不到文件不抛异常", res5.ok is False)

    bad_cwd = make_config(tmp, agent_command=ok_cmd, agent_cwd=str(tmp / "not-exist"))
    res6 = AgentRunner(bad_cwd).run("x")
    check("cwd 不存在有明确错误", "工作目录不存在" in res6.error, repr(res6.error))


def test_sanitize_reply() -> None:
    print("\n[用例7] sanitize_reply")
    check("截断并加省略号", len(sanitize_reply("x" * 600, 100)) == 100)
    check("空输入返回空", sanitize_reply("") == "")
    check("压掉多余空行", sanitize_reply("a\n\n\n\nb") == "a\n\nb")
    leaked = sanitize_reply("见 /opt/qq-bookkeeping/.env 和 Bearer abcdefgh12345678")
    check("隐藏绝对路径", "/opt/qq-bookkeeping" not in leaked, leaked)
    check("隐藏 Bearer", "abcdefgh12345678" not in leaked, leaked)
    check("隐藏 EBKTOOL 变量", "deadbeef" not in sanitize_reply("EBKTOOL_TOKEN=deadbeef"), "")


# --------------------------------------------------------------------------- 调度
class StubRunner:
    """冒充 AgentRunner，记录调用并唤醒等待者。"""

    def __init__(self, result: AgentResult, done: threading.Event):
        self.result = result
        self.done = done
        self.calls: list[tuple[str, str, list[str]]] = []

    def run(self, prompt, *, session_id="", images=None):
        self.calls.append((prompt, session_id, list(images or ())))
        self.done.set()
        return self.result


def make_bot(tmp: Path, runner: StubRunner, **overrides) -> tuple[QQBot, list]:
    cfg = make_config(tmp, **overrides)
    bot = QQBot(cfg)
    bot.agent = runner  # type: ignore[assignment]
    replies: list[tuple[Event, str]] = []
    bot.reply = lambda event, content, **kw: replies.append((event, content))  # type: ignore[assignment]
    return bot, replies


def test_dispatch(tmp: Path) -> None:
    print("\n[用例8] QQBot.dispatch_to_agent")

    # --- 纯图片消息：不启 agent、不回复 ---
    done = threading.Event()
    stub = StubRunner(AgentResult(ok=True, text="不该出现"), done)
    bot, replies = make_bot(tmp, stub)
    ev_img = image_only_event(tmp)
    check("纯图片返回 False", bot.dispatch_to_agent(ev_img) is False)
    check("纯图片不调用 agent", stub.calls == [], repr(stub.calls))
    check("纯图片不回复", replies == [], repr(replies))
    bot.stop()

    # --- 正常文本：提交并回复 ---
    done2 = threading.Event()
    stub2 = StubRunner(AgentResult(ok=True, text="记好了：30 元 / 餐饮"), done2)
    bot2, replies2 = make_bot(tmp, stub2)
    ok = bot2.dispatch_to_agent(msg_event("记 30 午饭"))
    check("文本消息返回 True", ok is True)
    check("等待 agent 完成", done2.wait(5))
    deadline = time.time() + 5
    while not replies2 and time.time() < deadline:
        time.sleep(0.02)
    check("agent 被调用一次", len(stub2.calls) == 1, repr(stub2.calls))
    check("prompt 含消息正文", "记 30 午饭" in stub2.calls[0][0], stub2.calls[0][0] if stub2.calls else "")
    check("session 按用户生成", stub2.calls[0][1].startswith("qq-"), stub2.calls[0][1] if stub2.calls else "")
    check("结果被回复", bool(replies2) and "记好了" in replies2[0][1], repr(replies2))

    # --- 重复推送 ---
    check("重复 msg_id 被忽略", bot2.dispatch_to_agent(msg_event("记 30 午饭")) is False)
    bot2.stop()

    # --- 机器人自己发的 ---
    done3 = threading.Event()
    stub3 = StubRunner(AgentResult(ok=True, text="x"), done3)
    bot3, _ = make_bot(tmp, stub3)
    ev_bot = msg_event("hi", msg_id="MSG-BOT")
    ev_bot.data["author"] = {"user_openid": "OPENID-A", "bot": True}
    check("机器人自己的消息被忽略", bot3.dispatch_to_agent(ev_bot) is False)
    bot3.stop()

    # --- 白名单 ---
    done4 = threading.Event()
    stub4 = StubRunner(AgentResult(ok=True, text="x"), done4)
    bot4, _ = make_bot(tmp, stub4, allowed_openids=("OTHER",))
    check("不在白名单被忽略", bot4.dispatch_to_agent(msg_event("hi", msg_id="MSG-W")) is False)
    bot4.stop()

    # --- agent 关闭 ---
    done5 = threading.Event()
    stub5 = StubRunner(AgentResult(ok=True, text="x"), done5)
    bot5, _ = make_bot(tmp, stub5, agent_enabled=False)
    check("agent 关闭时不处理", bot5.dispatch_to_agent(msg_event("hi", msg_id="MSG-OFF")) is False)
    bot5.stop()

    # --- 失败结果也会回复人话 ---
    done6 = threading.Event()
    stub6 = StubRunner(AgentResult(ok=False, timed_out=True, error="超时"), done6)
    bot6, replies6 = make_bot(tmp, stub6)
    bot6.dispatch_to_agent(msg_event("记 30 午饭", msg_id="MSG-T"))
    check("等待超时任务", done6.wait(5))
    deadline = time.time() + 5
    while not replies6 and time.time() < deadline:
        time.sleep(0.02)
    check("超时回复人话", bool(replies6) and "超时" in replies6[0][1], repr(replies6))
    bot6.stop()


def test_concurrent_same_user(tmp: Path) -> None:
    print("\n[用例9] 同一用户并发保护")
    release = threading.Event()
    started = threading.Event()

    class BlockingRunner:
        def __init__(self):
            self.count = 0

        def run(self, prompt, *, session_id="", images=None):
            self.count += 1
            started.set()
            release.wait(5)
            return AgentResult(ok=True, text="done")

    runner = BlockingRunner()
    cfg = make_config(tmp, agent_max_concurrency=4)
    bot = QQBot(cfg)
    bot.agent = runner  # type: ignore[assignment]
    replies = []
    bot.reply = lambda event, content, **kw: replies.append(content)  # type: ignore[assignment]

    first = bot.dispatch_to_agent(msg_event("第一条", msg_id="M-1"))
    check("第一条已提交", first is True)
    check("agent 已开始", started.wait(5))

    second = bot.dispatch_to_agent(msg_event("第二条", msg_id="M-2"))
    check("同用户第二条被拒", second is False)
    check("拒绝时给出提示", any("还在处理" in r for r in replies), repr(replies))
    check("agent 只跑了一次", runner.count == 1, str(runner.count))

    release.set()
    deadline = time.time() + 5
    while bot.inflight_agents and time.time() < deadline:
        time.sleep(0.02)
    check("释放后可再次处理", bot.inflight_agents == 0)
    bot.stop()


def main() -> int:
    print("=" * 62)
    print("  agent 层自测")
    print("=" * 62)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        test_session_manager()
        test_deduplicator()
        test_build_prompt(tmp)
        test_parse_event_time()
        test_ebktools_path_and_validate(tmp)
        test_agent_env(tmp)
        test_build_argv(tmp)
        test_runner_run(tmp)
        test_sanitize_reply()
        test_dispatch(tmp)
        test_concurrent_same_user(tmp)

    failed = [c for c in CHECKS if not c[1]]
    print("\n" + "=" * 62)
    print(f"  通过 {len(CHECKS) - len(failed)}/{len(CHECKS)}")
    if failed:
        print("  失败项:")
        for name, _ok, detail in failed:
            print(f"    - {name} {detail}")
        return 1
    print("  全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
