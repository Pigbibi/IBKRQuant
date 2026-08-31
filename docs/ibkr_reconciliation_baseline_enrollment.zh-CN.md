# IBKR 旧实盘基线：审核材料生成

`scripts/build_reconciliation_baseline_candidate.py` 只处理两份或更多私有的、已脱敏
`/reconcile` 回应或运行报告。它调用 QuantPlatformKit 的通用规则，验证这些收据是
新鲜、时间分离、账户身份一致且全部状态摘要相同，然后输出
`broker_reconciliation_baseline_candidate.v1`。

该工具不连接 IB Gateway、不访问原始账户资料、不写 Cloud Run 环境变量、不改
`RUNTIME_TARGET_JSON`，更不会下单。它的非零退出码表示候选尚不能进入审计；这不是
故障恢复授权。

私有控制面将候选的 `candidate_sha256` 交给 AIAuditBridge 的
`reconciliation_baseline` 强制双审。双审结果与候选摘要一致后，统一管理站点仍必须
让操作者人工确认“恢复原有实盘基线”。现有自动化权限策略将这类 broker/order
execution 变更视为高风险，禁止自动恢复。

## 发布给统一管理站点的最小来源快照

`scripts/publish_reconciliation_recovery_source.py` 只接受上一步的私有候选和
AIAuditBridge 的完整 `reconciliation_baseline` 输出。它会同时核验：强制多审已
执行、至少一名主审和一名独立复审存在、结论通过、候选 SHA-256 完全绑定，以及
审计结果仍明确要求人工恢复确认和 `escalate` 权限边界。

默认行为只是打印 `qsl_reconciliation_recovery_source_snapshot.v1`，其中只有不透明
恢复 ID、平台/策略、候选摘要、采样时间窗、审计绑定与稳定阻断码；不含账号、仓位、
现金、订单、成交明细或五项状态摘要。它不连接 IB Gateway，不修改运行时，也不会下单。

只有显式传入 QRS 的 HTTPS
`/api/internal/sync-reconciliation-recovery-source` 地址，且环境中存在专用的
`RECONCILIATION_RECOVERY_SYNC_TOKEN` 时，脚本才会把上述最小快照发给统一管理站点。
该写入仅供人工确认队列使用，不能恢复 `ACTIVE_LKG`。后续私有控制器仍须重新读取
券商、复核双审绑定，并以原子比较并设置方式转换状态；任一失败都保持
`RECONCILE_ONLY`。

来源发布器还可在显式给出
`gs://.../reconciliation-recovery/ibkr/source/...` 时，把完整候选与双审回执写入
私有证据包。该包不发送给 QRS；写入固定使用 GCS `if_generation_match=0`，所以已存在
对象会失败而不会被覆盖、读取或删除。验证器只能在专用私有存储中读取它。

## 私有验证器（暂不写状态）

`scripts/verify_reconciliation_recovery.py` 是恢复链路的第二层。它使用一枚不同于
来源发布令牌的 `RECONCILIATION_RECOVERY_CONTROLLER_TOKEN`，从 QRS 只读取得已确认
条目；然后重新解析本地私有候选与完整双审回执，读取一份**确认之后**新生成的
`/reconcile` 回执，并核对已部署 `RUNTIME_TARGET_JSON` 仍为同一
`RECONCILE_ONLY` 基线。

该验证器只输出 `ibkr_reconciliation_recovery_verification.v1` 和可能的 QPK 原子切换
计划。即使验证通过，输出也固定 `controller_mode=verify_only`、`no_order=true`、
`execution_authority_granted=false`、`state_write_attempted=false`；它没有 Cloud Run、
券商、执行标记或订单写入代码。下一层单独的最小权限控制器才可消费该计划，并且仍要
在同一目标上比较五项摘要后执行一次精确 CAS。

## 只读状态账本适配层（默认关闭）

为避免把一段旧的 `RUNTIME_TARGET_JSON` 直接写回 GitHub 变量，部署链路可选择读取一份
私有、不可覆盖的状态账本。账本的结构固定为
`ibkr_reconciliation_recovery_state_ledger.v1`，且只含四项：`recovery_id`、
`service_name` 和上述完整 QPK `transition_plan`（另加版本）。它**不**携带下一个运行
目标、账户、策略、仓位、订单或执行权限。

消费时系统从当前完整运行目标推导结果，并逐项验证：服务必须唯一匹配、平台必须为
IBKR、当前状态必须仍是 `RECONCILE_ONLY`、基线 ID 与目标指纹必须等于计划的冻结值，且
QPK 计划中的五项摘要、`no_order=true`、`execution_authority_granted=false` 和 CAS 标志
必须完整存在。验证成功后，唯一允许的差异是
`live_continuity.state: RECONCILE_ONLY -> ACTIVE_LKG`；五项摘要以
`IBKR_RECONCILIATION_EXPECTED_DIGESTS_JSON` 注入运行环境。任何字段缺失、账本重放、目标
漂移、服务不匹配或多服务误匹配都会失败关闭。

工作流只有在仓库变量 `IBKR_RECONCILIATION_RECOVERY_STATE_LEDGER_URI` 显式非空时才会读取
账本；URI 还必须落在专用私有桶的
`reconciliation-recovery/ibkr/state/*.json` 前缀。未设置时不下载账本、不改变同步计划，
既有实盘目标也不受影响。本阶段不写入该变量，也不创建账本对象；因此它只是经过测试的
兼容入口，而不是一次自动或隐式的实盘恢复。

`scripts/publish_reconciliation_recovery_state_ledger.py` 则是与消费端分离的发布适配器。
它只接受刚生成的 `ibkr_reconciliation_recovery_verification.v1`：验证结果必须无阻断、
完整携带 QPK 计划，并且仍声明 `verify_only`、无订单、无执行授权和未尝试状态写入。默认
只打印最小账本 JSON；只有显式提供状态前缀的 GCS URI 时才调用
`if_generation_match=0` 创建对象。该写入不读取、列举、覆盖或删除对象，也不会设置工作流
URI、部署 Cloud Run、连接券商或提交订单。实际启用仍需要单独的高风险控制面动作。

## 故障注入回归

恢复链路的回归测试会主动注入：控制台把不可执行策略篡改为可执行、五项摘要之一
漂移、人工确认过期、确认与新回执同秒、运行目标不再是 `RECONCILE_ONLY`、以及控制器
令牌被试图发送到普通管理站路径。每一种情况都必须抛出拒绝或返回没有
`transition_plan` 的结果；测试同时断言 `state_write_attempted=false`。这让后续接入
最小权限 CAS 时能持续证明“异常只能保持冻结，不能意外恢复实盘”。

## 收集两份候选收据

`Collect IBKR Reconciliation Evidence` 是显式手动工作流。由于这些 Cloud Run 服务只接受
内部入口，工作流会以部署身份创建一个名称绑定到本次运行、几分钟后只执行一次的 Cloud
Scheduler 任务，再由既有的最小权限 Scheduler 身份调用冻结服务的 `POST /reconcile`。它随后只从
私有运行报告提取脱敏 `ibkr_reconciliation_candidate.v1`，在 30 天内保留 artifact，并在
成功或失败时删除该一次性任务。它不调用 `/run`、不修改 GitHub 变量、不发布状态账本，也
不发送任何订单。

工作流 artifact 的外层是 `ibkr_reconciliation_artifact.v1`，内部仅保留同一份
`broker_reconciliation` 摘要；`build_reconciliation_baseline_candidate.py` 可直接消费它。
这样基线审核不必下载包含其他运行诊断的完整私有报告。

同一目标至少应在相隔一分钟的两次手动运行中得到候选，才能交给
`build_reconciliation_baseline_candidate.py`。工作流的成功只说明读取和收据格式正常；
候选仍可能因为未配置预期摘要或账本差异而正确保持阻断。

其中现金摘要只选择结算/账面现金标签（例如 `CashBalance`），不会把随市价变化的
`NetLiquidation` 或保证金可用额计入。这样没有现金、订单或仓位变化的账户不会因为正常
估值波动而被误判为基线漂移；没有可靠现金标签时仍会失败关闭。
