# US Stocks Swing Model v2

This is the independent replacement architecture for daily US stock/ETF
five-session research. It asks whether information causally available after a
completed session can support a useful forecast after explicit costs and
chronological walk-forward checks. It is research-only: not investment advice,
a live trading system, an options model, or proof of profitability.

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

Read [`docs/CURRENT_STATE.md`](docs/CURRENT_STATE.md) for the current milestone,
the selected next gate, and the distinction between routine local work and
work requiring approval. It is a concise snapshot, not authority: current
code, configuration, accepted releases, Git state, and action-specific
approval remain authoritative.

## Source roles

| Source | Current role |
|---|---|
| HF Data Library | Retired historical audit and trial-census evidence only |
| Alpaca SIP archive | Caveated PIT-unresolved legacy-discovery evidence |
| Alpaca Basic SIP | Selected qualified bar feed; canonical bars remain pending |
| Alpaca assets and Nasdaq Trader | Identity evidence, not bar or research authority |
| Alpha Vantage and options data | Excluded |

## Key documents

- [`AGENTS.md`](AGENTS.md): binding repository-operation and safety rules.
- [`docs/AGENT_WORKFLOW.md`](docs/AGENT_WORKFLOW.md): ordinary workflow and
  approval matrix.
- [`PROJECT_OUTLINE.md`](PROJECT_OUTLINE.md): enduring objective and roadmap.
- [`docs/REBUILD_CONSTITUTION.md`](docs/REBUILD_CONSTITUTION.md): binding
  scientific and design contract.

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
