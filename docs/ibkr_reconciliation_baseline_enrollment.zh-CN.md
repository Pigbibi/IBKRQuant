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
