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
