# micro-swe-agent

**[🇺🇸 English](README-en.md)** | **[🇨🇳 中文](README-zh.md)**

---

micro-swe-agent 是一个可本地自托管、可嵌入和可扩展的通用 Coding Agent
Runtime。GitHub Issue、SWE-bench 和交互式代码任务会转换为统一
`CodingTask`，再由 RuntimePolicy 与 Supervisor 选择执行图。

Runtime 支持 change、review、investigate 和 explain 任务，提供动态工具权限、
Sandbox、Hybrid Code Retrieval、树状 Session、长短期 Memory、结构化
Agent hand-off、Evidence Gate、Planner、Patch、Reviewer 和 Evidence Ledger。
GitHub App 入口可以完成 Issue 接收、隔离修改、测试、重试、创建 PR 和
Dashboard 管理。

## Architecture

```
received -> triaged -> sandbox_ready -> patching -> testing -> retrying -> patching
testing -> ready_for_pr -> pr_opened -> done
* -> failed
```

## Quick Start

### Docker Compose

```bash
cp .env.example .env
# Fill in OPENAI_API_KEY, GITHUB_APP_ID, GITHUB_WEBHOOK_SECRET, etc.
docker compose up --build
docker compose exec api alembic upgrade head
```

### Local Python

```bash
python -m pip install -r requirements.txt
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# In another terminal
python -m app.workers.poller
```

## Full Documentation

| Language | File | Description |
|---|---|---|
| 🇺🇸 English | [README-en.md](README-en.md) | Full English documentation |
| 🇨🇳 中文 | [README-zh.md](README-zh.md) | 完整中文文档 |
