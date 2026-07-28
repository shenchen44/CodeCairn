# CodeCairn Pi Extension

## 定位

Pi 负责模型交互、会话和代码工具，CodeCairn Extension 负责记录可审查的实现
决策和执行事件。两者通过稳定 CLI 协议连接，不复制或修改 Pi 源码。

Extension 记录结构化 rationale，不记录模型隐藏思维链。

## 安装与启动

```bash
python -m pip install -e .
pi install "$(pwd)/packages/pi-extension"
cd /path/to/repository
pi
```

编码完成后输入 `/cairn` 打开 Review 工作台；输入 `/cairn status` 查看当前
会话的采集状态。

## Decision Gate

Extension 注册 `cairn_decision` 工具，内容包括：

- 修改摘要和依据；
- 考虑过的备选方案；
- 精确的仓库相对路径；
- 代码路径、行号、符号和事实陈述；
- 残余风险；
- 验证计划。

只有 Pi 成功读取或搜索过的路径才能作为已检查证据。后续 `edit`、`write`
必须被一个 accepted Decision 覆盖。

默认使用阻断模式：

```bash
CODECAIRN_GATE_MODE=block pi
```

评估误拦截时可使用 `warn`；`off` 会关闭修改门禁。

## Capture Transport

Extension 通过以下命令写入事件：

```bash
cairn capture ingest --host pi --repo REPOSITORY
```

事件以 append-only JSONL 存储在 `~/.codecairn/captures/`，支持递归密钥脱敏、
幂等事件 ID 和 SHA-256 Hash Chain。采集命令不可用时，Extension 写入
`~/.codecairn/spool/pi/events.jsonl`，之后可重放：

```bash
cairn capture replay
```

## Change Proof

启动 `cairn review` 时，accepted Decision 会被编译为：

- 表示已检查仓库事实的 Evidence；
- 表示实现判断的 Claim；
- Claim 到对应 Diff Hunk 的 `explains_change` Mapping；
- 回指原始 Capture Event 的 Provenance。

原始事件和 Decision Record 始终保留，派生节点不会覆盖其来源。
