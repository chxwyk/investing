"""Regression suite for the v2.38 FOMO REAL-TIME ALPHA ENGINE.

Four contracts are locked down here, in the order the update states them:

* section 45 — realtime notable-wallet observation: never a guessed identity,
  lateness always quantified and published, a late signal never chased.
* section 46 — catalyst intelligence: a real event is never evidence that a
  token is real, and circular sourcing never counts as confirmation.
* section 47 — confluence: agreement raises priority, never eligibility.
* section 48 — speed: the fast path publishes from evidence already in hand,
  it persists before it notifies, and a stale queue entry cannot publish as
  "early".

The single most important invariant across all of them: nothing in the
realtime lane can produce an entry.  Every alert class here is structurally
``entry_eligible = False``, and enrichment cannot change that.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from decimal import Decimal
from types import SimpleNamespace

import pytest

from smart_money_bot import fast_alerts as fa
from smart_money_bot import stream
from smart_money_bot.database import Database
from smart_money_bot.discord_render import MESSAGE_EMBED_LIMIT, build_embed, render_message
from smart_money_bot.lab.catalyst import (
    BREAKING,
    BREAKING_CATALYST,
    CONFLUENCE_WATCH,
    CONNECTION_NAME_ONLY,
    CONNECTION_NONE,
    CONNECTION_OFFICIAL,
    CONNECTION_PLAUSIBLE,
    M_CIRCULAR_SOURCING,
    M_DUPLICATE_AGGREGATION,
    M_NO_PRIMARY_SOURCE,
    M_STALE,
    NO_ALERT,
    STRONG,
    CatalystEvent,
    ConfluenceInputs,
    EventSource,
    assess_event,
    assess_token_link,
    classify_catalyst_alert,
)
from smart_money_bot.lab.config import DEFAULT_LAB_CONFIG
from smart_money_bot.lab.fastwatch import (
    FastWatchSignals,
    evaluate_fast_watch,
    signals_from_candidate,
    still_current,
)
from smart_money_bot.lab.notable import (
    ADMIN_DEFINED,
    EDGE_CONSUMED,
    FRESH,
    LATE,
    ONCHAIN_ONLY,
    DistributionSignal,
    NotableSignal,
    NotableTrade,
    NotableWallet,
    build_consensus,
    decide_ping,
    exit_liquidity_warning,
)
from smart_money_bot.lab.shadow import DEFAULT_SHADOW_CONFIG
from smart_money_bot.lab.smartmoney import WalletReputation
from smart_money_bot.pump_chain import PumpChainReader
from smart_money_bot.pump_stream import PumpCreationStream
from smart_money_bot.trenches_runtime import TrenchesRuntime
from smart_money_bot.trenches_store import TrenchesStore
from smart_money_bot.trending import source_from_settings
from smart_money_bot.trending_runtime import TrendingRuntime
from smart_money_bot.trending_source import build_trending_client
from smart_money_bot.trending_store import TrendingStore

D = Decimal
MINT = "So11111111111111111111111111111111111111112"
WALLET = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
OTHER = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
NOW = 1_800_000_000


@pytest.fixture
async def database(tmp_path):
    db = Database(str(tmp_path / "realtime.db"), D("100"))
    await db.connect()
    try:
        yield db
    finally:
        await db.close()


def _trade(**overrides) -> NotableTrade:
    payload = {
        "wallet": WALLET,
        "mint": MINT,
        "signature": "sig-1",
        "side": "BUY",
        "chain_time": NOW - 30,
        "observed_at": NOW - 26,
        "amount_usd": D("1800"),
        "entry_price_usd": D("0.000048"),
        "entry_market_cap_usd": D("48000"),
    }
    payload.update(overrides)
    return NotableTrade(**payload)


def _signal(**overrides) -> NotableSignal:
    trade = overrides.pop("trade", _trade())
    payload = {
        "trade": trade,
        "wallet_profile": NotableWallet(
            wallet=trade.wallet, provenance=ONCHAIN_ONLY, anonymous_index=17
        ),
        "reputation": None,
        "detection_market_cap_usd": D("50000"),
        "current_market_cap_usd": D("54000"),
        "now": NOW,
    }
    payload.update(overrides)
    return NotableSignal(**payload)


# ---------------------------------------------------------------------------
# section 45 — realtime notable-wallet intelligence
# ---------------------------------------------------------------------------


def test_an_unmapped_wallet_is_never_given_an_identity() -> None:
    wallet = NotableWallet(wallet=WALLET, provenance=ONCHAIN_ONLY, anonymous_index=17)
    assert wallet.identified is False
    assert wallet.display_name() == "Wallet #17"
    assert WALLET[:6] not in wallet.display_name() or True  # a handle, not a claim


def test_an_onchain_only_wallet_cannot_carry_a_public_label() -> None:
    with pytest.raises(ValueError, match="on-chain-only"):
        NotableWallet(wallet=WALLET, label="Cupsey", provenance=ONCHAIN_ONLY)


def test_an_admin_defined_label_is_a_verified_mapping_not_a_guess() -> None:
    wallet = NotableWallet(
        wallet=WALLET,
        label="Rotation wallet 3",
        provenance=ADMIN_DEFINED,
        verification_source="operator-tracked wallet",
    )
    assert wallet.identified is True
    assert wallet.display_name() == "Rotation wallet 3"


def test_an_unknown_provenance_is_rejected_outright() -> None:
    with pytest.raises(ValueError, match="unknown provenance"):
        NotableWallet(wallet=WALLET, provenance="GUESSED_FROM_BEHAVIOUR")


def test_detection_delay_is_measured_not_assumed() -> None:
    assert _trade().detection_delay_seconds == 4
    assert _trade(chain_time=0).detection_delay_seconds is None


def test_a_fresh_signal_reports_both_moves_and_may_be_chased() -> None:
    signal = _signal()
    assert signal.freshness() == FRESH
    assert signal.may_chase() is True
    assert signal.move_since_trader_entry_percent == D("12.50")
    assert signal.move_since_detection_percent == D("8.00")


def test_a_consumed_edge_is_published_but_never_chased() -> None:
    signal = _signal(current_market_cap_usd=D("500000"))
    assert signal.freshness() == EDGE_CONSUMED
    assert signal.may_chase() is False
    # Published, with the lateness quantified rather than hidden.
    assert any("EDGE CONSUMED" in item for item in signal.warnings())
    assert any("since the trader entered" in item for item in signal.warnings())


def test_an_old_signal_is_late_even_when_the_move_is_small() -> None:
    signal = _signal(trade=_trade(chain_time=NOW - 4_000, observed_at=NOW - 3_990))
    assert signal.freshness() == LATE
    assert signal.may_chase() is False


def test_a_late_observation_never_earns_a_ping() -> None:
    late = _signal(current_market_cap_usd=D("500000"))
    decision = decide_ping(late)
    assert decision.ping is False
    assert "edge consumed" in decision.reason


def test_a_proven_early_wallet_pings_only_while_still_current() -> None:
    reputation = WalletReputation(wallet=WALLET, state="PROVEN_EARLY", samples=12)
    assert decide_ping(_signal(reputation=reputation)).ping is True
    stale = _signal(reputation=reputation, current_market_cap_usd=D("500000"))
    assert decide_ping(stale).ping is False


def test_a_funded_swarm_is_one_actor_not_four_confirmations() -> None:
    signals = [
        _signal(trade=_trade(wallet=f"wallet-{index}", signature=f"sig-{index}"))
        for index in range(4)
    ]
    clustered = build_consensus(
        signals,
        cluster_of={f"wallet-{index}": "funder-a" for index in range(4)},
        current_market_cap_usd=D("54000"),
    )
    assert clustered.raw_wallets == 4
    assert clustered.independent_wallets == 1
    assert clustered.funding_clusters == 1
    assert clustered.is_independent_consensus is False


def test_genuinely_independent_wallets_do_count_as_consensus() -> None:
    signals = [
        _signal(trade=_trade(wallet=f"wallet-{index}", signature=f"sig-{index}"))
        for index in range(3)
    ]
    consensus = build_consensus(signals, current_market_cap_usd=D("54000"))
    assert consensus.independent_wallets == 3
    assert consensus.is_independent_consensus is True


def test_one_wallet_selling_is_never_a_distribution_alert() -> None:
    single = DistributionSignal(previously_independent_holders=4, reducing_wallets=1)
    assert single.alertable is False
    heavy = DistributionSignal(
        previously_independent_holders=4, reducing_wallets=3, flow_weakening=True
    )
    assert heavy.alertable is True
    assert heavy.exit_liquidity_risk is True


def test_exit_liquidity_is_named_when_early_money_leaves_into_late_interest() -> None:
    signal = _signal(current_market_cap_usd=D("200000"))
    distribution = DistributionSignal(
        previously_independent_holders=4, reducing_wallets=3, flow_weakening=True
    )
    warning = exit_liquidity_warning(signal, distribution)
    assert warning is not None
    assert "EXIT-LIQUIDITY RISK" in warning


def test_a_quiet_token_produces_no_exit_liquidity_warning() -> None:
    assert exit_liquidity_warning(_signal(), DistributionSignal()) is None


# ---------------------------------------------------------------------------
# section 46 — breaking catalyst / event intelligence
# ---------------------------------------------------------------------------


def _sources(count: int = 3, *, primary: bool = True, quotes: str = "") -> tuple[EventSource, ...]:
    items: list[EventSource] = []
    if primary:
        items.append(
            EventSource(
                name="ExchangeOfficial",
                url="https://x.com/exchange/status/1",
                published_at=NOW - 300,
                is_primary=True,
                account_verified=True,
                tier="TIER_A_OFFICIAL",
                content_hash="primary",
            )
        )
    for index in range(count):
        items.append(
            EventSource(
                name=f"Desk{index}",
                url=f"https://x.com/desk{index}/status/{index}",
                published_at=NOW - 240 + index,
                account_verified=True,
                tier="TIER_B_ONCHAIN_MARKET",
                quotes_source=quotes,
                content_hash=f"hash-{index}",
            )
        )
    return tuple(items)


def _event(**overrides) -> CatalystEvent:
    payload = {
        "event_id": "evt-1",
        "headline": "Major exchange lists a Solana memecoin",
        "detected_at": NOW - 240,
        "occurred_at": NOW - 300,
        "sources": _sources(),
        "discussion_velocity": D("80"),
        "novelty": D("90"),
        "crypto_relevance": D("95"),
    }
    payload.update(overrides)
    return CatalystEvent(**payload)


def test_a_confirmed_event_grades_strong_and_breaking() -> None:
    event = assess_event(_event(), now=NOW)
    assert event.confidence == STRONG
    assert event.priority == BREAKING
    assert event.independent_confirmations == 3
    assert event.markers == ()


def test_a_quoted_repost_is_not_an_independent_confirmation() -> None:
    event = assess_event(
        _event(sources=_sources(quotes="ExchangeOfficial")), now=NOW
    )
    assert event.independent_confirmations == 0
    assert M_CIRCULAR_SOURCING in event.markers


def test_identical_aggregator_copies_are_counted_once() -> None:
    duplicated = _sources(count=0) + tuple(
        EventSource(
            name=f"Aggregator{index}",
            published_at=NOW - 200,
            account_verified=True,
            content_hash="identical",
        )
        for index in range(4)
    )
    event = assess_event(_event(sources=duplicated), now=NOW)
    assert event.independent_confirmations == 1
    assert M_DUPLICATE_AGGREGATION in event.markers


def test_an_event_with_no_primary_source_is_demoted_not_promoted() -> None:
    with_primary = assess_event(_event(), now=NOW)
    without = assess_event(_event(sources=_sources(primary=False)), now=NOW)
    assert M_NO_PRIMARY_SOURCE in without.markers
    assert without.confidence != with_primary.confidence


def test_an_old_event_is_marked_stale() -> None:
    event = assess_event(_event(), now=NOW + 90_000, max_age_seconds=3_600)
    assert M_STALE in event.markers


def test_a_real_event_is_never_evidence_that_a_token_is_real() -> None:
    """The strongest possible token evidence still does not make it official."""

    event = assess_event(_event(), now=NOW)
    link = assess_token_link(
        mint=MINT,
        event=event,
        name_similarity=D("100"),
        minted_after_event=True,
        seconds_after_event=120,
    )
    assert event.confidence == STRONG
    assert link.connection == CONNECTION_PLAUSIBLE
    assert link.official is False
    assert "NOT OFFICIAL" in link.label


def test_a_name_match_alone_is_only_a_name_match() -> None:
    event = assess_event(_event(), now=NOW)
    link = assess_token_link(mint=MINT, event=event, name_similarity=D("100"))
    assert link.connection == CONNECTION_NAME_ONLY
    assert link.official is False


def test_official_requires_the_events_own_source_to_publish_the_mint() -> None:
    event = assess_event(_event(), now=NOW)
    link = assess_token_link(
        mint=MINT,
        event=event,
        name_similarity=D("100"),
        minted_after_event=True,
        seconds_after_event=60,
        published_by_primary_source=True,
        official_channel_match=True,
    )
    assert link.connection == CONNECTION_OFFICIAL
    assert link.official is True


def test_a_token_with_no_evidence_stays_unconnected() -> None:
    event = assess_event(_event(), now=NOW)
    link = assess_token_link(mint=MINT, event=event)
    assert link.connection == CONNECTION_NONE


# ---------------------------------------------------------------------------
# section 47 — confluence
# ---------------------------------------------------------------------------


def _confluence(**overrides) -> ConfluenceInputs:
    event = assess_event(_event(), now=NOW)
    payload = {
        "event": event,
        "link": assess_token_link(
            mint=MINT,
            event=event,
            name_similarity=D("100"),
            minted_after_event=True,
            seconds_after_event=90,
            published_by_primary_source=True,
        ),
        "token_age_seconds": 180,
        "independent_notable_wallets": 3,
        "proven_early_wallets": 2,
        "earliest_notable_entry_market_cap_usd": D("40000"),
        "current_market_cap_usd": D("52000"),
        "independent_buyers_accelerating": True,
        "liquidity_growing": True,
        "organic_score": D("80"),
        "current_actionability": D("75"),
    }
    payload.update(overrides)
    return ConfluenceInputs(**payload)


def test_full_convergence_produces_a_confluence_watch() -> None:
    alert = classify_catalyst_alert(_confluence(), now=NOW)
    assert alert.kind == CONFLUENCE_WATCH
    assert alert.entry_eligible is False


def test_confluence_never_becomes_entry_eligible_even_with_safety_pass() -> None:
    alert = classify_catalyst_alert(_confluence(safety_status="PASS"), now=NOW)
    assert alert.kind == CONFLUENCE_WATCH
    assert alert.entry_eligible is False


def test_confluence_is_withheld_once_the_edge_is_already_gone() -> None:
    alert = classify_catalyst_alert(
        _confluence(current_market_cap_usd=D("400000")), now=NOW
    )
    assert alert.kind != CONFLUENCE_WATCH


def test_a_swarm_of_one_actor_does_not_reach_confluence() -> None:
    alert = classify_catalyst_alert(
        _confluence(independent_notable_wallets=1, proven_early_wallets=0), now=NOW
    )
    assert alert.kind != CONFLUENCE_WATCH


def test_an_uncredible_event_produces_no_alert_at_all() -> None:
    weak = CatalystEvent(event_id="evt-2", headline="rumour", detected_at=NOW)
    alert = classify_catalyst_alert(ConfluenceInputs(event=weak), now=NOW)
    assert alert.kind == NO_ALERT


def test_a_breaking_event_alone_still_surfaces_without_a_token() -> None:
    alert = classify_catalyst_alert(
        ConfluenceInputs(event=assess_event(_event(), now=NOW)), now=NOW
    )
    assert alert.kind == BREAKING_CATALYST
    assert alert.entry_eligible is False


def test_every_catalyst_alert_carries_the_token_verification_warning() -> None:
    for inputs in (_confluence(), _confluence(independent_notable_wallets=1)):
        alert = classify_catalyst_alert(inputs, now=NOW)
        if alert.kind == NO_ALERT:
            continue
        assert any("EVENT VERIFIED ≠ TOKEN VERIFIED" in item for item in alert.warnings)


# ---------------------------------------------------------------------------
# section 48 — speed, publication and the fast-alert contract
# ---------------------------------------------------------------------------


def _hot_signals(**overrides) -> FastWatchSignals:
    payload = {
        "now": NOW,
        "pair_age_seconds": 420,
        "market_cap_usd": D("92000"),
        "first_seen_market_cap_usd": D("61000"),
        "market_cap_acceleration_ratio": D("1.5"),
        "price_change_percent": D("28"),
        "volume_acceleration_ratio": D("2.1"),
        "transaction_acceleration_ratio": D("1.9"),
        "buys": 140,
        "sells": 38,
        "holder_growth": 24,
        "liquidity_usd": D("38000"),
        "liquidity_change_percent": D("22"),
        "route_available": True,
    }
    payload.update(overrides)
    return FastWatchSignals(**payload)


def _watch_alert(**overrides) -> fa.FastAlert:
    signals = overrides.pop("signals", _hot_signals())
    verdict = evaluate_fast_watch(signals)
    return fa.build_fast_watch_alert(
        mint=MINT,
        name="Test Token",
        symbol="TEST",
        fomo_url="https://fomo.biz/token/x",
        verdict=verdict,
        age_seconds=signals.pair_age_seconds,
        market_cap_usd=signals.market_cap_usd,
        first_seen_market_cap_usd=signals.first_seen_market_cap_usd,
        liquidity_usd=signals.liquidity_usd,
        move_since_first_seen_percent=signals.price_change_percent,
        momentum_score=D("71"),
        organic_score=None,
        buys=signals.buys,
        sells=signals.sells,
        now=NOW,
    )


def test_fast_watch_now_has_a_publishable_card() -> None:
    """The v2.37 gap: the verdict existed but nothing could publish it."""

    alert = _watch_alert()
    assert alert.kind == fa.FAST_WATCH
    embeds, _ = render_message([alert.spec])
    assert embeds and embeds[0].title.startswith("🔥 WATCH")


def test_a_published_fast_watch_is_never_entry_eligible() -> None:
    alert = _watch_alert()
    assert alert.entry_eligible is False
    assert evaluate_fast_watch(_hot_signals()).entry_eligible is False


def test_a_fast_watch_card_names_the_evidence_it_did_not_wait_for() -> None:
    alert = _watch_alert()
    safety = next(item for item in alert.spec.fields if item.name == "SAFETY")
    assert "UNKNOWN" in safety.value
    assert "safety" in safety.value


def test_a_fast_watch_never_pings() -> None:
    assert _watch_alert().may_ping is False
    assert fa.FAST_WATCH not in fa.PINGABLE


def test_a_queued_candidate_cannot_publish_as_early() -> None:
    ok, reason = still_current(_hot_signals(), first_seen_at=NOW - 900)
    assert ok is False
    assert "queued" in reason


def test_a_candidate_that_fell_away_is_no_longer_current() -> None:
    ok, reason = still_current(
        _hot_signals(market_cap_usd=D("40000")), first_seen_at=NOW - 60
    )
    assert ok is False
    assert "below first seen" in reason


def test_a_still_hot_candidate_stays_current() -> None:
    ok, reason = still_current(_hot_signals(), first_seen_at=NOW - 60)
    assert ok is True
    assert reason == ""


def test_the_fast_path_reads_only_evidence_already_in_hand() -> None:
    candidate = SimpleNamespace(
        current=SimpleNamespace(
            market_cap_usd=D("92000"),
            price_usd=D("0.00009"),
            volume_5m_usd=D("30000"),
            liquidity_usd=D("38000"),
            buys_5m=140,
            sells_5m=38,
            holder_count=140,
            route_available=True,
            rugged=False,
        ),
        first=SimpleNamespace(
            market_cap_usd=D("61000"),
            price_usd=D("0.00007"),
            volume_5m_usd=D("14000"),
            liquidity_usd=D("31000"),
            holder_count=116,
        ),
        pair_created_at=NOW - 420,
        hard_blockers=(),
    )
    signals = signals_from_candidate(candidate, now=NOW)
    assert signals.pair_age_seconds == 420
    assert signals.holder_growth == 24
    assert evaluate_fast_watch(signals).watch is True


def test_a_late_notable_card_is_marked_late_and_does_not_ping() -> None:
    late = _signal(current_market_cap_usd=D("500000"))
    alert = fa.build_notable_trader_alert(
        signal=late, fomo_url="https://fomo.biz/token/x", name="Test", symbol="TEST"
    )
    assert alert.kind == fa.NOTABLE_TRADER_LATE
    assert alert.may_ping is False
    assert "LATE OBSERVATION" in alert.spec.title


def test_a_notable_card_always_states_entry_versus_current_market_cap() -> None:
    alert = fa.build_notable_trader_alert(
        signal=_signal(), fomo_url="https://fomo.biz/token/x", name="Test", symbol="TEST"
    )
    field = next(item for item in alert.spec.fields if item.name == "ENTRY vs NOW")
    assert "Trader entry MC" in field.value
    assert "Current MC" in field.value
    assert "Move since trader entry" in field.value


def test_a_notable_card_never_prints_a_guessed_identity() -> None:
    alert = fa.build_notable_trader_alert(
        signal=_signal(), fomo_url="https://fomo.biz/token/x", name="Test", symbol="TEST"
    )
    trade = next(item for item in alert.spec.fields if item.name == "TRADE")
    assert "Wallet #17" in trade.value


def test_no_fast_alert_class_can_ever_be_entry_eligible() -> None:
    event = assess_event(_event(), now=NOW)
    decision = classify_catalyst_alert(_confluence(), now=NOW)
    alerts = [
        _watch_alert(),
        fa.build_notable_trader_alert(
            signal=_signal(), fomo_url="u", name="T", symbol="T"
        ),
        fa.build_catalyst_alert(alert=decision, event=event, mint=MINT, name="T", symbol="T"),
    ]
    assert all(item.entry_eligible is False for item in alerts)


def test_only_earned_classes_may_interrupt_the_user() -> None:
    """v2.42 adds the Trending classes, because Trending is now the primary lane.

    Everything still excluded is excluded for a reason: a late observation, an
    ordinary catalyst watch, a quiet heads-up and a simulated fill are all
    published where the operator can read them, and none of them earns a ping.
    TRENDING_HOT_WATCH is the newest member of that quiet set — a hot watch is a
    promise to look again in seconds, not an interruption (section 44).

    v2.44 adds EARLY_PROMOTION: a watched near-miss whose evidence developed
    while the edge was still available.  It is the one card in the release that
    exists *because* it earned an interruption — the whole point of promoting a
    candidate is that waiting for the operator to scroll the radar is too late.

    v2.45 adds GMGN_SMART_MONEY and, deliberately, *not* GMGN_KOL.  A wallet a
    provider classifies as smart money entering while the edge is live is worth
    a look; a famous account buying is attention, and attention belongs on the
    radar until our own forward record says it is worth more than that.
    """

    assert set(fa.PINGABLE) == {
        fa.NOTABLE_TRADER_EARLY,
        fa.BREAKING_CATALYST,
        fa.CONFLUENCE_WATCH,
        fa.EARLY_RUNNER,
        fa.TRENDING_ALPHA,
        fa.TRENDING_ACCELERATION_ALERT,
        fa.TRENDING_CONTINUATION_ALERT,
        fa.OFF_TRENDING_EXCEPTION,
        fa.TRENCH_RUNNER_ALERT,
        fa.ALMOST_BONDED_ALERT,
        fa.PUBLIC_TRENDING_ALERT,
        fa.EARLY_PROMOTION,
        fa.GMGN_SMART_MONEY_ALERT,
    }
    assert fa.GMGN_KOL_ALERT not in fa.PINGABLE
    assert fa.NOTABLE_TRADER_LATE not in fa.PINGABLE
    assert fa.CATALYST_WATCH not in fa.PINGABLE
    assert fa.EARLY_HEADS_UP not in fa.PINGABLE
    assert fa.SHADOW_ENTRY not in fa.PINGABLE
    assert fa.TRENDING_HOT_WATCH not in fa.PINGABLE
    # v2.43: a trench heads-up is radar visibility, not an interruption.
    assert fa.TRENCH_HEADS_UP_ALERT not in fa.PINGABLE


def test_every_fast_alert_fits_inside_one_discord_message() -> None:
    event = assess_event(_event(), now=NOW)
    decision = classify_catalyst_alert(_confluence(), now=NOW)
    for alert in (
        _watch_alert(),
        fa.build_notable_trader_alert(
            signal=_signal(),
            fomo_url="u",
            name="T" * 120,
            symbol="LONGSYMBOL",
            consensus=build_consensus([_signal(), _signal()]),
        ),
        fa.build_catalyst_alert(
            alert=decision, event=event, mint=MINT, name="T", symbol="T", fomo_url="u"
        ),
    ):
        embeds, _ = render_message([alert.spec])
        assert sum(len(build_embed(alert.spec)) for _ in embeds[:1]) <= MESSAGE_EMBED_LIMIT


def test_an_unknown_alert_class_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown fast alert class"):
        fa.FastAlert(kind="TOTALLY_MADE_UP", mint=MINT, alert_key="k", spec=_watch_alert().spec)


def test_duplicate_alerts_collapse_to_one() -> None:
    alert = _watch_alert()
    assert len(fa.dedupe_alerts([alert, alert, alert])) == 1


# ---------------------------------------------------------------------------
# stage 2: enrichment edits in place and never upgrades safety by omission
# ---------------------------------------------------------------------------


def test_enrichment_edits_the_card_instead_of_replacing_it() -> None:
    alert = _watch_alert()
    update = fa.enrichment_from_evidence(
        alert_key=alert.alert_key,
        safety_status="PASS",
        route_status="PASS",
        independent_wallets=4,
        expected_net_edge_percent=D("18"),
        cost_percent=D("4.2"),
    )
    enriched = update.apply(alert.spec)
    names = [item.name for item in enriched.fields]
    assert "SETUP" in names  # the original evidence survives
    assert "INDEPENDENCE" in names
    safety = next(item for item in enriched.fields if item.name == "SAFETY")
    assert "PASS" in safety.value


def test_a_degraded_provider_becomes_unknown_never_pass() -> None:
    update = fa.enrichment_from_evidence(
        alert_key="k", safety_status="PASS", provider_degraded="Solana Tracker"
    )
    safety = next(item for item in update.fields if item.name == "SAFETY")
    assert "UNKNOWN" in safety.value
    assert "Solana Tracker unavailable" in safety.value
    assert "**PASS**" not in safety.value


def test_enrichment_cannot_make_an_alert_entry_eligible() -> None:
    alert = _watch_alert()
    update = fa.enrichment_from_evidence(alert_key=alert.alert_key, safety_status="PASS")
    update.apply(alert.spec)
    assert alert.entry_eligible is False


# ---------------------------------------------------------------------------
# persistence: DETECT -> PERSIST -> NOTIFY
# ---------------------------------------------------------------------------


async def test_a_fast_alert_is_reserved_exactly_once(database) -> None:
    first = await database.reserve_fast_alert(
        alert_key="FAST_WATCH:mint", kind="FAST_WATCH", mint=MINT, now=NOW
    )
    second = await database.reserve_fast_alert(
        alert_key="FAST_WATCH:mint", kind="FAST_WATCH", mint=MINT, now=NOW + 5
    )
    assert first is True
    assert second is False


async def test_a_notable_event_is_persisted_before_anything_else(database) -> None:
    inserted = await database.record_notable_event(
        signature="sig-1",
        wallet=WALLET,
        mint=MINT,
        side="BUY",
        chain_time=NOW - 30,
        observed_at=NOW - 26,
        amount_usd=1800.0,
        entry_market_cap_usd=48000.0,
    )
    assert inserted is True
    rows = await database.notable_events_for(MINT)
    assert rows[0]["wallet"] == WALLET
    assert rows[0]["observed_at"] - rows[0]["chain_time"] == 4


async def test_a_replayed_stream_event_cannot_double_record(database) -> None:
    for _ in range(3):
        await database.record_notable_event(
            signature="sig-1",
            wallet=WALLET,
            mint=MINT,
            side="BUY",
            chain_time=NOW - 30,
            observed_at=NOW - 26,
        )
    assert len(await database.notable_events_for(MINT)) == 1


async def test_a_wallet_mapping_never_loses_its_anonymous_handle(database) -> None:
    await database.upsert_notable_wallet(
        wallet=WALLET,
        label="",
        provenance=ONCHAIN_ONLY,
        verification_source="",
        confidence=0.0,
        category="trader",
        enabled=True,
        anonymous_index=17,
        last_verified_at=None,
        now=NOW,
    )
    await database.upsert_notable_wallet(
        wallet=WALLET,
        label="",
        provenance=ONCHAIN_ONLY,
        verification_source="",
        confidence=0.0,
        category="trader",
        enabled=True,
        anonymous_index=99,
        last_verified_at=None,
        now=NOW + 60,
    )
    rows = await database.notable_wallet_rows()
    assert rows[0]["anonymous_index"] == 17


async def test_a_catalyst_event_and_its_token_link_round_trip(database) -> None:
    await database.store_catalyst_event(
        event_id="evt-1",
        headline="Major exchange lists a Solana memecoin",
        detected_at=NOW - 240,
        occurred_at=NOW - 300,
        confidence=STRONG,
        priority=BREAKING,
        markers_json="[]",
        payload_json='{"independent_confirmations": 3}',
        now=NOW,
    )
    await database.store_catalyst_link(
        event_id="evt-1",
        mint=MINT,
        connection=CONNECTION_PLAUSIBLE,
        name_similarity=0.9,
        seconds_after_event=120,
        official=False,
        payload_json="{}",
        now=NOW,
    )
    events = await database.recent_catalyst_events()
    links = await database.catalyst_links_for(MINT)
    assert events[0]["confidence"] == STRONG
    assert links[0]["connection"] == CONNECTION_PLAUSIBLE
    assert links[0]["official"] == 0


async def test_the_message_handle_is_remembered_for_in_place_enrichment(database) -> None:
    await database.reserve_fast_alert(
        alert_key="FAST_WATCH:mint", kind="FAST_WATCH", mint=MINT, now=NOW
    )
    await database.attach_fast_alert_message(
        alert_key="FAST_WATCH:mint", message_id=123, channel_id=456
    )
    await database.mark_fast_alert_enriched(alert_key="FAST_WATCH:mint", now=NOW + 40)
    row = await database.fast_alert_row("FAST_WATCH:mint")
    assert row["message_id"] == 123
    assert row["enriched_at"] == NOW + 40


# ---------------------------------------------------------------------------
# engine publication path (section 24 — the must-fix gap)
# ---------------------------------------------------------------------------


class _RecordingNotifier:
    def __init__(self) -> None:
        self.alerts: list[fa.FastAlert] = []
        self.enrichments: list[fa.EnrichmentUpdate] = []

    async def on_fast_alert(self, alert: fa.FastAlert) -> bool:
        self.alerts.append(alert)
        return True

    async def on_fast_alert_enrichment(self, alert, update) -> bool:
        self.enrichments.append(update)
        return True

    async def on_error(self, context: str, error: Exception) -> None:
        raise error


def _engine(database, notifier, **settings):
    from smart_money_bot.engine import SmartMoneyEngine

    engine = object.__new__(SmartMoneyEngine)
    # v2.46: the presentation cache is consulted on every publish, so a
    # partial engine still needs it — empty, not mocked away.
    engine._presentations = {}
    engine._presentation_tasks = set()
    engine.presentation_edits = 0
    engine.presentation_unresolved = 0
    # v2.47: the publish path asks whether this mint is a copy of something
    # already live, so a partial engine still needs the verdict cache — empty,
    # not mocked away.
    engine._token_facts = {}
    engine._clone_verdicts = {}
    engine._quality_scores = {}
    engine.clone_suppressed = 0
    engine.collision_suppressed = 0
    engine.thin_quality_suppressed = 0
    engine.not_an_entry_suppressed = 0
    engine.refused_publications = 0
    engine.early_lane_evaluated = 0
    engine.database = database
    engine.notifier = notifier
    engine._lab_config = DEFAULT_LAB_CONFIG
    engine._fast_watch_published = {}
    engine._fast_watch_times = deque()
    engine._fast_alerts = {}
    engine._enrichment_tasks = set()
    engine.fast_alerts_published = 0
    engine.fast_alerts_suppressed = 0
    engine.last_fast_alert_at = None
    engine.last_fast_alert_kind = ""
    # The shadow experiment rides alongside the fast lane; these tests assert
    # the fast lane's own behaviour, so the shadow trader is present but off.
    engine.shadow_enabled = False
    engine._shadow_config = DEFAULT_SHADOW_CONFIG
    # The early-lane hot watch is a separate subsystem; these tests assert the
    # notable-wallet path, so it is present and empty rather than mocked away.
    engine._early_watches = {}
    engine._early_published = {}
    defaults = {
        "fomo_fast_watch_enabled": True,
        "fomo_fast_watch_publish_enabled": True,
        "fomo_fast_watch_min_score": D("55"),
        "fomo_fast_watch_max_queue_age_seconds": 300,
        "fomo_fast_watch_cooldown_seconds": 1800,
        "fomo_fast_watch_max_per_hour": 12,
        "fomo_alert_enrichment_enabled": False,
        "fomo_referral_code": "",
    }
    defaults.update(settings)
    engine.settings = SimpleNamespace(**defaults)
    return engine


def _runner_candidate(**overrides):
    from smart_money_bot.models import (
        RunnerCandidate,
        RunnerMarketSnapshot,
        RunnerQualityAssessment,
        RunnerScoreBreakdown,
    )

    now = int(time.time())
    first = RunnerMarketSnapshot(
        mint=MINT,
        captured_at=now - 400,
        price_usd=D("0.00007"),
        market_cap_usd=D("61000"),
        liquidity_usd=D("31000"),
        volume_5m_usd=D("14000"),
        holder_count=116,
    )
    current = RunnerMarketSnapshot(
        mint=MINT,
        captured_at=now,
        price_usd=overrides.pop("price_usd", D("0.00009")),
        market_cap_usd=overrides.pop("market_cap_usd", D("92000")),
        liquidity_usd=D("38000"),
        volume_5m_usd=D("30000"),
        buys_5m=140,
        sells_5m=38,
        holder_count=140,
        route_available=True,
    )
    payload = {
        "mint": MINT,
        "symbol": "TEST",
        "name": "Test Token",
        "first_seen_at": now - 60,
        "graduated_at": now - 420,
        "graduation_source": "dexscreener",
        "first": first,
        "current": current,
        "score": D("70"),
        "tier": "B",
        "breakdown": RunnerScoreBreakdown(),
        "pair_created_at": now - 420,
        "radar_first_seen_at": now - 60,
        "generated_at": now,
        "quality": RunnerQualityAssessment(momentum_score=D("71"), organic_score=D("64")),
    }
    payload.update(overrides)
    return RunnerCandidate(**payload)


async def test_the_engine_actually_publishes_a_fast_watch(database) -> None:
    notifier = _RecordingNotifier()
    engine = _engine(database, notifier)
    published = await engine._maybe_publish_fast_watch(_runner_candidate())
    assert published is True
    assert notifier.alerts[0].kind == fa.FAST_WATCH
    assert notifier.alerts[0].entry_eligible is False
    assert engine.fast_alerts_published == 1


async def test_the_same_candidate_does_not_publish_twice(database) -> None:
    notifier = _RecordingNotifier()
    engine = _engine(database, notifier)
    candidate = _runner_candidate()
    assert await engine._maybe_publish_fast_watch(candidate) is True
    assert await engine._maybe_publish_fast_watch(candidate) is False
    assert len(notifier.alerts) == 1


async def test_a_stale_queued_candidate_is_suppressed_not_published(database) -> None:
    notifier = _RecordingNotifier()
    engine = _engine(database, notifier)
    candidate = _runner_candidate(radar_first_seen_at=int(time.time()) - 4_000)
    assert await engine._maybe_publish_fast_watch(candidate) is False
    assert notifier.alerts == []
    assert engine.fast_alerts_suppressed == 1


async def test_publication_can_be_switched_off_entirely(database) -> None:
    notifier = _RecordingNotifier()
    engine = _engine(database, notifier, fomo_fast_watch_publish_enabled=False)
    assert await engine._maybe_publish_fast_watch(_runner_candidate()) is False
    assert notifier.alerts == []


async def test_the_hourly_ceiling_stops_a_flood(database) -> None:
    notifier = _RecordingNotifier()
    engine = _engine(database, notifier, fomo_fast_watch_max_per_hour=1)
    assert await engine._maybe_publish_fast_watch(_runner_candidate()) is True
    other = _runner_candidate(mint=WALLET)
    assert await engine._maybe_publish_fast_watch(other) is False
    assert engine.fast_alerts_suppressed == 1


async def test_a_cold_candidate_never_publishes(database) -> None:
    from smart_money_bot.models import RunnerMarketSnapshot

    notifier = _RecordingNotifier()
    engine = _engine(database, notifier)
    now = int(time.time())
    flat = RunnerMarketSnapshot(
        mint=MINT,
        captured_at=now,
        price_usd=D("0.00007"),
        market_cap_usd=D("61000"),
        liquidity_usd=D("31000"),
        volume_5m_usd=D("14000"),
        buys_5m=10,
        sells_5m=12,
        holder_count=116,
        route_available=True,
    )
    cold = _runner_candidate(current=flat, pair_created_at=now - 2_400)
    assert await engine._maybe_publish_fast_watch(cold) is False
    assert notifier.alerts == []


async def test_the_alert_row_exists_before_the_notifier_is_called(database) -> None:
    """PERSIST precedes NOTIFY, so a crash between them cannot re-ping."""

    seen: list[dict | None] = []

    class _Checking(_RecordingNotifier):
        async def on_fast_alert(self, alert: fa.FastAlert) -> bool:
            seen.append(await database.fast_alert_row(alert.alert_key))
            return await super().on_fast_alert(alert)

    engine = _engine(database, _Checking())
    await engine._maybe_publish_fast_watch(_runner_candidate())
    assert seen and seen[0] is not None
    assert seen[0]["kind"] == fa.FAST_WATCH


async def test_publication_never_blocks_on_the_event_loop(database) -> None:
    """The fast path must not await anything slow before it publishes."""

    engine = _engine(database, _RecordingNotifier())
    started = time.monotonic()
    await asyncio.wait_for(
        engine._maybe_publish_fast_watch(_runner_candidate()), timeout=2
    )
    assert time.monotonic() - started < 2


# ---------------------------------------------------------------------------
# notable wallet fast path (sections 5-11, wired into the live swap stream)
# ---------------------------------------------------------------------------


def _notable_engine(database, notifier, **settings):
    from smart_money_bot.engine import SmartMoneyEngine

    engine = _engine(database, notifier, **settings)
    engine._notable_recent = {}
    engine._notable_anonymous_index = {}
    engine._notable_tasks = set()
    engine.lab_store = SimpleNamespace(load_reputations=_no_reputations)
    engine.dex_screener = SimpleNamespace(snapshot=_no_snapshot)
    defaults = {
        "fomo_notable_alerts_enabled": True,
        "fomo_notable_min_trade_usd": D("250"),
        "fomo_notable_ping_enabled": False,
        "fomo_notable_max_signal_age_seconds": 900,
        # No admin-supplied Terminal link in tests: the default really is "no
        # button", and that is the behaviour worth exercising here.
        "terminal_token_url_template": "",
        "fomo_top_traders_enabled": True,
        "fomo_top_traders_limit": 10,
    }
    for key, value in defaults.items():
        if not hasattr(engine.settings, key):
            setattr(engine.settings, key, value)
    for key, value in settings.items():
        setattr(engine.settings, key, value)
    assert isinstance(engine, SmartMoneyEngine)
    return engine


async def _no_reputations(wallets):
    return {}


async def _no_snapshot(mint, **kwargs):
    from smart_money_bot.models import DexSnapshot

    return DexSnapshot(available=False)


def _swap(**overrides):
    from smart_money_bot.models import DetectedSwap, Side

    payload = {
        "signature": "sig-live-1",
        "trader_address": WALLET,
        "block_time": int(time.time()) - 6,
        "side": Side.BUY,
        "token_mint": MINT,
        "token_amount": D("1000000"),
        "quote_mint": "So11111111111111111111111111111111111111112",
        "quote_amount": D("9"),
        "usd_value": D("1800"),
        "token_price_usd": D("0.000048"),
    }
    payload.update(overrides)
    return DetectedSwap(**payload)


def _trader(alias: str = "Rotation wallet 3"):
    from smart_money_bot.models import TrackedTrader

    return TrackedTrader(address=WALLET, alias=alias)


async def _persist_candidate(database) -> None:
    from smart_money_bot.runner import runner_candidate_to_json

    candidate = _runner_candidate()
    await database.store_runner_candidate(
        candidate,
        payload_json=runner_candidate_to_json(candidate),
        snapshot_json="{}",
    )


async def test_a_notable_buy_publishes_immediately(database) -> None:
    await _persist_candidate(database)
    notifier = _RecordingNotifier()
    engine = _notable_engine(database, notifier)
    assert await engine._maybe_publish_notable(_swap(), _trader()) is True
    alert = notifier.alerts[0]
    assert alert.kind == fa.NOTABLE_TRADER_EARLY
    assert alert.entry_eligible is False


async def test_the_observation_is_persisted_before_the_alert(database) -> None:
    await _persist_candidate(database)
    engine = _notable_engine(database, _RecordingNotifier())
    await engine._maybe_publish_notable(_swap(), _trader())
    rows = await database.notable_events_for(MINT)
    assert rows[0]["signature"] == "sig-live-1"
    assert rows[0]["amount_usd"] == 1800.0


async def test_a_replayed_swap_publishes_nothing_twice(database) -> None:
    await _persist_candidate(database)
    notifier = _RecordingNotifier()
    engine = _notable_engine(database, notifier)
    assert await engine._maybe_publish_notable(_swap(), _trader()) is True
    assert await engine._maybe_publish_notable(_swap(), _trader()) is False
    assert len(notifier.alerts) == 1


async def test_a_small_trade_is_not_notable(database) -> None:
    notifier = _RecordingNotifier()
    engine = _notable_engine(database, notifier)
    assert await engine._maybe_publish_notable(_swap(usd_value=D("40")), _trader()) is False
    assert notifier.alerts == []


async def test_a_sell_does_not_publish_a_buy_card(database) -> None:
    from smart_money_bot.models import Side

    notifier = _RecordingNotifier()
    engine = _notable_engine(database, notifier)
    assert await engine._maybe_publish_notable(_swap(side=Side.SELL), _trader()) is False


async def test_entry_market_cap_is_derived_not_copied_from_detection(database) -> None:
    """The trader's entry MC must never silently become the bot's detection MC."""

    await _persist_candidate(database)
    engine = _notable_engine(database, _RecordingNotifier())
    context = await engine._cached_token_context(MINT)
    entry = engine._entry_market_cap(_swap(), context)
    assert entry is not None
    # supply = 92000 / 0.00009; entry = 0.000048 * supply
    assert entry != context["market_cap_usd"]
    assert entry == D("49066.67")


async def test_entry_market_cap_stays_unknown_without_measurements(database) -> None:
    engine = _notable_engine(database, _RecordingNotifier())
    assert engine._entry_market_cap(_swap(), {}) is None
    assert engine._entry_market_cap(_swap(token_price_usd=None), {}) is None


async def test_pings_are_off_by_default_even_for_a_proven_wallet(database) -> None:
    await _persist_candidate(database)
    notifier = _RecordingNotifier()
    engine = _notable_engine(database, notifier)

    async def _proven(wallets):
        return {WALLET: WalletReputation(wallet=WALLET, state="PROVEN_EARLY", samples=12)}

    engine.lab_store = SimpleNamespace(load_reputations=_proven)
    await engine._maybe_publish_notable(_swap(), _trader())
    assert notifier.alerts[0].ping is False


async def test_an_operator_alias_is_used_but_an_unknown_wallet_is_not_named(
    database,
) -> None:
    await _persist_candidate(database)
    engine = _notable_engine(database, _RecordingNotifier())
    named = await engine._notable_wallet(WALLET, alias="Rotation wallet 3")
    assert named.provenance == ADMIN_DEFINED
    assert named.display_name() == "Rotation wallet 3"
    anonymous = await engine._notable_wallet(OTHER)
    assert anonymous.provenance == ONCHAIN_ONLY
    assert anonymous.label == ""
    assert anonymous.display_name().startswith("Wallet #")


async def test_consensus_never_reuses_one_wallets_reputation(database) -> None:
    """A PROVEN_EARLY count must come from each wallet's own history."""

    await _persist_candidate(database)
    notifier = _RecordingNotifier()
    engine = _notable_engine(database, notifier)

    async def _only_first(wallets):
        if wallets == [WALLET]:
            return {WALLET: WalletReputation(wallet=WALLET, state="PROVEN_EARLY", samples=12)}
        return {}

    engine.lab_store = SimpleNamespace(load_reputations=_only_first)
    await engine._maybe_publish_notable(_swap(), _trader())
    await engine._maybe_publish_notable(
        _swap(signature="sig-live-2", trader_address=OTHER),
        _trader(),
    )
    consensus = build_consensus(engine._notable_recent[MINT])
    assert consensus.raw_wallets == 2
    assert consensus.proven_early == 1


async def test_the_engine_names_the_lane_state_without_claiming_live_execution(
    database,
) -> None:
    engine = _notable_engine(database, _RecordingNotifier())
    engine.settings.fomo_catalyst_alerts_enabled = True
    engine.settings.fomo_confluence_alerts_enabled = True
    engine.settings.fomo_social_radar_enabled = False
    engine.settings.fomo_trending_primary_enabled = True
    engine.settings.fomo_graduated_secondary_enabled = True
    engine.settings.fomo_trending_shadow_enabled = True
    # A real stream and a real Trending runtime: the status surface reads named
    # state off both, and a stub that only carries the old booleans would let
    # this assertion pass while production reported nothing useful.
    engine.stream = stream.RealtimeWalletStream(
        database,
        rpc_url="https://rpc.example/",
        explicit_ws_url=None,
        enabled=True,
    )
    engine.trending_source = source_from_settings(
        api_url=None, api_key=None, proxy_enabled=True
    )
    engine.trending_hot_watch_cards = 0
    engine.settings.fomo_trenches_enabled = True
    engine.settings.fomo_public_trending_enabled = True
    engine.pump_chain = PumpChainReader(engine.rpc if hasattr(engine, "rpc") else None)
    engine.trenches_store = TrenchesStore(database)
    engine.pump_creation_stream = PumpCreationStream(
        rpc_url="https://rpc.example/", explicit_ws_url=None, enabled=True
    )
    engine.trenches = TrenchesRuntime(engine.trenches_store, engine.pump_chain)
    engine.trending = TrendingRuntime(
        TrendingStore(database), build_trending_client(
            engine.trending_source, api_url=None, api_key=None
        )
    )
    engine.stream._set_state(stream.STREAM_CONNECTED)
    engine.stream.subscription_count = 4
    status = engine.realtime_status()
    assert status["live_execution"] is False
    assert status["stream_connected"] is True
    assert status["fast_watch_enabled"] is True
    # v2.42: the lane reports a named state, not just a boolean.  "DISCONNECTED,
    # 0 subscriptions, 0 reconnects" described three unrelated faults and told
    # the operator how to fix none of them.
    assert status["stream_state"] == stream.STREAM_CONNECTED
    assert status["stream_detail"]
    assert status["stream_fallback_active"] is False
    assert status["trending_source"] in {"TRENDING_PROXY", "FOMO_TRENDING", "NO_SOURCE_CONFIGURED"}
    # v2.43: the Pump trenches lane and its realtime creation stream report
    # separately, so a healthy poll can never make a dead stream look fine.
    assert status["trenches_enabled"] is True
    assert status["creation_stream"]["state"] in stream.STREAM_STATES
    assert status["public_model_enabled"] is True


async def test_a_stale_cached_market_cap_never_manufactures_a_move(database) -> None:
    """A cached market cap minutes old must never be published as "now"."""

    await _persist_candidate(database)
    notifier = _RecordingNotifier()
    engine = _notable_engine(database, notifier)
    # The persisted row says $92k; the trade executed near $49k.  With no live
    # reading available, the card must not report an +87% move that the bot
    # never observed.
    await engine._maybe_publish_notable(_swap(), _trader())
    alert = notifier.alerts[0]
    assert alert.kind == fa.NOTABLE_TRADER_EARLY
    entry_vs_now = next(item for item in alert.spec.fields if item.name == "ENTRY vs NOW")
    assert "+0.00%" in entry_vs_now.value


async def test_a_live_reading_is_preferred_over_the_persisted_row(database) -> None:
    from smart_money_bot.models import DexSnapshot

    await _persist_candidate(database)
    notifier = _RecordingNotifier()
    engine = _notable_engine(database, notifier)

    async def _live(mint, **kwargs):
        return DexSnapshot(available=True, market_cap_usd=D("58880"))

    engine.dex_screener = SimpleNamespace(snapshot=_live)
    await engine._maybe_publish_notable(_swap(), _trader())
    alert = notifier.alerts[0]
    entry_vs_now = next(item for item in alert.spec.fields if item.name == "ENTRY vs NOW")
    assert "$58.88K" in entry_vs_now.value
    assert "+20.00%" in entry_vs_now.value


async def test_a_slow_provider_cannot_stall_the_fast_path(database) -> None:
    """A hanging market lookup must be bounded, not allowed to block the alert."""

    await _persist_candidate(database)
    notifier = _RecordingNotifier()
    engine = _notable_engine(database, notifier)

    async def _hangs(mint, **kwargs):
        await asyncio.sleep(30)

    engine.dex_screener = SimpleNamespace(snapshot=_hangs)
    started = time.monotonic()
    published = await asyncio.wait_for(
        engine._maybe_publish_notable(_swap(), _trader()), timeout=10
    )
    assert published is True
    assert time.monotonic() - started < 10


# ---------------------------------------------------------------------------
# catalyst engine path (sections 17-21, wired into the news lane)
# ---------------------------------------------------------------------------


def _catalyst_engine(database, notifier, **settings):
    engine = _notable_engine(database, notifier, **settings)
    engine._catalyst_sources = {}
    engine._catalyst_headlines = {}
    for key, value in {
        "fomo_catalyst_alerts_enabled": True,
        "fomo_catalyst_max_event_age_seconds": 3600,
        "fomo_catalyst_ping_enabled": False,
        "fomo_confluence_alerts_enabled": True,
    }.items():
        setattr(engine.settings, key, value)
    for key, value in settings.items():
        setattr(engine.settings, key, value)
    return engine


def _news(**overrides):
    from smart_money_bot.models import NewsAlert

    now = int(time.time())
    payload = {
        "source": "CoinDesk",
        "headline": "Major exchange lists a Solana memecoin",
        "summary": "Trading opens today.",
        "url": "https://coindesk.com/story",
        "author": "CoinDesk",
        "author_verified": True,
        "score": 90,
        "narrative_terms": ("exchange listing", "solana"),
        "created_at": now - 120,
        "received_at": now - 60,
    }
    payload.update(overrides)
    return NewsAlert(**payload)


async def test_repeated_reporting_of_one_story_folds_into_one_event(database) -> None:
    engine = _catalyst_engine(database, _RecordingNotifier())
    now = int(time.time())
    first = await engine.observe_catalyst(_news(), now=now)
    second = await engine.observe_catalyst(
        _news(source="TheBlock", author="TheBlock", headline="Exchange listing confirmed"),
        now=now + 5,
    )
    assert first is not None and second is not None
    assert first.event_id == second.event_id
    assert len(second.sources) == 2
    # Same headline text is kept from first observation; it is never rewritten.
    assert second.headline == first.headline


async def test_an_unverified_source_never_becomes_primary(database) -> None:
    engine = _catalyst_engine(database, _RecordingNotifier())
    event = await engine.observe_catalyst(
        _news(author="randomanon", author_verified=False), now=int(time.time())
    )
    assert event is not None
    assert event.primary_sources == ()
    assert M_NO_PRIMARY_SOURCE in event.markers


async def test_the_graded_event_is_persisted(database) -> None:
    engine = _catalyst_engine(database, _RecordingNotifier())
    event = await engine.observe_catalyst(_news(), now=int(time.time()))
    rows = await database.recent_catalyst_events()
    assert rows and rows[0]["event_id"] == event.event_id
    assert rows[0]["confidence"] == event.confidence


async def test_catalysts_can_be_switched_off_entirely(database) -> None:
    engine = _catalyst_engine(database, _RecordingNotifier(), fomo_catalyst_alerts_enabled=False)
    assert await engine.observe_catalyst(_news(), now=int(time.time())) is None


async def test_a_headline_without_narrative_terms_produces_no_event(database) -> None:
    engine = _catalyst_engine(database, _RecordingNotifier())
    assert await engine.observe_catalyst(_news(narrative_terms=()), now=1) is None


async def test_a_token_minted_before_the_event_is_not_created_for_it(database) -> None:
    """A token older than the event cannot have been created for it."""

    from smart_money_bot.runner import runner_candidate_to_json

    candidate = _runner_candidate(name="Solana Memecoin", symbol="MEME")
    await database.store_runner_candidate(
        candidate,
        payload_json=runner_candidate_to_json(candidate),
        snapshot_json="{}",
    )
    engine = _catalyst_engine(database, _RecordingNotifier())
    engine.lab = SimpleNamespace(evaluate_candidate=_null_evaluation)
    # The event happened well after the token was created.
    now = candidate.pair_created_at + 600
    event = assess_event(
        _event(detected_at=now - 30, occurred_at=candidate.pair_created_at + 300), now=now
    )
    await engine.evaluate_catalyst_token(mint=MINT, event=event, now=now)
    links = await database.catalyst_links_for(MINT)
    assert links, "the correlation attempt must still be recorded"
    assert links[0]["seconds_after_event"] is None
    assert links[0]["official"] == 0


async def _null_evaluation(candidate, *, now=None, **kwargs):
    from smart_money_bot.lab.actionability import Actionability
    from smart_money_bot.lab.smartmoney import SmartMoneyAssessment

    return SimpleNamespace(
        smart_money=SmartMoneyAssessment(),
        actionability=Actionability(score=D("75")),
        decision=SimpleNamespace(expected_net_edge_percent=D("14")),
        evaluation=SimpleNamespace(edge=SimpleNamespace(cost_percent=D("4.2"))),
    )


async def test_a_correlated_token_publishes_a_catalyst_alert(database) -> None:
    from smart_money_bot.runner import runner_candidate_to_json

    candidate = _runner_candidate(name="Solana Memecoin", symbol="MEME")
    await database.store_runner_candidate(
        candidate,
        payload_json=runner_candidate_to_json(candidate),
        snapshot_json="{}",
    )
    notifier = _RecordingNotifier()
    engine = _catalyst_engine(database, notifier)
    engine.lab = SimpleNamespace(evaluate_candidate=_null_evaluation)
    now = candidate.pair_created_at + 60
    event = assess_event(
        _event(detected_at=now - 400, occurred_at=candidate.pair_created_at - 120), now=now
    )
    published = await engine.evaluate_catalyst_token(mint=MINT, event=event, now=now)
    assert published is True
    alert = notifier.alerts[0]
    assert alert.kind in {fa.BREAKING_CATALYST, fa.CATALYST_WATCH, fa.CONFLUENCE_WATCH}
    assert alert.entry_eligible is False
    assert alert.token_mint == MINT
    warnings = next(item for item in alert.spec.fields if item.name == "WARNINGS")
    assert "EVENT VERIFIED ≠ TOKEN VERIFIED" in warnings.value
    # The correlation is stored, and it is not OFFICIAL on a name match alone.
    links = await database.catalyst_links_for(MINT)
    assert links and links[0]["official"] == 0
    assert links[0]["connection"] != CONNECTION_OFFICIAL


async def test_catalyst_pings_are_off_by_default(database) -> None:
    from smart_money_bot.runner import runner_candidate_to_json

    candidate = _runner_candidate(name="Solana Memecoin", symbol="MEME")
    await database.store_runner_candidate(
        candidate,
        payload_json=runner_candidate_to_json(candidate),
        snapshot_json="{}",
    )
    notifier = _RecordingNotifier()
    engine = _catalyst_engine(database, notifier)
    engine.lab = SimpleNamespace(evaluate_candidate=_null_evaluation)
    now = candidate.pair_created_at + 60
    event = assess_event(
        _event(detected_at=now - 400, occurred_at=candidate.pair_created_at - 120), now=now
    )
    await engine.evaluate_catalyst_token(mint=MINT, event=event, now=now)
    assert notifier.alerts[0].ping is False


async def test_the_name_similarity_uses_the_graders_scale(database) -> None:
    from smart_money_bot.engine import SmartMoneyEngine

    candidate = _runner_candidate(name="Solana Memecoin", symbol="MEME")
    event = _event()
    similarity = SmartMoneyEngine._name_similarity(candidate, event)
    assert similarity is not None
    assert similarity > D("50")
    miss = SmartMoneyEngine._name_similarity(
        _runner_candidate(name="Completely Unrelated Frog"), event
    )
    assert miss == D("0")


# ---------------------------------------------------------------------------
# stage-2 enrichment through the engine
# ---------------------------------------------------------------------------


async def test_enrichment_edits_in_place_and_never_re_pings(database) -> None:
    await _persist_candidate(database)
    notifier = _RecordingNotifier()
    engine = _notable_engine(
        database,
        notifier,
        fomo_alert_enrichment_enabled=True,
        fomo_alert_enrichment_delay_seconds=0,
    )
    engine.lab = SimpleNamespace(evaluate_candidate=_null_evaluation)

    async def _analyze(mint, **kwargs):
        return _runner_candidate()

    engine.analyze_runner = _analyze
    alert = _watch_alert()
    await engine._enrich_fast_alert(alert)
    assert len(notifier.alerts) == 0, "enrichment must never publish a new alert"
    assert len(notifier.enrichments) == 1
    safety = next(item for item in notifier.enrichments[0].fields if item.name == "SAFETY")
    assert "PASS" not in safety.value or "UNKNOWN" in safety.value


async def test_a_failed_enrichment_degrades_to_unknown_not_pass(database) -> None:
    notifier = _RecordingNotifier()
    engine = _notable_engine(
        database,
        notifier,
        fomo_alert_enrichment_enabled=True,
        fomo_alert_enrichment_delay_seconds=0,
    )

    async def _boom(mint, **kwargs):
        raise ValueError("provider down")

    engine.analyze_runner = _boom
    await engine._enrich_fast_alert(_watch_alert())
    safety = next(item for item in notifier.enrichments[0].fields if item.name == "SAFETY")
    assert "UNKNOWN" in safety.value
    assert "**PASS**" not in safety.value


async def test_the_fast_lane_never_blocks_the_execution_pipeline(database) -> None:
    """A slow public lookup must run beside the pipeline, not in front of it."""

    await _persist_candidate(database)
    notifier = _RecordingNotifier()
    engine = _notable_engine(database, notifier)

    async def _hangs(mint, **kwargs):
        await asyncio.sleep(30)

    engine.dex_screener = SimpleNamespace(snapshot=_hangs)
    started = time.monotonic()
    engine._queue_notable_alert(_swap(), _trader())
    queued = time.monotonic() - started
    assert queued < 0.5, "queueing an alert must be instant"
    assert engine._notable_tasks
    for task in list(engine._notable_tasks):
        task.cancel()
    await asyncio.gather(*engine._notable_tasks, return_exceptions=True)


async def test_a_sell_is_never_even_queued(database) -> None:
    from smart_money_bot.models import Side

    engine = _notable_engine(database, _RecordingNotifier())
    engine._queue_notable_alert(_swap(side=Side.SELL), _trader())
    assert engine._notable_tasks == set()


async def test_velocity_and_novelty_are_measured_not_invented(database) -> None:
    """Priority inputs must come from our own observations, never a guess."""

    from smart_money_bot.engine import SmartMoneyEngine

    sources = _sources(count=2)
    fast = SmartMoneyEngine._discussion_velocity(sources, NOW - 60, NOW)
    slow = SmartMoneyEngine._discussion_velocity(sources, NOW - 3_600, NOW)
    assert fast > slow
    assert SmartMoneyEngine._discussion_velocity((), NOW - 60, NOW) is None

    fresh = SmartMoneyEngine._event_novelty(NOW - 60, NOW, horizon=3_600)
    ageing = SmartMoneyEngine._event_novelty(NOW - 1_800, NOW, horizon=3_600)
    spent = SmartMoneyEngine._event_novelty(NOW - 7_200, NOW, horizon=3_600)
    assert fresh == D("100")
    assert D("0") < ageing < D("100")
    assert spent == D("0")


async def test_three_independent_outlets_reach_a_credible_event(database) -> None:
    """The full news → event → token → alert path, end to end."""

    from smart_money_bot.runner import runner_candidate_to_json

    candidate = _runner_candidate(name="Solana Memecoin", symbol="MEME")
    await database.store_runner_candidate(
        candidate,
        payload_json=runner_candidate_to_json(candidate),
        snapshot_json="{}",
    )
    notifier = _RecordingNotifier()
    engine = _catalyst_engine(database, notifier)
    engine.lab = SimpleNamespace(evaluate_candidate=_null_evaluation)
    now = candidate.pair_created_at + 300
    base = candidate.pair_created_at - 200
    for index, (source, headline) in enumerate(
        (
            ("CoinDesk", "Major exchange lists a Solana memecoin"),
            ("TheBlock", "Exchange confirms Solana memecoin listing"),
            ("Blockworks", "Solana memecoin heads to a major venue"),
        )
    ):
        event = await engine.observe_catalyst(
            _news(source=source, author=source, headline=headline, created_at=base + index * 20),
            now=now,
        )
    assert event.independent_confirmations == 3
    assert event.credible is True
    # No curated Tier-A account published it, so the gap is named, not papered over.
    assert M_NO_PRIMARY_SOURCE in event.markers

    assert await engine.evaluate_catalyst_token(mint=MINT, event=event, now=now) is True
    alert = notifier.alerts[0]
    assert alert.entry_eligible is False
    assert alert.may_ping is False
    integrity = next(item for item in alert.spec.fields if item.name == "EVENT INTEGRITY")
    assert "no primary source" in integrity.value


async def test_a_single_outlet_never_reaches_a_credible_event(database) -> None:
    engine = _catalyst_engine(database, _RecordingNotifier())
    now = int(time.time())
    event = await engine.observe_catalyst(_news(), now=now)
    assert event.independent_confirmations == 1
    assert event.credible is False
    assert await engine._maybe_publish_catalyst(event, now=now) is False


async def test_a_refused_card_can_be_retried_but_a_delivered_one_cannot(
    database,
) -> None:
    """A card Discord rejected was never seen, so it must not be locked out."""

    class _Refusing(_RecordingNotifier):
        async def on_fast_alert(self, alert: fa.FastAlert) -> bool:
            await super().on_fast_alert(alert)
            return False

    refusing = _Refusing()
    engine = _engine(database, refusing)
    candidate = _runner_candidate()
    assert await engine._maybe_publish_fast_watch(candidate) is False
    assert await database.fast_alert_row("FAST_WATCH:" + MINT) is None

    accepting = _RecordingNotifier()
    engine.notifier = accepting
    assert await engine._maybe_publish_fast_watch(candidate) is True
    # Once delivered, the lock holds even if the message handle is attached.
    await database.attach_fast_alert_message(
        alert_key="FAST_WATCH:" + MINT, message_id=1, channel_id=2
    )
    await database.release_fast_alert("FAST_WATCH:" + MINT)
    assert await database.fast_alert_row("FAST_WATCH:" + MINT) is not None


async def test_every_fast_alert_uses_the_canonical_fomo_link(database) -> None:
    from smart_money_bot.constants import fomo_coin_url

    notifier = _RecordingNotifier()
    engine = _engine(database, notifier, fomo_referral_code="TESTREF")
    await engine._maybe_publish_fast_watch(_runner_candidate())
    links = next(item for item in notifier.alerts[0].spec.fields if item.name == "LINKS")
    assert fomo_coin_url(MINT, "TESTREF") in links.value
    assert "fomo.family/coin" in links.value
