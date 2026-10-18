"""cli-gateway 测试：验证落盘、回放、结构化返回、超时、失败。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cli_gateway import list_logs, replay, run  # noqa: E402


def _tmp_logdir(tmp_path: Path) -> str:
    d = tmp_path / "logs"
    d.mkdir()
    return str(d)


def test_success_logs_and_replay(tmp_path: Path) -> None:
    d = _tmp_logdir(tmp_path)
    res = run(["echo", "hello-gateway"], log_dir=d)
    assert res.success is True
    assert res.exit_code == 0
    assert "hello-gateway" in res.stdout_tail
    # 日志落盘且可回放
    assert Path(res.log_path).is_file()
    assert "hello-gateway" in replay(res.log_path)
    # 日志可列出
    assert res.log_path in [str(p) for p in list_logs(d)]


def test_failure_captures_stderr(tmp_path: Path) -> None:
    d = _tmp_logdir(tmp_path)
    # 用 python 保证跨平台（Windows 上 bash 可能不可用）
    res = run(
        [sys.executable, "-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(3)"],
        log_dir=d,
    )
    assert res.success is False
    assert res.exit_code == 3
    assert "boom" in res.stderr_tail
    assert "boom" in replay(res.log_path)


def test_command_not_found(tmp_path: Path) -> None:
    d = _tmp_logdir(tmp_path)
    res = run(["definitely-not-a-real-cmd-xyz"], log_dir=d)
    assert res.success is False
    assert "not found" in res.stdout_tail


def test_timeout(tmp_path: Path) -> None:
    d = _tmp_logdir(tmp_path)
    res = run(["sleep", "5"], timeout=0.2, log_dir=d)
    assert res.success is False
    assert "TIMEOUT" in replay(res.log_path)
