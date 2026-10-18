# cli-gateway

统一 CLI 进程调用网关：让"AI 调 CLI 是否真跑、输出能否逐字核对"变得可观测、可审计、可回放。

**v1 定位**：本地 subprocess 薄包装。不涉及任何 URL/网络请求。

## 为什么

团队有 9 个 CLI（claude / codex / kilo / mcode / goose / opencode / qoder / crush / hermes）作为 agent 运行时。痛点：
- 调用了但无法直接观测原始 stdout/stderr，只能靠进程日志+时间戳佐证"跑了"；
- 无法逐字核对输出、无法回放、无法统一审计。

`cli-gateway` 把任何 CLI 调用包装成"全量日志落盘 + 可回放 + 结构化返回"，让每次调用可观测、可审计、可验证。

## 快速开始

```bash
pip install -e .            # 或直接 PYTHONPATH=.

# 作为库
from cli_gateway import run, replay
res = run(["claude", "--version"])
print(res.exit_code, res.duration_ms, res.log_path)
print(replay(res.log_path))   # 逐字回放

# 作为 CLI
# 注意：--timeout/--dir 必须写在 -- 之前；-- 之后的参数全部属于被调命令
cli-gateway run --timeout 10 -- echo hello
cli-gateway replay ~/.cli-gateway/logs/20261018-190000-echo-abc12345.log
cli-gateway logs
```

## 返回结构（GatewayResult）

| 字段 | 说明 |
|---|---|
| `command` | 原始命令 |
| `exit_code` | 退出码：命令未找到=**127**，启动失败（不可执行等）=**126**，超时=**124**，其余为命令真实退出码（POSIX 被信号杀死时为负信号值） |
| `duration_ms` | 耗时毫秒 |
| `log_path` | 全量日志文件路径 |
| `stdout_tail` / `stderr_tail` | 输出尾部（默认各 2000 字符，从日志文件取尾，内存有界） |
| `timestamp` / `pid` / `success` | 时间戳 / 真实进程号 / 是否成功 |
| `state` | 枚举：`completed` / `timed_out` / `not_found` / `spawn_error` / `killed`（spawn_error 是对 shell 惯例枚举的扩展） |
| `cwd` | 本次调用的 working directory（未传为 None） |
| `truncated` | 输出超过 tail 长度被截断时为 True |
| `timed_out` | 超时标志（等价 `state == "timed_out"`） |
| `env_overrides` | 显式传入的 env 覆盖项（含 KEY/TOKEN/SECRET/PASSWORD 的值脱敏为 `***`，不记录全量环境） |

## 日志格式

每次调用写一个文件 `<ts>-<cmd>-<id>.log`：元数据头（command/timestamp/pid/cwd/env_overrides/timeout）+ STDOUT 段 + STDERR 段 + END + 结果尾（exit_code/duration_ms/state），可逐字回放、可审计。超时路径保留已产生的部分输出（partial）。

## 注册表（run_cli，白名单路径）

```python
from cli_gateway import REGISTRY, CliSpec, run_cli

REGISTRY["claude"] = CliSpec(default_timeout=600, tail=2000)  # 零依赖顶层 dict
res = run_cli("claude", "--version")        # 未注册名字 → KeyError；run() 不查注册表
```

`CliSpec` 字段：`default_timeout` / `capture` / `tail` / `cwd` / `env` / `retry_on`。重试只在显式传 `retry_count` 时发生，且只重试"启动前失败"（`not_found` / `spawn_error`；超时与非零退出绝不自动重试），每次尝试独立日志文件。`retry_on` 字段当前仅支持 `"spawn"`，其他值保留但不生效。

## Windows 须知：npm shim 与直调

Windows 上 `Popen` 直控走 `CreateProcess`，只解析 `.exe`——npm 全局安装的 CLI（claude / kilo / mcode / opencode 等）目录里只有 `xxx.cmd` shim，直接传名字会得到 `127/not_found`。两种调用方式：

- **原生 exe 直调**：codex / goose / qoderclicn / hermes；crush 的 npm shim 没有 `.exe` 同名文件，`CreateProcess` 会继续搜 PATH 命中 WinGet 的 `crush.exe`，同样直调可用；
- **npm shim 用 `cmd /c <name> ...` 包装**：gateway 的 `taskkill /T` 树杀仍覆盖 cmd 与其下的整棵进程树。

Git Bash 传参注意：MSYS 会把独立参数 `/c` 转换成 `C:\`，命令行路径须写 `cmd //c` 或设 `MSYS_NO_PATHCONV=1`；走 Python 库传 argv 列表不受影响。

## 真机验证矩阵（2026-10-18，Windows Server 2022）

9 个 CLI 全部经网关真实验证：`--version` 探测 + 真实模型调用（提示词"只回复两个字：收到"）各一次。

| CLI | 版本 | 直调方式 | 非交互用法 | 真实调用 |
|---|---|---|---|---|
| claude | 2.1.177 | `cmd /c claude` | `claude -p "<prompt>"` | ✅ |
| codex | 0.162.1 | 直调 exe | `codex exec "<prompt>"` | ✅ |
| kilo | 7.7.5 | `cmd /c kilo` | `kilo run "<prompt>"` | ✅ |
| mcode | 0.4.12 | `cmd /c mcode` | `mcode exec "<prompt>"` | ✅ |
| goose | 1.51.0 | 直调 exe | `goose run -t "<prompt>"` | ✅ |
| opencode | 1.18.31 | `cmd /c opencode` | `opencode run "<prompt>"` | ✅ |
| qoder | 1.1.72 | 直调 exe（命令名 `qoderclicn`） | `qoderclicn -p "<prompt>"` | ✅ |
| crush | v0.88.0 | 直调 exe（自动命中 WinGet 版） | `crush run "<prompt>"` | ✅ |
| hermes | v0.21.3 | 直调 exe | `hermes -z "<prompt>"` | ✅ |

## 超时与进程组

超时（`timeout` 秒，默认无限等）会按进程组/进程树终止：POSIX 用 `start_new_session` + `os.killpg`（先 SIGTERM，宽限 2s 后 SIGKILL）；Windows 用 `CREATE_NEW_PROCESS_GROUP` + `taskkill /F /T`。这会大幅降低孤儿孙进程残留的概率，但不是绝对保证——极端场景（第三方进程自行脱离进程组/进程树、或孙进程继承管道写句柄导致输出流不关闭）可能有残留或输出不完整，此时结果会置 `truncated=True` 并在日志尾部追加 `# warning:` 行，由调用方按需兜底。

POSIX 下进程被信号杀死时 `state="killed"`、`exit_code` 为负信号值；Windows 正常退出（含非零退出码）均为 `completed`。

输出读取统一按 UTF-8（`errors="replace"`），日志始终是合法 UTF-8。gateway 默认向子进程注入 `PYTHONUTF8=1`（调用方或父环境已显式设置该变量时不覆盖），使 python 类子进程在 Windows 传统代码页（CP936 等）环境下也输出 UTF-8——注意子进程会看到这个变量。

## 设计边界（v1）

- **只做本地 subprocess**，不发起 URL/网络请求——未来 v2 若加远程/HTTP 能力，服务端请求 URL 时仅允许 http/https，且发请求前校验 host、拒绝 localhost/环回/私有/保留地址；
- 不做沙箱（用 Docker 的 MCP 网关管沙箱，见下）；不做 Web UI；不做多租户。

## 与 docker/mcp-gateway 的关系（不同层）

| | docker/mcp-gateway | cli-gateway |
|---|---|---|
| 管 | MCP 服务器（外部工具数据源，Docker 容器） | CLI 进程调用（本地 subprocess） |
| 解决 | AI 统一访问 MCP 服务器 + 认证/密钥 | CLI 调用日志落盘/回放/结构化返回 |
| 层 | 协议层（MCP） | 进程层（subprocess） |

两者互补不冲突：MCP 网关管"AI 连外部工具"，cli-gateway 管"AI 调本地 CLI"。

## License

MIT

## 安全约束（Mimosa）

本仓库 v1 不发起任何网络请求。若未来扩展远程调用，必须遵守：仅允许 http/https；发请求前校验 host，拒绝 localhost、环回（127.0.0.0/8, ::1）、私有（RFC1918）与保留地址。
