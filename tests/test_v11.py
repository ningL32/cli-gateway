"""cli-gateway v1.1 契约测试：Popen 直控 / 进程组终止 / 流式 tee / 编码 / 注册表 / 重试。

平台分支约定：POSIX-only 断言用 skipif 跳过（本机 win32 不跑）；Windows 分支必须真实跑。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cli_gateway import REGISTRY, CliSpec, GatewayResult, list_logs, replay, run, run_cli  # noqa: E402


def _tmp_logdir(tmp_path: Path) -> str:
    d = tmp_path / "logs"
    d.mkdir()
    return str(d)


def _pid_alive_windows(pid: int) -> bool:
    """Windows 下查 pid 是否仍存活（tasklist 过滤，不依赖本地化文案）。"""
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}"],
        capture_output=True,
        text=True,
        encoding="utf-8",  # tasklist 输出走本地代码页，显式声明 + replace 防解码崩溃
        errors="replace",
        check=False,
    ).stdout
    return re.search(rf"\b{pid}\b", out) is not None


# --- 方案第八点 1/6：超时杀进程组（子进程随之消失） -------------------------

_GRANDCHILD_CODE = (
    "import subprocess, sys\n"
    "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
    "print(g.pid, flush=True)\n"
    "g.wait()\n"
)


def _timed_out_grandchild_pid(tmp_path: Path) -> int:
    """跑一个会 spawn 孙进程的命令到超时，返回孙进程 pid。"""
    d = _tmp_logdir(tmp_path)
    res = run([sys.executable, "-c", _GRANDCHILD_CODE], timeout=4, log_dir=d)
    assert res.state == "timed_out"
    assert res.exit_code == 124
    assert res.timed_out is True
    return int(res.stdout_tail.strip().splitlines()[0])


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 分支必须真实跑（本机 win32）")
def test_timeout_kills_process_tree_windows(tmp_path: Path) -> None:
    gc_pid = _timed_out_grandchild_pid(tmp_path)
    # taskkill /F /T 后孙进程消失（留短暂余量防收尸竞态）
    for _ in range(20):
        if not _pid_alive_windows(gc_pid):
            break
        time.sleep(0.25)
    assert not _pid_alive_windows(gc_pid), f"grandchild {gc_pid} still alive after timeout kill"


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only：本机 win32 跳过（注意 sys.platform 在 Linux/macOS 是 linux/darwin）")
def test_timeout_kills_process_tree_posix(tmp_path: Path) -> None:
    gc_pid = _timed_out_grandchild_pid(tmp_path)
    # 僵尸进程对 os.kill(pid, 0) 恒成功，必须带截止时间轮询到真正消失
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(gc_pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    pytest.fail(f"grandchild {gc_pid} still alive after timeout kill (possibly zombie)")


# --- 方案第八点 2/6：超时保留部分输出 ---------------------------------------


def test_timeout_preserves_partial_output(tmp_path: Path) -> None:
    # Arrange
    d = _tmp_logdir(tmp_path)
    # Act：先打印再长睡，超时杀掉
    res = run(
        [sys.executable, "-c", "import time; print('PARTIAL-MARK', flush=True); time.sleep(30)"],
        timeout=2,
        log_dir=d,
    )
    # Assert
    assert res.state == "timed_out"
    assert res.exit_code == 124
    assert "PARTIAL-MARK" in res.stdout_tail
    assert "PARTIAL-MARK" in replay(res.log_path)


# --- 方案第八点 3/6：大输出内存有界且 tail 正确 ------------------------------


def test_large_output_bounded(tmp_path: Path) -> None:
    # Arrange
    d = _tmp_logdir(tmp_path)
    # Act：约 10MB 输出 + 尾部标记
    res = run(
        [sys.executable, "-c", "print('x' * 10_000_000); print('TAIL-END-MARK')"],
        log_dir=d,
    )
    # Assert：全量落盘、tail 有界且是真正的尾部、无死锁
    assert res.state == "completed"
    assert Path(res.log_path).stat().st_size > 10_000_000
    assert "TAIL-END-MARK" in res.stdout_tail
    assert len(res.stdout_tail) <= 2000
    assert res.truncated is True


# --- 方案第八点 4/6：中文编码（日志 UTF-8 可读） ------------------------------


def test_chinese_output_utf8_log(tmp_path: Path) -> None:
    # Arrange
    d = _tmp_logdir(tmp_path)
    # Act：子进程直接往 stdout 写 UTF-8 字节
    res = run(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write('中文输出ok\\n'.encode('utf-8'))",
        ],
        log_dir=d,
    )
    # Assert：tail 与日志文件均按 UTF-8 读出中文，不乱码
    assert "中文输出ok" in res.stdout_tail
    assert "中文输出ok" in Path(res.log_path).read_text(encoding="utf-8")


def test_env_overrides_recorded_and_masked(tmp_path: Path) -> None:
    # Arrange
    d = _tmp_logdir(tmp_path)
    # Act：env 显式覆盖传递 + 敏感 key 脱敏
    res = run(
        [sys.executable, "-c", "print('中文env')"],
        env={"PYTHONIOENCODING": "utf-8", "MY_API_KEY": "sk-secret", "HOME_DIR": "x"},
        log_dir=d,
    )
    # Assert
    assert "中文env" in res.stdout_tail
    assert res.env_overrides == {"PYTHONIOENCODING": "utf-8", "MY_API_KEY": "***", "HOME_DIR": "x"}
    assert res.env_overrides["MY_API_KEY"] == "***"
    assert "sk-secret" not in replay(res.log_path)


# --- 方案第八点 5/6：并发 N=20 日志唯一不串 ----------------------------------


def test_concurrent_runs_unique_logs(tmp_path: Path) -> None:
    # Arrange
    d = _tmp_logdir(tmp_path)

    def _worker(i: int):
        return run([sys.executable, "-c", f"print('idx-{i}')"], log_dir=d)

    # Act
    with ThreadPoolExecutor(max_workers=20) as ex:
        results = list(ex.map(_worker, range(20)))
    # Assert：20 条日志互异、文件都在、各自输出不串
    assert len({r.log_path for r in results}) == 20
    assert all(Path(r.log_path).is_file() for r in results)
    assert all(r.success and r.state == "completed" for r in results)
    for i, r in enumerate(results):
        assert f"idx-{i}" in r.stdout_tail


# --- 方案第八点 6/6：pid 回归（v0.1 恒 0 的 bug） -----------------------------


def test_pid_positive_and_in_log(tmp_path: Path) -> None:
    # Arrange
    d = _tmp_logdir(tmp_path)
    # Act
    res = run([sys.executable, "-c", "print(1)"], log_dir=d)
    # Assert：真 pid > 0 且写进日志头
    assert res.pid > 0
    assert f"# pid: {res.pid}" in replay(res.log_path)


# --- 入参守卫与边界 ----------------------------------------------------------


def test_reject_str_command() -> None:
    # v0.1 bug：list("echo hi") 会拆成单字符
    with pytest.raises(TypeError):
        run("echo hi")  # type: ignore[arg-type]


def test_reject_empty_command() -> None:
    with pytest.raises(ValueError):
        run([])


def test_cwd_missing_raises_and_not_misreported(tmp_path: Path) -> None:
    bad = tmp_path / "no-such-dir"
    with pytest.raises(FileNotFoundError, match="cwd"):
        run([sys.executable, "-c", "print(1)"], cwd=bad)


def test_completed_state_fields(tmp_path: Path) -> None:
    d = _tmp_logdir(tmp_path)
    res = run([sys.executable, "-c", "print('hello')"], log_dir=d)
    assert res.state == "completed"
    assert res.timed_out is False
    assert res.truncated is False
    assert res.cwd is None
    # tail=0：tail 为空且标记截断
    res0 = run([sys.executable, "-c", "print('hello')"], log_dir=d, tail=0)
    assert res0.stdout_tail == ""
    assert res0.truncated is True
    # cwd 回显
    resc = run([sys.executable, "-c", "print(1)"], log_dir=d, cwd=str(tmp_path))
    assert resc.cwd == str(tmp_path)


def test_not_found_exit_127_and_state(tmp_path: Path) -> None:
    d = _tmp_logdir(tmp_path)
    res = run(["no-such-cmd-xyz-987"], log_dir=d)
    assert res.exit_code == 127
    assert res.state == "not_found"
    assert res.pid == 0
    assert res.success is False
    assert "not found" in res.stdout_tail
    # v1.1：not_found 也落盘可审计
    assert Path(res.log_path).is_file()
    assert "command not found" in replay(res.log_path)


def test_log_dir_not_writable_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker.log"
    blocker.write_text("i am a file", encoding="utf-8")
    with pytest.raises(OSError):
        run([sys.executable, "-c", "print(1)"], log_dir=blocker)


# --- 注册表 run_cli ----------------------------------------------------------


def test_run_cli_unregistered_raises() -> None:
    with pytest.raises(KeyError):
        run_cli("no-such-cli-xyz", "--flag")


def test_run_cli_uses_registry_spec(tmp_path: Path) -> None:
    # Arrange：白名单注册（用真可执行文件做 key 以便真实跑通）
    d = _tmp_logdir(tmp_path)
    key = sys.executable
    REGISTRY[key] = CliSpec(default_timeout=10, tail=100)
    try:
        # Act
        res = run_cli(key, "-c", "print('y' * 500)", log_dir=d)
        # Assert：spec.tail=100 生效、spec.default_timeout 透传成功
        assert res.success and res.state == "completed"
        assert len(res.stdout_tail) == 100
        assert "yyy" in res.stdout_tail
    finally:
        REGISTRY.pop(key, None)


# --- 重试语义 ----------------------------------------------------------------


def test_retry_spawn_failure_three_logs(tmp_path: Path) -> None:
    # Arrange
    d = _tmp_logdir(tmp_path)
    # Act：spawn 前失败重试 2 次 = 3 次尝试
    res = run(["no-such-cmd-retry-xyz"], retry_count=2, log_dir=d)
    # Assert：最终 not_found，每次尝试独立日志
    assert res.exit_code == 127
    assert res.state == "not_found"
    assert len(list_logs(d)) == 3


def test_timeout_never_retried(tmp_path: Path) -> None:
    # Arrange
    d = _tmp_logdir(tmp_path)
    # Act：超时不自动重试（重试超时是放大器）
    res = run(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        timeout=0.5,
        retry_count=3,
        log_dir=d,
    )
    # Assert
    assert res.exit_code == 124
    assert res.state == "timed_out"
    assert len(list_logs(d)) == 1


# --- CLI 入口回归：run 子命令曾因 argparse dest 同名冲突静默空转 -------------


def test_cli_run_entry_prints_summary(tmp_path: Path, capsys) -> None:
    # Arrange
    from cli_gateway.__main__ import main

    d = _tmp_logdir(tmp_path)
    # Act
    rc = main(["run", "--dir", d, "--", sys.executable, "-c", "print('cli-entry-ok')"])
    out = capsys.readouterr().out
    # Assert：入口打印摘要且透传子进程输出，而不是静默返回 0
    assert rc == 0
    assert "exit=0" in out
    assert "cli-entry-ok" in out


# --- 旧契约回归：GatewayResult 新字段带默认值，旧字段不动 ---------------------


def test_gateway_result_backward_compatible_construction() -> None:
    # 旧调用方按位置构造 9 个旧字段仍可用（v0.1 契约）
    r = GatewayResult(
        "cmd",
        0,
        1,
        "log",
        "",
        "",
        "2026-10-18T00:00:00",
        1,
        True,
    )
    assert r.state == "completed"
    assert r.timed_out is False
    assert r.to_dict()["state"] == "completed"


# --- 回炉 P0-1：_copy_part 块边界解码损坏（65535 ASCII + 汉字跨界 + 尾部 ASCII）


def test_block_boundary_chinese_preserved(tmp_path: Path) -> None:
    # Arrange：恰好让多字节汉字跨过 64KB 泵块边界
    d = _tmp_logdir(tmp_path)
    expected = b"a" * 65535 + "中".encode("utf-8") + b"b" * 10
    # Act（chr(0x4E2D) 避免命令行传中文）
    res = run(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'a'*65535 + chr(0x4E2D).encode('utf-8') + b'b'*10)",
        ],
        log_dir=d,
    )
    # Assert：日志字节与真实输出逐字一致，无 U+FFFD 残留
    assert res.state == "completed"
    data = Path(res.log_path).read_bytes()
    assert expected in data
    assert "\ufffd" not in data.decode("utf-8")


# --- 回炉 P0-2：_tail_from_file seek 落点多字节字符中间 -----------------------


def test_tail_seek_boundary_no_replacement(tmp_path: Path) -> None:
    # Arrange：3000 字节汉字串 + 尾部 ASCII，seek 落点必劈开 3 字节字符
    d = _tmp_logdir(tmp_path)
    # Act
    res = run(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(chr(0x4E2D).encode('utf-8')*1000 + b'z')",
        ],
        log_dir=d,
        tail=100,
    )
    # Assert：tail 精确等于最后 100 字符，无替换符
    assert res.stdout_tail == "中" * 99 + "z"
    assert "\ufffd" not in res.stdout_tail
    assert res.truncated is True


# --- 回炉 P1-4：CRCRLF 字节失真（Windows 文本模式换行翻译） -------------------


def test_crcrlf_byte_exact(tmp_path: Path) -> None:
    # Arrange
    d = _tmp_logdir(tmp_path)
    # Act：子进程写 CRLF 字节
    res = run(
        [sys.executable, "-c", r"import sys; sys.stdout.buffer.write(b'L1\r\nL2\r\n')"],
        log_dir=d,
    )
    # Assert：日志 STDOUT 段字节级一致，\r\n 不膨胀为 \r\r\n
    data = Path(res.log_path).read_bytes()
    start = data.index(b"--- STDOUT ---\n") + len(b"--- STDOUT ---\n")
    end = data.index(b"--- STDERR ---\n")
    assert data[start:end] == b"L1\r\nL2\r\n"
    assert b"\r\r" not in data


# --- 回炉 P1-1：孙进程继承管道 → reader 不 EOF 的连锁缺陷 --------------------


def test_reader_hang_warns_truncated_and_duration(tmp_path: Path) -> None:
    # Arrange：子进程 spawn 孙进程后立刻退出（孙进程继承管道写端 → 管道不 EOF）
    d = _tmp_logdir(tmp_path)
    child = (
        "import subprocess, sys\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "print(g.pid, flush=True)\n"
        "print('parent-done', flush=True)\n"
    )
    gc_pid = None
    # Act
    t0 = time.monotonic()
    try:
        res = run([sys.executable, "-c", child], log_dir=d)
        elapsed = time.monotonic() - t0
        # Assert①：duration 在 wait 返回定格，不被 join(10) 虚高
        assert res.state == "completed" and res.exit_code == 0
        assert res.duration_ms < 8000, f"duration inflated by reader join: {res.duration_ms}ms"
        # Assert②：join 超时视为异常出口 → warning 行 + truncated 置真
        assert res.truncated is True
        assert "# warning:" in replay(res.log_path)
        # partial 输出（孙 pid）仍可读——reader 未写出则此行先失败
        gc_pid = int(res.stdout_tail.strip().splitlines()[0])
    finally:
        # 清理 30s 孙进程
        if gc_pid is not None:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(gc_pid)], capture_output=True, check=False)
            else:
                import signal

                try:
                    os.kill(gc_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


# --- 回炉 P1-2：_tail_from_file 缺 part 守卫 ---------------------------------


def test_tail_from_file_missing_part_guard(tmp_path: Path) -> None:
    #Arrange
    from cli_gateway import _tail_from_file

    # Act / Assert：part 文件不存在（pump open 失败被吞的场景）不抛，按空返回
    assert _tail_from_file(tmp_path / "nope.stdout.part", 100) == ("", False)


# --- 回炉 P1-3：spawn 错误只认 FileNotFoundError -----------------------------


def test_spawn_error_structured_and_retried(tmp_path: Path) -> None:
    # Arrange：存在但不可执行（Windows: WinError 193 / POSIX: PermissionError）
    d = _tmp_logdir(tmp_path)
    blocker = tmp_path / "not-an-executable.txt"
    blocker.write_text("plain text", encoding="utf-8")
    # Act
    res = run([str(blocker)], log_dir=d)
    # Assert：结构化返回 + 落盘留痕 + 126
    assert res.state == "spawn_error"
    assert res.exit_code == 126
    assert res.pid == 0
    assert res.success is False
    assert Path(res.log_path).is_file()
    assert "spawn" in replay(res.log_path)
    # spawn_error 同属"启动前失败"，参与重试：1 + 1 次 = 2 个日志
    res2 = run([str(blocker)], retry_count=1, log_dir=d)
    assert res2.state == "spawn_error"
    assert len(list_logs(d)) == 3  # 第一次 1 个 + 重试 2 个


# --- 回炉 P1-6：__main__ 空 payload IndexError -------------------------------


def test_main_empty_payload_systemexit() -> None:
    from cli_gateway.__main__ import main

    with pytest.raises(SystemExit):  # parser.error → SystemExit(2)，而非 IndexError
        main(["run"])


# --- 回炉 P1-7：裸 CP936 环境乱码 + PYTHONUTF8 注入 ---------------------------


def test_injects_pythonutf8_when_unset(tmp_path: Path, monkeypatch) -> None:
    # Arrange：模拟裸环境（父进程无 PYTHONUTF8/PYTHONIOENCODING）
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    d = _tmp_logdir(tmp_path)
    # Act / Assert①：未显式设置 → gateway 注入 PYTHONUTF8=1
    res = run(
        [sys.executable, "-c", "import os; print('UTF8=' + str(os.environ.get('PYTHONUTF8')))"],
        log_dir=d,
    )
    assert "UTF8=1" in res.stdout_tail
    # Assert②：调用方显式设置时不覆盖（"0" 是合法值；乱字符串会让 python 预初始化 Fatal）
    res2 = run(
        [sys.executable, "-c", "import os; print('UTF8=' + str(os.environ.get('PYTHONUTF8')))"],
        env={"PYTHONUTF8": "0"},
        log_dir=d,
    )
    assert "UTF8=0" in res2.stdout_tail


def test_chinese_exact_under_bare_cp936(tmp_path: Path, monkeypatch) -> None:
    # Arrange：裸 CP936 模拟——无 PYTHONUTF8 时 python 子进程会按本地代码页输出
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    d = _tmp_logdir(tmp_path)
    # Act
    res = run([sys.executable, "-c", "print('中文注入测试')"], log_dir=d)
    # Assert：注入后子进程输出 UTF-8，日志逐字可读
    assert "中文注入测试" in res.stdout_tail
    assert "中文注入测试" in Path(res.log_path).read_text(encoding="utf-8")


def test_gbk_bytes_no_crash_log_stays_utf8(tmp_path: Path) -> None:
    # Arrange：模拟非 python CLI 直接吐 GBK 字节
    d = _tmp_logdir(tmp_path)
    # Act
    res = run(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'GBK-START' + chr(0x4E2D).encode('gbk') + b'GBK-END')",
        ],
        log_dir=d,
    )
    # Assert：不崩、日志始终是合法 UTF-8、无法解码的字节 replace 兜底
    text = Path(res.log_path).read_text(encoding="utf-8")
    assert "GBK-START" in text and "GBK-END" in text
    assert res.state == "completed"
    assert "\ufffd" in res.stdout_tail


# --- 回炉 P2-1 / P2-2：脱敏词表 + 日志头补字段 -------------------------------


def test_sensitive_pass_and_credential_masked(tmp_path: Path) -> None:
    d = _tmp_logdir(tmp_path)
    res = run(
        [sys.executable, "-c", "print(1)"],
        env={"DB_PASS": "leo", "GOOGLE_APPLICATION_CREDENTIALS": "/x.json", "NOTE": "keep"},
        log_dir=d,
    )
    assert res.env_overrides == {
        "DB_PASS": "***",
        "GOOGLE_APPLICATION_CREDENTIALS": "***",
        "NOTE": "keep",
    }


def test_header_capture_and_notfound_pid(tmp_path: Path) -> None:
    d = _tmp_logdir(tmp_path)
    res = run([sys.executable, "-c", "print(1)"], log_dir=d)
    assert "# capture: True" in replay(res.log_path)
    res2 = run(["no-such-cmd-p2-check"], log_dir=d)
    text = replay(res2.log_path)
    assert "# pid: 0" in text
    assert "# capture: True" in text
    assert res2.timestamp in text  # 日志 timestamp 与返回值统一
