# IBKR PAPER execution admission

`IBKR_PAPER_EXECUTION_ADMISSION_ENABLED` is an opt-in guard for the ordinary
IBKR `execution_mode=paper` rebalance path. Its default is disabled. It does
not apply to `IBKR_PAPER_LIQUIDATE_ONLY`, dry-run previews, live execution,
deployment workflows, or schedulers.

When explicitly enabled, the strategy/control-plane producer must place a QPK
`execution_command.v1` object at `signal_metadata.paper_execution_command`.
The command must be content-addressed, use `execution_mode=paper`, and contain:

- the exact promoted `strategy_release` identity;
- an embedded `paper_risk_admission_receipt.v1`; and
- the same effective session, profile, and account scope as the paper cycle.

Before any order adapter is invoked, the platform verifies that command and
receipt, self-attests the runtime release, and derives every exposure effect
from the current position quantities and quotes used by the cycle. Invalid or
missing evidence, an unmodelled option intent, or a `reducing_only` receipt
that would increase exposure blocks the whole cycle. The safe receipt,
reconciled exposure facts, and enforced runtime-gate receipts are persisted in
the normal reconciliation record under `paper_execution_admission`.

Do not enable the flag until an upstream producer can supply this immutable
command contract. Enabling it for a non-PAPER or dry-run target fails at
startup rather than silently weakening the guard.
