# CodeCairn

CodeCairn 是 AI 辅助开发到人工 Review 之间的本地优先证据层。Pi 负责成熟的
交互式编码体验，CodeCairn 负责采集实现决策、关联最终 Diff、执行受限验证，
并生成可审查的 Change Proof。

CodeCairn 保存的是 Agent 主动输出的结构化判断依据和仓库证据，不采集或展示
模型隐藏思维链。

## 安装

```bash
python -m pip install -e .
pi install "$(pwd)/packages/pi-extension"
```

进入待修改仓库并启动 Pi。Agent 在执行 `edit` 或 `write` 前，会通过
`cairn_decision` 记录修改摘要、判断依据、备选方案、影响文件、代码证据、风险
和验证计划。

```bash
cd /path/to/repository
pi
```

编码完成后在 Pi 中输入 `/cairn`。浏览器会先打开 Review 工作台，再随着分析
进度把已完成的文件动态加入左侧列表。

## Review 工作台

当前提供：

- 对齐的修改前/修改后源码与 Diff 高亮；
- 文件、Hunk 和关联位置导航；
- 实现决策、Claim、代码证据之间的逻辑链；
- Requirement 映射、Residual Risk 和来源标签；
- 受限 Docker 环境中的本地验证；
- Stale 状态与哈希链完整性检查；
- Markdown、JSON、HTML、SVG、PNG 导出；
- GitHub PR Description、Comment 和 Check 发布。

已有本地改动时也可直接启动：

```bash
cairn review --base main --requirement "空输入必须返回零"
```

无浏览器导出：

```bash
cairn review --base main --format json
cairn review --base main --format markdown
cairn review --base main --format html --output change-proof.html
```

## Capture

不同 Agent 宿主通过统一命令提交事件：

```bash
cairn capture ingest --host pi --repo .
cairn capture sessions --repo .
cairn capture show SESSION_ID --repo .
cairn capture replay --repo .
```

事件会递归脱敏、幂等追加，并使用 SHA-256 Hash Chain 连接，默认存储在
`~/.codecairn/captures/`。采集器暂时不可用时，Pi Extension 会写入脱敏后的
本地 spool。

## 架构

```text
Pi Extension
  -> CaptureEvent + DecisionRecord
  -> Change Proof 编译
  -> Review Workspace + Evidence Graph
  -> Sandbox Verification
  -> GitHub Delivery
```

正式包只保留当前产品模块：

```text
codecairn/
  review/         变更分析、证据模型、Review UI、导出与 CI 信任
  verification/   仓库策略与 Sandbox 执行
  github/         认证与 PR 发布
  cli.py           本地命令入口
packages/
  pi-extension/   Pi 生命周期适配器和修改门禁
tests/             面向当前产品的可靠性测试
```

详细设计见[产品 PRD](docs/codecairn_product_prd_zh.md)和
[Pi Extension 使用文档](docs/pi_extension.md)。
