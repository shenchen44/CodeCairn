# CodeCairn 产品需求文档

> 基于 Pi 的 Proof-Carrying Coding 与 AI Change Review 平台

| 项目 | 内容 |
| --- | --- |
| 文档版本 | PRD v2.0 |
| 文档状态 | Approved for implementation |
| 产品名称 | CodeCairn |
| 产品定位 | AI Coding 到 Pull Request 之间的可验证变更证据层 |
| 原生运行宿主 | Pi Coding Agent |
| 兼容宿主 | Claude Code 官方 Plugin / Hook |
| 核心产品形态 | Pi Extension、Claude Code Plugin、Local Core、Web Review Workspace、GitHub/CI Integration |
| 核心概念 | Proof-Carrying Coding、Change Proof、Evidence Ledger、Evidence Graph |
| 默认部署方式 | Local-first，按需发布到 GitHub |
| 目标读者 | 产品、研发、测试、设计、安全、开源维护者与项目面试评审者 |

---

## 1. 文档目的

本文档定义 CodeCairn 下一阶段的完整产品方向、系统边界、用户体验、功能需求、
数据协议、评测指标、研发里程碑和上线标准。

本次重构基于以下战略判断：

1. CodeCairn 不再自研完整的通用 Coding Agent Runtime、模型 Provider、TUI、
   Session、流式输出和基础 Tool Loop。
2. Pi 作为 CodeCairn 的原生 Coding Runtime，承担模型调用、工具执行、终端交互、
   Session 和扩展生命周期。
3. CodeCairn 保留并强化自己的核心资产：开发证据采集、Evidence Ledger、
   Change Proof、Review UI、验证、导出、GitHub 发布和可信度治理。
4. Claude Code 通过官方 Plugin、Skill、Hook 和 MCP 能力接入，不使用非授权源码。
5. CodeCairn 的竞争优势不是“更会写代码”，而是让 AI 生成的每一处关键修改
   都能够被解释、验证、审核和交付。

本文档描述目标产品。文中标记为“现有”的能力应尽量复用，标记为“目标”的能力
需要后续开发。

---

## 2. 执行摘要

### 2.1 一句话定义

CodeCairn 是一个面向 AI Coding 工作流的本地优先 Change Intelligence 平台：
它在 Agent 开发过程中采集需求、代码证据、修改决策、Patch 和验证结果，
将其编译为可交互、可审计、可发布的 Change Proof。

### 2.2 核心问题

Claude Code、Codex、Cursor 和 Pi 等产品显著提高了代码生成速度，但没有同比例
降低代码审核成本。团队面临的新瓶颈包括：

- AI 生成 PR 数量上升，Reviewer 时间成为稀缺资源。
- PR Summary 多为事后生成，可能与真实开发过程不一致。
- Reviewer 只能看到“改了什么”，很难快速确认“为什么这样改”。
- 需求、定位依据、修改位置和测试结果分散在会话、Diff 和 CI 中。
- 测试通过不等于每项需求都被覆盖。
- 多个 Agent 产生不同轨迹格式，团队无法统一治理。
- 模型内部推理不可依赖、不可审计，也不适合作为合并依据。

### 2.3 产品解法

CodeCairn 将一次开发过程转换为：

```text
Requirement
  -> Repository Evidence
  -> Decision / Claim
  -> Tool Action
  -> Patch Hunk
  -> Impact Relation
  -> Verification
  -> Residual Risk
  -> Reviewer Decision
```

其中每个节点都包含来源、时间、Git 快照、可信度和关联关系。

### 2.4 核心差异

传统 Coding Agent：

```text
用户任务 -> Agent 修改代码 -> 事后生成总结
```

CodeCairn Proof-Carrying Coding：

```text
用户任务
  -> Agent 找到证据
  -> Agent 登记结构化修改决策
  -> Evidence Gate 允许修改
  -> 实际 Patch 与决策绑定
  -> 测试结果与需求和 Hunk 绑定
  -> Reviewer 确认
```

CodeCairn 不展示或声称恢复模型的隐藏 Chain of Thought。产品只记录模型主动
输出的结构化决策、可观察工具事件、代码快照和可复现验证结果。

---

## 3. 产品愿景与定位

### 3.1 产品愿景

让 AI 生成的代码像经过良好工程实践的人类代码一样，天然携带需求、依据、
验证和风险说明。

### 3.2 定位陈述

面向使用 AI Coding 工具开发并通过 Pull Request 协作的软件团队，CodeCairn
提供一个 Agent 无关的开发证据与 Review 平台。与普通 PR Summary 不同，
CodeCairn 的解释与真实开发事件、Git Hunk 和验证结果绑定，并明确区分事实、
推导、模型声明和人工确认。

### 3.3 产品类别

CodeCairn 不定义为另一个通用 Coding Agent，而定义为：

- AI Change Intelligence
- Proof-Carrying Coding
- AI Code Review Infrastructure
- Agent Development Trace and Governance

### 3.4 为什么以 Pi 为原生宿主

Pi 已提供 CodeCairn 不应重复建设的通用能力：

- 多 Provider 和多模型接口。
- Tool-calling Agent Loop。
- 成熟 TUI 与流式渲染。
- Session、分支、恢复和压缩。
- SDK、RPC、JSON 和无头模式。
- TypeScript Extension、自定义命令、工具和 UI。
- 完整 Agent、Turn、Message、Tool 和 Session 生命周期事件。
- MIT 许可证和可分发扩展机制。

CodeCairn 在 Pi 上以 Extension 形式实现，可以直接监听真实执行过程，而不必
维护一个能力较弱的平行 Runtime。

### 3.5 为什么兼容 Claude Code

Claude Code 具有广泛用户基础和成熟编码能力。CodeCairn 通过官方 Plugin、
Hook、Skill 和 MCP 接入，可以服务现有 Claude Code 用户，而不要求其迁移到
Pi。

Claude Code 兼容模式的重点是事件采集和 Review，不复制或修改 Claude Code
内部实现。

---

## 4. 产品原则

1. **Evidence over narrative**
   可定位代码、真实工具事件和测试结果优先于自然语言总结。

2. **Capture before inference**
   优先记录开发时产生的证据；只有缺少轨迹时才进行事后分析。

3. **Truth labeling**
   所有结论必须标记为 Captured、Derived、Inferred、Verified 或 Confirmed。

4. **No hidden-CoT dependency**
   不采集、展示或营销模型的隐藏思维链。

5. **Local first**
   源码、会话和证据默认保存在本机，未经用户操作不上传。

6. **Host-independent core**
   Pi 和 Claude Code 只负责产生统一 Capture Event，核心 Proof 不依赖宿主。

7. **Human authority**
   Agent 声明不自动等于事实，Reviewer 保留最终确认权。

8. **Reproducible verification**
   验证必须绑定命令、退出码、环境和 Git/工作区快照。

9. **Progressive disclosure**
   默认界面服务快速 Review，完整证据按需展开。

10. **Fail open for capture, fail closed for trust**
    采集失败不应阻断用户编码；但缺少证据时不得提升可信等级。

---

## 5. 目标与非目标

### 5.1 产品目标

#### G1：缩短理解时间

Reviewer 在 30 秒内能够回答：

- 这次修改解决什么需求？
- 修改了哪些文件和符号？
- 每一处修改的依据是什么？
- 哪些修改彼此关联？
- 运行了哪些测试？
- 哪些风险尚未解决？

#### G2：提高逻辑链真实性

关键修改优先使用开发过程中的结构化 Decision 和工具事件，而不是根据 Diff
事后生成理由。

#### G3：降低 PR 往返

减少“为什么这样改”“测试覆盖什么”“还有哪些影响”等重复沟通。

#### G4：支持多个 Coding Agent

同一 Change Proof 协议同时接收 Pi、Claude Code、Codex、Cursor、CI 和人工
输入。

#### G5：形成可评测的 Agent 数据

统一轨迹可用于：

- Agent 框架评测。
- Tool policy 评测。
- Evidence Gate 评测。
- SFT 轨迹构建。
- DPO 正负轨迹对构建。

### 5.2 非目标

- 不与 Claude Code、Codex、Cursor 正面竞争基础编码能力。
- 不开发新的通用模型 Provider 层。
- 不重写完整终端 TUI。
- 不恢复模型未公开的内部推理。
- 不仅凭模型声明自动批准 PR。
- MVP 不实现企业级多人实时协作。
- MVP 不自动合并 Pull Request。
- MVP 不承诺完整语言级静态分析。

---

## 6. 用户角色

### 6.1 AI Coding 开发者

主要任务：

- 使用 Pi 或 Claude Code 完成功能开发。
- 在提交前检查 Agent 修改是否符合意图。
- 生成准确的 PR 描述和证据报告。

成功标准：

- 不需要额外手写长篇修改说明。
- 能发现 Agent 理由和实际代码不一致。
- 能在提交前看到未验证需求。

### 6.2 Pull Request Reviewer

主要任务：

- 快速理解陌生改动。
- 从 Hunk 跳转到需求、证据和测试。
- 确认或驳回 Agent 声明。

成功标准：

- 减少上下文切换。
- 降低首轮 Review 时间。
- 提高高风险改动发现率。

### 6.3 Maintainer / Tech Lead

主要任务：

- 检查跨模块影响。
- 制定合并门禁。
- 识别 AI 代码的共性风险。

### 6.4 平台与安全团队

主要任务：

- 管理 Hook、工具权限和发布策略。
- 审核 CI 证明和数据来源。
- 统计 Agent 生成代码的质量和成本。

---

## 7. 核心使用场景

### S1：Pi 原生开发与 Review

```text
$ pi
> 为登录接口增加限流并补充测试

Agent:
  read / grep / edit / bash
  cairn_decision

> /cairn
```

系统打开 Review Workspace，展示完整开发逻辑链。

### S2：Claude Code 开发后 Review

```text
$ claude
> 修复支付回调幂等问题
> /codecairn:review
```

Claude Code Plugin 将 Hook 事件发送到 Local Core，随后打开 Review Workspace。

### S3：缺少轨迹的 Git-only Review

```bash
cairn review --base main
```

系统从 Git Diff 重建证据，但所有修改理由必须标记为 `Inferred/Post-hoc`。

### S4：CI 回填

GitHub Actions 运行测试后产生签名结果，CodeCairn 导入并将对应 Verification
从 Captured 升级为 Verified。

### S5：发布到 PR

用户确认逻辑链后，将摘要发布为 PR Description，将详细证据发布为 Comment 或
GitHub Check，并附带静态报告链接或 Artifact。

---

## 8. 产品组成

### 8.1 CodeCairn Pi Extension

职责：

- 注册 `/cairn`、`/cairn-status`、`/cairn-note`。
- 注册结构化 `cairn_decision` 工具。
- 监听 Agent、Turn、Tool、Message 和 Session 事件。
- 在 mutation 前执行 Decision Gate。
- 将事件写入 Local Core。
- 在 Agent settled 后触发 Proof 编译。
- 在 Pi TUI 中展示简洁状态。

### 8.2 CodeCairn Claude Plugin

职责：

- 提供 `/codecairn:review` Skill/Command。
- 配置官方 Hook。
- 监听 UserPromptSubmit、PreToolUse、PostToolUse、
  PostToolUseFailure、StopFailure 和 SessionEnd。
- 将 Claude transcript path、session id 和工具事件转换为统一事件。
- 调用 Local Core 打开 Review Workspace。

### 8.3 CodeCairn Local Core

职责：

- Capture Event 存储。
- Git Snapshot 与 Patch Fingerprint。
- Evidence Compiler。
- Evidence Ledger 与 Gate。
- Review State 和审计日志。
- HTTP API。
- HTML、JSON、Markdown、SVG、PNG 导出。
- GitHub 和 CI 集成。

现阶段优先复用当前 Python/FastAPI 实现。

### 8.4 Review Workspace

职责：

- 双栏代码对比。
- 文件与 Hunk 导航。
- 修改逻辑链。
- Evidence Graph。
- Verification 和风险。
- 人工确认。
- 导出和发布。

### 8.5 GitHub / CI Integration

职责：

- PR Description、Comment、Check 发布。
- 幂等更新。
- Git 绑定校验。
- CI Result 和 Attestation 导入。

### 8.6 当前能力与目标差距

| 能力 | 当前状态 | 本 PRD 目标 | 处理方式 |
| --- | --- | --- | --- |
| Git Change Proof | 已实现 | 包含 Decision、Tool Action、Capture Session | 演进 |
| 双栏 Diff Review UI | 已有 | 支持原生 Decision、虚拟化和完整导航 | 保留并增强 |
| Evidence Graph 与导出 | 已有 | 增加 Decision 和 Tool Action 节点 | 保留并增强 |
| Capture Event | 已实现 | 跨 Pi/Claude 的统一事件信封 | 迁移 |
| Claude Hook Adapter | 部分已有 | 官方 Plugin 化、完整生命周期和诊断 | 重构 |
| Pi Extension | 已实现 | 原生 Coding 宿主 | 持续增强 |
| `cairn_decision` | 已实现 | Proof-Carrying Coding 核心工具 | 持续增强 |
| Pre-mutation Gate | 已实现 | warning/block/off 策略 | 持续评测 |
| GitHub 发布 | 已有 Mock 和服务层 | 真实环境 Alpha 验收 | 验证并增强 |
| CI/Attestation | 已有模型与导入 | 真实 GitHub Actions 闭环 | 验证并增强 |
| 自研 Coding Runtime | 已移出产品仓库 | 不再作为主产品路径 | 独立归档 |
| 历史 Coding Agent 评测 | 已归档 | 不进入产品运行时 | 仓库外保留 |

---

## 9. 系统架构

```text
┌─────────────────────────────────────────────────────────────┐
│ Coding Hosts                                                │
│                                                             │
│  Pi + CodeCairn Extension      Claude Code + Official Plugin│
│  - native decision gate        - official hooks             │
│  - full event lifecycle        - compatibility capture      │
└───────────────────────┬───────────────────────┬─────────────┘
                        │ Unified Capture Event │
                        v                       v
┌─────────────────────────────────────────────────────────────┐
│ CodeCairn Local Core                                        │
│ Event Store -> Git Binder -> Evidence Compiler -> Gate      │
│                    -> Change Proof -> Audit Ledger           │
└──────────────────────────────┬──────────────────────────────┘
                               │
               ┌───────────────┼────────────────┐
               v               v                v
        Review Workspace   Static Export   GitHub / CI
```

### 9.1 进程模型

推荐 MVP：

- Pi Extension：TypeScript/npm package。
- Claude Plugin：官方 Plugin 目录。
- Local Core：Python 安装包和本地 FastAPI 进程。
- 通信：`127.0.0.1` HTTP + Session Token。
- 长期可选：Unix Domain Socket 或单文件原生二进制。

### 9.2 生命周期

1. 宿主 Session 启动。
2. Adapter 注册或恢复 CodeCairn capture session。
3. 用户提交任务。
4. Adapter 写入 Requirement Event。
5. 工具事件持续写入。
6. mutation 前执行 Decision Gate。
7. Agent settled 后采集 Git Snapshot。
8. Evidence Compiler 绑定 Decision、Tool Action 和 Patch Hunk。
9. 用户输入 `/cairn` 打开 Review。
10. 用户确认、验证、导出或发布。

### 9.3 宿主失效处理

- Local Core 不可用时，Adapter 写入本地 spool JSONL。
- Capture 失败不得阻止普通读取和测试。
- Decision Gate 是否 fail-open 由策略决定，个人默认 warning，团队策略可 block。
- Core 恢复后自动重放 spool。

---

## 10. Proof-Carrying Coding

### 10.1 定义

Proof-Carrying Coding 指 Agent 在执行关键 mutation 前，必须提交一个结构化
Decision Record，描述：

- 对应需求。
- 已观察证据。
- 修改主张。
- 目标文件或符号。
- 预期行为变化。
- 验证计划。
- 已知风险。

### 10.2 `cairn_decision` 工具

建议输入协议：

```json
{
  "requirement_ids": ["req_login_rate_limit"],
  "statement": "在认证入口增加按用户和 IP 的限流检查",
  "evidence": [
    {
      "path": "src/auth/login.ts",
      "line": 84,
      "symbol": "login",
      "reason": "所有密码登录请求都经过该入口"
    }
  ],
  "affected_paths": [
    "src/auth/login.ts",
    "tests/auth/login.test.ts"
  ],
  "expected_behavior": [
    "超过阈值返回 429",
    "正常登录行为不变"
  ],
  "verification_plan": [
    "运行登录限流测试",
    "运行认证模块回归测试"
  ],
  "risks": [
    "多实例部署下需要共享计数器"
  ]
}
```

返回：

```json
{
  "decision_id": "decision_xxx",
  "status": "accepted",
  "expires_on_workspace_change": true
}
```

### 10.3 Decision Gate

对 `edit`、`write`、`apply_patch` 等 mutation：

1. 检查是否存在未过期 Decision。
2. 检查目标路径是否在 `affected_paths`。
3. 检查 Evidence 是否来自已发生的 read/search/tool event。
4. 检查 Requirement 是否存在。
5. 检查 Decision 对应当前 Git Snapshot。

结果：

- `allow`：证据完整。
- `warn`：允许但标记缺口。
- `block`：拒绝 mutation，并向 Agent 返回缺少内容。

### 10.4 防止形式主义

Gate 不应只检查字段非空，还应检查：

- Evidence path 是否存在。
- line/symbol 是否可定位。
- Agent 是否实际读取过该文件。
- Decision 与目标 edit path 是否一致。
- 预期行为是否可验证。
- Decision 是否重复使用在无关修改。

### 10.5 轨迹不等于正确性

Decision 是模型声明，不自动证明实现正确。只有以下信息可以提升可信等级：

- 确定性 Git/代码推导。
- 本地可复现验证。
- 可信 CI Attestation。
- Reviewer 明确确认。

---

## 11. 统一 Capture Event 协议

### 11.1 设计目标

- 跨宿主。
- 追加写。
- 可重放。
- 可去重。
- 可校验。
- 可脱敏。
- 不依赖隐藏推理。

### 11.2 事件信封

```json
{
  "schema_version": "3",
  "event_id": "evt_xxx",
  "session_id": "session_xxx",
  "host": "pi",
  "event_type": "tool_result",
  "sequence": 12,
  "timestamp": "2026-07-28T12:00:00Z",
  "cwd": "/repo",
  "repository_id": "repo_xxx",
  "git_snapshot_id": "snapshot_xxx",
  "parent_event_id": "evt_previous",
  "payload_hash": "sha256:...",
  "previous_event_hash": "sha256:...",
  "payload": {}
}
```

### 11.3 标准事件

| 事件 | 用途 |
| --- | --- |
| `session_started` | 宿主 Session 建立 |
| `task_submitted` | 用户原始任务 |
| `agent_started` | Agent 开始 |
| `turn_started` | 新一轮模型调用 |
| `message_completed` | 可观察消息完成 |
| `tool_requested` | 工具调用参数 |
| `tool_started` | 工具执行开始 |
| `tool_progress` | 工具增量输出 |
| `tool_succeeded` | 工具成功 |
| `tool_failed` | 工具失败 |
| `decision_recorded` | 结构化修改决策 |
| `mutation_allowed` | Gate 放行 |
| `mutation_blocked` | Gate 拒绝 |
| `workspace_snapshot` | 工作区快照 |
| `agent_settled` | 本次任务无后续自动执行 |
| `verification_imported` | 本地或 CI 验证 |
| `review_decision` | 人工确认或驳回 |
| `session_ended` | Session 结束 |

### 11.4 Pi 事件映射

| Pi Event | CodeCairn Event |
| --- | --- |
| `before_agent_start` | `task_submitted` |
| `agent_start` | `agent_started` |
| `turn_start` | `turn_started` |
| `message_end` | `message_completed` |
| `tool_call` | `tool_requested` |
| `tool_execution_start` | `tool_started` |
| `tool_execution_update` | `tool_progress` |
| `tool_result` / `tool_execution_end` | `tool_succeeded` / `tool_failed` |
| `agent_settled` | `agent_settled` |
| `session_shutdown` | `session_ended` |

### 11.5 Claude Code 事件映射

| Claude Hook | CodeCairn Event |
| --- | --- |
| `UserPromptSubmit` | `task_submitted` |
| `PreToolUse` | `tool_requested` |
| `PostToolUse` | `tool_succeeded` |
| `PostToolUseFailure` | `tool_failed` |
| `StopFailure` | Agent failure event |
| `SessionEnd` | `session_ended` |

注意：Claude Code `Stop` 表示一次响应结束，不一定表示完整任务完成，因此不能
直接等价为 `agent_settled`。Adapter 应结合 transcript、Git 静默窗口和 Session
事件判断编译时机。

### 11.6 幂等和顺序

- `(host, session_id, event_id)` 全局唯一。
- 重复事件返回成功但不重复写入。
- sequence 跳号时标记 `capture_incomplete`。
- hash chain 失败时标记 `ledger_integrity=false`。
- 乱序事件允许暂存，超时后以缺失事件状态编译。

---

## 12. Change Proof 数据模型

### 12.1 顶层结构

```json
{
  "schema_version": "3",
  "change_id": "change_xxx",
  "repository": {},
  "git_snapshot": {},
  "capture_sessions": [],
  "requirements": [],
  "decisions": [],
  "evidence": [],
  "claims": [],
  "tool_actions": [],
  "file_changes": [],
  "patch_hunks": [],
  "mappings": [],
  "impact_relations": [],
  "verifications": [],
  "coverage_assertions": [],
  "risks": [],
  "review_decisions": [],
  "assurance": {},
  "gate": {},
  "audit_events": []
}
```

### 12.2 Provenance

所有主要实体必须包含：

```json
{
  "kind": "captured",
  "source": "pi_extension",
  "source_event_ids": ["evt_1", "evt_2"],
  "model": "provider/model",
  "confidence": 1.0,
  "created_at": "..."
}
```

`kind`：

- `captured`：宿主或用户真实产生。
- `derived`：通过 Git、AST 或哈希确定性推导。
- `inferred`：模型或启发式事后推断。
- `verified`：通过绑定环境的执行验证。
- `confirmed`：Reviewer 明确确认。
- `unknown`：无法确定来源。

### 12.3 Requirement

必须保存：

- 原始用户文本。
- 规范化文本。
- 来源宿主和 Session。
- 修订历史。
- 删除状态。
- 验收条件。

### 12.4 Decision

必须保存：

- Decision statement。
- Requirement IDs。
- Evidence IDs。
- affected paths/symbols。
- expected behavior。
- verification plan。
- risk statements。
- Git snapshot。
- Gate 结果。

### 12.5 Evidence

Evidence 必须可定位：

- path。
- line range。
- symbol。
- content hash。
- excerpt。
- 采集工具。
- stale 状态。

### 12.6 Patch Hunk

必须保存：

- old/new path。
- old/new range。
- diff。
- file change id。
- decision ids。
- claim ids。
- requirement ids。
- reviewed 状态。

### 12.7 Verification

必须保存：

- command 和 argv。
- result/effective status。
- exit code。
- output hash 和安全摘要。
- environment。
- commit SHA。
- content tree hash。
- patch fingerprint。
- requirement/hunk/file coverage。
- 本地或 CI 来源。
- attestation。

### 12.8 Residual Risk

风险必须包含：

- severity。
- statement。
- rationale。
- related entity ids。
- status。
- provenance。

---

## 13. Evidence Compiler

### 13.1 编译输入

- Capture Event。
- Pi/Claude Session 元数据。
- Git base/head/worktree。
- Decision Record。
- Tool Action。
- 测试和 CI 结果。
- Reviewer 输入。

### 13.2 编译步骤

1. 确定 Base Ref 和工作区。
2. 计算 Snapshot、Content Tree Hash 和 Patch Fingerprint。
3. 解析文件变化和 Hunk。
4. 从 task event 创建 Requirement。
5. 验证 Decision 引用的 Evidence。
6. 将 mutation tool action 与文件变化绑定。
7. 将 Decision 与 Hunk 绑定。
8. 将测试命令与 Requirement/Hunk 绑定。
9. 构建 Impact Relation。
10. 计算缺失覆盖和 Residual Risk。
11. 运行 Evidence Gate。
12. 写入不可变审计事件。

### 13.3 Hunk 绑定规则

优先级：

1. 工具返回的精确 edit range。
2. before/after content hash。
3. path + old/new text。
4. path + symbol。
5. path + 时间窗口。
6. 模型推断。

低于第 3 级的绑定不得自动标记为高可信。

### 13.4 手动修改

用户在 Agent 结束后手动修改代码时：

- 重新计算 Patch Fingerprint。
- 将不匹配部分标记为 `unattributed_change`。
- 不复用旧 Decision。
- 允许用户补充人工 Decision。

### 13.5 多 Agent 和并行工具

- 每个 Agent/Subagent 具有 actor id。
- Tool call 通过 tool call id 配对。
- 并行 mutation 分别绑定。
- 同一 Hunk 被多次修改时保留 superseded relation。

---

## 14. Evidence Gate

### 14.1 Gate 阶段

- Pre-mutation Gate。
- Post-patch Gate。
- Verification Gate。
- Publish Gate。

### 14.2 Pre-mutation 检查

- 是否存在 Requirement。
- 是否存在 Decision。
- 是否有可定位 Evidence。
- 目标路径是否被 Decision 覆盖。
- Decision 是否绑定当前 Snapshot。

### 14.3 Post-patch 检查

- 每个 Hunk 是否有 Claim。
- Claim 是否有 Evidence。
- Requirement 是否至少映射到一个 Hunk 或明确标记无需修改。
- 是否存在未归因 Hunk。
- 是否超出 declared scope。

### 14.4 Verification 检查

- 每项 Requirement 是否有验证。
- 每个高风险 Hunk 是否有验证。
- Verification 是否对应当前 Patch Fingerprint。
- 测试是否真实执行。
- CI 结果是否可信。

### 14.5 Publish 检查

- Git head/base 是否匹配目标 PR。
- 是否存在 rejected claim。
- 是否存在 stale verification。
- 是否存在高风险未接受项。
- 是否需要 Reviewer 确认。

### 14.6 Gate 结果

- `passed`：满足策略。
- `warning`：允许继续但有可见缺口。
- `blocked`：策略禁止继续。

### 14.7 误杀控制

必须记录：

- Gate block 次数。
- 用户 override 次数。
- 被阻止后最终证明正确的比例。
- Gate 增加的延迟和 Token。
- Gate 减少的错误 Patch。

个人默认策略应偏 warning；团队仓库可配置严格 block。

---

## 15. Review Workspace

### 15.1 启动方式

Pi：

```text
/cairn
```

Claude Code：

```text
/codecairn:review
```

独立 CLI：

```bash
cairn review --base main
```

### 15.2 默认布局

```text
┌───────────────┬──────────────────────┬──────────────────────┐
│ Files         │ Before               │ After                │
│               │                      │                      │
│ src/a.ts      │ aligned source       │ aligned source       │
│ tests/a.ts    │ highlighted diff     │ highlighted diff     │
├───────────────┴──────────────────────┴──────────────────────┤
│ Selected Hunk Logic Chain                                  │
│ Requirement -> Evidence -> Decision -> Hunk -> Verification│
└─────────────────────────────────────────────────────────────┘
```

### 15.3 文件列表

展示：

- change type。
- added/deleted lines。
- reviewed 状态。
- evidence completeness。
- verification 状态。
- risk badge。

### 15.4 双栏 Diff

要求：

- 完整源文件。
- old/new 代码行对齐。
- 新增、删除、修改高亮。
- Hunk sticky header。
- 横向滚动同步。
- 行号。
- 代码搜索。
- 大文件虚拟化。

### 15.5 点击 Hunk

侧栏展示：

- Requirement。
- Decision statement。
- Repository Evidence。
- Tool Action timeline。
- Verification。
- Impact Relation。
- Residual Risk。
- Provenance 和可信等级。

### 15.6 Evidence Graph

节点：

- Requirement。
- Evidence。
- Decision/Claim。
- Hunk/File/Symbol。
- Verification。
- Risk。
- Reviewer。

交互：

- 点击节点定位代码。
- 按来源和可信度过滤。
- 展开/收起。
- 查找未覆盖节点。
- 导出 SVG、PNG 和 HTML。

### 15.7 Review 操作

Reviewer 可以：

- Confirm/Reject Claim。
- Confirm/Revoke Mapping。
- Confirm Verification Coverage。
- 标记 Hunk reviewed。
- 接受或解决 Risk。
- 编辑 Requirement。
- 添加评论和人工证据。

所有操作写入 append-only Audit Ledger。

---

## 16. Pi Extension 功能需求

### PI-1 安装

支持：

```bash
pi install npm:@codecairn/pi
```

或项目级：

```json
{
  "packages": ["npm:@codecairn/pi@x.y.z"]
}
```

### PI-2 命令

| 命令 | 行为 |
| --- | --- |
| `/cairn` | 编译当前变更并打开 Review |
| `/cairn status` | 展示 capture、proof 和 gate 状态 |
| `/cairn note` | 添加人工 Decision/Evidence |
| `/cairn verify` | 运行或导入验证 |
| `/cairn publish` | 打开发布确认 |

### PI-3 TUI 状态

Extension 使用 Pi UI：

- Footer 状态显示 capture 是否正常。
- mutation 被 Gate 阻止时显示原因。
- Agent settled 后提示 Proof 已生成。
- 不重复实现 Pi 编辑器和消息列表。

### PI-4 Session 恢复

- 使用 Pi custom entry 保存 CodeCairn session binding。
- `/resume` 后恢复。
- `/fork` 和 `/clone` 创建新 Proof lineage。
- `/compact` 不得删除已持久化证据。

### PI-5 Tool Event

必须捕获：

- tool name。
- tool call id。
- input。
- partial update 的摘要。
- result/error。
- timing。
- actor/turn。

### PI-6 mutation 控制

`edit/write` 等 mutation 触发 Gate；`read/grep/find` 不触发 Gate。

---

## 17. Claude Code Plugin 功能需求

### CC-1 合法接入原则

- 仅使用 Claude Code 官方 Plugin、Hook、Skill、MCP 和公开 CLI。
- 不复制、打包或依赖非授权 Claude Code 源码。
- 不依赖内部未公开 API。

### CC-2 插件结构

```text
codecairn-claude-plugin/
  .claude-plugin/plugin.json
  hooks/hooks.json
  skills/review/SKILL.md
  scripts/capture
  scripts/open-review
```

### CC-3 Hook

Hook 默认异步采集，避免阻塞 Claude Code。

要求：

- stdin JSON 严格解析。
- stdout 不输出无关内容。
- 超时后写 diagnostic。
- 敏感字段脱敏。
- 重复事件幂等。

### CC-4 Review 命令

`/codecairn:review` 调用本地 CLI：

```bash
cairn review --capture-session "$SESSION_ID"
```

### CC-5 能力差异提示

Claude 兼容模式必须显示：

- 是否捕获到完整 Pre/Post Tool 事件。
- 是否存在原生 Decision。
- 是否存在缺失轨迹。
- 哪些理由属于 Post-hoc Inference。

---

## 18. GitHub 与 CI

### 18.1 发布目标

- PR Description。
- PR Comment。
- GitHub Check。
- Static HTML Artifact。

### 18.2 发布原则

- 默认 dry-run。
- 必须确认 repository、PR、base 和 head。
- 使用 marker 幂等更新。
- 不自动覆盖用户自定义内容。
- 不发布敏感源码全文。

### 18.3 PR 内容

默认包含：

- Requirement Summary。
- Files/Hunks Summary。
- Key Decisions。
- Verification。
- Residual Risks。
- Assurance。
- 详细报告链接。

### 18.4 CI 结果

支持：

- GitHub Actions Artifact。
- 本地 JSON 导入。
- 签名 Attestation。
- OIDC/平台证明。

`workflow success` 不自动等于 trusted verification。

---

## 19. CLI 与 API

### 19.1 CLI

```text
cairn daemon start|stop|status
cairn capture ingest
cairn capture sessions
cairn proof build
cairn review
cairn verify
cairn export --format json|markdown|html|svg|png
cairn publish --target description|comment|check
cairn doctor
```

原有 `cairn` 自研交互 Coding 模式进入维护状态，并在 Pi Extension 可用后废弃。

### 19.2 Local API

建议：

```text
POST /v1/capture/events
POST /v1/proofs/build
GET  /v1/proofs/{id}
GET  /v1/proofs/{id}/comparison/{file_id}
POST /v1/proofs/{id}/review-decisions
POST /v1/proofs/{id}/verifications
POST /v1/proofs/{id}/exports
POST /v1/proofs/{id}/publications
```

### 19.3 本地安全

- 仅绑定 `127.0.0.1`。
- 每次启动生成高熵 token。
- Host allowlist。
- 防止目录穿越。
- API 请求大小限制。
- 空闲自动关闭。

---

## 20. 隐私、安全与许可证

### 20.1 数据边界

默认不上传：

- 源码。
- 完整会话。
- 工具输出。
- API Key。
- 环境变量。

### 20.2 敏感信息

采集前后均执行：

- Token/API Key 检测。
- Authorization Header 脱敏。
- 数据库 URL 脱敏。
- 私钥和证书脱敏。
- 大输出截断和哈希。

### 20.3 Extension 安全

Pi Extension 和 Claude Plugin 都运行在用户权限下。必须：

- 开源可审计。
- 最小依赖。
- 禁止隐式网络上传。
- 清晰展示启用的 Hook。
- 支持一键禁用。

### 20.4 许可证策略

- Pi 为 MIT，可作为原生宿主和依赖。
- CodeCairn 自有代码使用项目选定的开源许可证。
- 不引入无许可证、泄露或仅研究用途的 Claude Code 源码。
- 发布前生成第三方依赖清单和 NOTICE。

---

## 21. 非功能需求

### 21.1 性能

- capture event 写入 P95 < 30 ms。
- mutation Gate P95 < 100 ms，不含可选模型判断。
- 1000 行 Diff 首屏 < 1.5 s。
- 100 文件 Proof 编译 < 5 s。
- `/cairn` 到浏览器可交互 P95 < 2 s。

### 21.2 稳定性

- capture 失败不导致宿主崩溃。
- Event Store 追加写具备崩溃恢复。
- 重复事件不产生重复 Proof 节点。
- Core 重启后 Session 可恢复。

### 21.3 兼容性

- macOS、Linux。
- Windows WSL 作为 P1。
- Pi 当前稳定版和上一稳定版。
- Claude Code 当前稳定版。
- GitHub.com，GitHub Enterprise 为 P1。

### 21.4 可访问性

- 全键盘导航。
- 颜色不是唯一状态信号。
- Diff 高亮满足对比度。
- 文本可复制。

### 21.5 可观察性

记录：

- capture latency/error。
- proof compile latency。
- gate decision。
- event loss。
- adapter version。
- schema migration。

默认仅本地记录。

---

## 22. 核心指标

### 22.1 North Star

**Verified Review Coverage**

```text
同时具备 Requirement、Evidence、Hunk 和有效 Verification 的关键改动
/
全部关键改动
```

### 22.2 用户价值

- 首轮 Review 时间。
- PR 往返轮数。
- Reviewer 找到关键修改的时间。
- Reviewer 对逻辑链有用性的评分。
- PR 描述手工修改比例。

### 22.3 逻辑链质量

- Requirement-Hunk Precision/Recall。
- Claim-Evidence Grounding Accuracy。
- Decision-Hunk Binding Accuracy。
- Verification Coverage Precision/Recall。
- Residual Risk Recall。
- Unattributed Hunk Rate。

### 22.4 Capture 质量

- Event completeness。
- Tool call/result pairing rate。
- Session binding success。
- Snapshot binding success。
- Adapter event loss。

### 22.5 Gate 质量

- Wrong Patch Prevention Rate。
- Viable Patch False Block Rate。
- Override Rate。
- Gate Added Latency。
- Gate Added Token Cost。

### 22.6 Guardrail

- 隐私事件。
- 错误发布。
- stale verification 被标记为有效的比例。
- Inferred 内容被误标为 Captured/Verified 的比例。
- 宿主崩溃率。

---

## 23. 评测设计

### 23.1 离线数据集

构建 CodeCairn Change Proof Benchmark：

- 真实 Pi Session。
- Claude Code Hook Session。
- Git commit/PR。
- 人工标注 Requirement-Hunk Mapping。
- 人工标注 Claim-Evidence。
- 测试覆盖关系。
- 高风险遗漏。

所有数据必须有授权和脱敏。

### 23.2 实验

#### E1：Captured vs Post-hoc

比较：

- Pi 原生 Decision。
- Claude Hook Capture。
- 仅 Git Diff 事后推断。

指标：

- Mapping F1。
- Claim Grounding。
- Reviewer 修改率。

#### E2：Decision Gate

比较：

- 无 Gate。
- Warning Gate。
- Blocking Gate。

指标：

- 错误 Patch。
- False Block。
- 延迟。
- Tool Calls。

#### E3：Review UI

比较：

- GitHub 原生 Diff。
- GitHub + AI Summary。
- CodeCairn Change Proof。

指标：

- Review 时间。
- 缺陷发现率。
- 信心评分。

#### E4：Adapter 一致性

同一任务分别由 Pi 和 Claude Code 执行，评估统一协议是否能产生可比较 Proof。

### 23.3 SWE-bench 的角色

SWE-bench 不再是产品唯一效果指标，主要用于验证：

- Adapter 不降低宿主编码结果。
- Gate 不显著误杀可行 Patch。
- Verification 绑定正确。
- Proof 能否解释 resolved/unresolved 差异。

产品核心评测转向 Review 效率和逻辑链真实性。

---

## 24. MVP 范围

### 24.1 P0

- Pi Extension。
- `/cairn`。
- `cairn_decision`。
- Pi Tool/Agent/Session Capture。
- Capture Event。
- Git Hunk Binding。
- Change Proof。
- Review Workspace。
- HTML/JSON/SVG 导出。
- 本地 Verification。
- Evidence Gate warning mode。
- 现有 GitHub dry-run。

### 24.2 P1

- Claude Code 官方 Plugin。
- GitHub Description/Comment/Check 真实发布。
- CI Artifact/Attestation。
- 大型 Diff 虚拟化。
- 完整键盘导航。
- Repository Knowledge。

### 24.3 P2

- Codex/Cursor Adapter。
- 团队策略。
- GitHub Enterprise。
- 云端协作。
- 组织级分析。

### 24.4 MVP 明确不做

- 自研模型 Provider。
- 自研 TUI。
- 自动合并。
- 隐藏思维链展示。
- 企业多租户云。
- 全语言精确调用图。

---

## 25. 研发里程碑

### M0：架构收敛

交付：

- 冻结 Capture Event。
- 冻结 Change Proof。
- 标记自研 Runtime 为 legacy。
- 建立 Pi Extension package。

退出条件：

- Pi Extension 能加载。
- Local Core 能接收事件。

### M1：Pi Capture

交付：

- 全生命周期事件。
- Session 恢复。
- Git Snapshot。
- `/cairn status`。

退出条件：

- 真实 Pi 任务事件完整率 >= 95%。

### M2：Proof-Carrying Coding

交付：

- `cairn_decision`。
- Pre-mutation Gate。
- Decision-Hunk Binding。
- Verification Binding。

退出条件：

- 标注集 Decision-Hunk Precision >= 90%。
- False Block <= 5%。

### M3：Review Product

交付：

- Review Workspace。
- Evidence Graph。
- Reviewer Decision。
- Export。

退出条件：

- 目标任务可完整完成开发、Review、导出。

### M4：Claude Compatibility

交付：

- 官方 Plugin。
- Hook Capture。
- `/codecairn:review`。
- Capture completeness 提示。

退出条件：

- 不使用非公开 API。
- Claude Code 更新兼容测试通过。

### M5：Delivery

交付：

- GitHub 发布。
- CI 导入和 Attestation。
- Beta 安装和升级。

退出条件：

- 真实仓库 Alpha 验收通过。

---

## 26. 从当前代码迁移

### 26.1 保留

- `codecairn/review/models.py`
- `codecairn/review/analyzer.py`
- `codecairn/review/server.py`
- `codecairn/review/graph.py`
- `codecairn/review/ledger.py`
- `codecairn/review/capture.py`
- `codecairn/review/ci.py`
- GitHub publishing。
- Review UI。
- Trust policy。
- Sandbox Verification。

### 26.2 改造

- 统一 Capture Event。
- Trace Compiler 支持 Pi Event。
- Change Proof 增加 Decision、Tool Action 和 Capture Session。
- Review UI 优先显示 native decision。
- CLI 增加 daemon/proof 命令。

### 26.3 废弃

以下历史实现已从产品包移除并独立归档：

- 自研 interactive prompt loop。
- 自研 OpenAI/DeepSeek Agent Loop。
- 自研 staged localization 作为默认 Coding 流程。
- 自研终端进度渲染。

历史 Coding Agent 评测资产归档在产品仓库之外，不进入安装包和测试路径。

### 26.4 兼容期

- 保留 `cairn review`。
- 在存储层兼容读取历史 Capture 记录并迁移到统一模型。
- 旧 Session 只读。
- 提供迁移诊断，不静默丢数据。

---

## 27. 风险与应对

### R1：产品退化为漂亮的 PR Summary

应对：

- 强制 provenance。
- 原生 Decision。
- Hunk 精确绑定。
- Verification Coverage。
- Reviewer Decision。

### R2：Agent 为满足 Gate 编造证据

应对：

- Evidence 必须引用真实 read/search event。
- 检查 path/hash/snapshot。
- 编造证据不提升 Assurance。

### R3：Gate 降低开发效率

应对：

- 个人默认 warning。
- 低风险 mutation 可批量 Decision。
- 持续评测 False Block 和延迟。

### R4：Pi API 变化

应对：

- 固定支持版本范围。
- Adapter contract test。
- 只使用公开 Extension API。
- Host capability negotiation。

### R5：Claude Hook 不完整

应对：

- 显示 capture completeness。
- 允许 Git reconstruction。
- 不将不完整轨迹标为 Captured full trace。

### R6：Python Sidecar 安装复杂

应对：

- Extension 自动 doctor。
- 后续提供单文件二进制。
- 明确端口和生命周期。

### R7：数据泄露

应对：

- Local-first。
- 脱敏。
- 发布预览。
- 最小内容发布。

### R8：无授权源码风险

应对：

- 禁止引入 claude-code-haha 等无许可证/泄露源码。
- 仅使用官方 Claude 扩展接口。
- 发布前许可证扫描。

---

## 28. 上线验收

### 28.1 功能 Definition of Done

- Pi 中能完成真实 Coding 任务。
- 每个 mutation 均可追溯到 Decision 或明确标记缺失。
- `/cairn` 能打开当前正确 Snapshot。
- Hunk 点击后可查看 Requirement、Evidence、Decision 和 Verification。
- Proof 可导出。
- Capture 失败不使 Pi 崩溃。
- 手动修改后旧证据自动 stale。

### 28.2 质量

- 单元、协议、集成和真实 E2E 测试通过。
- 无 P0/P1 数据损坏问题。
- Event 重放产生确定性 Proof。
- 无 Inferred 冒充 Verified。

### 28.3 安全

- 无敏感信息写入测试日志。
- 本地 API 有 Token。
- 发布前预览和确认。
- 依赖许可证检查通过。

### 28.4 文档

- Pi 安装指南。
- Claude Plugin 安装指南。
- 隐私说明。
- Event/Proof Schema。
- 故障排查。
- Adapter 开发指南。

---

## 29. 对外表达

### 29.1 中文

> CodeCairn 是面向 AI Coding 的可验证变更证据层。它基于 Pi 提供原生
> Proof-Carrying Coding，并兼容 Claude Code，将需求、代码证据、修改决策、
> Patch 和测试结果连接成可交互的 Change Proof。

### 29.2 英文

> CodeCairn is a verifiable change-intelligence layer for AI coding. It brings
> proof-carrying development to Pi and integrates with Claude Code, connecting
> requirements, repository evidence, change decisions, patch hunks, and
> verification into an interactive Change Proof.

### 29.3 核心口号

```text
Ship AI-written code with evidence.
```

### 29.4 不使用的表达

- “展示模型真实思维链。”
- “证明 AI 代码绝对正确。”
- “比 Claude Code 更强的 Coding Agent。”
- “自动替代人工 Reviewer。”

---

## 30. 参考与约束

- Pi 官方仓库：<https://github.com/earendil-works/pi>
- Pi Extension 文档：
  <https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md>
- Pi Session Format：
  <https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/session-format.md>
- Pi SDK：
  <https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sdk.md>
- Claude Code 官方 Hooks：
  <https://code.claude.com/docs/en/hooks>
- Claude Code 官方 Plugins：
  <https://code.claude.com/docs/en/plugins>
- Claude Code Skills：
  <https://code.claude.com/docs/en/slash-commands>

本文档明确排除将无许可证、泄露或仅供研究用途的 Claude Code 源码作为
CodeCairn 产品依赖。

---

## 31. 已确定决策与待验证事项

### 31.1 已确定

- CodeCairn 不再投入建设独立通用 Coding Runtime。
- Pi 是原生 Coding 宿主。
- Claude Code 仅通过官方扩展能力接入。
- Local Core MVP 继续使用现有 Python/FastAPI。
- Evidence/Review/GitHub/CI 是 CodeCairn 自有核心。
- 产品不展示隐藏 Chain of Thought。
- `cairn_decision` 和 Decision Gate 是主要差异化能力。

### 31.2 实施前需验证

- Pi Extension 在 npm 和 git package 两种安装方式下的资源路径。
- Pi 并行 mutation 工具事件的顺序和 Hunk 绑定策略。
- Pi Session fork/clone 后自定义 Entry 的继承语义。
- Claude Code Plugin 的 Hook 超时、异步重放和跨版本兼容性。
- Python Sidecar 的自动启动、升级和卸载体验。
- 个人 warning Gate 与团队 blocking Gate 的默认阈值。
- npm Extension 与 Python Core 的联合版本兼容策略。

### 31.3 版本兼容策略

每个 Adapter 启动时发送：

```json
{
  "adapter": "pi",
  "adapter_version": "0.1.0",
  "host_version": "x.y.z",
  "capture_schema_versions": ["3"],
  "capabilities": [
    "decision_gate",
    "parallel_tool_events",
    "session_entries"
  ]
}
```

Local Core 必须拒绝无法安全解释的更高 Schema，允许兼容读取受支持的旧 Schema，
并在 UI 中显示降级原因。
