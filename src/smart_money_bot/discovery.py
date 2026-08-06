from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any

from .config import Settings
from .errors import DiscoveryError
from .models import DiscoveryCandidate

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DiscoveryPolicy:
    fetch_limit: int
    max_wallets: int
    minimum_pnl_usd: Decimal
    minimum_win_rate_percent: Decimal
    minimum_roi_percent: Decimal
    minimum_trades: int
    maximum_trades: int
    minimum_closed_tokens: int
    maximum_single_token_percent: Decimal
    minimum_7d_pnl_usd: Decimal = Decimal("300")
    minimum_7d_win_rate_percent: Decimal = Decimal("55")
    minimum_7d_roi_percent: Decimal = Decimal("5")
    minimum_7d_trades: int = 10
    maximum_7d_trades: int = 1000

    @classmethod
    def from_settings(cls, settings: Settings) -> DiscoveryPolicy:
        return cls(
            fetch_limit=settings.discovery_fetch_limit,
            max_wallets=settings.discovery_max_wallets,
            minimum_pnl_usd=settings.discovery_min_24h_pnl_usd,
            minimum_win_rate_percent=settings.discovery_min_win_rate_percent,
            minimum_roi_percent=settings.discovery_min_roi_percent,
            minimum_trades=settings.discovery_min_trades,
            maximum_trades=settings.discovery_max_trades,
            minimum_closed_tokens=settings.discovery_min_closed_tokens,
            maximum_single_token_percent=settings.discovery_max_single_token_percent,
            minimum_7d_pnl_usd=settings.discovery_min_7d_pnl_usd,
            minimum_7d_win_rate_percent=settings.discovery_min_7d_win_rate_percent,
            minimum_7d_roi_percent=settings.discovery_min_7d_roi_percent,
            minimum_7d_trades=settings.discovery_min_7d_trades,
            maximum_7d_trades=settings.discovery_max_7d_trades,
        )


@dataclass(frozen=True, slots=True)
class WindowCandidate:
    address: str
    alias: str
    realized_pnl_usd: Decimal
    roi_percent: Decimal
    win_rate_percent: Decimal
    trades: int
    buys: int
    sells: int
    closed_tokens: int
    invested_usd: Decimal
    volume_usd: Decimal
    last_trade_ms: int | None


class SolanaTrackerClient:
    """Authorized multi-window Solana discovery through Solana Tracker PnL V2."""

    BASE_URL = "https://data.solanatracker.io"

    def __init__(self, api_key: str, *, timeout_seconds: int = 30) -> None:
        if not api_key.strip():
            raise ValueError("Solana Tracker API key cannot be blank")
        try:
            import aiohttp
        except ImportError as exc:  # pragma: no cover - production dependency
            raise DiscoveryError("aiohttp is required for automatic discovery") from exc
        self._aiohttp = aiohttp
        self.api_key = api_key.strip()
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: Any = None

    async def _get_session(self) -> Any:
        if self._session is None or self._session.closed:
            self._session = self._aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def top_24h(self, policy: DiscoveryPolicy) -> list[DiscoveryCandidate]:
        """Backward-compatible daily-only discovery used by older integrations/tests."""

        payload = await self._leaderboard_payload(policy, days=1)
        return parse_candidates(payload, policy)

    async def daily_pool(self, policy: DiscoveryPolicy) -> list[WindowCandidate]:
        payload = await self._leaderboard_payload(policy, days=1)
        return parse_window_candidates(payload, policy, days=1)

    async def weekly_pool(self, policy: DiscoveryPolicy) -> list[WindowCandidate]:
        payload = await self._leaderboard_payload(policy, days=7)
        return parse_window_candidates(payload, policy, days=7)

    async def _leaderboard_payload(self, policy: DiscoveryPolicy, *, days: int) -> Any:
        weekly = days == 7
        params = {
            "days": str(days),
            "sort": "realized",
            "direction": "desc",
            "limit": str(policy.fetch_limit),
            "excludeArbitrage": "true",
            "pnlMode": "strict",
            "minDays": "2" if weekly else "1",
            "minTrades": str(
                policy.minimum_7d_trades if weekly else policy.minimum_trades
            ),
            "minInvested": "1",
            "minWinRate": str(
                policy.minimum_7d_win_rate_percent
                if weekly
                else policy.minimum_win_rate_percent
            ),
            "minRoi": str(
                policy.minimum_7d_roi_percent if weekly else policy.minimum_roi_percent
            ),
            "minClosedTokens": str(policy.minimum_closed_tokens),
            "maxSingleTokenPct": str(policy.maximum_single_token_percent),
        }
        return await self._request("/v2/pnl/leaderboard/top", params=params)

    async def _request(self, path: str, *, params: dict[str, str]) -> Any:
        session = await self._get_session()
        url = f"{self.BASE_URL}{path}"
        headers = {"x-api-key": self.api_key}
        for attempt in range(3):
            try:
                async with session.get(url, params=params, headers=headers) as response:
                    if (response.status == 429 or response.status >= 500) and attempt < 2:
                        await asyncio.sleep(2**attempt)
                        continue
                    body = await response.text()
                    if response.status >= 400:
                        raise DiscoveryError(
                            f"Solana Tracker HTTP {response.status}: {body[:500]}"
                        )
                    try:
                        return await response.json(content_type=None)
                    except ValueError as exc:
                        raise DiscoveryError("Solana Tracker returned invalid JSON") from exc
            except (TimeoutError, self._aiohttp.ClientError) as exc:
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
                    continue
                raise DiscoveryError(f"Solana Tracker request failed: {exc}") from exc
        raise DiscoveryError("Solana Tracker request exhausted retries")


def parse_candidates(payload: Any, policy: DiscoveryPolicy) -> list[DiscoveryCandidate]:
    """Parse the strict daily feed without claiming seven-day verification."""

    rows = parse_window_candidates(payload, policy, days=1)
    candidates = [
        DiscoveryCandidate(
            address=row.address,
            alias=row.alias,
            realized_pnl_24h=row.realized_pnl_usd,
            previous_pnl_24h=None,
            roi_24h_percent=row.roi_percent,
            win_rate_percent=row.win_rate_percent,
            trades_24h=row.trades,
            buys_24h=row.buys,
            sells_24h=row.sells,
            closed_tokens=row.closed_tokens,
            invested_24h_usd=row.invested_usd,
            volume_24h_usd=row.volume_usd,
            last_trade_ms=row.last_trade_ms,
            score=score_candidate(
                pnl=row.realized_pnl_usd,
                roi_percent=row.roi_percent,
                win_rate_percent=row.win_rate_percent,
                trades=row.trades,
                closed_tokens=row.closed_tokens,
                minimum_pnl=policy.minimum_pnl_usd,
            ),
            rank=0,
        )
        for row in rows
    ]
    candidates.sort(key=_candidate_sort_key, reverse=True)
    return [
        replace(candidate, rank=index)
        for index, candidate in enumerate(candidates[: policy.max_wallets], start=1)
    ]


def parse_window_candidates(
    payload: Any, policy: DiscoveryPolicy, *, days: int
) -> list[WindowCandidate]:
    if not isinstance(payload, dict) or not isinstance(payload.get("traders"), list):
        raise DiscoveryError("Solana Tracker leaderboard response has an unexpected shape")
    if days not in {1, 7}:
        raise ValueError("Only 1-day and 7-day discovery windows are supported")

    blocked_tags = {
        "arbitrage",
        "sandwich",
        "mev",
        "exchange",
        "pool",
        "program",
        "hacker",
        "drainer",
    }
    if days == 7:
        min_pnl = policy.minimum_7d_pnl_usd
        min_roi = policy.minimum_7d_roi_percent
        min_win = policy.minimum_7d_win_rate_percent
        min_trades = policy.minimum_7d_trades
        max_trades = policy.maximum_7d_trades
    else:
        min_pnl = policy.minimum_pnl_usd
        min_roi = policy.minimum_roi_percent
        min_win = policy.minimum_win_rate_percent
        min_trades = policy.minimum_trades
        max_trades = policy.maximum_trades

    candidates: list[WindowCandidate] = []
    for row in payload["traders"]:
        if not isinstance(row, dict):
            continue
        wallet = str(row.get("wallet") or "").strip()
        try:
            wallet = _normalize_wallet(wallet)
        except ValueError:
            continue

        period = row.get("period") if isinstance(row.get("period"), dict) else {}
        counts = row.get("counts") if isinstance(row.get("counts"), dict) else {}
        tokens = row.get("tokens") if isinstance(row.get("tokens"), dict) else {}
        timing = row.get("timing") if isinstance(row.get("timing"), dict) else {}
        identity = row.get("identity") if isinstance(row.get("identity"), dict) else {}

        pnl = _decimal(period.get("realized"))
        roi = _decimal(period.get("roi"))
        win_rate = _decimal(row.get("winRate"))
        trades = _integer(counts.get("trades"))
        closed_tokens = _integer(tokens.get("closed"))
        if (
            pnl < min_pnl
            or roi < min_roi
            or win_rate < min_win
            or trades < min_trades
            or trades > max_trades
            or closed_tokens < policy.minimum_closed_tokens
        ):
            continue
        if _tags(row, identity) & blocked_tags:
            continue

        candidates.append(
            WindowCandidate(
                address=wallet,
                alias=_identity_alias(identity, wallet),
                realized_pnl_usd=pnl,
                roi_percent=roi,
                win_rate_percent=win_rate,
                trades=trades,
                buys=_integer(counts.get("buys")),
                sells=_integer(counts.get("sells")),
                closed_tokens=closed_tokens,
                invested_usd=_decimal(row.get("invested")),
                volume_usd=_decimal(period.get("volume")),
                last_trade_ms=_optional_integer(timing.get("lastTrade")),
            )
        )
    return candidates[: policy.fetch_limit]


def merge_verified_windows(
    daily: list[WindowCandidate],
    weekly: list[WindowCandidate],
    policy: DiscoveryPolicy,
) -> list[DiscoveryCandidate]:
    """Return only wallets independently profitable in both strict windows."""

    weekly_by_wallet = {candidate.address: candidate for candidate in weekly}
    merged: list[DiscoveryCandidate] = []
    for day in daily:
        week = weekly_by_wallet.get(day.address)
        if week is None:
            continue
        score = score_candidate(
            pnl=day.realized_pnl_usd,
            roi_percent=day.roi_percent,
            win_rate_percent=day.win_rate_percent,
            trades=day.trades,
            closed_tokens=day.closed_tokens,
            minimum_pnl=policy.minimum_pnl_usd,
            pnl_7d=week.realized_pnl_usd,
            roi_7d_percent=week.roi_percent,
            win_rate_7d_percent=week.win_rate_percent,
            trades_7d=week.trades,
            minimum_pnl_7d=policy.minimum_7d_pnl_usd,
        )
        merged.append(
            DiscoveryCandidate(
                address=day.address,
                alias=day.alias,
                realized_pnl_24h=day.realized_pnl_usd,
                previous_pnl_24h=None,
                roi_24h_percent=day.roi_percent,
                win_rate_percent=day.win_rate_percent,
                trades_24h=day.trades,
                buys_24h=day.buys,
                sells_24h=day.sells,
                closed_tokens=day.closed_tokens,
                invested_24h_usd=day.invested_usd,
                volume_24h_usd=day.volume_usd,
                last_trade_ms=max(
                    value
                    for value in (day.last_trade_ms, week.last_trade_ms, 0)
                )
                or None,
                score=score,
                rank=0,
                realized_pnl_7d=week.realized_pnl_usd,
                roi_7d_percent=week.roi_percent,
                win_rate_7d_percent=week.win_rate_percent,
                trades_7d=week.trades,
                selection_reason=(
                    f"strict 24H + 7D profit verified; 24H ${day.realized_pnl_usd:,.2f}, "
                    f"7D ${week.realized_pnl_usd:,.2f}"
                ),
            )
        )

    merged.sort(key=_candidate_sort_key, reverse=True)
    return [
        replace(candidate, rank=index)
        for index, candidate in enumerate(merged[: policy.fetch_limit], start=1)
    ]


def score_candidate(
    *,
    pnl: Decimal,
    roi_percent: Decimal,
    win_rate_percent: Decimal,
    trades: int,
    closed_tokens: int,
    minimum_pnl: Decimal,
    pnl_7d: Decimal | None = None,
    roi_7d_percent: Decimal | None = None,
    win_rate_7d_percent: Decimal | None = None,
    trades_7d: int | None = None,
    minimum_pnl_7d: Decimal | None = None,
) -> Decimal:
    """Risk-adjusted score; two-window evidence receives the full weighting."""

    weekly_pnl = pnl if pnl_7d is None else pnl_7d
    weekly_roi = roi_percent if roi_7d_percent is None else roi_7d_percent
    weekly_win = win_rate_percent if win_rate_7d_percent is None else win_rate_7d_percent
    weekly_trades = trades if trades_7d is None else trades_7d
    weekly_minimum = minimum_pnl if minimum_pnl_7d is None else minimum_pnl_7d

    day_profit = _ratio_score(pnl, minimum_pnl * Decimal("10"), 10)
    week_profit = _ratio_score(weekly_pnl, weekly_minimum * Decimal("10"), 10)
    day_win = _ratio_score(win_rate_percent, Decimal("100"), 15)
    week_win = _ratio_score(weekly_win, Decimal("100"), 15)
    day_roi = _ratio_score(roi_percent, Decimal("50"), 10)
    week_roi = _ratio_score(weekly_roi, Decimal("100"), 15)
    day_activity = _ratio_score(Decimal(trades), Decimal("20"), 8)
    week_activity = _ratio_score(Decimal(weekly_trades), Decimal("100"), 7)
    diversity = _ratio_score(Decimal(closed_tokens), Decimal("10"), 8)
    consistency = Decimal("2") if pnl > 0 and weekly_pnl > 0 else Decimal("0")
    return _clamp(
        day_profit
        + week_profit
        + day_win
        + week_win
        + day_roi
        + week_roi
        + day_activity
        + week_activity
        + diversity
        + consistency,
        Decimal("0"),
        Decimal("100"),
    ).quantize(Decimal("0.01"))


def _ratio_score(value: Decimal, denominator: Decimal, weight: int) -> Decimal:
    safe_denominator = max(denominator, Decimal("1"))
    return _clamp(value / safe_denominator, Decimal("0"), Decimal("1")) * weight


def _candidate_sort_key(candidate: DiscoveryCandidate) -> tuple[Decimal, Decimal, Decimal, int]:
    return (
        candidate.score,
        candidate.realized_pnl_24h,
        candidate.realized_pnl_7d,
        candidate.trades_24h,
    )


def _identity_alias(identity: dict[str, Any], wallet: str) -> str:
    for key in ("name", "sns", "twitter"):
        value = identity.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lstrip("@")[:50]
    return f"Wallet {wallet[:5]}…{wallet[-5:]}"


def _tags(row: dict[str, Any], identity: dict[str, Any]) -> set[str]:
    raw_values: list[Any] = []
    for value in (row.get("tags"), identity.get("tags"), row.get("platforms")):
        if isinstance(value, list):
            raw_values.extend(value)
        elif isinstance(value, str):
            raw_values.extend(value.split(","))
    return {str(value).strip().lower() for value in raw_values if str(value).strip()}


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _optional_integer(value: Any) -> int | None:
    if value is None:
        return None
    number = _integer(value)
    return number if number > 0 else None


def _normalize_wallet(value: str) -> str:
    """Validate a base58-encoded 32-byte Solana public key without network calls."""

    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    if not 32 <= len(value) <= 44 or any(character not in alphabet for character in value):
        raise ValueError("invalid Solana public key")
    number = 0
    for character in value:
        number = number * 58 + alphabet.index(character)
    decoded = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_zeroes = len(value) - len(value.lstrip("1"))
    if len((b"\0" * leading_zeroes) + decoded) != 32:
        raise ValueError("invalid Solana public key")
    return value


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(low, min(high, value))
