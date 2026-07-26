# CodeCairn

CodeCairn 的核心定位是可嵌入、可扩展的通用 Coding Agent Runtime。核心只理解
`CodingTask`、会话、工具、模型和生命周期事件；GitHub Issue、SWE-bench、
交互式调用和自动化任务通过 adapter 接入。`full` Runtime 还提供声明式
AgentGraph、EvidenceLedger、隔离 worktree 候选补丁和通用失败恢复。架构见
[`docs/general_coding_agent_runtime.md`](docs/general_coding_agent_runtime.md)。

GitHub App 是其中一种入口：它监听 Issue webhook，在隔离工作目录和 Docker
Sandbox 中修改与验证仓库，成功后创建 PR。Runtime 也支持 SWE-bench
adapter、只读 review、investigate、explain、多轮 Session、动态工具和
非 GitHub 调用。

## 当前功能

- **Tool-calling agent loop**：支持 `list_files`、`search_code`、`read_file`、`write_file`、`apply_patch`、`run_tests`
- **通用任务协议**：change / review / investigate / explain 共享同一核心，只有 change 任务允许修改工作区
- **Pi-style 扩展机制**：运行时注册和切换工具，拦截 model/tool 生命周期事件
- **树状会话**：JSONL 持久化、上下文续接和任意历史节点 fork
- **多语言仓库能力**：自动识别 Python、JavaScript/TypeScript、Rust、Go、Java，并使用沙箱命令白名单
- **混合代码检索**：BM25、AST 符号和 import graph 经 RRF 融合，返回可解释的检索来源
- **多 Agent Runtime**：Supervisor 按复杂度选择标准链路或 Localization / Planner / Patch / Reviewer 深度链路
- **长短期记忆**：失败写入 task memory，通过测试的方案才提升为 repository memory，并支持置信度、去重和失效
- **Evidence Gates**：定位、计划和审查均使用强类型协议与可执行门禁
- **Sandbox guardrails**：限制允许修改路径、阻止高风险命令、限制最大变更文件数和 diff 行数
- **Observability**：记录 task 级与 attempt 级耗时、模型调用次数、工具调用次数
- **SWE-bench adapter**：读取实例、运行不同 RuntimePolicy、导出官方 `model_patch`
- **轨迹数据工具**：将结构化 rollout 转换为 SFT records、DPO preference pairs 和汇总结果

## 推荐使用方式

- 直接在本地仓库运行通用任务：

```bash
cairn run \
  --repo /path/to/repository \
  --intent review \
  --objective "审查缓存模块的并发与失效策略" \
  --session-file .agent/sessions/review.jsonl
```

- 在本机或服务器上运行 Runtime
- 配置 GitHub App 和模型 API
- 处理授权仓库中的 Issue、交互式任务或 SWE-bench 实例

## 目录结构

```text
./
  app/
    api/routes/
    core/
    db/
      migrations/
      models/
    schemas/
    services/
      comments/
      github/
      openai/
      sandbox/
      task_runner/
    tests/
      fixtures/toy_repo/
    workers/
    main.py
  experiments/
    swe_alignment/
  docs/
    general_coding_agent_runtime.md
  secrets/
  .workspaces/
  .env.example
  alembic.ini
  docker-compose.yml
  Dockerfile
  Makefile
  pytest.ini
  README.md
  requirements.txt
```

## 架构说明

### API 进程

- `POST /webhooks/github`：校验 GitHub webhook 签名，筛选 issue，落库 repository / issue / task / raw webhook artifact
- `GET /health`：健康检查
- `GET /tasks`：任务分页列表
- `GET /tasks/{task_id}`：任务详情、尝试记录、artifact
- `POST /tasks/{task_id}/rerun`：手动重新排队
- `GET /repositories`：已记录仓库列表
- `GET /dashboard`：PR review / merge / integration dashboard
- `GET /dashboard/prs`：dashboard 数据接口
- `GET /dashboard/metrics`：Agent 路由、门禁、成本、时延和结果指标
- `POST /dashboard/prs/{task_id}/merge`：合并可 merge 的 PR
- `POST /dashboard/prs/{task_id}/resolve-conflict`：为冲突 PR 创建 conflict resolution task
- `POST /dashboard/integrations`：基于多个 PR 创建 integration task

### Worker 进程

- 轮询 `triaged` 状态任务
- 用 GitHub App JWT 换 installation token
- clone 仓库到独立临时目录
- 创建基于默认分支的新分支
- 读取 `.agent.yml` 或安全默认配置
- 执行安装命令
- 由 Supervisor 选择标准或深度多 Agent 执行链
- 先运行只读 Localization Agent 和 evidence gate，再进入修改阶段
- 通过 `retrieve_code` 执行 BM25 + AST + import graph 混合检索
- 深度链路额外运行 Planner 和独立 Reviewer
- 通过工具接口执行 `list_files`、`search_code`、`read_file`、`apply_patch`、`git_diff`、`run_tests`
- 在重试中召回 task memory，测试通过后沉淀 repository memory
- 最多 3 轮 patch -> test -> retry
- 成功后 commit / push / create PR / issue comment
- 失败后记录 failure reason 与 test log，并回写 issue comment
- 支持 integration task
- 支持 conflict resolution task

通用 Runtime 结构见 `docs/general_coding_agent_runtime.md`。

### 状态机

```text
received -> triaged -> sandbox_ready -> patching -> testing -> retrying -> patching
testing -> ready_for_pr -> pr_opened -> done
* -> failed
```

### 关键安全护栏

- 只允许修改 `allowed_paths`
- 拒绝修改 `blocked_paths`
- 超过文件数或 diff 行数上限立即失败
- install/test command 做危险命令拦截
- 所有改动都在新 branch 上执行
- PR 默认添加 `needs-human-review`

## 环境变量

参考 `.env.example`：

- `APP_ENV`
- `DATABASE_URL`
- `OPENAI_API_KEY`
- `OPENAI_MODEL`
- `AGENT_RUNTIME_VARIANT`：`legacy`、`retrieval`、`memory` 或 `full`
- `GITHUB_APP_ID`
- `GITHUB_WEBHOOK_SECRET`
- `GITHUB_PRIVATE_KEY_PATH`
- `GITHUB_TARGET_LABELS`
- `WORKER_POLL_INTERVAL`
- `SANDBOX_BASE_IMAGE`
- `SANDBOX_TIMEOUT_SECONDS`
- `SANDBOX_MEMORY_LIMIT`
- `SANDBOX_CPU_LIMIT`
- `WORKSPACE_ROOT`
- `DOCKER_BIND_HOST_ROOT`
- `DOCKER_BIND_CONTAINER_ROOT`
- `LOG_LEVEL`

### 关键环境变量说明

- `GITHUB_PRIVATE_KEY_PATH`
  - Docker Compose 模式下建议填写容器内路径，例如：
    `GITHUB_PRIVATE_KEY_PATH=/app/secrets/your-app.private-key.pem`
  - 本地 Python 模式下应填写宿主机绝对路径，例如：
    `/Users/yourname/.../secrets/your-app.private-key.pem`
- `WORKSPACE_ROOT`
  - Docker Compose 模式下推荐为 `/app/.workspaces`
  - 本地模式可使用默认值，或显式设置成本地目录
- `DOCKER_BIND_CONTAINER_ROOT` / `DOCKER_BIND_HOST_ROOT`
  - 用于 Docker Compose 模式下把容器内工作区路径映射回宿主机真实路径
  - 当前 `docker-compose.yml` 已自动注入，通常不需要手改

## 推荐运行方式

推荐优先使用 Docker Compose 模式，因为它最接近"别人 clone 后直接自托管"的体验。

### 模式对比

- **Docker Compose 模式**
  - 推荐给大多数用户
  - API / worker / postgres 都在容器里
  - 需要 Docker Desktop 或 Docker Engine
  - `GITHUB_PRIVATE_KEY_PATH` 应使用容器内路径
- **本地 Python 模式**
  - 适合开发和调试
  - API / worker 在宿主机 Python 环境里运行
  - 仍然需要本机 Docker 可用
  - `GITHUB_PRIVATE_KEY_PATH` 必须是宿主机真实路径

## 本地开发启动

### 方式 1：Docker Compose

1. **准备 GitHub App 私钥文件**

将下载得到的 `.pem` 文件放到：

```text
secrets/<your-private-key>.pem
```

2. **复制环境变量文件**

```bash
cp .env.example .env
```

3. **填入 GitHub App 和 OpenAI 配置**

至少确认这些值：

```text
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4.1-mini
GITHUB_APP_ID=...
GITHUB_WEBHOOK_SECRET=...
GITHUB_PRIVATE_KEY_PATH=/app/secrets/<your-private-key>.pem
DATABASE_URL=sqlite:///./local.db
```

4. **启动服务**

```bash
docker compose up --build
```

5. **执行迁移**

如果是第一次启动，且数据库文件是全新的：

```bash
docker compose exec api alembic upgrade head
```

如果你之前已经有旧的 `local.db`，迁移报 `table ... already exists`，可以改用：

```bash
docker compose exec api alembic stamp head
```

服务默认：

- API：`http://localhost:8000`
- PostgreSQL：`localhost:5432`
- Dashboard：`http://localhost:8000/dashboard`

6. **检查容器状态**

```bash
docker compose ps
docker compose logs -f api
docker compose logs -f worker
```

### 方式 2：本地 Python 运行

1. **安装依赖并复制环境变量**

```bash
python -m pip install -e .
cp .env.example .env
```

2. **将 `GITHUB_PRIVATE_KEY_PATH` 改成宿主机真实路径**

例如：

```text
GITHUB_PRIVATE_KEY_PATH=/Users/yourname/Desktop/codecairn/secrets/<your-private-key>.pem
```

3. **执行迁移并启动**

```bash
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

另开一个终端：

```bash
python -m app.workers.poller
```

### Docker Compose 模式下的额外要求

worker 会在容器里调用宿主机 Docker daemon 来运行 sandbox。当前 `docker-compose.yml` 已包含：

- Docker socket 挂载
- 工作区目录路径映射
- `DOCKER_BIND_*` 环境变量

如果你修改了项目目录结构或运行方式，需要确保：

- 宿主机 Docker 正常运行
- 容器里能执行 `docker`
- 容器里的工作区路径能映射到宿主机真实项目目录

## GitHub App 创建方式

1. 在 GitHub Developer Settings 中创建 GitHub App
2. 开启权限：
   - Repository permissions：`Contents: Read & write`、`Issues: Read & write`、`Pull requests: Read & write`、`Metadata: Read-only`
3. 订阅 webhook 事件：
   - `Issues`
4. 记录：
   - App ID
   - Webhook secret
   - Private key PEM 文件
5. 将 App 安装到目标仓库

### 建议的 GitHub App 权限

- Repository permissions
  - `Contents: Read & write`
  - `Issues: Read & write`
  - `Pull requests: Read & write`
  - `Metadata: Read-only`

如果权限不够，常见失败会体现在：

- clone 失败
- push 失败
- create PR 失败
- merge 失败

## Webhook 本地调试

可以使用 ngrok 或 GitHub App webhook delivery：

```bash
ngrok http 8000
```

把公开地址配置到 GitHub App webhook URL，例如：

```text
https://<your-ngrok-subdomain>.ngrok.app/webhooks/github
```

本地调试时，也可以手工发送一个带签名的 JSON payload 到该接口。

## OpenAI API 配置

在 `.env` 中设置：

```text
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4.1-mini
```

当前实现使用 OpenAI Responses API，并保留工具调用与 trace/artifact 结构用于审计。

## Worker 与 Docker 沙箱

worker 会调用宿主机 Docker 来运行安装与测试命令。

- Linux/macOS 开发时，`/var/run/docker.sock` 挂载到 `api` / `worker` 容器即可
- Docker Desktop on Windows 也可通过 Docker socket 暴露方式运行，但请确保容器内能访问宿主 Docker Engine
- 每次任务都在独立临时目录执行，容器以 repo 目录挂载到 `/workspace`
- Docker Compose 模式下，工作区默认位于容器内 `/app/.workspaces`，系统会自动转换成宿主机真实路径后再挂载给 sandbox 容器

## Dashboard 功能

打开：

```text
http://localhost:8000/dashboard
```

当前 dashboard 支持：

- 查看 open PR
- 查看 root cause / change summary / diff
- 查看 mergeability 状态
- 直接打开 GitHub PR
- 对无冲突 PR 执行 merge
- 选择多个 PR 创建 integration task
- 对冲突 PR 发起 conflict resolution task
- 旧 PR 被 resolved PR 替代时显示 superseded 状态

## 常见问题

### 1. `/dashboard/prs` 返回 500

常见原因：

- `GITHUB_PRIVATE_KEY_PATH` 写错
- Docker 模式下用了宿主机路径
- 本地 Python 模式下用了容器内路径

### 2. worker 里找不到 `docker`

请确认：

```bash
docker compose exec worker sh -lc 'command -v docker'
docker compose exec worker docker ps
```

### 3. resolve conflict 报 mount denied

这通常是容器内路径被直接传给宿主机 Docker。当前版本已经通过 `DOCKER_BIND_HOST_ROOT` / `DOCKER_BIND_CONTAINER_ROOT` 做了映射；如果你改动了 compose 或目录结构，请检查这两个变量是否仍正确。

### 4. 任务卡在 `patching`

先查看：

```bash
curl http://127.0.0.1:8000/tasks
docker compose logs -f worker
```

### 5. `rerun` 返回 `task_not_rerunnable`

只有 `failed` 或 `done` 状态的 task 才能 rerun。

## 运行测试

```bash
python -m pytest app/tests -q -p no:cacheprovider
```

当前测试覆盖：

- webhook 签名校验
- issue 过滤逻辑
- 状态机流转
- repo config 解析
- diff / blocked path 限制
- task 去重
- PR body 生成
- toy repo 最小闭环
- dashboard / integration / conflict resolution
- sandbox 路径映射与 repo-local `.venv`

## 如何演示一次最小闭环

### 纯本地闭环

直接运行测试中的 toy repo 场景：

```bash
python -m pytest app/tests/test_toy_repo_integration.py -q -p no:cacheprovider
```

这个测试会：

- 初始化一个最小 Python toy repo
- 模拟 issue 对应的 patch
- 通过工具接口应用 patch
- 运行 toy repo 的 pytest
- 生成 PR body

### GitHub App 真机联调

1. 启动 `postgres`、`api`、`worker`
2. 在目标仓库创建带目标 label 的 issue
3. 确保 issue body 非空
4. GitHub webhook 到达 `POST /webhooks/github`
5. 访问 `GET /tasks` 查看任务
6. 访问 `GET /tasks/{task_id}` 查看 attempt、diff、test log、PR 信息
