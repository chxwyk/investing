"""Liquidity computed from reserves, never read off a provider card.

The production failure this exists for: a card said ``$61.8K liquidity`` and the
operator had no way to know whether anything was behind that number.  A provider
figure is a claim.  Two vault balances and a base price are arithmetic.

So nothing here trusts a displayed figure.  The pool is verified — allowlisted
program, the two mints we expect, the vaults that belong to it — the reserves are
read, and the dollar value is *calculated*.  The provider's number is then
compared against the calculation and used for exactly one thing: detecting that
somebody is lying.  A provider that materially exceeds on-chain reserves is a
``DATA CONFLICT``, which is a refusal, not a tiebreak.

Two further things a raw dollar figure cannot tell you, both of which have cost
the operator money:

**Depth is not the same as size.**  A pool can hold $60K and still move 40% on a
$500 sell if the reserves are lopsided.  What matters is executable impact at the
size actually being considered, which is constant-product arithmetic on the
reserves, not a fraction of the headline.

**Liquidity that is present now may have arrived ninety seconds ago.**  Flash
liquidity is added to clear a threshold and withdrawn once the alert has fired.
A single snapshot cannot see that; a short series of reserve observations can,
and that is why stability over a window is required rather than a reading.

Pure logic: no provider, no database, no signer, no order path.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from .hardgates import FAIL, PASS, UNKNOWN, VERIFIED_LIQUIDITY, VERIFIED_POOL, GateResult

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")
CENT = Decimal("0.01")

# --- the only base assets a real Solana pool pairs a memecoin against ---------
# Canonical, publicly documented mints.  A pool whose other side is not one of
# these is either exotic or spoofed, and this lane does not need to tell those
# apart: neither is a base asset we can price a token against.
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

BASE_MINTS: frozenset[str] = frozenset({WSOL_MINT, USDC_MINT, USDT_MINT})

#: Fake-WSOL is a real attack: a mint whose *symbol* is SOL sitting on the other
#: side of a pool so the pair looks normal.  Only the address decides.
BASE_SYMBOLS_ARE_NOT_PROOF = ("SOL", "WSOL", "USDC", "USDT")

# --- reason codes ------------------------------------------------------------
POOL_PROGRAM_NOT_ALLOWED = "POOL_PROGRAM_NOT_ALLOWLISTED"
POOL_MISSING_CANDIDATE = "POOL_DOES_NOT_HOLD_THE_CANDIDATE"
POOL_BASE_NOT_RECOGNISED = "BASE_MINT_NOT_RECOGNISED"
POOL_VAULT_MISMATCH = "VAULTS_DO_NOT_BELONG_TO_THIS_POOL"
LIQUIDITY_DUST = "RESERVES_ARE_DUST"
LIQUIDITY_ONE_SIDED = "LIQUIDITY_IS_ONE_SIDED"
LIQUIDITY_STALE = "RESERVES_TOO_OLD_TO_USE"
LIQUIDITY_PROVIDER_CONFLICT = "PROVIDER_EXCEEDS_ONCHAIN_RESERVES"
LIQUIDITY_FLASH = "FLASH_LIQUIDITY_ADDED_THEN_REMOVED"
LIQUIDITY_UNSTABLE = "LIQUIDITY_NOT_STABLE_LONG_ENOUGH"
LIQUIDITY_WITHDRAWABLE = "TOO_MUCH_LIQUIDITY_WITHDRAWABLE_BY_ONE_PARTY"
IMPACT_TOO_HIGH = "EXECUTABLE_IMPACT_TOO_HIGH"
NO_BASE_PRICE = "NO_TRUSTED_BASE_PRICE"


@dataclass(frozen=True, slots=True)
class PoolAccount:
    """One AMM pool as decoded from chain, not as described by an API."""

    address: str
    program_id: str = ""
    mint_a: str = ""
    mint_b: str = ""
    vault_a: str = ""
    vault_b: str = ""
    #: Raw reserve balances in whole tokens (already decimal-adjusted).
    reserve_a: Decimal | None = None
    reserve_b: Decimal | None = None
    #: Vault owners as read on chain.  Both must be this pool.
    vault_a_owner: str = ""
    vault_b_owner: str = ""
    slot: int | None = None
    observed_at: int | None = None
    #: LP supply held by the creator or an unlocked authority, as a rate 0..1.
    withdrawable_rate: Decimal | None = None
    lp_burned: bool | None = None
    lp_locked_until: int | None = None

    def side_for(self, mint: str) -> str:
        if mint and mint == self.mint_a:
            return "a"
        if mint and mint == self.mint_b:
            return "b"
        return ""

    def reserve_of(self, side: str) -> Decimal | None:
        return self.reserve_a if side == "a" else self.reserve_b if side == "b" else None

    def to_json(self) -> dict[str, object]:
        return {
            "address": self.address,
            "program_id": self.program_id,
            "mint_a": self.mint_a,
            "mint_b": self.mint_b,
            "vault_a": self.vault_a,
            "vault_b": self.vault_b,
            "reserve_a": _s(self.reserve_a),
            "reserve_b": _s(self.reserve_b),
            "slot": self.slot,
            "observed_at": self.observed_at,
            "withdrawable_rate": _s(self.withdrawable_rate),
            "lp_burned": self.lp_burned,
        }


@dataclass(frozen=True, slots=True)
class ReserveObservation:
    """One reading of the base-side reserve, for stability over a window."""

    at: int
    base_reserve: Decimal
    slot: int | None = None


@dataclass(frozen=True, slots=True)
class LiquidityConfig:
    """Floors, tolerances and the window liquidity must survive."""

    allowed_programs: frozenset[str] = field(default_factory=frozenset)
    #: Below this the pool is dust whatever the card says.
    min_liquidity_usd: Decimal = Decimal("4000")
    #: Reserves older than this cannot back a decision.
    max_reserve_age_seconds: int = 90
    #: How far a provider may exceed the calculated figure before it is a
    #: conflict rather than rounding.  1.35 = 35% over.
    provider_tolerance: Decimal = Decimal("1.35")
    #: Liquidity must have held above the floor for this long.
    stability_window_seconds: int = 120
    #: A drop this large inside the window reads as a withdrawal.
    flash_drop_rate: Decimal = Decimal("0.4")
    #: More than this fraction removable by one party is a standing risk.
    max_withdrawable_rate: Decimal = Decimal("0.5")
    #: Paper sizes the executable impact is measured at, in USD.
    probe_sizes_usd: tuple[Decimal, ...] = (Decimal("10"), Decimal("50"))
    #: Impact above this at the largest probe size is not tradeable.
    max_impact_rate: Decimal = Decimal("0.15")


DEFAULT_LIQUIDITY_CONFIG = LiquidityConfig()


@dataclass(frozen=True, slots=True)
class LiquidityProof:
    """What the reserves actually say, and what the provider claimed."""

    mint: str
    pool_address: str = ""
    base_mint: str = ""
    base_reserve: Decimal | None = None
    token_reserve: Decimal | None = None
    base_price_usd: Decimal | None = None
    #: Computed as 2x the base side, the standard constant-product convention.
    computed_liquidity_usd: Decimal | None = None
    provider_liquidity_usd: Decimal | None = None
    #: Impact at each configured probe size, worst last.
    impact_by_size: tuple[tuple[str, str], ...] = ()
    slot: int | None = None
    observed_at: int | None = None
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def provider_overstatement(self) -> Decimal | None:
        if (
            self.provider_liquidity_usd is None
            or self.computed_liquidity_usd is None
            or self.computed_liquidity_usd <= ZERO
        ):
            return None
        return (self.provider_liquidity_usd / self.computed_liquidity_usd).quantize(CENT)

    @property
    def worst_impact(self) -> Decimal | None:
        if not self.impact_by_size:
            return None
        return max(Decimal(value) for _, value in self.impact_by_size)

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "pool_address": self.pool_address,
            "base_mint": self.base_mint,
            "base_reserve": _s(self.base_reserve),
            "token_reserve": _s(self.token_reserve),
            "base_price_usd": _s(self.base_price_usd),
            "computed_liquidity_usd": _s(self.computed_liquidity_usd),
            "provider_liquidity_usd": _s(self.provider_liquidity_usd),
            "provider_overstatement": _s(self.provider_overstatement),
            "impact_by_size": [list(item) for item in self.impact_by_size],
            "worst_impact": _s(self.worst_impact),
            "slot": self.slot,
            "observed_at": self.observed_at,
            "reasons": list(self.reasons),
        }


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def price_impact(
    reserve_in: Decimal, reserve_out: Decimal, amount_in: Decimal
) -> Decimal | None:
    """Constant-product impact for one trade, as a rate 0..1.

    ``x*y=k``: putting ``dx`` in returns ``y*dx/(x+dx)``.  The impact is how far
    the realised rate falls short of the spot rate, which is the number that
    decides whether a $50 exit costs $2 or $20 — and no fraction of a headline
    liquidity figure can stand in for it.
    """

    if reserve_in <= ZERO or reserve_out <= ZERO or amount_in <= ZERO:
        return None
    out = (reserve_out * amount_in) / (reserve_in + amount_in)
    spot_out = amount_in * (reserve_out / reserve_in)
    if spot_out <= ZERO:
        return None
    return max(ZERO, (ONE - (out / spot_out))).quantize(Decimal("0.0001"))


def verify_pool(
    mint: str,
    pool: PoolAccount,
    *,
    config: LiquidityConfig = DEFAULT_LIQUIDITY_CONFIG,
    now: int,
) -> GateResult:
    """Is this a real pool for this exact mint against a real base asset?

    Every check is on an address.  A pool whose other side is a token *called*
    SOL is exactly as wrong as one paired with something random, and only the
    mint address separates the two.
    """

    def fail(code: str, detail: str) -> GateResult:
        return GateResult(
            gate=VERIFIED_POOL,
            answer=FAIL,
            reason=detail,
            source="decoded pool account",
            observed_at=pool.observed_at,
            evidence=(("pool", pool.address), ("code", code)),
        )

    if config.allowed_programs and pool.program_id not in config.allowed_programs:
        return fail(
            POOL_PROGRAM_NOT_ALLOWED,
            f"pool program {pool.program_id[:12]}… is not an allowlisted AMM",
        )
    side = pool.side_for(mint)
    if not side:
        return fail(POOL_MISSING_CANDIDATE, "this pool does not hold the candidate mint")
    base_mint = pool.mint_b if side == "a" else pool.mint_a
    if base_mint not in BASE_MINTS:
        return fail(
            POOL_BASE_NOT_RECOGNISED,
            f"the other side ({base_mint[:12]}…) is not WSOL, USDC or USDT — a "
            "token merely named SOL is not a base asset",
        )
    for owner, label in ((pool.vault_a_owner, "a"), (pool.vault_b_owner, "b")):
        if owner and owner != pool.address:
            return fail(
                POOL_VAULT_MISMATCH,
                f"vault {label} is owned by {owner[:12]}…, not this pool",
            )
    if pool.observed_at is None:
        return GateResult(
            gate=VERIFIED_POOL,
            answer=UNKNOWN,
            reason="pool state carries no observation time",
            source="decoded pool account",
        )
    return GateResult(
        gate=VERIFIED_POOL,
        answer=PASS,
        reason=f"allowlisted pool holding the exact mint against {base_mint[:8]}…",
        source="decoded pool account",
        observed_at=pool.observed_at,
        max_age_seconds=config.max_reserve_age_seconds,
        evidence=(
            ("pool", pool.address),
            ("base_mint", base_mint),
            ("slot", str(pool.slot or "")),
        ),
    )


def prove_liquidity(
    mint: str,
    pool: PoolAccount,
    *,
    base_price_usd: Decimal | None,
    provider_liquidity_usd: Decimal | None = None,
    history: Sequence[ReserveObservation] = (),
    config: LiquidityConfig = DEFAULT_LIQUIDITY_CONFIG,
    now: int,
) -> tuple[GateResult, LiquidityProof]:
    """Compute liquidity from reserves and decide whether it can be trusted.

    Returns the gate *and* the arithmetic behind it, because a refusal the
    operator cannot check is indistinguishable from a bug.
    """

    reasons: list[str] = []
    side = pool.side_for(mint)
    base_side = "b" if side == "a" else "a"
    token_reserve = pool.reserve_of(side)
    base_reserve = pool.reserve_of(base_side)
    base_mint = pool.mint_b if side == "a" else pool.mint_a

    proof = LiquidityProof(
        mint=mint,
        pool_address=pool.address,
        base_mint=base_mint,
        base_reserve=base_reserve,
        token_reserve=token_reserve,
        base_price_usd=base_price_usd,
        provider_liquidity_usd=provider_liquidity_usd,
        slot=pool.slot,
        observed_at=pool.observed_at,
    )

    def unknown(code: str, detail: str) -> tuple[GateResult, LiquidityProof]:
        return (
            GateResult(
                gate=VERIFIED_LIQUIDITY,
                answer=UNKNOWN,
                reason=detail,
                source="on-chain reserves",
                observed_at=pool.observed_at,
                evidence=(("code", code),),
            ),
            proof,
        )

    def fail(code: str, detail: str, **extra: object) -> tuple[GateResult, LiquidityProof]:
        return (
            GateResult(
                gate=VERIFIED_LIQUIDITY,
                answer=FAIL,
                reason=detail,
                source="on-chain reserves",
                observed_at=pool.observed_at,
                evidence=tuple(
                    (str(k), str(v)) for k, v in (("code", code), *extra.items())
                ),
            ),
            proof,
        )

    if not side:
        return fail(POOL_MISSING_CANDIDATE, "this pool does not hold the candidate mint")
    if base_price_usd is None or base_price_usd <= ZERO:
        # Without a trusted base price there is no dollar figure to compute, and
        # borrowing the provider's would defeat the entire point of this module.
        return unknown(NO_BASE_PRICE, "no trusted base-asset price to value reserves with")
    if token_reserve is None or base_reserve is None:
        return unknown(LIQUIDITY_STALE, "reserves were not readable")
    if pool.observed_at is None or (now - pool.observed_at) > config.max_reserve_age_seconds:
        return unknown(
            LIQUIDITY_STALE,
            f"reserves are older than {config.max_reserve_age_seconds}s and cannot "
            "back a decision",
        )

    computed = (base_reserve * base_price_usd * Decimal(2)).quantize(CENT)
    impacts: list[tuple[str, str]] = []
    for size in sorted(config.probe_sizes_usd):
        amount_in_base = size / base_price_usd
        impact = price_impact(base_reserve, token_reserve, amount_in_base)
        if impact is not None:
            impacts.append((f"${size}", str(impact)))
    proof = LiquidityProof(
        **{
            **{f: getattr(proof, f) for f in LiquidityProof.__slots__},
            "computed_liquidity_usd": computed,
            "impact_by_size": tuple(impacts),
        }
    )

    if token_reserve <= ZERO or base_reserve <= ZERO:
        return fail(LIQUIDITY_ONE_SIDED, "one side of this pool is empty")
    if computed < config.min_liquidity_usd:
        return fail(
            LIQUIDITY_DUST,
            f"reserves are worth ${computed} against a ${config.min_liquidity_usd} floor",
            computed=computed,
        )

    # The provider comparison.  Used only to catch a lie, never to raise the
    # computed figure.
    overstatement = proof.provider_overstatement
    if overstatement is not None and overstatement > config.provider_tolerance:
        return fail(
            LIQUIDITY_PROVIDER_CONFLICT,
            f"the provider reports ${provider_liquidity_usd} against ${computed} "
            f"actually in the vaults ({overstatement}x) — DATA CONFLICT",
            computed=computed,
            provider=provider_liquidity_usd,
        )

    # Flash liquidity, which a single snapshot cannot see.
    flash = _flash_liquidity(history, config=config, now=now)
    if flash is not None:
        return fail(LIQUIDITY_FLASH, flash)
    stable = _stable_for(history, floor_base=config.min_liquidity_usd / (base_price_usd * 2),
                         config=config, now=now)
    if stable is False:
        return unknown(
            LIQUIDITY_UNSTABLE,
            f"liquidity has not held above the floor for "
            f"{config.stability_window_seconds}s — a brand-new pool is a watch, "
            "not proven depth",
        )

    if (
        pool.withdrawable_rate is not None
        and pool.withdrawable_rate > config.max_withdrawable_rate
    ):
        return fail(
            LIQUIDITY_WITHDRAWABLE,
            f"{(pool.withdrawable_rate * HUNDRED).quantize(CENT)}% of this pool is "
            "removable by one party",
        )

    worst = proof.worst_impact
    if worst is not None and worst > config.max_impact_rate:
        return fail(
            IMPACT_TOO_HIGH,
            f"a ${max(config.probe_sizes_usd)} exit moves the price "
            f"{(worst * HUNDRED).quantize(CENT)}% — depth is not size",
            impact=worst,
        )

    if overstatement is not None:
        reasons.append(f"provider within tolerance ({overstatement}x computed)")
    return (
        GateResult(
            gate=VERIFIED_LIQUIDITY,
            answer=PASS,
            reason=f"${computed} computed from vault reserves at slot {pool.slot}",
            source="on-chain reserves",
            observed_at=pool.observed_at,
            max_age_seconds=config.max_reserve_age_seconds,
            evidence=(
                ("computed_usd", str(computed)),
                ("base_reserve", str(base_reserve)),
                ("base_price_usd", str(base_price_usd)),
                ("provider_usd", str(provider_liquidity_usd or "")),
                ("worst_impact", str(worst or "")),
                ("slot", str(pool.slot or "")),
            ),
        ),
        proof,
    )


def _flash_liquidity(
    history: Sequence[ReserveObservation],
    *,
    config: LiquidityConfig,
    now: int,
) -> str | None:
    """Liquidity added to clear a bar and pulled once the alert fired."""

    window = [item for item in history if now - item.at <= config.stability_window_seconds]
    if len(window) < 2:
        return None
    ordered = sorted(window, key=lambda item: item.at)
    peak = max(item.base_reserve for item in ordered)
    latest = ordered[-1].base_reserve
    if peak <= ZERO:
        return None
    drop = (peak - latest) / peak
    if drop >= config.flash_drop_rate:
        return (
            f"base reserves peaked at {peak} and are now {latest} "
            f"({(drop * HUNDRED).quantize(CENT)}% removed inside "
            f"{config.stability_window_seconds}s)"
        )
    return None


def _stable_for(
    history: Sequence[ReserveObservation],
    *,
    floor_base: Decimal,
    config: LiquidityConfig,
    now: int,
) -> bool | None:
    """Whether liquidity held above the floor for the whole window.

    ``None`` when there is not enough history to say — which is honestly
    different from "it dipped", and is why a brand-new pool becomes a watch
    rather than a refusal.
    """

    if not history:
        return None
    ordered = sorted(history, key=lambda item: item.at)
    span = now - ordered[0].at
    if span < config.stability_window_seconds:
        return False
    window = [item for item in ordered if now - item.at <= config.stability_window_seconds]
    return all(item.base_reserve >= floor_base for item in window) if window else None
