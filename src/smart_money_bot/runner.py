from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict
from decimal import Decimal
from typing import Any

from .models import (
    CoinCallout,
    RunnerCandidate,
    RunnerMarketSnapshot,
    RunnerMomentumWindow,
    RunnerScoreBreakdown,
    XSocialSnapshot,
)

RUNNER_HORIZONS_SECONDS = (60, 300, 900, 1_800, 3_600, 14_400, 86_400)
RUNNER_MOMENTUM_WINDOWS_SECONDS = (15, 30, 60, 180, 300)


def _pct(current: Decimal | None, base: Decimal | None) -> Decimal | None:
    if current is None or base is None or base <= 0:
        return None
    return ((current / base) - Decimal("1")) * Decimal("100")


def _ratio(current: Decimal | int, base: Decimal | int) -> Decimal | None:
    denominator = Decimal(str(base))
    if denominator <= 0:
        return None
    return Decimal(str(current)) / denominator


def _window_change(current: Decimal | int, base: Decimal | int) -> Decimal | None:
    ratio = _ratio(current, base)
    return (ratio - Decimal("1")) * Decimal("100") if ratio is not None else None


def _momentum_windows(
    *,
    first: RunnerMarketSnapshot,
    current: RunnerMarketSnapshot,
    history: tuple[RunnerMarketSnapshot, ...],
) -> tuple[RunnerMomentumWindow, ...]:
    series = tuple(
        sorted(
            {item.captured_at: item for item in (first, *history)}.values(),
            key=lambda item: item.captured_at,
        )
    )
    windows: list[RunnerMomentumWindow] = []
    for seconds in RUNNER_MOMENTUM_WINDOWS_SECONDS:
        ages = [
            (current.captured_at - item.captured_at, item)
            for item in series
            if item.captured_at < current.captured_at
        ]
        eligible = [
            (age, item)
            for age, item in ages
            if seconds <= age <= seconds + max(15, seconds // 2)
        ]
        if not eligible:
            continue
        _age, baseline = min(eligible, key=lambda row: row[0])
        current_transactions = current.buys_5m + current.sells_5m
        baseline_transactions = baseline.buys_5m + baseline.sells_5m
        holder_growth = (
            current.holder_count - baseline.holder_count
            if current.holder_count is not None and baseline.holder_count is not None
            else None
        )
        windows.append(
            RunnerMomentumWindow(
                seconds=seconds,
                price_change_percent=_pct(current.price_usd, baseline.price_usd),
                market_cap_change_percent=_pct(
                    current.market_cap_usd,
                    baseline.market_cap_usd,
                ),
                rolling_volume_change_percent=_window_change(
                    current.volume_5m_usd,
                    baseline.volume_5m_usd,
                ),
                rolling_transactions_change_percent=_window_change(
                    current_transactions,
                    baseline_transactions,
                ),
                holder_growth=holder_growth,
            )
        )
    return tuple(windows)


def runner_snapshot_from_callout(
    callout: CoinCallout,
    *,
    captured_at: int,
    verified_unique_buyers: int = 0,
    largest_verified_buyer_percent: Decimal | None = None,
) -> RunnerMarketSnapshot:
    token = callout.token_info
    risk = callout.tracker_risk
    quote = callout.executable_quote
    liquidity = None
    if token and token.liquidity_usd is not None:
        liquidity = token.liquidity_usd
    elif callout.dex.liquidity_usd is not None:
        liquidity = callout.dex.liquidity_usd
    return RunnerMarketSnapshot(
        mint=callout.mint,
        captured_at=captured_at,
        price_usd=token.usd_price if token else None,
        market_cap_usd=(
            token.market_cap_usd
            if token and token.market_cap_usd is not None
            else callout.dex.market_cap_usd
        ),
        liquidity_usd=liquidity,
        volume_5m_usd=callout.dex.volume_5m_usd,
        dex_price_change_5m_percent=callout.dex.price_change_5m_percent,
        buys_5m=callout.dex.buys_5m,
        sells_5m=callout.dex.sells_5m,
        holder_count=token.holder_count if token else None,
        verified_unique_buyers=max(0, verified_unique_buyers),
        largest_verified_buyer_percent=largest_verified_buyer_percent,
        smart_wallet_count=len(set(callout.smart_wallets)),
        top10_percent=(
            token.top_holders_percent
            if token and token.top_holders_percent is not None
            else risk.top10_percent
        ),
        dev_percent=(
            token.dev_balance_percent
            if token and token.dev_balance_percent is not None
            else risk.dev_percent
        ),
        bundlers_percent=risk.bundlers_percent,
        insiders_percent=risk.insiders_percent,
        snipers_percent=risk.snipers_percent,
        risk_score=risk.score,
        rugged=risk.rugged,
        route_available=quote is not None,
        route_price_impact_percent=(quote.price_impact_percent if quote else None),
    )


def score_runner_candidate(
    callout: CoinCallout,
    *,
    first: RunnerMarketSnapshot,
    current: RunnerMarketSnapshot,
    history: Iterable[RunnerMarketSnapshot] = (),
    graduated_at: int | None,
    graduation_source: str,
    earliest_smart_entry_at: int | None = None,
    smart_wallets: tuple[str, ...] = (),
    now: int,
) -> RunnerCandidate:
    """Score only evidence available at ``now``; future outcome rows never enter here."""

    positives: list[str] = []
    warnings: list[str] = []
    blockers: list[str] = []
    parts = {
        "graduation_recency": 0,
        "momentum": 0,
        "acceleration": 0,
        "buy_quality": 0,
        "liquidity": 0,
        "holders": 0,
        "smart_wallets": 0,
        "safety_route": 0,
        "x_social": 0,
        "penalties": 0,
    }

    age_seconds = max(0, now - graduated_at) if graduated_at else None
    if age_seconds is not None:
        if age_seconds <= 300:
            parts["graduation_recency"] = 12
        elif age_seconds <= 900:
            parts["graduation_recency"] = 10
        elif age_seconds <= 1_800:
            parts["graduation_recency"] = 7
        elif age_seconds <= 3_600:
            parts["graduation_recency"] = 4
        else:
            parts["graduation_recency"] = 1
        positives.append(f"pair/graduation recency evidence is {age_seconds // 60}m old")
    else:
        warnings.append("exact graduation time is unavailable")

    price_change = _pct(current.price_usd, first.price_usd)
    mc_change = _pct(current.market_cap_usd, first.market_cap_usd)
    dex_5m = callout.dex.price_change_5m_percent
    if price_change is not None:
        if Decimal("2") <= price_change < Decimal("15"):
            parts["momentum"] += 5
        elif Decimal("15") <= price_change < Decimal("60"):
            parts["momentum"] += 8
        elif Decimal("60") <= price_change < Decimal("150"):
            parts["momentum"] += 5
        elif price_change < Decimal("-10"):
            parts["penalties"] -= 6
    if dex_5m is not None:
        if Decimal("2") <= dex_5m < Decimal("20"):
            parts["momentum"] += 4
        elif Decimal("20") <= dex_5m < Decimal("60"):
            parts["momentum"] += 6
        elif dex_5m < Decimal("-10"):
            parts["penalties"] -= 5
    if mc_change is not None and Decimal("5") <= mc_change < Decimal("100"):
        parts["momentum"] += 3

    history_items = tuple(history)
    momentum_windows = _momentum_windows(
        first=first,
        current=current,
        history=history_items,
    )
    prior = history_items[-1] if history_items else None
    if prior is not None and prior.captured_at < current.captured_at:
        volume_ratio = _ratio(current.volume_5m_usd, prior.volume_5m_usd)
        tx_ratio = _ratio(
            current.buys_5m + current.sells_5m,
            prior.buys_5m + prior.sells_5m,
        )
        if volume_ratio is not None and volume_ratio >= Decimal("1.50"):
            parts["acceleration"] += 5
            positives.append("five-minute volume is accelerating")
        elif volume_ratio is not None and volume_ratio <= Decimal("0.60"):
            parts["penalties"] -= 3
            warnings.append("five-minute volume is fading")
        if tx_ratio is not None and tx_ratio >= Decimal("1.35"):
            parts["acceleration"] += 4
            positives.append("transaction flow is accelerating")
    elif current.volume_5m_usd >= Decimal("2500"):
        parts["acceleration"] += 2

    total = current.buys_5m + current.sells_5m
    buy_ratio = Decimal(current.buys_5m) / Decimal(total) if total else Decimal("0")
    if total >= 20 and buy_ratio >= Decimal("0.60"):
        parts["buy_quality"] += 6
        positives.append(f"five-minute flow favors buyers ({current.buys_5m}/{total})")
    elif total and buy_ratio < Decimal("0.45"):
        parts["penalties"] -= 5
        warnings.append("five-minute flow favors sellers")
    if current.verified_unique_buyers >= 3:
        parts["buy_quality"] += min(5, current.verified_unique_buyers)
    if (
        current.largest_verified_buyer_percent is not None
        and current.largest_verified_buyer_percent > Decimal("60")
    ):
        parts["penalties"] -= 8
        warnings.append("one verified wallet dominates observed smart-wallet buy value")

    liquidity_change = _pct(current.liquidity_usd, first.liquidity_usd)
    if current.liquidity_usd is None or current.liquidity_usd < Decimal("2000"):
        blockers.append("liquidity is below $2,000 or unavailable")
    elif current.liquidity_usd >= Decimal("25000"):
        parts["liquidity"] += 8
    elif current.liquidity_usd >= Decimal("10000"):
        parts["liquidity"] += 6
    else:
        parts["liquidity"] += 3
    if liquidity_change is not None:
        if liquidity_change >= Decimal("10"):
            parts["liquidity"] += 3
        elif liquidity_change <= Decimal("-25"):
            blockers.append("liquidity has fallen at least 25% since first seen")

    holder_growth = None
    if current.holder_count is not None and first.holder_count is not None:
        holder_growth = current.holder_count - first.holder_count
    if current.holder_count is not None:
        if current.holder_count >= 250:
            parts["holders"] += 6
        elif current.holder_count >= 100:
            parts["holders"] += 4
        elif current.holder_count >= 30:
            parts["holders"] += 2
        else:
            warnings.append(f"only {current.holder_count} holders")
    else:
        warnings.append("holder count is unavailable")
    if holder_growth is not None and holder_growth >= 10:
        parts["holders"] += 3
        positives.append(f"holder count grew by {holder_growth} since first seen")

    smart_count = len(set(smart_wallets))
    parts["smart_wallets"] += min(12, smart_count * 4)
    earliest_age = None
    if earliest_smart_entry_at is not None and graduated_at is not None:
        earliest_age = max(0, earliest_smart_entry_at - graduated_at)
        if earliest_age <= 300:
            parts["smart_wallets"] += 3
            positives.append("a verified smart wallet entered within five minutes")
        elif earliest_age > 1_800:
            parts["penalties"] -= 3
            warnings.append("verified wallet overlap arrived late")
    if smart_count:
        positives.append(f"{smart_count} independent tracked smart wallet(s) overlap")
    else:
        warnings.append("no tracked smart-wallet overlap yet")

    if current.rugged:
        blockers.append("Solana Tracker marks the token as rugged")
    if current.risk_score is None:
        warnings.append("complete Tracker risk score is unavailable")
    elif current.risk_score >= Decimal("8"):
        blockers.append(f"Tracker risk is {current.risk_score}/10")
    elif current.risk_score <= Decimal("3"):
        parts["safety_route"] += 5
    else:
        parts["safety_route"] += 2
    for label, value, hard in (
        ("bundlers", current.bundlers_percent, Decimal("20")),
        ("insiders", current.insiders_percent, Decimal("20")),
        ("snipers", current.snipers_percent, Decimal("35")),
        ("developer", current.dev_percent, Decimal("10")),
        ("top holders", current.top10_percent, Decimal("45")),
    ):
        if value is not None and value > hard:
            blockers.append(f"{label} concentration is {value:.1f}%")
    if current.route_available:
        impact = current.route_price_impact_percent or Decimal("0")
        if impact <= Decimal("3"):
            parts["safety_route"] += 6
        else:
            blockers.append(f"$5 Jupiter route impact is {impact:.2f}%")
    else:
        blockers.append("executable $5 Jupiter route is unavailable")

    social = callout.social
    if social.available:
        exact_quality = min(4, social.credible_contract_authors * 2)
        diversity = min(3, social.contract_authors)
        velocity = 2 if social.posts_per_minute >= Decimal("0.25") else 0
        parts["x_social"] = exact_quality + diversity + velocity
        if social.duplicate_percent >= Decimal("35"):
            parts["penalties"] -= 6
            warnings.append("X exact-contract text is highly duplicated")
    else:
        warnings.append("X exact-contract velocity was not verified")

    overextended = bool(
        (price_change is not None and price_change >= Decimal("200"))
        or (dex_5m is not None and dex_5m >= Decimal("100"))
        or (mc_change is not None and mc_change >= Decimal("300"))
    )
    if overextended:
        parts["penalties"] -= 15
        warnings.append("move is already parabolic; late-chase penalty applied")
    elif price_change is not None and price_change >= Decimal("80") and buy_ratio < Decimal("0.55"):
        parts["penalties"] -= 10
        warnings.append("price expanded while buyer pressure deteriorated")

    raw_score = sum(parts.values())
    score = Decimal(max(0, min(100, raw_score))).quantize(Decimal("0.01"))
    if blockers:
        tier = "BLOCKED — RESEARCH ONLY"
    elif score >= Decimal("82") and social.available:
        tier = "FOMO RUNNER VERIFIED"
    elif score >= Decimal("70"):
        tier = "FOMO RUNNER DEVELOPING"
    else:
        tier = "FOMO EARLY WATCH"
    return RunnerCandidate(
        mint=callout.mint,
        symbol=callout.symbol,
        name=callout.name,
        first_seen_at=first.captured_at,
        graduated_at=graduated_at,
        graduation_source=graduation_source,
        first=first,
        current=current,
        score=score,
        tier=tier,
        breakdown=RunnerScoreBreakdown(**parts),
        momentum_windows=momentum_windows,
        smart_wallets=tuple(dict.fromkeys(smart_wallets)),
        earliest_smart_entry_at=earliest_smart_entry_at,
        earliest_smart_entry_age_seconds=earliest_age,
        top_trader_overlap=None,
        x_evidence=social,
        positives=tuple(dict.fromkeys(positives)),
        warnings=tuple(dict.fromkeys(warnings)),
        hard_blockers=tuple(dict.fromkeys(blockers)),
        overextended=overextended,
        research_only=True,
        pair_url=callout.dex.pair_url,
        generated_at=now,
    )


def runner_candidate_to_json(candidate: RunnerCandidate) -> str:
    return json.dumps(asdict(candidate), default=str, separators=(",", ":"))


def runner_snapshot_to_json(snapshot: RunnerMarketSnapshot) -> str:
    return json.dumps(asdict(snapshot), default=str, separators=(",", ":"))


def _decimal_fields(payload: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    for field in fields:
        value = payload.get(field)
        payload[field] = Decimal(str(value)) if value is not None else None
    return payload


def runner_candidate_from_json(raw: str) -> RunnerCandidate:
    payload = json.loads(raw)
    snapshot_decimal_fields = (
        "price_usd",
        "market_cap_usd",
        "liquidity_usd",
        "volume_5m_usd",
        "dex_price_change_5m_percent",
        "largest_verified_buyer_percent",
        "top10_percent",
        "dev_percent",
        "bundlers_percent",
        "insiders_percent",
        "snipers_percent",
        "risk_score",
        "route_price_impact_percent",
    )
    first = RunnerMarketSnapshot(
        **_decimal_fields(dict(payload.pop("first")), snapshot_decimal_fields)
    )
    current = RunnerMarketSnapshot(
        **_decimal_fields(dict(payload.pop("current")), snapshot_decimal_fields)
    )
    breakdown = RunnerScoreBreakdown(**payload.pop("breakdown"))
    momentum_windows = tuple(
        RunnerMomentumWindow(
            **_decimal_fields(
                dict(item),
                (
                    "price_change_percent",
                    "market_cap_change_percent",
                    "rolling_volume_change_percent",
                    "rolling_transactions_change_percent",
                ),
            )
        )
        for item in payload.pop("momentum_windows", ())
    )
    x_data = dict(payload.pop("x_evidence"))
    x_data["duplicate_percent"] = Decimal(str(x_data.get("duplicate_percent") or 0))
    x_data["posts_per_minute"] = Decimal(str(x_data.get("posts_per_minute") or 0))
    for key in ("notable_accounts", "notable_posts"):
        x_data[key] = tuple(x_data.get(key) or ())
    for key in ("smart_wallets", "positives", "warnings", "hard_blockers"):
        payload[key] = tuple(payload.get(key) or ())
    payload["score"] = Decimal(str(payload["score"]))
    return RunnerCandidate(
        first=first,
        current=current,
        breakdown=breakdown,
        momentum_windows=momentum_windows,
        x_evidence=XSocialSnapshot(**x_data),
        **payload,
    )


def runner_snapshot_from_json(raw: str) -> RunnerMarketSnapshot:
    decimal_fields = (
        "price_usd",
        "market_cap_usd",
        "liquidity_usd",
        "volume_5m_usd",
        "dex_price_change_5m_percent",
        "largest_verified_buyer_percent",
        "top10_percent",
        "dev_percent",
        "bundlers_percent",
        "insiders_percent",
        "snipers_percent",
        "risk_score",
        "route_price_impact_percent",
    )
    return RunnerMarketSnapshot(**_decimal_fields(json.loads(raw), decimal_fields))


def forward_return_percent(
    current: Decimal | None,
    first: Decimal | None,
) -> Decimal | None:
    result = _pct(current, first)
    return result.quantize(Decimal("0.01")) if result is not None else None
