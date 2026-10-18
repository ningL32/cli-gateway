# Changelog

格式参考 [Keep a Changelog](https://keepachangelog.com/)，版本遵循语义化版本。

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
