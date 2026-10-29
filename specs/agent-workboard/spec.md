# LivingRuntime Remote · Agent Workboard 产品 Spec v0.1

> **状态：DRAFT — 待产品审议；禁止以本稿代表已批准实施。**
>
> 日期：2026-10-27  
> 仓库：`jboone1989/livingruntime-remote`  
> 类型：统一产品 Spec（目标、产品体验、架构边界、分期和验收）；不是代码实现 PR  
> 前置讨论：Owner 与 ChatGPT、独立 Claude Opus 的多轮产品/反方审查  
> 关联现有规划：[Web Control Plane](../../docs/WEB_CONTROL_PLANE.md)、[Host Connector Architecture](../../docs/HOST_CONNECTOR_ARCHITECTURE.md)、[Repository Boundary](../../docs/REPOSITORY_BOUNDARY.md)

## 0. 一句话定义与决定

**LivingRuntime Remote 是连接用户、自有计算资源和异构 Agent 的持续协作基础设施。**

用户把长期目标发出去，指定可参与的服务器、Agent、权限和预算；多个 Agent 从同一工作池安全领取任务，独立工作自动并行，有依赖的工作自动接力；只有确实需要新授权、凭据或产品决策时，才进入“待我处理”；完成结果、证据和未读回执进入收件箱。用户离开 ChatGPT 不使执行停顿。

**不在“任务收件箱”和“跨 Agent 任务派发”中二选一。** 它们是同一系统的不同视图和工作阶段；基础连接、Agent 生命周期和部署体验是支撑层。

### 已经确定的产品原则

1. **自动推进是默认行为，人工介入是例外**，但绝不越过用户授予的权限、预算、验收或现有 PR/CI 门禁。
2. **人不是心跳器**：用户不回复“继续”不应导致还有合规且就绪的任务原地不动。
3. **执行生命周期独立于会话生命周期**：ChatGPT、Web 或 MCP 客户端离线，不影响服务器上现有 Agent 自主循环。
4. **供应商中立**：用户自有 VPS、台式机、NAS；可用 Pi、Claude Code、Codex 和未来的 Adapter，不绑定特定云厂商、模型或客户端。
5. **共享任务权威状态唯一，外部需求可引用**：Remote 管目标/工作项身份、执行状态和审计；GitHub Issue、Spec、文档等可做需求源，不强制所有任务都在 GitHub。
6. **先复用现有 Core、系统服务和上游组件**，不额外研发第二套 Agent 大脑、通用 PaaS、CI 引擎或复杂编排器。
7. **证明工作完成，而不只是“模型回复完成”**：结果必须满足该项事先确定的验收规则和证据要求。
8. **按真实价值分期**：先修阻断和实现一条持续接力闭环，再补完整操作界面和首次安装体验。

## 1. 用户问题、非目标与市场假设

### 1.1 用户场景

- **持续开发**：例如 [EventGraph 自动续办队列 #176](https://github.com/jboone1989/EventGraph/issues/176)，有多个 Spec、独立任务、PR、CI 和 Claude review；执行者完成一项后不得因 ChatGPT 结束本轮回复而停住，独立任务允许并行。
- **长期内容生产**：Ferro 能领取研究、发布、检查任务；其内容策略、认知和学习仍由 Ferro 负责，不迁进 Remote。
- **个人运维/研究**：不同服务器上的工具/Agent 执行可核查的检查、分析、测试和服务管理；只有权限、真实外部依赖或需要主观决定时才让人介入。
- **跨设备监督**：用户从 ChatGPT 发出目标，之后在 Web 或另一个 MCP 客户端查看同一事实、批准一个例外、修改后续目标或暂停整个工作。

### 1.2 核心问题

当前常见流程是“用户下目标 → Agent 完成一步 → 回复 → 等用户说继续”。当已有大量就绪工作时，人的反馈频率成为不必要瓶颈。单纯收件箱解决“回来时我知道发生了什么”，但**不解决谁自动接下一项**；单纯任务派发则难以让用户看清委托的整体进展及真正需要处理的问题。

### 1.3 重要反对意见与未获验证假设

- 本项目已观察到用户自身的真实痛点，但**没有独立外部用户的长期留存或付费需求证据**。不能把内部使用等同于 PMF。
- 更多 Agent 并行可能增加 PR 冲突、评审和验收负担；若瓶颈转移到 CI、审查、合并、部署，派发速度提高未必提高最终交付速度。
- 对开放式设计问题不能假设 CI 永远能代替人判断；必须明确可自动验收与需人决策的边界。
- 各 Agent 的原生工具权限可能绕开 Remote 自己的 exec 审批；不能以 Remote 的命令白名单冒充对被委托 Agent 的完整沙箱。
- ChatGPT 主动事件订阅不是确定可用的连续推理机制；Work-host 订阅暂不构成自主循环前提。

**成功指标看“经验证的有效交付”和人类非必要介入次数，不看创建任务数、Agent 对话数或仅仅有 RUNNING 状态。**

## 2. 产品结构与用户体验

同一 Goal 的四个主视图，不是四套重复存储：

| 视图 | 用户要回答的问题 | 主要对象与操作 |
| --- | --- | --- |
| **发件箱 / Outbox** | 我委托了什么？授权到哪一步？ | Goal、约束、验收、允许的 Agent/服务器、预算、整体进度；新增、暂停、修改后继工作、取消 |
| **共享任务表 / Workboard** | 现在谁在做哪项？下一项是谁？ | WorkItem、依赖、可领取/执行/验证、当前占用、Agent、主机、执行轨迹、并行状态 |
| **待我处理 / Human Actions** | 哪件事确实需要我？ | 审批/授权、现场输入受保护凭证、产品决策、真实外部阻塞；批准、拒绝、补充信息、恢复原工作项 |
| **收件箱 / Inbox** | 有什么已经完成、失败或值得看？ | 完成回执、实测证据、PR/部署链接、差异报告、终态异常、可继续引导的任务历史；阅读、确认、重新打开 |

### 2.1 主流程

```text
用户在 ChatGPT / Web / MCP 中提交 Goal
  -> Outbox 保存目标、授权、完成定义、范围
  -> 引用已有 Spec/Issue；必要时 controller Agent 提议有限子任务
  -> Workboard 计算依赖已满足的 READY WorkItem
  -> 允许的 Connector/Agent 原子认领任务
  -> Agent 在其服务器运行原生循环、记录检查点
  -> 证据满足 WorkItem 验收 -> 自动释放依赖后继项
  -> 结果/回执进 Inbox；Agent 立即认领下一项
  `->` 需要用户处理时，仅该任务进入 Human Actions
  -> 用户做必要动作 -> 同一个 WorkItem 恢复排队/执行
  -> 无 READY 且全部 Goal 验收完成 -> Goal 完成、停止领取
```

- **ChatGPT 不是常驻调度器**：它可创建目标、审议、干预、读取回执；真正的领取、续办由 Relay + Connector 的持久服务完成。没有 ChatGPT MCP Events 订阅，仍能运行；主动推送/模型回调属于可选增强。
- **没有人回应时**：只要还有已授权、可验收、无阻塞的 WorkItem，执行不停止。
- **只有一个项 BLOCKED**：不冻结其他独立项；受其依赖及同一独占资源约束的项正常等待。
- **离线与断网**：ChatGPT 离线无影响；Connector 到 Relay 断线时，已启动的原生执行可继续，但不领取新项；若原租约可能过期，写入共享状态和发生不可逆外部副作用前必须重新确认权限/租约或暂停该副作用。

### 2.2 统一操作台与安装体验

- 复用现有 MCP Apps Control Plane 和 Relay 授权接口；Web/PWA 是**相同权威数据的薄客户端**，不得维护平行任务状态。
- 首屏按 Goal 分组展示：进行中、待人处理、最近成果、Agent/服务器健康度；再进入任务依赖、时间线、日志、审批、证据。
- 支持从任意客户端按目标/项目/服务器/Agent 筛选和跳转到同一 WorkItem。
- **安装和生命周期同属产品范围**：先把 Connector 配对、Agent 发现、启动/继续/取消、服务重启/健康/升级的一条现有路径做可靠；之后用用户首用数据决定是否提供幂等的 Pi/GLM 快速安装模板。
- 不集成所有云厂商的 VPS 购买/计费；不直接管理任意云 API。机器由用户自行购买，按既有跨平台安装程序配对。

## 3. 领域模型、权限与状态

### 3.1 Goal：用户长期授权边界

最小字段：

- `goal_id`、`owner_id`、`project_id`、`title`、`intent`、`created_at`。
- `acceptance_ref` / 固定的完成标准快照；`source_ref` 可选，支持 GitHub/文件/URL/内部文档。
- `allowed_devices[]`、`allowed_agent_adapters[]`、`allowed_projects[]`、`allowed_actions`、`approval_policy`。
- `limits`：并发、时间、调用费用或 token、总尝试次数及子任务扩展上限；缺少可靠成本信息时不能假装已正确计费，应先按时间、并发和尝试次数限制。
- `paused`、`cancel_requested`；总体进度/终态**根据相关 WorkItem 和验收证据推导**，不得再造一份可任意手工篡改的真相。

### 3.2 WorkItem：可领取的工作单位

至少包含：

- `work_item_id`、`goal_id`、`parent_id?`、`idempotency_key`、`title`、`kind`、`created_at`。
- `source_ref: { system, uri, revision }`；`acceptance_ref` 或固定的验收快照；内部原生任务也可使用不可变快照，不依赖 GitHub。
- `depends_on[]`；`priority`、`capabilities_required[]`、`eligible_agents[]` / `eligible_devices[]`。
- `exclusive_key`：工作区/仓库/分支/部署环境等不可并行争抢的资源标识；是否互斥由任务类型确定，不机械地对全项目串行。
- `status`、`attempt`、`lease_owner`、`lease_until`、单调递增 `fence`；执行引用 `runtime_job_id` / `agent_session_id`。
- `evidence_refs[]`、`human_action_ids[]`、`last_checkpoint`、`blocked_reason`、`next_action`。

需求源（Spec/Issue/稿件）与执行状态分开管理。远端可编辑需求必须在认领前冻结指定 revision / 内容哈希；需求变化产生新 revision，避免无人注意地改变执行中的验收标准。对外平台的状态回写应避免双向编辑冲突，优先写可追溯的评论或引用。

### 3.3 状态机

| 状态 | 含义 | 合法后继（完整集合） |
| --- | --- | --- |
| `PROPOSED` | Agent 提议的子工作，尚未被当前 Goal 授权 | READY / WAITING / REJECTED / CANCELLED |
| `WAITING` | 等待依赖、外部 CI / review、Goal 暂停或指定时间；不得虚称 RUNNING | READY / VERIFYING（已有执行证据、只待验收结果）/ BLOCKED_HUMAN / FAILED / CANCELLED |
| `READY` | 依赖已满足且已有授权，可领取 | CLAIMED / WAITING / BLOCKED_HUMAN / FAILED / CANCELLED |
| `CLAIMED` | 已原子授予某 worker 有限租约 | RUNNING / READY（租约失效）/ WAITING（暂停）/ BLOCKED_HUMAN / FAILED / CANCELLED |
| `RUNNING` | 存在实际在运行的 Agent/进程证据 | VERIFYING / WAITING（安全暂停）/ BLOCKED_HUMAN / RETRY_WAIT / FAILED / CANCELLED |
| `VERIFYING` | 正等待已定义的可验证证据 | SUCCEEDED / READY（需修复）/ WAITING / BLOCKED_HUMAN / FAILED / CANCELLED |
| `BLOCKED_HUMAN` | 只等待用户不可替代的决定/授权 | READY / WAITING（依赖未满足）/ FAILED / CANCELLED |
| `RETRY_WAIT` | 短暂故障，按有界重试策略等待 | READY / WAITING（暂停）/ FAILED / CANCELLED |
| `SUCCEEDED`, `FAILED`, `CANCELLED`, `REJECTED` | 终态；用户可新建后续项，但不可假装原项仍执行 | 无 |

**初始态**：用户直接创建且已授权的 WorkItem，依赖满足则为 `READY`，否则为 `WAITING`；只有 Agent 自行提出的子项进入 `PROPOSED`。Goal `paused=true` 时禁止新领取，`CLAIMED/RUNNING` 经安全停止和检查点进入 `WAITING`（暂停原因），恢复后依依赖状态回到 `READY`；`cancel_requested` 必须先停止/确认进程与外部副作用边界，再转 `CANCELLED`，不能仅修改状态字符串。所有状态转移通过原子条件更新与审计事件记录。

**就绪与验收边界**：`READY/CLAIMED` 发现预算耗尽、能力不覆盖或需求冲突时，按已授权策略转 `WAITING`（有可恢复条件）、`BLOCKED_HUMAN`（确需新决定）或 `FAILED`（终止），不得无原因停在可领取态。`VERIFYING → WAITING → VERIFYING` 仅用于执行已结束、等待外部 CI/review/证据的情况；外部结果到达后直接恢复验收，**不重新认领或重跑已完成的副作用**。只有确需修复时才由 `VERIFYING → READY` 发起新执行尝试。

状态表示 WorkItem 的生命周期，不等于 Agent Session 状态，也不等于收件箱回执的 `PENDING/ACKED`。例如原生 Agent 已结束但 CI 未通过时 WorkItem 仍可在 VERIFYING/WAITING；完成回执未被用户 ACK 不会阻塞其他工作。

### 3.4 HumanAction

`human_action_id`、`work_item_id`、`type`（批准受限命令/补足凭证/决策/外部不可代办步骤）、`request_scope`、`requested_by`、`context_ref`、**`attempt`、`fence`、`source_revision`（绑定 WorkItem `source_ref.revision` 或不可变内容哈希）**、`status`、`created_at`、`decided_at`、`resolution_ref`。

- 只允许真正无法由当前授权和已配置能力完成的工作进入该列表；缺少权限不等于默许突破权限。
- 一次明确操作（批准/拒绝/完成配置）后从**原 WorkItem**恢复，保留证据、历史和身份，不重复创建目标。
- HumanAction 决策必须原子核对请求记录的 `attempt + fence + source_revision` 与当前 WorkItem；任一值已变化（例如租约重领、需求更新）则拒绝旧批准，标记该请求过期，并基于最新快照重新发起必要的人工请求。旧批准**不得**授权新尝试或新版本的外部副作用。
- **不在网页、ChatGPT 或任务正文收集原始服务器密码/API Key**；通过用户自有机器上的受控凭证配置和现有 opaque handle/lease 机制来完成授权。
- 人工决策可拒绝；拒绝后依既定策略取消、改派、缩减目标或终止受影响的项，不要求用户不停点击“继续”。

## 4. 最小跨服务器自动接力协议

### 4.1 派发与幂等

1. 由每台在线 Connector 上的持久 Agent Worker 主动向 Relay 拉取可认领项；Relay **在同一个事务中**检查 owner/project 权限、依赖、Agent 能力、独占资源、并发和预算。
2. 事务授予 `lease_owner`、`lease_until`，每次重新认领递增 `fence`。首版可复用现有 Relay SQLite 事务（`BEGIN IMMEDIATE`）；若出现真实多 Relay/高并发瓶颈再评估 Postgres 行级锁/成熟编排产品。
3. Worker 周期心跳，绑定实际 Agent Session 活性与租约；心跳续期、进度写入、检查点和提交完成必须带有效 `work_item_id + fence`。
4. 超时或进程失联时由后台核对后释放/重派；旧 Agent 的过期提交一律拒绝。不能仅靠延时未更新认定执行失败，应以 session/process 事实纠正状态。
5. 对 Git push、发帖、部署、开 PR 等外部副作用，仅 `fence` 不够：还需在执行副作用前校验授权/租约，并使用可核验的幂等标识和已存在资源查询。**只承诺至少一次尝试 + 去重及幂等补偿，绝不承诺天然 exactly-once 外部副作用。**
6. Item 真正 `SUCCEEDED` 才释放依赖后继工作；若前驱最终 `FAILED`/`CANCELLED`/`REJECTED`，其尚未开始的依赖项留在 `WAITING`，按 Goal 既定策略转 `BLOCKED_HUMAN` 请求取消/改派/替代依赖决定，或自动 `CANCELLED`（若策略已授权）；**绝不自动视作依赖满足，也不无限等待而无原因**。完成一项后 Connector 立即尝试下一项（可有短退避），不等每小时自动任务唤醒 ChatGPT。
7. 多个 Agent/子 Agent 并行的前提：不同 WorkItem 或受控子任务，互斥资源无冲突；同一 Git worktree 不允许多个写入 Agent 无约束并发；PR 通过现有 CI、review、分支保护/merge queue 处理最终集成。

建议 MVP 初值（均为待验证配置，不是硬编码架构）：每 30 秒 heartbeat，lease 180 秒，每设备并发上限 2，每 WorkItem 自动短暂重试不超过 2 次。先基于真故障注入调整，不扩大范围。

### 4.2 工作拆分与 Agent 子任务

- 自治 Agent 可基于 Goal 和真实发现**提议**子项；子项必须具备来源/目的、验收、依赖、预算与相同或更窄的权限，严禁自动扩大原目标授权。
- MVP 限一层自动子项、每项最多 5 个提议；可按预先授权的可自动审核策略将 PROPOSED 转 READY，超出边界则进入待人处理。默认不允许无限自我复制、无限层级递归和重复提同一工作。
- 拆分/推理归原生 Controller/Agent；确定性“哪个 READY item 应由哪个 Agent 领取”归 Workboard，避免每次领取都调用模型。
- 子 Agent 与父项共享预算总额，单个子项又有自己上限；分工不等于绕开独立 review 和权限门禁。

### 4.3 被阻塞后自动继续

- 用户侧：授权/提交所需本机凭证/回答产品问题时，恢复**同一任务链**；后继项仍依赖原 WorkItem 的验收结果。
- 无需用户的阻塞：网络抖动、临时 provider 故障、CI 等待、Agent 繁忙，用有界重试/等待/外部事件处理，不混进“待我处理”。
- 超预算、无能力覆盖、任务需求冲突、持续失败时显式终止或请求人类改变授权，不能无限空转。
- 用户可以随时暂停 Goal；暂停后停止新领取、对已有 RUNNING 任务实行约定的优雅停止/安全收尾；不可任意取消 GitHub 上已经触发的外部 CI 或修改用户未授权的生产环境。

## 5. 职责边界（防止变成第二个 Agent Runtime）

| 组件 | 应负责 | 明确不负责 |
| --- | --- | --- |
| Remote Core / Relay | OAuth、配对、多设备路由、共享 Goal/WorkItem 状态、依赖与租约、授权/审批、执行回执、控制台数据 API | 模型推理、规划全部任务、实现项目业务、私钥明文存储 |
| Connector | 本机驻留 Worker、Agent 发现/会话管理、Agent 活性/资源监测、lease 心跳、受控工具与凭证执行 | 重新造一套大模型执行循环 |
| Agent Adapter | 映射 Pi、Claude、Codex、HTTP Agent 的 start/status/events/send/cancel/resume 与能力标识 | 把一家厂商的内部模型/tool API 强行标准化成另一家 |
| Pi / Claude / Codex | 原生推理、代码/研究/测试工具调用、按工作项目执行和生成证据 | 替其他 Agent 私自修改共享权限和全局状态 |
| Ferro / 垂直 Agent | 身份、长期策略、内容生产、成果学习、领域业务 | 替所有其他项目提供通用跨主机协调 |
| EventGraph / GitHub / CI | Spec、PR、独立代码审查、CI、部署版本事实及各自业务验收 | 成为 Remote 跨行业/跨 Agent 的唯一任务数据库 |

**一个应用只能有一个权威任务分配者。** 在 EventGraph 的 #176 接管试点中，必须先影子验证共享队列；正式切换后停用旧的“每小时再次启动同一队列”派发路径，避免两个系统重复启动同一任务。只有完成切换验收才切掉旧路径；本 Spec 不直接改变现有自动任务。

## 6. 既有实现、必须修复的已知缺口

优先复用已存在的 Relay OAuth/配对、Connector、多 Agent Adapter、`plugin/scripts/jobs.py` 的 checkpoint/回执、`agent_sessions.py` 的会话包装、权限/凭证机制、MCP Apps 控制台。

**现状不是完整 Workboard：**
- `plugin/scripts/jobs.py` 的持久 Job 主要是每机本地 JSON；未提供跨机器 READY Item 的原子认领、依赖、lease/fence。
- Relay 已有设备 RPC 队列，不等于通用可恢复的共享工作队列。
- [Remote Issue #56](https://github.com/jboone1989/livingruntime-remote/issues/56) 记录真实 Pi 开发成功，也记录当时的 Session RUNNING / Job PENDING、Relay allowlist、取消续办、在运行中插话、GitHub 凭证、model provenance、参数 schema 问题；需核对当前版本后逐项关账，不能假设仍全部存在。
- [Remote Issue #10](https://github.com/jboone1989/livingruntime-remote/issues/10) 记录主动事件订阅端到端未通过；MCP Events/Work 主机自动唤醒属增强项，不是执行回路的依赖。

**P0 先消除阻断：**
1. Agent Session、Goal/WorkItem、进程观察和回执之间建立正确状态映射不变式：**不是要求状态字符串字面一致**，而是运行活性可信、终态回执不丢、等待和人工阻塞不伪装成 RUNNING。
2. 运行中的指令有可预期的处理方式（可排队至安全点或受控中断/恢复）；取消/恢复语义和各 Adapter 参数 schema 明确。
3. Relay 工具可见性与实际授权一致，避免“宣称可用但调用返回 not allowlisted”；可核实地展示 Agent/provider/model/usage（若上游未给数据就标未知）。
4. 明确原生 Agent 的权限边界：哪些工具由 Remote 审批、哪些直接由 Agent 运行，分别如何限制。
5. 任务回执与授权流程在断线、重新连接和不同客户端查看时保持真实，不重复 ACK 和不丢来源引用。

## 7. 分期与明确不做项

### Phase 0 — 修正可靠性基线（先于自动并行）

修 #56 所确认且仍存在的阻断；状态不变式、真实进程证据、回执、控制台视图一致；20 个真实 Agent 任务覆盖运行中调整、停止、恢复、断线等，违反状态/回执协议次数为 0。此为工程准入烟雾测试，不足以证明统计可靠性。

**退出产物：** 现有 Remote 用户单 Agent 完整委托—执行—验收—回执闭环可靠，已知阻断明确关账。

### Phase 1 — 可离开的共享工作闭环（MVP）

- Relay 内新增最小共享 `Goal/WorkItem/WorkClaim/HumanAction` 存储和原子领取/续租/终态提交协议；复用已有用户/设备身份。
- **每台 Connector 运行一个持久 Worker；Phase 1 实测至少两台在线 Connector** 共同向同一个 Relay 原子领取。任一 Worker 能在原生 Agent 完成后自动领取下一项；同一 Item 不被两台主机错误同时认领。
- 最小依赖与 exclusive_key、至少两个设备/两种 Agent 的任务分工、人工阻塞隔离、同一 Item 恢复。
- 利用现有 MCP 控制台展示 Outbox / Workboard / Human Actions / Inbox 的最小有效界面；只增必需的数据能力，不先造大型 SaaS/PWA。
- 外部源（例如 GitHub Issue）先**单向导入版本引用、只追加运行证据**；不做完整双向 issue 同步。
- 模型推理只发生在 VPS 上的原生 Agent；ChatGPT 离线时确认可正常推进。

**退出产物：** EventGraph #176 的 4 Item 真人离线验收通过（见 §8）。

### Phase 2 — 可用产品和 Agent 安装体验

- 跨客户端同一任务收件箱、可操作的待人工列表、移动端读写、Agent/主机状态筛选、历史和成本归因。
- 首次安装全过程观测；如果 Agent/provider/policy 配置构成主要瓶颈，提供**一个**幂等 Pi 安装简化（复用官方安装、systemd 或 Docker Compose），再依据使用数据扩展。
- 高级动态 Agent 分工、多层级依赖、可选通知、主动回调、自定义模板按验收证据排期，而非全部默认进入 MVP。

### 现在不做

- 对接各云厂商购买/账单 API、云服务器商城、通用 K8s/PaaS、独立多租户收费 SaaS。
- 第二套 Agent 推理循环、完整通用 workflow DSL、自研容器编排、重做 GitHub CI / 分支保护 / merge queue。
- 无限自动分解、自我复制、大规模并行、无边界的自动发布或自动付款。
- 为了美化控制台而维护一套与后端不同步的任务状态。
- 将网页输入原始凭证作为“点击就继续”的实现。
- 在验证外部使用需求前因单用户需求扩展为复杂的分布式编排集群。

## 8. 验收：使用 EventGraph #176 做真实切片

### 8.1 场景及准备

选取 #176 中 **4 个可核实的工作项**：一条具有前置依赖的双项链，加两个独立项；只选择当前尚未被 Agent 认领和实际未完成的内容，**先检查 GitHub 最新状态和现有 Session，避免重复开工**。开发可交给 cn-dev/Pi，其他允许的主机适配的 Codex/Claude 负责独立测试/审查，实际部署方案以可用性和项目授权为准。不同开发项采用互不冲突的工作区/分支/PR。

先以影子模式对照 #176 的原自动续办，确认一致后切换唯一派发者；保留 GitHub Issue、Spec 与原 PR 作为外部证据和需求出处。

### 8.2 产品硬性验收（必须同时满足）

| 类别 | 可观察场景 | 准入标准 |
| --- | --- | --- |
| 自动接力 | ChatGPT 不在线至少 30 分钟；A 项经自动验收成功后 B 项 READY | 不需用户回复“继续”，B 自动领取；READY→CLAIMED 中位时间 ≤ 2 分钟 |
| 真并发与阻塞隔离 | ≥2 台在线 Connector / ≥2 种原生 Agent；一项人工阻塞，另两项独立可执行 | BLOCKED 项不阻止独立项；不争同一 exclusive_key；有真实 Agent Session/证据 |
| 故障与租约 | 至少 20 次可重复故障注入：杀 worker、断网超过 lease、重连、过期写回 | 过期 fence 写回 100% 被拒；测试内无重复创建相同 PR/副作用；无无证据的长期虚假 RUNNING |
| 人工处理 | 创建一次真正的新权限或产品决定请求 | Human Actions 显示原因与范围；用户一次操作后恢复原 WorkItem，不重建目标；未经授权不执行 |
| 交付闭环 | 工作从开发到 PR/CI/review/需要时的部署和浏览器验收 | 未满足原始 acceptance 不得标 SUCCEEDED；证据含来源 revision、Session、提交/PR 和时间 |
| 用户视图 | 两个不同客户端读同一 Goal | 发件箱、工作板、待人工、收件箱状态一致；回执未 ACK 前可追溯；不能把“通知送达”当“用户看过” |

注意：“故障重复领取为 0”不是正确指标，因为租约超时后的**安全重领**是必需的；指标应是同一时刻有效租约不冲突、旧 fence 被拒和外部副作用去重。20 次故障试验只是首轮准入，不宣称工业级可用性。

### 8.3 用户采用/产品价值试验

- 找至少 5 名无项目开发背景或不是作者的目标用户，测成功配对、首次委托、一次自动接力、一次人工介入所用时间和卡点。
- 第 3–4 周衡量每周 ≥3 次真实委托的回访用户、至少两类 Agent/服务器组合的实际需要、首次价值中位耗时和每项人工无谓确认次数。
- 若外部用户只使用一家厂商原生 Remote Control、无需共享任务，或并行导致验收返工与冲突时间超过节省的等待时间，则停止扩展通用平台方向，优先修已有体验和稳定性。
- 只有首次安装实测显示配置 Agent/模型成为主要阻塞，才把“批量安装模板”提前。

## 9. 非功能约束与观测指标

- **真实性**：RUNNING 必须有对应活性证据；离线显示 OFFLINE / UNKNOWN；WAITING、BLOCKED 和 DONE 不能混用。
- **持久性**：Goal/WorkItem 及关键事件在 Relay 重启后可恢复；本机 Agent 运行事实来源保留；Job result/回执与 WorkItem 引用不可丢。
- **权限**：所有 claim、心跳、修改和确认操作按 owner + project + device + Agent scopes 校验；代理/子任务继承或缩小授权；操作可审计。
- **效率**：主要衡量就绪等待时间、从目标到真实验收耗时、Agent 有效工作时间、无谓人工干预次数、冲突返工率、单位有效交付成本。
- **成本**：先利用现有 Relay、SQLite 和用户已有机器；新组件需经“可否复用”审查，低资源成本优先。
- **兼容性**：存量 direct Remote 工具、现有 Agent Adapter 和已经持久化的 Job 不应因为试验 Workboard 而失效；不用未被使用的永久兼容层掩盖错误。
- **系统升级**：保持 Connector 与控制面的版本/能力协商和可观察性；不把任意新 Agent 软件版本锁死在 Remote。

## 10. 实施前必须明确的工程决策

在写实施 PR 之前，以独立 Spec/ADR 或本 Spec 的批准版冻结以下选择，不能边写代码边扩大范围：

1. WorkItem 的事务存储落点：**首选现有 Relay SQLite 单实例**，用短事务完成 claim/fence；明确 SQLite WAL 是单写者，断网不能领取新任务；如发现并发和可靠性瓶颈再评估 Postgres / Temporal / Hatchet。
2. 与 `plugin/scripts/jobs.py` 本地 Job 的关系：**共享 WorkItem 负责全局派发和授权，本地 Job 负责已启动执行与可恢复回执**；要求一一可追溯，不迁移和复制既有历史记录。
3. 原生 Agent 的权限事实：按 Adapter 实际工具执行路径证明权限边界；不要声称 Remote 的 exec 审批覆盖 Claude/Codex/Pi 的所有原生工具。
4. Agent 结束、CI/评审完成、WorkItem 验收和 Goal 终态的职责归属及映射规则；明确父子状态不要求字面相同。
5. 与现有 EventGraph hourly 自动续办任务的切换协议：**只能有一个自动派发权威**，先影子验证，后切换。
6. 同一仓库/工作区的独占粒度、并行 PR 的分支策略及 PR/部署副作用去重；拒绝“自动并行 = 任意多个 Agent 写同一工作区”。
7. 改需求和新子任务的权限边界：来源 revision 冻结、子项预算继承、由哪些规则自动授权，哪些进入人工列表。

## 11. 验收测试清单（供 Spec-first / CI）

- **正例**：目标已授权；两独立 READY item 不同主机原子认领；依赖解除自动派发；人工阻塞一项其他项继续；断线后租约合法重领；同一项人工批准后恢复；两个控制端看到同一回执；完成满足 acceptance。
- **反例**：不在允许范围内的 Agent 认领；不符合依赖直接执行；不同 Agent 同一 exclusive_key 同时写；过期 fence 完成/审批；重复提交外部 PR；超预算继续拆分；无证据宣布 SUCCEEDED；被阻塞项拉着整个 Goal 停止；在 Web 输入明文密码；ChatGPT 离线就中断有效执行；旧自动任务与 Workboard 同时认领。
- **故障**：Relay 重启、Connector 失联、子进程意外退出、Agent 返回失败、CI 长时间挂起、model quota、凭证失效；每种都应以可解释的 WAITING/RETRY_WAIT/BLOCKED/FAILED 呈现，不无限自我重试。

## 12. 研究依据与替代方案

### 项目现状（第一手）

- [LivingRuntime Remote README](../../README.md)
- [Remote Web Control Plane 规划](../../docs/WEB_CONTROL_PLANE.md)
- [Host-local Connector 设计](../../docs/HOST_CONNECTOR_ARCHITECTURE.md)
- [Remote Job 现有实现](../../plugin/scripts/jobs.py) 与 [Agent Session 实现](../../plugin/scripts/agent_sessions.py)
- [Remote Issue #56：Pi 真实验收与缺口](https://github.com/jboone1989/livingruntime-remote/issues/56)
- [Remote Issue #10：Host 主动事件订阅与完成 ACK](https://github.com/jboone1989/livingruntime-remote/issues/10)
- [EventGraph Issue #176：跨任务自动接力实战需求](https://github.com/jboone1989/EventGraph/issues/176)

### Upstream / 技术选择（查证或采用前需继续确认 API/许可）

- [SQLite Transaction / BEGIN IMMEDIATE](https://www.sqlite.org/lang_transaction.html)：首版单 Relay 原子 claim 的候选基础。
- [PostgreSQL SELECT FOR UPDATE / SKIP LOCKED](https://www.postgresql.org/docs/current/sql-select.html)：多消费 Worker 行级队列的扩展选项，**不是首版必需**。
- [Temporal](https://docs.temporal.io/)：更复杂的 durable workflow、human-in-the-loop 和可靠活动编排选项。
- [Hatchet](https://docs.hatchet.run/)：持久任务/并发执行能力的可选现成实现。
- [Coolify](https://coolify.io/docs/)：自有服务器上应用/容器部署，不在 Remote 中重造这类 PaaS。
- [OpenClaw](https://docs.openclaw.ai/)：持续自主 Agent/Gateway 体验的参照；不复制它的 Agent 大脑。

## 13. 文档状态与批准约束

本文件同时吸收了两条被 Owner 认可且**并不冲突**的方向：

- **可靠基础设施与体验**：跨云厂商 Connector、Agent 发现/生命周期、持久任务和回执、MCP/Web 控制台、以后可选的 Agent 安装简化；
- **自主协作**：长期目标发件箱、共享任务池、跨服务器/Agent 原子认领与并发接力、真正需要人的独立列表、可核验的收件箱。

它是统一产品 Spec，不是发布完成报告，也不自动授权任何生产部署。**未经 Owner 明确接受此 Spec 的产品边界和验收条件，不启动 Workboard 的功能开发或自动合并该 Spec。** 后续实现 PR 应引用批准时的固定 revision，且优先复用本仓库已有代码与官方/成熟现成能力。
