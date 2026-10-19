"""观测模式契约测试：原子事件 sidecar、元素周期表配方、化合物检索与分子式。

不直接 open 任何文件——一律走库 API（run/read_events/task_atoms/replay），
与产品同构。事实/推测分离：claude/codex 的内部事件 schema 未实测，
v1 解析只做行级 JSON 透传 + 全文 answer，测试按此契约锁定。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cli_gateway import (  # noqa: E402
    read_events,
    run,
    task_atoms,
    task_formula,
)
from cli_gateway.__main__ import main  # noqa: E402
from cli_gateway.events import (  # noqa: E402
    ELEMENTS,
    observation_flags,
    observation_format,
    parse_stream,
)


def _tmp_logdir(tmp_path: Path) -> str:
    d = tmp_path / "logs"
    d.mkdir()
    return str(d)


def test_element_table_recipes() -> None:
    assert observation_format("claude") == "jsonl"
    assert "--output-format" in observation_flags("claude")
    assert observation_format("codex") == "jsonl"
    # 未知元素 = 连续谱：无配方、text 谱线
    assert observation_format("python") == "text"
    assert observation_flags("python") == []
    assert set(ELEMENTS) >= {"claude", "codex"}


def test_parse_stream_jsonl_and_fallback() -> None:
    text = 'not json\n{"type": "tool_use", "id": 1}\n\n{"broken": \n{"type": "done"}\n'
    notes = parse_stream(text)
    assert len(notes) == 2  # 两条合法 JSON 行；坏行忽略
    assert notes[0]["kind"] == "note"
    assert notes[0]["payload"]["raw"]["type"] == "tool_use"


def test_observe_writes_sidecar_with_atom_events(tmp_path: Path) -> None:
    d = _tmp_logdir(tmp_path)
    res = run(
        [sys.executable, "-c", "print('hello-observe')"],
        log_dir=d,
        observe=True,
        task_id="T-atom",
    )
    assert res.state == "completed"
    sidecar = Path(res.log_path + ".events.jsonl")
    assert sidecar.is_file()
    events = read_events(sidecar)
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "question" and kinds[-1] == "exit"
    assert "answer" in kinds
    # 原子不可分：question 携带完整 argv，answer 携带全文，exit 携带结合证据
    assert events[0]["payload"]["argv"][-1] == "print('hello-observe')"
    answer = next(e for e in events if e["kind"] == "answer")
    assert "hello-observe" in answer["payload"]["text"]
    exit_ev = events[-1]
    assert exit_ev["payload"]["exit_code"] == 0 and exit_ev["payload"]["state"] == "completed"
    # 元数据：task 绑定 + 元素 + 谱线类型都落在事件与日志头
    assert all(e["task"] == "T-atom" for e in events)
    assert all(e["element"] == "python" for e in events)
    header = Path(res.log_path).read_text(encoding="utf-8").split("--- STDOUT ---")[0]
    assert "# task: T-atom" in header
    assert "# element: python" in header
    assert "# observe: text" in header  # 未知元素照常执行，argv 无追加


def test_read_events_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_events(tmp_path / "no-such.events.jsonl")


def test_task_binding_and_formula(tmp_path: Path) -> None:
    d = _tmp_logdir(tmp_path)
    for i in range(2):
        run(
            [sys.executable, "-c", f"print({i})"],
            log_dir=d,
            observe=True,
            task_id="T-mol",
        )
    atoms = task_atoms("T-mol", d)
    assert len(atoms) == 2
    assert all(a["element"] == "python" and a["exit_code"] == 0 for a in atoms)
    assert all(a["events_path"] for a in atoms)
    assert task_formula(atoms) == "python×2"
    # 异任务 ID 互不串
    assert task_atoms("T-other", d) == []


def test_task_formula_mixed_elements() -> None:
    atoms = [{"element": "claude"}, {"element": "claude"}, {"element": "codex"}]
    assert task_formula(atoms) == "claude×2 + codex×1"
    assert task_formula([]) == "(empty)"


def test_cli_run_observe_and_task_roundtrip(tmp_path: Path) -> None:
    d = _tmp_logdir(tmp_path)
    rc = main(
        ["run", "--observe", "--task-id", "T-cli", "--dir", d, "--",
         sys.executable, "-c", "print(7)"],
    )
    assert rc == 0
    out = json.loads("\n".join(_capture_task_json(d, "T-cli")))
    assert out["formula"] == "python×1"
    assert len(out["atoms"]) == 1
    assert out["atoms"][0]["exit_code"] == 0


def _capture_task_json(d: str, task_id: str) -> list[str]:
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(["task", task_id, "--dir", d, "--json"])
    assert rc == 0
    return buf.getvalue().splitlines()
