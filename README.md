# cli-gateway

[English](README.en.md) | 简体中文

统一 CLI 进程调用网关：让"AI 调 CLI 是否真跑、输出能否逐字核对"变得可观测、可审计、可回放。

**v1 定位**：本地 subprocess 薄包装。不涉及任何 URL/网络请求。

## 为什么

团队有 9 个 CLI（claude / codex / kilo / mcode / goose / opencode / qoder / crush / hermes）作为 agent 运行时。痛点：
- 调用了但无法直接观测原始 stdout/stderr，只能靠进程日志+时间戳佐证"跑了"；
- 无法逐字核对输出、无法回放、无法统一审计。

`cli-gateway` 把任何 CLI 调用包装成"全量日志落盘 + 可回放 + 结构化返回"，让每次调用可观测、可审计、可验证。

## 快速开始

```bash
pip install ai-cli-gateway   # PyPI 安装（PyPI 上 `cli-gateway` 名字已被占，故用此名）
pip install -e .             # 或本仓开发安装（或直接 PYTHONPATH=.）

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
cli-gateway doctor
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

矩阵可在你自己的机器上实时复现：

```bash
cli-gateway doctor                # 体检内置 9 个编码 CLI：形态/版本/日志路径
cli-gateway doctor --json         # 结构化输出（agent 消费友好）
cli-gateway doctor --no-probe     # 静态快查：只看安装形态，不实跑版本探测
cli-gateway doctor --only codex   # 只查指定命令
```

形态判定以网关真实调用结果为准（先直调，Windows 下 not_found 自动再试 `cmd /c` 包装），不是静态猜测；全部可用退出码 0，否则 1——可直接接进 CI 或 agent 的启动自检。

## 观测模式：原子 / 元素 / 化合物

任务过程的物理化学模型（`--observe` + `--task-id`）：

- **原子**：一次网关调用。问（argv/prompt）与答（输出）成对出现、天然不可分割；`exit_code` + `duration_ms` 是"结合完成"的证据。**守恒律**：原始日志是唯一真相，事件 sidecar（`<log>.events.jsonl`）是派生解读——解析失败降级为全文 answer 事件，绝不丢字。
- **元素**：CLI 的种类。周期表（`ELEMENTS`）里的元素带一条**观测配方**——激发它以"离散谱线"（结构化 JSON 事件流）而非"连续谱"（纯文本）发光。同位素 = 版本/格式差异。
- **化合物**：任务 = 化学键（上一个原子的输出成为下一个原子的输入）连接的原子图；**分子式** = 组成配比。

```bash
cli-gateway run --observe --task-id fix-login -- cmd //c claude -p "分析这个 diff"
cli-gateway run --observe --task-id fix-login -- codex exec "修复它"
cli-gateway task fix-login            # 原子清单 + 分子式（--json 机器可读）
# task fix-login: 2 atoms | formula: claude×1 + codex×1
```

```python
from cli_gateway import run, read_events, task_atoms, task_formula
run(["claude", "-p", "分析"], observe=True, task_id="fix-login")
events = read_events(log_path + ".events.jsonl")   # question → [note…] → answer → exit
atoms = task_atoms("fix-login")                    # 化合物检索
task_formula(atoms)                                # 'claude×1 + codex×1'
```

**谱线现状**：claude（`--output-format stream-json --verbose`）与 codex（`exec --json`）为 jsonl 离散谱，且已**类型化**——事件 kind 为 `system` / `text` / `tool_call` / `tool_result` / `reasoning` / `result` / `error`（assistant 消息里的 tool_use 块 → tool_call 事件，对话树可重建）。claude 的 schema 来自真机实测；codex 工具类 item 的字段名为推测实现（宽容兜底，待真任务样本回填）。未收录形态退化为 `note` 兜底，**payload.raw 永远保留**；原始字节始终以 raw 日志为准。

**边界（v1.7 内存有界）**：sidecar 流式写入——逐行解析谱线，全程只持一行；answer 事件不再内嵌全文，改为 `text_ref` 外置引用（指回 raw 日志）+ 500 字符 preview，需要全文时用 `answer_text()` 按需显式取。spawn 失败的调用不写 sidecar（raw 日志仍有完整记录）。

### 对话树与分子可视化（实验性）

```bash
cli-gateway task fix-login --tree          # 对话树：每个原子的类型化事件缩进渲染（--all 含 system）
cli-gateway task fix-login --graph > molecule.json   # 分子数据 JSON（nodes/links/formula）
cli-gateway task fix-login --html  > galaxy.html     # 自包含 HTML 球棍模型，双击即开
cli-gateway serve                          # 本机实时分子宇宙（127.0.0.1:7817，5s 轮询）
```

`--html` 单文件离线可用：暗色星野、元素色卡辉光球（半径 ∝ 耗时、timed_out 虚线裂解环）、元素色渐变化学键（粗细 ∝ 数据流量）+ 方向箭头 + 流量徽章、点击原子下钻事件流。`serve` 为本机 127.0.0.1 只读视图（星系/分子两态、运行中任务绿色脉动），非多用户 Web 产品。以上均为**实验性**，UI 与 schema 可能调整。

## 子智能体编队（agents/）

9 个编码 CLI 的 ZCode 子代理操作员提示词（首席架构/攻坚/全栈/TDD/QA/评审/研究员），内置经网关留证的调用配方与完整方法论——详见 [agents/README.md](agents/README.md)。

## 超时与进程组

超时（`timeout` 秒，默认无限等）会按进程组/进程树终止：POSIX 用 `start_new_session` + `os.killpg`（先 SIGTERM，宽限 2s 后 SIGKILL）；Windows 用 `CREATE_NEW_PROCESS_GROUP` + `taskkill /F /T`。这会大幅降低孤儿孙进程残留的概率，但不是绝对保证——极端场景（第三方进程自行脱离进程组/进程树、或孙进程继承管道写句柄导致输出流不关闭）可能有残留或输出不完整，此时结果会置 `truncated=True` 并在日志尾部追加 `# warning:` 行，由调用方按需兜底。

POSIX 下进程被信号杀死时 `state="killed"`、`exit_code` 为负信号值；Windows 正常退出（含非零退出码）均为 `completed`。

输出读取统一按 UTF-8（`errors="replace"`），日志始终是合法 UTF-8。gateway 默认向子进程注入 `PYTHONUTF8=1`（调用方或父环境已显式设置该变量时不覆盖），使 python 类子进程在 Windows 传统代码页（CP936 等）环境下也输出 UTF-8——注意子进程会看到这个变量。

## 设计边界

- **只做本地 subprocess**，不发起任何网络请求——未来 v2 若加远程/HTTP 能力，服务端请求 URL 时仅允许 http/https，且发请求前校验 host、拒绝 localhost/环回/私有/保留地址；
- 不做沙箱（用 Docker 的 MCP 网关管沙箱，见下）；不做多用户 Web 产品（`serve` 例外：仅绑定 127.0.0.1 的本机只读日志视图，零外部请求）。

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
