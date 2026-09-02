"""After the alert, which is where the money is actually lost.

An entry card starts an obligation. The conditions that justified it can stop
holding thirty seconds later, and saying nothing then is worse than never having
sent the card — it leaves someone holding a position the bot has quietly stopped
believing in.

Covers specification tests 36-43. Research and paper only: nothing in the module
under test can buy, sell, sign or broadcast.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

from smart_money_bot.lab.paperlifecycle import (
    ENTRY,
    EXIT_RISK,
    HOLD,
    INVALIDATED,
    PROFIT_PROTECTION,
    R_DEPTH_COLLAPSED,
    R_DISTRIBUTION,
    R_GATES_FAILED,
    R_LIQUIDITY_REMOVED,
    R_SELL_ACCELERATION,
    R_SELL_ROUTE_LOST,
    R_STALE_EVIDENCE,
    R_TRAILING_STOP,
    LifecycleConfig,
    Observation,
    observe,
    open_position,
    replay,
    should_publish,
    summarise,
)

NOW = 1_700_000_000
MINT = "GPR7Ax4kQ2mVn8hLdT6yWc3JbRfE9uZsXqM1oP5tH4dK"


def _entry():
    return open_position(
        MINT, at=NOW, price_usd=Decimal("0.001"),
        market_cap_usd=Decimal("31000"), liquidity_usd=Decimal("60000"),
    )


def _reading(**overrides) -> Observation:
    values = dict(at=NOW + 60, price_usd=Decimal("0.0011"))
    values.update(overrides)
    return Observation(**values)


# ===========================================================================
# 40 — each risk-off condition independently ends or downgrades the position
# ===========================================================================


def test_40_the_exit_closing_outranks_whatever_the_chart_is_doing() -> None:
    """A position you cannot leave is a different problem from one that is down.

    Each of these fires while the *price is up*, which is the point: the most
    dangerous change after an entry is the exit closing while the chart looks
    fine.
    """

    for label, reading, expected in (
        ("sell route lost", _reading(price_usd=Decimal("0.0014"), sell_route_ok=False),
         R_SELL_ROUTE_LOST),
        ("liquidity pulled", _reading(price_usd=Decimal("0.0014"),
                                      liquidity_usd=Decimal("9000")), R_LIQUIDITY_REMOVED),
        ("depth collapsed", _reading(price_usd=Decimal("0.0014"),
                                     sell_impact=Decimal("0.6")), R_DEPTH_COLLAPSED),
        ("gates stopped passing", _reading(price_usd=Decimal("0.0014"), gates_pass=False),
         R_GATES_FAILED),
    ):
        result = observe(_entry(), reading)
        assert result.state == INVALIDATED, label
        assert result.reasons == (expected,), label


def test_40b_distribution_downgrades_without_closing_the_observation() -> None:
    # Somebody leaving is a warning; the exit closing is the end. Keeping them
    # distinct is what makes the louder state mean something.
    insiders = observe(_entry(), _reading(insider_selling=True))
    assert insiders.state == EXIT_RISK
    assert insiders.reasons == (R_DISTRIBUTION,)
    assert insiders.closed is False

    accelerating = observe(_entry(), _reading(sell_to_buy_ratio=Decimal("2.4")))
    assert accelerating.state == EXIT_RISK
    assert accelerating.reasons == (R_SELL_ACCELERATION,)


def test_40c_stale_evidence_is_a_downgrade_not_a_reassurance() -> None:
    result = observe(_entry(), _reading(evidence_age_seconds=5_000))
    assert result.state == EXIT_RISK
    assert result.reasons == (R_STALE_EVIDENCE,)


def test_a_healthy_reading_holds() -> None:
    assert observe(_entry(), _reading()).state == HOLD


# ===========================================================================
# 37, 39 — the full research-only lifecycle
# ===========================================================================


def test_39_a_runner_that_gives_it_back_walks_the_whole_lifecycle() -> None:
    position, cards = replay(
        _entry(),
        [
            Observation(at=NOW + 30, price_usd=Decimal("0.0018")),
            Observation(at=NOW + 60, price_usd=Decimal("0.0022")),
            Observation(at=NOW + 90, price_usd=Decimal("0.0012")),
        ],
    )
    assert cards == (PROFIT_PROTECTION, EXIT_RISK)
    assert position.reasons == (R_TRAILING_STOP,)
    # And the card can state the arithmetic rather than a mood.
    assert position.mfe == Decimal("1.2000")
    assert position.paper_return == Decimal("0.2000")
    line = summarise(position)
    assert "paper 20.00%" in line and "best 120.00%" in line


def test_39b_a_position_sitting_still_produces_one_card_not_sixty() -> None:
    _, cards = replay(_entry(), [_reading(at=NOW + n) for n in range(30, 330, 30)])
    assert cards == (HOLD,)


def test_39c_returning_to_a_state_already_published_stays_silent() -> None:
    # The operator has been told. Telling them again is noise.
    position, cards = replay(
        _entry(),
        [
            Observation(at=NOW + 30, price_usd=Decimal("0.0011")),
            Observation(at=NOW + 60, price_usd=Decimal("0.0011"), insider_selling=True),
            Observation(at=NOW + 90, price_usd=Decimal("0.0011")),
        ],
    )
    assert cards == (HOLD, EXIT_RISK)
    assert position.state == HOLD


def test_37_a_safe_token_whose_edge_is_spent_is_not_an_entry() -> None:
    # It ran, it stopped, nothing is wrong with it. That is its own answer and
    # it is not ENTRY.
    position, _ = replay(
        _entry(),
        [Observation(at=NOW + 30, price_usd=Decimal("0.0016")),
         Observation(at=NOW + 60, price_usd=Decimal("0.00158"))],
    )
    assert position.state == PROFIT_PROTECTION
    assert position.state != ENTRY


# ===========================================================================
# 38, 41 — immutable entry evidence, and restart safety
# ===========================================================================


def test_38_entry_evidence_is_never_rewritten_by_later_data() -> None:
    """"Was this a good call?" is only answerable if the snapshot survives."""

    position = _entry()
    later, _ = replay(
        position,
        [
            Observation(at=NOW + 30, price_usd=Decimal("0.009"),
                        liquidity_usd=Decimal("900000")),
            Observation(at=NOW + 60, price_usd=Decimal("0.004")),
        ],
    )
    assert later.entry_price_usd == position.entry_price_usd == Decimal("0.001")
    assert later.entry_liquidity_usd == Decimal("60000")
    assert later.entry_market_cap_usd == Decimal("31000")
    assert later.entry_at == NOW


def test_41_replaying_the_same_readings_does_not_duplicate_cards() -> None:
    readings = [
        Observation(at=NOW + 30, price_usd=Decimal("0.0011")),
        Observation(at=NOW + 60, price_usd=Decimal("0.0011"), sell_route_ok=False),
    ]
    first, cards_a = replay(_entry(), readings)
    # A restart replays the same history against the same entry.
    _, cards_b = replay(_entry(), readings)
    assert cards_a == cards_b == (HOLD, INVALIDATED)
    # And continuing from the recovered position publishes nothing new.
    _, cards_c = replay(first, readings)
    assert cards_c == ()


def test_41b_readings_are_applied_in_time_order_whatever_order_they_arrive() -> None:
    """Order has to be by timestamp, not by arrival.

    A run followed by a giveback and a giveback followed by a run are different
    outcomes, so this sequence is one where getting the order wrong changes the
    answer — a rising pair would not have caught it, because a running peak is
    order-insensitive.
    """

    ordered = [
        Observation(at=NOW + 30, price_usd=Decimal("0.0022")),  # ran to +120%
        Observation(at=NOW + 60, price_usd=Decimal("0.0012")),  # gave it back
    ]
    forwards, cards_a = replay(_entry(), ordered)
    shuffled, cards_b = replay(_entry(), list(reversed(ordered)))

    assert forwards.state == EXIT_RISK
    assert forwards.reasons == (R_TRAILING_STOP,)
    # Same readings, arriving backwards, must reach the same conclusion.
    assert shuffled.state == forwards.state
    assert cards_a == cards_b
    assert shuffled.last_price_usd == forwards.last_price_usd == Decimal("0.0012")


def test_a_closed_observation_never_reopens() -> None:
    dead = observe(_entry(), _reading(sell_route_ok=False))
    assert dead.closed is True
    revived = observe(dead, _reading(price_usd=Decimal("0.05")))
    assert revived.state == INVALIDATED
    assert revived is dead


def test_should_publish_only_fires_on_a_genuinely_new_state() -> None:
    entry = _entry()
    held = observe(entry, _reading())
    assert should_publish(entry, held) is True
    assert should_publish(held, observe(held, _reading(at=NOW + 120))) is False


# ===========================================================================
# 43, 44 — telemetry surface and the standing prohibition
# ===========================================================================


def test_43_the_card_reports_pnl_excursion_and_drawdown() -> None:
    position, _ = replay(
        _entry(),
        [Observation(at=NOW + 30, price_usd=Decimal("0.002")),
         Observation(at=NOW + 60, price_usd=Decimal("0.0009"))],
    )
    payload = position.to_json()
    for field in ("paper_return", "mfe", "max_drawdown", "entry_price_usd", "giveback"):
        assert payload[field] is not None, field
    assert payload["research_only"] is True


def test_44_nothing_in_the_lifecycle_can_buy_sell_sign_or_broadcast() -> None:
    import smart_money_bot.lab.paperlifecycle as module

    source = inspect.getsource(module)
    for forbidden in (
        "send_transaction", "sign_transaction", "Keypair", "private_key",
        "import aiohttp", "aiosqlite", "place_order", "swap(",
    ):
        assert forbidden not in source, f"a spending path appeared: {forbidden}"


def test_config_thresholds_are_configurable_rather_than_literal() -> None:
    strict = LifecycleConfig(max_drawdown=Decimal("0.1"))
    result = observe(_entry(), _reading(price_usd=Decimal("0.0008")), config=strict)
    assert result.state == INVALIDATED
    # The same reading under production defaults is only a hold.
    assert observe(_entry(), _reading(price_usd=Decimal("0.0008"))).state == HOLD
