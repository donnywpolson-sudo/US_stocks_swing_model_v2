"""Causal, discovery-only Alpaca price features and non-executable WFA plan."""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from .common import canonical_json_bytes, reject_link, sha256_bytes, sha256_file
from .errors import ContractError, IntegrityError
from .releases import verify_accepted_release


PROJECT = "US_stocks_swing_model_v2"
CONTRACT_PATH = "config/alpaca_discovery_proxy_feature_wfa_contract.json"
FEATURE_NAMES = (
    "d0_raw_intraday_return",
    "trailing_5_session_raw_return",
    "trailing_5_session_raw_volatility",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_feature_wfa_contract(repo_root: Path | None = None) -> dict[str, Any]:
    root = Path(repo_root or _repo_root()).resolve(strict=True)
    path = root / CONTRACT_PATH
    reject_link(path)
    payload = json.loads(path.read_bytes())
    if type(payload) is not dict:
        raise ContractError("proxy feature/WFA contract must be an object")
    contract_id = payload.pop("contract_id", None)
    if contract_id != sha256_bytes(canonical_json_bytes(payload)):
        raise IntegrityError("proxy feature/WFA contract ID differs")
    features, wfa = payload.get("features"), payload.get("wfa")
    claims = payload.get("claims", {})
    if (
        payload.get("schema_version") != 1
        or payload.get("project") != PROJECT
        or payload.get("mode") != "ALPACA_LEGACY_DISCOVERY_PROXY_FEATURE_WFA_PLAN_ONLY"
        or type(features) is not dict
        or tuple(features.get("feature_names", ())) != FEATURE_NAMES
        or features.get("lookback_sessions") != 5
        or features.get("uses_only_current_or_prior_pinned_sessions") is not True
        or features.get("may_read_outcomes") is not False
        or features.get("real_row_build_authorized") is not False
        or type(wfa) is not dict
        or wfa.get("outer_protocol") != "rolling_origin"
        or wfa.get("purge_sessions") != 5
        or wfa.get("embargo_sessions") != 5
        or wfa.get("fold_local_transforms_required") is not True
        or wfa.get("trial_registration_required") is not True
        or wfa.get("real_history_execution_authorized") is not False
        or wfa.get("training_or_evaluation_authorized") is not False
        or claims != {"historical_proxy": True, "canonical_target_equivalent": False, "trusted_sleeve_eligible": False, "alpha_claim": False}
    ):
        raise ContractError("proxy feature/WFA contract differs")
    return {**payload, "contract_id": contract_id}


def build_price_only_proxy_features(sessions: Sequence[date], bars: Sequence[Mapping[str, object]]) -> tuple[dict[str, object], ...]:
    """Build features from D0 and its five preceding pinned sessions only."""

    ordered = tuple(sessions)
    if not ordered or tuple(sorted(ordered)) != ordered or len(set(ordered)) != len(ordered) or any(type(value) is not date for value in ordered):
        raise ContractError("feature sessions must be sorted unique exact dates")
    by_symbol: dict[str, dict[date, Mapping[str, object]]] = {}
    for row in bars:
        if type(row) is not dict or set(row) != {"symbol", "session", "open", "close"}:
            raise ContractError("feature bar fields differ")
        symbol, session = row["symbol"], row["session"]
        if type(symbol) is not str or not symbol or symbol != symbol.strip().upper() or type(session) is not date:
            raise ContractError("feature bar identity differs")
        if session not in ordered or session in by_symbol.setdefault(symbol, {}):
            raise ContractError("feature bar session differs")
        by_symbol[symbol][session] = row
    rows: list[dict[str, object]] = []
    for symbol in sorted(by_symbol):
        for index, decision_session in enumerate(ordered[5:], start=5):
            current = by_symbol[symbol].get(decision_session)
            history = [by_symbol[symbol].get(session) for session in ordered[index - 5 : index + 1]]
            closes = [None if value is None else value["close"] for value in history]
            open_value = None if current is None else current["open"]
            close_value = None if current is None else current["close"]
            valid = current is not None and all(type(value) in {int, float} and not isinstance(value, bool) and math.isfinite(float(value)) and float(value) > 0 for value in closes) and type(open_value) in {int, float} and not isinstance(open_value, bool) and math.isfinite(float(open_value)) and float(open_value) > 0 and type(close_value) in {int, float} and not isinstance(close_value, bool) and math.isfinite(float(close_value)) and float(close_value) > 0
            if valid:
                prices = [float(value) for value in closes]
                returns = [prices[position] / prices[position - 1] - 1.0 for position in range(1, len(prices))]
                mean = sum(returns) / len(returns)
                rows.append({"symbol": symbol, "decision_session": decision_session, "d0_raw_intraday_return": float(close_value) / float(open_value) - 1.0, "trailing_5_session_raw_return": prices[-1] / prices[0] - 1.0, "trailing_5_session_raw_volatility": math.sqrt(sum((value - mean) ** 2 for value in returns) / len(returns)), "status": "READY_CAUSAL_RAW_PRICE_FEATURES"})
            else:
                rows.append({"symbol": symbol, "decision_session": decision_session, "d0_raw_intraday_return": None, "trailing_5_session_raw_return": None, "trailing_5_session_raw_volatility": None, "status": "UNRESOLVED_CAUSAL_LOOKBACK"})
    return tuple(rows)


def build_feature_wfa_plan(proxy_outcome_release_directory: Path, *, accepted_root: Path, repo_root: Path | None = None) -> dict[str, Any]:
    """Bind a caveated proxy-outcome release without reading its rows."""

    contract = load_feature_wfa_contract(repo_root)
    release = verify_accepted_release(Path(proxy_outcome_release_directory), accepted_root=Path(accepted_root))
    if release.dataset != "alpaca_discovery_proxy_outcomes" or release.role != "legacy_discovery_only" or release.quality_state != "LEGACY_CAVEATED":
        raise ContractError("proxy outcome release differs")
    evidence_path = Path(proxy_outcome_release_directory) / "source_evidence_manifest.json"
    reject_link(evidence_path)
    evidence = json.loads(evidence_path.read_bytes())
    if evidence.get("historical_proxy") is not True or evidence.get("canonical_target_equivalent") is not False or evidence.get("survivorship_safe") is not False or evidence.get("training_or_evaluation") is not False:
        raise ContractError("proxy outcome caveats differ")
    unsigned = {"schema_version": 1, "mode": contract["mode"], "contract_id": contract["contract_id"], "proxy_outcome_release": {"release_id": release.release_id, "manifest_sha256": sha256_file(Path(proxy_outcome_release_directory) / "release_manifest.json"), "row_count": release.row_count, "event_start": release.event_start, "event_end": release.event_end}, "features": contract["features"], "wfa": contract["wfa"], "claims": contract["claims"], "validation_scope": {"proxy_outcome_rows_opened": 0, "files_written": 0}, "required_later_authority": {"real_feature_build": True, "registered_historical_trial": True, "training_or_evaluation": True}, "stop_conditions": contract["stop_conditions"]}
    return {**unsigned, "feature_wfa_plan_id": sha256_bytes(canonical_json_bytes(unsigned))}
