"""观测模式契约测试：原子事件 sidecar、元素周期表配方、化合物检索与分子式。

不直接 open 任何文件——一律走库 API（run/read_events/task_atoms/replay），
与产品同构。事实/推测分离：claude/codex 的内部事件 schema 未实测，
v1 解析只做行级 JSON 透传 + 全文 answer，测试按此契约锁定。
"""

from __future__ import annotations

import json
import sys
import contextlib
import io
import time
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
    render_atom_tree,
)


def _tmp_logdir(tmp_path: Path) -> str:
    d = tmp_path / "logs"
    d.mkdir()
    return str(d)


def _capture_task_output(d: str, task_id: str, *flags: str) -> list[str]:
    """捕获 task 子命令的人类可读输出，供树形渲染契约断言。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(["task", task_id, "--dir", d, *flags])
    assert rc == 0
    return buf.getvalue().splitlines()


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


def test_render_atom_tree_indents_and_summarizes_events() -> None:
    # 渲染层只消费事件字典：分支符由是否末条决定，长字段按契约截断
    events = [
        {"kind": "question", "payload": {"argv": ["claude", "修复测试"]}},
        {"kind": "text", "payload": {"text": "字" * 61}},
        {"kind": "tool_call", "payload": {"name": "Read", "input": {"f": "a.py"}}},
        {"kind": "tool_result", "payload": {"content": "x" * 41}},
        {"kind": "result", "payload": {"result": "ok", "usage": {"output_tokens": 1}}},
        {"kind": "system", "payload": {"subtype": "init", "raw": {"type": "system"}}},
        {"kind": "exit", "payload": {"exit_code": 0, "duration_ms": 12, "state": "completed"}},
    ]
    lines = render_atom_tree(events)
    assert lines[0] == "  ├─ question: 修复测试"
    assert lines[1] == "  ├─ text: " + "字" * 60
    assert lines[2] == '  ├─ tool_call: Read {"f":"a.py"}'
    assert lines[3] == "  ├─ tool_result: " + "x" * 40
    assert lines[4] == "  ├─ result: ok"
    assert lines[5] == "  ├─ system: init"
    assert lines[-1] == "  └─ exit: exit_code=0 duration_ms=12 state=completed"


# --- 类型化谱线（fixture 为 2026-10-19 真机采样，脱敏） ----------------------


def test_typed_claude_lines() -> None:
    text = "\n".join(
        [
            '{"type":"system","subtype":"init","session_id":"s1"}',
            '{"type":"assistant","message":{"content":[{"type":"text","text":"收到"}]},"session_id":"s1"}',
            '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read","input":{"f":"a.py"}}]}}',
            '{"type":"user","message":{"content":[{"type":"tool_result","content":"file body"}]}}',
            '{"type":"result","subtype":"success","result":"收到","is_error":false,"usage":{"output_tokens":20}}',
            "plain text line ignored",
        ]
    )
    evs = parse_stream(text, "claude")
    kinds = [e["kind"] for e in evs]
    assert kinds == ["system", "text", "tool_call", "tool_result", "result"]
    assert evs[1]["payload"]["text"] == "收到"
    assert evs[2]["payload"]["name"] == "Read" and evs[2]["payload"]["input"] == {"f": "a.py"}
    assert evs[4]["payload"]["result"] == "收到"
    # 守恒律：raw 永远在
    assert all("raw" in e["payload"] for e in evs)


def test_typed_codex_lines() -> None:
    text = "\n".join(
        [
            '{"type":"thread.started","thread_id":"01a"}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"收到"}}',
            '{"type":"turn.completed","usage":{"input_tokens":17244,"output_tokens":3}}',
        ]
    )
    evs = parse_stream(text, "codex")
    kinds = [e["kind"] for e in evs]
    assert kinds == ["system", "system", "text", "result"]
    assert evs[0]["payload"]["subtype"] == "thread_started"
    assert evs[2]["payload"]["text"] == "收到"
    assert evs[3]["payload"]["usage"]["output_tokens"] == 3


def test_typed_unknown_item_falls_back_to_note() -> None:
    # 未收录的 codex item 形态 → note 兜底（守恒律），不丢字
    evs = parse_stream('{"type":"item.completed","item":{"id":"x","type":"future_thing","data":1}}', "codex")
    assert evs[0]["kind"] == "note"
    assert evs[0]["payload"]["raw"]["item"]["type"] == "future_thing"


def test_observe_produces_typed_events_for_real_element(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 集成：给 python 临时注册 jsonl 配方（monkeypatch，测试后自动恢复），
    # 观测模式 → question/system/text/result/answer/exit 类型化事件
    from cli_gateway import events as ev

    monkeypatch.setitem(ev.ELEMENTS, "python", {"format": "jsonl", "parser": "claude", "flags": []})
    d = _tmp_logdir(tmp_path)
    fake_claude = [
        '{"type":"system","subtype":"init"}',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"typed-ok"}]}}',
        '{"type":"result","subtype":"success","result":"typed-ok"}',
    ]
    fixture = tmp_path / "fixture.jsonl"
    fixture.write_text("\n".join(fake_claude), encoding="utf-8")
    res = run(
        [sys.executable, "-c", "import sys; print(open(sys.argv[1]).read(), end='')", str(fixture)],
        log_dir=d, observe=True, task_id="T-typed",
    )
    assert res.state == "completed"
    events = read_events(res.log_path + ".events.jsonl")
    kinds = [e["kind"] for e in events]
    assert "text" in kinds and "result" in kinds and "system" in kinds
    assert kinds[0] == "question" and kinds[-1] == "exit"


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


def _run_typed_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task_id: str) -> str:
    """生成一个带 system/text/result 谱线的观测原子，作为 task --tree 输入。"""
    monkeypatch.setitem(ELEMENTS, "python", {"format": "jsonl", "parser": "claude", "flags": []})
    d = _tmp_logdir(tmp_path)
    fixture = tmp_path / "fixture.jsonl"
    fixture.write_text(
        "\n".join(
            [
                '{"type":"system","subtype":"init"}',
                '{"type":"assistant","message":{"content":[{"type":"text","text":"typed-ok"}]}}',
                '{"type":"result","subtype":"success","result":"typed-ok"}',
            ]
        ),
        encoding="utf-8",
    )
    rc = main(
        [
            "run", "--observe", "--task-id", task_id, "--dir", d, "--",
            sys.executable, "-c", "import sys; print(open(sys.argv[1]).read(), end='')", str(fixture),
        ]
    )
    assert rc == 0
    return d


def test_task_tree_hides_system_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = _run_typed_task(tmp_path, monkeypatch, "T-tree")
    lines = _capture_task_output(d, "T-tree", "--tree")
    # 原子头在前，事件树缩进在后；system 心跳默认不出现在树中
    assert any("python" in line and "exit=0" in line and "completed" in line for line in lines)
    assert any(line.startswith("  ├─ question: ") for line in lines)
    assert any(line.startswith("  ├─ text: typed-ok") for line in lines)
    assert any(line.startswith("  └─ exit: exit_code=0") for line in lines)
    assert not any(line.startswith("  ├─ system:") or line.startswith("  └─ system:") for line in lines)


def test_task_tree_all_displays_system_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = _run_typed_task(tmp_path, monkeypatch, "T-tree-all")
    lines = _capture_task_output(d, "T-tree-all", "--tree", "--all")
    assert "  ├─ system: init" in lines
    assert any(line.startswith("  ├─ question: ") for line in lines)
    assert any(line.startswith("  └─ exit: exit_code=0") for line in lines)


def test_render_neutralizes_newlines_and_clips_result() -> None:
    # 评审 P1 回归：换行不撑破树布局、result 截断、error 优先 message
    from cli_gateway.events import render_atom_tree

    events = [
        {"kind": "text", "payload": {"text": "line1\nline2\nline3"}},
        {"kind": "result", "payload": {"result": "x" * 200}},
        {"kind": "error", "payload": {"message": "boom-real", "raw": {"type": "item.completed"}}},
    ]
    lines = render_atom_tree(events)
    assert len(lines) == 3
    assert all("\n" not in line for line in lines)
    assert len(lines[1]) < 80
    assert "boom-real" in lines[2]


def test_build_graph_and_html(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 分子数据面：nodes/links/公式/色卡；渲染面：自包含 HTML 内嵌数据
    from cli_gateway import events as ev

    monkeypatch.setitem(ev.ELEMENTS, "python", {"format": "jsonl", "parser": "claude", "flags": []})
    d = _tmp_logdir(tmp_path)
    for i in range(2):
        run([sys.executable, "-c", f"print('g{i}')"], log_dir=d, observe=True, task_id="T-graph")
    g = ev.build_graph("T-graph", d)
    assert g["formula"] == "python×2"
    assert len(g["nodes"]) == 2 and len(g["links"]) == 1
    assert g["nodes"][0]["color"] == "#94a3b8"  # 未知元素中性灰
    assert g["links"][0]["source"] == 0 and g["links"][0]["target"] == 1
    # 同秒两次调用的文件序由随机后缀决定——按集合断言，不假设先后
    qs = {n["question"] for n in g["nodes"]}
    assert qs == {"print('g0')", "print('g1')"}
    assert any(k == "answer" for k in g["nodes"][0]["kinds"])
    assert g["nodes"][0]["preview"] and g["nodes"][0]["preview"][0]["kind"] in ("question", "system", "text", "note", "answer")

    html = ev.render_html(g)
    assert html.startswith("<!DOCTYPE html>")
    assert "const G = " in html and '"formula": "python×2"' in html
    assert "</script>" in html
    # 内嵌 JSON 块不得含未转义的闭合序列（提前终止 script 的风险）
    import re

    m = re.search(r"const G = (\{.*?\});\n", html, re.S)
    assert m is not None and "</" not in m.group(1)


def test_element_color_table() -> None:
    from cli_gateway.events import element_color

    assert element_color("claude") == "#f59e0b" and element_color("codex") == "#10b981"
    assert element_color("no-such-element") == "#94a3b8"


def test_bond_header_and_graph_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 显式化学键：--bond 声明输入来源 → header 落盘 → build_graph 跳线连接
    from cli_gateway import events as ev

    monkeypatch.setitem(ev.ELEMENTS, "python", {"format": "jsonl", "parser": "claude", "flags": []})
    d = _tmp_logdir(tmp_path)
    r0 = run([sys.executable, "-c", "print('root')"], log_dir=d, observe=True, task_id="T-bond")
    id0 = Path(r0.log_path).stem
    r1 = run([sys.executable, "-c", "print('mid')"], log_dir=d, observe=True, task_id="T-bond")
    # 第三个原子显式键回连第一个（绕过中间）。文件名时间戳为秒级（对外契约），
    # POSIX 跑得快会同秒 → 排序随机；强制跨秒保证时序确定
    time.sleep(1.05)
    r2 = run([sys.executable, "-c", "print('leaf')"], log_dir=d, observe=True, task_id="T-bond",
             bond=id0)
    atoms = task_atoms("T-bond", d)
    assert len(atoms) == 3 and all(a["bond"] == "" or a["atom"] == Path(r2.log_path).stem for a in atoms)
    g = ev.build_graph("T-bond", d)
    id_by_idx = {i: n["id"] for i, n in enumerate(g["nodes"])}
    leaf_idx = next(i for i, n in enumerate(g["nodes"]) if n["id"] == Path(r2.log_path).stem)
    incoming = [l for l in g["links"] if l["target"] == leaf_idx]
    assert len(incoming) == 1  # 显式键取代时序链，不叠加
    src_id = id_by_idx[incoming[0]["source"]]
    assert src_id == id0 and incoming[0].get("explicit") is True


def test_serve_api_galaxy_and_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # serve：本机回环 + /api/galaxy + /api/graph + SPA 页面
    import threading
    import urllib.request

    from cli_gateway import events as ev
    from cli_gateway.serve import make_server

    monkeypatch.setitem(ev.ELEMENTS, "python", {"format": "jsonl", "parser": "claude", "flags": []})
    d = _tmp_logdir(tmp_path)
    run([sys.executable, "-c", "print('srv')"], log_dir=d, observe=True, task_id="T-serve")
    httpd = make_server(port=0, log_dir=d)  # 端口 0 = 系统分配，测试无冲突
    # SSRF 边界自证：被测服务端只绑环回，下面的请求永远指向测试自身实例
    assert httpd.server_address[0] == "127.0.0.1"
    port = httpd.server_address[1]
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    try:
        base = f"http://127.0.0.1:{port}"
        page = urllib.request.getstart = urllib.request.urlopen(base + "/")
        assert page.status == 200 and b"canvas" in page.read()
        gal = json.loads(urllib.request.urlopen(base + "/api/galaxy").read())
        assert any(t["task"] == "T-serve" for t in gal["tasks"])
        g = json.loads(urllib.request.urlopen(base + "/api/graph?task=T-serve").read())
        assert g["formula"] == "python×1" and g["nodes"][0]["n_events"] >= 3
        try:
            urllib.request.urlopen(base + "/api/graph")  # 缺 task → 400
            raised = False
        except urllib.error.HTTPError as e:
            raised = e.code == 400
        assert raised
    finally:
        httpd.shutdown()
        httpd.server_close()


def _capture_task_json(d: str, task_id: str) -> list[str]:
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(["task", task_id, "--dir", d, "--json"])
    assert rc == 0
    return buf.getvalue().splitlines()
