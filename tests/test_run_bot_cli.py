#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""run_bot.py 命令行入口自测。

重点回归：这个脚本以前**没有参数解析** —— 随手敲的 `--check` 会被静默忽略，
然后把机器人真的拉起来连上 QQ。现在未知参数必须报错退出，`--check` 必须
只做自检、不碰网络。

全程离线：只用临时 .env + 临时工作目录，不建任何真实连接。

运行： python tests/test_run_bot_cli.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qqbot import QQBot, __version__  # noqa: E402

# scripts/ 不是包，按路径加载。模块底部有 __main__ 守卫，不会被误跑
_spec = importlib.util.spec_from_file_location("run_bot_under_test", ROOT / "scripts" / "run_bot.py")
assert _spec and _spec.loader
run_bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_bot)


CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}" + (f"  -> {detail}" if detail and not ok else ""))


# --------------------------------------------------------------------------- 工具
ENV_KEYS = (
    "QQ_BOT_APP_ID", "QQ_BOT_CLIENT_SECRET", "QQ_BOT_LOG_LEVEL",
    "QQ_BOT_REF_INDEX_PATH", "QQ_BOT_AUTO_DOWNLOAD_DIR", "QQ_BOT_AGENT_CWD",
    "QQ_BOT_AGENT_ENABLED", "QQ_BOT_SANDBOX",
)


@contextlib.contextmanager
def isolated_env():
    """把 QQ_BOT_* 从进程环境里摘掉，结束后还原。

    必须做：load_env_file 默认**不覆盖**已存在的环境变量，
    留着上一轮的残留会让后面的用例结果不可信。
    """
    saved = {k: os.environ.pop(k, None) for k in ENV_KEYS}
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def write_env(tmp: Path, *, agent_cwd: Path, with_secret: bool = True) -> Path:
    lines = ["QQ_BOT_APP_ID=12345"]
    if with_secret:
        lines.append("QQ_BOT_CLIENT_SECRET=test-secret")
    lines += [
        "QQ_BOT_LOG_LEVEL=CRITICAL",  # 别把日志刷进测试输出
        f"QQ_BOT_REF_INDEX_PATH={tmp / 'ref-index.jsonl'}",
        f"QQ_BOT_AUTO_DOWNLOAD_DIR={tmp / 'media'}",
        f"QQ_BOT_AGENT_CWD={agent_cwd}",
    ]
    path = tmp / "cli.env"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    """跑 main(argv)，捕获 stdout/stderr 与 SystemExit。"""
    out, err = io.StringIO(), io.StringIO()
    rc: int
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = run_bot.main(argv)
        except SystemExit as exc:  # argparse 的 --help / 出错走这里
            rc = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return rc, out.getvalue(), err.getvalue()


# --------------------------------------------------------------------------- 用例
def test_cli(tmp: Path) -> None:
    print("\n[用例1] 命令行参数")

    with isolated_env():
        # --help
        rc, out, _ = run_cli(["--help"])
        check("--help 退出码 0", rc == 0, f"rc={rc}")
        check("--help 提到 --check", "--check" in out and "--env-file" in out)

        # --version
        rc, out, _ = run_cli(["--version"])
        check("--version 退出码 0", rc == 0, f"rc={rc}")
        check("--version 打印版本", __version__ in out, repr(out))

        # 未知参数：**这是本次修的回归点**
        rc, _, err = run_cli(["--checkk"])
        check("未知参数退出码 2", rc == 2, f"rc={rc}")
        check("未知参数有提示", "unrecognized arguments" in err, repr(err))

        # 典型误用：把 --check 拼错成 --dry-run，绝不能静默启动
        rc, _, err = run_cli(["--dry-run"])
        check("拼错的 --dry-run 被拒（曾经会真的启动机器人）", rc == 2, f"rc={rc}")


def test_check_mode(tmp: Path) -> None:
    print("\n[用例2] --check 只自检")
    ws = tmp / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "AGENTS.md").write_text("# test\n", encoding="utf-8")
    env_file = write_env(tmp, agent_cwd=ws)

    # 一旦 --check 去连网关，这里会炸
    original_run = QQBot.run

    def forbidden(self):  # noqa: ANN001
        raise AssertionError("--check 不该调用 QQBot.run（会真的连上 QQ）")

    QQBot.run = forbidden  # type: ignore[method-assign]
    try:
        with isolated_env():
            rc, out, _ = run_cli(["--check", "--env-file", str(env_file)])
    finally:
        QQBot.run = original_run  # type: ignore[method-assign]

    check("--check 退出码 0（自检通过）", rc == 0, f"rc={rc}")
    check("--check 没连网关", rc == 0, f"rc={rc}（run() 被调用过）")
    check("--check 打印摘要", "QQ 记账机器人" in out, repr(out[:120]))
    check("--check 打印自检结论", "[自检] agent 运行环境就绪" in out, repr(out[-200:]))
    check("--check 没有上线日志", "机器人已上线" not in out, repr(out[-200:]))


def test_check_detects_problems(tmp: Path) -> None:
    print("\n[用例3] --check 能发现问题")
    empty_ws = tmp / "empty-ws"
    empty_ws.mkdir(parents=True, exist_ok=True)
    # 故意不放 AGENTS.md
    sub = tmp / "sub"
    sub.mkdir(exist_ok=True)
    env_file = write_env(sub, agent_cwd=empty_ws)

    with isolated_env():
        rc, out, _ = run_cli(["--check", "--env-file", str(env_file)])
    check("缺 AGENTS.md 时退出码 1", rc == 1, f"rc={rc}")
    check("缺 AGENTS.md 时有 [警告]", "[警告]" in out, repr(out[-300:]))


def test_config_errors(tmp: Path) -> None:
    print("\n[用例4] 配置错误")
    ws = tmp / "ws2"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "AGENTS.md").write_text("# t\n", encoding="utf-8")

    # 缺 client_secret
    bad = write_env(tmp, agent_cwd=ws, with_secret=False)
    with isolated_env():
        rc, _, err = run_cli(["--check", "--env-file", str(bad)])
    check("缺 client_secret 退出码 2", rc == 2, f"rc={rc}")
    check("缺 client_secret 给人话", "配置错误" in err and "client_secret" in err, repr(err))
    check("配置错误不吐 traceback", "Traceback" not in err, repr(err[:200]))

    # 文件不存在 = 不读文件，全靠环境变量 → 这里应当报缺 app_id
    with isolated_env():
        rc, _, err = run_cli(["--check", "--env-file", str(tmp / "nope.env")])
    check("不存在的 env 文件按“不读”处理", rc == 2, f"rc={rc}")

    # --env-file "" 同样表示不读文件
    with isolated_env():
        rc, _, _ = run_cli(["--check", "--env-file", ""])
    check('--env-file "" 也可用', rc == 2, f"rc={rc}")


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        test_cli(tmp)
        test_check_mode(tmp)
        test_check_detects_problems(tmp)
        test_config_errors(tmp)

    passed = sum(1 for _, ok, _ in CHECKS if ok)
    total = len(CHECKS)
    print("\n" + "=" * 60)
    print(f"  通过 {passed}/{total}")
    if passed != total:
        print("  失败项：")
        for name, ok, detail in CHECKS:
            if not ok:
                print(f"    - {name}  {detail}")
        return 1
    print("  全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
