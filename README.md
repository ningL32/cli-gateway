# cli-gateway

统一 CLI 进程调用网关：让"AI 调 CLI 是否真跑、输出能否逐字核对"变得可观测、可审计、可回放。

**v1 定位**：本地 subprocess 薄包装（~150 行）。不涉及任何 URL/网络请求。

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
cli-gateway run -- echo hello
cli-gateway replay ~/.cli-gateway/logs/20261018-190000-echo-abc12345.log
cli-gateway logs
```

## 返回结构（GatewayResult）

| 字段 | 说明 |
|---|---|
| `command` | 原始命令 |
| `exit_code` | 退出码（超时=-1，找不到命令=-1） |
| `duration_ms` | 耗时毫秒 |
| `log_path` | 全量日志文件路径 |
| `stdout_tail` / `stderr_tail` | 输出尾部（默认各 2000 字符） |
| `timestamp` / `pid` / `success` | 时间戳 / 进程号 / 是否成功 |

## 日志格式

每次调用写一个文件 `<ts>-<cmd>-<id>.log`，内容为元数据头 + STDOUT 段 + STDERR 段，可逐字回放、可审计。

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
