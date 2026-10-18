"""cli-gateway — 统一 CLI 进程调用网关：全量日志落盘 + 回放 + 结构化返回。

v1 范围：本地 subprocess 包装，不涉及任何 URL/网络请求。
设计目标：让"AI 调 CLI 是否真跑、输出能否逐字核对"变得可观测、可审计、可回放。
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

DEFAULT_LOG_DIR = Path(os.environ.get("CLI_GATEWAY_LOG_DIR", "~/.cli-gateway/logs")).expanduser()


@dataclass
class GatewayResult:
    """一次 CLI 调用的结构化返回。"""

    command: str
    exit_code: int
    duration_ms: int
    log_path: str
    stdout_tail: str
    stderr_tail: str
    timestamp: str
    pid: int
    success: bool

    def to_dict(self) -> dict:
        return asdict(self)


def _safe_component(cmd: Sequence[str]) -> str:
    """取命令首段用于文件名（防路径穿越）。"""
    head = cmd[0] if cmd else "unknown"
    return Path(head).name.replace("/", "_").replace("\\", "_")[:40]


def _write_log(log_path: Path, cmd: Sequence[str], result: subprocess.CompletedProcess, start: float) -> None:
    """把一次执行全量落盘：元数据头 + stdout + stderr 分段，可回放。"""
    with open(log_path, "w", encoding="utf-8", errors="replace") as f:
        f.write(f"# command: {shlex.join(cmd)}\n")
        f.write(f"# timestamp: {datetime.now().isoformat(timespec='seconds')}\n")
        f.write(f"# exit_code: {result.returncode}\n")
        f.write(f"# duration_ms: {int((time.monotonic() - start) * 1000)}\n")
        f.write(f"# pid: {result.pid if hasattr(result, 'pid') else 'n/a'}\n")
        f.write("--- STDOUT ---\n")
        f.write(result.stdout)
        if result.stdout and not result.stdout.endswith("\n"):
            f.write("\n")
        f.write("--- STDERR ---\n")
        f.write(result.stderr)
        if result.stderr and not result.stderr.endswith("\n"):
            f.write("\n")
        f.write("--- END ---\n")


def run(
    cmd: Sequence[str],
    *,
    cwd: str | os.PathLike | None = None,
    timeout: float | None = None,
    log_dir: str | os.PathLike | None = None,
    capture: bool = True,
    tail: int = 2000,
) -> GatewayResult:
    """执行一条 CLI 命令，全量落盘并返回结构化结果。

    - stdout/stderr 全量写入 log 文件（可回放、可审计）
    - 返回结构化 GatewayResult（exit_code / 耗时 / log_path / 输出尾部）
    - 纯本地 subprocess，不发起任何网络请求
    """
    log_root = Path(log_dir) if log_dir else DEFAULT_LOG_DIR
    log_root.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id = uuid.uuid4().hex[:8]
    log_path = log_root / f"{ts}-{_safe_component(cmd)}-{run_id}.log"

    start = time.monotonic()
    try:
        proc = subprocess.run(
            list(cmd),
            cwd=str(cwd) if cwd else None,
            capture_output=capture,
            text=True,
            timeout=timeout,
        )
        _write_log(log_path, cmd, proc, start)
        return GatewayResult(
            command=shlex.join(list(cmd)),
            exit_code=proc.returncode,
            duration_ms=int((time.monotonic() - start) * 1000),
            log_path=str(log_path),
            stdout_tail=proc.stdout[-tail:] if proc.stdout else "",
            stderr_tail=proc.stderr[-tail:] if proc.stderr else "",
            timestamp=datetime.now().isoformat(timespec="seconds"),
            pid=getattr(proc, "pid", 0),
            success=proc.returncode == 0,
        )
    except subprocess.TimeoutExpired as exc:
        # 超时：写日志并返回非零 + 错误信息
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"# command: {shlex.join(list(cmd))}\n")
            f.write(f"# error: TIMEOUT after {timeout}s\n")
        return GatewayResult(
            command=shlex.join(list(cmd)),
            exit_code=-1,
            duration_ms=int((time.monotonic() - start) * 1000),
            log_path=str(log_path),
            stdout_tail=str(exc),
            stderr_tail="",
            timestamp=datetime.now().isoformat(timespec="seconds"),
            pid=0,
            success=False,
        )
    except FileNotFoundError as exc:
        return GatewayResult(
            command=shlex.join(list(cmd)),
            exit_code=-1,
            duration_ms=int((time.monotonic() - start) * 1000),
            log_path="",
            stdout_tail=f"command not found: {exc}",
            stderr_tail="",
            timestamp=datetime.now().isoformat(timespec="seconds"),
            pid=0,
            success=False,
        )


def replay(log_path: str | os.PathLike) -> str:
    """回放一次已记录的 CLI 调用（打印完整日志）。"""
    p = Path(log_path)
    if not p.is_file():
        raise FileNotFoundError(f"log not found: {p}")
    return p.read_text(encoding="utf-8", errors="replace")


def list_logs(log_dir: str | os.PathLike | None = None) -> list[Path]:
    """列出全部调用日志（按时间倒序）。"""
    root = Path(log_dir) if log_dir else DEFAULT_LOG_DIR
    if not root.is_dir():
        return []
    return sorted(root.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)


__all__ = [
    "GatewayResult",
    "run",
    "replay",
    "list_logs",
    "DEFAULT_LOG_DIR",
]
