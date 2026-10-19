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
import threading
from collections import Counter
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# 元素周期表：已知能以结构化谱线发光的元素及其观测配方（flags 追加在 argv 末尾）。
# 其余元素默认 text（连续谱）：照常执行，仅记录全文 answer 事件。
ELEMENTS: dict[str, dict[str, Any]] = {
    "claude": {"format": "jsonl", "parser": "claude", "color": "#f59e0b", "flags": ["--output-format", "stream-json", "--verbose"]},
    "codex": {"format": "jsonl", "parser": "codex", "color": "#10b981", "flags": ["--json"]},
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


def parse_line(line: str, element: str = "") -> list[dict[str, Any]]:
    """单行谱线解析（流式场景逐行调用，内存有界）。返回事件片段列表（可能为空）。

    守恒律分层：类型化解析器命中 → 语义事件；合法 JSON 不匹配 → note 透传；
    非 JSON 行 → 空（全文由 answer 事件兜底）。payload.raw 永远保留。
    """
    line = line.strip()
    if not line:
        return []
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(obj, dict):
        if isinstance(obj, (list, int, float, bool)) or obj is None:
            return [{"kind": "note", "payload": {"raw": obj}}]
        return []
    # 配方与解读统一在周期表：parser 字段指向分派器，注册配方 = 注册解读
    parser_name = ELEMENTS.get(element, {}).get("parser")
    dispatch = _TYPED_DISPATCH.get(parser_name) if parser_name else None
    typed = dispatch(obj) if dispatch else None
    if typed:
        return [{"kind": ev.pop("type", "note"), "payload": {"raw": ev.pop("raw", obj), **ev}} for ev in typed]
    return [{"kind": "note", "payload": {"raw": obj}}]


def parse_stream(text: str, element: str = "") -> list[dict[str, Any]]:
    """从输出文本提取结构化谱线事件：逐行 JSON，按元素分发类型化解析。

    守恒律三层兜底：元素有类型化解析器且行匹配 → 语义事件（tool_call/text/
    reasoning/system/result/error）；行是合法 JSON 但不匹配已知 schema → note
    透传；非 JSON 行 → 忽略（全文由 answer 事件兜底）。payload.raw 永远保留。
    """
    notes: list[dict[str, Any]] = []
    for line in text.splitlines():
        for ev in parse_line(line, element):
            notes.append(ev)
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


def stream_events(
    sidecar: Path,
    *,
    atom: str,
    log_path: Path,
    argv: list[str],
    task_id: str | None,
    element: str,
    ts: str,
    stdout_part: Path | None,
    stderr_part: Path | None,
    exit_code: int | None,
    duration_ms: int,
    state: str,
    preview_chars: int = 500,
) -> int:
    """流式写原子事件 sidecar（v1.7 内存有界）。

    逐行读 part 解析谱线，全程只持有一行；answer 事件不再内嵌全文——改为
    text_ref 外置引用（raw 日志本身就是全文）+ 有界 preview。返回事件数。
    消费方需要全文时用 answer_text() 显式取（内存换信息，调用方自决）。
    """
    count = 0
    meta = {"atom": atom, "task": task_id, "element": element, "ts": ts}
    with open(Path(sidecar).resolve(), "w", encoding="utf-8", newline="") as f:
        def emit(kind: str, payload: dict[str, Any]) -> None:
            nonlocal count
            rec = {"v": SCHEMA_VERSION, **meta, "seq": count, "kind": kind, "payload": payload}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            count += 1

        emit("question", {"argv": argv})
        for part in (stdout_part, stderr_part):
            if part is None:
                continue
            try:
                with open(part, encoding="utf-8", errors="replace") as pf:
                    for line in pf:
                        for ev in parse_line(line, element):
                            ev.update(meta)
                            ev["seq"] = count
                            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
                            count += 1
            except OSError:
                continue  # part 读不到（杀软/清理竞态）：谱线缺失，answer 兜底仍在
        # answer：外置引用 + 有界预览（预览取 stdout 前 preview_chars 字符）
        preview = ""
        try:
            if stdout_part is not None and stdout_part.is_file():
                preview = stdout_part.read_text(encoding="utf-8", errors="replace")[:preview_chars]
        except OSError:
            preview = ""
        emit(
            "answer",
            {
                "text_ref": {"log": str(log_path), "streams": ["stdout", "stderr"]},
                "preview": preview,
            },
        )
        emit("exit", {"exit_code": exit_code, "duration_ms": duration_ms, "state": state})
    return count


def answer_text(events: list[dict[str, Any]]) -> str:
    """从 answer 事件取全文：text_ref 指回 raw 日志（守恒律：原文在日志里）。

    新 schema（text_ref）读取日志 STDOUT/STDERR 段合并；旧 schema（内嵌 text）
    直接返回。调用方显式调用 = 自愿承担全文内存。
    """
    ans = next((e for e in events if e.get("kind") == "answer"), None)
    if ans is None:
        return ""
    payload = ans.get("payload") or {}
    if "text" in payload:
        return str(payload["text"])
    ref = payload.get("text_ref") or {}
    log_path = ref.get("log")
    if not log_path:
        return str(payload.get("preview", ""))
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            full = f.read()
    except OSError:
        return str(payload.get("preview", ""))
    out: list[str] = []
    for section in ("STDOUT", "STDERR"):
        marker = f"--- {section} ---"
        start = full.find(marker)
        if start < 0:
            continue
        start += len(marker)
        end = full.find("--- END ---", start)
        chunk = full[start:end if end > 0 else len(full)]
        if chunk.strip():
            out.append(chunk.strip("\n"))
    return "\n".join(out)


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


# 元素色卡（球棍模型渲染用；agents/*.md frontmatter color 的同源配色，未知元素灰）
ELEMENT_COLORS: dict[str, str] = {
    "claude": "#f59e0b",
    "codex": "#10b981",
    "goose": "#ef4444",
    "opencode": "#06b6d4",
    "qoder": "#3b82f6",
    "mcode": "#eab308",
    "kilo": "#d97706",
    "crush": "#ec4899",
    "hermes": "#f472b6",
}


def element_color(name: str) -> str:
    """元素色：周期表配方优先，色卡兜底，未知元素中性灰。"""
    return str(ELEMENTS.get(name, {}).get("color") or ELEMENT_COLORS.get(name) or "#94a3b8")


def _atom_preview(events: list[dict[str, Any]], cap: int = 40) -> list[dict[str, str]]:
    """原子内事件的树视图摘要（system 心跳剔除，超长裁剪）——HTML 下钻面板用。"""
    out: list[dict[str, str]] = []
    for e in events:
        kind = str(e.get("kind", "note"))
        if kind == "system":
            continue
        payload = e.get("payload") or {}
        if kind == "question":
            argv = payload.get("argv") or []
            summary = str(argv[-1])[:120] if argv else ""
        elif kind in ("text", "reasoning", "answer"):
            summary = str(payload.get("text", ""))[:160]
        elif kind == "tool_call":
            summary = f"{payload.get('name') or '?'} {str(payload.get('input', ''))[:120]}"
        elif kind == "tool_result":
            summary = str(payload.get("content", ""))[:120]
        else:
            summary = str(payload.get("message") or payload.get("result") or "")[:120]
        out.append({"kind": kind, "summary": summary})
        if len(out) >= cap:
            break
    return out


def build_graph(task_id: str, log_dir: str | Path | None = None) -> dict[str, Any]:
    """分子数据面：task → 球棍模型 JSON（nodes/links/formula）。

    nodes：原子（颜色=元素色卡、大小原料=duration、下钻预览=类型化事件摘要）；
    links：task 内相邻原子的时序键（weight=上一个原子 answer 的字节数，数据流量）。
    """
    from datetime import datetime as _dt

    atoms = task_atoms(task_id, log_dir)
    nodes: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    for i, a in enumerate(atoms):
        node: dict[str, Any] = {
            "id": a["atom"],
            "element": a["element"],
            "color": element_color(a["element"]),
            "exit_code": a["exit_code"],
            "state": a["state"],
            "duration_ms": a["duration_ms"],
            "n_events": 0,
            "kinds": {},
            "question": "",
            "preview": [],
        }
        if a["events_path"]:
            try:
                evs = read_events(a["events_path"])
            except (OSError, FileNotFoundError):
                evs = []
            node["n_events"] = len(evs)
            kinds: Counter = Counter(e.get("kind", "?") for e in evs)
            node["kinds"] = dict(kinds)
            for e in evs:
                if e.get("kind") == "question":
                    argv = (e.get("payload") or {}).get("argv") or []
                    node["question"] = str(argv[-1])[:160] if argv else ""
                    break
            node["preview"] = _atom_preview(evs)
        node["running"] = bool(a.get("running"))
        node["bond"] = a.get("bond", "")
        if i > 0:
            # 显式键优先（run --bond 指定输入来源），否则时序链（前一个原子）
            bond_id = a.get("bond", "")
            bond_idx = next((j for j, n in enumerate(nodes) if n["id"] == bond_id), None) if bond_id else None
            src = bond_idx if bond_idx is not None else i - 1
            # 键的粗细 = 数据流量，以源原子的 sidecar 体量为代理（事件总量≈输出活动量）
            weight = 0
            src_events = atoms[src]["events_path"]
            if src_events:
                try:
                    weight = Path(src_events).stat().st_size
                except OSError:
                    weight = 0
            link = {"source": src, "target": i, "weight": weight}
            if bond_idx is not None:
                link["explicit"] = True
            links.append(link)
        nodes.append(node)
    return {
        "task": task_id,
        "formula": task_formula(atoms),
        "generated_at": _dt.now().isoformat(timespec="seconds"),
        "nodes": nodes,
        "links": links,
    }


_GALAXY_LOCK = threading.Lock()
_GALAXY_INDEX: dict[str, dict[str, Any]] = {}   # str(path) -> {mtime, size, meta}
_GALAXY_CACHE: dict[str, dict[str, Any]] = {}   # str(root) -> {at, data}


def galaxy(
    log_dir: str | Path | None = None,
    ttl: float = 2.0,
) -> dict[str, Any]:
    """星系扫描：全部日志按化合物分组 + 未绑定任务的游离原子（serve 首页数据源）。

    v1.7 性能：mtime 增量索引——只有新出现或 mtime/size 变化的日志才重读头尾，
    未变化的直接复用缓存（serve 5s 轮询下，IO 从 O(全部日志) 降到 O(变化量)）；
    外层再叠 TTL 短缓存吸收高频轮询。线程安全（ThreadingHTTPServer 并发）。
    """
    from datetime import datetime as _dt

    root = Path(log_dir) if log_dir else None
    if root is None:
        from . import DEFAULT_LOG_DIR

        root = DEFAULT_LOG_DIR
    now = _dt.now().isoformat(timespec="seconds")
    if not root.is_dir():
        return {"tasks": [], "loose": [], "generated_at": now}
    key = str(root)
    with _GALAXY_LOCK:
        cached = _GALAXY_CACHE.get(key)
        if cached and (_dt.now() - cached["at"]).total_seconds() < ttl:
            return cached["data"]
    tasks: dict[str, dict[str, Any]] = {}
    loose: list[dict[str, Any]] = []
    seen: set[str] = set()
    for log_path in sorted(root.glob("*.log"), reverse=True):
        seen.add(str(log_path))
        try:
            st = log_path.stat()
        except OSError:
            continue
        with _GALAXY_LOCK:
            entry = _GALAXY_INDEX.get(str(log_path))
        if entry and entry["mtime"] == st.st_mtime and entry["size"] == st.st_size:
            meta = entry["meta"]
        else:
            try:
                with open(log_path, encoding="utf-8", errors="replace") as f:
                    head = f.read(4096)
                    f.seek(0, 2)
                    size = f.tell()
                    f.seek(max(0, size - 2048))
                    tail = f.read()
            except OSError:
                continue

            def _field(prefix: str, section: str) -> str:
                line = next((l for l in section.splitlines() if l.startswith(prefix)), "")
                return line[len(prefix):].strip() if line else ""

            _parts = log_path.stem.split("-")  # <日期>-<时间>-<命令首词>-<8hex>
            meta = {
                "element": _field("# element: ", head) or (_parts[2].removesuffix(".exe") if len(_parts) > 3 else "unknown"),
                "task": _field("# task: ", head),
                "exit": _field("# exit_code: ", tail),
                "running": log_path.with_suffix(".stdout.part").is_file(),
            }
            with _GALAXY_LOCK:
                _GALAXY_INDEX[str(log_path)] = {"mtime": st.st_mtime, "size": st.st_size, "meta": meta}
        atom = {
            "atom": log_path.stem,
            "element": meta["element"],
            "color": element_color(meta["element"]),
            "exit_code": meta["exit"] or None,
            "running": meta["running"],
            "task": meta["task"],
        }
        if atom["task"]:
            bucket = tasks.setdefault(atom["task"], {"task": atom["task"], "n": 0, "running": 0, "elements": Counter(), "last": atom["atom"]})
            bucket["n"] += 1
            bucket["running"] += 1 if meta["running"] else 0
            bucket["elements"][meta["element"]] += 1
        else:
            if len(loose) < 30:
                loose.append(atom)
    with _GALAXY_LOCK:
        for gone in list(_GALAXY_INDEX):
            if gone not in seen:
                _GALAXY_INDEX.pop(gone)  # 日志被清理/轮转：索引同步收缩
        data = {
            "tasks": [
                {**{k: v for k, v in t.items() if k != "elements"},
                 "formula": " + ".join(f"{k}×{v}" for k, v in t["elements"].most_common())}
                for t in tasks.values()
            ],
            "loose": loose,
            "generated_at": now,
        }
        _GALAXY_CACHE[key] = {"at": _dt.now(), "data": data}
    return data


def render_html(graph: dict[str, Any]) -> str:
    """分子数据 → 自包含 HTML 球棍模型（零依赖 canvas 3D，暗色宇宙风）。

    数据内嵌于页面；渲染器零外部请求，双击即开、离线可用、数据不出本机。
    """
    payload = json.dumps(graph, ensure_ascii=False).replace("</", "<\\/")
    return _HTML_TEMPLATE.replace("__GRAPH__", payload)


_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>cli-gateway · 分子球棍模型</title>
<style>
  html,body{margin:0;height:100%;background:#04060c;overflow:hidden;font-family:"Segoe UI",system-ui,sans-serif;color:#cbd5e1}
  #sky{position:fixed;inset:0}
  #hud{position:fixed;top:14px;left:18px;z-index:5;pointer-events:none}
  #hud h1{margin:0;font-size:17px;font-weight:600;color:#e2e8f0;letter-spacing:.5px}
  #hud .formula{margin-top:6px;font-size:13px}
  #hud .meta{margin-top:4px;font-size:11px;color:#64748b}
  .chip{display:inline-block;padding:1px 9px;margin-right:6px;border-radius:999px;border:1px solid;color:#e2e8f0;font-size:12px}
  #legend{position:fixed;bottom:14px;left:18px;z-index:5;font-size:11px;color:#64748b}
  #legend b{display:inline-block;width:8px;height:8px;border-radius:50%;margin:0 4px 0 10px}
  #hint{position:fixed;bottom:14px;right:18px;z-index:5;font-size:11px;color:#475569}
  #panel{position:fixed;top:0;right:-380px;width:360px;height:100%;background:rgba(8,12,22,.92);backdrop-filter:blur(6px);
         border-left:1px solid #1e293b;transition:right .25s;z-index:6;padding:18px;box-sizing:border-box;overflow-y:auto}
  #panel.open{right:0}
  #panel h3{margin:0 0 4px;color:#e2e8f0;font-size:15px}
  #panel .sub{font-size:11px;color:#64748b;margin-bottom:10px}
  .ev{margin:7px 0;font-size:12px;line-height:1.5;border-left:2px solid #334155;padding-left:8px}
  .ev b{font-size:10px;letter-spacing:.5px;padding:0 5px;border-radius:3px;margin-right:6px}
  #close{position:absolute;top:10px;right:12px;cursor:pointer;color:#64748b;font-size:18px}
</style>
</head>
<body>
<canvas id="sky"></canvas>
<canvas id="scene"></canvas>
<div id="hud"><h1 id="title"></h1><div class="formula" id="formula"></div><div class="meta" id="meta"></div></div>
<div id="legend"></div>
<div id="hint">拖拽旋转 · 滚轮缩放 · 点击原子下钻事件流</div>
<div id="panel"><span id="close">✕</span><h3 id="p-title"></h3><div class="sub" id="p-sub"></div><div id="p-evs"></div></div>
<script>
const G = __GRAPH__;
const KIND_COLOR = {question:"#94a3b8",text:"#38bdf8",tool_call:"#f59e0b",tool_result:"#a3e635",
  reasoning:"#c084fc",result:"#34d399",error:"#f87171",answer:"#e2e8f0",exit:"#64748b",note:"#475569"};

// 星野
const sky = document.getElementById("sky"), sctx = sky.getContext("2d");
function paintSky(){ sky.width=innerWidth; sky.height=innerHeight; sctx.fillStyle="#04060c";
  sctx.fillRect(0,0,sky.width,sky.height);
  for(let i=0;i<240;i++){ const x=Math.random()*sky.width, y=Math.random()*sky.height,
    r=Math.random()*1.1+.2, a=Math.random()*.5+.1;
    sctx.fillStyle="rgba(148,163,184,"+a+")"; sctx.beginPath(); sctx.arc(x,y,r,0,7); sctx.fill(); } }
paintSky();

// 球面斐波那契螺旋布点
function spiral(n){ const pts=[], ga=Math.PI*(3-Math.sqrt(5));
  for(let i=0;i<n;i++){ const y=1-2*(i+.5)/Math.max(n,1), r=Math.sqrt(Math.max(0,1-y*y)), th=ga*i;
    pts.push([Math.cos(th)*r, y, Math.sin(th)*r]); } return pts; }

const nodes = G.nodes.map((n,i)=>({...n, base:[0,0,0]}));
const pts = spiral(nodes.length);
nodes.forEach((n,i)=>{ n.base=pts[i]; });
const R = Math.min(innerWidth,innerHeight)*0.33;
let rotY=0.6, tiltX=-0.25, zoom=1.0, spin=0.0025, dragging=false, lx=0, ly=0, selected=-1;

const cv = document.getElementById("scene"), ctx = cv.getContext("2d");
function resize(){ cv.width=innerWidth; cv.height=innerHeight; } resize();
addEventListener("resize", ()=>{ paintSky(); resize(); });

function project(p){ // 绕 Y 旋转 → 绕 X 倾斜 → 透视
  const [x,y,z]=p, cy=Math.cos(rotY), sy=Math.sin(rotY);
  let x1=x*cy+z*sy, z1=-x*sy+z*cy;
  const cx=Math.cos(tiltX), sx=Math.sin(tiltX);
  let y1=y*cx - z1*sx, z2=y*sx + z1*cx;
  const d=3.2, f=1/(1+z2/d);
  return {x:innerWidth/2 + x1*R*zoom*f, y:innerHeight/2 + y1*R*zoom*f, z:z2, f};
}
function ring(ctx2,x,y,r,color,dashed){ ctx2.strokeStyle=color; ctx2.lineWidth=1.4;
  if(dashed) ctx2.setLineDash([4,4]); ctx2.beginPath(); ctx2.arc(x,y,r,0,7); ctx2.stroke(); ctx2.setLineDash([]); }

function draw(){
  ctx.clearRect(0,0,cv.width,cv.height);
  if(!dragging) rotY+=spin;
  const proj = nodes.map(n=>({...project(n.base), n}));
  // 键（时序链，粗细=数据流量）
  for(const l of G.links){ const a=proj[l.source], b=proj[l.target]; if(!a||!b) continue;
    ctx.strokeStyle="rgba(100,116,139,"+(0.12+Math.min(0.45,(l.weight||200)/8000))+")";
    ctx.lineWidth=0.8+Math.min(3.2,(l.weight||200)/2500);
    ctx.beginPath(); ctx.moveTo(a.x,a.y); ctx.lineTo(b.x,b.y); ctx.stroke(); }
  // 球（远→近）
  proj.sort((a,b)=>b.z-a.z).forEach(p=>{ const n=p.n, r=(8+Math.min(26,(n.duration_ms||1000)/4000))*p.f;
    p.sr=r;
    const g=ctx.createRadialGradient(p.x,p.y,0,p.x,p.y,r*2.6);
    g.addColorStop(0,n.color+"cc"); g.addColorStop(.45,n.color+"44"); g.addColorStop(1,n.color+"00");
    ctx.fillStyle=g; ctx.beginPath(); ctx.arc(p.x,p.y,r*2.6,0,7); ctx.fill();
    ctx.fillStyle=n.color; ctx.beginPath(); ctx.arc(p.x,p.y,r*.62,0,7); ctx.fill();
    if(n.state==="timed_out") ring(ctx,p.x,p.y,r,dashed="#f59e0b",true);
    else if((n.exit_code||0)!==0 && n.state!=="not_found") ring(ctx,p.x,p.y,r,"#f87171",false);
    else if(n.state==="completed") ring(ctx,p.x,p.y,r*1.15,"rgba(226,232,240,.35)",false);
    ctx.fillStyle="rgba(226,232,240,.85)"; ctx.font=(10*p.f).toFixed(1)+"px system-ui"; ctx.textAlign="center";
    ctx.fillText(n.element, p.x, p.y - r*1.5);
    if(p.f>1.02){ ctx.fillStyle="rgba(148,163,184,.7)"; ctx.font=(9*p.f).toFixed(1)+"px system-ui";
      ctx.fillText((n.question||"").slice(0,28), p.x, p.y + r*1.9); }
  });
  cv._proj = proj;
  requestAnimationFrame(draw);
}

// HUD 与图例
document.getElementById("title").textContent = "task · " + G.task;
document.getElementById("formula").innerHTML = G.formula.split(" + ").map(s=>{
  const [el,c]=s.split("×"); const color=(G.nodes.find(n=>n.element===el)||{}).color||"#94a3b8";
  return '<span class="chip" style="border-color:'+color+'55;background:'+color+'14">'+el+' ×'+c+'</span>'; }).join("");
document.getElementById("meta").textContent = G.nodes.length+" atoms · generated "+G.generated_at;
const els=[...new Set(G.nodes.map(n=>n.element))];
document.getElementById("legend").innerHTML = "元素色卡："+els.map(e=>{
  const c=(G.nodes.find(n=>n.element===e)||{}).color||"#94a3b8";
  return "<b style='background:"+c+"'></b>"+e; }).join("");

// 交互
cv.addEventListener("mousedown",e=>{dragging=true;lx=e.clientX;ly=e.clientY});
addEventListener("mouseup",()=>dragging=false);
addEventListener("mousemove",e=>{ if(!dragging) return;
  rotY+=(e.clientX-lx)*0.006; tiltX=Math.max(-1.2,Math.min(1.2,tiltX+(e.clientY-ly)*0.004));
  lx=e.clientX; ly=e.clientY; });
cv.addEventListener("wheel",e=>{ zoom=Math.max(.5,Math.min(2.6,zoom*(e.deltaY<0?1.08:.93))); e.preventDefault(); },{passive:false});
cv.addEventListener("click",e=>{ const proj=cv._proj||[]; let best=-1,bd=1e9;
  proj.forEach((p,i)=>{ const d=Math.hypot(e.clientX-p.x,e.clientY-p.y);
    if(d<Math.max(14,p.sr||12)+8 && d<bd){bd=d;best=i;} });
  if(best>=0){ selected=best; openPanel(nodes[best]); } });
function openPanel(n){
  document.getElementById("panel").classList.add("open");
  document.getElementById("p-title").textContent = n.element+" · "+n.id;
  document.getElementById("p-sub").textContent =
    "exit="+n.exit_code+"  state="+n.state+"  "+n.duration_ms+"ms  events="+n.n_events;
  document.getElementById("p-evs").innerHTML = (n.preview||[]).map(ev=>{
    const c=KIND_COLOR[ev.kind]||"#475569";
    return '<div class="ev" style="border-color:'+c+'66"><b style="color:'+c+';border:1px solid '+c+'55">'+ev.kind+
           '</b>'+ (ev.summary||"").replace(/&/g,"&amp;").replace(/</g,"&lt;") +'</div>'; }).join("")
    || '<div class="ev">（无事件预览——sidecar 缺失或未观测）</div>';
}
document.getElementById("close").onclick=()=>document.getElementById("panel").classList.remove("open");
draw();
</script>
</body>
</html>
"""


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
        bond = _field("# bond: ", head)
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
                # 显式化学键（run --bond <atom-id>）：本原子的输入来自哪个原子
                "bond": bond or "",
                # 运行中检测：part 文件尚在 = 进程仍在落盘（组装时删除）
                "running": (log_path.with_suffix(".stdout.part")).is_file(),
            }
        )
    return atoms
