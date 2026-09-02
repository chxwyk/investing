"""Dependency-light verification for restricted build environments."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import tempfile
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from smart_money_bot.config import Settings
from smart_money_bot.constants import WRAPPED_SOL_MINT
from smart_money_bot.database import Database
from smart_money_bot.detector import SwapDetector
from smart_money_bot.models import (
    DetectedSwap,
    DiscoveryCandidate,
    ExecutionMode,
    Side,
    Signal,
    TokenInfo,
    TraderMetrics,
)
from smart_money_bot.risk import RiskEngine
from smart_money_bot.scoring import score_trader

WALLET = "wallet"
TOKEN = "token"


class FakeMarket:
    async def price(self, mint: str) -> Decimal | None:
        return Decimal("100") if mint == WRAPPED_SOL_MINT else Decimal("1")


def token_balance(amount: int) -> dict:
    return {
        "mint": TOKEN,
        "owner": WALLET,
        "uiTokenAmount": {"amount": str(amount), "decimals": 6},
    }


def transaction(pre_sol: int, post_sol: int, pre_token: int, post_token: int) -> dict:
    return {
        "transaction": {"message": {"accountKeys": [{"pubkey": WALLET}]}},
        "meta": {
            "err": None,
            "fee": 5000,
            "preBalances": [pre_sol],
            "postBalances": [post_sol],
            "preTokenBalances": [token_balance(pre_token)],
            "postTokenBalances": [token_balance(post_token)],
        },
    }


def metrics(pnl: str, cost: str, wins: int, losses: int, trades: int, dd: str) -> TraderMetrics:
    return TraderMetrics(
        address=WALLET,
        alias="Trader",
        window_seconds=86_400,
        trades=trades,
        buys=trades // 2,
        sells=trades - trades // 2,
        wins=wins,
        losses=losses,
        realized_pnl_usd=Decimal(pnl),
        matched_cost_usd=Decimal(cost),
        volume_usd=Decimal("10000"),
        max_drawdown_usd=Decimal(dd),
    )


def make_settings(database_path: str) -> Settings:
    env = {
        "DATABASE_PATH": database_path,
        "POLL_INTERVAL_SECONDS": "5",
        "PAPER_STARTING_USD": "1000",
        "DEFAULT_COPY_USD": "10",
        "MAX_COPY_USD": "25",
        "CONSENSUS_MIN_TRADERS": "2",
    }
    with patch.dict(os.environ, env, clear=True):
        return Settings.from_env(require_discord_token=False)


async def main() -> None:
    detector = SwapDetector(FakeMarket(), Decimal("10"))
    buy = await detector.detect(
        transaction(10_000_000_000, 8_999_995_000, 0, 100_000_000),
        wallet=WALLET,
        signature="buy",
        block_time=int(time.time()),
    )
    assert buy and buy.side is Side.BUY and buy.usd_value == Decimal("100")
    sell = await detector.detect(
        transaction(9_000_000_000, 9_499_995_000, 100_000_000, 50_000_000),
        wallet=WALLET,
        signature="sell",
        block_time=int(time.time()),
    )
    assert sell and sell.side is Side.SELL and sell.usd_value == Decimal("50.0")

    consistent = metrics("300", "1500", 8, 2, 20, "40")
    lucky = metrics("1000", "100", 1, 0, 2, "0")
    assert score_trader(consistent, consistent) > score_trader(lucky, lucky)

    with tempfile.TemporaryDirectory() as directory:
        database_path = str(Path(directory) / "selfcheck.db")
        settings = make_settings(database_path)
        database = Database(database_path, Decimal("1000"))
        await database.connect()
        try:
            await database.add_trader(WALLET, "Trader")
            now = int(time.time())
            await database.record_swap(
                DetectedSwap(
                    signature="wallet-buy",
                    trader_address=WALLET,
                    block_time=now - 10,
                    side=Side.BUY,
                    token_mint=TOKEN,
                    token_amount=Decimal("100"),
                    quote_mint=WRAPPED_SOL_MINT,
                    quote_amount=Decimal("1"),
                    usd_value=Decimal("100"),
                    token_price_usd=Decimal("1"),
                )
            )
            await database.record_swap(
                DetectedSwap(
                    signature="wallet-sell",
                    trader_address=WALLET,
                    block_time=now,
                    side=Side.SELL,
                    token_mint=TOKEN,
                    token_amount=Decimal("100"),
                    quote_mint=WRAPPED_SOL_MINT,
                    quote_amount=Decimal("1.2"),
                    usd_value=Decimal("120"),
                    token_price_usd=Decimal("1.2"),
                )
            )
            trader_metrics = (await database.metrics(86_400))[0]
            assert trader_metrics.realized_pnl_usd == Decimal("20.0")

            signal = Signal(
                token_mint=TOKEN,
                side=Side.BUY,
                created_at=now,
                trader_addresses=("a", "b"),
                trader_aliases=("A", "B"),
                source_signatures=("a1", "b1"),
                combined_score=Decimal("75"),
                reference_price_usd=Decimal("1"),
            )
            signal_id = await database.record_signal(signal)
            paper_buy = await database.paper_execute(
                signal_id=signal_id,
                token_mint=TOKEN,
                side=Side.BUY,
                market_price_usd=Decimal("1"),
                size_usd=Decimal("100"),
                fee_bps=50,
                slippage_bps=100,
            )
            assert paper_buy is not None
            paper_sell = await database.paper_execute(
                signal_id=signal_id,
                token_mint=TOKEN,
                side=Side.SELL,
                market_price_usd=Decimal("1"),
                size_usd=Decimal("100"),
                fee_bps=50,
                slippage_bps=100,
            )
            assert paper_sell and paper_sell["realized_pnl"] < 0

            risk = RiskEngine(settings, database)
            healthy = TokenInfo(
                mint=TOKEN,
                decimals=6,
                liquidity_usd=Decimal("500000"),
                holder_count=5000,
                organic_score=Decimal("80"),
                mint_authority_disabled=True,
                freeze_authority_disabled=True,
                top_holders_percent=Decimal("20"),
            )
            decision = await risk.assess(
                signal=signal,
                mode=ExecutionMode.PAPER,
                token_info=healthy,
                market_price_usd=Decimal("1"),
            )
            assert decision.allowed

            discovery_candidate = DiscoveryCandidate(
                address="auto-wallet-one",
                alias="Auto One",
                realized_pnl_24h=Decimal("250"),
                previous_pnl_24h=None,
                roi_24h_percent=Decimal("18"),
                win_rate_percent=Decimal("70"),
                trades_24h=20,
                buys_24h=10,
                sells_24h=10,
                closed_tokens=8,
                invested_24h_usd=Decimal("1000"),
                volume_24h_usd=Decimal("2400"),
                last_trade_ms=None,
                score=Decimal("72"),
                rank=1,
            )
            refresh = await database.apply_discovery([discovery_candidate])
            assert refresh.added_wallets == ("auto-wallet-one",)
            discovered = await database.list_discovered()
            assert discovered[0].realized_pnl_24h == Decimal("250.0")
            tracked = await database.resolve_trader("auto-wallet-one")
            assert tracked and tracked.enabled and tracked.source == "auto"
        finally:
            await database.close()

    await check_paper_laboratory()
    await check_shadow_auto_trader()
    await check_profit_optimization()
    await check_early_alpha()
    await check_trending_alpha()
    await check_trenches_intelligence()
    await check_token_identity()
    await check_promotion_intelligence()
    await check_gmgn_integration()
    await check_production_hardening()
    await check_clone_defence()
    await check_direction_not_level()

    print(
        "SELF-CHECK PASSED: detector, scoring, database, discovery rotation, "
        "paper P&L, risk gate, PAPER laboratory, discovery-speed, realtime-alpha, "
        "SHADOW auto-trader, profit-optimization, early-alpha, Trending-first, "
        "trenches-intelligence, token-identity, promotion-intelligence, "
        "GMGN-integration, production-hardening, clone-defence and\n        "
        "direction-not-level invariants"
    )


async def check_token_identity() -> None:
    """A token is its chain plus its exact mint.  Everything else is display.

    This deploy gate exists because the bot once alerted on a brand-new
    same-ticker clone, called it an organic runner, admitted in the same card
    that safety was UNKNOWN, and offered a buy button.  Four separate defects,
    four separate invariants.
    """

    import ast
    import inspect
    import pathlib
    import textwrap

    import smart_money_bot.bot as bot_module
    import smart_money_bot.engine as engine_module
    from smart_money_bot.fast_alerts import build_early_alert
    from smart_money_bot.lab.early import EarlySignals, evaluate_early_signal
    from smart_money_bot.token_identity import (
        SOURCE_RESOLVED_MISMATCH,
        UNRESOLVED_EXACT_MINT,
        ResolutionProvenance,
        TokenIdentityError,
        assert_exact_propagation,
        detect_symbol_collision,
        exact,
        unresolved,
    )

    watched = "GPR7Ax4kQ2mVn8hLdT6yWc3JbRfE9uZsXqM1oP5tH4dK"
    clone = "7TqH1d4Vf9QG578vB99Q7ewFQPoxSYqBDxSAzBpBpump"

    # 1. No symbol-based token resolution anywhere on the identity path.
    #    A text search may produce a *lead*; it may never decide which token a
    #    card is about, so the tie-break that used to pick the youngest pair is
    #    gone and the ambiguous case returns nothing.
    from smart_money_bot.news import DexNarrativeMatcher

    search_source = inspect.getsource(DexNarrativeMatcher.search)
    selectors = [
        ast.unparse(node)
        for node in ast.walk(ast.parse(textwrap.dedent(search_source)))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "max"
        for keyword in node.keywords
        if keyword.arg == "key"
        for node in (keyword.value,)
    ]
    assert selectors, "the resolver must still have an explicit selection rule"
    for selector in selectors:
        assert "age" not in selector, (
            "resolving a symbol to the youngest pair is how the clone got chosen"
        )
    assert "distinct_mints" in search_source and "return None" in search_source, (
        "an ambiguous symbol must resolve to nothing, never to a guess"
    )

    matcher = DexNarrativeMatcher(min_liquidity_usd=Decimal("1000"), max_age_minutes=600)
    created = int(time.time() * 1000) - 120_000

    def _pair(mint: str, liquidity: str, age_ms: int) -> dict:
        return {
            "chainId": "solana",
            "baseToken": {"address": mint, "symbol": "GPRO", "name": "GPRO"},
            "liquidity": {"usd": liquidity},
            "pairCreatedAt": age_ms,
            "txns": {"m5": {"buys": 40, "sells": 5}},
            "volume": {"m5": "9000"},
        }

    async def _pairs(query: str) -> list[dict]:
        return [
            _pair(watched, "90000", created - 3_000_000),
            _pair(clone, "6000", created),
        ]

    matcher._search_pairs = _pairs  # type: ignore[method-assign]
    assert await matcher.search("GPRO") is None, (
        "two live tokens sharing a ticker must resolve to neither"
    )

    # 1b. Enrichment is keyed on the exact address: a pair belonging to another
    #     token can never be read into this token's snapshot.
    from smart_money_bot.callouts import parse_dex_snapshot

    payload = {
        "pairs": [
            {
                "chainId": "solana",
                "baseToken": {"address": clone, "symbol": "GPRO"},
                "liquidity": {"usd": "250000"},
                "marketCap": "900000",
                "pairCreatedAt": created,
            }
        ]
    }
    assert not parse_dex_snapshot(payload, mint=watched).available, (
        "a same-symbol pair for another mint must never enrich this one"
    )

    # 2. Exact-mint propagation is asserted at the hand-offs, and a mismatch is
    #    a hard failure rather than a silently different card.
    assert_exact_propagation(watched, watched, stage="selfcheck")
    try:
        assert_exact_propagation(watched, clone, stage="selfcheck")
    except TokenIdentityError:
        pass
    else:  # pragma: no cover - the gate is the point
        raise AssertionError("a swapped mint must raise, not warn")

    swapped = ResolutionProvenance(
        source="fomo_trending",
        source_mint=watched,
        resolved_mint=clone,
        resolution_method="EXACT_MINT",
    )
    assert swapped.substituted and not swapped.identity_verified
    assert swapped.failure_reason() == SOURCE_RESOLVED_MISMATCH
    assert exact(watched, source="fomo_trending").identity_verified
    assert unresolved(watched, source="fomo_trending").failure_reason() == UNRESOLVED_EXACT_MINT

    # 3. A shared ticker groups tokens; it never merges or ranks them.
    collision = detect_symbol_collision(
        "GPRO", {watched: "GPRO", clone: "gpro"}, subject_mint=watched
    )
    assert collision.detected and collision.count == 2
    assert watched in collision.warning_line(watched)

    identity_source = pathlib.Path(
        inspect.getsourcefile(exact) or ""
    ).read_text()
    identity_tree = ast.parse(identity_source)
    imported = {
        node.module.split(".")[0]
        for node in ast.walk(identity_tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(identity_tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not (imported & {"aiohttp", "httpx", "requests", "solana", "solders"}), (
        "identity resolution must not be able to call a provider"
    )

    # 4. A card that does not know cannot sound like it does, and the buy
    #    control is gated on eligibility the early lane can never grant.
    signals = EarlySignals(
        mint=watched,
        now=1_700_000_000,
        first_seen_at=1_700_000_000 - 8,
        pair_age_seconds=82,
        market_cap_usd=Decimal("33100"),
        first_seen_market_cap_usd=Decimal("31180"),
        liquidity_usd=Decimal("6900"),
        volume_5m_usd=Decimal("5200"),
        price_change_5m_percent=Decimal("14"),
        buys_5m=26,
        sells_5m=6,
        independent_buyers_5m=19,
        route_available=True,
    )
    verdict = evaluate_early_signal(signals)
    alert = build_early_alert(
        mint=watched,
        name="Grok Pocket",
        symbol="GPRO",
        fomo_url=f"https://fomo.family/coin?address={watched}",
        verdict=verdict,
        age_seconds=82,
        first_seen_seconds_ago=8,
        first_seen_market_cap_usd=Decimal("31180"),
        alert_market_cap_usd=Decimal("33100"),
        current_market_cap_usd=Decimal("33100"),
        liquidity_usd=Decimal("6900"),
        buys=26,
        sells=6,
        safety_status="UNKNOWN",
    )
    for phrase in ("LOOK NOW", "BUY NOW", "APE", "SEND IT"):
        assert phrase not in alert.spec.title, (
            "an unvalidated card may not lead with actionable language"
        )
    state = {field.name: field.value for field in alert.spec.fields}["STATE"]
    assert "Entry eligible: **NO**" in state and "Trade CTA: **DISABLED**" in state
    assert alert.trade_eligible is False
    assert watched in alert.spec.description, "the exact mint must be on the card"

    view_source = inspect.getsource(bot_module._token_view)
    assert "if trade_eligible:" in view_source, "the buy control must be gated"
    buttons = {item.label for item in bot_module._token_view(watched).children}
    assert "Buy on Jupiter" not in buttons
    assert {"Open in Fomo", "Chart", "Solscan"} <= buttons, "research links always render"
    eligible = {
        item.label for item in bot_module._token_view(watched, trade_eligible=True).children
    }
    assert "Buy on Jupiter" in eligible
    assert "trade_eligible=alert.trade_eligible" in inspect.getsource(
        bot_module.SmartMoneyBot.on_fast_alert
    ), "the renderer must read the gate the lane set"

    # 5. "Organic" is a claim about who is buying, not how many trades printed.
    loud = evaluate_early_signal(
        EarlySignals(
            **{
                **{
                    field: getattr(signals, field)
                    for field in EarlySignals.__dataclass_fields__
                },
                "buys_5m": 542,
                "sells_5m": 144,
                "volume_5m_usd": Decimal("180000"),
                "independent_buyers_5m": None,
            }
        )
    )
    assert "ORGANIC_MARKET_EVIDENCE" not in loud.evidence_categories, (
        "542 buys against 144 sells is activity, not proven organic demand"
    )
    assert loud.tier != "ORGANIC_RUNNER"
    assert loud.visible, "restraint must not make the token invisible"

    lane_source = inspect.getsource(engine_module.SmartMoneyEngine._independent_buyers_5m)
    assert "return None" in lane_source, (
        "unknown independence must be distinguishable from zero independence"
    )


async def check_promotion_intelligence() -> None:
    """A strong near-miss must get a second look, and exactly one interrupt.

    This gate exists because of one production card: a three-minute-old token at
    $71.93K with 78 buys against 48 sells and price up 46.48% scored 76/100 —
    twenty-one points clear of the runner bar — and never reached anyone.  Each
    assertion below is one link in the chain that failed.
    """

    import inspect
    import pathlib

    import smart_money_bot.bot as bot_module
    import smart_money_bot.engine as engine_module
    import smart_money_bot.fast_alerts as fast_alerts_module
    from smart_money_bot.constants import TERMINAL_TOKEN_URL_TEMPLATE
    from smart_money_bot.lab.early import EarlySignals, evaluate_early_signal
    from smart_money_bot.lab.promotion import (
        CONCENTRATION_WORSENING,
        FAMILY_KNOWN_TRADER,
        WHY_ALREADY_PROMOTED,
        WHY_CONCENTRATION_WORSENING,
        WHY_EDGE_CONSUMED,
        WHY_KNOWN_MONEY_LEAVING,
        WHY_NO_NEW_EVIDENCE,
        PromotionEvidence,
        entry_from_json,
        evaluate_promotion,
        open_early_watch,
        should_open_watch,
    )
    from smart_money_bot.lab.toptraders import (
        FLOW_DISTRIBUTING,
        TraderFill,
        build_positions,
        independent_confirmations,
        join_known_traders,
    )

    mint = "GPR7Ax4kQ2mVn8hLdT6yWc3JbRfE9uZsXqM1oP5tH4dK"
    other = "7TqH1d4Vf9QG578vB99Q7ewFQPoxSYqBDxSAzBpBpump"
    now = 1_700_000_000

    # 1. The production candidate is reproduced, and it opens a watch.
    signals = EarlySignals(
        mint=mint,
        now=now,
        first_seen_at=now - 5,
        pair_age_seconds=180,
        market_cap_usd=Decimal("71930"),
        first_seen_market_cap_usd=Decimal("71930"),
        liquidity_usd=Decimal("21090"),
        volume_5m_usd=Decimal("21090") * Decimal("1.214"),
        price_change_5m_percent=Decimal("46.48"),
        buys_5m=78,
        sells_5m=48,
        route_available=True,
    )
    verdict = evaluate_early_signal(signals)
    assert verdict.score == Decimal("76.00") and not verdict.may_ping, (
        "the reproduction of the reported card drifted; the diagnosis below is "
        "only meaningful if this is still the same candidate"
    )
    assert verdict.evidence_categories == (), (
        "the suppression cause was an empty evidence-category set, not a low score"
    )
    assert should_open_watch(verdict), (
        "a 76/100 near-miss with live edge must get a second look (section 2)"
    )

    entry = open_early_watch(
        mint,
        verdict=verdict,
        now=now,
        market_cap_usd=Decimal("71930"),
        first_seen_market_cap_usd=Decimal("71930"),
        liquidity_usd=Decimal("21090"),
        buys=78,
        holder_count=26,
    )

    # 2. Promotion requires *new* information, not another look at the old.
    stalled = evaluate_promotion(
        entry, PromotionEvidence(now=now + 30, score=Decimal("77"), buys=80, holder_count=27)
    )
    assert not stalled.decision.promote
    assert stalled.entry.suppression_reason == WHY_NO_NEW_EVIDENCE

    promoted = evaluate_promotion(
        entry,
        PromotionEvidence(
            now=now + 40,
            score=Decimal("77"),
            proven_independent_traders=2,
            known_money_flow="KNOWN_MONEY_ACCUMULATING",
            trigger="known_trader_buy",
        ),
    )
    assert promoted.decision.promote and promoted.decision.family == FAMILY_KNOWN_TRADER
    assert promoted.should_ping

    # 3. Exactly one interrupt per candidate, and never after the move.
    again = evaluate_promotion(
        promoted.entry,
        PromotionEvidence(
            now=now + 200, score=Decimal("99"), buys=900, proven_independent_traders=9
        ),
    )
    assert not again.decision.promote and not again.should_ping
    assert again.entry.suppression_reason == WHY_ALREADY_PROMOTED

    spent = evaluate_promotion(
        entry,
        PromotionEvidence(
            now=now + 300,
            score=Decimal("95"),
            buys=400,
            edge_available=False,
            proven_independent_traders=4,
        ),
    )
    assert spent.entry.suppression_reason == WHY_EDGE_CONSUMED

    # 4. The two things that look like good news and are not.
    tightening = evaluate_promotion(
        entry,
        PromotionEvidence(
            now=now + 120,
            score=Decimal("77"),
            holder_count=94,
            holders_per_minute=Decimal("34"),
            concentration_trend=CONCENTRATION_WORSENING,
        ),
    )
    assert tightening.entry.suppression_reason == WHY_CONCENTRATION_WORSENING, (
        "holders growing into fewer hands is accumulation, not distribution"
    )
    leaving = evaluate_promotion(
        entry,
        PromotionEvidence(
            now=now + 120,
            score=Decimal("77"),
            proven_independent_traders=3,
            known_money_flow=FLOW_DISTRIBUTING,
        ),
    )
    assert leaving.entry.suppression_reason == WHY_KNOWN_MONEY_LEAVING

    # The vocabularies really do line up across package boundaries; a drift here
    # would make both guards above silently unreachable.
    from smart_money_bot.trenches.holders import (
        CONCENTRATION_WORSENING as TRENCH_WORSENING,
    )

    assert CONCENTRATION_WORSENING == TRENCH_WORSENING

    # 5. The baseline survives every recheck.  It is the comparison itself.
    walked = entry
    for index in range(4):
        walked = evaluate_promotion(
            walked,
            PromotionEvidence(now=now + 30 * (index + 1), score=Decimal("77"), buys=80),
        ).entry
    assert walked.entry_score == Decimal("76.00") and walked.entry_buys == 78
    assert entry_from_json(walked.to_json()) == walked

    # 6. Five wallets behind one funder are one confirmation (section 8).
    fills = [
        TraderFill(f"wallet{key}", mint, "BUY", now, Decimal("1000"), Decimal("100"))
        for key in "ABCDE"
    ]
    positions = build_positions(fills, mint=mint)
    known = join_known_traders(
        positions,
        mint=mint,
        registry={f"wallet{key}": key for key in "ABCDE"},
        reputations={f"wallet{key}": ("PROVEN_EARLY", 20) for key in "ABCDE"},
        clusters={f"wallet{key}": "funder:X" for key in "ABCDE"},
    )
    confirmation = independent_confirmations(known, mint=mint)
    assert confirmation.wallet_count == 5 and confirmation.independent_count == 1, (
        "a sybil group must not be able to manufacture its own consensus"
    )

    # 7. A wallet's history on a same-ticker token never reaches this one.
    crossed = build_positions(
        [TraderFill("walletA", other, "BUY", now, Decimal("90000"), Decimal("50000"))],
        mint=mint,
    )
    assert crossed == (), "trader evidence must be exact-mint or absent"

    # 8. The promotion card labels what it does not know and hands out no buy.
    alert = fast_alerts_module.build_promotion_alert(
        mint=mint,
        name="Grok Pocket",
        symbol="GPRO",
        fomo_url=f"https://fomo.family/coin?address={mint}",
        decision=promoted.decision,
        entry=entry,
        current_market_cap_usd=Decimal("78200"),
        liquidity_usd=Decimal("21090"),
        safety_status="UNKNOWN",
    )
    state = {field.name: field.value for field in alert.spec.fields}["STATE"]
    assert "Safety: **UNKNOWN**" in state and "this is not a safety pass" in state
    assert "Entry eligible: **NO**" in state and "Trade CTA: **DISABLED**" in state
    assert alert.trade_eligible is False, "no promotion may hand out a buy control"
    assert mint in alert.spec.description and other not in alert.spec.description
    assert alert.alert_key == f"{fast_alerts_module.EARLY_PROMOTION}:{mint}"
    unverified = fast_alerts_module.build_promotion_alert(
        mint=mint,
        name="Grok Pocket",
        symbol="GPRO",
        fomo_url="https://fomo.family/coin",
        decision=promoted.decision,
        entry=entry,
        identity_verified=False,
    )
    assert "LOOK NOW" not in unverified.spec.title and not unverified.ping, (
        "identity outranks evidence: an unidentified token is never actionable"
    )

    # 9. The engine really is wired to open a watch and to react to an event.
    lane = inspect.getsource(engine_module.SmartMoneyEngine._early_lane_task)
    assert "_open_early_watch" in lane
    notable = inspect.getsource(engine_module.SmartMoneyEngine._maybe_publish_notable)
    assert "note_early_watch_event" in notable, (
        "a known trader entering a watched candidate must not wait for the timer"
    )

    # 10. Terminal is navigation, from one definition, and never a data source.
    assert "{mint}" in TERMINAL_TOKEN_URL_TEMPLATE
    assert mint in fast_alerts_module._terminal_url(mint)
    assert bot_module._terminal_token_url("https://x.example/{mint}", mint).endswith(mint)
    assert bot_module._terminal_token_url("https://x.example/token", mint) == ""
    root = pathlib.Path(engine_module.__file__).parent
    for path in root.rglob("*.py"):
        text = path.read_text().lower()
        for forbidden in ("cookies=", "set_cookie", "cookiejar"):
            assert forbidden not in text, (
                f"{path.name} looks like it carries a third-party session"
            )

    # 11. Nothing on the promotion path can move real money (section 38).
    for module in ("lab/promotion.py", "lab/toptraders.py"):
        text = (root / module).read_text()
        for forbidden in ("Keypair", "send_transaction", "sign_transaction", "aiohttp"):
            assert forbidden not in text, f"{module} must stay signer- and provider-free"

    # 12. Shadow history is untouched by this release.
    from smart_money_bot.lab.shadow import SIGNAL_FAMILIES

    assert len(SIGNAL_FAMILIES) >= 8, "no shadow signal family may be removed"


async def check_gmgn_integration() -> None:
    """GMGN is a research provider, and this build cannot trade through it.

    The two things worth failing a deploy over: a credential that could escape,
    and a code path that could spend money.  Everything else about a provider
    integration is recoverable; those two are not.
    """

    import inspect
    import json as _json
    import pathlib

    import smart_money_bot
    import smart_money_bot.gmgn as gmgn
    from smart_money_bot.execution import (
        GATE_NAMES,
        MODE_LIVE_AUTO,
        ExecutionIntent,
        LiveTradingGates,
        ShadowExecutionProvider,
        gates_from_settings,
    )
    from smart_money_bot.gmgn import lifecycle as gmgn_stages

    secret = "gmgn-selfcheck-key-0123456789abcdef"
    mint = "GPR7Ax4kQ2mVn8hLdT6yWc3JbRfE9uZsXqM1oP5tH4dK"
    other = "7TqH1d4Vf9QG578vB99Q7ewFQPoxSYqBDxSAzBpBpump"

    class _Response:
        def __init__(self, payload, status=200, headers=None):
            self._payload, self.status = payload, status
            self.headers = headers or {}

        async def text(self):
            return _json.dumps(self._payload)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Session:
        closed = False

        def __init__(self, handler):
            self._handler, self.requests = handler, []

        def request(self, method, url, **kwargs):
            self.requests.append((method, url, kwargs))
            return self._handler(method, url, kwargs)

    # 1. No signed-auth mode, no signing key, no order route.  This is what
    #    makes "cannot trade" structural rather than configured.
    client_source = inspect.getsource(gmgn.GmgnClient)
    for forbidden in ("X-Signature", "GMGN_PRIVATE_KEY", "detectAlgorithm"):
        assert forbidden not in client_source, (
            "the GMGN client must not gain a request signer — that is the half "
            "of the API that places orders"
        )
    assert gmgn.ORDER_PATHS.isdisjoint(gmgn.READ_PATHS)
    for path in gmgn.ORDER_PATHS:
        assert path not in client_source

    root = pathlib.Path(smart_money_bot.__file__).parent
    for path in root.rglob("*.py"):
        text = path.read_text()
        for needle in ('getenv("GMGN_PRIVATE_KEY"', "gmgn_private_key"):
            assert needle not in text, f"{path.name} reads a GMGN signing key"
    for path in (root / "gmgn").glob("*.py"):
        text = path.read_text()
        for forbidden in ("Keypair", "send_transaction", "sign_transaction", "solders"):
            assert forbidden not in text, f"gmgn/{path.name} must stay signer-free"

    # 2. A non-read path is refused before a request is ever built.
    client = gmgn.GmgnClient(
        api_key=secret,
        session=_Session(lambda m, u, k: _Response({"code": 0, "data": []})),
    )
    try:
        await client._request("POST", "/v1/trade/swap", kind="rank", body={})
    except gmgn.GmgnError as exc:
        assert "research reads only" in str(exc)
    else:  # pragma: no cover - the gate is the point
        raise AssertionError("the client must refuse a non-read path")
    assert client._session.requests == [], "a refused path must not reach the network"

    # 3. The credential is a header, never a query param, and never in output.
    await client.trending(interval="1m")
    _, url, kwargs = client._session.requests[0]
    assert kwargs["headers"]["X-APIKEY"] == secret
    assert secret not in url
    assert all(secret not in str(value) for _, value in kwargs["params"])
    assert secret not in _json.dumps(client.usage_snapshot())

    leaky = gmgn.GmgnClient(
        api_key=secret,
        session=_Session(
            lambda m, u, k: _Response({"code": 500, "error": f"echo {secret}"}, 500)
        ),
    )
    try:
        await leaky.trending(interval="1m")
    except gmgn.GmgnError as exc:
        assert secret not in str(exc), "a provider echoing the key must not leak it"
    assert secret not in _json.dumps(leaky.health.to_json())

    # 4. Failure modes are provider states, never token verdicts.
    for payload, status, expected in (
        ({"code": 401, "error": "INVALID_API_KEY"}, 401, gmgn.AUTH_REJECTED),
        ({"code": 429, "error": "RATE_LIMIT_EXCEEDED"}, 429, gmgn.RATE_LIMITED),
        ({"code": 429, "error": "RATE_LIMIT_BANNED"}, 429, gmgn.RATE_LIMIT_BANNED),
        ({"code": 500, "error": "boom"}, 500, gmgn.PROVIDER_DEGRADED),
    ):
        probe = gmgn.GmgnClient(
            api_key=secret,
            session=_Session(lambda m, u, k, p=payload, st=status: _Response(p, st)),
        )
        try:
            await probe.trending(interval="1m")
        except gmgn.GmgnError as exc:
            assert exc.state == expected
        else:  # pragma: no cover
            raise AssertionError(f"{expected} must be raised, not swallowed")
        assert expected in gmgn.UNKNOWN_STATES

    # A rate limit is never retried into: that is how a 429 becomes a ban.
    limited = gmgn.GmgnClient(
        api_key=secret,
        session=_Session(
            lambda m, u, k: _Response({"code": 429, "error": "RATE_LIMIT_EXCEEDED"}, 429)
        ),
    )
    with contextlib.suppress(gmgn.GmgnError):
        await limited.trending(interval="1m")
    assert len(limited._session.requests) == 1

    # 5. An outage is UNKNOWN, never a safety pass.
    down = gmgn.parse_security(None, mint=mint)
    assert down.unknown and not down.hard_fail
    silent = gmgn.parse_security({}, mint=mint)
    assert silent.unknown and not silent.hard_fail
    assert gmgn.parse_security({"is_honeypot": True}, mint=mint).hard_fail

    # 6. Identity: a row without an exact mint is dropped, and a row about
    #    another mint never answers for this one.
    rows = gmgn.parse_rank_response(
        {"rank": [{"address": mint}, {"symbol": "NOMINT"}, {"address": "nope"}]},
        interval="1m",
    )
    assert [item.mint for item in rows] == [mint]
    holders = gmgn.parse_participants(
        {"holders": [{"address": "W1"}, {"address": "W2", "token_address": other}]},
        mint=mint,
    )
    assert [item.wallet for item in holders] == ["W1"]

    # 7. An unknown provider signal code is reported, never guessed at.
    assert gmgn.classify_signal(12).name == "SMART_DEGEN_BUY"
    unknown_signal = gmgn.classify_signal(999)
    assert unknown_signal.name == gmgn.SIGNAL_UNKNOWN and not unknown_signal.demand

    # 8. One mint keeps one life.
    life = gmgn_stages.open_lifecycle(
        mint,
        stage=gmgn_stages.NEW_PAIR,
        at=0,
        market_cap_usd=Decimal("9000"),
        source="pump_realtime",
    )
    life = gmgn_stages.advance(
        life,
        stage=gmgn_stages.TRENDING,
        at=600,
        market_cap_usd=Decimal("90000"),
        source="gmgn_rank",
    )
    assert life.first_seen_market_cap_usd == Decimal("9000")
    assert life.lead_over(gmgn_stages.NEW_PAIR, gmgn_stages.TRENDING) == 600
    stale = gmgn_stages.advance(life, stage=gmgn_stages.EARLY_CURVE, at=610)
    assert stale.stage == gmgn_stages.TRENDING, "a token does not un-graduate"

    # 9. Every live gate is closed, and opening them all still trades nothing.
    assert LiveTradingGates().all_open is False
    assert set(LiveTradingGates().blocked_by()) == set(GATE_NAMES)
    assert gates_from_settings(object()).all_open is False
    provider = ShadowExecutionProvider(gates=LiveTradingGates(True, True, True))
    receipt = await provider.submit(
        ExecutionIntent(mint=mint, side="BUY", size_usd=Decimal("10"), mode=MODE_LIVE_AUTO)
    )
    assert not receipt.accepted and receipt.real_money_spent_usd == Decimal("0")
    assert provider.can_trade is False and provider.recorded == []
    shadow_receipt = await provider.submit(
        ExecutionIntent(mint=mint, side="BUY", size_usd=Decimal("10"), signal_id="s1")
    )
    assert shadow_receipt.accepted and shadow_receipt.real_money_spent_usd == Decimal("0")

    # 10. No shadow family or experiment was removed by this release.
    from smart_money_bot.lab.shadow import (
        DEFAULT_SHADOW_CONFIG,
        GMGN_FAMILIES,
        SIGNAL_FAMILIES,
    )

    for family in (
        "FAST_WATCH",
        "NOTABLE_TRADER_EARLY",
        "TRENDING_NEW_ENTRY",
        "PUMP_TRENCH_RUNNER",
        "PUBLIC_TRENDING_MODEL",
    ):
        assert family in SIGNAL_FAMILIES, f"{family} must not be removed"
    assert set(GMGN_FAMILIES) <= set(SIGNAL_FAMILIES)
    assert DEFAULT_SHADOW_CONFIG.position_usd == Decimal("10")
    assert DEFAULT_SHADOW_CONFIG.bankroll_usd == Decimal("100")
    assert DEFAULT_SHADOW_CONFIG.max_concurrent_positions == 5
    assert DEFAULT_SHADOW_CONFIG.max_total_exposure_usd == Decimal("50")

    await client.close()
    await leaky.close()
    await limited.close()


async def check_production_hardening() -> None:
    """No card says "?", and one optional endpoint cannot mute the provider.

    Both of these reached production.  Cards rendered ``?`` / ``$?`` for tokens
    whose symbol the discovery response had already carried, and a hot-search
    429 flipped the whole GMGN integration to RATE_LIMITED while trending,
    trenches and signals were answering fine.
    """

    import inspect
    import json as _json

    import smart_money_bot.bot as bot_module
    import smart_money_bot.fast_alerts as fast_alerts_module
    from smart_money_bot.engine import SmartMoneyEngine
    from smart_money_bot.gmgn import GmgnClient, GmgnError, parse_trenches_response
    from smart_money_bot.gmgn.endpoints import (
        CORE_ACTIVE,
        PARTIAL_DEGRADATION,
        EndpointRegistry,
        tier_for,
    )
    from smart_money_bot.lab.providers import BACKOFF_SECONDS, ProviderState, record_failure
    from smart_money_bot.pump_stream import PumpCreationStream
    from smart_money_bot.token_presentation import (
        PENDING_NAME,
        SOURCE_GMGN_BOARD,
        SOURCE_GMGN_TOKEN_INFO,
        TokenPresentation,
        build_presentation,
        safe_image_url,
    )
    from smart_money_bot.token_presentation import (
        merge as merge_presentation,
    )

    mint = "GPR7Ax4kQ2mVn8hLdT6yWc3JbRfE9uZsXqM1oP5tH4dK"
    clone = "7TqH1d4Vf9QG578vB99Q7ewFQPoxSYqBDxSAzBpBpump"
    secret = "gmgn-selfcheck-secret-0123456789abc"

    # 1. A token we cannot name yet says so.  It never says "?".
    blank = TokenPresentation(mint=mint)
    assert blank.display_name == PENDING_NAME
    assert "?" not in blank.display_name and blank.display_symbol != "?"
    for accessor in (
        SmartMoneyEngine._cached_token_names,
        SmartMoneyEngine._card_identity,
    ):
        accessor_source = inspect.getsource(accessor)
        assert "presentation_for" in accessor_source or "self._presentations" in (
            accessor_source
        ), f"{accessor.__name__} must read the canonical presentation record"
        assert '"?"' not in accessor_source, (
            f"the card fallback that produced `$?` must not come back in "
            f"{accessor.__name__}"
        )
        assert '"Unknown token"' not in accessor_source

    # 2. A field that is known stays known.
    known = merge_presentation(
        None,
        build_presentation(
            mint,
            name="Moo Deng Returns",
            symbol="MDR",
            image_url="https://cdn.example/mdr.png",
            source=SOURCE_GMGN_TOKEN_INFO,
        ),
    )
    partial = merge_presentation(known, build_presentation(mint, source=SOURCE_GMGN_BOARD))
    assert partial.name == "Moo Deng Returns" and partial.thumbnail

    # 3. Two same-symbol tokens never share an identity.
    other = merge_presentation(
        None,
        build_presentation(
            clone,
            name="Moo Deng Returns",
            symbol="MDR",
            image_url="https://cdn.example/clone.png",
            source=SOURCE_GMGN_TOKEN_INFO,
        ),
    )
    assert other.thumbnail != known.thumbnail
    try:
        merge_presentation(known, other)
    except ValueError:
        pass
    else:  # pragma: no cover - the guard is the point
        raise AssertionError("presentations must never merge across mints")

    # 4. Only a safe image is ever rendered.
    for unsafe in (
        "http://x/a.png",
        "/tmp/a.png",
        "https://x/a.png?api_key=abc",
        "https://x/a.png?X-Amz-Signature=abc",
    ):
        assert safe_image_url(unsafe) == "", unsafe
    assert safe_image_url("ipfs://Qm1/a.png").startswith("https://")

    # 5. Publish first, enrich the same message afterwards.  v2.51 moved the
    #    send behind the universal dispatcher; the invariant is unchanged.
    publish_source = inspect.getsource(SmartMoneyEngine._publish_fast_alert)
    assert publish_source.index("_schedule_presentation_enrichment") < publish_source.index(
        "await self._dispatch_card(alert)"
    ), "metadata resolution must never delay the alert"
    resolve_source = inspect.getsource(SmartMoneyEngine.resolve_presentation)
    for forbidden in ("symbol_search", "by_symbol", "narrative_match"):
        assert forbidden not in resolve_source, (
            "exact-mint resolution must never fall back to a ticker search"
        )
    update = fast_alerts_module.enrichment_from_presentation(
        alert_key="k", mint=mint, presentation=known, fomo_url="https://fomo.family/coin"
    )
    assert "Moo Deng Returns" in update.description
    assert update.thumbnail_url == "https://cdn.example/mdr.png"
    # An empty update must not blank a card that already had content.
    spec = fast_alerts_module.CardSpec(
        title="t", description="**Real**", thumbnail_url="https://cdn.example/a.png"
    )
    kept = fast_alerts_module.EnrichmentUpdate(alert_key="k").apply(spec)
    assert kept.description == "**Real**" and kept.thumbnail_url

    # 6. The trenches response key is ``pump``; reading it wrong loses the
    #    entire FINAL STRETCH section.
    sections = parse_trenches_response({"pump": [{"address": mint}]})
    assert "near_completion" in sections and "pump" not in sections

    # 7. An optional endpoint's 429 cools that endpoint and nothing else.
    class _Response:
        def __init__(self, payload, status=200, headers=None):
            self._payload, self.status = payload, status
            self.headers = headers or {}

        async def text(self):
            return _json.dumps(self._payload)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Session:
        closed = False

        def __init__(self, handler):
            self._handler, self.requests = handler, []

        def request(self, method, url, **kwargs):
            self.requests.append(url)
            return self._handler(method, url, kwargs)

    def handler(method, url, kwargs):
        if "hot_searches" in url:
            return _Response({"code": 429, "error": "RATE_LIMIT_EXCEEDED"}, 429)
        return _Response({"code": 0, "data": {"rank": [{"address": mint}]}})

    client = GmgnClient(api_key=secret, session=_Session(handler))
    with contextlib.suppress(GmgnError):
        await client.hot_searches()
    assert len(await client.trending(interval="1m")) == 1, (
        "a hot-search rate limit must not stop core discovery"
    )
    health = client.endpoints.to_json()
    assert health["summary"] == PARTIAL_DEGRADATION
    by_kind = {item["kind"]: item for item in health["endpoints"]}
    assert by_kind["hot_searches"]["cooling"] and by_kind["rank"]["healthy"]
    before = len(client._session.requests)
    with contextlib.suppress(GmgnError):
        await client.hot_searches()
    assert len(client._session.requests) == before, "no probing during a cooldown"
    assert secret not in _json.dumps(client.usage_snapshot())
    await client.close()

    # 8. Discovery is protected; attention is shed first.
    assert tier_for("rank") == "A" and tier_for("trenches") == "A"
    assert tier_for("hot_searches") == "C"
    registry = EndpointRegistry()
    assert registry.admits("rank", budget_headroom=0.01)[0] is True
    assert registry.admits("hot_searches", budget_headroom=0.2)[0] is False
    registry.note_success("rank", rows=1)
    registry.disable("hot_searches")
    assert registry.summary() == CORE_ACTIVE, (
        "an endpoint switched off on purpose is not a degradation"
    )

    # 9. The Pump lane names its fault and never calls polling a websocket.
    stream = PumpCreationStream(rpc_url="https://api.mainnet-beta.solana.com")
    status = stream.status()
    for key in ("subscribe_acks", "notifications", "stale_rebuilds", "ack_timeouts"):
        assert key in status, f"the Pump lane must report {key}"
    assert status["fallback_source"] in {"", "GMGN_TRENCH_POLLING"}
    assert "websocket" not in str(status["fallback_source"]).lower()
    run_source = inspect.getsource(PumpCreationStream.run)
    assert "healthy_run" in run_source, (
        "a silent socket must not reset the backoff — that is the reconnect spin"
    )

    # 10. A spent quota backs off for the long window immediately.
    exhausted = record_failure(
        ProviderState(name="solana_tracker"),
        now=0.0,
        status=403,
        message='{"error":"Insufficient credits for this request"}',
    )
    assert exhausted.degraded_until == BACKOFF_SECONDS[-1]

    # 11. The realtime panel carries GMGN, and no credential.
    realtime_source = inspect.getsource(bot_module._realtime_embed)
    assert "GMGN ALPHA" in realtime_source and "PUMP REALTIME" in realtime_source
    for forbidden in ("api_key", "X-APIKEY", "GMGN_API_KEY"):
        assert forbidden not in realtime_source


async def check_clone_defence() -> None:
    """Two live mints, one name, and both cards said "Symbol collision: NO".

    The deploy gate for v2.47.  Three defects produced that alert pair and each
    one is checked here directly, because each one is the kind that comes back
    quietly: a table missing from a list, a budget shared between two unrelated
    things, and a card builder that forgets to ask.
    """

    import inspect
    from dataclasses import replace
    from decimal import Decimal as _Decimal

    import smart_money_bot.engine as engine_module
    import smart_money_bot.fast_alerts as fast_alerts_module
    from smart_money_bot.database import Database
    from smart_money_bot.engine import SmartMoneyEngine
    from smart_money_bot.lab.clone import (
        ORIGINAL,
        SUSPECTED_CLONE,
        TokenFacts,
        classify_clone,
    )
    from smart_money_bot.lab.tokenquality import rank_candidates, score_quality

    real_mint = "3DV5zV8sQhRtYwXnLp2CkAaB7mNfE9uJqZrGdTxfXUjp"
    copy_mint = "J8GLnJ7Qk2m5t9WcQeF3bXn4Zr8vH1sYp6uJdLxAKpump"
    real = TokenFacts(
        mint=real_mint,
        name="Sock And Pussy 500",
        symbol="$SNP-500",
        created_at=1_000_000,
        first_seen_at=1_000_000,
        age_seconds=420,
        liquidity_usd=_Decimal("15180"),
        volume_usd=_Decimal("64000"),
        holder_count=520,
        buys=450,
        sells=438,
        total_fee_sol=_Decimal("9.6"),
        top10_holder_rate=_Decimal("0.19"),
        dev_hold_rate=_Decimal("0.02"),
        bundler_rate=_Decimal("0.08"),
        sniper_hold_rate=_Decimal("0.11"),
        insider_rate=_Decimal("0.07"),
    )
    copy = TokenFacts(
        mint=copy_mint,
        name="Sock and Pussy 500",
        symbol="SNP500",
        created_at=1_000_120,
        first_seen_at=1_000_120,
        age_seconds=300,
        liquidity_usd=_Decimal("12080"),
        volume_usd=_Decimal("21000"),
        holder_count=180,
        buys=399,
        sells=334,
        total_fee_sol=_Decimal("0.9"),
        top10_holder_rate=_Decimal("0.42"),
        dev_hold_rate=_Decimal("0.05"),
        bundler_rate=_Decimal("0.22"),
        sniper_hold_rate=_Decimal("0.28"),
        insider_rate=_Decimal("0.18"),
    )

    # 1. Case and punctuation must not hide the collision.  "$SNP-500" against
    #    "SNP500" is the whole reason the fold exists.
    assert real.identity_key == copy.identity_key

    # 2. The later mint is named a copy and loses its ping; the earlier one
    #    keeps its alert.  Being imitated is not a reason to lose the signal.
    assert classify_clone(copy, [real]).verdict == SUSPECTED_CLONE
    assert classify_clone(copy, [real]).may_ping is False
    assert classify_clone(real, [copy]).verdict == ORIGINAL
    assert classify_clone(real, [copy]).may_ping is True

    # 3. Order beats depth.  A copy that pumps harder for five minutes is still
    #    a copy, and depth may never buy it an interruption.
    loud = replace(
        copy,
        liquidity_usd=_Decimal("90000"),
        volume_usd=_Decimal("400000"),
        total_fee_sol=_Decimal("40"),
    )
    assert classify_clone(loud, [real]).may_ping is False

    # 4. The copy is still published, with a warning.  Hiding it leaves the
    #    operator exactly as blind as "Symbol collision: NO" did.
    assert "SUSPECTED COPY" in classify_clone(copy, [real]).warning_line()

    # 5. Fee velocity is a rate.  0.5 SOL in two minutes and 0.5 SOL in four
    #    hours are different tokens wearing the same number.
    assert real.fee_velocity_sol_per_minute > copy.fee_velocity_sol_per_minute
    assert score_quality(real).score > score_quality(copy).score

    # 6. An unmeasured token never wins by being unknown, and is never called
    #    thin either — "we could not look" is not "there is nothing there".
    blind = TokenFacts(mint="x", name="X", symbol="X", liquidity_usd=_Decimal("20000"))
    assert score_quality(blind).confident() is False
    assert score_quality(blind).weak() is False

    # 7. Ranking, not feed order.  A real runner behind two hundred dead
    #    launches was the 424-second alert.
    dead = [
        TokenFacts(
            mint=f"dead{index}",
            name=f"Dead {index}",
            symbol=f"D{index}",
            age_seconds=1_200,
            liquidity_usd=_Decimal("500"),
            volume_usd=_Decimal("50"),
            holder_count=3,
            buys=1,
            sells=0,
            total_fee_sol=_Decimal("0.001"),
        )
        for index in range(200)
    ]
    assert rank_candidates([*dead, real])[0][0].mint == real_mint

    # 8. The collision check can see every table a mint can land in.  The bug
    #    was never the query — it was the list.
    known_source = inspect.getsource(Database.known_symbols)
    for table in (
        "token_presentations",
        "gmgn_observations",
        "pump_tokens",
        "runner_candidates",
    ):
        assert table in known_source, f"known_symbols() cannot see {table}"

    # 9. Evaluation has its own budget, and it is not GMGN's call budget.
    #    Sharing one number between them was the whole of the lateness.
    cycle_source = inspect.getsource(SmartMoneyEngine._gmgn_cycle)
    assert "gmgn_early_lane_per_scan" in cycle_source
    assert "gmgn_enrichment_per_scan" not in cycle_source
    assert cycle_source.index("rank_candidates(") < cycle_source.index("ranked[:budget]")
    # Every candidate is remembered even when only some are evaluated, or the
    # copy cannot be recognised on the scan it appears in.
    assert cycle_source.index("_note_token_facts") < cycle_source.index("budget = max(")

    # 10. One publish path, one place the rule lives, and it runs before the
    #     alert is reserved — reserving first would record a ping nobody got
    #     and then dedupe the corrected card away.
    publish_source = inspect.getsource(SmartMoneyEngine._publish_fast_alert)
    assert publish_source.index("_guard_publication") < publish_source.index(
        "reserve_fast_alert"
    )
    # v2.50.  The guard must consult QUALITY as well as the clone verdict.
    # Every gate v2.47-2.49 added lived in build_early_alert and the GMGN scan
    # loop, and the cards the operator complained about came from
    # build_promotion_alert off the hot-watch timer, which touches neither —
    # so a promotion could be titled "EARLY RUNNER - LOOK NOW" above a body
    # reporting -63.70% over five minutes.
    guard_source = inspect.getsource(SmartMoneyEngine._guard_publication)
    assert "_quality_scores" in guard_source
    assert "disqualified" in guard_source
    assert "strip_actionable" in guard_source
    promotion_source = inspect.getsource(SmartMoneyEngine._publish_promotion)
    assert "_quality_check" in promotion_source, (
        "the promotion path still never asks whether this is an entry"
    )
    assert "if quality.disqualified:" in promotion_source
    assert promotion_source.index("_quality_check") < promotion_source.index(
        "build_promotion_alert"
    )

    # 10b. v2.51: exactly one path in the codebase may send a card.  v2.50
    #      guarded one call site out of sixteen and reported the bypass class
    #      fixed; three builders — trending, trench and the trending radar —
    #      were reaching Discord directly, skipping the dedupe reservation too.
    import re as _re

    engine_source = inspect.getsource(engine_module)
    sites = _re.findall(r"notifier\.on_fast_alert\(", engine_source)
    assert len(sites) == 1, (
        f"{len(sites)} paths can send a card; exactly one (the dispatcher) may"
    )
    dispatch_source = inspect.getsource(SmartMoneyEngine._dispatch_card)
    assert dispatch_source.index("_guard_publication") < dispatch_source.index(
        "notifier.on_fast_alert"
    )
    # The guarded card is the one that goes out; guarding a copy and sending
    # the original would be a silent no-op.
    assert "on_fast_alert(guarded)" in dispatch_source

    # 11. The card gates on both answers and prints both.
    card_source = inspect.getsource(fast_alerts_module.build_early_alert)
    assert "clone_ok" in card_source and "quality_ok" in card_source
    assert "REAL MONEY" in card_source

    # 12. The early lane can still answer "why wasn't I pinged?".
    lane_source = inspect.getsource(SmartMoneyEngine._early_lane_task)
    for reason in (
        "EARLY_WHY_SUSPECTED_CLONE",
        "EARLY_WHY_NAME_COLLISION",
        "EARLY_WHY_THIN_QUALITY",
    ):
        assert reason in lane_source

    # 13. The new strategy modules stay pure logic: no provider, no database,
    #     no signer, and nothing that could spend a lamport.
    import smart_money_bot.lab.clone as clone_module
    import smart_money_bot.lab.tokenquality as quality_module

    for module in (clone_module, quality_module):
        source = inspect.getsource(module)
        for forbidden in (
            "import aiohttp",
            "import requests",
            "aiosqlite",
            "from solders",
            "private_key",
            "cookies=",
            'getenv("GMGN_PRIVATE_KEY"',
        ):
            assert forbidden not in source, f"{module.__name__} must stay pure logic"


async def check_direction_not_level() -> None:
    """A dying token produces the biggest volume of its life.  v2.47 rewarded it.

    The deploy gate for v2.48, built from two rows the operator screenshotted.
    POKEMON at -99.8% with 3,400 sells against 252 buys scored 42/100 and
    passed; ISABELLA with three holders and -47.1% on every timeframe scored
    56.88 and returned ``strong() is True``.  Both earned *full* marks on
    volume, depth ratio and transactions, because the scorer measured levels
    and levels cannot tell a run from a rug.
    """

    import inspect
    from decimal import Decimal as _Decimal

    from smart_money_bot.config import Settings
    from smart_money_bot.engine import SmartMoneyEngine
    from smart_money_bot.lab.clone import TokenFacts
    from smart_money_bot.lab.tokenquality import rank_candidates, score_quality

    pokemon = TokenFacts(
        mint="POKEMONmint", name="Pokemon", symbol="POKEMON", age_seconds=7_200,
        liquidity_usd=_Decimal("3500"), volume_usd=_Decimal("40000"),
        market_cap_usd=_Decimal("1700"), ath_market_cap_usd=_Decimal("850000"),
        holder_count=252, buys=252, sells=3_400, total_fee_sol=_Decimal("0.021"),
        price_change_5m_percent=_Decimal("-30"),
    )
    isabella = TokenFacts(
        mint="ISABELLAmint", name="Isabella Cognita", symbol="ISABELLA", age_seconds=34,
        liquidity_usd=_Decimal("6260"), volume_usd=_Decimal("12000"),
        market_cap_usd=_Decimal("3080"), holder_count=3, buys=2, sells=1,
        total_fee_sol=_Decimal("0.575"), price_change_1m_percent=_Decimal("-20.3"),
    )

    # 1. Both are refused outright, and score zero rather than merely low — a
    #    residual score would keep a dying chart near the top of the ranking.
    for facts in (pokemon, isabella):
        result = score_quality(facts)
        assert result.disqualified, f"{facts.symbol} still passes"
        assert result.score == 0
        assert result.weak() is True
        assert result.strong() is False

    # 2. The specific v2.47 defect: volume, depth ratio and transactions are
    #    the three figures a dump maximises, and all three paid full marks.
    components = dict(score_quality(pokemon).components)
    for family in ("volume", "volume vs liquidity", "transactions"):
        assert _Decimal(components[family]) == 0, f"{family} still rewards a dump"

    # 2b. Each refusal must stand on its own.  POKEMON trips two of them at
    #     once, so it cannot prove either: these are one-at-a-time cases with
    #     everything else healthy.
    only_selling = TokenFacts(
        mint="S", name="S", symbol="S", age_seconds=600,
        liquidity_usd=_Decimal("20000"), volume_usd=_Decimal("80000"),
        market_cap_usd=_Decimal("50000"), ath_market_cap_usd=_Decimal("51000"),
        holder_count=400, buys=100, sells=420, total_fee_sol=_Decimal("4"),
        price_change_1m_percent=_Decimal("5"),
    )
    assert score_quality(only_selling).disqualified, "sell-pressure refusal is off"
    only_drawn_down = TokenFacts(
        mint="D", name="D", symbol="D", age_seconds=600,
        liquidity_usd=_Decimal("20000"), volume_usd=_Decimal("80000"),
        market_cap_usd=_Decimal("15000"), ath_market_cap_usd=_Decimal("120000"),
        holder_count=400, buys=300, sells=250, total_fee_sol=_Decimal("4"),
        price_change_1m_percent=_Decimal("2"),
    )
    assert score_quality(only_drawn_down).disqualified, "drawdown refusal is off"
    only_empty = TokenFacts(
        mint="E", name="E", symbol="E", age_seconds=600,
        liquidity_usd=_Decimal("20000"), volume_usd=_Decimal("80000"),
        market_cap_usd=_Decimal("50000"), ath_market_cap_usd=_Decimal("51000"),
        holder_count=4, buys=300, sells=250, total_fee_sol=_Decimal("4"),
        price_change_1m_percent=_Decimal("5"),
    )
    assert score_quality(only_empty).disqualified, "holder-floor refusal is off"
    only_collapsing = TokenFacts(
        mint="C", name="C", symbol="C", age_seconds=600,
        liquidity_usd=_Decimal("20000"), volume_usd=_Decimal("80000"),
        market_cap_usd=_Decimal("50000"), ath_market_cap_usd=_Decimal("51000"),
        holder_count=400, buys=300, sells=250, total_fee_sol=_Decimal("4"),
        price_change_1m_percent=_Decimal("-70"),
    )
    assert score_quality(only_collapsing).disqualified, "collapse refusal is off"

    # 3. The same token while it is actually running still pings.  Refusing
    #    corpses is only half the job; refusing everything is not the other half.
    running = TokenFacts(
        mint="RETAmint", name="peptidezz", symbol="RETA", age_seconds=120,
        liquidity_usd=_Decimal("22600"), volume_usd=_Decimal("27500"),
        market_cap_usd=_Decimal("131000"), ath_market_cap_usd=_Decimal("133000"),
        holder_count=191, buys=140, sells=91, total_fee_sol=_Decimal("0.42"),
        price_change_1m_percent=_Decimal("45.0"), top10_holder_rate=_Decimal("0.34"),
        dev_hold_rate=_Decimal("0.02"), bundler_rate=_Decimal("0.005"),
        sniper_hold_rate=_Decimal("0.09"), insider_rate=_Decimal("0.21"),
    )
    assert score_quality(running).strong() is True
    assert rank_candidates([pokemon, isabella, running])[0][0].mint == "RETAmint"

    # 4. A sixty-second-old token is thin because it is EARLY, which is the
    #    moment the operator asked to hear about it.  The score bar must not
    #    withhold anything from a token we could not properly see.
    grok = TokenFacts(
        mint="GROKmint", name="Grok Pocket", symbol="GROK", age_seconds=60,
        liquidity_usd=_Decimal("6900"), volume_usd=_Decimal("5200"),
        market_cap_usd=_Decimal("31180"), buys=26, sells=6,
        price_change_5m_percent=_Decimal("14"),
    )
    early = score_quality(grok)
    assert early.disqualified is False
    assert early.confident() is False
    assert early.weak() is False

    # 5. Unknown is never disqualifying.  The rule v2.47 established, extended.
    blind = TokenFacts(mint="U", name="U", symbol="U", liquidity_usd=_Decimal("12000"))
    assert score_quality(blind).disqualified is False

    # 6. The three columns that make all this possible were already arriving on
    #    every board row and were being dropped.
    facts_source = inspect.getsource(SmartMoneyEngine._facts_from_gmgn)
    for field in (
        "price_change_1m_percent",
        "history_highest_market_cap_usd",
    ):
        assert field in facts_source, f"{field} dropped from the GMGN row again"

    # 7. Looking widely and alerting widely are different things.  v2.47 raised
    #    evaluation 6 -> 60 and left publishing uncapped, which is the ten times
    #    the cards the operator reported.
    cycle_source = inspect.getsource(SmartMoneyEngine._gmgn_cycle)
    assert "card_budget" in cycle_source
    assert cycle_source.index("rank_candidates(") < cycle_source.index("card_budget")
    settings_fields = set(Settings.__dataclass_fields__)
    assert "gmgn_early_lane_max_cards_per_scan" in settings_fields

    # 7b. Trending only, by instruction.  The trenches board is three sections
    #     of up to sixty rows each, all minutes old, and it was landing in the
    #     same candidate list as Trending and burying it.
    assert "gmgn_trending_only" in settings_fields
    assert "FAMILY_GMGN_TRENDING" in cycle_source
    # The filter must run AFTER the same-name cache is filled: the copy
    # detection needs the wide view, because a trench launch is exactly what
    # clones a trending token.
    assert cycle_source.index("_note_token_facts") < cycle_source.index(
        "gmgn_trending_only"
    )

    # 8. ...but the cap gates the CARD, never the analysis: the first-seen
    #    market cap and the watch list both survive a spent budget.
    lane_source = inspect.getsource(SmartMoneyEngine._early_lane_task)
    assert "may_publish" in lane_source
    skip_at = lane_source.index("if not may_publish:")
    assert lane_source.index("STAGE_BOT_FIRST_SEEN") < skip_at, (
        "the card budget must not skip recording the first market cap we saw"
    )
    assert "_open_early_watch" in lane_source[skip_at:], (
        "a budget-skipped candidate must still reach the watch list"
    )
    assert "EARLY_WHY_SCAN_BUDGET" in lane_source



async def check_paper_laboratory() -> None:
    """The invariants that must hold before this release may ever be trusted.

    These are deliberately the non-negotiables from the product contract, not a
    happy path: no live execution, safety never becomes PASS by omission, an old
    pump never returns as a fresh setup, no public account can enter or launch,
    and the broad social radar stays off.
    """

    import smart_money_bot.lab as lab
    from smart_money_bot.lab.decision import Decision, Reason
    from smart_money_bot.lab.entry import EntryContext, evaluate_entry
    from smart_money_bot.lab.lifecycle import (
        FIRST_DISCOVERY,
        LifecycleObservation,
        advance_lifecycle,
        new_lifecycle,
    )
    from smart_money_bot.lab.registry import (
        IDEA_ONLY_ACCOUNTS,
        TIER_A_ACCOUNTS,
        TIER_B_ACCOUNTS,
        TIER_C_ACCOUNTS,
    )

    assert lab.LIVE_EXECUTION_ENABLED is False, "live execution must stay disabled"
    assert lab.DEFAULT_LAB_CONFIG.broad_social_radar_enabled is False
    assert lab.DEFAULT_LAB_CONFIG.bankroll_usd == Decimal("100")
    assert lab.DEFAULT_LAB_CONFIG.normal_position_usd == Decimal("5")
    assert lab.DEFAULT_LAB_CONFIG.max_position_usd == Decimal("10")

    for account in (*TIER_A_ACCOUNTS, *TIER_B_ACCOUNTS, *TIER_C_ACCOUNTS, *IDEA_ONLY_ACCOUNTS):
        assert account.can_enter is False
        assert account.can_launch is False
    for account in IDEA_ONLY_ACCOUNTS:
        assert account.can_qualify_token is False

    # Missing evidence must never become PASS.
    blank = evaluate_entry(
        EntryContext(mint=TOKEN, now=1_000),
        lifecycle=new_lifecycle(TOKEN, now=0),
        bankroll=lab.BankrollState(),
    )
    assert not blank.entry_eligible
    assert Reason.SAFETY_UNKNOWN in blank.decision.reason_codes
    assert blank.decision.decision is not Decision.ENTRY

    # An old pump is never a fresh setup again.
    record = new_lifecycle(TOKEN, now=0)
    for at, price, market_cap, extra in (
        (10, "0.000032", "32000", {"surfaced": True, "qualified": True}),
        (60, "0.00015", "150000", {}),
        (120, "0.000038", "38000", {}),
    ):
        record = advance_lifecycle(
            record,
            LifecycleObservation(
                observed_at=at,
                price_usd=Decimal(price),
                market_cap_usd=Decimal(market_cap),
                **extra,
            ),
        )
    assert record.state != FIRST_DISCOVERY
    assert record.first_surface_market_cap_usd == Decimal("32000")
    assert record.historical_high_market_cap_usd == Decimal("150000")
    assert not record.is_fresh_setup

    rehydrated = lab.lifecycle_from_json(lab.lifecycle_to_json(record))
    assert rehydrated == record

    # Only NET PnL counts.
    cost = lab.estimate_round_trip_cost(Decimal("5"), buy_price_impact_percent=Decimal("1"))
    assert cost.total_cost_usd > 0
    assert cost.platform_fees_usd > 0 and cost.network_fees_usd > 0

    # --- v2.37 invariants -------------------------------------------------
    from smart_money_bot.discord_render import (
        MESSAGE_EMBED_LIMIT,
        SAFE_MESSAGE_BUDGET,
        CardField,
        CardSpec,
        render_message,
    )
    from smart_money_bot.lab.actionability import ActionabilityInputs, assess_actionability
    from smart_money_bot.lab.fastwatch import FastWatchSignals, evaluate_fast_watch
    from smart_money_bot.lab.latency import HISTORICAL, LatencySample

    # FAST WATCH is research visibility only and can never authorise an entry.
    hot = FastWatchSignals(
        now=1_000,
        pair_age_seconds=300,
        price_change_percent=Decimal("25"),
        volume_acceleration_ratio=Decimal("2"),
        buys=90,
        sells=20,
        liquidity_usd=Decimal("30000"),
        route_available=True,
    )
    watch = evaluate_fast_watch(hot)
    assert watch.entry_eligible is False, "FAST WATCH must never be entry eligible"
    assert watch.pending_evidence, "FAST WATCH must declare the evidence it skipped"

    # A pair created long before we saw it is historical, not ingestion latency.
    stale_timing = LatencySample(
        mint=TOKEN, source_name="feed", source_event_at=1_000, first_seen_at=1_000 + 67_620
    )
    assert stale_timing.timing_quality == HISTORICAL
    assert not stale_timing.counts_as_realtime

    # A materially negative, fading candidate is kept out of the current radar.
    jelly = assess_actionability(
        ActionabilityInputs(
            now=10_000,
            first_seen_at=4_000,
            return_since_first_seen_percent=Decimal("-21"),
            momentum_score=Decimal("20"),
            buys=5,
            sells=30,
        )
    )
    assert jelly.suppressed, "a deteriorated candidate must not rank beside fresh ones"

    # Several rich cards must fit one Discord message.
    card = CardSpec(
        title="Card",
        description="D" * 400,
        compact_description="Token `MINT`",
        fields=tuple(CardField(f"F{index}", "v" * 900) for index in range(6)),
    )
    embeds, _ = render_message([card] * 5)
    total = sum(len(item) for item in embeds)
    assert total <= SAFE_MESSAGE_BUDGET <= MESSAGE_EMBED_LIMIT

    # --- v2.38 invariants -------------------------------------------------
    from smart_money_bot import fast_alerts as fast
    from smart_money_bot.lab.catalyst import (
        CONNECTION_OFFICIAL,
        M_CIRCULAR_SOURCING,
        CatalystEvent,
        ConfluenceInputs,
        EventSource,
        assess_event,
        assess_token_link,
        classify_catalyst_alert,
    )
    from smart_money_bot.lab.fastwatch import still_current
    from smart_money_bot.lab.notable import (
        EDGE_CONSUMED,
        ONCHAIN_ONLY,
        NotableSignal,
        NotableTrade,
        NotableWallet,
        build_consensus,
        decide_ping,
    )

    # FAST WATCH now has a publication path, and it is still not entry eligible.
    watch_alert = fast.build_fast_watch_alert(
        mint=TOKEN,
        name="Token",
        symbol="TKN",
        fomo_url="https://fomo.biz/token/x",
        verdict=watch,
        age_seconds=300,
        market_cap_usd=Decimal("90000"),
        first_seen_market_cap_usd=Decimal("60000"),
        liquidity_usd=Decimal("30000"),
        move_since_first_seen_percent=Decimal("50"),
        momentum_score=Decimal("70"),
        organic_score=None,
        buys=90,
        sells=20,
    )
    assert watch_alert.entry_eligible is False, "a published FAST WATCH cannot be an entry"
    assert watch_alert.may_ping is False, "FAST WATCH must never interrupt the user"
    assert any(item.name == "SAFETY" for item in watch_alert.spec.fields)

    # A queued candidate cannot publish as "early" after the move happened.
    queued_ok, queued_reason = still_current(hot, first_seen_at=hot.now - 3_600)
    assert queued_ok is False and "queued" in queued_reason

    # An unmapped wallet is never given an identity.
    anon = NotableWallet(wallet=WALLET, provenance=ONCHAIN_ONLY, anonymous_index=17)
    assert anon.identified is False and anon.display_name() == "Wallet #17"
    try:
        NotableWallet(wallet=WALLET, label="Someone", provenance=ONCHAIN_ONLY)
    except ValueError:
        pass
    else:  # pragma: no cover - the guard must hold
        raise AssertionError("an anonymous wallet must never carry a public label")

    # Lateness is quantified and published, and a late signal is never chased.
    late_trade = NotableTrade(
        wallet=WALLET,
        mint=TOKEN,
        signature="sig",
        chain_time=1_000,
        observed_at=1_004,
        entry_market_cap_usd=Decimal("48000"),
    )
    late_signal = NotableSignal(
        trade=late_trade,
        wallet_profile=anon,
        detection_market_cap_usd=Decimal("50000"),
        current_market_cap_usd=Decimal("500000"),
        now=1_030,
    )
    assert late_signal.freshness() == EDGE_CONSUMED
    assert late_signal.may_chase() is False
    assert decide_ping(late_signal).ping is False
    late_card = fast.build_notable_trader_alert(
        signal=late_signal, fomo_url="u", name="Token", symbol="TKN"
    )
    assert late_card.kind == fast.NOTABLE_TRADER_LATE
    assert late_card.may_ping is False and late_card.entry_eligible is False

    # A funded swarm is one actor, never several confirmations.
    swarm = [
        NotableSignal(
            trade=NotableTrade(
                wallet=f"w{index}",
                mint=TOKEN,
                signature=f"s{index}",
                chain_time=1_000,
                observed_at=1_002,
                entry_market_cap_usd=Decimal("48000"),
            ),
            wallet_profile=anon,
            current_market_cap_usd=Decimal("54000"),
            now=1_030,
        )
        for index in range(4)
    ]
    clustered = build_consensus(swarm, cluster_of={f"w{i}": "funder" for i in range(4)})
    assert clustered.raw_wallets == 4 and clustered.independent_wallets == 1
    assert clustered.is_independent_consensus is False

    # A quoted repost is not an independent confirmation.
    primary = EventSource(
        name="Official",
        published_at=900,
        is_primary=True,
        account_verified=True,
        tier="TIER_A_OFFICIAL",
        content_hash="p",
    )
    quoting = EventSource(
        name="Repost", published_at=940, quotes_source="Official", content_hash="q"
    )
    circular = assess_event(
        CatalystEvent(
            event_id="evt",
            headline="Exchange lists a Solana memecoin",
            detected_at=1_000,
            occurred_at=900,
            sources=(primary, quoting),
            crypto_relevance=Decimal("90"),
        ),
        now=1_000,
    )
    assert circular.independent_confirmations == 0
    assert M_CIRCULAR_SOURCING in circular.markers

    # A verified event is never evidence that a token is real.
    verified = assess_event(
        CatalystEvent(
            event_id="evt2",
            headline="Exchange lists a Solana memecoin",
            detected_at=1_000,
            occurred_at=900,
            sources=(
                primary,
                EventSource(name="A", published_at=930, account_verified=True, content_hash="a"),
                EventSource(name="B", published_at=935, account_verified=True, content_hash="b"),
            ),
            discussion_velocity=Decimal("80"),
            novelty=Decimal("90"),
            crypto_relevance=Decimal("95"),
        ),
        now=1_000,
    )
    link = assess_token_link(
        mint=TOKEN,
        event=verified,
        name_similarity=Decimal("100"),
        minted_after_event=True,
        seconds_after_event=120,
    )
    assert link.connection != CONNECTION_OFFICIAL
    assert link.official is False, "only the event's own source can make a link OFFICIAL"

    # Confluence raises priority, never eligibility.
    convergence = classify_catalyst_alert(
        ConfluenceInputs(
            event=verified,
            link=assess_token_link(
                mint=TOKEN,
                event=verified,
                name_similarity=Decimal("100"),
                minted_after_event=True,
                seconds_after_event=90,
            ),
            token_age_seconds=180,
            independent_notable_wallets=3,
            proven_early_wallets=2,
            earliest_notable_entry_market_cap_usd=Decimal("40000"),
            current_market_cap_usd=Decimal("52000"),
            independent_buyers_accelerating=True,
            liquidity_growing=True,
            current_actionability=Decimal("75"),
            safety_status="PASS",
        ),
        now=1_000,
    )
    assert convergence.entry_eligible is False, "confluence must never authorise an entry"
    assert any("EVENT VERIFIED" in item for item in convergence.warnings)

    # A degraded provider becomes UNKNOWN, never PASS by omission.
    degraded = fast.enrichment_from_evidence(
        alert_key="k", safety_status="PASS", provider_degraded="Solana Tracker"
    )
    safety_field = next(item for item in degraded.fields if item.name == "SAFETY")
    assert "UNKNOWN" in safety_field.value and "**PASS**" not in safety_field.value

    # Only the three urgent classes may interrupt the user.
    assert fast.FAST_WATCH not in fast.PINGABLE
    assert fast.NOTABLE_TRADER_LATE not in fast.PINGABLE
    assert fast.CATALYST_WATCH not in fast.PINGABLE


async def check_shadow_auto_trader() -> None:
    """The non-negotiables of the $100 / $10 forward experiment.

    These are the claims the whole experiment rests on: every entry is exactly
    $10, the book cannot exceed 5 positions or $50, the two strategy families
    never share state, and nothing in the shadow path can spend real money.
    """

    import ast
    import importlib
    import inspect
    from contextlib import suppress

    from smart_money_bot.database import Database
    from smart_money_bot.lab.bankroll import BankrollState
    from smart_money_bot.lab.config import DEFAULT_LAB_CONFIG
    from smart_money_bot.lab.shadow import (
        DEFAULT_SHADOW_CONFIG,
        SHADOW_REAL_MONEY_SPEND,
        SIGNAL_FAMILIES,
        ShadowConfig,
        ShadowExposure,
        ShadowSignal,
        ShadowTimestamps,
        evaluate_shadow_entry,
    )
    from smart_money_bot.lab.venues import (
        FILL_FALLBACK_PENALISED,
        BondingCurveState,
        bonding_curve_quote,
    )
    from smart_money_bot.shadow_runtime import ShadowRuntime
    from smart_money_bot.shadow_store import ShadowStore

    config = DEFAULT_SHADOW_CONFIG
    assert config.position_usd == Decimal("10"), "every shadow entry must be exactly $10"
    assert config.min_position_usd == Decimal("10"), "there is no $5 shadow entry"
    assert config.max_position_usd == Decimal("10"), "no signal may buy more than $10"
    assert config.bankroll_usd == Decimal("100")
    assert config.max_concurrent_positions == 5
    assert config.max_total_exposure_usd == Decimal("50")
    assert config.max_token_exposure_usd == Decimal("10")
    assert config.net_profit_objective_usd == Decimal("2")

    # A misconfigured stake must fail loudly, never silently skew the cohorts.
    try:
        ShadowConfig(position_usd=Decimal("5"))
    except ValueError:
        pass
    else:  # pragma: no cover - the guard above must raise
        raise AssertionError("a $5 shadow configuration must be refused")

    assert not SHADOW_REAL_MONEY_SPEND, "SHADOW_REAL_MONEY_SPEND must be zero"

    # No shadow module may reach a signer, a wallet or a swap submission.
    forbidden = {
        "Keypair",
        "sign_message",
        "sign_versioned_transaction",
        "VersionedTransaction",
        "execute_order",
        "load_keypair",
        "JupiterClient",
    }
    for module_name in (
        "smart_money_bot.lab.shadow",
        "smart_money_bot.lab.shadow_exits",
        "smart_money_bot.lab.shadow_metrics",
        "smart_money_bot.lab.venues",
        "smart_money_bot.shadow_runtime",
        "smart_money_bot.shadow_store",
    ):
        module = importlib.import_module(module_name)
        tree = ast.parse(inspect.getsource(module))
        names = {
            node.id if isinstance(node, ast.Name) else node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Name | ast.Attribute)
        }
        leaked = names & forbidden
        assert not leaked, f"{module_name} must not reference {leaked}"

    # STRICT PAPER and SHADOW are different strategy families.
    assert DEFAULT_SHADOW_CONFIG.strategy_version != DEFAULT_LAB_CONFIG.strategy_version

    def signal(mint: str, family: str) -> ShadowSignal:
        return ShadowSignal(
            mint=mint,
            family=family,
            timestamps=ShadowTimestamps(signal_at=1_000, decision_at=1_000),
            price_usd=Decimal("0.001"),
            market_cap_usd=Decimal("60000"),
            liquidity_usd=Decimal("40000"),
            buys=80,
            sells=20,
            route_available=True,
        )

    state = BankrollState(
        starting_usd=Decimal("100"),
        cash_usd=Decimal("100"),
        peak_equity_usd=Decimal("100"),
    )
    for family in SIGNAL_FAMILIES:
        decision = evaluate_shadow_entry(signal("mint-a", family), state)
        assert decision.accepted, f"{family} must be able to open a shadow trade"
        assert decision.size_usd == Decimal("10"), f"{family} must deploy exactly $10"

    # Only $7 left is refused honestly, never rounded into a fake $10 trade.
    thin = BankrollState(
        starting_usd=Decimal("100"),
        cash_usd=Decimal("7"),
        open_exposure_usd=Decimal("40"),
        peak_equity_usd=Decimal("47"),
    )
    refused = evaluate_shadow_entry(
        signal("mint-b", SIGNAL_FAMILIES[0]),
        thin,
        ShadowExposure(open_positions=4, open_exposure_usd=Decimal("40")),
    )
    assert not refused.accepted and refused.size_usd == Decimal("0")

    # A completed bonding curve refuses to invent a price for a $10 buy.
    completed = bonding_curve_quote(
        BondingCurveState(
            virtual_sol_reserves=Decimal("32"),
            virtual_token_reserves=Decimal("1073000000"),
            complete=True,
            sol_price_usd=Decimal("150"),
        ),
        side="BUY",
        notional_usd=Decimal("10"),
    )
    assert not completed.usable, "a graduated curve must not price a bonding-curve buy"

    # A fallback price is always labelled, never presented as an executable fill.
    from smart_money_bot.lab.venues import fallback_quote

    fallback = fallback_quote(
        side="BUY", notional_usd=Decimal("10"), observed_price_usd=Decimal("0.001")
    )
    assert fallback.source == FILL_FALLBACK_PENALISED
    assert fallback.fill_price_usd > Decimal("0.001")

    # End to end: the book stops at 5 positions and $50, and the strict PAPER
    # tables stay empty throughout.
    path = tempfile.mktemp(suffix=".db")
    database = Database(path, Decimal("1000"))
    await database.connect()
    try:
        runtime = ShadowRuntime(ShadowStore(database))
        await runtime.start_experiment(now=900)
        for index in range(7):
            await runtime.consider_signal(
                signal(f"mint-{index}", SIGNAL_FAMILIES[0]), now=1_000 + index
            )
        book = await runtime.bankroll()
        assert book.open_positions == 5, "the shadow book must stop at five positions"
        assert book.open_exposure_usd == Decimal("50"), "exposure must stop at $50"
        assert book.cash_usd == Decimal("50")

        cursor = await database.db.execute("SELECT COUNT(*) AS total FROM lab_positions")
        assert (await cursor.fetchone())["total"] == 0, "SHADOW must not touch STRICT PAPER"

        status = await runtime.status()
        assert status["live_execution_enabled"] is False
        assert not status["real_money_spend_usd"]
    finally:
        await database.close()
        with suppress(FileNotFoundError):
            os.unlink(path)


async def check_profit_optimization() -> None:
    """The invariants that keep the bot from wasting money on itself.

    Every one of these traces to something observed in production: a paid
    provider hammered every minute after its plan ran out, a failure logged with
    no message at all, and a shared exit rule that would half-sell healthy
    positions for the duration of that outage.
    """

    import ast
    import inspect

    from smart_money_bot.errors import describe_exception
    from smart_money_bot.lab.config import DEFAULT_LAB_CONFIG
    from smart_money_bot.lab.exits import EXIT_SAFETY_EMERGENCY, ExitContext, open_position
    from smart_money_bot.lab.forward import (
        MIN_SAMPLE,
        VERDICT_DISABLED,
        VERDICT_INSUFFICIENT,
        WEIGHT_CEILING,
        WEIGHT_FLOOR,
        calibrate_families,
    )
    from smart_money_bot.lab.providers import (
        BACKOFF_SECONDS,
        PROVIDER_FEATURES,
        ProviderState,
        record_failure,
    )
    from smart_money_bot.lab.shadow import DEFAULT_SHADOW_CONFIG, FAMILY_CATALYST_WATCH
    from smart_money_bot.lab.shadow_exits import (
        SHADOW_SAFETY_MONITOR,
        RunnerEvidence,
        plan_shadow_exit,
    )
    from smart_money_bot.lab.shadow_metrics import ShadowTradeRecord

    # A provider that is failing must be called less, not more.
    state = ProviderState(name="solana_tracker")
    state = record_failure(state, now=0.0, status=403, message="Insufficient credits")
    assert state.is_degraded(now=0.0), "a credit failure must open a backoff window"
    assert BACKOFF_SECONDS[-1] <= 3_600, "backoff must stay bounded"
    unknown_record = record_failure(ProviderState(name="p"), now=0.0, status=404)
    assert not unknown_record.is_degraded(now=0.0), "a 404 is not a credit failure"

    # Core detection must survive without Solana Tracker.
    tracker_features = [
        item for item in PROVIDER_FEATURES if item.provider == "solana_tracker"
    ]
    assert tracker_features, "the provider map must describe Solana Tracker"
    assert all(
        not item.essential and item.on_chain_fallback for item in tracker_features
    ), "Solana Tracker must be optional enrichment with an on-chain fallback"

    # The refresh throttle must not disengage when the pool is empty.
    from smart_money_bot import engine as engine_module

    tree = ast.parse(inspect.getsource(engine_module))
    refresh = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "refresh_discovery"
    )
    conditions = " ".join(
        ast.unparse(node) for node in ast.walk(refresh) if isinstance(node, ast.BoolOp)
    )
    assert "self._candidate_pool" not in conditions, (
        "the discovery throttle must not depend on a non-empty candidate pool"
    )

    # A failure with no message must still say what failed.
    assert describe_exception(TimeoutError()).startswith("TimeoutError"), (
        "a timeout must never log as an empty string"
    )

    # A provider outage is not a token failure.
    position = open_position(
        position_id="p",
        mint="mint",
        now=1_000,
        decision_price_usd=Decimal("0.001"),
        size_usd=Decimal("10"),
        config=DEFAULT_SHADOW_CONFIG.exit_config(),
    )
    healthy = ExitContext(
        now=1_600,
        price_usd=Decimal("0.00105"),
        liquidity_usd=Decimal("42000"),
        entry_liquidity_usd=Decimal("40000"),
        momentum_score=Decimal("70"),
        organic_score=Decimal("65"),
        buys=140,
        sells=40,
        safety_status="UNKNOWN",
        route_available=True,
    )
    unguarded = plan_shadow_exit(position, healthy, RunnerEvidence())
    guarded = plan_shadow_exit(
        position, healthy, RunnerEvidence(safety_provider_degraded=True)
    )
    assert unguarded.plan.reason_code == EXIT_SAFETY_EMERGENCY
    assert guarded.plan.reason_code == SHADOW_SAFETY_MONITOR, (
        "a provider outage must not be read as a token failure"
    )
    confirmed = plan_shadow_exit(
        position,
        dataclasses.replace(healthy, safety_status="FAIL"),
        RunnerEvidence(safety_provider_degraded=True, safety_confirmed_fail=True),
    )
    assert confirmed.plan.final and confirmed.plan.fraction == Decimal("1"), (
        "a confirmed hard safety failure must still exit immediately and in full"
    )

    # Forward weights must be bounded, and a tiny sample must do nothing.
    def _trade(family: str, net: str, index: int, reason: str = "") -> ShadowTradeRecord:
        return ShadowTradeRecord(
            position_id=f"{family}{index}",
            mint="mint",
            family=family,
            opened_at=1_000,
            closed_at=1_000 + index,
            size_usd=Decimal("10"),
            realized_net_pnl_usd=Decimal(net),
            close_reason=reason,
            open=False,
        )

    lucky = [_trade(FAMILY_CATALYST_WATCH, "90", 1)]
    weights = calibrate_families(lucky, as_of=999_999)
    assert weights[FAMILY_CATALYST_WATCH].verdict == VERDICT_INSUFFICIENT
    assert weights[FAMILY_CATALYST_WATCH].weight == Decimal("1"), (
        "one lucky coin must not move the ranking"
    )
    assert MIN_SAMPLE >= 10

    rugging = [
        _trade(FAMILY_CATALYST_WATCH, "-6", index, "SAFETY_DETERIORATION")
        for index in range(1, 31)
    ]
    disabled = calibrate_families(rugging, as_of=999_999)[FAMILY_CATALYST_WATCH]
    assert disabled.verdict == VERDICT_DISABLED and not disabled.enabled, (
        "a family that loses money and rugs must be retired"
    )
    for entry in calibrate_families(rugging, as_of=999_999).values():
        assert WEIGHT_FLOOR <= entry.weight <= WEIGHT_CEILING

    # The experiment and the strict floor are untouched.
    assert DEFAULT_SHADOW_CONFIG.bankroll_usd == Decimal("100")
    assert DEFAULT_SHADOW_CONFIG.position_usd == Decimal("10")
    assert DEFAULT_SHADOW_CONFIG.max_concurrent_positions == 5
    assert DEFAULT_SHADOW_CONFIG.max_total_exposure_usd == Decimal("50")
    assert DEFAULT_LAB_CONFIG.normal_position_usd == Decimal("5")
    assert DEFAULT_LAB_CONFIG.min_independent_buyers == 12


async def check_early_alpha() -> None:
    """The invariants behind "the bot knew at $31K and I found out at $61K".

    Being early is the whole point of the release, so the first three checks are
    about *ordering*: the cheap lane must run before deep enrichment, its verdict
    must be reachable without any provider, and the market cap it recorded must
    not be rewritten once the price has moved.  The rest is restraint — a score
    alone must not ping, a creator self-buy must not read as demand, and a token
    that merely copied a campaign link must not inherit the real story.
    """

    import ast
    import inspect

    from smart_money_bot import engine as engine_module
    from smart_money_bot.fast_alerts import PINGABLE, URGENT_CLASSES
    from smart_money_bot.lab.early import (
        BUY_INSIDER,
        EDGE_CONSUMED,
        PINGABLE_TIERS,
        TIER_EARLY_HEADS_UP,
        TIER_NONE,
        TIER_ORGANIC_RUNNER,
        WHY_INSIDER_ONLY,
        WHY_MOVE_CONSUMED,
        WHY_NOT_SERIOUS,
        EarlyConfig,
        EarlySignals,
        detect_large_buy,
        evaluate_early_signal,
    )
    from smart_money_bot.lab.exits import ExitContext, open_position
    from smart_money_bot.lab.narrative import (
        DIR_STORY_TO_TOKEN,
        DIR_TOKEN_TO_STORY,
        INHERITS_STORY,
        REL_NAME_ONLY,
        REL_PLAUSIBLE,
        NarrativeEntity,
        StorySource,
        TokenIdentityClaim,
        assess_narrative_link,
        mark_official,
    )
    from smart_money_bot.lab.shadow import DEFAULT_SHADOW_CONFIG
    from smart_money_bot.lab.shadow_exits import (
        SHADOW_SOFT_PAUSE_HOLD,
        RunnerEvidence,
        plan_shadow_exit,
    )

    now = 1_800_000_000
    mint = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"

    def signals(**overrides) -> EarlySignals:
        payload = {
            "mint": mint,
            "now": now,
            "first_seen_at": now - 8,
            "pair_age_seconds": 82,
            "market_cap_usd": Decimal("33100"),
            "first_seen_market_cap_usd": Decimal("31180"),
            "liquidity_usd": Decimal("6900"),
            "volume_5m_usd": Decimal("5200"),
            "price_change_5m_percent": Decimal("14"),
            "buys_5m": 26,
            "sells_5m": 6,
            "route_available": True,
            "independent_buyers_5m": 19,
        }
        payload.update(overrides)
        return EarlySignals(**payload)

    # 1. The cheap lane runs before the deep gather.  This ordering *is* the fix.
    radar = inspect.getsource(engine_module.SmartMoneyEngine._run_fomo_radar)
    assert radar.index("_run_early_lane") < radar.index("evaluate(mint) for mint in selected"), (
        "first operator visibility must not wait on deep enrichment"
    )

    # 2. The verdict must be reachable from a DEX snapshot alone: no provider
    #    call, no wallet forensics, no social lookup anywhere in the module.
    from smart_money_bot.lab import early as early_module

    early_tree = ast.parse(inspect.getsource(early_module))
    imported = {
        node.module.split(".")[0]
        for node in ast.walk(early_tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(early_tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not (imported & {"aiohttp", "httpx", "requests", "solana", "solders"}), (
        "the early lane must not be able to call a provider"
    )

    # 3. A grade the operator can act on, right at first sight.
    verdict = evaluate_early_signal(signals())
    assert verdict.tier == TIER_ORGANIC_RUNNER and verdict.may_ping
    assert verdict.entry_eligible is False, "the cheap lane must never authorise an entry"

    # 4. A late alert says so instead of dressing itself up as early.
    late = evaluate_early_signal(
        signals(first_seen_market_cap_usd=Decimal("31180"), market_cap_usd=Decimal("61490"))
    )
    assert late.edge_state == EDGE_CONSUMED and late.late
    assert WHY_MOVE_CONSUMED in late.why_not_pinged
    assert "EDGE CONSUMED" in late.label

    # 5. A score alone is never a reason to interrupt anyone.
    scored = evaluate_early_signal(
        signals(buys_5m=13, sells_5m=9, volume_5m_usd=Decimal("1400")),
        config=EarlyConfig(runner_min_score=Decimal("1")),
    )
    assert scored.tier == TIER_EARLY_HEADS_UP and not scored.may_ping
    assert WHY_NOT_SERIOUS in scored.why_not_pinged

    # 6. A creator self-buy is not demand.
    insider = detect_large_buy(
        signals(largest_buy_usd=Decimal("900"), largest_buy_is_creator_linked=True)
    )
    assert insider.quality == BUY_INSIDER and not insider.is_demand
    blocked = evaluate_early_signal(
        signals(largest_buy_usd=Decimal("900"), largest_buy_is_creator_linked=True, buys_5m=6)
    )
    assert blocked.tier == TIER_NONE and WHY_INSIDER_ONLY in blocked.why_not_pinged

    # 7. Only tiers that earned it may ping, and anything that may interrupt a
    #    person lands in the urgent lane.  A heads-up is radar only, always.
    assert set(PINGABLE_TIERS) == {"EARLY_RUNNER", "ORGANIC_RUNNER"}
    assert set(PINGABLE) <= set(URGENT_CLASSES), "a pingable class must ride the urgent lane"
    assert "EARLY_RUNNER" in PINGABLE
    assert "EARLY_HEADS_UP" not in PINGABLE and "EARLY_HEADS_UP" not in URGENT_CLASSES

    # 8. MINT IS IDENTITY: the same name is never the same token, and a token
    #    that only claims a link can never inherit the story's credibility.
    def story(links_mint: str = "") -> NarrativeEntity:
        return NarrativeEntity(
            narrative_id="grok-pocket",
            title="Grok Pocket",
            keywords=("grok pocket",),
            first_seen_at=now - 900,
            last_seen_at=now,
            sources=(
                StorySource(
                    name="campaign",
                    url="https://grokpocket.example",
                    observed_at=now - 900,
                    is_primary=True,
                    links_exact_mint=links_mint,
                ),
            ),
        )

    # A token that merely copied the campaign URL claims the link by itself, and
    # metadata can be copied, so it must never inherit the story's credibility.
    copycat = assess_narrative_link(
        story(),
        TokenIdentityClaim(
            mint=mint,
            name="Grok Pocket",
            website_url="https://grokpocket.example",
            created_at=now - 60,
        ),
        now=now,
    )
    assert copycat.direction == DIR_TOKEN_TO_STORY
    assert copycat.relationship not in INHERITS_STORY, (
        "metadata can be copied, so a token's own claim can never inherit a story"
    )
    assert copycat.relationship == REL_PLAUSIBLE and copycat.confidence <= Decimal("70")
    assert copycat.inherits_story is False

    other = "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"
    name_only = assess_narrative_link(
        story(links_mint=mint),
        TokenIdentityClaim(mint=other, name="Grok Pocket", created_at=now - 60),
        now=now,
    )
    assert name_only.relationship == REL_NAME_ONLY, "same name is not the same token"
    assert name_only.mint == other and name_only.mint != mint

    try:
        mark_official(copycat, authority="operator")
    except ValueError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("token-to-story evidence must never establish OFFICIAL")

    # Only the story side, naming the exact mint, can establish OFFICIAL.
    from_story = assess_narrative_link(
        story(links_mint=mint),
        TokenIdentityClaim(mint=mint, name="Grok Pocket", created_at=now - 60),
        now=now,
    )
    assert from_story.direction == DIR_STORY_TO_TOKEN
    official = mark_official(from_story, authority="verified campaign page")
    assert official.relationship == "OFFICIAL" and official.mint == mint

    try:
        mark_official(from_story, authority="   ")
    except ValueError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("OFFICIAL requires a named authority")

    # 9. A pause is not a reversal: one weak print must not dump a healthy runner.
    position = open_position(
        position_id="p",
        mint=mint,
        now=1_000,
        decision_price_usd=Decimal("0.001"),
        size_usd=Decimal("10"),
        market_cap_usd=Decimal("60000"),
        config=DEFAULT_SHADOW_CONFIG.exit_config(),
    )
    # Momentum prints weak while buyers still lead and liquidity is growing.
    cooling = ExitContext(
        now=1_600,
        price_usd=Decimal("0.00105"),
        market_cap_usd=Decimal("90000"),
        liquidity_usd=Decimal("42000"),
        entry_liquidity_usd=Decimal("40000"),
        momentum_score=Decimal("10"),
        organic_score=Decimal("70"),
        buys=140,
        sells=40,
        volume_usd=Decimal("18000"),
        entry_volume_usd=Decimal("12000"),
        safety_status="PASS",
        route_available=True,
    )
    paused = plan_shadow_exit(position, cooling, RunnerEvidence())
    assert paused.plan.fraction == Decimal("0"), (
        "a single weak momentum print must not sell a healthy runner"
    )
    assert paused.plan.reason_code == SHADOW_SOFT_PAUSE_HOLD and not paused.plan.final

    decaying = plan_shadow_exit(
        position, cooling, RunnerEvidence(consecutive_weak_observations=3)
    )
    assert decaying.plan.fraction > Decimal("0"), "confirmed decay must still de-risk"

    # 10. Nothing here spends a cent, and the experiment is untouched.
    from smart_money_bot.lab.shadow import SHADOW_REAL_MONEY_SPEND

    assert SHADOW_REAL_MONEY_SPEND == 0
    assert DEFAULT_SHADOW_CONFIG.bankroll_usd == Decimal("100")
    assert DEFAULT_SHADOW_CONFIG.position_usd == Decimal("10")



async def check_trending_alpha() -> None:
    """The Trending-first invariants that must hold before a deploy is trusted.

    These are the product's non-negotiables, not a sample of the test suite: a
    deployment that violates any of them is worse than one that never shipped
    the feature, because it would present guesses as facts.
    """

    import tempfile
    from pathlib import Path as _Path

    from smart_money_bot.lab.shadow import (
        DEFAULT_SHADOW_CONFIG,
        SHADOW_STRATEGY_VERSION,
        SIGNAL_FAMILIES,
    )
    from smart_money_bot.stream import (
        CONFIGURATION_STREAM_STATES,
        STREAM_CONNECTED,
        STREAM_DISABLED,
        STREAM_NO_WALLETS,
        STREAM_STATES,
        RealtimeWalletStream,
    )
    from smart_money_bot.trending import (
        CHANGE_WINDOW_UNKNOWN,
        LEGACY_STRATEGY_VERSION,
        SOURCE_FOMO_TRENDING,
        SOURCE_NONE,
        SOURCE_TRENDING_PROXY,
        TRENDING_EXIT_POLICIES,
        TRENDING_FAMILIES,
        TRENDING_STRATEGY_VERSION,
        HotWatchConfig,
        TrendingLedgerEntry,
        TrendingObservation,
        TrendingShadowConfig,
        build_risk_panel,
        classify_trending_event,
        decide_alert,
        normalise_change_window,
        open_hot_watch,
        ramp,
        rank_velocity,
        recheck_hot_watch,
        score_trending_edge,
        source_from_settings,
    )
    from smart_money_bot.trending.exits import TrendingExitContext, evaluate_policy
    from smart_money_bot.trending.hotwatch import ORIGIN_TRENDING_NEAR_MISS
    from smart_money_bot.trending_store import TrendingStore

    now = 1_700_000_000

    # 1. A proxy can never present itself as Fomo Trending (section 4).
    proxy = source_from_settings(api_url=None, api_key=None, proxy_enabled=True)
    assert proxy.kind == SOURCE_TRENDING_PROXY and not proxy.is_exact_fomo
    assert "not Fomo" in proxy.rank_caveat()
    assert source_from_settings(
        api_url=None, api_key=None, proxy_enabled=False
    ).kind == SOURCE_NONE
    authorised = source_from_settings(
        api_url="https://feed.example/t", api_key=None, proxy_enabled=True
    )
    assert authorised.kind == SOURCE_FOMO_TRENDING and authorised.is_exact_fomo

    # 2. An undocumented percentage window is never guessed (section 6).
    assert normalise_change_window("who knows") == CHANGE_WINDOW_UNKNOWN
    assert normalise_change_window(None) == CHANGE_WINDOW_UNKNOWN

    def observation(**kwargs):
        payload = {
            "mint": "MintSelfCheck",
            "observed_at": now,
            "rank": 40,
            "market_cap_usd": Decimal("200000"),
            "liquidity_usd": Decimal("80000"),
            "source": proxy,
        }
        payload.update(kwargs)
        return TrendingObservation(**payload)

    # 3. First observations are immutable (sections 5, 93).
    entry = TrendingLedgerEntry.from_first_observation(observation(rank=44))
    entry = entry.observe(
        observation(observed_at=now + 60, rank=22, market_cap_usd=Decimal("260000"))
    )
    entry = entry.observe(
        observation(observed_at=now + 120, rank=8, market_cap_usd=Decimal("330000"))
    )
    assert entry.first_rank == 44, "the entry rank must never be rewritten"
    assert entry.first_market_cap_usd == Decimal("200000")
    assert entry.first_seen_at == now

    # 4. Mint is identity (section 13).
    try:
        entry.observe(observation(mint="OtherMint"))
    except ValueError:
        pass
    else:  # pragma: no cover - a merged mint is a product failure
        raise AssertionError("a ledger entry must refuse to merge a different mint")

    # 5. Rank velocity, not absolute rank, is the signal (sections 9, 95).
    velocity = rank_velocity(entry.rank_history, now=now + 120, first_seen_at=entry.first_seen_at)
    assert velocity.delta == 36 and velocity.climbing
    flat = TrendingLedgerEntry.from_first_observation(observation(rank=2))
    for step in range(1, 13):
        flat = flat.observe(observation(observed_at=now + step * 300, rank=2))
    flat_velocity = rank_velocity(
        flat.rank_history, now=now + 3600, first_seen_at=flat.first_seen_at
    )
    flat_event = classify_trending_event(flat, flat_velocity, now=now + 3600)
    flat_score = score_trending_edge(flat, flat_event)
    flat_verdict = decide_alert(flat_score, flat_event, alpha_threshold=Decimal("62"))
    assert not flat_verdict.alert, "a high static rank is not alpha"

    # 6. No threshold cliffs (section 43).
    near = ramp(Decimal("1.94"), floor=Decimal("0"), target=Decimal("2"), weight=Decimal("10"))
    exact = ramp(Decimal("2.00"), floor=Decimal("0"), target=Decimal("2"), weight=Decimal("10"))
    assert exact - near < Decimal("0.5"), "1.94 and 2.00 must not be different universes"

    # 7. Hard safety beats every attention signal (sections 71, 100).
    hot_entry = TrendingLedgerEntry.from_first_observation(observation(rank=40))
    hot_entry = hot_entry.observe(
        observation(observed_at=now + 60, rank=3, market_cap_usd=Decimal("400000"))
    )
    hot_velocity = rank_velocity(
        hot_entry.rank_history, now=now + 60, first_seen_at=hot_entry.first_seen_at
    )
    hot_event = classify_trending_event(hot_entry, hot_velocity, now=now + 60)
    blocked = build_risk_panel("MintSelfCheck", sell_failed=True, liquidity_collapsed=True)
    assert blocked.blocked
    blocked_score = score_trending_edge(hot_entry, hot_event, risk=blocked)
    assert blocked_score.score == Decimal("0.0") and not blocked_score.reasons
    blocked_verdict = decide_alert(
        blocked_score, hot_event, alpha_threshold=Decimal("62"), risk=blocked
    )
    assert not blocked_verdict.alert, "trending must never override a hard failure"

    # 8. A verified badge is a badge (section 37).
    badged = build_risk_panel("MintSelfCheck", fomo_verified="VERIFIED", safety_status="UNKNOWN")
    assert not badged.blocked
    assert any("not a safety guarantee" in concern for concern in badged.concerns)

    # 9. HOT WATCH promotes once, on named evidence, and expires quietly.
    config = HotWatchConfig(ttl_seconds=300, recheck_seconds=30)
    watch = open_hot_watch(
        "MintSelfCheck",
        origin=ORIGIN_TRENDING_NEAR_MISS,
        now=now,
        score=Decimal("52"),
        market_cap_usd=Decimal("500000"),
        heads_up_market_cap_usd=Decimal("500000"),
        config=config,
    )
    unnamed = recheck_hot_watch(
        watch, now=now + 40, score=Decimal("99"), reasons=(), alpha_threshold=Decimal("62")
    )
    assert not unnamed.promoted, "a score without a named reason must never ping"
    promoted = recheck_hot_watch(
        watch,
        now=now + 40,
        score=Decimal("70"),
        reasons=("TRENDING_ACCELERATION",),
        market_cap_usd=Decimal("600000"),
        alpha_threshold=Decimal("62"),
    )
    assert promoted.promoted and promoted.should_ping
    assert promoted.entry.promotion_move_percent() == Decimal("20.0")
    faded = recheck_hot_watch(
        watch,
        now=now + 400,
        score=Decimal("20"),
        reasons=("TRENDING_ACCELERATION",),
        alpha_threshold=Decimal("62"),
        config=config,
    )
    assert faded.expired and not faded.promoted

    # 10. A hot watch recheck is genuinely fast (section 46).
    assert HotWatchConfig().recheck_seconds <= 120, (
        "a hot watch that rechecks as slowly as the legacy radar is the bug it fixes"
    )

    # 11. The two experiments are isolated and identically shaped (sections 62-63).
    trending_config = TrendingShadowConfig()
    assert trending_config.strategy_version == TRENDING_STRATEGY_VERSION
    assert TRENDING_STRATEGY_VERSION != LEGACY_STRATEGY_VERSION == SHADOW_STRATEGY_VERSION
    assert trending_config.bankroll_usd == DEFAULT_SHADOW_CONFIG.bankroll_usd == Decimal("100")
    assert trending_config.position_usd == DEFAULT_SHADOW_CONFIG.position_usd == Decimal("10")
    assert trending_config.max_concurrent_positions == 5
    assert trending_config.max_total_exposure_usd == Decimal("50")
    for family in TRENDING_FAMILIES:
        assert family in SIGNAL_FAMILIES, f"{family} must be a registered shadow family"

    # 12. Every exit policy still obeys a hard failure (section 71).
    failure = [
        TrendingExitContext(
            at=now, seconds_held=60, unrealized_percent=Decimal("50"),
            peak_percent=Decimal("50"), sell_failed=True,
        )
    ]
    for policy in TRENDING_EXIT_POLICIES:
        decision = evaluate_policy(policy, failure)
        assert decision.exit and decision.reason == "SELL_FAILED", policy

    # 13. The wallet lane names its state instead of a bare boolean (section 52).
    assert len(set(STREAM_STATES)) == len(STREAM_STATES)
    assert STREAM_DISABLED in CONFIGURATION_STREAM_STATES

    with tempfile.TemporaryDirectory() as directory:
        database = Database(str(_Path(directory) / "trending.db"), Decimal("1000"))
        await database.connect()
        try:
            # 14. The schema is additive and idempotent (section 110).
            await database._init_schema()
            store = TrendingStore(database)
            fresh = TrendingLedgerEntry.from_first_observation(observation(rank=44))
            await store.record_observation(fresh, observation(rank=44))
            # A tampered write must not move the persisted entry numbers.
            await store.record_observation(
                replace(fresh, first_rank=1, first_market_cap_usd=Decimal("1"), current_rank=2)
            )
            reloaded = await store.load_entry("MintSelfCheck")
            assert reloaded is not None and reloaded.first_rank == 44, (
                "the SQL upsert must never rewrite a first observation"
            )
            assert reloaded.first_market_cap_usd == Decimal("200000")

            offline = RealtimeWalletStream(
                database, rpc_url="https://rpc.example/", explicit_ws_url=None, enabled=False
            )
            assert offline.health().state == STREAM_DISABLED
            await offline._run_connection()
            live = RealtimeWalletStream(
                database, rpc_url="https://rpc.example/", explicit_ws_url=None, enabled=True
            )
            await live._run_connection()
            assert live.health().state == STREAM_NO_WALLETS, (
                "no wallets is its own state, not a bare DISCONNECTED"
            )
            live._set_state(STREAM_CONNECTED)
            assert live.health().healthy and not live.health().fallback_active
        finally:
            await database.close()

    # 15. Nothing in the Trending package can move real funds (section 109).
    import pathlib as _pathlib

    import smart_money_bot.trending as _trending

    for path in _pathlib.Path(_trending.__file__).parent.glob("*.py"):
        source = path.read_text()
        for forbidden in ("Keypair", "send_transaction", "sign_transaction", "aiohttp"):
            assert forbidden not in source, f"{path.name} must stay provider- and signer-free"



async def check_trenches_intelligence() -> None:
    """The v2.43 non-negotiables, checked before a deploy is trusted.

    These are the product's structural guarantees, not a sample of the suite: a
    deployment violating any of them would present guesses, purchased attention
    or somebody else's ranking as findings.
    """

    import struct
    import tempfile
    from pathlib import Path as _Path

    from smart_money_bot.pump_chain import bonding_curve_address, decode_bonding_curve
    from smart_money_bot.pump_stream import extract_created_mint
    from smart_money_bot.trenches import (
        BUNDLE_RISK_HIGH,
        CADENCE_HOT,
        CADENCE_NORMAL,
        CONCENTRATION_WORSENING,
        DEV_HISTORY_HIGH_FAILURE,
        FAIL,
        FORBIDDEN_RANKING_CLAIMS,
        MODEL_NAME,
        PASS,
        PUBLIC_TRENDING_MODEL,
        RISK_DIMENSIONS,
        SHAPE_SUSTAINED_TREND,
        STAGE_ALMOST_BONDED,
        STAGE_UNKNOWN,
        TIMEFRAMES,
        UNKNOWN,
        BuyerRecord,
        HolderAccount,
        MarketObservation,
        Nomination,
        PriorToken,
        SlotTrade,
        SourceRef,
        assert_honest_ranking_name,
        assess_bundles,
        assess_concentration_trend,
        assess_depth,
        assess_dev_history,
        assess_participants,
        build_consensus,
        build_holder_snapshot,
        build_risk_profile,
        build_timeframe_profile,
        cadence_tier,
        classify_lifecycle,
        decide_trench_tier,
        score_public_trend,
        window_metrics,
    )
    from smart_money_bot.trenches.provenance import (
        DEXSCREENER_PUBLIC,
        J7_AUTHORIZED,
        PUMP_ONCHAIN,
        SOLANA_RPC,
    )
    from smart_money_bot.trenches_runtime import TrenchesRuntime
    from smart_money_bot.trenches_store import TrenchesStore

    now = 1_700_000_000
    mint = "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump"

    def curve(remaining: int, *, complete: bool = False) -> bytes:
        return (
            b"\x00" * 8
            + struct.pack(
                "<QQQQQ",
                1_073_000_000_000_000,
                30_000_000_000,
                remaining,
                5_000_000_000,
                1_000_000_000_000_000,
            )
            + (b"\x01" if complete else b"\x00")
        )

    # 1. Bonding progress is computed from the chain, and an unreadable curve is
    #    UNKNOWN rather than 0% (sections 7, 8).
    blind = decode_bonding_curve(mint, None)
    assert blind.available is False and blind.progress_percent() is None, (
        "an unreadable bonding curve must be UNKNOWN, never 0%"
    )
    half = decode_bonding_curve(mint, curve(396_550_000_000_000))
    assert half.progress_percent() == Decimal("50.00")
    assert classify_lifecycle(blind, now=now).stage == STAGE_UNKNOWN
    assert (
        classify_lifecycle(
            decode_bonding_curve(mint, curve(39_655_000_000_000)), now=now, created_at=now - 60
        ).stage
        == STAGE_ALMOST_BONDED
    ), "graduation proximity comes from reserves, never from age"
    assert bonding_curve_address(mint) == bonding_curve_address(mint)

    # 2. The realtime creation detector only fires on an actual create.
    pumpy = "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump"
    assert extract_created_mint(
        ["Program log: Instruction: Create", f"Program log: mint: {pumpy}"]
    ) == pumpy
    assert extract_created_mint(
        ["Program log: Instruction: Buy", f"Program log: {pumpy}"]
    ) == "", "an ordinary trade must never be recorded as a launch"

    # 3. Five independent windows, no leakage (section 85).
    observations = [
        MarketObservation(at=now - 3000, market_cap_usd=Decimal("10000")),
        MarketObservation(at=now - 700, market_cap_usd=Decimal("15000")),
        MarketObservation(at=now - 200, market_cap_usd=Decimal("20000")),
        MarketObservation(at=now - 30, market_cap_usd=Decimal("30000")),
    ]
    profile = build_timeframe_profile(mint, observations, now=now)
    assert set(profile.windows) == set(TIMEFRAMES)
    one_minute = window_metrics(observations, timeframe="1m", now=now)
    assert not one_minute.usable, "a window with one sample must report nothing"
    assert one_minute.market_cap_change_percent is None
    hour = window_metrics(observations, timeframe="1h", now=now)
    assert hour.market_cap_change_percent == Decimal("200.00")

    # 4. Raw transactions are not demand; coordinated wallets collapse (s13, 15).
    bots = [
        BuyerRecord(wallet=f"B{index % 4}", at=now, amount_usd=Decimal("50"))
        for index in range(1000)
    ]
    wash = assess_participants(mint, bots, buys=1000, sells=20)
    assert wash.unique_buyers == 4 and not wash.organic, (
        "1000 buys from 4 wallets must never read as organic demand"
    )
    sybils = [
        BuyerRecord(
            wallet=f"S{index}",
            at=now,
            amount_usd=Decimal("25"),
            first_activity_at=now - 600,
            funded_by="ONE_SOURCE",
            funded_at=now - 700,
        )
        for index in range(20)
    ]
    clustered = assess_participants(mint, sybils, buys=20, sells=0)
    assert clustered.independent_buyers == 1, (
        "twenty wallets funded by one source are one actor"
    )
    assert clustered.independent_fresh_buyers == 0

    # 5. Holder concentration excludes infrastructure and is a trend (s20, 21).
    def snapshot(at: int, top10: str):
        whales = Decimal(top10) / 10
        tail = (Decimal("100") - Decimal(top10)) / 40
        return build_holder_snapshot(
            mint,
            [HolderAccount(address=f"W{i}", amount=whales) for i in range(10)]
            + [HolderAccount(address=f"T{i}", amount=tail) for i in range(40)],
            total_supply=Decimal("100"),
            at=at,
        )

    worsening = assess_concentration_trend(mint, [snapshot(now - 600, "18"), snapshot(now, "35")])
    assert worsening.state == CONCENTRATION_WORSENING

    with_curve = build_holder_snapshot(
        mint,
        [HolderAccount(address="CURVE", amount=Decimal("700"), infrastructure=True)]
        + [HolderAccount(address=f"H{i}", amount=Decimal("10")) for i in range(30)],
        total_supply=Decimal("1000"),
        at=now,
    )
    assert with_curve.infrastructure_percent == Decimal("70.00"), (
        "the bonding curve is infrastructure, never a holder"
    )

    # 6. Launch bundling is lifecycle-aware, and distribution escalates (s23, 92).
    launch = [
        SlotTrade(wallet=f"B{i}", slot=100, at=now + 5, token_amount=Decimal("60000000000000"))
        for i in range(5)
    ]
    assert (
        assess_bundles(
            mint, launch, created_at=now, total_supply=Decimal("1000000000000000")
        ).risk
        == BUNDLE_RISK_HIGH
    )
    mature = [
        SlotTrade(wallet=f"C{i}", slot=900, at=now + 7200, token_amount=Decimal("1000"))
        for i in range(5)
    ]
    assert (
        assess_bundles(
            mint,
            mature,
            created_at=now,
            total_supply=Decimal("1000000000000000"),
            pre_graduation=False,
        ).risk
        == "NONE"
    ), "ordinary same-slot co-trading is not launch bundling"

    # 7. A poor creator record stays a neutral label (section 19).
    history = assess_dev_history(
        "D", [PriorToken(mint=f"M{i}", collapsed=True) for i in range(4)]
    )
    assert history.label == DEV_HISTORY_HIGH_FAILURE
    assert "scam" not in history.operator_line().casefold()

    # 8. Risk is per-dimension, UNKNOWN never becomes PASS, hard fails win (59-61).
    blind_risk = build_risk_profile(mint)
    assert len(blind_risk.dimensions) == len(RISK_DIMENSIONS)
    assert PASS not in {item.verdict for item in blind_risk.dimensions}, (
        "an unknown dimension must never be reported as a pass"
    )
    assert blind_risk.blocked is False, "not knowing is not a hard failure"
    assert all(
        build_risk_profile(mint, liquidity_usd=Decimal("999999"), **kwargs).blocked
        for kwargs in (
            {"sell_failed": True},
            {"liquidity_collapsed": True},
            {"malicious_evidence": True},
            {"route_available": False},
        )
    ), "a hard failure outranks every positive signal"
    assert build_risk_profile(mint, dev_selling=True).dimension("DEV").verdict == FAIL
    assert UNKNOWN in {item.verdict for item in blind_risk.dimensions}

    # 9. Paid DEX placement cannot carry our public model (sections 26, 31, 95).
    flat = build_timeframe_profile(
        mint,
        [
            MarketObservation(at=now - 900, market_cap_usd=Decimal("10000")),
            MarketObservation(at=now - 10, market_cap_usd=Decimal("10000")),
        ],
        now=now,
    )
    boosted = score_public_trend(mint, timeframes=flat, dex_paid=True, dex_boosts=99)
    assert boosted.score <= Decimal("5"), (
        "purchased attention must never lift a token that is not moving"
    )
    moving = build_timeframe_profile(
        mint,
        [
            MarketObservation(at=now - 880, market_cap_usd=Decimal("10000")),
            MarketObservation(at=now - 300, market_cap_usd=Decimal("22000")),
            MarketObservation(at=now - 20, market_cap_usd=Decimal("45000")),
        ],
        now=now,
    )
    assert moving.shape == SHAPE_SUSTAINED_TREND
    organic = score_public_trend(mint, timeframes=moving, independent_buyers=90)
    assert organic.score > boosted.score * 5

    # 10. Our ranking is ours, and can never claim to be anyone else's (s97, 98).
    assert MODEL_NAME == PUBLIC_TRENDING_MODEL
    for claim in FORBIDDEN_RANKING_CLAIMS:
        try:
            assert_honest_ranking_name(claim)
        except ValueError:
            continue
        raise AssertionError(f"{claim} must be refused as a ranking name")
    caveat = organic.to_json()["caveat"]
    assert "not Terminal" in caveat and "not Fomo" in caveat

    # 11. Duplicate feeds are one evidence family (section 34).
    duplicates = build_consensus(
        [
            Nomination(mint=mint, lane="A", source=SourceRef(kind=SOLANA_RPC)),
            Nomination(mint=mint, lane="B", source=SourceRef(kind=DEXSCREENER_PUBLIC)),
            Nomination(mint=mint, lane="C", source=SourceRef(kind=PUMP_ONCHAIN)),
        ]
    )[mint]
    assert duplicates.lane_count == 3 and duplicates.independent_count == 1, (
        "three market feeds of the same chain are one observation"
    )
    genuine = build_consensus(
        [
            Nomination(mint=mint, lane="A", source=SourceRef(kind=PUMP_ONCHAIN)),
            Nomination(mint=mint, lane="B", source=SourceRef(kind=J7_AUTHORIZED)),
        ]
    )[mint]
    assert genuine.independent_count == 2

    # 12. A score is never a reason, and the risk gates are hard (sections 36, 37).
    assert (
        decide_trench_tier(mint, score=Decimal("95"), reasons=()).suppression
        == "NO_NAMED_SERIOUS_REASON"
    )
    assert not decide_trench_tier(
        mint,
        score=Decimal("95"),
        reasons=("INDEPENDENT_DEMAND",),
        clustered_demand=True,
    ).ping, "coordinated demand must never ping"
    assert not decide_trench_tier(
        mint, score=Decimal("95"), reasons=("MARKET_ACCELERATION",), dev_selling=True
    ).ping

    # 13. Cadence tiers are bounded and time-critical candidates get the fast one.
    assert cadence_tier(score=Decimal("58"), alpha_threshold=Decimal("62")) == CADENCE_HOT
    assert (
        cadence_tier(score=Decimal("10"), alpha_threshold=Decimal("62"), almost_bonded=True)
        == CADENCE_HOT
    )
    assert cadence_tier(score=Decimal("5"), alpha_threshold=Decimal("62")) == CADENCE_NORMAL

    # 14. Depth: the same market cap on different liquidity is not the same token.
    assert assess_depth(market_cap_usd=Decimal("50000"), liquidity_usd=Decimal("1000")).thin
    assert not assess_depth(
        market_cap_usd=Decimal("50000"), liquidity_usd=Decimal("15000")
    ).thin

    with tempfile.TemporaryDirectory() as directory:
        database = Database(str(_Path(directory) / "trenches.db"), Decimal("1000"))
        await database.connect()
        try:
            # 15. Schema additive and idempotent (section 101).
            await database._init_schema()
            store = TrenchesStore(database)
            runtime = TrenchesRuntime(store, _SelfCheckChain())

            # 16. First observation is stamped immediately and never rewritten.
            await runtime.observe_creation(
                mint, at=now, created_at=now - 3, source="PUMP_CREATION_STREAM"
            )
            row = await store.token(mint)
            assert row["first_observed_at"] == now
            latency = await store.discovery_latencies()
            assert latency[0]["latency_seconds"] == 3, (
                "launch-to-observation latency must be measurable"
            )
            await store.record_token(
                mint, now=now + 900, stage="PUMPSWAP", market_cap_usd=Decimal("90000")
            )
            assert (await store.token(mint))["first_observed_at"] == now, (
                "the first-observation stamp must survive every later write"
            )

            # 17. Graduation is recorded once and preserves earlier history.
            await store.mark_graduated(mint, at=now + 600, market_cap_usd=Decimal("69000"))
            await store.mark_graduated(mint, at=now + 9000, market_cap_usd=Decimal("1"))
            graduated = await store.token(mint)
            assert graduated["graduated_at"] == now + 600
            assert graduated["graduation_market_cap_usd"] == 69000.0
        finally:
            await database.close()

    # 18. Nothing in the trenches package can move real funds (section 100).
    import pathlib as _pathlib

    import smart_money_bot.trenches as _trenches

    for path in _pathlib.Path(_trenches.__file__).parent.glob("*.py"):
        source = path.read_text()
        for forbidden in ("Keypair", "send_transaction", "sign_transaction", "aiohttp"):
            assert forbidden not in source, f"{path.name} must stay provider- and signer-free"


class _SelfCheckChain:
    """A no-network chain reader for the deploy check."""

    def usage_snapshot(self) -> dict[str, int]:
        return {"curve_reads": 0}

    async def bonding_curves(self, mints: list[str]) -> dict[str, object]:
        return {}


if __name__ == "__main__":
    asyncio.run(main())
