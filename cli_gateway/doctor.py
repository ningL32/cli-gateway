"""cli-gateway doctor：本机编码 CLI 环境体检。

对已知 CLI 清单逐个探测：能否找到、以什么形态被网关调用（原生 exe 直调 /
npm shim 需 cmd /c 包装 / 未找到）、版本是什么——真机验证矩阵的产品化复现。

形态判定以网关真实调用结果为准（先直调，Windows 下 not_found 再试 cmd /c 包装），
不做静态猜测；`--no-probe` 才退化为按 CreateProcess 语义的 PATH 静态快查。
"""

from __future__ import annotations

import os
import shutil
from typing import Any

from . import run

# 内置体检清单：name 是展示名，cmd 是实际命令，probe 是版本探测参数
DOCTOR_SPECS: list[dict[str, Any]] = [
    {"name": "claude", "cmd": "claude", "probe": ["--version"]},
    {"name": "codex", "cmd": "codex", "probe": ["--version"]},
    {"name": "kilo", "cmd": "kilo", "probe": ["--version"]},
    {"name": "mcode", "cmd": "mcode", "probe": ["--version"]},
    {"name": "goose", "cmd": "goose", "probe": ["--version"]},
    {"name": "opencode", "cmd": "opencode", "probe": ["--version"]},
    {"name": "qoder", "cmd": "qoderclicn", "probe": ["--version"]},
    {"name": "crush", "cmd": "crush", "probe": ["--version"]},
    {"name": "hermes", "cmd": "hermes", "probe": ["--version"]},
]

FORM_MISSING = "未找到"
FORM_NATIVE = "原生 exe（直调）"
FORM_DIRECT_OK = "直调可用（自动命中同名 exe）"
FORM_SHIM = "npm shim（需 cmd /c 包装）"
FORM_INSTALLED = "已安装（直调）"


def _find_exe(cmd: str) -> str | None:
    """按 CreateProcess 语义搜 PATH：Windows 找 <cmd>.exe（shim 不算数）；含路径分隔符按原样判定。"""
    if os.sep in cmd or (os.altsep and os.altsep in cmd):
        return cmd if os.path.isfile(cmd) else None
    if os.name != "nt":
        return shutil.which(cmd)
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        candidate = os.path.join(d, cmd + ".exe")
        if os.path.isfile(candidate):
            return candidate
    return None


def _first_line(res: Any) -> str:
    """版本行：stdout 首行优先，stderr 兜底（部分 CLI 把版本打到 stderr）。"""
    src = (res.stdout_tail or "").strip().splitlines() or (res.stderr_tail or "").strip().splitlines()
    return src[0].strip()[:80] if src else ""


def _probe_one(cmd: str, probe: list[str], timeout: float) -> dict[str, Any]:
    """实跑探测：网关直调；Windows 下确认 shim 文件存在才用 cmd /c 复核（cmd 自身对
    不存在命令也能启动且退出非零，不能拿它判定形态）。"""
    res = run([cmd] + probe, timeout=timeout)
    if res.state == "not_found" and os.name == "nt":
        shim = shutil.which(cmd)
        if shim and shim.lower().endswith((".cmd", ".bat")):
            res = run(["cmd", "/c", cmd] + probe, timeout=timeout)
            return {
                "cmd": cmd,
                "form": FORM_SHIM,
                "ok": res.exit_code == 0 and res.state == "completed",
                "exit_code": res.exit_code,
                "state": res.state,
                "version": _first_line(res),
                "duration_ms": res.duration_ms,
                "log_path": res.log_path,
                "path": shim,
            }
    if res.state in ("not_found", "spawn_error"):
        form = FORM_MISSING
    else:
        form = FORM_NATIVE if _find_exe(cmd) else FORM_DIRECT_OK
    return {
        "cmd": cmd,
        "form": form,
        "ok": res.exit_code == 0 and res.state == "completed",
        "exit_code": res.exit_code,
        "state": res.state,
        "version": _first_line(res),
        "duration_ms": res.duration_ms,
        "log_path": res.log_path,
        "path": shutil.which(cmd) or "",
    }


def _static_one(cmd: str) -> dict[str, Any]:
    """静态快查：只按 PATH 判形态，不实跑（--no-probe；shim 判定对同名 exe 共存场景可能不准）。"""
    exe_hit = _find_exe(cmd)
    which_hit = shutil.which(cmd)
    if exe_hit:
        form, ok = FORM_NATIVE, True
    elif os.name == "nt" and which_hit:
        form, ok = FORM_SHIM, True
    elif which_hit:
        form, ok = FORM_INSTALLED, True
    else:
        form, ok = FORM_MISSING, False
    return {
        "cmd": cmd,
        "form": form,
        "ok": ok,
        "exit_code": None,
        "state": "static",
        "version": "",
        "duration_ms": None,
        "log_path": "",
        "path": which_hit or "",
    }


def doctor(commands: list[str] | None = None, probe: bool = True, timeout: float = 20.0) -> list[dict[str, Any]]:
    """体检本机 CLI，返回结构化行。commands 为空则用内置 9 个编码 CLI 清单。"""
    if commands:
        specs: list[dict[str, Any]] = [{"name": c, "cmd": c, "probe": ["--version"]} for c in commands]
    else:
        specs = DOCTOR_SPECS
    rows = []
    for spec in specs:
        if probe:
            row = _probe_one(spec["cmd"], list(spec["probe"]), timeout)
        else:
            row = _static_one(spec["cmd"])
        rows.append({"name": spec["name"], **row})
    return rows
