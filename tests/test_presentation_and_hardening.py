"""Regression suite for the v2.46 production hardening.

Four live failures, each with its own section here.

**Cards rendered ``?`` and ``$?``.**  The bot knew the exact mint the whole time
and had nowhere to keep the name.  The one lookup it had read the legacy
graduated-runner row, which does not exist for a token discovered by GMGN or by
the Pump creation stream, so the fallbacks collapsed to a question mark — for
tokens whose symbol the discovery response had already told us.

**Thumbnails were blank.**  Six card builders were called with no image at all,
and nothing persisted one, so a stage that knew the icon could not pass it to
the stage that did not.

**A hot-search 429 muted the whole provider.**  Health was one global record, so
the least important GMGN feed took trending, trenches and signals down with it.

**Smart money and KOL read zero forever.**  Those endpoints return *trades*, not
a wallet directory, and the parser was looking for a field that is never sent.

The rule underneath all of it is the same one v2.43.1 shipped: **mint is
identity**.  A presentation cache keyed by symbol would be a same-name
substitution waiting to happen, so the tests that matter most here are the ones
proving two same-symbol tokens never borrow each other's name or icon.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

import smart_money_bot.fast_alerts as fa
from smart_money_bot.gmgn import GmgnClient, GmgnError
from smart_money_bot.gmgn.endpoints import (
    CORE_ACTIVE,
    CORE_RATE_LIMITED,
    ENDPOINT_TIER,
    PARTIAL_DEGRADATION,
    TIER_A,
    TIER_C,
    EndpointRegistry,
    tier_for,
)
from smart_money_bot.lab.providers import ProviderState, is_exhaustion, record_failure
from smart_money_bot.pump_chain import (
    RPC_FORBIDDEN,
    RPC_RATE_LIMITED,
    RPC_TIMEOUT,
    RPC_UNSUPPORTED,
    classify_rpc_error,
)
from smart_money_bot.pump_stream import STREAM_NO_ACK, PumpCreationStream
from smart_money_bot.token_presentation import (
    PENDING_NAME,
    PENDING_SYMBOL,
    SOURCE_DEX_SNAPSHOT,
    SOURCE_GMGN_BOARD,
    SOURCE_GMGN_TOKEN_INFO,
    SOURCE_RUNNER_ROW,
    UNAVAILABLE_NAME,
    TokenPresentation,
    build_presentation,
    diff,
    mark_unresolved,
    merge,
    needs_enrichment,
    presentation_from_json,
    safe_image_url,
)

D = Decimal

MINT = "GPR7Ax4kQ2mVn8hLdT6yWc3JbRfE9uZsXqM1oP5tH4dK"
CLONE = "7TqH1d4Vf9QG578vB99Q7ewFQPoxSYqBDxSAzBpBpump"
NOW = 1_700_000_000
SECRET = "gmgn-key-must-never-leak-0123456789"


# ===========================================================================
# 3, 7, 39. Never "?"
# ===========================================================================


def test_an_unknown_token_says_pending_rather_than_a_question_mark() -> None:
    """The exact production symptom.  A card must never read ``?`` / ``$?``."""

    blank = TokenPresentation(mint=MINT)

    assert blank.display_name == PENDING_NAME
    assert blank.display_symbol == PENDING_SYMBOL
    assert "?" not in blank.display_name
    assert blank.display_symbol != "?"


def test_a_failed_resolution_reads_unavailable_not_pending() -> None:
    """The two states differ to an operator: one will improve on its own."""

    failed = mark_unresolved(None, MINT, at=NOW)

    assert failed.display_name == UNAVAILABLE_NAME
    assert failed.resolution_failed is True


def test_a_provider_that_literally_sends_a_question_mark_is_not_stored() -> None:
    record = build_presentation(MINT, name="?", symbol="unknown", source=SOURCE_GMGN_BOARD)

    assert record.name == ""
    assert record.symbol == ""
    assert record.display_name == PENDING_NAME


def test_the_engine_reads_the_presentation_record_rather_than_the_runner_row() -> None:
    """The direct cause: the only lookup was a table GMGN tokens are never in."""

    from smart_money_bot.engine import SmartMoneyEngine

    source = inspect.getsource(SmartMoneyEngine._cached_token_names)
    assert "presentation_for" in source
    assert '"?"' not in source


# ===========================================================================
# 5, 6, 11, 12, 40. One mint, one identity, whole lifecycle
# ===========================================================================


def test_a_board_row_that_carries_a_symbol_is_never_thrown_away() -> None:
    """Section 6: the response that found the token already named it."""

    record = merge(
        None, build_presentation(MINT, symbol="MDR", source=SOURCE_GMGN_BOARD, at=NOW)
    )

    assert record.display_symbol == "MDR"
    assert record.resolved is True


def test_a_better_source_corrects_an_abbreviated_name() -> None:
    board = build_presentation(MINT, name="MDR", symbol="MDR", source=SOURCE_GMGN_BOARD)
    info = build_presentation(
        MINT, name="Moo Deng Returns", symbol="MDR", source=SOURCE_GMGN_TOKEN_INFO
    )

    assert merge(board, info).name == "Moo Deng Returns"


def test_a_weaker_source_cannot_overwrite_a_better_name() -> None:
    info = build_presentation(
        MINT, name="Moo Deng Returns", source=SOURCE_GMGN_TOKEN_INFO
    )
    runner = build_presentation(MINT, name="MDR", source=SOURCE_RUNNER_ROW)

    assert merge(info, runner).name == "Moo Deng Returns"


def test_a_later_partial_observation_never_blanks_a_known_field() -> None:
    """Never move backwards: the whole reason a promotion forgot the image."""

    known = merge(
        None,
        build_presentation(
            MINT,
            name="Moo Deng Returns",
            symbol="MDR",
            image_url="https://cdn.example/mdr.png",
            source=SOURCE_GMGN_TOKEN_INFO,
        ),
    )
    partial = merge(known, build_presentation(MINT, source=SOURCE_DEX_SNAPSHOT))

    assert partial.name == "Moo Deng Returns"
    assert partial.symbol == "MDR"
    assert partial.thumbnail == "https://cdn.example/mdr.png"


def test_a_presentation_round_trips_through_persistence() -> None:
    """Section 41: a shadow exit tomorrow must still know today's name."""

    record = merge(
        None,
        build_presentation(
            MINT,
            name="Moo Deng Returns",
            symbol="MDR",
            image_url="https://cdn.example/mdr.png",
            source=SOURCE_GMGN_TOKEN_INFO,
            at=NOW,
        ),
    )

    assert presentation_from_json(record.to_json()) == record


# ===========================================================================
# 42. The wrong-PFP bug
# ===========================================================================


def test_two_same_symbol_tokens_never_borrow_each_others_identity() -> None:
    """Section 42, and the reason this cache is keyed by mint and nothing else."""

    real = merge(
        None,
        build_presentation(
            MINT,
            name="Moo Deng Returns",
            symbol="MDR",
            image_url="https://cdn.example/real.png",
            source=SOURCE_GMGN_TOKEN_INFO,
        ),
    )
    clone = merge(
        None,
        build_presentation(
            CLONE,
            name="Moo Deng Returns",
            symbol="MDR",
            image_url="https://cdn.example/clone.png",
            source=SOURCE_GMGN_TOKEN_INFO,
        ),
    )

    assert real.thumbnail != clone.thumbnail
    assert real.mint != clone.mint
    # And the merge itself refuses, rather than relying on callers being careful.
    with pytest.raises(ValueError, match="one mint"):
        merge(real, clone)


def test_the_cache_has_no_symbol_keyed_lookup_to_misuse() -> None:
    from smart_money_bot import token_presentation

    source = inspect.getsource(token_presentation)
    # `collides` reports same-symbol mints; it must never select between them.
    assert "def collides" in source
    assert "return None" not in inspect.getsource(token_presentation.collides)
    assert "mint" in inspect.getsource(token_presentation.merge)


# ===========================================================================
# 9, 10. Images: never invented, never unsafe
# ===========================================================================


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.example/a.png",
        "/tmp/local.png",
        "https://cdn.example/a.png?api_key=SECRET",
        "https://s3.example/a.png?X-Amz-Signature=abc",
        "https://cdn.example/a.png?session_id=xyz",
        "",
        None,
    ],
)
def test_an_unsafe_image_url_is_refused(url: object) -> None:
    """Section 10: a thumbnail is a URL every viewer's client will fetch."""

    assert safe_image_url(url) == ""


def test_a_legitimate_ipfs_reference_is_rewritten_not_dropped() -> None:
    assert safe_image_url("ipfs://QmAbc/logo.png") == "https://ipfs.io/ipfs/QmAbc/logo.png"
    assert safe_image_url("https://cdn.example/a.png") == "https://cdn.example/a.png"


def test_no_thumbnail_is_better_than_the_wrong_one() -> None:
    """Section 9: nothing here substitutes a same-name token's image."""

    record = merge(
        None, build_presentation(MINT, symbol="MDR", image_url="ftp://x/a.png")
    )

    assert record.thumbnail == ""
    assert record.display_symbol == "MDR"


# ===========================================================================
# 4, 13, 45. Publish fast, enrich in place
# ===========================================================================


def test_a_card_publishes_before_metadata_and_is_edited_after() -> None:
    """Speed at T0, completeness at T+1 — never the other way round."""

    published = fa.build_early_alert(
        mint=MINT,
        name=PENDING_NAME,
        symbol=PENDING_SYMBOL,
        fomo_url=f"https://fomo.family/coin?address={MINT}",
        verdict=SimpleNamespace(
            tier="EARLY_HEADS_UP", score=D("76"), label="👀 EARLY HEADS-UP"
        ),
        age_seconds=180,
        first_seen_seconds_ago=5,
        first_seen_market_cap_usd=D("71930"),
        alert_market_cap_usd=D("71930"),
        current_market_cap_usd=D("71930"),
        liquidity_usd=D("21090"),
        buys=78,
        sells=48,
        discovered_via="GMGN Trending 1m",
    )

    assert "?" not in published.spec.description.split("Mint:")[0]
    assert PENDING_NAME in published.spec.description
    assert MINT in published.spec.description

    resolved = merge(
        None,
        build_presentation(
            MINT,
            name="Moo Deng Returns",
            symbol="MDR",
            image_url="https://cdn.example/mdr.png",
            source=SOURCE_GMGN_TOKEN_INFO,
        ),
    )
    update = fa.enrichment_from_presentation(
        alert_key=published.alert_key,
        mint=MINT,
        presentation=resolved,
        fomo_url=f"https://fomo.family/coin?address={MINT}",
    )
    edited = update.apply(published.spec)

    assert "Moo Deng Returns" in edited.description
    assert "$MDR" in edited.description
    assert edited.thumbnail_url == "https://cdn.example/mdr.png"
    # The fields the card already had survive the edit.
    assert {item.name for item in published.spec.fields} <= {
        item.name for item in edited.fields
    }


def test_an_enrichment_that_learned_nothing_blanks_nothing() -> None:
    spec = fa.CardSpec(
        title="t",
        description="**Real** `$REAL`",
        compact_description="compact",
        thumbnail_url="https://cdn.example/a.png",
    )
    unchanged = fa.EnrichmentUpdate(alert_key="k").apply(spec)

    assert unchanged.description == "**Real** `$REAL`"
    assert unchanged.thumbnail_url == "https://cdn.example/a.png"
    assert unchanged.compact_description == "compact"


def test_an_edit_is_only_worth_making_when_something_visible_improved() -> None:
    before = TokenPresentation(mint=MINT)
    after = merge(None, build_presentation(MINT, name="Real", symbol="REAL"))

    assert diff(before, after).worth_editing is True
    assert diff(after, after).worth_editing is False
    assert needs_enrichment(after) is True  # no image yet
    assert (
        needs_enrichment(
            merge(after, build_presentation(MINT, image_url="https://cdn.example/a.png"))
        )
        is False
    )


def test_metadata_enrichment_is_scheduled_after_publication_not_before() -> None:
    """Section 45: the fix is "edit later", never "wait before alerting"."""

    from smart_money_bot.engine import SmartMoneyEngine

    source = inspect.getsource(SmartMoneyEngine._publish_fast_alert)
    schedule = source.index("_schedule_presentation_enrichment")
    # v2.51: the send moved behind the universal dispatcher, which is now the
    # only path to Discord. The invariant is unchanged — enrichment is
    # scheduled, never awaited, before the card goes out.
    notify = source.index("await self._dispatch_card(alert)")
    assert schedule < notify, "scheduling must not await the metadata call"
    assert "await self._resolve_presentation" not in source


def test_resolution_never_falls_back_to_a_ticker_search() -> None:
    """Section 7.  Failure is safer than substitution, at any price."""

    from smart_money_bot.engine import SmartMoneyEngine

    source = inspect.getsource(SmartMoneyEngine.resolve_presentation)
    for forbidden in ("search(", "symbol_search", "by_symbol", "narrative_match"):
        assert forbidden not in source
    assert "mark_unresolved" in source


# ===========================================================================
# 14, 15. Provenance
# ===========================================================================


def test_the_card_says_why_the_bot_saw_the_token() -> None:
    assert fa.discovery_line(("GMGN_TRENDING",), interval="1m") == "GMGN Trending 1m"
    assert (
        fa.discovery_line(("GMGN_TRENCH_NEW",)) == "GMGN Trenches — new creation"
    )
    assert fa.discovery_line(("pump_realtime",)) == "Pump on-chain realtime"
    assert fa.discovery_line(()) == ""


def test_provenance_stays_phone_readable() -> None:
    """Section 15: a compact line, not a twenty-line provider dump."""

    line = fa.discovery_line(
        ("GMGN_TRENDING", "pump_realtime", "GMGN_TRENCH_NEW", "GMGN_HOT_SEARCH"),
        interval="1m",
    )

    assert line.count("•") <= 2
    assert len(line) < 100


# ===========================================================================
# 16-21. One optional endpoint cannot mute the provider
# ===========================================================================


class _Response:
    def __init__(self, payload, *, status: int = 200, headers: dict | None = None) -> None:
        self._payload, self.status, self.headers = payload, status, headers or {}

    async def text(self) -> str:
        return json.dumps(self._payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    closed = False

    def __init__(self, handler) -> None:
        self._handler, self.requests = handler, []

    def request(self, method, url, **kwargs):
        self.requests.append(url)
        return self._handler(method, url, kwargs)


def _rate_limited_hot_search(method, url, kwargs):
    if "hot_searches" in url:
        return _Response(
            {"code": 429, "error": "RATE_LIMIT_EXCEEDED"},
            status=429,
            headers={"x-ratelimit-reset": str(NOW + 120)},
        )
    return _Response({"code": 0, "data": {"rank": [{"address": MINT}]}})


def test_a_hot_search_429_does_not_stop_trending() -> None:
    """The exact production failure: the least important feed muted the rest."""

    client = GmgnClient(api_key=SECRET, session=_Session(_rate_limited_hot_search))

    with pytest.raises(GmgnError):
        asyncio.run(client.hot_searches())
    rows = asyncio.run(client.trending(interval="1m"))

    assert len(rows) == 1, "core discovery must keep working"
    health = client.endpoints.to_json()
    assert health["summary"] == PARTIAL_DEGRADATION
    by_kind = {item["kind"]: item for item in health["endpoints"]}
    assert by_kind["hot_searches"]["cooling"] is True
    assert by_kind["rank"]["healthy"] is True


def test_a_cooling_endpoint_is_not_probed_again() -> None:
    """Section 19: continuing to knock is how a limit becomes a ban."""

    client = GmgnClient(api_key=SECRET, session=_Session(_rate_limited_hot_search))
    with pytest.raises(GmgnError):
        asyncio.run(client.hot_searches())
    before = len(client._session.requests)

    with pytest.raises(GmgnError):
        asyncio.run(client.hot_searches())

    assert len(client._session.requests) == before, "no network call during cooldown"


def test_a_core_endpoint_rate_limit_is_reported_as_core() -> None:
    registry = EndpointRegistry()
    registry.note_success("rank", rows=3)
    assert registry.summary() == CORE_ACTIVE

    registry.note_failure("rank", state="RATE_LIMITED", cooldown_seconds=60)
    assert registry.summary() == CORE_RATE_LIMITED


def test_the_discovery_feeds_are_tier_a_and_hot_search_is_not() -> None:
    """Section 18: quota goes to discovery before it goes to attention."""

    assert tier_for("rank") == TIER_A
    assert tier_for("trenches") == TIER_A
    assert tier_for("token_signal") == TIER_A
    assert tier_for("hot_searches") == TIER_C
    # An unranked kind is treated as tier B: not protected, not silently shed.
    assert tier_for("something_new") == "B"
    assert set(ENDPOINT_TIER.values()) <= {"A", "B", "C"}


def test_tier_c_is_shed_before_tier_b_and_tier_a_never_is() -> None:
    registry = EndpointRegistry()

    allowed_c, reason_c = registry.admits("hot_searches", budget_headroom=0.2)
    allowed_b, _ = registry.admits("top_holders", budget_headroom=0.2)
    allowed_a, _ = registry.admits("rank", budget_headroom=0.01)

    assert allowed_c is False and reason_c == "SHED_TIER_C"
    assert allowed_b is True, "tier B survives until pressure is worse"
    assert allowed_a is True, "a last call goes to discovery"
    assert registry.admits("top_holders", budget_headroom=0.05)[0] is False


def test_hot_search_can_be_switched_off_without_touching_the_core() -> None:
    """Section 20."""

    registry = EndpointRegistry()
    registry.note_success("rank", rows=1)
    registry.disable("hot_searches")

    assert registry.admits("hot_searches")[0] is False
    assert registry.admits("rank")[0] is True
    # A deliberate off switch is not a degradation.
    assert registry.summary() == CORE_ACTIVE


def test_a_cooldown_survives_a_restart() -> None:
    """Section 21: do not restart and hammer a banned endpoint again."""

    registry = EndpointRegistry()
    registry.restore("hot_searches", state="RATE_LIMITED", cooldown_until=NOW + 300)

    allowed, reason = registry.admits("hot_searches", now=NOW)
    assert allowed is False and reason == "RATE_LIMITED"
    assert registry.admits("hot_searches", now=NOW + 400)[0] is True


# ===========================================================================
# 22-25. The live response contract
# ===========================================================================


def test_the_trenches_response_key_is_pump_not_near_completion() -> None:
    """Documented explicitly, and reading it wrong loses FINAL STRETCH entirely."""

    from smart_money_bot.gmgn import parse_trenches_response

    sections = parse_trenches_response(
        {
            "new_creation": [{"address": MINT}],
            "pump": [{"address": CLONE}],
            "completed": [],
        }
    )

    assert "near_completion" in sections
    assert sections["near_completion"][0].mint == CLONE
    assert "pump" not in sections


def test_the_board_row_logo_becomes_the_card_thumbnail() -> None:
    from smart_money_bot.gmgn import parse_rank_response

    token = parse_rank_response(
        {"rank": [{"address": MINT, "symbol": "MDR", "logo": "https://cdn.example/m.png"}]},
        interval="1m",
    )[0]

    assert token.image_url == "https://cdn.example/m.png"
    assert token.symbol == "MDR"


def test_a_holder_percentage_is_a_fraction_not_a_percent() -> None:
    """Reading it as a percent turns an 8.5% holder into 0.085%."""

    from smart_money_bot.gmgn import parse_participants

    holder = parse_participants(
        {"holders": [{"address": "W1", "amount_percentage": 0.085}]}, mint=MINT
    )[0]

    assert holder.holding_percent == D("8.50")


def test_zero_and_missing_stay_distinguishable() -> None:
    """Section 23: do not make zero mean "field missing"."""

    from smart_money_bot.gmgn import parse_participants

    absent, zero = parse_participants(
        {
            "holders": [
                {"address": "W1"},
                {"address": "W2", "realized_profit": 0, "buy_tx_count_cur": 0},
            ]
        },
        mint=MINT,
    )

    assert absent.realized_pnl_usd is None and absent.buys is None
    assert zero.realized_pnl_usd == D("0") and zero.buys == 0


def test_token_level_tags_survive_an_empty_directory() -> None:
    """Section 25: a wallet tagged on the token stays tagged."""

    from smart_money_bot.gmgn import parse_participants, parse_wallet_trades

    assert parse_wallet_trades({"list": []}, tag="GMGN_SMART_MONEY") == ()
    tagged = parse_participants(
        {"holders": [{"address": "W1", "tags": ["smart_degen"]}]}, mint=MINT
    )[0]
    assert tagged.is_smart_money is True


def test_a_kol_tag_is_never_read_as_smart_money() -> None:
    from smart_money_bot.gmgn import parse_participants

    kol = parse_participants(
        {"holders": [{"address": "W1", "tags": ["kol", "renowned"]}]}, mint=MINT
    )[0]

    assert kol.is_kol is True
    assert kol.is_smart_money is False


# ===========================================================================
# 26-28. Pump realtime
# ===========================================================================


def test_the_stream_names_an_unacknowledged_subscription() -> None:
    """Production showed RECONNECTING with no reason.  Now there is a reason."""

    stream = PumpCreationStream(rpc_url="https://api.mainnet-beta.solana.com")
    status = stream.status()

    assert "subscribe_acks" in status
    assert "notifications" in status
    assert "stale_rebuilds" in status
    assert "ack_timeouts" in status
    assert STREAM_NO_ACK == "NO_SUBSCRIPTION_ACK"


def test_a_silent_socket_is_treated_as_a_failure_not_a_clean_pass() -> None:
    """The reconnect spin: a stale return used to reset the backoff to one second."""

    source = inspect.getsource(PumpCreationStream.run)
    assert "healthy_run" in source
    assert "self.failed_attempts += 1" in source
    connection = inspect.getsource(PumpCreationStream._run_connection)
    assert "-> bool" in inspect.getsource(PumpCreationStream._run_connection).split("\n")[0] or (
        "return delivered" in connection
    )


def test_polling_is_never_described_as_a_realtime_websocket() -> None:
    """Section 28."""

    stream = PumpCreationStream(rpc_url="https://api.mainnet-beta.solana.com")
    status = stream.status()

    assert status["fallback_active"] is True
    assert status["fallback_source"] == "GMGN_TRENCH_POLLING"
    assert "websocket" not in str(status["fallback_source"]).lower()


def test_a_disabled_stream_is_not_reported_as_broken() -> None:
    stream = PumpCreationStream(rpc_url="https://api.mainnet-beta.solana.com", enabled=False)

    assert stream.status()["state"] == "DISABLED_BY_CONFIG"


# ===========================================================================
# 29, 30. On-chain and Tracker diagnostics
# ===========================================================================


def test_rpc_failures_are_classified_rather_than_counted() -> None:
    """Section 29: "errors 27" tells an operator nothing actionable."""

    assert classify_rpc_error("HTTP 429 too many requests") == RPC_RATE_LIMITED
    assert classify_rpc_error("403 Forbidden") == RPC_FORBIDDEN
    assert classify_rpc_error("Method not found (-32601)") == RPC_UNSUPPORTED
    assert classify_rpc_error("request timed out") == RPC_TIMEOUT


def test_the_chain_reader_reports_success_and_failure_per_operation() -> None:
    from smart_money_bot.pump_chain import PumpChainReader

    reader = PumpChainReader(SimpleNamespace())
    reader._note_call("holder_snapshot")
    reader._note_call("holder_snapshot")
    reader._note_error("holder_snapshot", "HTTP 429 rate limit")
    snapshot = reader.usage_snapshot()

    assert snapshot["calls_by_operation"]["holder_snapshot"] == 2
    assert snapshot["errors_by_operation"]["holder_snapshot"] == 1
    assert snapshot["success_by_operation"]["holder_snapshot"] == 1
    assert snapshot["errors_by_cause"][RPC_RATE_LIMITED] == 1


def test_insufficient_credits_opens_the_long_breaker_immediately() -> None:
    """Section 30: a spent quota does not refill in sixty seconds."""

    state = ProviderState(name="solana_tracker")
    exhausted = record_failure(
        state,
        now=0,
        status=403,
        message='Solana Tracker HTTP 403: {"error":"Insufficient credits for this request"}',
    )
    throttled = record_failure(state, now=0, status=429, message="rate limited")

    assert is_exhaustion("Insufficient credits for this request") is True
    assert exhausted.degraded_until == 3_600
    assert throttled.degraded_until == 60


def test_a_missing_record_is_not_a_credit_failure() -> None:
    state = record_failure(
        ProviderState(name="solana_tracker"), now=0, status=404, message="no such token"
    )

    assert state.degraded_until == 0


# ===========================================================================
# 31-33, 53. The realtime panel tells the truth
# ===========================================================================


def test_the_realtime_panel_carries_a_gmgn_block() -> None:
    import smart_money_bot.bot as bot_module

    source = inspect.getsource(bot_module._realtime_embed)
    assert "GMGN ALPHA" in source
    assert "endpoint_health" in source
    assert "PUMP REALTIME" in source
    assert "GMGN_TRENCH_FALLBACK" in source


def test_the_realtime_panel_never_renders_a_credential() -> None:
    import smart_money_bot.bot as bot_module

    source = inspect.getsource(bot_module._realtime_embed)
    for forbidden in ("api_key", "GMGN_API_KEY", "X-APIKEY"):
        assert forbidden not in source


def test_gmgn_trending_is_visibly_separate_from_the_fomo_proxy() -> None:
    """Section 33: an operator must never read TRENDING_PROXY as GMGN."""

    import smart_money_bot.bot as bot_module

    source = inspect.getsource(bot_module._realtime_embed)
    assert "GMGN ALPHA — real provider data" in source


def test_the_provider_panel_breaks_gmgn_down_by_endpoint() -> None:
    import smart_money_bot.bot as bot_module

    line = bot_module._gmgn_provider_line(
        {
            "provider": "gmgn",
            "state": "ACTIVE",
            "human": "answering normally",
            "cache_hits": 4,
            "coalesced": 2,
            "rate_limited": 1,
            "p95_latency_ms": 391,
            "rate_limited_for_seconds": 42,
        }
    )

    assert "GMGN state" in line
    assert "p95" in line
    assert "backing off" in line
    assert SECRET not in line


# ===========================================================================
# 57. Real money
# ===========================================================================


def test_nothing_in_this_release_can_spend() -> None:
    import pathlib

    import smart_money_bot

    root = pathlib.Path(smart_money_bot.__file__).parent
    for name in ("token_presentation.py", "gmgn/endpoints.py"):
        text = (root / name).read_text()
        for forbidden in ("Keypair", "send_transaction", "sign_transaction", "aiohttp"):
            assert forbidden not in text, f"{name} must stay signer- and provider-free"

    from smart_money_bot.execution import LiveTradingGates

    assert LiveTradingGates().all_open is False
