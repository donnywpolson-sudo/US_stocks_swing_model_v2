from __future__ import annotations
from pathlib import Path
import pytest
from us_stocks_swing_model_v2.errors import ContractError
from us_stocks_swing_model_v2.providers import alpaca_sip_non_active_cutover as cutover
from us_stocks_swing_model_v2.providers import alpaca_canonical_bars as canonical

ROOT=Path(__file__).resolve().parents[1]
def test_non_active_cutover_policy_is_exact() -> None:
    policy=cutover.load_policy(ROOT)
    assert policy["target"] == {"qualified_feed":"sip","status":"qualified_sip_not_active","enabled_for_active_pipeline":False}
    assert policy["receipt_release_id"] == "c87e98a610c22b6a3e9101d813e69a244d97e79bf078595c3a4b71f8932f1bd0"
def test_july_policy_is_explicitly_diagnostic() -> None:
    policy=canonical._load_policy(ROOT)
    assert policy["diagnostic_only"] is True
    with pytest.raises(ContractError, match="diagnostic-only"):
        canonical._context(ROOT, require_clean=False)
