# Phase 11.5：受控执行与审批闭环

## 目标

在 Phase 11.4 的项目级对话、Master Agent、MCP 控制面和只读 Worker 基础上，增加**显式用户授权后才允许写入项目工作区**的受控执行能力。本阶段同时确立简体中文（zh-CN）为默认用户语言。

## 当前基线

- 主分支基线：`1c82043bea7b94c20f93b3bfa514ece44482d2ae`
- Master Agent 当前提供项目只读检查及 plan-mode Task / Workflow 创建。
- Task 已有 `plan` / `configured` 权限模式；configured 使用 Claude Code `acceptEdits`，不是 `bypassPermissions`。
- API 当前没有用户身份认证。审批 UI/API 仅适用于可信本机环境；不能将本机按钮视为远程身份认证。

## 安全不变量

1. **默认拒绝写入**：新建任务默认 plan；没有有效审批的 configured 写任务不得被 Worker claim。
2. **Agent 不可自批**：Master Agent MCP 不提供 approve / reject / revoke 工具。审批只能来自独立本机控制台。
3. **审批绑定不可变范围**：审批绑定 Task、Project、规范化 workspace 路径、权限模式、任务提示词摘要、验收标准、验证命令和修复配置。审批后范围变化必须使审批失效。
4. **单任务生命周期授权且有时效**：一次审批只授权该 Task 的一个执行生命周期，并受已批准的最大尝试次数/自动修复配置约束；任务进入终态后不能复用审批。审批有状态、决策时间和过期时间；拒绝、撤销、过期均不能启动写执行。
5. **执行前再次检查**：Worker claim 和启动 Claude 子进程前都检查审批状态及范围摘要，避免排队后被修改或撤销仍执行。
6. **取消与租约优先**：任务取消、Worker lease fencing 和进程组终止继续有效；审批不能覆盖取消状态。
7. **可审计**：申请、批准、拒绝、撤销、过期、执行开始/结束写入结构化事件；事件不保存 prompt 正文、密钥或原始工具参数。
8. **隔离不夸大**：Bubblewrap 是可选 Linux 隔离边界；host 网络与读写挂载仍有风险。未启用沙箱时，不宣称具备 OS 级隔离。
9. **中文默认**：用户可见 UI、Master Agent 回复、任务计划/摘要/报告和错误说明默认简体中文；技术标识、CLI 原始输出和堆栈保留原文，必要时附中文解释。用户明确指定其他语言时遵从用户要求。

## 设计方案

### 受控执行申请

新增 MCP `request_execution`：

- 仅允许在当前会话绑定项目内创建任务。
- 创建 `permission_mode=configured`、`requires_approval=true` 的 Task。
- 同一事务创建 `pending` 审批记录和审计事件。
- 任务只进入等待审批状态，不可被 Worker claim。
- 原有 `create_task` / `create_workflow` 保持 plan-only。
- Master Agent 只能申请，不能批准、拒绝或撤销。

### 审批记录

新增 `execution_approvals` 表，绑定：

- approval ID、task ID、project ID；
- 规范化 workspace 路径；
- 执行权限模式和执行范围摘要；
- pending / approved / rejected / revoked / expired 状态；
- requested / decided / expires 时间；
- 决策来源标记（当前为本机控制台，不伪造用户身份）。

任务内容或工作区范围发生变化时审批失效。审批过期默认 24 小时，可通过 `AI_WORK_OS_APPROVAL_TTL_HOURS` 配置（1–168 小时）。

### API 与控制台

- `POST /execution-approvals/request/{task_id}`
- `GET /execution-approvals?status=pending`
- `GET /execution-approvals/{approval_id}`
- `POST /execution-approvals/{approval_id}/approve`
- `POST /execution-approvals/{approval_id}/reject`
- `POST /execution-approvals/{approval_id}/revoke`

审批控制面仅接受 loopback 本机请求。批准只更新审批状态，不直接启动进程；Worker 仍通过正常队列和 lease 机制领取任务。

### Worker 门禁

- claim 查询排除所有没有有效审批的非 plan 任务。
- claim 后、构造 Claude 命令前再次检查审批状态、有效期和范围摘要。
- 未审批、拒绝、过期、撤销、摘要不匹配的任务不得启动 Claude 子进程。Claude 主执行、自动修复和验证命令均通过同一任务租约/审批/取消门禁启动。
- configured 仍使用 `acceptEdits`，禁止 Bash 和 MCP；绝不使用 bypass 权限。
- 已运行任务撤销审批会被拒绝，必须通过任务取消接口停止。

## 分阶段交付

| 子阶段 | 交付 |
|---|---|
| 11.5.1 | 审批模型、迁移兼容、审批 API 和审计 |
| 11.5.2 | Worker claim / 子进程启动前双重门禁 |
| 11.5.3 | Master Agent 受控执行申请 MCP 工具 |
| 11.5.4 | 中文 Chat、Dashboard、审批界面和 Agent 提示词 |
| 11.5.5 | 取消、撤销、过期、范围变更和并发竞态验证 |
| 11.5.6 | 全量自动化、E2E、真实 CLI + Bubblewrap 集成验收 |

## 明确不在本阶段承诺

- 不是远程多用户身份认证或 RBAC；当前 API 无认证，生产网络部署前必须另行建设认证与访问控制。
- 不是任意 Shell / 主机级操作授权；Worker 仍禁止 Bash 和 MCP 工具。
- 不保证自动回滚所有文件系统副作用；变更审查与后续显式回滚机制独立设计。
- 不把模型自述的“已修改/已测试”当作事实；以 Worker 状态、验证结果和工作区变更报告为准。

## Phase 11.5 一键本机集中验收（Linux）

在独立 worktree 中安装项目依赖、激活该 worktree 的 Python 虚拟环境，并确认 Claude Code CLI、Bubblewrap 与模型网关可用。设置模型名后运行一条命令，即可执行全量自动化、真实 CLI/MCP、真实 Bubblewrap MCP、真实受控写入和三轮 Dashboard E2E：

```bash
AI_WORK_OS_CLAUDE_MODEL=你的网关模型名 bash scripts/acceptance/phase-11-5.sh
```

脚本会检查当前用户非 root、Claude Code CLI 和 Bubblewrap 可用；先执行 `git diff --check origin/main...HEAD`，再使用临时 SQLite 数据库和临时存储目录运行测试。真实受控写入会先验证 pending/rejected/revoked/scope-changed/expired 五种情况都不能领取任务，再批准一个 configured Task，由真实 Worker 在 Bubblewrap 中调用 Claude Code 创建指定文件并运行验证命令。脚本不会改动正式项目工作区，但会产生真实模型调用费用。

## 最终验收门槛

1. 未审批、已拒绝、已过期、已撤销、范围摘要不匹配的任务，Claude 子进程启动次数为 0。
2. Master Agent 无法调用批准/拒绝/撤销能力。
3. 并发重复批准不能产生多个执行；遵守现有唯一 claim / lease 语义。
4. 只有批准任务能以 configured 模式执行，命令中不含 bypass 权限参数。
5. 用户可查看申请、决策、执行和验证的完整事件链。
6. Chat、Dashboard、Agent 输出默认简体中文。
7. Phase 11.4 的自动化、E2E、真实 CLI、MCP、Bubblewrap 验收全部不回归。
8. Linux 本机真实环境集中验收通过后，才允许合并 main。
