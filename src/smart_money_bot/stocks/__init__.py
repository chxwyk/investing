"""Coins anchored to real stocks.

The one lane in this bot whose catalyst exists off-chain and in public: a stock
moves for reasons that have nothing to do with crypto, and the coin minted
against it inherits that reason.  Every other lane has to infer intent from a
chart.

Read-only and pure: this package contains no provider, no database, no wallet
and no order path, and nothing in it can spend anything.
"""

from __future__ import annotations

from .anchors import (
    ANCHOR_LAUNCHPAD,
    ANCHOR_NAME_ONLY,
    ANCHOR_ONCHAIN,
    HUMAN_ANCHOR,
    VERIFIED_ANCHORS,
    AnchoredCoin,
    StockAnchor,
)
from .signal import (
    ANCHOR_HOT_NO_COIN,
    ANCHOR_QUIET,
    CLAIM_UNVERIFIED,
    DEFAULT_ANCHOR_CONFIG,
    HUMAN_OUTCOME,
    NOT_THE_LEADER,
    PINGABLE,
    STOCK_RUNNER,
    AnchorConfig,
    AnchorHeat,
    AnchorVerdict,
    evaluate_anchor,
    score_anchor,
)

__all__ = [
    "ANCHOR_HOT_NO_COIN",
    "ANCHOR_LAUNCHPAD",
    "ANCHOR_NAME_ONLY",
    "ANCHOR_ONCHAIN",
    "ANCHOR_QUIET",
    "CLAIM_UNVERIFIED",
    "DEFAULT_ANCHOR_CONFIG",
    "HUMAN_ANCHOR",
    "HUMAN_OUTCOME",
    "NOT_THE_LEADER",
    "PINGABLE",
    "STOCK_RUNNER",
    "VERIFIED_ANCHORS",
    "AnchorConfig",
    "AnchorHeat",
    "AnchorVerdict",
    "AnchoredCoin",
    "StockAnchor",
    "evaluate_anchor",
    "score_anchor",
]
