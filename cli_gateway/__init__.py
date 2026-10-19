"""cli-gateway — 统一 CLI 进程调用网关：全量日志落盘 + 回放 + 结构化返回。

v1.1 范围：本地 subprocess 包装（Popen 直控 + 进程组终止 + 流式 tee），不涉及任何 URL/网络请求。
设计目标：让"AI 调 CLI 是否真跑、输出能否逐字核对"变得可观测、可审计、可回放。

GatewayResult.state ∈ {completed, timed_out, not_found, spawn_error, killed}
（spawn_error 是对 shell 惯例枚举的扩展：找到但无法启动，见下方退出码）。
退出码语义：命令未找到=127，启动失败=126，超时=124，
其余为命令真实退出码（POSIX 被信号杀死时为负信号值）。
子进程 env 默认注入 PYTHONUTF8=1（调用方或父环境已显式设置时不覆盖），
使 python 类子进程在 Windows 传统代码页（CP936 等）环境下仍输出 UTF-8。
"""

from __future__ import annotations

import codecs
import math
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

__version__ = "1.6.1"

from . import events as _events

DEFAULT_LOG_DIR = Path(os.environ.get("CLI_GATEWAY_LOG_DIR", "~/.cli-gateway/logs")).expanduser()

_PUMP_CHUNK = 65536       # 流式泵每次从管道读取的字节数（内存有界）
_READER_JOIN_S = 10.0     # 等待 reader 线程正常 EOF 的宽限；打满视为异常出口
_KILL_GRACE_S = 2.0       # SIGTERM 后等待进程组自行退出的宽限期（POSIX）
_EXIT_NOT_FOUND = 127     # shell 惯例：命令未找到
_EXIT_SPAWN_ERROR = 126   # shell 惯例：找到但无法启动/不可执行
_EXIT_TIMEOUT = 124       # shell 惯例：超时
_SPAWN_FAILURE_STATES = ("not_found", "spawn_error")  # 允许重试的"启动前失败"
_SENSITIVE_MARKS = ("KEY", "TOKEN", "SECRET", "PASS", "CREDENTIAL", "PAT", "AUTH")


@dataclass
class GatewayResult:
    """一次 CLI 调用的结构化返回。

    旧字段（v0.1 契约，名字与含义不变）：command/exit_code/duration_ms/log_path/
    stdout_tail/stderr_tail/timestamp/pid/success。
    v1.1 新增（带默认值、追加在尾部，旧调用方按位置构造不受影响）：
    state/cwd/truncated/timed_out/env_overrides。
    """

    command: str
    exit_code: int
    duration_ms: int
    log_path: str
    stdout_tail: str
    stderr_tail: str
    timestamp: str
    pid: int
    success: bool
    state: str = "completed"
    cwd: str | None = None
    truncated: bool = False
    timed_out: bool = False
    env_overrides: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CliSpec:
    """一个 CLI 在注册表中的配置（白名单语义只在 run_cli 路径成立）。

    retry_on 当前仅支持 "spawn"（只重试启动前失败）；其他值保留字段但不生效。
    """

    default_timeout: float | None = None
    capture: bool = True
    tail: int = 2000
    cwd: str | os.PathLike | None = None
    env: dict[str, str] | None = None
    retry_on: str = "spawn"


REGISTRY: dict[str, CliSpec] = {}


def _safe_component(cmd: Sequence[str]) -> str:
    """取命令首段用于文件名（防路径穿越）。"""
    head = cmd[0] if cmd else "unknown"
    return Path(head).name.replace("/", "_").replace("\\", "_")[:40]


def _norm_log_path(p: Path) -> str:
    """规范化库内构造的日志路径再落盘（resolve 消除 .. 等相对成分）。

    所有日志/part 路径均由本库从 log_root + _safe_component 文件名拼出，
    此处 resolve 是纵深防御：即使上游成分异常，落盘目标也退化为规范绝对路径。
    """
    return str(Path(p).resolve())


def _mask_env(env: Mapping[str, str] | None) -> dict[str, str]:
    """只记显式传入的覆盖项；疑似敏感（KEY/TOKEN/SECRET/PASSWORD/PASS/CREDENTIAL）的值脱敏。"""
    if not env:
        return {}
    out: dict[str, str] = {}
    for k, v in env.items():
        ku = k.upper()
        out[k] = "***" if any(m in ku for m in _SENSITIVE_MARKS) else str(v)
    return out


def _copy_part(src: Path, f) -> None:
    """把 part 文件按块搬进主日志（内存 O(块)，不整读）。

    用增量解码器：多字节 UTF-8 字符跨块时由解码器内部缓冲衔接，不会产生 U+FFFD 残留。
    """
    if not src.is_file():
        return
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    with open(src, "rb") as s:
        while True:
            chunk = s.read(_PUMP_CHUNK)
            if not chunk:
                break
            f.write(decoder.decode(chunk))
        f.write(decoder.decode(b"", True))  # EOF 冲刷：尾部半字符按 replace 收尾


def _tail_from_file(path: Path, max_chars: int) -> tuple[str, bool]:
    """从文件尾部取最多 max_chars 字符，返回 (tail, truncated)。

    字节窗口 = max_chars*4（utf-8 每字符上界）+ 16 字节余量：seek 劈开多字节字符时
    产生的替位符（至多 3 个，且必在窗口开头）保证被 [-max_chars:] 切片裁掉——
    tail 永不残留 U+FFFD。内存 O(max_chars)，10MB 输出也不会整读进内存。
    文件缺失/不可读按空尾部处理（pump 写盘失败时的兜底，不让 run() 崩溃）。
    """
    try:
        size = path.stat().st_size
    except OSError:
        return ("", False)
    if max_chars <= 0:
        return ("", size > 0)
    want = max_chars * 4 + 16
    try:
        with open(path, "rb") as f:
            if size > want:
                f.seek(size - want)
            data = f.read()
    except OSError:
        return ("", size > 0)
    text = data.decode("utf-8", errors="replace")
    truncated = size > want or len(text) > max_chars
    if len(text) > max_chars:
        text = text[-max_chars:]
    return text, truncated


def _close_quietly(pipe) -> None:
    try:
        pipe.close()
    except (OSError, ValueError):
        pass


def _close_in_background(pipe) -> None:
    """在守护线程里关管道：Windows 上 close 可能与阻塞中的 ReadFile 互锁，
    放到后台执行，防止 close 本身挂死主线程。"""
    threading.Thread(target=_close_quietly, args=(pipe,), daemon=True).start()


def _pump(pipe, out, errors: list[str]) -> None:
    """reader 线程体：边读管道边写盘（流式 tee）。

    buffering=0：每块立即落盘，主线程稍后取 tail / 组装日志时不依赖 reader 的
    Python 层缓冲被刷出（reader 卡死场景下 partial 依然可读）。
    out 为 None（part 打开失败，如磁盘满）时纯排水：仍消费管道，防止子进程
    因管道满而阻塞——排水是 reader 的硬职责，落盘是尽力而为。
    意外异常（非 OSError/ValueError 的并发关闭类）记入共享 errors，结果侧显式
    标记截断——reader 悄悄死掉等于观测层说谎。
    """
    try:
        if out is not None:
            while True:
                chunk = pipe.read1(_PUMP_CHUNK)  # read1：有数据即返回，不等满块
                if not chunk:
                    break
                out.write(chunk)
        else:
            while pipe.read1(_PUMP_CHUNK):
                pass
    except (OSError, ValueError):
        pass  # 管道被并发关闭等场景，尽力而为
    except Exception as exc:  # noqa: BLE001 - reader 意外死亡必须留痕
        errors.append(f"reader crashed: {type(exc).__name__}: {exc}")
    finally:
        if out is not None:
            _close_quietly(out)
        _close_quietly(pipe)


def _terminate_process_group(proc: subprocess.Popen) -> None:
    """超时终止：杀整个进程组/进程树，防孤儿孙进程。

    POSIX：start_new_session 使子进程自成进程组 → killpg(SIGTERM) → 宽限后 SIGKILL。
    Windows：CREATE_NEW_PROCESS_GROUP + taskkill /F /T（按 PID 杀整棵树）。
    """
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,  # 进程可能刚好自行退出，容忍 taskkill 失败
                timeout=10,  # taskkill 走 RPC，卡死不得拖垮 run() 的兜底链
            )
        except (OSError, subprocess.TimeoutExpired):
            pass  # taskkill 缺失/卡死：交由后续 proc.wait 兜底
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        return

    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        pgid = None
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=_KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        pass
    if pgid is not None:
        # 无条件补刀 SIGKILL：直接子进程可能在宽限期内先死，而 TERM 免疫的
        # 孙进程仍占着进程组——跳过补刀会让它永久存活并拖住管道。
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _guard_inputs(cmd: Sequence[str], cwd: str | os.PathLike | None) -> list[str]:
    """入参守卫：str 拒收（防拆字符）、空列表拒收、元素类型校验、cwd 前置校验。"""
    if isinstance(cmd, str):
        raise TypeError("cmd 必须是参数序列（如 ['echo', 'hi']），不要传字符串")
    argv = list(cmd)
    if not argv:
        raise ValueError("cmd 不能为空列表")
    if not all(isinstance(a, str) for a in argv):
        # 混入 bytes/int 的 argv 在 POSIX 会真实执行后才在 shlex.join 处炸出——
        # 命令跑了却零审计条目，违反"有调用必有审计"契约，必须在 spawn 前拒收
        raise TypeError("cmd 的每个元素都必须是 str")
    if cwd is not None and not Path(cwd).is_dir():
        raise FileNotFoundError(f"cwd does not exist: {cwd}")
    return argv


def _one_line(s: str) -> str:
    """审计头字段必须单行：argv 含换行会伪造日志分段/结果尾（shlex.quote 不转义换行）。"""
    return s.replace("\r", " ").replace("\n", " ")


def _header_lines(
    argv: list[str],
    started_at: str,
    pid: int,
    cwd: str | os.PathLike | None,
    env: Mapping[str, str] | None,
    timeout: float | None,
    capture: bool,
) -> list[str]:
    """日志元数据头（capture=False 时"没输出"与"没采集"可区分）。"""
    return [
        f"# command: {_one_line(shlex.join(argv))}",
        f"# timestamp: {started_at}",
        f"# pid: {pid}",
        f"# capture: {capture}",
        f"# cwd: {Path(cwd) if cwd else ''}",
        f"# env_overrides: {_mask_env(env)}",
        f"# timeout: {timeout if timeout is not None else 'none'}",
    ]


def _write_spawn_failure_log(
    log_path: Path,
    header: list[str],
    state: str,
    exit_code: int,
    message: str,
    duration_ms: int,
) -> None:
    """启动前失败也落盘留痕（失败场景恰恰最需要审计）。newline="" 保字节保真。"""
    try:
        with open(_norm_log_path(log_path), "w", encoding="utf-8", errors="replace", newline="") as f:
            for line in header:
                f.write(line + "\n")
            f.write("--- STDOUT ---\n--- STDERR ---\n--- END ---\n")
            f.write(f"# exit_code: {exit_code}\n")
            f.write(f"# duration_ms: {duration_ms}\n")
            f.write(f"# state: {state}\n")
            f.write(f"# error: {message}\n")
    except OSError:
        pass  # 日志都写不进时不再叠加异常，结果对象里仍有 log_path/信息


def _spawn_failed_result(
    log_path: Path,
    argv: list[str],
    started_at: str,
    start: float,
    cwd: str | os.PathLike | None,
    env: Mapping[str, str] | None,
    timeout: float | None,
    capture: bool,
    state: str,
    exit_code: int,
    message: str,
) -> GatewayResult:
    duration_ms = int((time.monotonic() - start) * 1000)
    _write_spawn_failure_log(
        log_path,
        _header_lines(argv, started_at, 0, cwd, env, timeout, capture),
        state,
        exit_code,
        message,
        duration_ms,
    )
    return GatewayResult(
        command=shlex.join(argv),
        exit_code=exit_code,
        duration_ms=duration_ms,
        log_path=str(log_path),
        stdout_tail=message,
        stderr_tail="",
        timestamp=started_at,
        pid=0,
        success=False,
        state=state,
        cwd=str(cwd) if cwd else None,
        timed_out=False,
        env_overrides=_mask_env(env),
    )


def _assemble_log(
    log_path: Path,
    header: list[str],
    stdout_part: Path | None,
    stderr_part: Path | None,
    trailer: list[str],
) -> None:
    """组装最终日志：头（元数据）+ STDOUT 段 + STDERR 段 + END + 尾（结果元数据）。

    newline=""：禁用 Windows 文本模式换行翻译，part 里的 CRLF 原样落盘（不膨胀成 CRCRLF）。
    """
    with open(_norm_log_path(log_path), "w", encoding="utf-8", errors="replace", newline="") as f:
        for line in header:
            f.write(line + "\n")
        f.write("--- STDOUT ---\n")
        if stdout_part is not None:
            _copy_part(stdout_part, f)
        f.write("--- STDERR ---\n")
        if stderr_part is not None:
            _copy_part(stderr_part, f)
        f.write("--- END ---\n")
        for line in trailer:
            f.write(line + "\n")


def _write_minimal_log(log_path: Path, header: list[str], trailer: list[str]) -> None:
    """组装失败时的兜底最小日志：保证"有调用必有审计条目"。"""
    try:
        with open(_norm_log_path(log_path), "w", encoding="utf-8", errors="replace", newline="") as f:
            for line in header:
                f.write(line + "\n")
            f.write("--- STDOUT ---\n--- STDERR ---\n--- END ---\n")
            for line in trailer:
                f.write(line + "\n")
    except OSError:
        pass


def _run_once(
    argv: list[str],
    *,
    cwd: str | os.PathLike | None,
    timeout: float | None,
    log_root: Path,
    capture: bool,
    tail: int,
    env: Mapping[str, str] | None,
    stdin_devnull: bool = False,
    observe: bool = False,
    task_id: str | None = None,
    bond: str | None = None,
) -> GatewayResult:
    """单次尝试：spawn → 流式 tee → 终止/收尾 → 组装日志 → 结构化返回。"""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id = uuid.uuid4().hex[:8]
    log_path = log_root / f"{ts}-{_safe_component(argv)}-{run_id}.log"
    stdout_part = log_path.with_suffix(".stdout.part")
    stderr_part = log_path.with_suffix(".stderr.part")
    start = time.monotonic()
    started_at = datetime.now().isoformat(timespec="seconds")

    # env：显式覆盖合并进当前环境；注入 PYTHONUTF8=1（已显式设置时不覆盖）
    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)
    merged_env.setdefault("PYTHONUTF8", "1")

    # 观测模式：查元素周期表。已知元素追加观测配方（激发离散谱线——结构化
    # 事件流）；未知元素以连续谱（纯文本）照常执行，仍记录全文 answer 事件。
    element = _events._element_name(argv)
    observe_format = _events.observation_format(element) if observe else None
    spawn_argv = list(argv) + (_events.observation_flags(element) if observe else [])

    popen_kwargs: dict = {}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True  # 子进程自成进程组，killpg 才有靶子

    try:
        proc = subprocess.Popen(
            spawn_argv,
            cwd=str(cwd) if cwd else None,
            env=merged_env,
            stdin=subprocess.DEVNULL if stdin_devnull else None,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            **popen_kwargs,
        )
    except FileNotFoundError as exc:
        # 启动前失败①：命令未找到（cwd 不存在已在守卫分流，不会到这里）
        return _spawn_failed_result(
            log_path, spawn_argv, started_at, start, cwd, env, timeout, capture,
            "not_found", _EXIT_NOT_FOUND, f"command not found: {exc}",
        )
    except OSError as exc:
        # 启动前失败②：找到但不可执行等（PermissionError / WinError 193 ...）
        return _spawn_failed_result(
            log_path, spawn_argv, started_at, start, cwd, env, timeout, capture,
            "spawn_error", _EXIT_SPAWN_ERROR, f"spawn failed: {exc}",
        )

    # 流式 tee：两个 reader 线程边读边写盘（part 在主线程打开后移交，失败则纯排水）。
    # 装配段整体置于 try/finally 内：spawn 成功后任何一步炸出（线程耗尽等）都必须
    # 走兜底（杀活进程/关管道/清 part），不得留孤儿进程或零审计穿出 run()。
    pipes = [p for p in (proc.stdout, proc.stderr) if p is not None]
    readers: list[threading.Thread] = []
    reader_errors: list[str] = []
    part_open_failed = False

    result: GatewayResult
    try:
        if capture:
            for pipe, part in ((proc.stdout, stdout_part), (proc.stderr, stderr_part)):
                try:
                    out = open(_norm_log_path(part), "wb", buffering=0)
                except OSError:
                    out = None  # 磁盘满/杀软拦截等：放弃落盘，但排水不停（防管道满死锁）
                    part_open_failed = True
                t = threading.Thread(target=_pump, args=(pipe, out, reader_errors), daemon=True)
                t.start()
                readers.append(t)

        # 等进程结束或超时（reader 持续排水，wait 不会因管道满而死锁）
        timed_out = False
        try:
            returncode = proc.wait(timeout=timeout)
            # duration 定格于终态达成时——杀树/收 reader 的成本不得计入
            duration_ms = int((time.monotonic() - start) * 1000)
        except subprocess.TimeoutExpired:
            timed_out = True
            duration_ms = int((time.monotonic() - start) * 1000)
            _terminate_process_group(proc)
            returncode = proc.returncode

        # 收 reader：共享 deadline（并行预算）——单个 hung 不得吃掉其他线程的 join 时间
        join_deadline = time.monotonic() + _READER_JOIN_S
        for t in readers:
            t.join(timeout=max(0.0, join_deadline - time.monotonic()))
        if any(t.is_alive() for t in readers):
            # 孙进程继承管道写端 → 管道不 EOF。后台关管道解堵（防 close 挂死主线程）。
            for p in pipes:
                _close_in_background(p)
            grace_deadline = time.monotonic() + 5
            for t in readers:
                t.join(timeout=max(0.0, grace_deadline - time.monotonic()))
        # join 后复评而非初判定终身：等待期间孙进程退场/管道解堵后 reader 可能已
        # 恢复收尾，输出其实是完整的——误报 truncated 会误导调用方丢弃有效结果
        readers_hung = any(t.is_alive() for t in readers)

        if timed_out:
            state, exit_code = "timed_out", _EXIT_TIMEOUT
        elif returncode is not None and returncode < 0:
            state, exit_code = "killed", returncode  # POSIX：被信号杀死，exit_code 为负信号值
        else:
            state, exit_code = "completed", returncode if returncode is not None else -1

        trailer = [
            f"# exit_code: {exit_code}",
            f"# duration_ms: {duration_ms}",
            f"# state: {state}",
        ]
        if timed_out:
            trailer.append(f"# error: TIMEOUT after {timeout}s")
        if readers_hung:
            trailer.append(
                "# warning: reader did not finish (descendant may hold the pipe); output may be incomplete"
            )
        for err in reader_errors[:2]:
            trailer.append(f"# warning: {err}")

        # header 记录实际执行的 argv（观测配方的 flags 透明可见）
        header = _header_lines(spawn_argv, started_at, proc.pid, cwd, env, timeout, capture)
        if task_id is not None:
            header.append(f"# task: {task_id}")
        if bond:
            header.append(f"# bond: {bond}")
        if observe:
            header.append(f"# element: {element}")
            header.append(f"# observe: {observe_format}")

        # 组装日志：失败兜底最小日志，保证"有调用必有审计条目"
        assembly_error: OSError | None = None
        try:
            _assemble_log(
                log_path,
                header,
                stdout_part if capture else None,
                stderr_part if capture else None,
                trailer,
            )
        except OSError as exc:
            assembly_error = exc
            trailer.append(f"# warning: log assembly failed: {exc}")
            _write_minimal_log(log_path, header, trailer)

        # tail 从 part 文件取尾部（内存有界），不保留整份输出在内存
        if capture and assembly_error is None:
            stdout_tail, out_trunc = _tail_from_file(stdout_part, tail)
            stderr_tail, err_trunc = _tail_from_file(stderr_part, tail)
            truncated = out_trunc or err_trunc
        else:
            stdout_tail = stderr_tail = ""
            truncated = assembly_error is not None  # 组装失败=输出不完整
        if readers_hung or reader_errors or part_open_failed:
            truncated = True  # reader 异常出口/落盘通道残缺，输出可能不完整，显式告警

        result = GatewayResult(
            command=shlex.join(spawn_argv),
            exit_code=exit_code,
            duration_ms=duration_ms,
            log_path=str(log_path),
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            timestamp=started_at,
            pid=proc.pid,
            success=(not timed_out) and exit_code == 0,
            state=state,
            cwd=str(cwd) if cwd else None,
            truncated=truncated,
            timed_out=timed_out,
            env_overrides=_mask_env(env),
        )

        # 观测模式：输出落盘后组装原子事件（question → [note…] → answer → exit）。
        # 全文经 _tail_from_file 超大 tail 读取（复用存量 IO；适合中小输出，
        # 超大输出的流式事件化留待后续版本）。
        if observe:
            merged_output: str | None = ""
            if capture:
                out_text, _ = _tail_from_file(stdout_part, 2**31 - 1)
                err_text, _ = _tail_from_file(stderr_part, 2**31 - 1)
                merged_output = "\n".join(t for t in (out_text, err_text) if t)
            _events.write_events(
                Path(str(log_path) + ".events.jsonl"),
                _events.build_events(
                    atom=log_path.stem,
                    task_id=task_id,
                    element=element,
                    ts=started_at,
                    argv=spawn_argv,
                    output_text=merged_output,
                    exit_code=exit_code,
                    duration_ms=duration_ms,
                    state=state,
                ),
            )
    finally:
        # 兜底：杀活进程、关管道、清 part——异常/半途退出也不泄漏资源
        try:
            if proc.poll() is None:
                _terminate_process_group(proc)
        except Exception:
            pass
        # reader 仍阻塞时同步 close 会与其未决 ReadFile 互锁挂死主线程
        # （Windows 实测：close 时长 = 孙进程剩余寿命）——该场景交后台线程收尾
        for i, p in enumerate(pipes):
            if i < len(readers) and readers[i].is_alive():
                _close_in_background(p)
            else:
                _close_quietly(p)
        for part in (stdout_part, stderr_part):
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass  # reader 仍持句柄时 unlink 失败（WinError 32），尽力清理即可

    return result


def run(
    cmd: Sequence[str],
    *,
    cwd: str | os.PathLike | None = None,
    timeout: float | None = None,
    log_dir: str | os.PathLike | None = None,
    capture: bool = True,
    tail: int = 2000,
    env: Mapping[str, str] | None = None,
    retry_count: int = 0,
    stdin_devnull: bool = False,
    observe: bool = False,
    task_id: str | None = None,
    bond: str | None = None,
) -> GatewayResult:
    """执行一条 CLI 命令，全量落盘并返回结构化结果。

    - stdout/stderr 全量流式写入 log 文件（可回放、可审计，内存有界）
    - 退出码语义：未找到=127、启动失败=126、超时=124
      state ∈ {completed, timed_out, not_found, spawn_error, killed}
    - env 只作覆盖合并进当前环境（另注入 PYTHONUTF8=1，已显式设置时不覆盖），
      审计仅记录显式传入项（敏感值脱敏）
    - retry_count 只对"启动前失败"（not_found / spawn_error）重试；
      超时/非零退出绝不自动重试；每次尝试独立日志
    - stdin_devnull=True 时子进程 stdin 接 DEVNULL（无人值守/探测场景防交互挂起）
    - observe=True 开观测模式：已知元素（claude/codex）追加观测配方激发结构化
      事件流，并把本次调用写成原子事件 sidecar（<log>.events.jsonl：
      question/[note…]/answer/exit）；task_id 把多个原子绑定为一个任务（化合物），
      供 task_atoms()/CLI `task` 子命令检索
    - bond=<atom-id> 显式化学键：声明本次调用的输入来自哪个原子（默认时序链），
      build_graph 据此连线
    - 纯本地 subprocess，不发起任何网络请求
    """
    if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
        # 非法值必须在 spawn 前拒收，不能交给平台各自解释：Windows 负值经 DWORD
        # 回绕等效无限等待（超时契约静默失效），POSIX 却是立即杀
        raise ValueError(f"timeout 必须为正的有限数值或 None（不限时），收到 {timeout!r}")
    argv = _guard_inputs(cmd, cwd)

    log_root = Path(log_dir) if log_dir else DEFAULT_LOG_DIR
    log_root.mkdir(parents=True, exist_ok=True)  # 目录不可写 → fail-fast 抛 OSError

    attempts = max(0, int(retry_count)) + 1
    result: GatewayResult | None = None
    for attempt in range(attempts):
        result = _run_once(
            argv,
            cwd=cwd,
            timeout=timeout,
            log_root=log_root,
            capture=capture,
            tail=tail,
            env=env,
            stdin_devnull=stdin_devnull,
            observe=observe,
            task_id=task_id,
            bond=bond,
        )
        if result.state not in _SPAWN_FAILURE_STATES or attempt == attempts - 1:
            break  # 只有启动前失败才重试
    assert result is not None
    return result


def run_cli(
    name: str,
    *args: str,
    timeout: float | None = None,
    log_dir: str | os.PathLike | None = None,
    retry_count: int = 0,
) -> GatewayResult:
    """按注册表白名单调用 CLI：name 必须已注册，配置取自 CliSpec（显式参数优先）。"""
    spec = REGISTRY.get(name)
    if spec is None:
        raise KeyError(f"CLI not registered: {name!r} (known: {sorted(REGISTRY)})")
    return run(
        [name, *args],
        cwd=spec.cwd,
        timeout=spec.default_timeout if timeout is None else timeout,
        log_dir=log_dir,
        capture=spec.capture,
        tail=spec.tail,
        env=spec.env,
        retry_count=retry_count,
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
    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0  # 并发清理/轮转把日志删了：不崩列表，排最后

    return sorted(root.glob("*.log"), key=_mtime, reverse=True)


__all__ = [
    "GatewayResult",
    "CliSpec",
    "REGISTRY",
    "run",
    "run_cli",
    "replay",
    "list_logs",
    "DEFAULT_LOG_DIR",
    "read_events",
    "task_atoms",
    "task_formula",
]


def read_events(sidecar: str | os.PathLike) -> list[dict]:
    """读取一次观测调用的原子事件序列（question/[note…]/answer/exit）。"""
    return _events.read_events(sidecar)


def task_atoms(task_id: str, log_dir: str | os.PathLike | None = None) -> list[dict]:
    """化合物检索：返回属于 task_id 的原子摘要列表（按时间序）。"""
    return _events.task_atoms(task_id, log_dir)


def task_formula(atoms: list[dict]) -> str:
    """分子式：任务内元素组成配比，如 "claude×3 + codex×2"。"""
    return _events.task_formula(atoms)
