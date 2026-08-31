"""Same-slot bundle analysis, and the lifecycle context that makes it meaningful.

Terminal documents a bundle as *a collection of trades in the same direction that
happened in the same slot*.  That is a good, checkable definition and it is what
this module implements from public slot data.

The subtlety section 23 insists on: **ordinary post-graduation same-slot activity
is not launch bundling.**  On a busy PumpSwap pool, unrelated people land in the
same slot constantly — that is just block production.  A launch bundle is a
same-slot group in the token's *first moments*, before organic flow exists.  So
every assessment here takes the lifecycle stage, and a mature token's same-slot
groups are reported as ordinary co-trading rather than flagged as a bundle.

What actually matters afterwards is whether those wallets are **distributing**
(section 92): a bundle that bought and is now selling into retail is a different
risk from one that is still holding.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal("0")
HUNDRED = Decimal("100")

BUNDLE_RISK_NONE = "NONE"
BUNDLE_RISK_LOW = "LOW"
BUNDLE_RISK_MODERATE = "MODERATE"
BUNDLE_RISK_HIGH = "HIGH"
BUNDLE_RISK_UNKNOWN = "UNKNOWN"

BUNDLE_RISK_LEVELS: tuple[str, ...] = (
    BUNDLE_RISK_NONE,
    BUNDLE_RISK_LOW,
    BUNDLE_RISK_MODERATE,
    BUNDLE_RISK_HIGH,
    BUNDLE_RISK_UNKNOWN,
)


@dataclass(frozen=True, slots=True)
class SlotTrade:
    """One trade, with the slot it landed in."""

    wallet: str
    slot: int
    at: int
    side: str = "BUY"
    amount_sol: Decimal = ZERO
    token_amount: Decimal = ZERO


@dataclass(frozen=True, slots=True)
class Bundle:
    """A same-slot, same-direction group of trades."""

    slot: int
    side: str
    wallets: tuple[str, ...]
    amount_sol: Decimal = ZERO
    token_amount: Decimal = ZERO
    at: int = 0
    #: True when this landed early enough in the token's life to be a launch
    #: bundle rather than ordinary co-trading.
    launch_window: bool = False

    @property
    def size(self) -> int:
        return len(self.wallets)


@dataclass(frozen=True, slots=True)
class BundleProfile:
    """What the bundles bought, and whether they are still holding it."""

    mint: str
    bundles: tuple[Bundle, ...] = ()
    bundle_count: int = 0
    bundled_wallets: int = 0
    bundle_sol: Decimal = ZERO
    bundle_token_amount: Decimal = ZERO
    bundle_supply_percent: Decimal | None = None
    current_holding_percent: Decimal | None = None
    distributing: bool = False
    risk: str = BUNDLE_RISK_UNKNOWN
    reasons: tuple[str, ...] = ()

    @property
    def held_share(self) -> Decimal | None:
        """How much of what the bundles bought they still hold."""

        if self.bundle_supply_percent is None or self.current_holding_percent is None:
            return None
        if self.bundle_supply_percent <= ZERO:
            return None
        return (self.current_holding_percent / self.bundle_supply_percent).quantize(
            Decimal("0.01")
        )

    def operator_line(self) -> str:
        if self.risk == BUNDLE_RISK_UNKNOWN:
            return "bundles: unknown"
        parts = [f"`{self.risk}`", f"{self.bundle_count} bundle(s)"]
        if self.bundle_supply_percent is not None:
            parts.append(f"bought {self.bundle_supply_percent}% of supply")
        if self.current_holding_percent is not None:
            parts.append(f"now hold {self.current_holding_percent}%")
        if self.distributing:
            parts.append("**distributing**")
        return "bundles: " + " • ".join(parts)

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "bundle_count": self.bundle_count,
            "bundled_wallets": self.bundled_wallets,
            "bundle_sol": str(self.bundle_sol),
            "bundle_token_amount": str(self.bundle_token_amount),
            "bundle_supply_percent": _s(self.bundle_supply_percent),
            "current_holding_percent": _s(self.current_holding_percent),
            "held_share": _s(self.held_share),
            "distributing": self.distributing,
            "risk": self.risk,
            "reasons": list(self.reasons),
            "bundles": [
                {
                    "slot": item.slot,
                    "side": item.side,
                    "size": item.size,
                    "amount_sol": str(item.amount_sol),
                    "launch_window": item.launch_window,
                }
                for item in self.bundles
            ],
        }


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


@dataclass(frozen=True, slots=True)
class BundleConfig:
    min_bundle_size: int = 3
    #: How long after creation a same-slot group is a launch bundle rather than
    #: ordinary co-trading (section 23).
    launch_window_seconds: int = 120
    moderate_supply_percent: Decimal = Decimal("10")
    high_supply_percent: Decimal = Decimal("25")


DEFAULT_BUNDLE_CONFIG = BundleConfig()


def detect_bundles(
    trades: Sequence[SlotTrade],
    *,
    created_at: int | None = None,
    config: BundleConfig = DEFAULT_BUNDLE_CONFIG,
) -> tuple[Bundle, ...]:
    """Group same-slot, same-direction trades by distinct wallet."""

    grouped: dict[tuple[int, str], list[SlotTrade]] = {}
    for trade in trades:
        grouped.setdefault((trade.slot, trade.side), []).append(trade)

    bundles: list[Bundle] = []
    for (slot, side), group in sorted(grouped.items()):
        wallets = sorted({item.wallet for item in group})
        if len(wallets) < config.min_bundle_size:
            continue
        landed = min(item.at for item in group)
        bundles.append(
            Bundle(
                slot=slot,
                side=side,
                wallets=tuple(wallets),
                amount_sol=sum((item.amount_sol for item in group), ZERO),
                token_amount=sum((item.token_amount for item in group), ZERO),
                at=landed,
                launch_window=(
                    created_at is not None
                    and 0 <= landed - created_at <= config.launch_window_seconds
                ),
            )
        )
    return tuple(bundles)


def assess_bundles(
    mint: str,
    trades: Sequence[SlotTrade],
    *,
    created_at: int | None = None,
    total_supply: Decimal | None = None,
    current_bundle_holdings: Decimal | None = None,
    pre_graduation: bool = True,
    config: BundleConfig = DEFAULT_BUNDLE_CONFIG,
) -> BundleProfile:
    """Grade bundle risk with lifecycle context (section 23).

    ``pre_graduation`` is what stops a busy mature pool's ordinary same-slot
    activity being reported as launch bundling.
    """

    reasons: list[str] = []
    all_bundles = detect_bundles(trades, created_at=created_at, config=config)
    if not all_bundles:
        return BundleProfile(
            mint=mint,
            risk=BUNDLE_RISK_NONE if trades else BUNDLE_RISK_UNKNOWN,
            reasons=(
                ("no same-slot group large enough to be a bundle",)
                if trades
                else ("no trade detail available",)
            ),
        )

    # Only launch-window groups count as launch bundles.  On a mature pool,
    # same-slot co-trading is block production, not coordination.
    relevant = tuple(item for item in all_bundles if item.launch_window)
    if not relevant and not pre_graduation:
        reasons.append(
            f"{len(all_bundles)} same-slot group(s) found, all after the launch "
            "window — ordinary co-trading, not launch bundling"
        )
        return BundleProfile(
            mint=mint,
            bundles=all_bundles,
            bundle_count=0,
            risk=BUNDLE_RISK_NONE,
            reasons=tuple(reasons),
        )
    if not relevant:
        relevant = all_bundles
        reasons.append("no creation time available; treating same-slot groups as bundles")

    buy_bundles = tuple(item for item in relevant if item.side == "BUY")
    wallets = {wallet for item in buy_bundles for wallet in item.wallets}
    token_amount = sum((item.token_amount for item in buy_bundles), ZERO)
    supply_percent = (
        (token_amount / total_supply * HUNDRED).quantize(Decimal("0.01"))
        if total_supply and total_supply > ZERO
        else None
    )

    distributing = False
    if supply_percent is not None and current_bundle_holdings is not None:
        distributing = current_bundle_holdings < supply_percent * Decimal("0.6")
        if distributing:
            reasons.append("bundle wallets have sold a material part of their position")

    if distributing and supply_percent is not None:
        # Section 92: what they are doing with the position outranks how big it
        # was.  A 12% bundle selling into retail is a bigger problem than a 20%
        # bundle sitting still.
        risk = BUNDLE_RISK_HIGH
        reasons.append("bundle risk escalated: the wallets are selling")
    elif supply_percent is None:
        risk = BUNDLE_RISK_UNKNOWN
        reasons.append("bundle size relative to supply is not determinable")
    elif supply_percent >= config.high_supply_percent:
        risk = BUNDLE_RISK_HIGH
        reasons.append(f"bundles took {supply_percent}% of supply at launch")
    elif supply_percent >= config.moderate_supply_percent:
        risk = BUNDLE_RISK_MODERATE
        reasons.append(f"bundles took {supply_percent}% of supply at launch")
    elif buy_bundles:
        risk = BUNDLE_RISK_LOW
        reasons.append(f"small launch bundling ({supply_percent}% of supply)")
    else:
        risk = BUNDLE_RISK_NONE

    return BundleProfile(
        mint=mint,
        bundles=relevant,
        bundle_count=len(buy_bundles),
        bundled_wallets=len(wallets),
        bundle_sol=sum((item.amount_sol for item in buy_bundles), ZERO),
        bundle_token_amount=token_amount,
        bundle_supply_percent=supply_percent,
        current_holding_percent=current_bundle_holdings,
        distributing=distributing,
        risk=risk,
        reasons=tuple(reasons),
    )


# --- bot activity (section 24) ------------------------------------------------
#: Publicly known trading-app routing programs.  Their presence means a trading
#: app was used, which is *attention*, not skill and not smart money.
KNOWN_ROUTER_PROGRAMS: dict[str, str] = {
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter",
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB": "Jupiter",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "Pump.fun",
}


@dataclass(frozen=True, slots=True)
class BotActivity:
    """Trading-app/bot transaction share.  Context only (section 24)."""

    mint: str
    total_trades: int = 0
    router_trades: int = 0
    routers: tuple[str, ...] = ()

    @property
    def router_share(self) -> Decimal | None:
        if self.total_trades <= 0:
            return None
        return (Decimal(self.router_trades) / Decimal(self.total_trades)).quantize(
            Decimal("0.01")
        )

    def operator_line(self) -> str:
        share = self.router_share
        if share is None:
            return "trading-app activity: unknown"
        return (
            f"trading-app activity: {share} of observed trades "
            "(attention, not smart money)"
        )

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "total_trades": self.total_trades,
            "router_trades": self.router_trades,
            "routers": list(self.routers),
            "router_share": _s(self.router_share),
        }


def assess_bot_activity(
    mint: str,
    *,
    program_ids_per_trade: Sequence[Sequence[str]],
) -> BotActivity:
    """Count trades routed through known public trading programs."""

    routers: set[str] = set()
    matched = 0
    for programs in program_ids_per_trade:
        names = {KNOWN_ROUTER_PROGRAMS[item] for item in programs if item in KNOWN_ROUTER_PROGRAMS}
        if names:
            matched += 1
            routers |= names
    return BotActivity(
        mint=mint,
        total_trades=len(program_ids_per_trade),
        router_trades=matched,
        routers=tuple(sorted(routers)),
    )
