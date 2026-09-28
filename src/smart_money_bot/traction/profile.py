"""The Early Traction profile: the operator's Axiom Discover filters, exactly.

This module is a deliberate translation of a specific screen the operator
already uses by hand:

    Solana · launchpads {Pump, Bags, Bonk, LiquidAF, Heaven} · pre- and
    post-migration · age <= 25m · market cap >= $8,000 · volume >= $5,000 ·
    has a Twitter/X link · Dex-paid not required

The single most important property of that list is **what is not in it**.  There
is no top-10 limit, no dev-holding limit, no insider or bundler ceiling, and no
holder-count floor — those fields are blank on the operator's screen, and this
module therefore does not filter on them.  It would be very easy to "improve"
this profile by adding a safety gate, and that would silently produce a
different screen from the one being replicated, which is worse than useless
because it would look like it worked.

Safety is still computed, and it still reaches the alert — as a separate,
clearly-labelled block that never participates in the pass/fail decision.  See
:mod:`smart_money_bot.traction.safety`.  The split is the point: the operator
wants to see a token *and* see what is wrong with it, rather than not see it.

Everything here is pure and cheap by construction.  These are the checks that
run in memory on stream data for every new mint, so nothing in this module may
make a network call, touch a database, or cost a provider credit.  The expensive
work happens only for tokens that pass, which is what makes the fast path fast.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .launchpads import (
    MIGRATION_UNKNOWN,
    POST_MIGRATION,
    PRE_MIGRATION,
    LaunchpadRegistry,
)

ZERO = Decimal("0")

# --- why a candidate did not qualify ----------------------------------------
REASON_LAUNCHPAD = "LAUNCHPAD_NOT_IN_PROFILE"
REASON_CHAIN = "CHAIN_NOT_IN_PROFILE"
REASON_AGE = "OLDER_THAN_MAX_AGE"
REASON_AGE_UNKNOWN = "AGE_UNKNOWN"
REASON_MARKET_CAP = "MARKET_CAP_BELOW_FLOOR"
REASON_MARKET_CAP_UNKNOWN = "MARKET_CAP_UNKNOWN"
REASON_VOLUME = "VOLUME_BELOW_FLOOR"
REASON_VOLUME_UNKNOWN = "VOLUME_UNKNOWN"
REASON_NO_X_LINK = "NO_X_LINK"
REASON_MIGRATION_EXCLUDED = "MIGRATION_STATE_EXCLUDED"

#: Reasons that mean "not yet", as opposed to "no".  A token below the market-cap
#: floor at two minutes old may clear it at four; a token older than the age
#: ceiling never gets younger.  The runtime keeps the first group in its pool and
#: evicts the second, which is the difference between a screen and a snapshot.
RETRYABLE_REASONS: frozenset[str] = frozenset(
    {
        REASON_MARKET_CAP,
        REASON_MARKET_CAP_UNKNOWN,
        REASON_VOLUME,
        REASON_VOLUME_UNKNOWN,
        REASON_NO_X_LINK,
        REASON_AGE_UNKNOWN,
    }
)

#: Reasons that are permanent for this mint.
TERMINAL_REASONS: frozenset[str] = frozenset(
    {REASON_LAUNCHPAD, REASON_CHAIN, REASON_AGE, REASON_MIGRATION_EXCLUDED}
)

CHAIN_SOLANA = "solana"
CHAIN_INK = "ink"


@dataclass(frozen=True, slots=True)
class TractionProfile:
    """The thresholds, every one of them operator-configurable."""

    max_age_seconds: int = 1_500          # 25 minutes
    min_market_cap_usd: Decimal = Decimal("8000")
    min_volume_usd: Decimal = Decimal("5000")
    require_x_link: bool = True
    #: Dex-paid is explicitly NOT required by the operator's screen.
    require_dex_paid: bool = False
    include_pre_migration: bool = True
    include_post_migration: bool = True
    chains: frozenset[str] = frozenset({CHAIN_SOLANA})
    #: Which volume window the floor applies to, for the card to state honestly.
    volume_window: str = "5m"

    def to_json(self) -> dict[str, Any]:
        return {
            "max_age_seconds": self.max_age_seconds,
            "min_market_cap_usd": str(self.min_market_cap_usd),
            "min_volume_usd": str(self.min_volume_usd),
            "require_x_link": self.require_x_link,
            "require_dex_paid": self.require_dex_paid,
            "include_pre_migration": self.include_pre_migration,
            "include_post_migration": self.include_post_migration,
            "chains": sorted(self.chains),
            "volume_window": self.volume_window,
            # Stated in the payload so an operator reading /status can see that
            # the absence of safety gates is deliberate, not an oversight.
            "safety_gates": "NONE — safety is reported, never filtered on",
        }


DEFAULT_PROFILE = TractionProfile()


@dataclass(frozen=True, slots=True)
class TractionObservation:
    """What the cheap path knows about one mint.  Every field may be unknown."""

    mint: str
    launchpad: str = ""
    chain: str = CHAIN_SOLANA
    #: On-chain creation time.  The anchor every latency number is measured from.
    chain_created_at: int | None = None
    market_cap_usd: Decimal | None = None
    volume_usd: Decimal | None = None
    liquidity_usd: Decimal | None = None
    price_usd: Decimal | None = None
    migration_state: str = MIGRATION_UNKNOWN
    x_link: str = ""
    dex_paid: bool | None = None
    name: str = ""
    symbol: str = ""
    #: When our collector first saw this mint at all.
    first_seen_at: int | None = None

    def age_seconds(self, *, now: int) -> int | None:
        if self.chain_created_at is None:
            return None
        return max(0, now - self.chain_created_at)


@dataclass(frozen=True, slots=True)
class ProfileVerdict:
    """Whether a mint matches the profile, and precisely why not when it does not."""

    mint: str
    qualifies: bool
    reasons: tuple[str, ...] = ()
    #: Values that were checked, for the card and for later threshold tuning.
    measured: dict[str, Any] = field(default_factory=dict)

    @property
    def retryable(self) -> bool:
        """Whether this mint is worth re-checking as it develops.

        True when every failing reason is a "not yet".  A single terminal reason
        makes the whole verdict terminal, because no amount of traction makes a
        token younger or moves it to a different launchpad.
        """

        if self.qualifies:
            return False
        return bool(self.reasons) and all(
            reason in RETRYABLE_REASONS for reason in self.reasons
        )

    @property
    def terminal(self) -> bool:
        return not self.qualifies and not self.retryable

    def to_json(self) -> dict[str, Any]:
        return {
            "mint": self.mint,
            "qualifies": self.qualifies,
            "reasons": list(self.reasons),
            "retryable": self.retryable,
            "terminal": self.terminal,
            "measured": dict(self.measured),
        }


def evaluate(
    observation: TractionObservation,
    *,
    registry: LaunchpadRegistry,
    profile: TractionProfile = DEFAULT_PROFILE,
    now: int,
) -> ProfileVerdict:
    """Apply the profile.  Pure, allocation-light, no I/O.

    Unknown values fail the check they belong to rather than passing it.  A token
    whose market cap we cannot read has not demonstrated an $8,000 market cap,
    and admitting it "because we are not sure" would quietly widen the screen the
    operator asked to replicate.  Those failures are retryable, so an unknown
    that resolves a few seconds later still qualifies.
    """

    reasons: list[str] = []
    measured: dict[str, Any] = {}

    # --- chain ----------------------------------------------------------
    chain = (observation.chain or CHAIN_SOLANA).strip().lower()
    measured["chain"] = chain
    if chain not in profile.chains:
        reasons.append(REASON_CHAIN)

    # --- launchpad ------------------------------------------------------
    measured["launchpad"] = observation.launchpad
    if not registry.accepts(observation.launchpad):
        reasons.append(REASON_LAUNCHPAD)

    # --- migration state ------------------------------------------------
    measured["migration_state"] = observation.migration_state
    excluded_side = (
        observation.migration_state == PRE_MIGRATION and not profile.include_pre_migration
    ) or (
        observation.migration_state == POST_MIGRATION
        and not profile.include_post_migration
    )
    if excluded_side:
        reasons.append(REASON_MIGRATION_EXCLUDED)
    # MIGRATION_UNKNOWN is admitted: the operator's screen covers both sides, so
    # not knowing which side does not exclude the token from either.

    # --- age ------------------------------------------------------------
    age = observation.age_seconds(now=now)
    measured["age_seconds"] = age
    if age is None:
        reasons.append(REASON_AGE_UNKNOWN)
    elif age > profile.max_age_seconds:
        reasons.append(REASON_AGE)

    # --- market cap -----------------------------------------------------
    measured["market_cap_usd"] = (
        None if observation.market_cap_usd is None else str(observation.market_cap_usd)
    )
    if observation.market_cap_usd is None:
        reasons.append(REASON_MARKET_CAP_UNKNOWN)
    elif observation.market_cap_usd < profile.min_market_cap_usd:
        reasons.append(REASON_MARKET_CAP)

    # --- volume ---------------------------------------------------------
    measured["volume_usd"] = (
        None if observation.volume_usd is None else str(observation.volume_usd)
    )
    measured["volume_window"] = profile.volume_window
    if observation.volume_usd is None:
        reasons.append(REASON_VOLUME_UNKNOWN)
    elif observation.volume_usd < profile.min_volume_usd:
        reasons.append(REASON_VOLUME)

    # --- X link ---------------------------------------------------------
    # Presence only.  Whether the account is real, a community page, or a reused
    # link is a separate question answered in traction.xlink -- and one that
    # costs a lookup, so it is deliberately not on the fast path.
    measured["x_link"] = observation.x_link
    if profile.require_x_link and not observation.x_link.strip():
        reasons.append(REASON_NO_X_LINK)

    # --- dex paid -------------------------------------------------------
    # Recorded, never required: the operator's screen leaves it off.
    measured["dex_paid"] = observation.dex_paid

    return ProfileVerdict(
        mint=observation.mint,
        qualifies=not reasons,
        reasons=tuple(reasons),
        measured=measured,
    )
