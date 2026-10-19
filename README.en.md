# cli-gateway

[English](README.en.md) | [简体中文](README.md)

> **Unified CLI process invocation gateway** — make every "AI/agent calls a CLI" observable, auditable, and replayable. Full output captured to disk, byte-exact replay, unified exit codes, cross-CLI timeline — with an observation mode that turns task history into typed event streams.

**Install**: `pip install ai-cli-gateway` · PyPI: [ai-cli-gateway](https://pypi.org/project/ai-cli-gateway/) · Docs: [README.md](README.md) (Chinese, full)

## Why

Agent workloads shell out to CLIs constantly — but "did it actually run, what did it print, why did it hang" is usually unanswerable. cli-gateway wraps any CLI invocation:

- **Full stdout/stderr captured to disk** — byte-exact, memory-bounded streaming
- **Verbatim replay** of any past invocation (command, pid, exit code, duration, env overrides)
- **Unified exit code semantics**: 127 not-found / 126 spawn-failure / 124 timeout (process-tree kill, partial output preserved)
- **Doctor**: probe which of 9 known coding CLIs are installed, and how to invoke them (native exe vs npm shim)
- **Observation mode** (experimental): typed event streams (`tool_call` / `text` / `result`…) from claude & codex, tasks bound as molecules with a ball-and-stick visualization

## Quick start

```bash
# Run anything, capture everything
cli-gateway run --timeout 30 -- node -e "console.log('hello')"

# Audit: list & replay verbatim
cli-gateway logs
cli-gateway replay <log-path>

# Probe your coding CLIs (claude / codex / goose / qoder / ...)
cli-gateway doctor

# Observation mode: bind calls into a task, render the dialog tree
cli-gateway run --observe --task-id fix-1 -- codex exec -s workspace-write "fix it"
cli-gateway task fix-1 --tree
cli-gateway task fix-1 --html > galaxy.html   # self-contained ball-and-stick view
```

Python API: `from cli_gateway import run, replay, read_events, task_atoms`

## Windows notes

Windows is a first-class platform here: npm-shim CLIs (claude/kilo/mcode/opencode) need a `cmd /c` wrapper because `CreateProcess` only resolves `.exe`; Git Bash mangles `/c` into `C:\` (write `//c`); encoding is forced to UTF-8 via `PYTHONUTF8=1`. The full, battle-tested invocation matrix for 9 coding CLIs lives in the [Chinese README](README.md#windows-须知npm-shim-与直调).

## More

- Full docs (Chinese): [README.md](README.md)
- Agent operator prompts for 9 coding CLIs: [agents/](agents/README.md)
- Changelog: [CHANGELOG.md](CHANGELOG.md)

## License

MIT
