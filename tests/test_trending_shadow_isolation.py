"""The two forward experiments must be genuinely independent (sections 62-63, 106).

The whole point of running $100 LEGACY against $100 TRENDING is to find out which
strategy actually makes more money and which actually gets rugged less.  That
question is unanswerable if the two books can touch each other in any way — a
shared bankroll, a shared position lock, a shared exposure ceiling, or a shared
experiment checkpoint would all silently couple the result to the thing being
measured.

Isolation here is structural rather than conventional: the shadow store keys
bankrolls by ``strategy_version`` and open positions by
``(mint, family, strategy_version)``.  These tests prove the partition holds
under the case most likely to break it — *the same mint traded in both books at
the same time*.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from smart_money_bot.database import Database
from smart_money_bot.lab.shadow import (
    DEFAULT_SHADOW_CONFIG,
    FAMILY_FAST_WATCH,
    SHADOW_EXPERIMENT_VERSION,
    SHADOW_STRATEGY_VERSION,
    ShadowSignal,
    ShadowTimestamps,
)
from smart_money_bot.shadow_runtime import ShadowRuntime
from smart_money_bot.shadow_store import ShadowStore
from smart_money_bot.trending import (
    TRENDING_EXPERIMENT_VERSION,
    TRENDING_STRATEGY_VERSION,
    TrendingShadowConfig,
)
from smart_money_bot.trending.shadow import FAMILY_ACCELERATION

D = Decimal
NOW = 1_700_000_000
MINT = "Mint1111111111111111111111111111111111111111"


@pytest.fixture
async def database():
    db = Database(":memory:", D("1000"))
    await db.connect()
    try:
        yield db
    finally:
        await db.close()


def _trending_config():
    """Exactly what the engine builds: legacy's shape, a different version."""

    return replace(
        DEFAULT_SHADOW_CONFIG,
        strategy_version=TRENDING_STRATEGY_VERSION,
        bankroll_usd=D("100"),
        position_usd=D("10"),
        min_position_usd=D("10"),
        max_position_usd=D("10"),
        max_token_exposure_usd=D("10"),
        max_concurrent_positions=5,
        max_total_exposure_usd=D("50"),
    )


def _signal(family: str = FAMILY_FAST_WATCH) -> ShadowSignal:
    return ShadowSignal(
        mint=MINT,
        family=family,
        timestamps=ShadowTimestamps(signal_at=NOW, decision_at=NOW),
        name="Test token",
        symbol="TEST",
        price_usd=D("0.001"),
        market_cap_usd=D("60000"),
        liquidity_usd=D("40000"),
        volume_usd=D("12000"),
        buys=80,
        sells=20,
        independent_buyers=30,
        organic_score=D("70"),
        momentum_score=D("70"),
        safety_status="UNKNOWN",
        route_available=True,
    )


async def _both(database):
    legacy = ShadowRuntime(ShadowStore(database))
    trending = ShadowRuntime(
        ShadowStore(database),
        config=_trending_config(),
        experiment_version=TRENDING_EXPERIMENT_VERSION,
    )
    await legacy.start_experiment(now=NOW - 10_000)
    await trending.start_experiment(now=NOW - 10_000)
    return legacy, trending


def test_the_two_experiments_use_different_version_strings() -> None:
    """A shared version string would silently merge the two books."""

    assert TRENDING_STRATEGY_VERSION != SHADOW_STRATEGY_VERSION
    assert TRENDING_EXPERIMENT_VERSION != SHADOW_EXPERIMENT_VERSION
    with pytest.raises(ValueError):
        TrendingShadowConfig(strategy_version=SHADOW_STRATEGY_VERSION)


async def test_both_experiments_start_at_one_hundred_dollars(database) -> None:
    """Section 52: identical shape, so the strategy is the only variable."""

    legacy, trending = await _both(database)
    legacy_state = await legacy.bankroll()
    trending_state = await trending.bankroll()

    assert legacy_state.cash_usd == D("100")
    assert trending_state.cash_usd == D("100")
    for runtime in (legacy, trending):
        assert runtime.config.position_usd == D("10")
        assert runtime.config.max_concurrent_positions == 5
        assert runtime.config.max_total_exposure_usd == D("50")


async def test_the_same_mint_can_be_open_in_both_books_at_once(database) -> None:
    """The case most likely to break the partition, asserted directly."""

    legacy, trending = await _both(database)

    legacy_decision, legacy_position = await legacy.consider_signal(_signal(), now=NOW)
    trending_decision, trending_position = await trending.consider_signal(
        _signal(FAMILY_ACCELERATION), now=NOW
    )

    assert legacy_decision.accepted is True
    assert trending_decision.accepted is True
    assert legacy_position is not None
    assert trending_position is not None
    assert legacy_position.position_id != trending_position.position_id
    assert legacy_position.mint == trending_position.mint == MINT


async def test_a_trending_fill_never_moves_the_legacy_bankroll(database) -> None:
    """Section 63: a dollar spent in one experiment is not spent in the other."""

    legacy, trending = await _both(database)
    await trending.consider_signal(_signal(FAMILY_ACCELERATION), now=NOW)

    legacy_state = await legacy.bankroll()
    trending_state = await trending.bankroll()
    assert legacy_state.cash_usd == D("100")
    assert legacy_state.open_positions == 0
    assert trending_state.cash_usd == D("90")
    assert trending_state.open_positions == 1


async def test_exhausting_one_book_leaves_the_other_free_to_trade(database) -> None:
    """Five open Trending positions must not stop the legacy experiment."""

    legacy, trending = await _both(database)
    for index in range(5):
        await trending.consider_signal(
            replace(
                _signal(FAMILY_ACCELERATION),
                mint=f"Mint{index}111111111111111111111111111111111111",
            ),
            now=NOW,
        )
    trending_state = await trending.bankroll()
    assert trending_state.open_positions == 5

    # The trending book is at its ceiling; the legacy book has not moved.
    refused, _ = await trending.consider_signal(
        replace(_signal(FAMILY_ACCELERATION), mint="MintZZZ1111111111111111111111111111111111"),
        now=NOW,
    )
    assert refused.accepted is False

    accepted, position = await legacy.consider_signal(_signal(), now=NOW)
    assert accepted.accepted is True
    assert position is not None


async def test_each_experiment_reads_only_its_own_positions(database) -> None:
    legacy, trending = await _both(database)
    await legacy.consider_signal(_signal(), now=NOW)
    await trending.consider_signal(_signal(FAMILY_ACCELERATION), now=NOW)

    store = ShadowStore(database)
    legacy_rows = await store.open_positions(strategy_version=SHADOW_STRATEGY_VERSION)
    trending_rows = await store.open_positions(strategy_version=TRENDING_STRATEGY_VERSION)

    assert len(legacy_rows) == 1
    assert len(trending_rows) == 1
    assert legacy_rows[0].family == FAMILY_FAST_WATCH
    assert trending_rows[0].family == FAMILY_ACCELERATION


async def test_each_experiment_keeps_its_own_checkpoint(database) -> None:
    """Section 41's forward boundary applies per experiment, not globally."""

    legacy, trending = await _both(database)
    store = ShadowStore(database)
    legacy_row = await store.experiment(experiment_version=SHADOW_EXPERIMENT_VERSION)
    trending_row = await store.experiment(experiment_version=TRENDING_EXPERIMENT_VERSION)

    assert legacy_row is not None
    assert trending_row is not None
    assert legacy_row["experiment_version"] != trending_row["experiment_version"]
    assert legacy_row["starting_bankroll_usd"] == 100.0
    assert trending_row["starting_bankroll_usd"] == 100.0


async def test_both_books_survive_a_restart_independently(database) -> None:
    """Section 111: a redeploy restores two accounts, not one merged one."""

    legacy, trending = await _both(database)
    await legacy.consider_signal(_signal(), now=NOW)
    await trending.consider_signal(_signal(FAMILY_ACCELERATION), now=NOW)

    restarted_legacy = ShadowRuntime(ShadowStore(database))
    restarted_trending = ShadowRuntime(
        ShadowStore(database),
        config=_trending_config(),
        experiment_version=TRENDING_EXPERIMENT_VERSION,
    )
    legacy_state = await restarted_legacy.bankroll()
    trending_state = await restarted_trending.bankroll()

    assert legacy_state.cash_usd == D("90")
    assert legacy_state.open_positions == 1
    assert trending_state.cash_usd == D("90")
    assert trending_state.open_positions == 1


async def test_the_legacy_experiment_definition_is_unchanged(database) -> None:
    """Section 62: the existing experiment is preserved exactly as-is."""

    assert DEFAULT_SHADOW_CONFIG.strategy_version == SHADOW_STRATEGY_VERSION
    assert DEFAULT_SHADOW_CONFIG.bankroll_usd == D("100")
    assert DEFAULT_SHADOW_CONFIG.position_usd == D("10")
    assert DEFAULT_SHADOW_CONFIG.max_concurrent_positions == 5
    assert DEFAULT_SHADOW_CONFIG.max_total_exposure_usd == D("50")


async def test_neither_experiment_can_reach_a_real_wallet(database) -> None:
    """Section 109: no signer, no key, no swap, no SOL — in either book."""

    legacy, trending = await _both(database)
    for runtime in (legacy, trending):
        for attribute in ("wallet", "keypair", "signer", "private_key", "rpc"):
            assert not hasattr(runtime, attribute)
    _, position = await trending.consider_signal(_signal(FAMILY_ACCELERATION), now=NOW)
    assert position is not None
    # A simulated fill has no transaction, because there is nothing to sign.
    assert not hasattr(position, "signature")
