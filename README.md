# US Stocks Swing Model v2

This is the independent replacement architecture for daily US stock/ETF
five-session research. It asks whether information causally available after a
completed session can support a useful forecast after explicit costs and
chronological walk-forward checks. It is research-only: not investment advice,
a live trading system, an options model, or proof of profitability.

The active operating path is completely free and uses local desktop storage
with GitHub backup only. Paid data, subscriptions, commercial trial offers,
and additional hosted infrastructure are excluded. Until a complete free
effective-event and delisting source satisfies the existing evidence contract,
the repository is a discovery and prospective-mechanics system: production
inputs, outcomes, real-trial registration, training, evaluation, activation,
candidate sealing, and readiness claims remain fail-closed.

The opt-in [`ALPACA_FREE_BOUNDED_V1`](docs/ALPACA_FREE_BOUNDED_V1.md)
profile adds a bounded reconstructed-history and prospective-as-observed path
for Alpaca SIP long/short data research. It preserves the frozen strict
contract, keeps training and evaluation blocked, and does not claim complete
total return or verified historical borrowability.

## Five-session contract

```text
decision_at = D0 close + provider publication latency
entry_at    = D1 regular-session open
exit_at     = D5 regular-session close
target      = (split-normalized exit price / split-normalized entry price) - 1
```

`D1` through `D5` are pinned exchange sessions, not calendar days. The project
uses underlying stock/ETF evidence only; dividends are excluded from this
price-return target.

## Start here

Read [`docs/CURRENT_STATE.md`](docs/CURRENT_STATE.md) for the current milestone
and selected next gate. It is a concise snapshot, not authority: current code,
configuration, accepted releases, Git state, and action-specific approval
remain authoritative. Use the canonical task-routing and gate checklist in
[`AGENTS.md`](AGENTS.md#canonical-task-routing-and-gate-checklist) for
operational rules.

## Source roles

| Source | Current role |
|---|---|
| Alpaca historical archive | Caveated legacy evidence with unresolved universe selection |
| Alpaca Basic SIP | Sole qualified bar feed; bounded smoke completed, still non-active |
| Alpaca assets and Nasdaq Trader | Identity evidence, not bar or research authority |
| Alpaca corporate actions | Raw provider-process-date evidence only; not complete effective-event or delisting evidence |
| Alpha Vantage and options data | Excluded |

The table describes the strict/default contract. Under the opt-in bounded
profile only, dated Alpha Vantage `LISTING_STATUS` is a candidate historical
membership source and options remain excluded.

## Key documents

- [`AGENTS.md`](AGENTS.md): binding repository-operation, safety, and task
  routing rules.
- [`docs/AGENT_WORKFLOW.md`](docs/AGENT_WORKFLOW.md): ordinary workflow
  examples.
- [`PROJECT_OUTLINE.md`](PROJECT_OUTLINE.md): enduring objective and roadmap.
- [`docs/REBUILD_CONSTITUTION.md`](docs/REBUILD_CONSTITUTION.md): binding
  scientific and design contract.
- [`docs/ALPACA_FREE_BOUNDED_V1.md`](docs/ALPACA_FREE_BOUNDED_V1.md): opt-in
  free Alpaca long/short data profile, commands, and readiness boundary.

For provider/publication work, read
[`docs/NETWORK_ACQUISITION.md`](docs/NETWORK_ACQUISITION.md). For historical
research planning or review, read
[`docs/HISTORICAL_RESEARCH_HARNESS.md`](docs/HISTORICAL_RESEARCH_HARNESS.md).
For audit work, read [`docs/AUDIT_WORKFLOW.md`](docs/AUDIT_WORKFLOW.md).

## Safe Codex entrypoint

For a status check or local task, state the desired outcome and keep work in
this repository. Codex should inspect Git and the applicable binding documents,
complete routine local work and focused checks, and ask only at a genuine
approval boundary. Do not paste commands, hashes, or continuation prompts to
keep same-thread work moving.
