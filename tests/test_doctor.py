"""doctor 契约测试：内置清单形态、实跑探测、缺失命令、静态模式、CreateProcess 语义搜索。

探测测试用 sys.executable（任何平台都存在的绝对路径命令），不依赖具体编码 CLI。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cli_gateway.doctor import DOCTOR_SPECS, _find_exe, doctor  # noqa: E402


def test_doctor_specs_shape() -> None:
    names = [s["name"] for s in DOCTOR_SPECS]
    assert len(names) == 9 and len(set(names)) == 9
    for s in DOCTOR_SPECS:
        assert isinstance(s["cmd"], str) and s["cmd"]
        assert isinstance(s["probe"], list) and s["probe"]


def test_doctor_finds_interpreter() -> None:
    (row,) = doctor(commands=[sys.executable], probe=True, timeout=30)
    assert row["ok"] is True
    assert row["state"] == "completed"
    assert row["exit_code"] == 0
    assert row["version"]  # "Python 3.x.y" 首行
    assert row["log_path"]


def test_doctor_missing_cli() -> None:
    (row,) = doctor(commands=["definitely-no-such-cli-xyz"], probe=True, timeout=20)
    assert row["ok"] is False
    assert row["form"] == "未找到"
    assert row["exit_code"] == 127


def test_doctor_static_mode() -> None:
    (row,) = doctor(commands=[sys.executable], probe=False)
    assert row["ok"] is True
    assert row["state"] == "static"
    assert row["exit_code"] is None
    assert row["path"]


def test_find_exe_absolute_path() -> None:
    assert _find_exe(sys.executable) == sys.executable
    assert _find_exe("definitely-no-such-cli-xyz") is None
