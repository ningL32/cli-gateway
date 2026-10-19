# Changelog

格式参考 [Keep a Changelog](https://keepachangelog.com/)，版本遵循语义化版本。

## [1.4.0] - 2026-10-19

### Added — task --tree：对话树渲染

- `cli-gateway task <id> --tree`：把任务内每个原子的类型化事件渲染成缩进树（原子头 element/exit/duration + 事件流）；`--all` 显示默认隐藏的 system 心跳
- `render_atom_tree()`（events.py）：类型感知摘要（question 取 argv 末元素、text/reasoning 前 60 字符、tool_call 取 name+input、tool_result 前 40 字符、result 取结论或 usage；中文按字符截断）
- CLI `run` 新增 `--stdin-devnull`（库参数此前已存在，补齐 CLI 面）
- **编队实战回填**：本版本由 codex 在观测模式下实现（`--observe --task-id drill-tree`），演练收获 60 个真实 `command_execution → tool_call` 事件，**坐实**了 v1.3.0 的推测字段（name="shell"、input=完整命令）；claude 评审岗同观测链路参与

### Fixed

- codex 元素观测配方与 0.162 实测对齐：`exec` 子命令无 `--full-auto`，无人值守用 `-s workspace-write`（agents/codex.md 配方同步修正）

## [1.3.0] - 2026-10-19

### Added — 类型化谱线（对话树重建的第一步）

- 谱线事件从 note 透传升级为**语义类型**：`system` / `text` / `tool_call` / `tool_result` / `reasoning` / `result` / `error`（note 保留为未知形态兜底）
- 周期表条目新增 `parser` 字段：注册配方 = 注册解读器，配方与解读统一在 `ELEMENTS` 一张表
- claude 解析器（schema 真机实测）：system( subtype)/assistant.content 块（text→text、tool_use→tool_call、tool_result→tool_result）/result
- codex 解析器（thread/turn/item 模型，实测样本归纳）：thread.started、turn.started/completed、item.completed（agent_message→text、error→error；command_execution/file_change/mcp_tool_call/web_search→tool_call 的字段名为【推测，待真任务样本回填】，取值全走 .get 宽容兜底）
- 守恒律不变：payload.raw 永远保留，未收录形态退化为 note 不丢字
- 真机复验：claude 观测调用事件分布 `{question:1, system:14, text:1, result:1, answer:1, exit:1}`——tool_use 块出现时即得 tool_call 事件，对话树可重建

### Changed

- 契约测试 54→58（claude/codex 类型化 fixture、兜底、集成）

## [1.2.0] - 2026-10-19

### Added — 观测模式：原子 / 元素 / 化合物

任务过程建模：原子 = 一次调用（问/答/exit 不可分）；元素 = CLI 种类（周期表 + 观测配方，激发结构化"离散谱线"）；化合物 = 任务（键连接的原子图，分子式 = 组成配比）。

- `run()` 新增 `observe` / `task_id`；CLI 新增 `--observe` / `--task-id` 与 `task` 子命令（含 `--json`）
- `events.py`：原子事件 schema（`question → [note…] → answer → exit`），JSONL sidecar `<log>.events.jsonl`；守恒律——raw 日志是唯一真相，解析失败降级为全文 answer
- 元素周期表 `ELEMENTS`：claude（`--output-format stream-json --verbose`）、codex（`--json`）观测配方；未知元素 text 连续谱照常执行
- `task_atoms()` / `task_formula()`：化合物检索与分子式；日志头部新增 `# task:` / `# element:` / `# observe:` 记录（仅观测模式写入）
- 真机验证：claude 观测调用产生 37 事件（1 question + 34 note 谱线 [system/assistant/result] + 1 answer + 1 exit）
- 库导出：`read_events` / `task_atoms` / `task_formula` / `ELEMENTS`

### Changed

- 契约测试 47→54（sidecar、配方表、化合物检索、CLI roundtrip）

## [1.1.1] - 2026-10-19

三岗异构评审回炉（claude 八维 / qoder 边界交叉复核 / codex 并发攻坚，发现全部实测取证）。

### Fixed

- **P1·finally 同步 close 与未决 ReadFile 互锁**：reader 阻塞时 finally 按存活分流至后台 close——此前 run() 真实返回时间被孙进程剩余寿命拖到无界（实测 45.3s），且 duration_ms 对此完全不可见
- **P1·timeout 非法值 spawn 前拒收**：负值在 Windows 经 DWORD 回绕等效无限等待（超时契约静默失效）、POSIX 却立即杀，两平台行为相反——现在 NaN/Inf/≤0 一律 ValueError，"不限时"显式传 None
- **P1·reader 装配段纳入 try/finally**：spawn 成功后线程耗尽等异常不再留孤儿进程 + 零审计穿出 run()
- **P1·part 打开失败显式标记**：磁盘满/杀软拦截丢弃输出时不再静默说谎（truncated=True + trailer warning）
- readers_hung 在 join 后复评：消除孙进程及时退场时的 truncated 误报；reader 意外死亡记入 trailer 告警
- join 改共享 deadline（单个 hung 不再吃满 30s 串行预算）；duration 定格于终态达成（杀树成本不计入）；taskkill 加 10s 超时
- POSIX 超时补刀：宽限期内直接子进程先死也无条件对进程组补 SIGKILL（TERM 免疫的孙进程不再永久存活占管道）
- argv 含换行 one-line 化（shlex.quote 不转义换行，可伪造日志分段/结果尾）；argv 元素类型 spawn 前校验（混入 bytes 在 POSIX 会真实执行后才炸、零审计）
- list_logs 对并发删除的 stat 竞态免疫；_assemble_log 统一走 _norm_log_path；env 脱敏词表补 PAT/AUTH
- CLI：退出码透传（124/127 脚本可直接判断）；库层参数错误单行报错不甩 traceback；doctor 探测默认 stdin=DEVNULL（防会读 stdin 的 CLI 挂满超时）
- run() 新增 `stdin_devnull` 参数（无人值守/探测场景）

### Changed

- 契约测试 40→47（timeout 校验、墙钟上界断言、日志头注入、argv 类型）

## [1.1.0] - 2026-10-18

### Added

- `cli-gateway doctor`：本机编码 CLI 环境体检——形态判定（原生 exe 直调 / npm shim 需 cmd /c 包装 / 未找到）、版本探测、`--json` 结构化输出、`--no-probe` 静态快查、`--only` 指定命令；内置 9 个编码 CLI 清单（`cli_gateway.doctor.DOCTOR_SPECS`）
- CI：GitHub Actions 三平台（Windows / Linux / macOS）× Python 3.11 / 3.13 测试矩阵
- Release workflow（手动触发）：构建 sdist/wheel + PyPI Trusted Publishing
- 版本单源化：`cli_gateway.__version__`，pyproject 动态引用
- 真机验证矩阵：9 个编码 CLI（claude/codex/kilo/mcode/goose/opencode/qoder/crush/hermes）穿网关全通过（版本探测 + 真实模型调用），详见 README
- `agents/`：9 个编码 CLI 的 ZCode 子代理操作员提示词（开源脱敏版）

### Changed（v1.1 核心，相对 0.1.0）

- Popen 直控子进程：进程组终止（Windows `taskkill /F /T` / POSIX `killpg`）+ 超时 partial 结果回收
- 退出码语义：127 未找到 / 126 启动失败 / 124 超时；`state` 枚举（completed/timed_out/not_found/spawn_error/killed）
- 流式 tee 内存有界：增量 UTF-8 解码器、日志 `newline=""` 免 CRLF 翻译失真
- env 脱敏（KEY/TOKEN/SECRET/PASS/CREDENTIAL → `***`）+ 默认注入 `PYTHONUTF8=1`（显式设置不覆盖）
- `CliSpec` 注册表 + `run_cli` 白名单入口（retry 仅"启动前失败"，`retry_on` 当前仅 `spawn`）
- 修复 `__main__` argparse dest 冲突（0.1.0 的 `run` 子命令从未工作）

## [0.1.0] - 2026-10-18

- 基础三命令：`run` / `replay` / `logs`——全量日志落盘、逐字回放、结构化 `GatewayResult`
