"""Regression suite for the GMGN research integration.

Built against the official ``GMGNAI/gmgn-skills`` client at commit ``267ff6b``.
Nothing in this file talks to GMGN: every test drives a fake session, which is
the point — a provider integration whose failure modes can only be exercised
against the live provider has no tested failure modes at all.

Four things these tests exist to keep true:

1. **A provider that is down says nothing about a token.**  Every failure mode
   produces UNKNOWN, never a safety pass and never a silent zero.
2. **The credential never escapes.**  Not into a log, an exception, a status
   payload, a database row, or a Discord embed.
3. **A mint is the identity.**  A GMGN row without a valid exact mint is
   dropped; a row about a different mint never answers a question about this
   one.
4. **This build cannot trade.**  Not "is configured not to" — cannot: the
   signed-auth mode GMGN requires for orders is not implemented, and the
   request path refuses any non-read path.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import pathlib
from decimal import Decimal
from types import SimpleNamespace

import pytest

import smart_money_bot.fast_alerts as fa
from smart_money_bot.execution import (
    GATE_NAMES,
    MODE_LIVE_AUTO,
    MODE_MANUAL_CONFIRM,
    MODE_SHADOW,
    ExecutionIntent,
    LiveOrderPrecheck,
    LiveTradingGates,
    ShadowExecutionProvider,
    evaluate_precheck,
    gates_from_settings,
)
from smart_money_bot.gmgn import (
    ACTIVE,
    ACTIVE_NO_EVENTS,
    AUTH_MISSING,
    AUTH_REJECTED,
    DISABLED_BY_CONFIG,
    ORDER_PATHS,
    PROVIDER_DEGRADED,
    RANK_INTERVALS,
    RATE_LIMIT_BANNED,
    RATE_LIMITED,
    READ_PATHS,
    SIGNAL_TYPES,
    SIGNAL_UNKNOWN,
    TIMEOUT,
    UNKNOWN_STATES,
    GmgnClient,
    GmgnError,
    GmgnParticipant,
    classify_signal,
    classify_stage,
    parse_participants,
    parse_rank_response,
    parse_security,
    parse_signals,
    parse_trenches_response,
)
from smart_money_bot.gmgn import lifecycle as stages
from smart_money_bot.gmgn.budget import BudgetConfig, RequestBudget
from smart_money_bot.gmgn_runtime import GmgnRuntime, independent_provider_wallets

D = Decimal

MINT = "GPR7Ax4kQ2mVn8hLdT6yWc3JbRfE9uZsXqM1oP5tH4dK"
OTHER = "7TqH1d4Vf9QG578vB99Q7ewFQPoxSYqBDxSAzBpBpump"
SECRET = "gmgn-live-key-do-not-leak-0123456789"
NOW = 1_700_000_000


# ===========================================================================
# Fake transport
# ===========================================================================


class _Response:
    def __init__(self, payload, *, status: int = 200, headers: dict | None = None) -> None:
        self._payload = payload
        self.status = status
        self.headers = headers or {}

    async def text(self) -> str:
        return self._payload if isinstance(self._payload, str) else json.dumps(self._payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    """Records every request so the tests can assert on what was actually sent."""

    closed = False

    def __init__(self, handler) -> None:
        self._handler = handler
        self.requests: list[tuple[str, str, dict]] = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return self._handler(method, url, kwargs)


def _client(handler, **overrides) -> GmgnClient:
    payload = {"api_key": SECRET, "session": _Session(handler)}
    payload.update(overrides)
    return GmgnClient(**payload)


def _ok(data):
    return lambda method, url, kwargs: _Response({"code": 0, "data": data})


# ===========================================================================
# 97. Auth, rate limits, failure modes
# ===========================================================================


def test_a_missing_key_is_a_provider_state_not_a_crash() -> None:
    client = GmgnClient(api_key="", session=_Session(_ok([])))

    assert client.configured is False
    assert client.health.state == AUTH_MISSING
    with pytest.raises(GmgnError) as excinfo:
        asyncio.run(client.trending(interval="1m"))
    assert excinfo.value.state == AUTH_MISSING


def test_being_switched_off_is_not_a_failure() -> None:
    client = GmgnClient(api_key=SECRET, enabled=False, session=_Session(_ok([])))

    assert client.health.state == DISABLED_BY_CONFIG
    with pytest.raises(GmgnError) as excinfo:
        asyncio.run(client.trending(interval="1m"))
    assert excinfo.value.state == DISABLED_BY_CONFIG


def test_a_rejected_key_is_classified_as_auth_not_as_degraded() -> None:
    client = _client(
        lambda m, u, k: _Response({"code": 401, "error": "INVALID_API_KEY"}, status=401)
    )

    with pytest.raises(GmgnError) as excinfo:
        asyncio.run(client.trending(interval="1m"))
    assert excinfo.value.state == AUTH_REJECTED
    assert client.health.auth_errors == 1


def test_a_rate_limit_is_never_retried_into() -> None:
    """A 429 answered with an immediate retry is how a limit becomes a ban."""

    client = _client(
        lambda m, u, k: _Response(
            {"code": 429, "error": "RATE_LIMIT_EXCEEDED"},
            status=429,
            headers={"x-ratelimit-reset": str(NOW + 30)},
        )
    )

    with pytest.raises(GmgnError) as excinfo:
        asyncio.run(client.trending(interval="1m"))
    assert excinfo.value.state == RATE_LIMITED
    # Exactly one attempt: no retry, no second request.
    assert len(client._session.requests) == 1
    assert client.health.rate_limited == 1


def test_a_ban_is_harder_than_a_passing_limit() -> None:
    client = _client(
        lambda m, u, k: _Response({"code": 429, "error": "RATE_LIMIT_BANNED"}, status=429)
    )

    with pytest.raises(GmgnError) as excinfo:
        asyncio.run(client.trending(interval="1m"))
    assert excinfo.value.state == RATE_LIMIT_BANNED
    assert client.health.usable is False


def test_a_timeout_is_a_timeout_and_the_bot_keeps_running() -> None:
    def handler(method, url, kwargs):
        raise TimeoutError("too slow")

    client = _client(handler)
    with pytest.raises(GmgnError) as excinfo:
        asyncio.run(client.trending(interval="1m"))

    assert excinfo.value.state == TIMEOUT
    assert client.health.timeouts >= 1


def test_non_json_and_partial_payloads_degrade_rather_than_explode() -> None:
    garbage = _client(lambda m, u, k: _Response("<html>gateway error</html>", status=502))
    with pytest.raises(GmgnError) as excinfo:
        asyncio.run(garbage.trending(interval="1m"))
    assert excinfo.value.state == PROVIDER_DEGRADED

    # A well-formed envelope with nothing in it is a real answer, not an error.
    empty = _client(_ok({"rank": []}))
    assert asyncio.run(empty.trending(interval="1m")) == ()
    assert empty.health.state == ACTIVE_NO_EVENTS

    # Rows missing every optional field parse; the absences stay absent.
    partial = _client(_ok({"rank": [{"address": MINT}]}))
    rows = asyncio.run(partial.trending(interval="1m"))
    assert rows[0].mint == MINT
    assert rows[0].market_cap_usd is None
    assert rows[0].holder_count is None


def test_an_undocumented_interval_is_refused_rather_than_sent() -> None:
    client = _client(_ok({"rank": []}))

    with pytest.raises(GmgnError):
        asyncio.run(client.trending(interval="3m"))
    assert client._session.requests == []
    assert "3m" not in RANK_INTERVALS


def test_the_credential_never_appears_in_health_status_or_errors() -> None:
    """Section 0: never print it, never log it, never put it in an exception."""

    client = _client(
        lambda m, u, k: _Response(
            # A provider echoing the key back must not leak it either.
            {"code": 500, "error": f"upstream said {SECRET}"}, status=500
        )
    )
    with pytest.raises(GmgnError) as excinfo:
        asyncio.run(client.trending(interval="1m"))

    assert SECRET not in str(excinfo.value)
    assert SECRET not in json.dumps(client.usage_snapshot())
    assert SECRET not in json.dumps(client.health.to_json())
    assert SECRET not in repr(client.health)


def test_the_key_is_sent_as_a_header_and_never_as_a_query_parameter() -> None:
    client = _client(_ok({"rank": []}))
    asyncio.run(client.trending(interval="1m"))
    _, url, kwargs = client._session.requests[0]

    assert kwargs["headers"]["X-APIKEY"] == SECRET
    assert SECRET not in url
    assert all(SECRET not in str(value) for _, value in kwargs["params"])


def test_the_documented_auth_query_parameters_are_sent() -> None:
    """timestamp + client_id, per the official signer's buildAuthQuery."""

    client = _client(_ok({"rank": []}))
    asyncio.run(client.trending(interval="1m"))
    params = dict(client._session.requests[0][2]["params"])

    assert int(params["timestamp"]) > 1_600_000_000
    assert len(params["client_id"]) == 36  # a UUID
    assert params["chain"] == "sol"
    assert params["interval"] == "1m"


def test_no_credential_is_written_to_the_database_or_a_card() -> None:
    from smart_money_bot import database as database_module

    source = pathlib.Path(database_module.__file__).read_text()
    assert "api_key" not in source.lower()
    assert "GMGN_API_KEY" not in pathlib.Path(fa.__file__).read_text()


# ===========================================================================
# 108. The live boundary
# ===========================================================================


def test_this_client_implements_no_order_route() -> None:
    """Not "configured off" — absent.  There is nothing behind a flag."""

    assert ORDER_PATHS.isdisjoint(READ_PATHS)
    source = inspect.getsource(GmgnClient)
    for path in ORDER_PATHS:
        assert path not in source
    for forbidden in ("X-Signature", "private_key", "GMGN_PRIVATE_KEY", "sign("):
        assert forbidden not in source


def test_the_request_path_refuses_a_non_read_path() -> None:
    client = _client(_ok({}))

    with pytest.raises(GmgnError) as excinfo:
        asyncio.run(
            client._request("POST", "/v1/trade/swap", kind="rank", body={"chain": "sol"})
        )
    assert "research reads only" in str(excinfo.value)
    assert client._session.requests == []


def test_no_signing_key_is_read_anywhere_in_the_codebase() -> None:
    import smart_money_bot

    root = pathlib.Path(smart_money_bot.__file__).parent
    # Code shapes, not prose: the client's own docstring names the variable in
    # order to explain that it is deliberately never read, and a bare substring
    # search would flag exactly the module being careful.
    reads = ('getenv("GMGN_PRIVATE_KEY"', "environ['GMGN_PRIVATE_KEY']",
             'environ["GMGN_PRIVATE_KEY"]', "gmgn_private_key")
    for path in root.rglob("*.py"):
        text = path.read_text()
        for needle in reads:
            assert needle not in text, f"{path.name} reads a signing key"


def test_every_live_gate_defaults_to_false() -> None:
    gates = LiveTradingGates()

    assert gates.all_open is False
    assert set(gates.blocked_by()) == set(GATE_NAMES)
    # And an object that has never heard of them is closed, not open.
    assert gates_from_settings(SimpleNamespace()).all_open is False


def test_a_shadow_intent_records_and_spends_nothing() -> None:
    provider = ShadowExecutionProvider()
    intent = ExecutionIntent(mint=MINT, side="BUY", size_usd=D("10"), signal_id="s1")
    receipt = asyncio.run(provider.submit(intent))

    assert receipt.accepted is True
    assert receipt.mode == MODE_SHADOW
    assert receipt.real_money_spent_usd == D("0")
    assert provider.can_trade is False


@pytest.mark.parametrize("mode", [MODE_MANUAL_CONFIRM, MODE_LIVE_AUTO])
def test_a_live_mode_is_refused_and_spends_nothing(mode: str) -> None:
    provider = ShadowExecutionProvider()
    receipt = asyncio.run(
        provider.submit(ExecutionIntent(mint=MINT, side="BUY", size_usd=D("10"), mode=mode))
    )

    assert receipt.accepted is False
    assert receipt.real_money_spent_usd == D("0")
    assert provider.recorded == []


def test_opening_every_gate_still_does_not_produce_a_trade() -> None:
    """The gates are necessary, never sufficient: no provider here can trade."""

    provider = ShadowExecutionProvider(
        gates=LiveTradingGates(True, True, True)
    )
    receipt = asyncio.run(
        provider.submit(
            ExecutionIntent(mint=MINT, side="BUY", size_usd=D("10"), mode=MODE_LIVE_AUTO)
        )
    )

    assert receipt.accepted is False
    assert receipt.reason == "EXECUTION_MODE_NOT_IMPLEMENTED"
    assert receipt.real_money_spent_usd == D("0")


def test_a_shadow_record_cannot_become_a_live_order_by_a_flag_change() -> None:
    """Section 82: the mode is a property of the record, not of the process."""

    intent = ExecutionIntent(mint=MINT, side="BUY", size_usd=D("10"), mode=MODE_SHADOW)
    provider = ShadowExecutionProvider(gates=LiveTradingGates(True, True, True))
    receipt = asyncio.run(provider.submit(intent))

    assert intent.mode == MODE_SHADOW
    assert receipt.simulated is True
    assert receipt.real_money_spent_usd == D("0")


def test_a_replayed_signal_produces_the_same_order_id() -> None:
    """Section 81: a restart must not turn one decision into two orders."""

    first = ExecutionIntent(
        mint=MINT, side="BUY", size_usd=D("10"), signal_id="sig-1", strategy_id="trend"
    )
    replay = ExecutionIntent(
        mint=MINT, side="BUY", size_usd=D("10"), signal_id="sig-1", strategy_id="trend",
        created_at=NOW + 999,
    )
    other_mint = ExecutionIntent(
        mint=OTHER, side="BUY", size_usd=D("10"), signal_id="sig-1", strategy_id="trend"
    )

    assert first.client_order_id == replay.client_order_id
    assert first.client_order_id != other_mint.client_order_id
    # A deliberate second attempt is a different order, which is the point.
    assert first.client_order_id != ExecutionIntent(
        mint=MINT, side="BUY", size_usd=D("10"), signal_id="sig-1",
        strategy_id="trend", attempt=2,
    ).client_order_id


def test_the_live_precheck_lists_every_unmet_requirement() -> None:
    failures = evaluate_precheck(LiveOrderPrecheck())

    assert "NO_SELL_ROUTE" in failures
    assert "HARD_SAFETY_NOT_PASSED" in failures
    assert "NO_EXACT_MINT" in failures
    # A position you cannot exit is not a position.
    assert evaluate_precheck(
        LiveOrderPrecheck(
            exact_mint=MINT, signal_age_seconds=5, buy_route_available=True,
            sell_route_available=False, liquidity_usd=D("50000"), slippage_bps=50,
            price_impact_bps=50, provider_healthy=True, hard_safety_passed=True,
        )
    ) == ("NO_SELL_ROUTE",)


def test_no_module_on_the_gmgn_path_can_sign_or_submit_a_transaction() -> None:
    import smart_money_bot.gmgn as gmgn_package

    root = pathlib.Path(gmgn_package.__file__).parent
    for path in root.glob("*.py"):
        text = path.read_text()
        for forbidden in ("Keypair", "send_transaction", "sign_transaction", "solders"):
            assert forbidden not in text, f"gmgn/{path.name} must stay signer-free"


# ===========================================================================
# 98. Exact mint
# ===========================================================================


def test_a_row_without_a_valid_exact_mint_is_dropped() -> None:
    rows = parse_rank_response(
        {
            "rank": [
                {"address": MINT, "symbol": "GPRO"},
                {"symbol": "NOMINT"},
                {"address": "not-a-real-mint", "symbol": "BAD"},
                {"address": "", "symbol": "EMPTY"},
            ]
        },
        interval="1m",
    )

    assert [item.mint for item in rows] == [MINT]


def test_a_participant_row_about_another_mint_never_answers_for_this_one() -> None:
    holders = parse_participants(
        {
            "holders": [
                {"address": "W1", "holding_percentage": "12"},
                {"address": "W2", "token_address": OTHER, "holding_percentage": "99"},
            ]
        },
        mint=MINT,
    )

    assert [item.wallet for item in holders] == ["W1"]
    assert all(item.mint == MINT for item in holders)


def test_a_symbol_is_never_used_to_resolve_a_token() -> None:
    from smart_money_bot.gmgn import models

    source = pathlib.Path(models.__file__).read_text()
    # The only lookup keys are address-shaped.
    assert '"symbol"' not in source.split("_mint_of")[1].split("def ")[0]
    assert "is_valid_mint" in source


def test_the_runtime_refuses_a_row_it_cannot_identify() -> None:
    runtime = GmgnRuntime(GmgnClient(api_key=SECRET, session=_Session(_ok([]))))
    from smart_money_bot.gmgn.models import GmgnToken

    assert runtime._accept(GmgnToken(mint=MINT)) is True
    assert runtime._accept(GmgnToken(mint="nope")) is False


# ===========================================================================
# 99. Lifecycle
# ===========================================================================


def test_the_documented_curve_states_map_to_our_stages() -> None:
    assert classify_stage(bonding_progress=D("0.01"), complete=False) == stages.NEW_PAIR
    assert classify_stage(bonding_progress=D("0.5"), complete=False) == stages.MID_CURVE
    assert classify_stage(bonding_progress=D("0.8"), complete=False) == stages.FINAL_STRETCH
    assert (
        classify_stage(bonding_progress=D("0.95"), complete=False) == stages.NEAR_COMPLETION
    )
    assert (
        classify_stage(bonding_progress=None, complete=True, seconds_since_migration=60)
        == stages.RECENTLY_MIGRATED
    )
    assert (
        classify_stage(bonding_progress=None, complete=True, on_amm=True,
                       seconds_since_migration=99_999)
        == stages.PUMPSWAP
    )
    assert (
        classify_stage(bonding_progress=None, complete=True, trending_rank=3)
        == stages.TRENDING
    )


def test_an_unknown_curve_reading_is_unknown_not_early() -> None:
    assert classify_stage(bonding_progress=None, complete=False) == stages.UNKNOWN


def test_the_same_mint_keeps_its_history_across_stages() -> None:
    """Section 10: a token that graduates is not a new discovery."""

    life = stages.open_lifecycle(
        MINT, stage=stages.TOKEN_CREATED, at=0, market_cap_usd=D("4000"),
        source="pump_realtime",
    )
    for stage, at, mc, source in (
        (stages.NEW_PAIR, 30, D("9000"), "pump_realtime"),
        (stages.FINAL_STRETCH, 600, D("52000"), "gmgn_trenches"),
        (stages.RECENTLY_MIGRATED, 900, D("69000"), "gmgn_trenches"),
        (stages.TRENDING, 1_200, D("180000"), "gmgn_rank"),
    ):
        life = stages.advance(life, stage=stage, at=at, market_cap_usd=mc, source=source)

    assert life.first_seen_market_cap_usd == D("4000")
    assert life.render_path().startswith("TOKEN_CREATED → NEW_PAIR")
    assert life.move_since_first_seen_percent() == D("4400.00")
    assert life.board_section == stages.BOARD_MIGRATED
    assert life.sources == ("pump_realtime", "gmgn_trenches", "gmgn_rank")


def test_a_stale_read_cannot_un_graduate_a_token() -> None:
    life = stages.open_lifecycle(MINT, stage=stages.TRENDING, at=100, market_cap_usd=D("100"))
    stale = stages.advance(life, stage=stages.EARLY_CURVE, at=110, market_cap_usd=D("90"))

    assert stale.stage == stages.TRENDING
    assert stale.current_market_cap_usd == D("90")
    assert stale.peak_market_cap_usd == D("100")


def test_source_lead_is_measurable() -> None:
    """Section 95: did realtime discovery actually see it first?"""

    life = stages.open_lifecycle(MINT, stage=stages.NEW_PAIR, at=0, source="pump_realtime")
    life = stages.advance(life, stage=stages.TRENDING, at=600, source="gmgn_rank")

    assert life.lead_over(stages.NEW_PAIR, stages.TRENDING) == 600


def test_the_trench_sections_map_to_our_lifecycle_explicitly() -> None:
    from smart_money_bot.gmgn_runtime import TRENCH_STAGE

    assert TRENCH_STAGE["new_creation"] == stages.NEW_PAIR
    assert TRENCH_STAGE["near_completion"] == stages.FINAL_STRETCH
    assert TRENCH_STAGE["completed"] == stages.RECENTLY_MIGRATED


# ===========================================================================
# 100-101. Trending, smart money, KOLs
# ===========================================================================


def test_the_trenches_body_matches_the_official_client_shape() -> None:
    captured: dict = {}

    def handler(method, url, kwargs):
        captured.update(kwargs)
        return _Response({"code": 0, "data": {}})

    asyncio.run(_client(handler).trenches())
    body = captured["json"]

    assert body["version"] == "v2"
    assert set(body) == {"version", "new_creation", "near_completion", "completed"}
    section = body["new_creation"]
    assert section["filters"] == ["offchain", "onchain"]
    assert section["launchpad_platform_v2"] is True
    assert section["quote_address_type"] == [4, 5, 3, 1, 13, 0]
    # Platforms are left to the service: a client-side allow-list would hide
    # newly supported launchpads until this bot is redeployed.
    assert "launchpad_platform" not in section


def test_trenches_sections_are_parsed_per_section() -> None:
    sections = parse_trenches_response(
        {
            "new_creation": {"tokens": [{"address": MINT}]},
            "completed": {"tokens": [{"address": OTHER}]},
            "some_new_section": {"tokens": [{"address": MINT}]},
        }
    )

    assert set(sections) == {"new_creation", "completed", "some_new_section"}
    # An unrecognised section is kept, not dropped: it is a feed to go and read
    # about, and discarding it silently hides something we already pay for.
    assert sections["some_new_section"][0].mint == MINT


def test_a_provider_signal_code_is_named_or_reported_unknown() -> None:
    assert classify_signal(12).name == "SMART_DEGEN_BUY"
    assert classify_signal(20).name == "KOL_BUY"
    assert classify_signal(2).name == "DEX_AD"
    unknown = classify_signal(999)
    assert unknown.name == SIGNAL_UNKNOWN
    assert unknown.known is False
    assert unknown.demand is False
    assert len(SIGNAL_TYPES) == 21


def test_paid_placement_is_never_treated_as_demand() -> None:
    """A Dex ad is someone buying a slot, not someone buying the token."""

    rows = parse_signals(
        {"list": [{"address": MINT, "signal_type": code} for code in (2, 5, 12, 14)]}
    )
    demand = {item.signal_name for item in rows if item.demand}

    assert demand == {"SMART_DEGEN_BUY", "LARGE_AMOUNT_BUY"}


def test_a_kol_is_not_smart_money() -> None:
    """Section 20: famous is attention; it is not measured expectancy."""

    smart = _fake_participant_alert(kind=fa.GMGN_SMART_MONEY_ALERT)
    kol = _fake_participant_alert(kind=fa.GMGN_KOL_ALERT)

    assert "SMART MONEY BUY" in smart.spec.title
    assert "KOL ACTIVITY" in kol.spec.title
    assert "SMART MONEY" not in kol.spec.title
    assert smart.ping is True
    assert kol.ping is False, "fame does not earn an interruption"
    assert kol.lane == fa.LANE_RADAR


def test_the_card_separates_the_provider_label_from_our_own_record() -> None:
    """Section 24: a GMGN tag is evidence about a classification, not a record."""

    alert = _fake_participant_alert(bot_reputation="LATE_CHASER", bot_reputation_samples=19)
    wallet_field = {field.name: field.value for field in alert.spec.fields}["WALLET"]

    assert "GMGN classification: **SMART MONEY**" in wallet_field
    assert "Our own forward record: `LATE_CHASER` (19 samples)" in wallet_field


def test_a_wallet_with_no_forward_sample_says_so() -> None:
    wallet_field = {
        field.name: field.value for field in _fake_participant_alert().spec.fields
    }["WALLET"]

    assert "no sample yet" in wallet_field
    assert "a classification, not a track record" in wallet_field


def test_a_late_smart_money_signal_is_shown_but_not_dressed_up_as_early() -> None:
    """Section 50: do not hide it, do not pretend it was early."""

    late = _fake_participant_alert(
        wallet_entry_market_cap_usd=D("50000"),
        current_market_cap_usd=D("300000"),
        edge_consumed=True,
    )

    assert "EDGE CONSUMED" in late.spec.title
    assert "LOOK NOW" not in late.spec.title
    assert late.ping is False
    assert late.lane == fa.LANE_RADAR


def test_an_unverified_identity_outranks_every_provider_label() -> None:
    unverified = _fake_participant_alert(identity_verified=False)

    assert "IDENTITY UNVERIFIED" in unverified.spec.title
    assert unverified.ping is False
    assert unverified.trade_eligible is False


def test_a_participant_card_never_offers_a_buy() -> None:
    alert = _fake_participant_alert()
    state = {field.name: field.value for field in alert.spec.fields}["STATE"]

    assert alert.trade_eligible is False
    assert "Trade CTA: **DISABLED**" in state
    assert "Safety: **UNKNOWN**" in state


def _fake_participant_alert(**overrides):
    payload = {
        "mint": MINT,
        "name": "Grok Pocket",
        "symbol": "GPRO",
        "fomo_url": f"https://fomo.family/coin?address={MINT}",
        "wallet": "WalletAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1",
        "kind": fa.GMGN_SMART_MONEY_ALERT,
        "trade_usd": D("4200"),
        "wallet_entry_market_cap_usd": D("69000"),
        "detection_market_cap_usd": D("71000"),
        "current_market_cap_usd": D("78000"),
        "safety_status": "UNKNOWN",
    }
    payload.update(overrides)
    return fa.build_gmgn_participant_alert(**payload)


# ===========================================================================
# 102-103. Holders, traders, clusters
# ===========================================================================


def test_provider_tagged_wallets_are_collapsed_per_cluster() -> None:
    """Section 32: a provider tag does not exempt a sybil group."""

    tagged = [GmgnParticipant(wallet=f"w{index}", is_smart_money=True) for index in range(20)]

    assert independent_provider_wallets(tagged) == 20
    assert (
        independent_provider_wallets(
            tagged, clusters={f"w{index}": "funder:X" for index in range(20)}
        )
        == 1
    )


def test_untagged_wallets_do_not_count_as_provider_confirmation() -> None:
    plain = [GmgnParticipant(wallet="w1"), GmgnParticipant(wallet="w2")]

    assert independent_provider_wallets(plain) == 0


def test_one_wallet_carrying_two_tags_is_still_one_wallet() -> None:
    both = [GmgnParticipant(wallet="w1", is_smart_money=True, is_kol=True)]

    assert independent_provider_wallets(both) == 1


def test_participant_flags_are_read_from_documented_fields() -> None:
    """v2.45 guessed at boolean flags that do not exist in the response.

    The documented holder object carries two tag arrays — ``tags`` for the
    wallet and ``maker_token_tags`` for what it did to *this* token — and no
    ``is_smart_money``/``is_sniper`` booleans at all.  Looking for fields that
    were never sent is why nothing was ever tagged at token level.
    """

    parsed = parse_participants(
        {
            "holders": [
                {
                    "address": "W1",
                    # A 0-1 fraction, not a percent.  Reading it as a percent
                    # turns an 8.5% holder into "0.085%".
                    "amount_percentage": 0.085,
                    "realized_profit": "1200",
                    "sell_volume_cur": "2400",
                    "sell_amount_percentage": "0.2",
                    "buy_tx_count_cur": 3,
                    "tags": ["smart_degen"],
                    "maker_token_tags": ["sniper"],
                    "native_transfer": {"from_address": "FUNDER1"},
                }
            ]
        },
        mint=MINT,
    )[0]

    assert parsed.is_smart_money is True
    assert parsed.sniper is True
    assert parsed.bundler is False
    assert parsed.realized_pnl_usd == D("1200")
    assert parsed.holding_percent == D("8.50")
    assert parsed.sold_usd == D("2400")
    assert parsed.sold_fraction == D("0.2")
    assert parsed.buys == 3
    # The funding edge arrives with the row: free cluster evidence.
    assert parsed.funded_by == "FUNDER1"


def test_a_liquidity_pool_is_not_reported_as_a_holder() -> None:
    """``addr_type`` 2 is a DEX pool.  Counting one as a whale makes every
    token look dangerously concentrated."""

    rows = parse_participants(
        {
            "holders": [
                {"address": "W1", "amount_percentage": 0.05, "addr_type": 0},
                {"address": "POOL", "amount_percentage": 0.56, "addr_type": 2},
            ]
        },
        mint=MINT,
    )

    by_wallet = {item.wallet: item for item in rows}
    assert by_wallet["POOL"].address_type == 2
    assert by_wallet["W1"].address_type == 0
    assert by_wallet["POOL"].to_json()["is_pool"] is True


def test_smart_money_and_kol_are_trade_feeds_not_wallet_directories() -> None:
    """The exact cause of ``Smart-money wallets: 0`` in production.

    ``/v1/user/smartmoney`` returns *trades* by platform-tagged wallets, not a
    list of wallets.  v2.45 looked for a top-level ``wallet_address``, found
    none, and reported zero forever.
    """

    from smart_money_bot.gmgn import parse_wallet_trades

    trades = parse_wallet_trades(
        {
            "list": [
                {
                    "maker": "W9",
                    "base_address": MINT,
                    "side": "buy",
                    "amount_usd": 4200,
                    "timestamp": NOW,
                    "is_open_or_close": 1,
                    "base_token": {"symbol": "MDR", "logo": "https://x/mdr.png"},
                    "maker_info": {"name": "Alpha", "tags": ["smart_degen", "gmgn"]},
                },
                {"maker": "W8", "base_address": MINT, "maker_info": {"tags": ["kol"]}},
                {"maker": "W7", "base_address": "not-a-mint"},
            ]
        },
        tag="GMGN_SMART_MONEY",
    )

    assert len(trades) == 2, "a row without a valid exact mint is dropped"
    assert trades[0].is_smart_money is True and trades[0].is_kol is False
    assert trades[1].is_kol is True and trades[1].is_smart_money is False
    assert trades[0].amount_usd == D("4200")
    # The feed carries the token's symbol and logo, which is what fills the
    # presentation cache and stops cards rendering "?".
    assert trades[0].symbol == "MDR"
    assert trades[0].image_url == "https://x/mdr.png"


# ===========================================================================
# 40-41. Safety: a provider outage is not a token verdict
# ===========================================================================


def test_a_provider_outage_reads_as_unknown_never_as_clean() -> None:
    down = parse_security(None, mint=MINT)

    assert down.provider_available is False
    assert down.unknown is True
    assert down.hard_fail is False, "an absent answer is not a passing one"


def test_only_an_explicit_provider_statement_is_a_hard_fail() -> None:
    silent = parse_security({}, mint=MINT)
    honeypot = parse_security({"is_honeypot": True}, mint=MINT)
    unsellable = parse_security({"can_sell": False}, mint=MINT)

    assert silent.hard_fail is False and silent.unknown is True
    assert honeypot.hard_fail is True
    assert unsellable.hard_fail is True


def test_a_security_call_that_fails_returns_unknown_rather_than_raising() -> None:
    client = _client(lambda m, u, k: _Response({"code": 500, "error": "boom"}, status=500))
    security = asyncio.run(client.security(MINT))

    assert security.unknown is True
    assert security.hard_fail is False


def test_every_unhealthy_state_is_an_unknown_state() -> None:
    assert ACTIVE not in UNKNOWN_STATES
    assert ACTIVE_NO_EVENTS not in UNKNOWN_STATES
    for state in (AUTH_MISSING, AUTH_REJECTED, RATE_LIMITED, TIMEOUT, PROVIDER_DEGRADED):
        assert state in UNKNOWN_STATES


# ===========================================================================
# 7. Budget, cache, coalescing, breaker
# ===========================================================================


def test_identical_simultaneous_requests_become_one_call() -> None:
    """A cache only helps after the first call returns; coalescing is the rest."""

    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        await asyncio.sleep(0.01)
        return ["row"]

    async def main():
        budget = RequestBudget()
        key = budget.cache_key("rank", chain="sol", interval="1m")
        await asyncio.gather(*[budget.run(key, factory, ttl=20) for _ in range(6)])
        await budget.run(key, factory, ttl=20)
        return budget

    budget = asyncio.run(main())
    assert calls["n"] == 1
    assert budget.coalesced == 5
    assert budget.cache_hits == 1


def test_a_failed_call_does_not_poison_the_cache() -> None:
    async def main():
        budget = RequestBudget()
        key = budget.cache_key("rank", chain="sol")

        async def boom():
            raise RuntimeError("down")

        with pytest.raises(RuntimeError):
            await budget.run(key, boom, ttl=20)
        return budget.cached(key)[0]

    assert asyncio.run(main()) is False


def test_the_breaker_opens_after_repeated_failures_and_closes_on_success() -> None:
    budget = RequestBudget(config=BudgetConfig(breaker_threshold=2, breaker_seconds=30))

    budget.note_failure()
    assert budget.admit() == "ALLOW"
    budget.note_failure()
    assert budget.admit() == "DENY_BREAKER_OPEN"
    budget.note_success()
    assert budget.admit() == "ALLOW"


def test_the_budget_stops_a_burst_from_spending_the_hour() -> None:
    budget = RequestBudget(config=BudgetConfig(max_calls_per_minute=3))
    for _ in range(3):
        budget.note_call()

    assert budget.admit() == "DENY_BUDGET"


def test_a_rate_limit_window_is_honoured_as_a_duration() -> None:
    """The reset header is wall-clock; the budget is monotonic.  Convert, do not mix."""

    budget = RequestBudget()
    budget.note_rate_limited(reset_at_unix=NOW + 45, wall_now=NOW)

    assert budget.admit() == "DENY_RATE_LIMITED"
    assert 40 <= budget.rate_limited_for <= 50


def test_expensive_endpoints_are_cached_for_longer_than_cheap_ones() -> None:
    budget = RequestBudget()

    assert budget.ttl_for("rank") < budget.ttl_for("token_security")
    assert budget.ttl_for("token_security") < budget.ttl_for("created_tokens")


# ===========================================================================
# 8, 41. The runtime keeps working when a feed does not
# ===========================================================================


def _runtime_settings(**overrides):
    payload = {
        "gmgn_trending_intervals": ("1m",),
        "gmgn_trending_limit": 50,
        "gmgn_trenches_enabled": True,
        "gmgn_trenches_limit": 60,
        "gmgn_market_signals_enabled": True,
        "gmgn_hot_search_enabled": True,
        "gmgn_smart_money_enabled": True,
        "gmgn_kol_enabled": True,
        "gmgn_holders_enabled": True,
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def test_one_failing_feed_does_not_cost_us_the_others() -> None:
    def handler(method, url, kwargs):
        if "/v1/trenches" in url:
            return _Response({"code": 500, "error": "boom"}, status=500)
        if "/v1/market/rank" in url:
            return _Response({"code": 0, "data": {"rank": [{"address": MINT}]}})
        return _Response({"code": 0, "data": []})

    runtime = GmgnRuntime(_client(handler), settings=_runtime_settings())
    result = asyncio.run(runtime.scan(now=NOW))

    assert any("trenches" in error for error in result.errors)
    assert [item.mint for item in result.candidates] == [MINT]


def test_an_unconfigured_provider_returns_an_empty_result_not_an_exception() -> None:
    runtime = GmgnRuntime(GmgnClient(api_key="", session=_Session(_ok([]))))
    result = asyncio.run(runtime.scan(now=NOW))

    assert result.candidates == ()
    assert result.errors == ("gmgn not configured",)


def test_discovery_priority_puts_trending_ahead_of_attention() -> None:
    """Section 8: hot search surfaces a mint; it does not classify it."""

    def handler(method, url, kwargs):
        if "/v1/market/rank" in url:
            return _Response({"code": 0, "data": {"rank": [{"address": MINT}]}})
        if "/v1/market/hot_searches" in url:
            return _Response(
                {"code": 0, "data": [{"interval": "24h", "tokens": [{"address": MINT}]}]}
            )
        return _Response({"code": 0, "data": {}})

    runtime = GmgnRuntime(_client(handler), settings=_runtime_settings())
    result = asyncio.run(runtime.scan(now=NOW))

    assert len(result.candidates) == 1
    assert result.candidates[0].family == "GMGN_TRENDING"


def test_the_status_payload_is_safe_to_display() -> None:
    runtime = GmgnRuntime(_client(_ok({"rank": []})), settings=_runtime_settings())
    asyncio.run(runtime.scan(now=NOW))
    status = runtime.status()

    assert SECRET not in json.dumps(status)
    assert status["provider"] == "gmgn"
    assert "board" in status


# ===========================================================================
# 64-67. Attribution and cohorts
# ===========================================================================


def test_every_gmgn_family_is_registered_and_labelled() -> None:
    from smart_money_bot.lab.shadow import FAMILY_LABELS, GMGN_FAMILIES, SIGNAL_FAMILIES

    assert len(GMGN_FAMILIES) == 13
    for family in GMGN_FAMILIES:
        assert family in SIGNAL_FAMILIES
        assert family in FAMILY_LABELS


def test_existing_signal_families_were_not_removed() -> None:
    """Section 62: no experiment is reset and no family disappears."""

    from smart_money_bot.lab.shadow import SIGNAL_FAMILIES

    for family in (
        "FAST_WATCH",
        "NOTABLE_TRADER_EARLY",
        "TRENDING_NEW_ENTRY",
        "TRENDING_CONTINUATION",
        "PUMP_TRENCH_RUNNER",
        "PUBLIC_TRENDING_MODEL",
    ):
        assert family in SIGNAL_FAMILIES


def test_provider_label_cohorts_separate_kol_from_smart_money() -> None:
    from smart_money_bot.lab.forward import (
        COHORT_GMGN_KOL_AND_SMART_MONEY,
        COHORT_GMGN_KOL_ONLY,
        COHORT_MULTI_GMGN_SMART_MONEY,
        COHORT_NO_GMGN_SMART_MONEY,
        assign_cohorts,
    )

    assert COHORT_NO_GMGN_SMART_MONEY in assign_cohorts()
    assert COHORT_GMGN_KOL_ONLY in assign_cohorts(gmgn_kol_wallets=2)
    assert COHORT_GMGN_KOL_AND_SMART_MONEY in assign_cohorts(
        gmgn_kol_wallets=1, gmgn_smart_money_wallets=1
    )
    assert COHORT_MULTI_GMGN_SMART_MONEY in assign_cohorts(gmgn_smart_money_wallets=4)


def test_the_shadow_position_size_contract_is_unchanged() -> None:
    """Section 63: $100 bankroll, exactly $10 per position, 5 concurrent, $50 max."""

    from smart_money_bot.lab.shadow import DEFAULT_SHADOW_CONFIG as config

    assert config.position_usd == D("10")
    assert config.min_position_usd == config.max_position_usd == D("10")
    assert config.bankroll_usd == D("100")
    assert config.max_concurrent_positions == 5
    assert config.max_total_exposure_usd == D("50")


# ===========================================================================
# The source contract this integration was written against
# ===========================================================================


def test_the_upstream_contract_is_recorded() -> None:
    """So a future reader can diff the contract instead of re-deriving it."""

    from smart_money_bot.gmgn import GMGN_SOURCE_COMMIT, GMGN_SOURCE_REPO

    assert GMGN_SOURCE_REPO == "https://github.com/GMGNAI/gmgn-skills"
    assert len(GMGN_SOURCE_COMMIT) == 40


def test_every_read_path_is_one_the_official_client_uses() -> None:
    documented = {
        "/v1/market/rank",
        "/v1/trenches",
        "/v1/market/hot_searches",
        "/v1/market/token_signal",
        "/v1/market/token_top_holders",
        "/v1/market/token_top_traders",
        "/v1/market/token_kline",
        "/v1/token/info",
        "/v1/token/security",
        "/v1/token/pool_info",
        "/v1/user/smartmoney",
        "/v1/user/kol",
        "/v1/user/created_tokens",
        "/v1/user/info",
    }

    assert documented == READ_PATHS
