from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict
from decimal import Decimal
from typing import Any

from .models import (
    CoinCallout,
    RunnerCandidate,
    RunnerForensics,
    RunnerFundingCluster,
    RunnerFundingObservation,
    RunnerMarketSnapshot,
    RunnerMomentumWindow,
    RunnerSafetyAssessment,
    RunnerScoreBreakdown,
    XSocialSnapshot,
)

RUNNER_HORIZONS_SECONDS = (60, 300, 900, 1_800, 3_600, 14_400, 86_400)
RUNNER_MOMENTUM_WINDOWS_SECONDS = (15, 30, 60, 180, 300)
FRESH_WATCH_OFFSETS_SECONDS = (0, 15, 30, 60, 120, 180, 300, 600, 900)


def _unique(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for item in items if item))


def build_funding_clusters(
    observations: Iterable[RunnerFundingObservation],
    *,
    funding_window_seconds: int = 1_800,
    amount_tolerance_percent: Decimal = Decimal("10"),
) -> tuple[RunnerFundingCluster, ...]:
    """Group direct funder links and flag close-time/similar-amount coordination.

    This only describes public transaction relationships. It is a coordination
    signal, not proof that the wallets share a real-world owner or committed fraud.
    """

    by_funder: dict[str, list[RunnerFundingObservation]] = {}
    for item in observations:
        if item.funder:
            by_funder.setdefault(item.funder, []).append(item)
    groups: list[RunnerFundingCluster] = []
    for funder, rows in by_funder.items():
        wallets = _unique(item.wallet for item in rows)
        if len(wallets) < 2:
            continue
        times = sorted(item.funded_at for item in rows if item.funded_at is not None)
        interval = times[-1] - times[0] if len(times) >= 2 else None
        amounts = [item.amount_sol for item in rows if item.amount_sol is not None]
        similar = False
        if len(amounts) >= 2 and min(amounts) > 0:
            spread = (max(amounts) - min(amounts)) / min(amounts) * Decimal("100")
            similar = spread <= amount_tolerance_percent
        buys = sorted(item.bought_at for item in rows if item.bought_at is not None)
        buy_interval = buys[-1] - buys[0] if len(buys) >= 2 else None
        time_linked = bool(
            interval is not None
            and interval <= funding_window_seconds
            and similar
            and buy_interval is not None
            and buy_interval <= funding_window_seconds
        )
        supply_values = [item.supply_percent for item in rows if item.supply_percent is not None]
        supply = sum(supply_values, Decimal("0")) if supply_values else None
        groups.append(
            RunnerFundingCluster(
                cluster_id=funder,
                wallets=wallets,
                wallet_count=len(wallets),
                supply_percent=(supply.quantize(Decimal("0.01")) if supply is not None else None),
                funding_interval_seconds=interval,
                similar_amounts=similar,
                time_linked=time_linked,
                confidence="HIGH" if time_linked else "MEDIUM",
            )
        )
    return tuple(
        sorted(
            groups,
            key=lambda item: (item.wallet_count, item.supply_percent or Decimal("0")),
            reverse=True,
        )
    )


def summarize_forensics(
    observations: Iterable[RunnerFundingObservation],
    *,
    raw_unique_buyers: int = 0,
    raw_top10_percent: Decimal | None = None,
    checked_at: int = 0,
    warnings: Iterable[str] = (),
) -> RunnerForensics:
    rows = tuple(observations)
    clusters = build_funding_clusters(rows)
    linked_reductions = sum(max(0, item.wallet_count - 1) for item in clusters)
    raw_entities = max(raw_unique_buyers, len(_unique(item.wallet for item in rows)))
    independent = max(0, raw_entities - linked_reductions) if raw_entities else 0
    largest = clusters[0] if clusters else None
    linked_supply = largest.supply_percent if largest else None
    adjusted_values = [value for value in (raw_top10_percent, linked_supply) if value is not None]
    return RunnerForensics(
        available=True,
        raw_unique_buyers=raw_unique_buyers,
        estimated_independent_clusters=independent,
        largest_cluster_size=largest.wallet_count if largest else 1 if rows else 0,
        largest_cluster_supply_percent=linked_supply,
        cluster_adjusted_percent=max(adjusted_values) if adjusted_values else None,
        shared_funder_groups=clusters,
        time_linked_groups=tuple(item for item in clusters if item.time_linked),
        observations=rows,
        warnings=tuple(dict.fromkeys(warnings)),
        checked_at=checked_at,
        funding_checked_at=checked_at,
        dynamic_checked_at=checked_at,
    )


def funding_observation_from_transaction(
    transaction: dict[str, Any] | None,
    *,
    wallet: str,
    supply_percent: Decimal | None = None,
    bought_at: int | None = None,
) -> RunnerFundingObservation:
    """Extract a direct native-SOL funder from one documented parsed RPC transaction."""

    if not isinstance(transaction, dict):
        return RunnerFundingObservation(wallet=wallet, supply_percent=supply_percent)
    message = ((transaction.get("transaction") or {}).get("message") or {})
    meta = transaction.get("meta") or {}
    raw_keys = message.get("accountKeys") or []
    keys: list[str] = []
    signers: set[str] = set()
    for item in raw_keys:
        if isinstance(item, dict):
            key = str(item.get("pubkey") or "")
            if item.get("signer"):
                signers.add(key)
        else:
            key = str(item)
        keys.append(key)
    try:
        wallet_index = keys.index(wallet)
    except ValueError:
        return RunnerFundingObservation(wallet=wallet, supply_percent=supply_percent)
    pre = meta.get("preBalances") or []
    post = meta.get("postBalances") or []
    if wallet_index >= len(pre) or wallet_index >= len(post):
        return RunnerFundingObservation(wallet=wallet, supply_percent=supply_percent)
    wallet_gain = int(post[wallet_index]) - int(pre[wallet_index])
    funder: str | None = None
    funder_loss = 0
    for index, key in enumerate(keys):
        if index >= len(pre) or index >= len(post) or key == wallet:
            continue
        loss = int(pre[index]) - int(post[index])
        if key in signers and loss > funder_loss:
            funder = key
            funder_loss = loss
    amount = Decimal(wallet_gain) / Decimal("1000000000") if wallet_gain > 0 else None
    block_time = transaction.get("blockTime")
    observed_at = int(block_time) if block_time is not None else None
    return RunnerFundingObservation(
        wallet=wallet,
        funder=funder,
        funded_at=observed_at if funder else None,
        amount_sol=amount if funder else None,
        bought_at=bought_at,
        supply_percent=supply_percent,
    )


def assess_runner_safety(
    snapshot: RunnerMarketSnapshot,
    forensics: RunnerForensics | None = None,
) -> RunnerSafetyAssessment:
    """Fail closed for ENTRY while still allowing unsafe momentum research."""

    forensics = forensics or RunnerForensics()
    failures: list[str] = []
    unknowns: list[str] = []
    warnings: list[str] = []
    risk = Decimal("0")

    if snapshot.rugged:
        failures.append("rugged state is present")
        risk = Decimal("100")
    if snapshot.suspicious:
        failures.append("token metadata is flagged suspicious")
        risk += 35
    if snapshot.liquidity_usd is None:
        unknowns.append("liquidity")
    elif snapshot.liquidity_usd < Decimal("2000"):
        failures.append("liquidity is below $2,000")
        risk += 35
    elif snapshot.liquidity_usd < Decimal("5000"):
        warnings.append("launch-stage liquidity")
        risk += 15
    if snapshot.holder_count is None:
        unknowns.append("holders")
    elif snapshot.holder_count < 30:
        failures.append("holder count is below 30")
        risk += 25
    elif snapshot.holder_count < 100:
        warnings.append("holder count is below 100")
        risk += 10

    thresholds = (
        ("Top10", snapshot.top10_percent, Decimal("45"), Decimal("35"), 35),
        ("dev", snapshot.dev_percent, Decimal("10"), Decimal("5"), 25),
        ("bundlers", snapshot.bundlers_percent, Decimal("20"), Decimal("10"), 30),
        ("insiders", snapshot.insiders_percent, Decimal("20"), Decimal("10"), 30),
        ("snipers", snapshot.snipers_percent, Decimal("35"), Decimal("20"), 25),
    )
    for label, value, hard, caution, weight in thresholds:
        if value is None:
            unknowns.append(label)
        elif value > hard:
            failures.append(f"{label} concentration is {value:.1f}%")
            risk += weight
        elif value > caution:
            warnings.append(f"{label} concentration is elevated ({value:.1f}%)")
            risk += Decimal(weight) / 2

    if snapshot.risk_score is None:
        unknowns.append("Tracker risk")
    else:
        risk += max(Decimal("0"), min(Decimal("35"), snapshot.risk_score * Decimal("3.5")))
        if snapshot.risk_score >= Decimal("8"):
            failures.append(f"Tracker risk is {snapshot.risk_score}/10")

    for label, disabled in (
        ("mint authority", snapshot.mint_authority_disabled),
        ("freeze authority", snapshot.freeze_authority_disabled),
    ):
        if disabled is None:
            unknowns.append(label)
        elif disabled is False:
            failures.append(f"{label} is enabled")
            risk += 30

    if snapshot.buy_route_status == "FAIL":
        failures.append("buy route failed")
        risk += 20
    elif snapshot.buy_route_status != "PASS":
        unknowns.append("buy route")
    if snapshot.sell_route_status == "FAIL":
        failures.append("sell route failed")
        risk += 45
    elif snapshot.sell_route_status != "PASS":
        unknowns.append("sell route")
    for label, impact in (
        ("buy", snapshot.route_price_impact_percent),
        ("sell", snapshot.sell_route_price_impact_percent),
    ):
        if impact is not None and impact > Decimal("3"):
            failures.append(f"{label} route impact is {impact:.2f}%")
            risk += 25

    if not forensics.available or forensics.cluster_adjusted_percent is None:
        unknowns.append("cluster-adjusted concentration")
    else:
        if forensics.cluster_adjusted_percent > Decimal("45"):
            failures.append(
                "cluster-adjusted concentration is "
                f"{forensics.cluster_adjusted_percent:.1f}%"
            )
            risk += 40
        elif forensics.cluster_adjusted_percent > Decimal("35"):
            warnings.append("cluster-adjusted concentration is elevated")
            risk += 15
        if not forensics.observations or any(
            item.funder is None for item in forensics.observations
        ):
            unknowns.append("holder funding relationships")
    if forensics.time_linked_groups:
        warnings.append("time-linked funding coordination signal detected")
        risk += min(Decimal("20"), Decimal(len(forensics.time_linked_groups) * 5))

    risk = max(Decimal("0"), min(Decimal("100"), risk)).quantize(Decimal("0.01"))
    if failures:
        status = "FAIL"
    elif unknowns:
        status = "UNKNOWN"
    else:
        status = "PASS"
    level = (
        "UNKNOWN"
        if status == "UNKNOWN"
        else "LOW"
        if risk < 25
        else "MODERATE"
        if risk < 50
        else "HIGH"
        if risk < 75
        else "CRITICAL"
    )
    return RunnerSafetyAssessment(
        scam_risk_score=risk,
        scam_risk_level=level,
        status=status,
        entry_eligible=status == "PASS",
        critical_unknowns=tuple(dict.fromkeys(unknowns)),
        failures=tuple(dict.fromkeys(failures)),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def fresh_watch_schedule(base_seconds: int = 15, minutes: int = 15) -> tuple[int, ...]:
    ceiling = max(base_seconds, minutes * 60)
    canonical = tuple(
        value
        for value in FRESH_WATCH_OFFSETS_SECONDS
        if value == 0 or base_seconds <= value <= ceiling
    )
    return canonical or (0,)


def is_fresh_research_worthy(candidate: RunnerCandidate, *, max_age_seconds: int = 300) -> bool:
    age = (
        candidate.generated_at - candidate.pair_created_at
        if candidate.pair_created_at is not None
        else None
    )
    current = candidate.current
    immediate_catastrophe = bool(
        current.rugged
        or (current.liquidity_usd is not None and current.liquidity_usd < Decimal("2000"))
        or current.sell_route_status == "FAIL"
    )
    return bool(
        age is not None
        and 0 <= age <= max_age_seconds
        and current.market_cap_usd is not None
        and current.liquidity_usd is not None
        and current.liquidity_usd >= Decimal("2000")
        and current.buys_5m + current.sells_5m >= 3
        and current.buys_5m >= 2
        and current.volume_5m_usd >= Decimal("250")
        and not immediate_catastrophe
    )


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
    sell_status = "PASS" if callout.sell_quote is not None else "UNKNOWN"
    if callout.sell_quote_error and not any(
        phrase in callout.sell_quote_error.casefold()
        for phrase in (
            "not checked",
            "not configured",
            "decimals are unavailable",
            "could not be derived",
        )
    ):
        sell_status = "FAIL"
    buy_status = "PASS" if quote is not None else "UNKNOWN"
    if callout.quote_error and not any(
        phrase in callout.quote_error.casefold()
        for phrase in ("not configured", "decimals are unavailable")
    ):
        buy_status = "FAIL"
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
        buy_route_status=buy_status,
        sell_route_status=sell_status,
        sell_route_price_impact_percent=(
            callout.sell_quote.price_impact_percent if callout.sell_quote else None
        ),
        suspicious=bool(token.suspicious) if token else False,
        mint_authority_disabled=(token.mint_authority_disabled if token else None),
        freeze_authority_disabled=(token.freeze_authority_disabled if token else None),
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
    smart_wallet_addresses: tuple[str, ...] = (),
    forensics: RunnerForensics | None = None,
    score_history: tuple[Decimal, ...] = (),
    pair_created_at: int | None = None,
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

    observed_pair_created_at = (
        now - callout.dex.pair_age_minutes * 60
        if callout.dex.pair_age_minutes is not None
        else None
    )
    pair_created_at = pair_created_at or observed_pair_created_at
    source_created_at = graduated_at or pair_created_at
    age_seconds = max(0, now - source_created_at) if source_created_at else None
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

    raw_smart_count = len(set(smart_wallet_addresses or smart_wallets))
    independent_smart_count = raw_smart_count
    if forensics and forensics.available and smart_wallet_addresses:
        smart_addresses = set(smart_wallet_addresses)
        for group in forensics.shared_funder_groups:
            overlap = len(smart_addresses.intersection(group.wallets))
            independent_smart_count -= max(0, overlap - 1)
    smart_count = max(0, independent_smart_count)
    parts["smart_wallets"] += min(12, smart_count * 4)
    earliest_age = None
    if earliest_smart_entry_at is not None and source_created_at is not None:
        earliest_age = max(0, earliest_smart_entry_at - source_created_at)
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
    if current.buy_route_status == "PASS" or current.route_available:
        impact = current.route_price_impact_percent or Decimal("0")
        if impact <= Decimal("3"):
            parts["safety_route"] += 6
        else:
            blockers.append(f"$5 Jupiter route impact is {impact:.2f}%")
    else:
        blockers.append("executable $5 Jupiter route is unavailable")
    if current.sell_route_status == "FAIL":
        blockers.append("read-only sell route is unavailable")
    elif current.sell_route_status == "PASS":
        sell_impact = current.sell_route_price_impact_percent or Decimal("0")
        if sell_impact <= Decimal("3"):
            parts["safety_route"] += 3
        else:
            blockers.append(f"$5 sell-route impact is {sell_impact:.2f}%")

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
    forensic_result = forensics or RunnerForensics()
    safety = assess_runner_safety(current, forensic_result)
    age_seconds = now - pair_created_at if pair_created_at is not None else None
    if score >= Decimal("50") and safety.status == "FAIL":
        state = "⚠️ UNSAFE MOMENTUM"
    elif score >= Decimal("85") and safety.status == "PASS":
        state = "🚨 STRONG RUNNER"
    elif score >= Decimal("70") and safety.status == "PASS":
        state = "✅ ENTRY CANDIDATE"
    elif score >= Decimal("55"):
        state = "🔥 HEATING UP"
    elif score >= Decimal("35"):
        state = "🟡 WATCH"
    elif age_seconds is not None and age_seconds <= 300:
        state = "⚡ FRESH RUNNER"
    else:
        state = "👀 EARLY RESEARCH"
    tier = "BLOCKED — RESEARCH ONLY" if blockers and score < Decimal("50") else state
    first_research_eligible_at = (
        now
        if callout.dex.available
        and current.market_cap_usd is not None
        and current.liquidity_usd is not None
        and current.buys_5m + current.sells_5m >= 3
        else None
    )
    entry_ready = bool(score >= Decimal("70") and safety.entry_eligible and not overextended)
    history_values = tuple(score_history)
    if not history_values or history_values[-1] != score:
        history_values = (*history_values, score)
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
        pair_created_at=pair_created_at,
        radar_first_seen_at=first.captured_at,
        first_market_data_at=(first.captured_at if first.market_cap_usd is not None else None),
        first_research_eligible_at=first_research_eligible_at,
        entry_eligible_at=now if entry_ready else None,
        score_history=history_values[-8:],
        state=state,
        safety=safety,
        detection_safety=safety,
        forensics=forensic_result,
        detection_forensics=forensic_result,
        detection_score=score,
        raw_smart_wallet_count=raw_smart_count,
        estimated_independent_smart_wallets=smart_count,
    )


def runner_candidate_to_json(candidate: RunnerCandidate) -> str:
    return json.dumps(asdict(candidate), default=str, separators=(",", ":"))


def runner_snapshot_to_json(snapshot: RunnerMarketSnapshot) -> str:
    return json.dumps(asdict(snapshot), default=str, separators=(",", ":"))


def runner_forensics_to_json(forensics: RunnerForensics) -> str:
    return json.dumps(asdict(forensics), default=str, separators=(",", ":"))


def runner_forensics_from_json(raw: str) -> RunnerForensics:
    payload = json.loads(raw)
    observations: list[RunnerFundingObservation] = []
    for raw_item in payload.pop("observations", ()):
        item = dict(raw_item)
        for key in ("amount_sol", "supply_percent"):
            if item.get(key) is not None:
                item[key] = Decimal(str(item[key]))
        observations.append(RunnerFundingObservation(**item))

    def cluster(raw_item: Any) -> RunnerFundingCluster:
        item = dict(raw_item)
        item["wallets"] = tuple(item.get("wallets") or ())
        if item.get("supply_percent") is not None:
            item["supply_percent"] = Decimal(str(item["supply_percent"]))
        return RunnerFundingCluster(**item)

    payload["shared_funder_groups"] = tuple(
        cluster(item) for item in payload.get("shared_funder_groups", ())
    )
    payload["time_linked_groups"] = tuple(
        cluster(item) for item in payload.get("time_linked_groups", ())
    )
    payload["observations"] = tuple(observations)
    for key in (
        "largest_cluster_supply_percent",
        "cluster_adjusted_percent",
        "creator_percent",
    ):
        if payload.get(key) is not None:
            payload[key] = Decimal(str(payload[key]))
    for key in ("creator_linked_wallets", "warnings"):
        payload[key] = tuple(payload.get(key) or ())
    return RunnerForensics(**payload)


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
        "sell_route_price_impact_percent",
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
    x_data = dict(payload.pop("x_evidence", {"available": False}))
    x_data["duplicate_percent"] = Decimal(str(x_data.get("duplicate_percent") or 0))
    x_data["posts_per_minute"] = Decimal(str(x_data.get("posts_per_minute") or 0))
    for key in ("notable_accounts", "notable_posts"):
        x_data[key] = tuple(x_data.get(key) or ())
    for key in ("smart_wallets", "positives", "warnings", "hard_blockers"):
        payload[key] = tuple(payload.get(key) or ())
    payload["score"] = Decimal(str(payload["score"]))
    payload["score_history"] = tuple(
        Decimal(str(item)) for item in payload.get("score_history", ())
    )

    def safety_from_payload(value: Any) -> RunnerSafetyAssessment:
        if not isinstance(value, dict):
            return RunnerSafetyAssessment()
        item = dict(value)
        item["scam_risk_score"] = Decimal(str(item.get("scam_risk_score") or 0))
        for key in ("critical_unknowns", "failures", "warnings"):
            item[key] = tuple(item.get(key) or ())
        return RunnerSafetyAssessment(**item)

    forensic_data = payload.pop("forensics", None)
    if isinstance(forensic_data, dict):
        forensic_values = dict(forensic_data)
        observations = []
        for raw_item in forensic_values.pop("observations", ()):
            item = dict(raw_item)
            for key in ("amount_sol", "supply_percent"):
                if item.get(key) is not None:
                    item[key] = Decimal(str(item[key]))
            observations.append(RunnerFundingObservation(**item))

        def cluster_from_payload(raw_item: Any) -> RunnerFundingCluster:
            item = dict(raw_item)
            item["wallets"] = tuple(item.get("wallets") or ())
            if item.get("supply_percent") is not None:
                item["supply_percent"] = Decimal(str(item["supply_percent"]))
            return RunnerFundingCluster(**item)

        forensic_values["shared_funder_groups"] = tuple(
            cluster_from_payload(item)
            for item in forensic_values.get("shared_funder_groups", ())
        )
        forensic_values["time_linked_groups"] = tuple(
            cluster_from_payload(item)
            for item in forensic_values.get("time_linked_groups", ())
        )
        forensic_values["observations"] = tuple(observations)
        for key in (
            "largest_cluster_supply_percent",
            "cluster_adjusted_percent",
            "creator_percent",
        ):
            if forensic_values.get(key) is not None:
                forensic_values[key] = Decimal(str(forensic_values[key]))
        for key in ("creator_linked_wallets", "warnings"):
            forensic_values[key] = tuple(forensic_values.get(key) or ())
        forensics = RunnerForensics(**forensic_values)
    else:
        forensics = RunnerForensics()
    detection_forensic_data = payload.pop("detection_forensics", None)
    detection_forensics = (
        runner_forensics_from_json(json.dumps(detection_forensic_data))
        if isinstance(detection_forensic_data, dict)
        else RunnerForensics()
    )
    safety = safety_from_payload(payload.pop("safety", None))
    detection_safety = safety_from_payload(payload.pop("detection_safety", None))
    if payload.get("detection_score") is not None:
        payload["detection_score"] = Decimal(str(payload["detection_score"]))
    return RunnerCandidate(
        first=first,
        current=current,
        breakdown=breakdown,
        momentum_windows=momentum_windows,
        x_evidence=XSocialSnapshot(**x_data),
        safety=safety,
        detection_safety=detection_safety,
        forensics=forensics,
        detection_forensics=detection_forensics,
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
        "sell_route_price_impact_percent",
    )
    return RunnerMarketSnapshot(**_decimal_fields(json.loads(raw), decimal_fields))


def forward_return_percent(
    current: Decimal | None,
    first: Decimal | None,
) -> Decimal | None:
    result = _pct(current, first)
    return result.quantize(Decimal("0.01")) if result is not None else None


def runner_path_metrics(
    first: RunnerMarketSnapshot,
    snapshots: Iterable[RunnerMarketSnapshot],
) -> dict[str, object]:
    """Measure the usable post-detection path without feeding it back into scoring."""

    series = sorted(snapshots, key=lambda item: item.captured_at)
    points: list[tuple[int, Decimal, Decimal]] = []
    for item in series:
        base = first.price_usd
        value = item.price_usd
        if base is None or value is None or base <= 0:
            base = first.market_cap_usd
            value = item.market_cap_usd
        change = forward_return_percent(value, base)
        if value is not None and change is not None:
            points.append((item.captured_at, value, change))
    peak_point = max(points, key=lambda item: item[2]) if points else None
    trough_point = min(points, key=lambda item: item[2]) if points else None
    running_peak: Decimal | None = None
    max_drawdown = Decimal("0")
    for _captured_at, value, _change in points:
        running_peak = value if running_peak is None else max(running_peak, value)
        if running_peak > 0:
            drawdown = (Decimal("1") - value / running_peak) * Decimal("100")
            max_drawdown = max(max_drawdown, drawdown)

    def first_cross(level: Decimal, *, upward: bool) -> int | None:
        for captured_at, _value, change in points:
            if (change >= level) if upward else (change <= level):
                return captured_at
        return None

    positive_times = {
        level: first_cross(Decimal(level), upward=True)
        for level in (10, 25, 50, 100)
    }
    negative_times = {
        level: first_cross(Decimal(-level), upward=False) for level in (25, 50)
    }

    def before(positive: int, negative: int) -> bool | None:
        win_at = positive_times[positive]
        loss_at = negative_times[negative]
        if win_at is None and loss_at is None:
            return None
        return win_at is not None and (loss_at is None or win_at <= loss_at)

    liquidities = [item.liquidity_usd for item in series if item.liquidity_usd is not None]
    return {
        "maximum_favorable_excursion": peak_point[2] if peak_point else None,
        "maximum_adverse_excursion": trough_point[2] if trough_point else None,
        "peak_return": peak_point[2] if peak_point else None,
        "time_to_peak_seconds": (
            peak_point[0] - first.captured_at if peak_point else None
        ),
        "max_drawdown_from_peak": max_drawdown.quantize(Decimal("0.01")),
        "minimum_liquidity": min(liquidities) if liquidities else None,
        "time_to_10": (
            positive_times[10] - first.captured_at if positive_times[10] else None
        ),
        "time_to_25": (
            positive_times[25] - first.captured_at if positive_times[25] else None
        ),
        "time_to_50": (
            positive_times[50] - first.captured_at if positive_times[50] else None
        ),
        "time_to_100": (
            positive_times[100] - first.captured_at if positive_times[100] else None
        ),
        "plus_25_before_minus_25": before(25, 25),
        "plus_10_before_minus_25": before(10, 25),
        "plus_50_before_minus_50": before(50, 50),
        "plus_100_before_minus_50": before(100, 50),
        "rug_or_liquidity_failure": any(
            item.rugged
            or item.liquidity_usd is None
            or item.liquidity_usd < Decimal("500")
            for item in series
        ),
    }
