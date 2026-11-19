# Phase 11.6：Master Agent 持续工作编排设计

> 状态：设计稿（待评审）
> 基线：`main@f1728b76049789860ff19683974aafe5744fb44d`
> 目标：让 Master Agent 从“理解请求并创建任务”升级为“围绕用户目标持续组织、跟踪、验证并汇报工作”的项目工作中枢。
> 默认用户语言：简体中文（zh-CN）。

## 1. 背景与现状

Phase 11.5 已提供可信本机环境下的受控写入审批闭环：Master Agent 可以通过 `request_execution` 申请 configured 任务，但不能批准；Worker 只有在审批有效时才执行。当前 Master Agent 运行时按对话轮次执行 Claude Code，持久化消息、turn、事件和 CLI session；MCP 控制面可读取项目/任务/工作流，创建 plan 任务、plan 工作流及受控执行申请。

当前能力的主要断点不是缺少一个创建任务的工具，而是**任务创建之后，Master Agent 没有一个明确、持久、可恢复的目标级工作循环**。对用户而言，创建任务不等于推进目标；任务排队、等待审批、执行、失败、验证和最终交付需要被串成一个可追踪闭环。

## 2. 产品目标

用户用自然语言提出一个目标后，Master Agent 应能够：

1. 理解目标，识别项目范围、约束、完成定义和风险。
2. 在必要时提出少量关键澄清问题；信息充分时不反复询问。
3. 检查项目现状，给出中文计划、任务拆分、依赖关系和验收标准。
4. 根据用户意图创建只读分析任务/工作流，或申请受控写入审批。
5. 跟踪任务与工作流的真实持久化状态，而不是根据模型推测进度。
6. 识别阻塞、失败、审批等待和验证失败，并提出安全的下一步。
7. 在允许的范围内继续推进；涉及写入时始终遵守 Phase 11.5 审批门禁。
8. 汇总实际执行证据、变更文件、验证结果、遗留问题，给出可核验的最终报告。
9. 浏览器刷新、Master Agent 进程重启或用户稍后返回时，能够从持久化状态恢复上下文。

## 3. 非目标与安全边界

本阶段不做以下事情：

- 不提供远程访问、多用户身份认证、RBAC 或 SaaS 化部署。
- 不自动批准审批，不让 Master Agent 自我授权，也不绕过 Bubblewrap / Worker 门禁。
- 不开放 Bash、任意 Shell、任意 MCP、直接文件写入或宿主机访问给 Master Agent。
- 不把“任务已创建/已排队”描述为“已开始/已完成”。
- 不承诺自动回滚、完整文件系统审计或对不可信工作区的安全保障。
- 不引入无界自主循环；每次编排必须有预算、步数/时间上限和停止条件。
- 不把 token 级实时流、WebSocket 或大规模 UI 重构作为前提。

继续遵守现有边界：Master Agent 内置文件工具只读；普通任务/工作流为 plan-only；写任务通过 `request_execution` 申请；用户在本机 Dashboard 审批；Worker 使用既有 configured + `acceptEdits` 和 Bubblewrap 配置；绝不使用 `bypassPermissions` 或 `--dangerously-skip-permissions`。

## 4. 核心用户体验

### 4.1 用户提出目标

例如：

> 检查这个项目目前最重要的问题，按优先级制定修复计划，先完成前三项并验证。

Master Agent 不应只返回一段静态计划，也不应立即无条件创建大量任务。它应先读取项目状态，判断目标是否清晰，然后给出：
- 目标理解与边界；
- 发现/假设；
- 分阶段计划；
- 每项任务的交付物和验收标准；
- 依赖关系；
- 哪些步骤只读，哪些步骤需要写入审批；
- 预计的阻塞点。

对于明确要求“直接开始”的目标，Master Agent 可以在计划清晰后创建相应记录；涉及修改时只申请审批，不能代替用户批准。

### 4.2 目标工作面板

Chat 是主要交互入口。用户不应被迫离开聊天去手动维护任务列表。每个目标应能在对话中呈现一个简明的工作摘要：
- 目标状态：规划中、执行中、等待审批、受阻、验证中、已完成、失败、已取消；
- 当前阶段和下一步；
- 任务进度（成功/运行/排队/失败/阻塞计数）；
- 等待用户处理的事项；
- 最近活动与结果链接/标识。

现有 `/dashboard` 继续作为详细操作控制台，承担任务细节、执行日志、产物和审批审查；Chat 提供目标层面的解释和导航，不复制整套 Dashboard。

### 4.3 结束与继续

每次回复必须区分：
- 已完成：有持久化状态及验证证据；
- 正在进行：有运行中的任务/turn；
- 等待用户：例如待审批、缺少关键输入；
- 受阻：明确原因和可选恢复动作；
- 未完成：说明剩余工作。

如果需要用户批准，直接指出待审批任务及 Dashboard 入口。若用户稍后说“继续”，系统应从最近一个未完成的目标/编排状态恢复，而不是重新创建重复任务。

## 5. 建议架构

采用**持久化目标编排记录 + 现有 Conversation / AgentTurn + Workflow / Task / ExecutionApproval**，尽量复用现有数据模型和 Worker，不建立第二套任务执行系统。

### 5.1 MasterRun（目标编排记录）

建议新增 `master_runs` 表，作为一个用户目标在多个 AgentTurn 和多个 Task/Workflow 之间的稳定关联。字段建议：

| 字段 | 用途 |
|---|---|
| id | UUID 主键 |
| conversation_id | 所属对话 |
| project_id | 项目范围；必须与 conversation 项目绑定一致 |
| goal | 用户目标摘要（中文） |
| status | 目标生命周期状态 |
| plan | 当前结构化计划 JSON |
| current_step | 当前步骤/阶段标识 |
| workflow_ids | 关联工作流 ID 列表，或用关联表 |
| task_ids | 关联任务 ID 列表，或通过 workflow/专用关联表查询 |
| checkpoint | 可恢复检查点，不存密钥或不必要的原始 prompt |
| step_count / max_steps | 防止无界循环 |
| deadline_at | 可选总时限 |
| created_at / updated_at / finished_at | 生命周期时间 |

如果同一目标确实需要跨多个工作流，优先使用独立关联表 `master_run_tasks` / `master_run_workflows`，避免把关系塞进 JSON。所有关联必须校验 project_id 一致。

建议状态：
- `planning`
- `ready`
- `running`
- `waiting_approval`
- `waiting_input`
- `blocked`
- `verifying`
- `succeeded`
- `failed`
- `cancelled`

状态转换必须由服务端根据持久化任务/审批状态推进，不允许模型任意声明完成。

### 5.2 编排协调器

新增独立的 orchestration service（优先作为可测试的 Python 服务模块，由 Master Agent runtime 调用），负责：
- 创建/读取 MasterRun；
- 校验 conversation/project 绑定；
- 关联 workflow/task；
- 汇总任务与审批状态；
- 计算下一可执行阶段；
- 写入 checkpoint 和结构化事件；
- 应用最大步骤数、总时限和停止条件；
- 幂等处理重复的推进请求。

不建议第一版再增加一个常驻 daemon。先由 Master Agent turn 驱动协调器，状态持久化；当用户发送“继续”或系统恢复时，新的 turn 从 checkpoint 恢复。只有验收证明需要无人值守的自动唤醒后，才考虑增加调度进程。

### 5.3 MCP 工具面

保留现有工具并扩展最少必要接口：

| 工具 | 设计用途 | 权限 |
|---|---|---|
| `get_project_overview` | 项目与任务概览 | 只读 |
| `list_tasks` / `get_task` | 任务状态与详情 | 只读 |
| `list_workflows` / `get_workflow` | 工作流与依赖状态 | 只读 |
| `create_task` | 创建 plan 任务 | 只读执行 |
| `create_workflow` | 创建 plan DAG | 只读执行 |
| `request_execution` | 申请 configured 写入 | 仅申请，不批准 |
| `get_master_run` | 读取当前目标的持久化状态 | 只读 |
| `update_master_run` | 保存计划、checkpoint、关联记录 | 受限写入；只允许当前对话项目及当前 turn |
| `get_execution_evidence` | 汇总 task events、verification、artifacts、diff 摘要 | 只读 |

工具设计要求：
- 所有参数严格 schema 校验、大小限制和额外字段拒绝。
- 创建/更新类调用沿用当前 turn 级幂等请求记录；重复 request id 且参数不同必须拒绝。
- 目标/任务/工作流/审批必须绑定当前 conversation 的 project，禁止跨项目 ID 探测。
- `update_master_run` 只允许有限状态/计划/检查点更新，不允许模型直接把状态写成 `succeeded`；完成状态由协调器根据证据计算。
- `get_execution_evidence` 返回结构化、脱敏的证据摘要，不复制密钥、完整敏感 prompt 或任意文件内容。

## 6. 目标推进状态机

建议目标层面的服务端状态机：

1. `planning`：收集上下文、制定计划。
2. `ready`：计划结构完整、依赖有效，等待开始/或按用户明确指令开始。
3. `running`：至少一个关联任务正在执行，或协调器正在推进允许的只读阶段。
4. `waiting_approval`：存在待审批的写任务；不应把它显示成 Worker 正在执行。
5. `waiting_input`：确实需要用户回答或决策。
6. `blocked`：任务失败/依赖失败/执行环境问题导致无法继续。
7. `verifying`：执行任务结束，正在收集和判断验收证据。
8. 终态：`succeeded`、`failed`、`cancelled`。

状态优先级/汇总规则应明确：
- 任一必需写任务处于 pending approval → `waiting_approval`。
- 有运行任务 → `running`。
- 所有任务终态但尚未核验交付标准 → `verifying`。
- 必需任务失败且没有安全重试路径 → `blocked` 或 `failed`。
- 所有必需任务成功且验收标准均有证据支持 → `succeeded`。
- 用户取消目标时，先请求取消关联运行任务，再在确认取消/终态后完成 run 状态转换。
- 可选任务失败不应自动让整个目标失败，计划中必须标记必需/可选。

工作流依赖仍由既有 DAG 校验；依赖失败时下游不得被误报为已完成。不要仅依据 workflow.status 或模型文本判断成功，必须核对具体 Task 状态及 verification 状态。

## 7. 失败处理与恢复

将失败分类为可操作的类别：
- CLI/模型调用失败；
- Worker lease 过期或进程异常退出；
- Bubblewrap/路径/挂载预检失败；
- 等待审批、审批拒绝、撤销或过期；
- 验证命令失败；
- 任务依赖失败；
- 用户输入不足；
- 目标预算/时限耗尽。

策略：
- 不自动重复启动可能产生副作用的 Master Agent turn；遵守当前过期 turn 标记失败的语义。
- 不自动重新申请或重复创建 configured 写任务；先查询原任务和审批状态。
- 只有明确幂等且有安全边界的只读步骤可以自动重试，并有次数上限。
- 写任务失败后的重试必须创建新的、独立审批范围；不得复用旧审批。
- 验证失败应报告命令、退出码和简要输出；不允许模型把失败解释成成功。
- 恢复时先读取 MasterRun checkpoint 和关联实体，再决定下一步，避免重复执行。

## 8. 执行证据与最终报告

最终报告至少包括：
- 用户目标与实际完成范围；
- 每个任务的最终状态及 task ID；
- 实际修改文件列表（基于 Worker 已有的变更快照/产物记录）；
- 验证命令及通过/失败结果；
- 审批记录状态（申请/批准/拒绝/撤销/过期）；
- 未完成事项、风险和后续建议。

没有证据的内容必须标注“未验证”或“未知”。不将 Agent 自述当作唯一事实来源。报告应从持久化任务、事件、verification 和 artifact 数据生成。

## 9. UI 设计

### Chat（主界面）

在现有聊天消息和 turn 活动基础上增加目标工作摘要：
- 当前目标标题和状态；
- 计划阶段/步骤；
- 任务进度和等待用户事项；
- 关键事件（任务创建、审批申请、执行开始、验证完成、失败）；
- “继续”操作发送普通对话消息，不创建隐藏的重复执行；
- “查看任务/工作流”跳转现有 Dashboard 对应详情。

避免将长日志塞进 Chat；日志、完整 diff、审批细节留在 Dashboard。

### Dashboard（操作控制台）

保留现有布局，补充：
- MasterRun 过滤/详情；
- run 与 task/workflow 关联；
- 等待审批/阻塞/验证失败的明确状态；
- 从目标定位任务，从任务返回目标；
- 执行证据与最终报告入口。

所有用户可见文本继续使用简体中文；状态内部枚举可保持英文并映射中文。

## 10. 数据与迁移

- 新增表必须通过现有 `ensure_schema_compatibility()` 路径兼容旧 SQLite 数据库。
- 对旧数据库执行迁移时不得重置 conversations/tasks/workflows/execution_approvals。
- 外键和索引覆盖 conversation_id、project_id、status、task_id/workflow_id。
- 删除项目/归档项目的行为应遵循现有外键策略；不能留下跨项目关联。
- 新增事件 payload 限制大小并脱敏。
- 迁移在 API 首次启动完成，遵循 API → health → Worker → Master Agent 串行启动顺序。

## 11. 分阶段实现建议

尽管本阶段作为一个完整功能交付，内部按可独立测试的切片实现：

| 切片 | 交付 |
|---|---|
| 11.6.1 数据模型与协调器 | MasterRun、关联关系、状态机、checkpoint、迁移 |
| 11.6.2 MCP 编排工具 | get/update run、证据读取、项目/turn 绑定、幂等 |
| 11.6.3 持续推进循环 | Master Agent 根据任务真实状态规划下一步；有限步数/时限；可恢复 |
| 11.6.4 失败与审批恢复 | pending approval、拒绝/过期、Worker/CLI/verification 失败处理 |
| 11.6.5 Chat 目标进度体验 | 目标状态卡片、下一步、待用户处理事项、恢复入口 |
| 11.6.6 Dashboard 关联与证据 | run-task-workflow 导航、证据汇总、最终报告 |
| 11.6.7 集成验收 | 端到端场景、真实 CLI/Bubblewrap、升级兼容性、回归 |

## 12. 验收标准

### A. 目标理解与规划
- 中文自然语言目标能形成结构化计划、明确验收标准和任务依赖。
- 模糊但不关键的信息采用明确假设；缺少关键输入时只提出必要澄清。
- 用户要求“先分析/只规划”时不创建写任务、不申请审批。

### B. 多任务持续推进
- 创建包含依赖关系的工作流后，系统依据真实状态跟踪上游/下游。
- 上游成功后才能将下游视为可执行；上游失败时明确阻塞原因。
- Master Agent 不把 queued 当作 running，不把 task 创建当作完成。
- 一次用户目标可跨多个 AgentTurn 持续推进，且不重复创建任务。

### C. 审批安全
- 写任务只能通过 `request_execution` 申请；Master Agent 没有 approve/reject/revoke 工具。
- pending/rejected/revoked/expired 或 scope mismatch 的任务不得被 Worker 执行。
- 批准后仍由现有 Worker lease、审批二次检查和 Bubblewrap 执行。
- 绝不使用 bypass 权限；无审批时真实 Claude 子进程启动次数必须为 0。

### D. 失败与恢复
- 模拟 CLI 失败、Worker 中断、lease 过期、审批拒绝/过期、验证失败、取消。
- “继续”时读取 checkpoint 和实体状态，不重复启动已执行任务。
- 有界重试、预算耗尽和停止原因可见且可审计。

### E. 可追溯交付
- 最终报告中的任务状态、文件列表、验证结果与持久化记录一致。
- 缺少证据时明确标记未验证。
- Chat 刷新后目标进度和活动历史可恢复。
- Dashboard 可从目标定位到任务、审批、日志和产物。

### F. 兼容与回归
- 旧数据库迁移不丢数据。
- 现有对话、turn lease、MCP 幂等、DAG、审批、Worker cancellation、Bubblewrap fail-closed 测试全部回归。
- UI E2E 连续多轮通过。
- 真实 Claude Code CLI + Bubblewrap 受控写入闭环通过。
- 一键验收脚本最终退出码为 0。
- 验收期间不操作正在运行的线上服务；仅在合并后由用户决定升级。

## 13. 关键设计决策（建议）

1. 目标级状态必须持久化，不能只依赖 Claude session transcript。
2. 复用现有 Task/Workflow/ExecutionApproval，不建立第二个执行引擎。
3. 第一版采用 turn 驱动恢复，不新增常驻调度 daemon。
4. 自动化推进必须有明确预算、步数和时限。
5. 写操作的审批边界完全继承 11.5，Master Agent 永远不能自批。
6. Chat 保持主入口，Dashboard 保持详细控制台。
7. 只有状态与证据一致时才能宣布目标完成。

## 14. 开发开始前需锁定的默认值

建议初始默认值（均可配置，需在实现时验证合理性）：
- 单个 MasterRun 最多 20 次编排步骤；
- 单个目标总运行时限 4 小时；
- 自动只读重试最多 2 次；
- 单个 workflow 最多 30 个任务（沿用当前 MCP 上限）；
- 单个任务的验证命令最多 5 条（沿用当前审批约束）；
- 每次恢复先查询关联任务/审批，再决定是否产生新任务；
- 超出预算进入 `blocked`，要求用户明确继续或调整目标。

这些上限是防止失控的保护措施，不代表系统承诺在时限内完成任意规模的工作。
