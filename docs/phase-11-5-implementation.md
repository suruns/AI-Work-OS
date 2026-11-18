# Phase 11.5 实施说明：受控执行与中文体验

## 本阶段目标

在 Phase 11.4 的项目级聊天、Master Agent、MCP 控制面和 Worker 基础上，建立明确的「申请 → 用户审批 → Worker 执行 → 验证 → 变更审查」闭环。AI Work OS 的默认用户语言统一为简体中文（zh-CN）。

## 使用方式

### 只读规划

Master Agent 继续使用 `get_project_overview`、`list_tasks`、`create_task` 和 `create_workflow`。普通任务和工作流始终以 `plan` 模式创建，不会获得写权限。

### 受控写入申请

用户明确要求修改项目时，Master Agent 可以调用 MCP 工具 `request_execution`。该工具可接收验收标准和最多五条明确的验证命令，并会：

1. 在当前对话绑定的项目中创建 `configured` 任务；
2. 将任务标记为 `requires_approval=true`；
3. 创建一条 `pending` 审批记录，并绑定项目、工作区路径、任务提示词摘要、权限模式、验收标准和验证配置；
4. 返回审批 ID，并告知用户需要在本机 Dashboard 审批。

该工具**不具备批准、拒绝或撤销能力**，也不会直接启动 Worker。

### 用户审批

Dashboard 的「写入执行审批」区域展示待审批申请，包括项目、任务、工作区、权限模式、验收标准、有效期、模型、沙箱模式和网络策略。用户可以明确选择：

- **批准并允许执行**：将审批改为 `approved`，任务仍由 Worker 按普通队列和 lease 机制领取；
- **拒绝**：审批进入 `rejected`，任务不会被 Worker 领取；
- 对已批准但仍排队的任务，可撤销审批；任务已运行后应使用任务取消接口。

API：

- `POST /execution-approvals/request/{task_id}`
- `GET /execution-approvals?status=pending`
- `GET /execution-approvals/{approval_id}`
- `POST /execution-approvals/{approval_id}/approve`
- `POST /execution-approvals/{approval_id}/reject`
- `POST /execution-approvals/{approval_id}/revoke`

审批接口会同时校验请求来源 IP、Host 与 Origin，只允许 `localhost`、`127.0.0.1` 或 `::1`，并要求 Origin 与请求协议、主机、端口完全一致，以降低 DNS rebinding 和跨端口 CSRF 风险。Dashboard 会提示用户在批准前检查项目/本地 Claude settings，因为其中可能配置 hooks 等命令；审批绑定相关文件内容摘要，变更后旧审批失效。当前项目没有用户身份认证，因此 `decision_source=local_control_plane` 只表示通过本机控制台作出决定，**不代表已建立可审计的真实用户身份或多用户 RBAC**。不要把服务暴露到公网或不可信局域网。

## Worker 安全门禁

- `plan` 任务沿用原有只读路径。
- 所有非 `plan`（当前为 `configured`）任务必须同时满足 `requires_approval=true` 和存在有效 `approved` 审批，才可被 claim。
- claim 时检查审批；首次执行和每一次自动修复启动 Claude 子进程前，都会再次检查任务状态、Worker 身份、租约、取消请求、审批状态与过期时间、项目和工作区路径及执行范围摘要。
- 任务提示词、验收标准、验证命令、权限模式、修复配置或工作区范围发生变化都会使原摘要不匹配，Worker 拒绝启动。
- 最终门禁会持有任务行锁（SQLite 使用 `BEGIN IMMEDIATE`）直到子进程创建完成，使取消请求要么先于进程启动生效，要么在进程创建后立即进入正常终止流程。无效审批会记录 `execution.approval_invalid`，并在 `process_started=false` 的情况下结束任务；数据库提交失败时会终止刚创建的子进程树。
- 配置模式使用 Claude Code `acceptEdits`，仍禁止 Bash 与 MCP；不使用 `bypassPermissions` 或 `--dangerously-skip-permissions`。
- 已经开始执行的任务不能通过撤销审批假装停止；必须调用任务取消接口并检查进程树终止记录。

## 数据与审计

新增 `execution_approvals` 表和 `tasks.requires_approval` 字段。审批记录绑定单个任务，不可跨任务复用，并带有过期时间；状态包括 `pending`、`approved`、`rejected`、`revoked`、`expired`。拒绝、撤销、过期或执行范围变化后，可在任务仍排队时重新申请，历史决策保留在事件记录中。

审批摘要绑定单个任务，覆盖项目 ID、规范化工作区路径、任务 ID、提示词摘要、权限模式、验收标准、验证命令、自动修复配置、最大尝试次数，以及 Claude 可执行文件路径及实际解析路径、模型、配置目录、实际启用的项目/本地/用户 settings 文件，以及项目根 CLAUDE.md、.claude/CLAUDE.md、rules、agents、commands 和 skills 指令文件内容摘要、Bubblewrap 可执行文件路径及实际解析路径、沙箱模式、网络策略和额外运行时路径。同时将 Anthropic、Bedrock、Vertex、Foundry 的路由与认证相关环境变量纳入摘要，仅保存 SHA-256，不暴露密钥原文。任何被加载的 Claude settings 或项目指令文件变化都会使旧审批失效。API、Master Agent 和 Worker 必须加载一致的执行策略环境变量；配置发生变化或进程配置不一致时，旧审批自动失效，Worker 会拒绝启动。任务仍在队列中时可重新提交审批申请。任务事件记录申请、批准、拒绝、撤销、过期、范围不匹配及执行门禁结果。事件仅保存审批 ID、状态和摘要等元数据，不复制完整提示词、密钥或原始工具参数。

## 中文默认规范

- Chat、Dashboard、审批按钮、状态、提示和错误说明默认简体中文。
- Master Agent 的系统指令要求默认使用简体中文，用户明确要求其他语言时遵从用户要求。
- Worker 收到任务时会附加简体中文报告要求；任务明确指定其他语言时仍遵从任务。
- 代码、变量名、API 路径、JSON 字段、CLI 命令和原始堆栈保留英文；面向用户的解释使用中文。
- 内部状态和事件类型保持稳定英文标识，Dashboard 通过中文映射展示。

## 部署启动顺序

SQLite 首次建库时，多个进程同时执行 `create_all()` 可能发生并发建表冲突。首次启动必须按顺序：

1. 启动 API；
2. 轮询 `http://127.0.0.1:8000/health`，直到返回 `{"status":"ok"}`；
3. 再启动 Worker；
4. 最后启动 Master Agent。

不要在全新数据库上同时启动三个进程。启动脚本会拒绝非回环 API 监听地址；API 和审批控制台必须绑定 localhost/127.0.0.1/::1。不要将当前无身份认证的 API 当作远程多人服务。

## Phase 11.5 一键验收

在 Linux 独立 worktree、非 root 用户和已配置模型网关的环境中运行：

```bash
bash scripts/acceptance/phase-11-5.sh
```

验收脚本将测试分为三个互相隔离的阶段：

1. **API/单元测试**：强制 `AI_WORK_OS_SANDBOX_MODE=off`，并清除所有真实 CLI/MCP/Bubblewrap/受控写入验收开关，避免 monkeypatch 假 CLI 被意外包进 Bubblewrap。
2. **真实执行验收**：只对三个真实验收测试文件启用 Bubblewrap、host 网络及真实验收开关；模型通过 `AI_WORK_OS_CLAUDE_MODEL` 从当前 shell 继承。
3. **Dashboard E2E**：重新强制 sandbox off，并清除真实验收开关；连续执行三轮浏览器测试。

### Python 与浏览器依赖

`e2e` extra 将 Playwright 固定为 **1.64.1**，对应 Chromium revision **1248**。在验收环境创建虚拟环境后安装项目依赖：

```bash
pip install -e ".[dev,e2e]"
```

首次准备浏览器时，需要能够访问 Playwright 浏览器下载源，并执行：

```bash
python -m playwright install chromium
```

如果验收机器无法访问下载源，可在可联网且使用相同 Playwright 1.64.1 的环境预先准备对应 Chromium 浏览器缓存，再将该缓存提供给验收环境。**不要用其他 Playwright 版本的 Chromium revision 代替。**

一键验收脚本只做版本与浏览器可执行文件预检，不会在测试过程中自动下载浏览器；缺少匹配浏览器时会立即失败并给出准备说明，避免网络受限环境长时间卡在下载阶段。

脚本结束前会运行 `git diff --check origin/main...HEAD`。任何阶段失败都会因 `set -Eeuo pipefail` 立即中止，只有全部阶段成功才会输出最终 PASS。

## 明确限制

- 当前审批是单机信任模型，不是身份认证、RBAC、远程审批或多租户安全方案。
- `acceptEdits` 不等于操作系统级沙箱；是否启用 Bubblewrap 仍由部署配置决定。
- Worker 不提供任意 Shell、Bash 或 MCP 写操作。
- 工作区变更报告是执行前后快照，不是完整文件系统审计；自动回滚不在本阶段保证范围内。
- 用户必须审查 diff、日志、验证结果和产物后，才能接受 Agent 变更。
