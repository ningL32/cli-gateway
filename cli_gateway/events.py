"""cli-gateway events：观测模式的原子/元素/化合物模型。

物理化学映射（设计的唯一依据，命名与语义在此定义）：
- 原子：一次网关调用。问（argv/prompt）与答（输出）成对出现、不可分割——
  exit_code/duration_ms 是"结合完成"的证据。原子记录永不修改原始日志。
- 元素：CLI 的种类。每种元素有一条"观测配方"——激发它以离散谱线（结构化
  JSON 事件流）而非连续谱（纯文本）发光。同位素 = 版本/格式差异（解析性不同）。
- 化合物：一个任务 = 化学键（上一个原子的输出成为下一个原子的输入）连接的
  原子图。分子式 = 组成配比（如 claude×3 + codex×2）。

守恒律：原始日志是唯一真相，events sidecar（<log>.events.jsonl）是派生的
结构化解读——解析失败永远降级为"全文 answer 事件"，绝不丢字。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# 元素周期表：已知能以结构化谱线发光的元素及其观测配方（flags 追加在 argv 末尾）。
# 其余元素默认 text（连续谱）：照常执行，仅记录全文 answer 事件。
ELEMENTS: dict[str, dict[str, Any]] = {
    "claude": {"format": "jsonl", "parser": "claude", "flags": ["--output-format", "stream-json", "--verbose"]},
    "codex": {"format": "jsonl", "parser": "codex", "flags": ["--json"]},
}


def _element_name(argv: list[str]) -> str:
    """从 argv 提取元素名：剥掉 cmd /c 包装（Windows npm shim），取首参文件名。"""
    parts = list(argv)
    if len(parts) >= 3 and parts[0].lower() == "cmd" and parts[1].lower() == "/c":
        parts = parts[2:]
    return Path(parts[0]).stem.lower() if parts else ""


def observation_format(element: str) -> str:
    """该元素的谱线类型：jsonl（离散谱）或 text（连续谱）。"""
    return str(ELEMENTS.get(element, {}).get("format", "text"))


def observation_flags(element: str) -> list[str]:
    """该元素的观测配方；未知元素返回空（照原样执行）。"""
    return list(ELEMENTS.get(element, {}).get("flags", []))


def _typed_claude(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """claude stream-json 谱线 → 类型化事件（schema 来自真机实测样本）。"""
    t = obj.get("type")
    if t == "system":
        return [{"type": "system", "subtype": obj.get("subtype"), "raw": obj}]
    if t == "assistant":
        out: list[dict[str, Any]] = []
        for block in (obj.get("message") or {}).get("content") or []:
            bt = block.get("type") if isinstance(block, dict) else None
            if bt == "text":
                out.append({"type": "text", "text": block.get("text", ""), "raw": obj})
            elif bt == "tool_use":
                out.append({"type": "tool_call", "name": block.get("name"), "input": block.get("input"), "raw": obj})
            elif bt == "tool_result":
                out.append({"type": "tool_result", "content": block.get("content"), "raw": obj})
        return out
    if t == "user":
        out = []
        for block in (obj.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                out.append({"type": "tool_result", "content": block.get("content"), "raw": obj})
        return out
    if t == "result":
        return [{"type": "result", "subtype": obj.get("subtype"), "result": obj.get("result"),
                 "is_error": obj.get("is_error"), "usage": obj.get("usage"), "raw": obj}]
    return []


# codex item.type → 归一化事件。agent_message/error 为真机实测；其余 item 形态
# （command_execution/file_change/reasoning/mcp_tool_call/web_search）的字段名为
# 【推测，待真任务样本回填】——取值全走 .get 宽容兜底，不匹配也只是退化为 note。
_CODEX_ITEM_MAP = {
    "agent_message": lambda it: {"type": "text", "text": it.get("text")},
    "error": lambda it: {"type": "error", "message": it.get("message")},
    "reasoning": lambda it: {"type": "reasoning", "text": it.get("text")},
    "command_execution": lambda it: {"type": "tool_call", "name": "shell", "input": it.get("command")},
    "file_change": lambda it: {"type": "tool_call", "name": "file_change", "input": it.get("changes")},
    "mcp_tool_call": lambda it: {"type": "tool_call", "name": it.get("tool"), "input": it.get("arguments")},
    "web_search": lambda it: {"type": "tool_call", "name": "web_search", "input": it.get("query")},
}


def _typed_codex(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """codex exec --json 谱线 → 类型化事件（thread/turn/item 模型，实测样本归纳）。"""
    t = obj.get("type")
    if t == "thread.started":
        return [{"type": "system", "subtype": "thread_started", "thread_id": obj.get("thread_id"), "raw": obj}]
    if t == "turn.started":
        return [{"type": "system", "subtype": "turn_started", "raw": obj}]
    if t == "turn.completed":
        return [{"type": "result", "usage": obj.get("usage"), "raw": obj}]
    if t in ("item.started", "item.updated", "item.completed"):
        item = obj.get("item") or {}
        mapper = _CODEX_ITEM_MAP.get(item.get("type"))
        if mapper is None:
            return []
        ev = mapper(item)
        ev["raw"] = obj
        ev["item_phase"] = t
        return [ev]
    return []


_TYPED_DISPATCH = {"claude": _typed_claude, "codex": _typed_codex}


def parse_stream(text: str, element: str = "") -> list[dict[str, Any]]:
    """从输出文本提取结构化谱线事件：逐行 JSON，按元素分发类型化解析。

    守恒律三层兜底：元素有类型化解析器且行匹配 → 语义事件（tool_call/text/
    reasoning/system/result/error）；行是合法 JSON 但不匹配已知 schema → note
    透传；非 JSON 行 → 忽略（全文由 answer 事件兜底）。payload.raw 永远保留。
    """
    # 配方与解读统一在周期表：parser 字段指向分派器，注册配方 = 注册解读
    parser_name = ELEMENTS.get(element, {}).get("parser")
    dispatch = _TYPED_DISPATCH.get(parser_name) if parser_name else None
    notes: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            if isinstance(obj, (list, int, float, bool)) or obj is None:
                notes.append({"kind": "note", "payload": {"raw": obj}})
            continue
        typed = dispatch(obj) if dispatch else None
        if typed:
            for ev in typed:
                notes.append({"kind": ev.pop("type", "note"), "payload": {"raw": ev.pop("raw", obj), **ev}})
        else:
            notes.append({"kind": "note", "payload": {"raw": obj}})
    return notes


def build_events(
    *,
    atom: str,
    task_id: str | None,
    element: str,
    ts: str,
    argv: list[str],
    output_text: str | None,
    exit_code: int | None,
    duration_ms: int,
    state: str,
) -> list[dict[str, Any]]:
    """把一次调用组装成原子事件序列（question → [note…] → answer → exit）。"""
    events: list[dict[str, Any]] = []

    def emit(kind: str, payload: dict[str, Any]) -> None:
        events.append(
            {
                "v": SCHEMA_VERSION,
                "atom": atom,
                "task": task_id,
                "element": element,
                "ts": ts,
                "kind": kind,
                "payload": payload,
            }
        )

    emit("question", {"argv": argv})
    if output_text is not None:
        for note in parse_stream(output_text, element):
            note["atom"], note["task"], note["element"], note["ts"] = atom, task_id, element, ts
            events.append(note)
        emit("answer", {"text": output_text})
    emit("exit", {"exit_code": exit_code, "duration_ms": duration_ms, "state": state})
    for i, e in enumerate(events):
        e["seq"] = i
    return events


def _event_text(value: Any) -> str:
    """把事件字段转成可渲染摘要；非字符串用紧凑 JSON，避免 Python 字典噪声。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _clip_event_text(value: Any, limit: int) -> str:
    """摘要收口：清洗换行（`_one_line` 先例——树形布局每行恰一个事件）+ 按字符
    数截断（中文一字一字符），避免长输出/多行文本撑破树视图。"""
    cleaned = _event_text(value).replace("\r", " ").replace("\n", " ")
    return cleaned[:limit]


def _fallback_summary(event: dict[str, Any]) -> str:
    """system/error/note 的摘要：error 类优先 message（真实报错信息——
    codex 的 error 事件 raw.type 是外层 item.completed，按 type 优先会把
    报错正文吞掉），其余优先 subtype，最后 raw 原生 type。"""
    payload = event.get("payload") or {}
    raw = payload.get("raw")
    if payload.get("message") is not None:
        return _event_text(payload["message"])
    summary = payload.get("subtype") or (raw.get("type") if isinstance(raw, dict) else None)
    if summary is not None:
        return str(summary)
    if raw is not None:
        return _event_text(raw)
    return "(empty)"


def render_atom_tree(events: list[dict]) -> list[str]:
    """把一次调用的原子事件序列渲染成缩进树（每行一个事件摘要）。

    - 固定前缀为两个空格；除末行用 `└─`，其余用 `├─`
    - question 取 argv 的最后一个元素；text/reasoning/answer 截取 text 前 60 字符
    - tool_call 取 name + input 前 60 字符；tool_result 取 content 前 40 字符
    - result 优先展示 result，否则展示 usage；system/error/note 展示类型摘要
    """
    lines: list[str] = []
    for index, event in enumerate(events):
        kind = str(event.get("kind", "unknown"))
        payload = event.get("payload") or {}
        if kind == "question":
            argv = payload.get("argv") or []
            summary = str(argv[-1]) if argv else "(empty)"
        elif kind in ("text", "reasoning", "answer"):
            summary = _clip_event_text(payload.get("text"), 60)
        elif kind == "tool_call":
            summary = _clip_event_text(
                f"{payload.get('name') or 'unknown'} {_event_text(payload.get('input'))}", 60
            )
        elif kind == "tool_result":
            summary = _clip_event_text(payload.get("content"), 40)
        elif kind == "result":
            # result 字段可能是全文答复：与其他摘要同一截断纪律（契约见 docstring）
            summary = _clip_event_text(
                payload.get("result")
                if payload.get("result") is not None
                else payload.get("usage"),
                60,
            )
        elif kind == "exit":
            summary = (
                f"exit_code={payload.get('exit_code')} "
                f"duration_ms={payload.get('duration_ms')} state={payload.get('state')}"
            )
        else:
            # system/error/note 与未来新增类型都走宽容摘要，渲染层绝不抛异常
            summary = _fallback_summary(event)
        branch = "└─" if index == len(events) - 1 else "├─"
        lines.append(f"  {branch} {kind}: {summary}")
    return lines


def write_events(sidecar: Path, events: list[dict[str, Any]]) -> None:
    """原子事件写 sidecar（JSONL，一行一事件；UTF-8）。路径先 resolve 规范化。"""
    target = Path(sidecar).resolve()
    with open(target, "w", encoding="utf-8", newline="") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def read_events(sidecar: str | Path) -> list[dict[str, Any]]:
    """读取一次调用的原子事件序列。路径 resolve 规范化（消解 .. 段）。"""
    p = Path(sidecar).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"events sidecar not found: {p}")
    events: list[dict[str, Any]] = []
    with open(p, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 半行/损坏行跳过，不拖垮整个序列
    return events


def task_formula(atoms: list[dict[str, str]]) -> str:
    """分子式：任务内元素的组成配比（按原子数降序，如 "claude×3 + codex×2"）。"""
    counts = Counter(a.get("element", "") for a in atoms if a.get("element"))
    if not counts:
        return "(empty)"
    return " + ".join(f"{name}×{n}" for name, n in counts.most_common())


def task_atoms(task_id: str, log_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """化合物检索：扫描日志目录，返回属于该任务的原子（含事件摘要）。

    判定依据是日志头部的 `# task:` 行（观测模式写入）。返回按文件名（时间序）
    排列的原子摘要：atom / element / exit_code / state / duration_ms / events
    sidecar 路径。
    """
    root = Path(log_dir) if log_dir else None
    if root is None:
        from . import DEFAULT_LOG_DIR

        root = DEFAULT_LOG_DIR
    if not root.is_dir():
        return []
    atoms: list[dict[str, Any]] = []
    for log_path in sorted(root.glob("*.log")):
        try:
            # 头部（task/element 等元数据）与尾部（trailer 的结果字段）分段读，
            # 避免大日志整读进内存
            with open(log_path, encoding="utf-8", errors="replace") as f:
                head = f.read(4096)
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 2048))
                tail = f.read()
        except OSError:
            continue
        task_line = next((l for l in head.splitlines() if l.startswith("# task: ")), None)
        if task_line is None or task_line[len("# task: "):].strip() != task_id:
            continue

        def _field(prefix: str, section: str) -> str:
            line = next((l for l in section.splitlines() if l.startswith(prefix)), "")
            return line[len(prefix):].strip() if line else ""

        element = _field("# element: ", head) or "unknown"
        exit_code = _field("# exit_code: ", tail)
        state = _field("# state: ", tail)
        duration = _field("# duration_ms: ", tail)
        sidecar = Path(str(log_path) + ".events.jsonl")
        atoms.append(
            {
                "task": task_id,
                "element": element,
                "atom": log_path.stem,
                "log_path": str(log_path),
                "events_path": str(sidecar) if sidecar.is_file() else "",
                "exit_code": int(exit_code) if exit_code.lstrip("-").isdigit() else None,
                "state": state or "unknown",
                "duration_ms": int(duration) if duration.isdigit() else None,
            }
        )
    return atoms
