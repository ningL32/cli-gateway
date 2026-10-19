# 子智能体编队（agents/）

9 个编码 CLI 的 **ZCode 子代理操作员提示词**——每个 CLI 配一个"专属操作员"：主会话（工头）负责拆解派工与验收，子代理通过 Bash 调用对应 CLI 真进程干活。方法论（C4/ADR/TDD/变异测试/八维审查等）全部内置在提示词里，开箱即用。

| 文件 | 角色 | 擅长 |
|---|---|---|
| [claude.md](claude.md) | 首席架构 | C4/ADR 架构规格、多维评审、技术决策 |
| [codex.md](codex.md) | 资深攻坚 | 五 Why 根因、性能剖析、Strangler Fig 重构 |
| [opencode.md](opencode.md) | 全栈 | React/Next + Node/Python + DB 前后端通吃 |
| [mcode.md](mcode.md) | 功能实现 | TDD 红绿重构一棒到底 |
| [kilo.md](kilo.md) | 功能实现 | 补位实现、修测试、环境坑 |
| [crush.md](crush.md) | 功能实现 | 快速编码、LSP 语义辅助 |
| [goose.md](goose.md) | QA | 测试金字塔、Playwright 真机验证、变异测试防假绿 |
| [qoder.md](qoder.md) | 代码评审 | 八维审查、异构模型交叉复核 |
| [hermes.md](hermes.md) | 研究员 | 技术调研、复盘 postmortem、经验库回写 |

> 这些操作员与 [cli-gateway](../README.md) 的关系：调用方式节内置了经网关留证的调用配方（`cli-gateway run --observe --task-id ...`），每次派工的完整过程可回放、可审计。

## 安装

```bash
# 用户级（所有项目生效）
cp agents/*.md ~/.zcode/agents/

# 或项目级（仅当前项目生效）
cp agents/*.md .zcode/agents/
```

重启 ZCode 会话后生效，Agent 工具即可按 `name` 派工（如 `subagent_type: "codex"`）。其他支持自定义子代理的框架同样可用：这些 `.md` 本质是"角色化操作员提示词 + 调用配方"，粘贴为任意 agent 的系统提示词即可复用。

## 使用

主会话按岗位派工，派单三要素（目标/范围/完成标准）写清，例如：

```markdown
# 任务
修复 tests/test_api.py 中 3 个失败用例（并发写竞态）

# 完成标准
pytest 全绿 + 亲自运行过验证命令

# 约束
只改 src/worker.py，不动接口签名
```

子代理按固定三段式汇报（修改了什么 / 如何验证 / 哪些仍未验证）；经 cli-gateway 留证调用的，汇报附 `log=` 路径，验收方 `cli-gateway replay <log>` 逐字回放复核——**验收不轻信汇报文字**。

## 提示词结构（自定义时照此改）

每个文件的固定骨架：`frontmatter`（`name` 派工标识 / `description` 主会话路由依据 / `tools` 工具白名单 / `injectAgentsMd`）+ 正文（岗位定位与方法论 / 完成条件 / 预检三问 / 固定汇报格式 / 退化路径 / 调用方式含 cli-gateway 留证配方 / 红线 / 实测档案）。

## 定制指引

- **CLI 名称/版本不同**：改「调用方式」节的命令与核对版本即可，方法论不绑定具体 CLI
- **后端不是 GLM**：这些操作员原本跑在 GLM 后端，换 provider 只需改 CLI 侧的 provider 配置，提示词无需动
- **POSIX 环境**：调用配方中 `cmd //c` 包装与 MSYS 注意事项仅适用 Windows + Git Bash，POSIX 直接写命令名
- **开源版说明**：`agents/` 为脱敏模板（团队名/内网配置/实测记录已替换为占位或通用表述），各文件末尾的「实测档案」留作你的团队自行回写
