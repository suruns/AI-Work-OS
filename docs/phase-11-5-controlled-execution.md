# Phase 11.5：受控执行与审批闭环

## 目标

在 Phase 11.4 的项目级对话、Master Agent、MCP 控制面和只读 Worker 基础上，增加**显式用户授权后才允许写入项目工作区**的受控执行能力。授权必须是一次明确、可审计、范围固定的决定；Master Agent 不能自行批准，也不能通过普通任务创建接口绕过审批。

本阶段同时确立简体中文（zh-CN）为默认用户语言。

## 当前基线

- 主分支基线：`1c82043bea7b94c20f93b3bfa514ece44482d2ae`
- Master Agent 当前仅提供项目只读检查及 plan-mode Task / Workflow 创建。
- Task 已有 `plan` / `configured` 权限模式；普通 Worker 的 configured 模式使用 Claude Code `acceptEdits`，不是 `bypassPermissions`。
- API 当前没有用户身份认证。审批 UI/API 只能在可信的本机环境中使用；服务必须绑定 localhost，不能把“点击按钮”误称为远程身份认证。

## 安全不变量

1. **默认拒绝写入**：新建任务默认 plan；没有有效审批的受控写任务不得被 Worker claim。
2. **Agent 不可自批**：Master Agent MCP 不提供 approve / reject / revoke 工具。审批只能来自独立控制面（当前阶段为用户操作的 Dashboard/API）。
3. **审批绑定不可变范围**：审批记录绑定 Task、Project、规范化 workspace 路径、执行权限模式，以及任务提示词/验收配置摘要。审批后若执行范围发生变化，审批必须失效。
4. **一次性且有时效**：审批有明确状态、时间戳和过期时间；拒绝、撤销、过期均不能启动写执行。
5. **执行前再次检查**：Worker claim 与实际启动子进程前都检查审批状态及绑定摘要，避免排队后被修改或撤销仍执行。
6. **取消与租约优先**：取消、Worker lease fencing、进程组终止继续有效；审批不能覆盖取消状态。
7. **可审计**：申请、批准、拒绝、撤销、过期、执行开始/结束均写入结构化事件；事件不保存 prompt 正文、密钥或原始工具参数。
8. **隔离不夸大**：Bubblewrap 仍是可选的 Linux 隔离边界；host 网络与读写挂载仍有风险。未启用沙箱时，不宣称具备 OS 级隔离。
9. **中文默认**：用户可见 UI、Master Agent 回复、任务计划/摘要/报告和错误说明默认简体中文；代码标识、API 字段、CLI 原始输出和堆栈保留原文，必要时附中文解释。用户明确指定其他语言时遵从用户要求。

## 设计方案

### 受控执行请求

新增独立的受控执行请求流程，而不是让 Master Agent 把普通 `create_task` 的权限直接提升：

- MCP 新增 `request_execution`（名称可在实现评审中微调），只允许在当前会话绑定项目内创建任务。
- 该工具创建 `permission_mode=configured`、`requires_approval=true`、`approval_status=pending` 的 Task。
- 任务只进入等待审批状态，不可被 Worker claim；Master Agent 必须明确告诉用户尚未开始执行。
- 原有 `create_task` / `create_workflow` 保持 plan-only 行为和兼容性。

### 审批记录

新增独立的执行审批记录（建议表名 `execution_approvals`），至少包含：

- approval id、task id、project id；
- 规范化 workspace 路径；
- 受控执行权限模式；
- 对 prompt、acceptance criteria、verification commands 及关键执行配置的规范化摘要；
- pending / approved / rejected / revoked / expired 状态；
- created / decided / expires 时间；
- 决策来源标记（当前为本地控制台用户操作，不伪造用户身份）。

审批范围创建后不可由 Agent 修改。任务内容或工作区发生变化时必须拒绝执行并要求重新申请审批。

### API 与控制台

提供待审批列表、审批详情、批准、拒绝和撤销端点。所有变更采用条件更新，避免重复点击、并发批准或审批与取消竞态。批准接口只改变审批记录，不直接启动子进程；Worker 仍通过正常队列和 lease 机制领取任务。

Chat / Dashboard 增加待审批提示与任务摘要，批准前展示项目、工作区、任务意图、权限模式、验收标准及风险说明。按钮需明确区分“批准并允许 Worker 执行”和“拒绝”。不提供默认勾选或自动批准。

### Worker 执行门禁

- claim 查询排除所有需要审批但尚未批准的 Task。
- claim 后、构造 Claude 命令前重新读取并校验审批状态、有效期和范围摘要。
- 拒绝、撤销、过期、摘要不匹配的任务不得启动 Claude 子进程，并记录明确事件。
- Worker 继续使用既有 configured 权限策略（`acceptEdits`、禁止 Bash/MCP）；绝不使用 `bypassPermissions` 或 `--dangerously-skip-permissions`。
- 任务取消和进程组终止逻辑不变。

## 分阶段交付

### 11.5.1 审批数据模型与 API
- 新增审批状态和持久化记录。
- 增加审批申请查询、批准、拒绝、撤销 API。
- 加入摘要绑定、过期、并发与取消竞态测试。
- 暂不开放 Agent 写执行工具。

### 11.5.2 Worker 强制门禁
- Worker claim 和启动前双重校验。
- 覆盖未审批、拒绝、过期、撤销、范围变更、重复批准、取消竞态。
- 验证未通过审批的任务不会启动任何 Claude 进程。

### 11.5.3 Master Agent 受控执行申请
- 增加项目绑定的 `request_execution` MCP 工具。
- 仅创建待审批请求，不提供批准工具。
- 强化中文系统提示词，要求先说明计划、风险、变更范围和验证方式。

### 11.5.4 中文控制台与操作体验
- Chat、Dashboard 的用户可见静态文案与状态默认简体中文。
- 增加审批卡片和明确的风险摘要。
- 状态/事件展示使用中文映射，原始事件类型与技术字段仍可查看。
- 增加语言回归测试，确保新增 UI 和 Agent 指令默认中文。

### 11.5.5 真实环境验收
- 自动化测试、E2E、多轮对话、MCP 请求审批。
- 未批准时确认 Worker 不启动；批准后仅在指定项目工作区内执行。
- 验证 Bubblewrap、任务产物、workspace diff、事件审计、取消和撤销。
- 用户在 Linux 独立 worktree 执行真实 Claude CLI + Bubblewrap 验收后，再考虑合并。

## 明确不在本阶段承诺的能力

- 不是远程多用户身份认证或 RBAC；当前 API 没有认证，生产网络部署前必须另行建设认证与 CSRF/访问控制。
- 不是任意 Shell / 主机级操作授权；Worker 仍禁止 Bash 和 MCP 工具。
- 不保证自动回滚所有文件系统副作用。变更审查、Git diff 与后续显式回滚机制应独立设计。
- 不把模型自述的“已修改/已测试”当成事实；必须以 Worker 状态、验证结果和工作区变更报告为准。

## 验收门槛

1. 未审批、已拒绝、已过期、已撤销、范围摘要不匹配的任务，Claude 进程启动次数必须为 0。
2. Master Agent 无法调用批准/拒绝/撤销能力。
3. 同一审批不能被并发重复批准产生多个执行；任务仍遵守既有唯一 claim/lease 语义。
4. 只有批准的任务能以 configured 模式执行，且权限参数不含 bypass。
5. 用户可查看申请、决策、执行和验证的完整事件链。
6. Chat 与 Dashboard 默认简体中文；关键 Agent 输出中文；技术原始信息可追溯。
7. 既有 plan-only 行为与全部 Phase 11.4 验收不回归。
