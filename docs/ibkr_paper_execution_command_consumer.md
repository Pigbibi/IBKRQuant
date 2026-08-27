# IBKR isolated paper command consumer

`POST /paper-command-consumer` is an explicit, default-disabled verifier for
delayed paper execution commands. It is not connected to `/run`, `/dry-run`,
Cloud Scheduler, or the ordinary rebalance/order path.

The consumer uses the shared QuantPlatformKit lifecycle: release attestation,
exact platform/account/strategy binding, create-only claim/events, immutable
paper-risk receipt, and enforced runtime gate. It then reads the IBKR
`portfolio()` market-value snapshot and current quotes to create audit-only
proposals. It has no execution-port or order-submission import.

## Required isolation

All conditions must be true before it reads the Gateway:

- `IBKR_PAPER_EXECUTION_COMMAND_CONSUMER_ENABLED=true`
- `RUNTIME_TARGET_ENABLED=false`
- `IBKR_DRY_RUN_ONLY=true`
- `IBKR_GATEWAY_MODE=paper`
- `RUNTIME_TARGET_JSON.execution_mode=paper`
- `CASH_ONLY_EXECUTION=true`
- `IBKR_EXECUTION_COMMAND_CLOUD_URI` (or `IBKR_EXECUTION_COMMAND_DIR`) is set

The expected command binding is taken from the runtime target, never from the
command: `platform=ibkr`, plus its `account_scope` and `strategy_profile`.
Any mismatch is rejected before the consumer opens the Gateway.

## Reconciliation behavior

The consumer fails closed if the selected IBKR account lacks a current market
value, market-currency cash value, valid quote, exactly matching managed symbol
set, or a reconciled cash-plus-positions total. It records a rejection or
reconciliation-required event; it does not infer a quantity, lower leverage,
or submit an order.

Disable the flag again after a manual verification run. Moving beyond paper
evidence requires separate reviewed release-readiness and live rollout.
