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
        )


class SolanaTrackerClient:
    """Authorized 24-hour Solana wallet discovery through Solana Tracker PnL V2."""

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
        params = {
            "days": "1",
            "sort": "realized",
            "direction": "desc",
            "limit": str(policy.fetch_limit),
            "excludeArbitrage": "true",
            "pnlMode": "strict",
            "minDays": "1",
            "minTrades": str(policy.minimum_trades),
            "minInvested": "1",
            "minWinRate": str(policy.minimum_win_rate_percent),
            "minRoi": str(policy.minimum_roi_percent),
            "minClosedTokens": str(policy.minimum_closed_tokens),
            "maxSingleTokenPct": str(policy.maximum_single_token_percent),
        }
        payload = await self._request("/v2/pnl/leaderboard/top", params=params)
        return parse_candidates(payload, policy)

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
            except (self._aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
                    continue
                raise DiscoveryError(f"Solana Tracker request failed: {exc}") from exc
        raise DiscoveryError("Solana Tracker request exhausted retries")


def parse_candidates(payload: Any, policy: DiscoveryPolicy) -> list[DiscoveryCandidate]:
    if not isinstance(payload, dict) or not isinstance(payload.get("traders"), list):
        raise DiscoveryError("Solana Tracker leaderboard response has an unexpected shape")

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
    candidates: list[DiscoveryCandidate] = []
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
            pnl < policy.minimum_pnl_usd
            or roi < policy.minimum_roi_percent
            or win_rate < policy.minimum_win_rate_percent
            or trades < policy.minimum_trades
            or trades > policy.maximum_trades
            or closed_tokens < policy.minimum_closed_tokens
        ):
            continue

        tags = _tags(row, identity)
        if tags & blocked_tags:
            continue

        alias = _identity_alias(identity, wallet)
        score = score_candidate(
            pnl=pnl,
            roi_percent=roi,
            win_rate_percent=win_rate,
            trades=trades,
            closed_tokens=closed_tokens,
            minimum_pnl=policy.minimum_pnl_usd,
        )
        candidates.append(
            DiscoveryCandidate(
                address=wallet,
                alias=alias,
                realized_pnl_24h=pnl,
                previous_pnl_24h=None,
                roi_24h_percent=roi,
                win_rate_percent=win_rate,
                trades_24h=trades,
                buys_24h=_integer(counts.get("buys")),
                sells_24h=_integer(counts.get("sells")),
                closed_tokens=closed_tokens,
                invested_24h_usd=_decimal(row.get("invested")),
                volume_24h_usd=_decimal(period.get("volume")),
                last_trade_ms=_optional_integer(timing.get("lastTrade")),
                score=score,
                rank=0,
            )
        )

    candidates.sort(
        key=lambda item: (
            item.score,
            item.realized_pnl_24h,
            item.win_rate_percent,
            item.trades_24h,
        ),
        reverse=True,
    )
    return [
        replace(candidate, rank=index)
        for index, candidate in enumerate(candidates[: policy.max_wallets], start=1)
    ]


def score_candidate(
    *,
    pnl: Decimal,
    roi_percent: Decimal,
    win_rate_percent: Decimal,
    trades: int,
    closed_tokens: int,
    minimum_pnl: Decimal,
) -> Decimal:
    """Risk-adjusted bootstrap score used until local history becomes reliable."""

    pnl_denominator = max(minimum_pnl * Decimal("10"), Decimal("1"))
    profit_component = _clamp(pnl / pnl_denominator, Decimal("0"), Decimal("1")) * 15
    win_component = _clamp(win_rate_percent / Decimal("100"), Decimal("0"), Decimal("1")) * 35
    roi_component = _clamp(roi_percent / Decimal("50"), Decimal("0"), Decimal("1")) * 20
    activity_component = _clamp(Decimal(trades) / Decimal("20"), Decimal("0"), Decimal("1")) * 15
    diversity_component = (
        _clamp(Decimal(closed_tokens) / Decimal("10"), Decimal("0"), Decimal("1")) * 15
    )
    return _clamp(
        profit_component
        + win_component
        + roi_component
        + activity_component
        + diversity_component,
        Decimal("0"),
        Decimal("100"),
    ).quantize(Decimal("0.01"))


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
