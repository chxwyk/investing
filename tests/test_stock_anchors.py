"""Coins anchored to real stocks — stonksonchain.lol's premise, as logic.

The operator's ask: *"the top coin on every stock"*, and *"if there's a coin
linked with a stock and it's a crazy popular stock, you gotta ping me"*.

This lane is different from every other one in the bot, and the difference is
the point. A memecoin has no referent — nothing outside its own chart says
whether it should be moving. A coin anchored to a stock token does: the stock is
a real instrument that moves for reasons that have nothing to do with crypto,
and those reasons are public before the coin reacts.

That is worth the loudest alert here — which is exactly why the bar is *higher*
than the memecoin lanes, not lower. The last four releases were all about loud
and wrong.

No network, no database, no wallet.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

from smart_money_bot.stocks import (
    ANCHOR_HOT_NO_COIN,
    ANCHOR_LAUNCHPAD,
    ANCHOR_NAME_ONLY,
    ANCHOR_ONCHAIN,
    ANCHOR_QUIET,
    CLAIM_UNVERIFIED,
    NOT_THE_LEADER,
    STOCK_RUNNER,
    AnchorConfig,
    AnchoredCoin,
    StockAnchor,
    evaluate_anchor,
    score_anchor,
)


def _hot(**overrides) -> StockAnchor:
    values = dict(
        ticker="NVDA",
        name="NVIDIA",
        token_address="0xAbC123",
        change_percent=Decimal("9.4"),
        relative_volume=Decimal("4.2"),
        news_sources=5,
    )
    values.update(overrides)
    return StockAnchor(**values)


def _coin(**overrides) -> AnchoredCoin:
    values = dict(
        mint="LeaderMint",
        symbol="NVDA",
        name="Nvidia Coin",
        launchpad="LONG",
        anchor_key="0xabc123",
        anchor_ticker="NVDA",
        anchor_claim=ANCHOR_ONCHAIN,
        age_seconds=300,
        liquidity_usd=Decimal("48000"),
        holder_count=310,
        buys=400,
        sells=280,
    )
    values.update(overrides)
    return AnchoredCoin(**values)


# --- the anchor side --------------------------------------------------------


def test_a_stock_that_is_moving_hard_on_heavy_volume_is_a_catalyst() -> None:
    heat = score_anchor(_hot())
    assert heat.hot() is True
    assert any("9.4%" in item for item in heat.reasons)
    assert any("4.2x" in item for item in heat.reasons)


def test_a_stock_doing_nothing_is_not_a_catalyst_however_good_the_coin() -> None:
    quiet = _hot(change_percent=Decimal("0.3"), relative_volume=Decimal("0.9"), news_sources=0)
    assert score_anchor(quiet).hot() is False
    verdict = evaluate_anchor(_coin(), quiet)
    assert verdict.outcome == ANCHOR_QUIET
    assert verdict.may_ping is False


def test_a_crashing_stock_is_just_as_much_of_a_catalyst_as_a_rising_one() -> None:
    # The coins that launch against a crash are some of the fastest movers in
    # this market. Reading the sign instead of the size would miss all of them.
    up = score_anchor(_hot(change_percent=Decimal("9.4")))
    down = score_anchor(_hot(change_percent=Decimal("-9.4")))
    assert up.score == down.score
    assert down.hot() is True
    assert any("down 9.4%" in item for item in down.reasons)


def test_volume_is_measured_against_the_instruments_own_normal() -> None:
    # A hundred million shares is enormous for one company and a quiet morning
    # for another, so the field is a ratio and there is no absolute share count
    # anywhere in the model.
    assert "relative_volume" in {f for f in StockAnchor.__slots__}
    assert not any("share" in f for f in StockAnchor.__slots__)


def test_an_unmeasured_anchor_is_never_called_hot() -> None:
    # A provider that did not answer must not read as a stock nobody is
    # trading — the same rule the memecoin lane runs on.
    blank = StockAnchor(ticker="NVDA", token_address="0xabc123")
    heat = score_anchor(blank)
    assert heat.measured is False
    assert heat.hot() is False


def test_a_corporate_action_is_reported_rather_than_scored() -> None:
    # Prices either side of a split are not comparable. Saying so is useful;
    # folding it into a score would be pretending it is comparable.
    heat = score_anchor(_hot(corporate_action="4:1 split"))
    assert any("corporate action" in item for item in heat.reasons)
    plain = score_anchor(_hot())
    assert heat.score == plain.score


# --- the claim ---------------------------------------------------------------


def test_a_ticker_in_a_coins_name_is_a_claim_and_not_a_link() -> None:
    """The NORMIE problem, generalised.

    A pump.fun coin calling itself $NVDA is a memecoin in a costume, and the
    entire premise of this lane — that the catalyst is real and public — does
    not apply to it. Note this one has the *deepest* liquidity of the three.
    """

    costume = _coin(
        mint="CostumeMint",
        launchpad="pump.fun",
        anchor_key="ticker:NVDA",
        anchor_claim=ANCHOR_NAME_ONLY,
        liquidity_usd=Decimal("90000"),
        holder_count=500,
    )
    verdict = evaluate_anchor(costume, _hot(), [costume, _coin()])
    assert verdict.outcome == CLAIM_UNVERIFIED
    assert verdict.may_ping is False
    assert "except its name" in verdict.reasons[0]


def test_depth_never_buys_an_unverified_claim_the_anchor() -> None:
    costume = _coin(
        mint="CostumeMint",
        anchor_key="ticker:NVDA",
        anchor_claim=ANCHOR_NAME_ONLY,
        liquidity_usd=Decimal("500000"),
    )
    assert evaluate_anchor(costume, _hot()).may_ping is False


def test_a_launchpad_declared_anchor_counts_as_verified() -> None:
    declared = _coin(anchor_claim=ANCHOR_LAUNCHPAD)
    assert declared.verified_anchor is True
    assert evaluate_anchor(declared, _hot()).outcome == STOCK_RUNNER


def test_a_costume_coin_cannot_take_the_anchor_from_a_real_one() -> None:
    # Rivalry is between coins with real claims. Otherwise anyone could park a
    # deep name-only coin on a ticker and permanently mute the genuine one.
    costume = _coin(
        mint="CostumeMint",
        anchor_key="ticker:NVDA",
        anchor_claim=ANCHOR_NAME_ONLY,
        liquidity_usd=Decimal("900000"),
    )
    verdict = evaluate_anchor(_coin(), _hot(), [costume])
    assert verdict.outcome == STOCK_RUNNER
    assert verdict.rivals == ()


# --- leadership --------------------------------------------------------------


def test_the_top_coin_on_a_hot_stock_is_the_one_that_interrupts() -> None:
    second = _coin(mint="SecondMint", launchpad="Pons", liquidity_usd=Decimal("12000"))
    verdict = evaluate_anchor(_coin(), _hot(), [_coin(), second])
    assert verdict.outcome == STOCK_RUNNER
    assert verdict.may_ping is True
    assert verdict.anchor_ticker == "NVDA"


def test_being_fourth_on_a_hot_ticker_is_the_noise_and_not_the_trade() -> None:
    leader = _coin(mint="LeaderMint", liquidity_usd=Decimal("48000"))
    also_ran = _coin(mint="AlsoRan", launchpad="Pair", liquidity_usd=Decimal("9000"))
    verdict = evaluate_anchor(also_ran, _hot(), [leader, also_ran])
    assert verdict.outcome == NOT_THE_LEADER
    assert verdict.may_ping is False
    assert "LeaderMint" in verdict.rivals


def test_coins_sharing_an_anchor_without_a_clear_leader_promote_nobody() -> None:
    # Two coins within noise of each other are sharing the anchor, not owning
    # it. Same conservatism as the same-name verdict in v2.47.
    a = _coin(mint="A", liquidity_usd=Decimal("30000"))
    b = _coin(mint="B", liquidity_usd=Decimal("28000"))
    assert evaluate_anchor(a, _hot(), [a, b]).outcome == NOT_THE_LEADER
    assert evaluate_anchor(b, _hot(), [a, b]).outcome == NOT_THE_LEADER


def test_coins_on_different_stocks_are_not_rivals() -> None:
    other = _coin(mint="TSLAcoin", anchor_key="0xtesla", anchor_ticker="TSLA",
                  liquidity_usd=Decimal("900000"))
    assert evaluate_anchor(_coin(), _hot(), [other]).outcome == STOCK_RUNNER


# --- the floors, so a leader of corpses is not promoted ----------------------


def test_the_leader_of_four_dead_coins_is_not_promoted() -> None:
    dead = _coin(liquidity_usd=Decimal("800"), holder_count=6)
    verdict = evaluate_anchor(dead, _hot())
    assert verdict.outcome == ANCHOR_HOT_NO_COIN
    assert verdict.may_ping is False


def test_a_hot_anchor_with_no_tradeable_coin_is_said_out_loud() -> None:
    # The one state where the operator may want to act before the bot can: the
    # catalyst is real and nothing has been minted against it worth buying yet.
    verdict = evaluate_anchor(_coin(holder_count=4), _hot())
    assert verdict.outcome == ANCHOR_HOT_NO_COIN
    assert "moving" in verdict.reasons[0]


def test_selling_into_the_move_is_refused_here_too() -> None:
    # Same refusal as the memecoin lane. A hot anchor does not excuse an exit.
    exiting = _coin(buys=100, sells=420)
    assert evaluate_anchor(exiting, _hot()).outcome == ANCHOR_HOT_NO_COIN


def test_only_a_stock_runner_can_ever_ping() -> None:
    outcomes = set()
    cases = (
        _coin(),
        _coin(anchor_claim=ANCHOR_NAME_ONLY),
        _coin(holder_count=2),
        _coin(mint="X", liquidity_usd=Decimal("100")),
    )
    for coin in cases:
        verdict = evaluate_anchor(coin, _hot(), cases)
        outcomes.add(verdict.outcome)
        assert verdict.may_ping is (verdict.outcome == STOCK_RUNNER)
    assert len(outcomes) > 1, "the fixtures must exercise more than one outcome"


# --- identity, and the standing rules ----------------------------------------


def test_a_coin_is_never_resolved_from_a_ticker() -> None:
    # v2.43.1's rule, and it matters more here than anywhere: ticker collision
    # across launchpads is expected on this lane rather than exceptional.
    import smart_money_bot.stocks.anchors as anchors_module
    import smart_money_bot.stocks.signal as signal_module

    for module in (anchors_module, signal_module):
        source = inspect.getsource(module)
        assert "def resolve" not in source
        assert "by_ticker" not in source
    # Anchors are keyed by contract address, with the ticker only as a marked
    # last resort.
    assert StockAnchor(ticker="NVDA", token_address="0xAbC").identity_key == "0xabc"
    # Case-folded, because an address written two ways is one address and a
    # ticker written two ways is one ticker.
    assert StockAnchor(ticker="NVDA").identity_key == "ticker:nvda"
    assert StockAnchor(ticker="nvda").identity_key == "ticker:nvda"


def test_the_package_holds_no_provider_database_or_signer() -> None:
    import smart_money_bot.stocks.anchors as anchors_module
    import smart_money_bot.stocks.signal as signal_module

    for module in (anchors_module, signal_module):
        source = inspect.getsource(module)
        for forbidden in (
            "import aiohttp",
            "import requests",
            "aiosqlite",
            "from solders",
            "private_key",
            "cookies=",
            "requests.get",
        ):
            assert forbidden not in source, f"{module.__name__} must stay pure logic"


def test_the_bar_here_is_higher_than_the_memecoin_lane_not_lower() -> None:
    # This lane produces the loudest alert in the product, so every one of the
    # three conditions must be independently capable of refusing it.
    hot, coin = _hot(), _coin()
    assert evaluate_anchor(coin, hot).outcome == STOCK_RUNNER
    config = AnchorConfig()
    assert evaluate_anchor(coin, hot, [_coin(mint="R", liquidity_usd=Decimal("45000"))],
                           config=config).outcome == NOT_THE_LEADER
    assert evaluate_anchor(_coin(anchor_claim=ANCHOR_NAME_ONLY), hot).outcome == CLAIM_UNVERIFIED
    assert evaluate_anchor(coin, _hot(change_percent=Decimal("0.1"), relative_volume=Decimal("1"),
                                      news_sources=0)).outcome == ANCHOR_QUIET
