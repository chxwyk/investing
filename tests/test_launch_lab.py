from __future__ import annotations

import asyncio
import io
import sqlite3
import time
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from smart_money_bot.bot import (
    LaunchConfirmationView,
    LaunchLabEditModal,
    LaunchLabView,
    SmartMoneyCommands,
    _launch_result_view,
    _member_is_admin,
)
from smart_money_bot.constants import PUMP_LAUNCH_ACK_TEXT
from smart_money_bot.database import Database
from smart_money_bot.engine import SmartMoneyEngine
from smart_money_bot.errors import PumpLaunchError, UnknownLaunchResultError
from smart_money_bot.launch import (
    NO_X_LAUNCH_VERDICT,
    X_VERIFIED_LAUNCH_VERDICT,
    J7LaunchClient,
    _j7_http_error,
    default_launch_draft,
    launch_draft_key,
    score_launch_opportunity,
    validate_launch_draft,
)
from smart_money_bot.models import (
    NarrativeCompetition,
    NewsAlert,
    PumpLaunchResult,
    XSocialSnapshot,
)

PUBLIC_WALLET = "So11111111111111111111111111111111111111112"


def _opportunity(now: int | None = None, *, x_verified: bool = False):
    now = now or int(time.time())
    alert = NewsAlert(
        source="CoinDesk",
        headline='BREAKING: Solana traders launch viral "Kitchen Moment" meme',
        summary="Crypto traders say the Kitchen Moment is spreading across Solana communities.",
        url="https://coindesk.com/markets/kitchen-moment",
        narrative_terms=("Kitchen Moment", "Solana"),
        image_urls=("https://cdn.example.com/kitchen.jpg",),
        created_at=now - 20,
        received_at=now,
    )
    social = (
        XSocialSnapshot(
            available=True,
            posts=12,
            unique_authors=8,
            established_authors=5,
            influential_authors=2,
            crypto_authors=6,
            credible_crypto_authors=4,
            promoter_posts=4,
            engagements=500,
            posts_per_minute=Decimal("1"),
            duplicate_percent=Decimal("5"),
        )
        if x_verified
        else None
    )
    return score_launch_opportunity(
        alert,
        x_evidence=social,
        competition=NarrativeCompetition(query="Kitchen Moment"),
        cross_source_count=2,
        now=now,
    )


def _configured(settings):
    return replace(
        settings,
        pump_launch_ack=PUMP_LAUNCH_ACK_TEXT,
        j7_launch_enabled=True,
        j7_launch_session_token="secret-session-jwt",
        j7_launch_api_key="secret-encrypted-wallet-key",
        j7_launch_wallet_address=PUBLIC_WALLET,
        pinata_jwt="secret-pinata-jwt",
    )


def _below_floor_opportunity(now: int | None = None):
    opportunity = _opportunity(now)
    return replace(
        opportunity,
        alert=replace(opportunity.alert, score=40),
        score=40,
        verdict="WATCH",
        x_verified=False,
        no_x_candidate_ready=False,
        positives=("credible current source",),
        warnings=("X/social velocity was not verified.",),
    )


class FakeResponse:
    def __init__(self, status: int, body=None) -> None:
        self.status = status
        self.body = body if body is not None else {}
        self.url = "https://lax.j7tracker.io/deploy/ping"
        self.headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def read(self):
        return b""

    async def json(self, **_kwargs):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class FakeSession:
    def __init__(self, *, get_response=None, post_response=None) -> None:
        self.get_response = get_response or FakeResponse(200)
        self.post_response = post_response or FakeResponse(200)
        self.closed = False
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self.get_response

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self.post_response


@pytest.mark.asyncio
async def test_public_wallet_balance_lookup_uses_public_address(settings) -> None:
    rpc = SimpleNamespace(call=AsyncMock(return_value={"value": 34_200_000}))
    client = J7LaunchClient(_configured(settings), rpc)

    balance = await client.wallet_balance()

    assert balance == Decimal("0.0342")
    rpc.call.assert_awaited_once_with(
        "getBalance", [PUBLIC_WALLET, {"commitment": "confirmed"}]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wallet", "message"),
    [(None, "not configured"), ("not-a-wallet", "not a valid Solana")],
)
async def test_missing_or_invalid_public_wallet_is_explicit(settings, wallet, message) -> None:
    client = J7LaunchClient(
        replace(_configured(settings), j7_launch_wallet_address=wallet),
        SimpleNamespace(call=AsyncMock()),
    )

    with pytest.raises(PumpLaunchError, match=message):
        await client.wallet_balance()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "healthy", "message"),
    [
        (200, True, "HEALTHY"),
        (401, False, "SESSION EXPIRED"),
        (403, False, "AUTH FAILED"),
        (429, False, "RATE LIMITED"),
        (500, False, "UNHEALTHY"),
    ],
)
async def test_j7_health_statuses_are_sanitized(settings, status, healthy, message) -> None:
    client = J7LaunchClient(_configured(settings), SimpleNamespace())
    session = FakeSession(get_response=FakeResponse(status))
    client._session = session

    result, detail = await client.health_check()

    assert result is healthy
    assert message in detail
    assert "secret" not in detail


def test_j7_submission_error_map_covers_auth_rate_limit_and_server() -> None:
    assert "SESSION EXPIRED" in _j7_http_error(401)
    assert "AUTH FAILED" in _j7_http_error(403)
    assert "RATE LIMITED" in _j7_http_error(429)
    assert "SERVER ERROR" in _j7_http_error(500)


@pytest.mark.asyncio
async def test_launch_check_spends_zero_sol_and_hides_credentials(settings, tmp_path) -> None:
    configured = _configured(settings)
    engine = SmartMoneyEngine(configured)
    engine.database = Database(str(tmp_path / "check.db"), Decimal("1000"))
    await engine.database.connect()
    engine.pump_launcher.j7.health_check = AsyncMock(return_value=(True, "HEALTHY"))
    engine.pump_launcher.j7.pinata_health = AsyncMock(return_value=(True, "READY"))
    engine.pump_launcher.j7.wallet_balance = AsyncMock(return_value=Decimal("0.0342"))
    engine.pump_launcher.j7.launch = AsyncMock()
    try:
        report = await engine.launch_readiness()
    finally:
        await engine.database.close()
        await engine.pump_launcher.close()

    assert report["overall_ready"] is True
    engine.pump_launcher.j7.launch.assert_not_awaited()
    rendered = repr(report)
    assert "secret-session-jwt" not in rendered
    assert "secret-encrypted-wallet-key" not in rendered
    assert "secret-pinata-jwt" not in rendered


def _engine_for_launch(settings, *, balance=Decimal("0.03"), reserved=True):
    engine = object.__new__(SmartMoneyEngine)
    engine.settings = _configured(settings)
    j7 = SimpleNamespace(
        configured=True,
        wallet_balance=AsyncMock(return_value=balance),
        launch=AsyncMock(
            return_value=PumpLaunchResult(
                success=True,
                status="SUBMITTED",
                message="created",
                alert_key="key",
                name="Kitchen Moment",
                symbol="KM",
                mint=PUBLIC_WALLET,
                signature="signature",
                metadata_uri="https://ipfs.io/ipfs/cid",
                created_at=int(time.time()),
                provider="J7 Tracker",
            )
        ),
    )
    engine.pump_launcher = SimpleNamespace(
        j7=j7,
        pump=SimpleNamespace(launch=AsyncMock()),
    )
    engine.database = SimpleNamespace(
        pump_launch_daily_usage=AsyncMock(return_value=(0, Decimal("0"))),
        reserve_pump_launch=AsyncMock(return_value=reserved),
        complete_pump_launch=AsyncMock(),
        fail_pump_launch=AsyncMock(),
        mark_pump_launch_unknown=AsyncMock(),
        set_setting=AsyncMock(),
    )
    return engine


@pytest.mark.asyncio
async def test_insufficient_sol_blocks_before_j7(settings) -> None:
    engine = _engine_for_launch(settings, balance=Decimal("0.001"))
    draft = default_launch_draft(
        _opportunity(), engine.settings.pump_launch_initial_buy_sol
    )

    result = await engine.launch_lab_draft(draft, requested_by="admin")

    assert result.status == "INSUFFICIENT_SOL"
    engine.pump_launcher.j7.launch.assert_not_awaited()
    engine.database.reserve_pump_launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_launch_lab_uses_j7_and_success_contains_real_provider_result(settings) -> None:
    engine = _engine_for_launch(settings)
    draft = default_launch_draft(
        _opportunity(), engine.settings.pump_launch_initial_buy_sol
    )

    result = await engine.launch_lab_draft(draft, requested_by="admin")

    assert result.success is True
    assert result.mint == PUBLIC_WALLET
    assert result.provider == "J7 Tracker"
    engine.pump_launcher.j7.launch.assert_awaited_once()
    engine.pump_launcher.pump.launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_and_double_click_launch_only_once(settings) -> None:
    engine = _engine_for_launch(settings)
    engine.database.reserve_pump_launch.side_effect = [True, False]
    draft = default_launch_draft(
        _opportunity(), engine.settings.pump_launch_initial_buy_sol
    )

    first = await engine.launch_lab_draft(draft, requested_by="admin")
    second = await engine.launch_lab_draft(draft, requested_by="admin")

    assert first.success is True
    assert second.status == "DUPLICATE"
    assert engine.pump_launcher.j7.launch.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage",
    [(3, Decimal("0.03")), (2, Decimal("0.05"))],
)
async def test_launch_lab_keeps_daily_count_and_sol_caps(settings, usage) -> None:
    engine = _engine_for_launch(settings)
    engine.database.pump_launch_daily_usage.return_value = usage
    draft = default_launch_draft(
        _opportunity(), engine.settings.pump_launch_initial_buy_sol
    )

    result = await engine.launch_lab_draft(draft, requested_by="admin")

    assert result.status == "DAILY_LIMIT"
    engine.pump_launcher.j7.launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_result_is_persisted_and_not_blindly_retried(settings) -> None:
    engine = _engine_for_launch(settings)
    engine.pump_launcher.j7.launch.side_effect = UnknownLaunchResultError(
        "UNKNOWN SUBMISSION STATE"
    )
    draft = default_launch_draft(
        _opportunity(), engine.settings.pump_launch_initial_buy_sol
    )

    result = await engine.launch_lab_draft(draft, requested_by="admin")

    assert result.status == "UNKNOWN_RESULT"
    engine.database.mark_pump_launch_unknown.assert_awaited_once()
    engine.database.fail_pump_launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_pinata_failure_is_sanitized(settings) -> None:
    client = J7LaunchClient(_configured(settings), SimpleNamespace())
    client._session = FakeSession(post_response=FakeResponse(500, {"secret": "do not show"}))

    with pytest.raises(PumpLaunchError, match="PINATA UPLOAD FAILED") as caught:
        await client._pin_file(filename="art.png", content=b"png", content_type="image/png")

    assert "do not show" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"type": "token_create_success"}, "MISSING MINT"),
        (["malformed"], "MALFORMED"),
    ],
)
async def test_j7_malformed_or_missing_mint_is_unknown(settings, body, expected) -> None:
    client = J7LaunchClient(_configured(settings), SimpleNamespace())
    client._session = FakeSession(post_response=FakeResponse(200, body))
    client._pin_file = AsyncMock(return_value="cid")
    client.render_draft_art = AsyncMock(return_value=b"png")
    draft = default_launch_draft(
        _opportunity(), client.settings.pump_launch_initial_buy_sol
    )

    with pytest.raises(UnknownLaunchResultError, match=expected):
        await client.launch(draft.opportunity, draft=draft, allow_launch_lab=True)


@pytest.mark.asyncio
async def test_j7_timeout_after_submit_is_unknown(settings) -> None:
    client = J7LaunchClient(_configured(settings), SimpleNamespace())

    class TimeoutSession(FakeSession):
        def post(self, url, **kwargs):
            raise TimeoutError

    client._session = TimeoutSession()
    client._pin_file = AsyncMock(return_value="cid")
    client.render_draft_art = AsyncMock(return_value=b"png")
    draft = default_launch_draft(
        _opportunity(), client.settings.pump_launch_initial_buy_sol
    )

    with pytest.raises(UnknownLaunchResultError, match="UNKNOWN SUBMISSION STATE"):
        await client.launch(draft.opportunity, draft=draft, allow_launch_lab=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "SESSION EXPIRED"),
        (403, "AUTH FAILED"),
        (429, "RATE LIMITED"),
        (500, "SERVER ERROR"),
    ],
)
async def test_j7_submit_http_failures_are_actionable(settings, status, expected) -> None:
    client = J7LaunchClient(_configured(settings), SimpleNamespace())
    client._session = FakeSession(post_response=FakeResponse(status, {"secret": "hidden"}))
    client._pin_file = AsyncMock(return_value="cid")
    client.render_draft_art = AsyncMock(return_value=b"png")
    draft = default_launch_draft(
        _opportunity(), client.settings.pump_launch_initial_buy_sol
    )

    with pytest.raises(PumpLaunchError, match=expected) as caught:
        await client.launch(draft.opportunity, draft=draft, allow_launch_lab=True)

    assert "hidden" not in str(caught.value)


@pytest.mark.asyncio
async def test_successful_j7_response_supplies_external_mint_and_signature(settings) -> None:
    body = {
        "type": "token_create_success",
        "mint_address": PUBLIC_WALLET,
        "signature": "provider-signature",
    }
    client = J7LaunchClient(_configured(settings), SimpleNamespace())
    client._session = FakeSession(post_response=FakeResponse(200, body))
    client._pin_file = AsyncMock(return_value="cid")
    client.render_draft_art = AsyncMock(return_value=b"png")
    draft = default_launch_draft(
        _opportunity(), client.settings.pump_launch_initial_buy_sol
    )

    result = await client.launch(draft.opportunity, draft=draft, allow_launch_lab=True)

    assert result.mint == PUBLIC_WALLET
    assert result.signature == "provider-signature"
    assert result.provider == "J7 Tracker"


@pytest.mark.asyncio
async def test_existing_contract_cannot_enter_launch_lab_or_create_duplicate(settings) -> None:
    opportunity = _opportunity()
    opportunity = replace(
        opportunity,
        alert=replace(opportunity.alert, token_mints=(PUBLIC_WALLET,)),
    )
    engine = _engine_for_launch(settings)
    draft = default_launch_draft(
        opportunity, engine.settings.pump_launch_initial_buy_sol
    )

    result = await engine.launch_lab_draft(draft, requested_by="admin")

    assert result.status == "BLOCKED"
    engine.pump_launcher.j7.launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_edit_regenerate_next_and_cancel_do_not_launch(settings) -> None:
    opportunities = (_opportunity(), _opportunity(int(time.time()) - 1))
    j7 = SimpleNamespace(
        wallet_address=PUBLIC_WALLET,
        render_draft_art=AsyncMock(return_value=b"png"),
        launch=AsyncMock(),
    )
    bot = SimpleNamespace(
        settings=_configured(settings),
        engine=SimpleNamespace(pump_launcher=SimpleNamespace(j7=j7)),
    )
    view = LaunchLabView(bot, opportunities, owner_id=1, balance=Decimal("0.03"))

    await view.preview()
    view.drafts[0] = validate_launch_draft(
        replace(view.draft, name="Edited Coin", art_variant=1),
        maximum_buy_sol=bot.settings.pump_launch_initial_buy_sol,
    )
    view.index = 1
    view.stop()

    j7.launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_unauthorized_user_cannot_use_launch_lab(settings) -> None:
    launch = AsyncMock()
    bot = SimpleNamespace(
        settings=_configured(settings),
        engine=SimpleNamespace(
            pump_launcher=SimpleNamespace(
                j7=SimpleNamespace(wallet_address=PUBLIC_WALLET, launch=launch)
            )
        ),
    )
    view = LaunchLabView(bot, (_opportunity(),), owner_id=1, balance=Decimal("0.03"))
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=2),
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    assert _member_is_admin(interaction.user, bot.settings) is False
    assert await view.interaction_check(interaction) is False
    launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_final_confirmation_calls_launch(settings) -> None:
    result = PumpLaunchResult(
        success=True,
        status="SUBMITTED",
        message="created",
        alert_key="key",
        name="Kitchen Moment",
        symbol="KM",
        mint=PUBLIC_WALLET,
        signature="signature",
        provider="J7 Tracker",
    )
    launch = AsyncMock(return_value=result)
    bot = SimpleNamespace(
        settings=_configured(settings),
        engine=SimpleNamespace(
            launch_lab_draft=launch,
            pump_launcher=SimpleNamespace(
                j7=SimpleNamespace(wallet_address=PUBLIC_WALLET)
            ),
        ),
        _send_alert=AsyncMock(),
    )
    lab = LaunchLabView(bot, (_opportunity(),), owner_id=1, balance=Decimal("0.03"))
    confirmation = LaunchConfirmationView(lab)
    button = confirmation.children[0]
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1),
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_response=AsyncMock(),
        message=SimpleNamespace(edit=AsyncMock()),
    )

    await button.callback(interaction)

    launch.assert_awaited_once()
    assert interaction.edit_original_response.await_count == 2
    interaction.message.edit.assert_not_awaited()


def test_success_links_are_exact_pump_fomo_and_solscan() -> None:
    result = PumpLaunchResult(
        success=True,
        status="SUBMITTED",
        message="created",
        alert_key="key",
        name="Kitchen Moment",
        symbol="KM",
        mint=PUBLIC_WALLET,
        signature="real-signature",
        provider="J7 Tracker",
    )

    view = _launch_result_view(result, "referral")
    assert view is not None
    links = {item.label: item.url for item in view.children}
    assert links["OPEN PUMP.FUN"] == f"https://pump.fun/coin/{PUBLIC_WALLET}"
    assert PUBLIC_WALLET in links["OPEN FOMO"]
    assert links["SOLSCAN"] == "https://solscan.io/tx/real-signature"


def test_automatic_no_x_and_x_verified_lanes_remain_intact() -> None:
    no_x = _opportunity()
    x_ready = _opportunity(x_verified=True)

    assert no_x.verdict == NO_X_LAUNCH_VERDICT
    assert no_x.score >= 78
    assert x_ready.verdict == X_VERIFIED_LAUNCH_VERDICT


@pytest.mark.asyncio
async def test_unknown_database_result_blocks_duplicate_retry(tmp_path) -> None:
    database = Database(str(tmp_path / "unknown.db"), Decimal("1000"))
    await database.connect()
    try:
        reserved = await database.reserve_pump_launch(
            alert_key="narrative",
            source_url="https://example.com/story",
            headline="story",
            name="Coin",
            symbol="COIN",
            score=74,
            initial_buy_sol=Decimal("0.01"),
            requested_by="admin",
        )
        await database.mark_pump_launch_unknown("narrative", "timeout")
        retry = await database.reserve_pump_launch(
            alert_key="narrative",
            source_url="https://example.com/story",
            headline="story",
            name="Coin",
            symbol="COIN",
            score=74,
            initial_buy_sol=Decimal("0.01"),
            requested_by="admin",
        )
        rows = await database.recent_pump_launches()
    finally:
        await database.close()

    assert reserved is True
    assert retry is False
    assert rows[0]["status"] == "UNKNOWN_RESULT"
    assert launch_draft_key(default_launch_draft(_opportunity(), Decimal("0.01")))


@pytest.mark.asyncio
async def test_v230_launch_history_migrates_without_reset(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE pump_launches (
                alert_key TEXT PRIMARY KEY, source_url TEXT NOT NULL,
                headline TEXT NOT NULL, name TEXT NOT NULL, symbol TEXT NOT NULL,
                score INTEGER NOT NULL, initial_buy_sol REAL NOT NULL,
                requested_by TEXT NOT NULL, status TEXT NOT NULL CHECK (
                    status IN ('RESERVED', 'SUBMITTED', 'CONFIRMED', 'FAILED')
                ), mint TEXT, signature TEXT, metadata_uri TEXT, error TEXT,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO pump_launches VALUES (
                'old-key', 'https://example.com', 'old story', 'Old Coin', 'OLD',
                80, 0.01, 'admin', 'CONFIRMED', ?, 'sig', 'ipfs', NULL, 1, 1
            )
            """,
            (PUBLIC_WALLET,),
        )
    database = Database(str(path), Decimal("1000"))
    await database.connect()
    try:
        rows = await database.recent_pump_launches()
        await database.mark_pump_launch_unknown("old-key", "reconcile")
        updated = await database.recent_pump_launches()
    finally:
        await database.close()

    assert rows[0]["mint"] == PUBLIC_WALLET
    assert updated[0]["status"] == "UNKNOWN_RESULT"


@pytest.mark.asyncio
async def test_launch_lab_reads_persistent_recent_candidate_pool(settings, tmp_path) -> None:
    configured = _configured(settings)
    engine = SmartMoneyEngine(configured)
    engine.database = Database(str(tmp_path / "pool.db"), Decimal("1000"))
    await engine.database.connect()
    engine._launch_lab_lock = asyncio.Lock()
    engine.news_poller.snapshot = AsyncMock(return_value=())
    opportunity = _opportunity()
    await engine._cache_launch_candidate(opportunity, now=int(time.time()))
    try:
        candidates = await engine.launch_lab_candidates()
    finally:
        await engine.database.close()
        await engine.pump_launcher.close()

    assert len(candidates) == 1
    assert candidates[0].primary_narrative == opportunity.primary_narrative


@pytest.mark.asyncio
async def test_launch_lab_test_displays_real_below_floor_rss_item(settings) -> None:
    engine = SmartMoneyEngine(_configured(settings))
    await engine.database.connect()
    now = int(time.time())
    alert = NewsAlert(
        source="Associated Press",
        headline="Museum reveals a new Moon Mascot for its summer exhibit",
        summary="The public unveiling begins this weekend.",
        url="https://apnews.com/article/moon-mascot",
        narrative_terms=("Moon Mascot",),
        created_at=now - 45,
        received_at=now,
    )
    engine.news_poller.snapshot = AsyncMock(return_value=(alert,))
    engine.news_matcher.competition = AsyncMock(
        return_value=NarrativeCompetition(query="Moon Mascot")
    )
    try:
        candidates = await engine.launch_lab_test_candidates()
    finally:
        await engine.close()

    assert len(candidates) == 1
    assert candidates[0].alert.headline == alert.headline
    assert candidates[0].alert.url == alert.url
    assert candidates[0].score < settings.launch_lab_min_score
    assert settings.launch_lab_min_score == 60
    assert settings.news_x_verify_min_score == 70
    assert settings.pump_launch_min_score == 72
    assert settings.no_x_launch_min_score == 78


@pytest.mark.asyncio
async def test_launch_lab_test_topic_uses_real_public_rss_fallback(settings) -> None:
    engine = SmartMoneyEngine(_configured(settings))
    await engine.database.connect()
    now = int(time.time())
    alert = NewsAlert(
        source="Google News",
        headline="Public report covers the Moon Mascot reveal",
        summary="A current public article about the requested topic.",
        url="https://news.google.com/rss/articles/real-item",
        narrative_terms=("Moon Mascot",),
        created_at=now - 90,
        received_at=now,
    )
    engine.news_poller.snapshot = AsyncMock(return_value=())
    engine.news_poller.topic_snapshot = AsyncMock(return_value=(alert,))
    engine.news_matcher.competition = AsyncMock(
        return_value=NarrativeCompetition(query="Moon Mascot")
    )
    try:
        candidates = await engine.launch_lab_test_candidates(topic="Moon Mascot")
    finally:
        await engine.close()

    engine.news_poller.topic_snapshot.assert_awaited_once_with(
        "Moon Mascot",
        max_age_seconds=21_600,
    )
    assert candidates[0].alert.url == alert.url


@pytest.mark.asyncio
async def test_research_candidate_hard_locks_j7_and_launch_reservation(settings) -> None:
    launch = AsyncMock()
    reserve = AsyncMock()
    bot = SimpleNamespace(
        settings=_configured(settings),
        engine=SimpleNamespace(
            launch_lab_draft=launch,
            database=SimpleNamespace(reserve_pump_launch=reserve),
            pump_launcher=SimpleNamespace(
                j7=SimpleNamespace(wallet_address=PUBLIC_WALLET, launch=AsyncMock())
            ),
        ),
    )
    view = LaunchLabView(
        bot,
        (_below_floor_opportunity(),),
        owner_id=1,
        balance=Decimal("0.03"),
        research_test=True,
    )
    button = next(item for item in view.children if item.label.startswith("J7 LAUNCH LOCKED"))
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1),
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    await button.callback(interaction)

    assert button.disabled is True
    launch.assert_not_awaited()
    reserve.assert_not_awaited()
    bot.engine.pump_launcher.j7.launch.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_launch_lab_test_command_is_admin_only(settings) -> None:
    engine = SimpleNamespace(
        launch_lab_test_candidates=AsyncMock(),
        launch_lab_candidates=AsyncMock(),
    )
    commands = SmartMoneyCommands(SimpleNamespace(settings=_configured(settings), engine=engine))
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=999),
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    await SmartMoneyCommands.launch_lab.callback(
        commands,
        interaction,
        mode="test",
        topic="",
    )

    engine.launch_lab_test_candidates.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once()


def test_real_qualifying_test_candidate_unlocks_normal_j7_control(settings) -> None:
    bot = SimpleNamespace(
        settings=_configured(settings),
        engine=SimpleNamespace(
            pump_launcher=SimpleNamespace(
                j7=SimpleNamespace(wallet_address=PUBLIC_WALLET, launch=AsyncMock())
            )
        ),
    )
    view = LaunchLabView(
        bot,
        (_opportunity(),),
        owner_id=1,
        balance=Decimal("0.03"),
        research_test=True,
    )

    launch_button = next(item for item in view.children if item.label == "LAUNCH VIA J7")
    assert view.production_eligible is True
    assert launch_button.disabled is False


@pytest.mark.asyncio
async def test_research_edit_art_next_and_cancel_controls_never_launch(settings) -> None:
    launch = AsyncMock()
    render = AsyncMock(
        side_effect=lambda draft: (
            f"{draft.opportunity.alert.headline}:{draft.art_variant}".encode()
        )
    )
    bot = SimpleNamespace(
        settings=_configured(settings),
        engine=SimpleNamespace(
            launch_lab_draft=launch,
            pump_launcher=SimpleNamespace(
                j7=SimpleNamespace(
                    wallet_address=PUBLIC_WALLET,
                    render_draft_art=render,
                    launch=AsyncMock(),
                )
            ),
        ),
    )
    first = _below_floor_opportunity()
    second = replace(
        _below_floor_opportunity(int(time.time()) - 1),
        alert=replace(
            _below_floor_opportunity(int(time.time()) - 1).alert,
            headline="Real second current story",
            source="Reuters",
            url="https://reuters.com/second-current-story",
        ),
        coin_name="Second Story",
        coin_symbol="SECOND",
        score=43,
    )
    view = LaunchLabView(
        bot,
        (first, second),
        owner_id=1,
        balance=Decimal("0.03"),
        research_test=True,
    )
    response = SimpleNamespace(
        send_modal=AsyncMock(),
        defer=AsyncMock(),
        edit_message=AsyncMock(),
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1),
        response=response,
        edit_original_response=AsyncMock(),
        message=SimpleNamespace(edit=AsyncMock()),
    )
    edit = next(item for item in view.children if item.label == "EDIT")
    regenerate = next(item for item in view.children if item.label == "REGENERATE ART")
    next_candidate = next(item for item in view.children if item.label == "NEXT CANDIDATE")
    cancel = next(item for item in view.children if item.label == "CANCEL")

    await edit.callback(interaction)
    await regenerate.callback(interaction)
    regenerated_call = interaction.edit_original_response.await_args_list[-1]
    regenerated_file = regenerated_call.kwargs["attachments"][0]
    assert regenerated_file.fp.getvalue().endswith(b":1")
    await next_candidate.callback(interaction)
    next_call = interaction.edit_original_response.await_args_list[-1]
    next_embed = next_call.kwargs["embed"]
    next_file = next_call.kwargs["attachments"][0]
    await cancel.callback(interaction)

    assert view.drafts[0].art_variant == 1
    assert view.index == 1
    assert render.await_count == 2
    assert "Candidate `2/2`" in (next_embed.description or "")
    assert "Real second current story" in (next_embed.description or "")
    assert next_file.fp.getvalue().startswith(b"Real second current story")
    assert interaction.edit_original_response.await_count == 2
    interaction.message.edit.assert_not_awaited()
    launch.assert_not_awaited()
    bot.engine.pump_launcher.j7.launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_launch_lab_edit_modal_updates_ephemeral_original_response(settings) -> None:
    render = AsyncMock(return_value=b"edited-art")
    launch = AsyncMock()
    bot = SimpleNamespace(
        settings=_configured(settings),
        engine=SimpleNamespace(
            pump_launcher=SimpleNamespace(
                j7=SimpleNamespace(
                    wallet_address=PUBLIC_WALLET,
                    render_draft_art=render,
                    launch=launch,
                )
            )
        ),
    )
    view = LaunchLabView(
        bot,
        (_below_floor_opportunity(),),
        owner_id=1,
        balance=Decimal("0.03"),
        research_test=True,
    )
    modal = LaunchLabEditModal(view)
    modal.name_input._value = "Edited Real Story"
    modal.symbol_input._value = "EDIT"
    modal.description_input._value = "Edited research description"
    modal.buy_input._value = "0.01"
    modal.links_input._value = "https://example.com/source"
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
        message=SimpleNamespace(edit=AsyncMock()),
    )

    await modal.on_submit(interaction)

    assert view.draft.name == "Edited Real Story"
    assert view.draft.symbol == "EDIT"
    interaction.edit_original_response.assert_awaited_once()
    interaction.message.edit.assert_not_awaited()
    launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_fallback_art_regeneration_is_a_real_distinct_1024_png(settings) -> None:
    client = J7LaunchClient(_configured(settings), SimpleNamespace())
    opportunity = replace(
        _below_floor_opportunity(),
        alert=replace(_below_floor_opportunity().alert, image_urls=()),
    )
    first_draft = default_launch_draft(opportunity, Decimal("0.01"))
    try:
        first = await client.render_draft_art(first_draft)
        second = await client.render_draft_art(replace(first_draft, art_variant=1))
    finally:
        await client.close()

    assert first != second
    with Image.open(io.BytesIO(first)) as first_image:
        assert first_image.size == (1024, 1024)
        assert first_image.format == "PNG"
    with Image.open(io.BytesIO(second)) as second_image:
        assert second_image.size == (1024, 1024)
        assert second_image.format == "PNG"
