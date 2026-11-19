# Phase 11.6 实施任务与验收矩阵

> 基线：`f1728b76049789860ff19683974aafe5744fb44d`
> 开发分支：`feat/phase-11-6-master-agent-orchestration`
> 设计依据：`docs/phase-11-6-design.md`
> 约束：开发期间不操作线上服务；不修改 main；不绕过 11.5 审批门禁。

## 实施前架构核对

已核对基线代码：
- `models.py` 已有 Project、Task、Workflow、Conversation、AgentTurn、AgentTurnEvent、ExecutionApproval 等实体。
- `db.py` 使用 additive `ensure_schema_compatibility()`，旧 SQLite 数据库通过新增表/字段迁移。
- `master_agent.py` 由持久化 AgentTurn 队列驱动，包含 lease、heartbeat、orphan recovery 和最大 turn 限制。
- `mcp_control_plane.py` 当前工具包括项目/任务/工作流读取、plan 任务/工作流创建和仅申请审批的 `request_execution`。
- `api.py` 提供 Chat、Dashboard、Conversation、Task、Workflow 和审批相关 API。
- 当前模型没有目标级 MasterRun 持久化实体；这是本阶段的主要数据层缺口。

## 开发任务

### 11.6.1 目标编排数据层 <状态：实现初版完成，CI 已通过>
- 新增 MasterRun、MasterRunTask、MasterRunWorkflow（或经实现审查后采用等价规范化关联模型）。
- 字段：conversation/project 作用域、goal、status、结构化 plan、checkpoint、step_count/max_steps、deadline、时间戳。
- 添加数据库约束、外键、索引和 additive 迁移；不得重建/清空现有数据。
- 服务层集中执行状态转换；禁止模型直接写终态成功。
- 测试跨项目关联拒绝、非法状态转换、并发/重复更新和旧库升级。

### 11.6.2 编排控制面 <状态：核心 MCP 工具与任务自动关联已实现，CI 验证中>
- 增加读取当前 MasterRun、受限保存计划/checkpoint、读取任务证据的 MCP 工具。
- 所有写调用绑定 conversation、project、turn，校验实体归属。
- 写工具具备 request-id 幂等保护；参数不一致时拒绝。
- 不新增任何审批批准/拒绝/撤销工具。
- 证据接口仅返回脱敏结构化摘要。

### 11.6.3 持续推进引擎 <状态：自动续跑调度初版已实现，CI 验证中>
- 将 turn 驱动逻辑与 orchestration service 解耦，Master Agent 在每轮开始/恢复时读取目标 checkpoint 和关联任务状态。
- 用户目标只创建一次 MasterRun；继续对话恢复同一 run，不能重复创建工作流。
- 按任务状态/依赖关系决定下一步；不将 queued 误报为 running。
- 支持只规划、分析、执行、继续等意图；不清楚是否授权写入时先澄清。
- 每次 turn 有最大推进步骤和 token/时间预算，耗尽后持久化 checkpoint 并停止。

### 11.6.4 失败、审批和恢复 <状态：目标取消、审批撤销、预算阻断/扩展初版已实现；完整故障注入待做>
- 统一识别 waiting_approval、waiting_input、blocked、verifying。
- 审批拒绝/撤销/过期不自动重新申请；需用户新指令。
- CLI/Worker/lease/verification 失败按类别记录，重试有上限。
- 恢复先查询现有 task/approval/event，再决定下一步，避免重复副作用。
- 取消目标要协调关联任务取消并等待其真实终态。

### 11.6.5 Chat 目标进度体验 <状态：目标进度卡片、刷新恢复与取消入口已实现，CI 验证中>
- 中文显示目标名称、状态、阶段、任务计数、待审批/待输入事项。
- 对话刷新后从 API 持久化状态恢复。
- “继续”只恢复当前 run，不创建重复 run。
- 状态文案严格映射真实持久化状态。

### 11.6.6 Dashboard 目标关联与交付证据
- MasterRun 过滤和详情。
- 目标与 Workflow/Task/Approval 互相导航。
- 证据摘要包含文件变更、验证结果、审批结果、错误摘要。
- 最终报告区分已验证、未验证、未知和未完成。

### 11.6.7 集成验收
- 单元/集成：模型、迁移、状态机、绑定校验、幂等、预算、恢复。
- API：目标创建/查询/恢复/取消/证据。
- UI E2E：中文、刷新恢复、继续、审批等待、失败状态。
- 真实 Claude Code + Bubblewrap：只读规划、多任务 DAG、受控写入申请、人工批准、Worker 执行、验证、最终证据。
- 故障注入：CLI 退出、Worker 崩溃/lease 过期、审批拒绝/撤销/过期、验证失败、取消。
- 回归 11.2–11.5 所有会话、MCP、DAG、审批、sandbox 和 worker 测试。
- 最终一键验收退出码 0；验收期间不操作线上服务。


## 当前已提交实现

- `MasterRun`、`MasterRunTask`、`MasterRunWorkflow` 数据模型及服务层。
- 目标创建/恢复、计划与 checkpoint 保存、状态转换、步数/时限预算和跨项目关联保护。
- MCP 工具 `start_goal`、`get_goal`、`update_goal`；目标写入要求 JSON-RPC request id，并记录幂等调用。
- 目标活动期间通过 MCP 创建的 Task/Workflow 自动关联到 MasterRun；plan 任务支持显式 verification commands。
- `get_goal` 提供按任务状态和审批状态计算的 `derived_status`，包括审批拒绝/撤销/过期阻塞。
- `GET /conversations/{conversation_id}/master-run` 提供 Chat 刷新恢复所需的目标、任务和进度摘要，并可读取最近一个已结束目标；进度只将验证通过的任务计入完成数。
- Master Agent 主循环自动检测必需任务的终态变化，按任务状态签名排入隐藏续跑轮次；审批待处理/拒绝/撤销/过期、等待用户输入和预算耗尽均阻止自动续跑。
- 目标支持显式取消：取消排队任务、请求停止运行任务、撤销未开始执行的审批；Chat 提供二次确认的取消入口。
- 支持用户明确授权后扩展目标步骤/时限预算；系统不会自行放大预算。
- DAG 子任务支持配置验证命令；目标成功必须经过 MasterRun 终态转换，并有全部必需任务的验证通过证据。
- 已新增目标状态、MCP 生命周期、幂等 checkpoint、自动续跑/审批门禁/预算停止、取消流程、Chat 恢复与 API 端点测试；新提交的 CI 仍在运行，尚未把本阶段整体标记为验收通过。

## 验收矩阵

| 场景 | 关键断言 |
|---|---|
| 只规划 | 不创建 configured 写任务，不产生审批申请 |
| 明确要求执行 | 写任务只能创建 pending approval，不能自行批准 |
| 目标跨多个 turn | run id 稳定，任务不重复创建 |
| DAG 执行 | 依赖上游真实成功后下游才推进 |
| 等待审批 | run 显示等待用户，不谎报 Worker 执行 |
| 拒绝/撤销/过期 | run 可恢复/阻塞；无未经审批的子进程 |
| Worker/CLI 故障 | 错误分类、checkpoint 持久化、无重复副作用 |
| 验证失败 | 不得宣布目标成功 |
| 证据完整 | 文件、验证、审批、任务状态与数据库一致 |
| 刷新/重启恢复 | 进度从数据库恢复，不依赖前端内存 |
| 预算耗尽 | 明确停止原因，可由用户继续或调整 |
| 旧数据库迁移 | 原对话/任务/审批数据完整保留 |

## 默认保护预算

- 每个 MasterRun 最多 20 个推进步骤。
- 每个目标最长 4 小时（配置可调）。
- 只读自动重试最多 2 次。
- Workflow 任务数上限沿用 MCP 30。
- 单次 AgentTurn 沿用当前 turn timeout / lease 机制。
- 任一写任务必须走 Phase 11.5 审批，不得自动批准。
