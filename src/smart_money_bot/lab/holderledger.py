"""Holders reconstructed from Transfer logs, not read off a card.

A provider's holder number is a claim, and the operator has now been burned by
one twice: a card that said nothing about holders for a token FOMO showed as
"No holders yet", and a board figure that no independent source could confirm.
The only auditable answer is a ledger — every ``Transfer`` since the launch
block, applied in order, with the balance set that falls out of it.

Three ideas do the work here.

**A wallet is not a holder.**  A positive balance can belong to the pool's own
vault, a router mid-hop, the token contract, a burn address, or a locker.  None
of those is a person who bought this.  So the ledger keeps two counts and shows
both: ``raw`` (every address with a positive balance, which is what an explorer
prints) and ``economic`` (what is left after the machinery is removed).  Nothing
is silently dropped — an address we could not classify stays in raw and is
flagged, because an unexplained exclusion is indistinguishable from a bug.

**A wallet is not an actor.**  Fifty addresses funded by one wallet are one
person holding through fifty envelopes.  Clustering collapses them for counting
and for concentration, which is the difference between "broadly held" and "one
man and his scripts".

**Excluding the machinery must not excuse the insiders.**  Removing the LP vault
from the holder count is correct; letting that also erase the creator's stack
would be the fix quietly undoing the protection.  Creator, sniper, bundler and
insider balances are tracked separately and never netted away.

Reorgs and restarts are ordinary here, not edge cases.  Every applied log is
identified by transaction hash and log index, so replaying a range changes
nothing, and a removed log is rolled back rather than left in the balances.

Pure logic: no provider, no RPC client, no database, no signer.  This module is
handed logs and returns a ledger.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal

ZERO = Decimal("0")
HUNDRED = Decimal("100")
CENT = Decimal("0.01")

#: Addresses that are the absence of a holder rather than one.
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
DEAD_ADDRESS = "0x000000000000000000000000000000000000dead"
BURN_ADDRESSES: frozenset[str] = frozenset({ZERO_ADDRESS, DEAD_ADDRESS})

# --- why an address is not counted as an economic holder ---------------------
EX_BURN = "BURN_OR_ZERO_ADDRESS"
EX_SELF = "TOKEN_CONTRACT_ITSELF"
EX_POOL = "POOL_OR_VAULT"
EX_SYSTEM = "ROUTER_FACTORY_BRIDGE_OR_LOCKER"
EX_EMPTY = "ZERO_BALANCE"
EX_CLUSTER = "SAME_ECONOMIC_ACTOR_AS_ANOTHER_ADDRESS"
EX_UNCLASSIFIED = "UNCLASSIFIED_CONTRACT_FLAGGED_FOR_REVIEW"

HUMAN_EXCLUSION: dict[str, str] = {
    EX_BURN: "burn or zero address",
    EX_SELF: "the token contract itself",
    EX_POOL: "a pool or vault holding the other side of the market",
    EX_SYSTEM: "a router, factory, bridge or locker",
    EX_EMPTY: "balance is zero",
    EX_CLUSTER: "the same economic actor as another counted address",
    EX_UNCLASSIFIED: "an unclassified contract — kept in raw and flagged",
}

# --- risk tags that survive every exclusion ----------------------------------
TAG_CREATOR = "creator"
TAG_INSIDER = "insider"
TAG_SNIPER = "sniper"
TAG_BUNDLER = "bundler"
RISK_TAGS: tuple[str, ...] = (TAG_CREATOR, TAG_INSIDER, TAG_SNIPER, TAG_BUNDLER)


def normalise(address: object) -> str:
    return str(address or "").strip().lower()


@dataclass(frozen=True, slots=True)
class TransferLog:
    """One decoded ERC-20 ``Transfer``, with the identity that makes it replayable."""

    transaction_hash: str
    log_index: int
    block_number: int
    from_address: str
    to_address: str
    value: Decimal
    at: int | None = None
    #: Set when the node reports this log as removed by a reorg.
    removed: bool = False

    @property
    def identity(self) -> str:
        return f"{normalise(self.transaction_hash)}:{self.log_index}"

    @property
    def is_mint(self) -> bool:
        return normalise(self.from_address) in BURN_ADDRESSES

    @property
    def is_burn(self) -> bool:
        return normalise(self.to_address) in BURN_ADDRESSES


@dataclass(frozen=True, slots=True)
class AddressRole:
    """What an address is, when we have been able to establish it.

    ``is_contract`` matters on its own: an unclassified *contract* is suspicious
    enough to flag, while an unclassified EOA is just a wallet.
    """

    address: str
    is_pool: bool = False
    is_system: bool = False
    is_contract: bool = False
    cluster_id: str = ""
    tags: tuple[str, ...] = ()

    @property
    def risky(self) -> bool:
        return any(tag in RISK_TAGS for tag in self.tags)


@dataclass(frozen=True, slots=True)
class LedgerConfig:
    """What counts, and how far a provider may disagree before it is a conflict."""

    #: Balances below this are dust rather than a position.  Expressed in whole
    #: tokens; a holder with one billionth of a token is not a holder.
    dust_balance: Decimal = Decimal("0")
    #: Provider holder count over reconstructed, above which it is a conflict.
    provider_tolerance: Decimal = Decimal("1.25")
    #: And the absolute gap below which a ratio is not worth arguing about.
    provider_absolute_slack: int = 5


DEFAULT_LEDGER_CONFIG = LedgerConfig()


@dataclass(frozen=True, slots=True)
class HolderLedger:
    """Reconstructed balances at a known block, plus everything derived from them.

    Immutable: applying logs returns a new ledger.  That is what makes a replay
    of stored snapshots produce the historical answer rather than today's.
    """

    token: str
    balances: dict[str, Decimal] = field(default_factory=dict)
    applied: frozenset[str] = field(default_factory=frozenset)
    last_block: int | None = None
    last_block_hash: str = ""
    observed_at: int | None = None
    roles: dict[str, AddressRole] = field(default_factory=dict)
    total_minted: Decimal = ZERO
    total_burned: Decimal = ZERO

    # ---- the two counts, both shown ------------------------------------
    @property
    def raw_holders(self) -> tuple[str, ...]:
        """Every address with a positive balance — what an explorer prints."""

        return tuple(sorted(a for a, b in self.balances.items() if b > ZERO))

    def exclusion_for(
        self, address: str, *, config: LedgerConfig = DEFAULT_LEDGER_CONFIG
    ) -> str:
        """Why this address is not an economic holder, or "" if it is one."""

        key = normalise(address)
        balance = self.balances.get(key, ZERO)
        if key in BURN_ADDRESSES:
            return EX_BURN
        if key == normalise(self.token):
            return EX_SELF
        if balance <= config.dust_balance:
            return EX_EMPTY
        role = self.roles.get(key)
        if role is not None:
            if role.is_pool:
                return EX_POOL
            if role.is_system:
                return EX_SYSTEM
        return ""

    def economic_holders(
        self, *, config: LedgerConfig = DEFAULT_LEDGER_CONFIG
    ) -> tuple[str, ...]:
        """Addresses that plausibly represent a person holding this token.

        One address per economic actor: clustered wallets collapse to their
        first address by sort order, deterministically, so the count does not
        depend on iteration order.
        """

        kept: dict[str, str] = {}
        for address in self.raw_holders:
            if self.exclusion_for(address, config=config):
                continue
            role = self.roles.get(address)
            actor = (role.cluster_id if role and role.cluster_id else address)
            kept.setdefault(actor, address)
        return tuple(sorted(kept.values()))

    @property
    def unclassified_contracts(self) -> tuple[str, ...]:
        """Contracts holding a balance that we could not identify.

        Deliberately surfaced rather than dropped: an exclusion nobody can
        explain and a bug look exactly the same from outside.
        """

        return tuple(
            address
            for address in self.raw_holders
            if (role := self.roles.get(address)) is not None
            and role.is_contract
            and not role.is_pool
            and not role.is_system
        )

    def balance_of(self, address: str) -> Decimal:
        return self.balances.get(normalise(address), ZERO)

    # ---- risk exposure, which exclusions must never erase ---------------
    def tagged_balance(self, tag: str) -> Decimal:
        return sum(
            (
                balance
                for address, balance in self.balances.items()
                if balance > ZERO
                and (role := self.roles.get(address)) is not None
                and tag in role.tags
            ),
            ZERO,
        )

    @property
    def circulating(self) -> Decimal:
        return self.total_minted - self.total_burned

    def concentration(
        self, top: int, *, cluster_adjusted: bool, config: LedgerConfig = DEFAULT_LEDGER_CONFIG
    ) -> Decimal | None:
        """Share of circulating supply held by the largest ``top`` holders.

        ``cluster_adjusted`` sums each economic actor's addresses together
        first, which is what turns "no single wallet holds much" back into the
        truth when one person is spread across twenty of them.
        """

        if self.circulating <= ZERO:
            return None
        pools: dict[str, Decimal] = {}
        for address in self.raw_holders:
            if self.exclusion_for(address, config=config) in {EX_BURN, EX_SELF, EX_POOL}:
                # Machinery is not supply anyone is holding.  System routers are
                # left in: a router sitting on a balance is a real overhang.
                continue
            role = self.roles.get(address)
            key = (
                (role.cluster_id or address)
                if cluster_adjusted and role and role.cluster_id
                else address
            )
            pools[key] = pools.get(key, ZERO) + self.balances[address]
        if not pools:
            return None
        largest = sorted(pools.values(), reverse=True)[:top]
        return (sum(largest, ZERO) / self.circulating).quantize(Decimal("0.0001"))

    def to_json(self, *, config: LedgerConfig = DEFAULT_LEDGER_CONFIG) -> dict[str, object]:
        economic = self.economic_holders(config=config)
        return {
            "token": self.token,
            "last_block": self.last_block,
            "observed_at": self.observed_at,
            "raw_positive_balance_addresses": len(self.raw_holders),
            "economic_holder_count": len(economic),
            "unclassified_contracts": list(self.unclassified_contracts),
            "circulating": str(self.circulating),
            "top_1": _s(self.concentration(1, cluster_adjusted=False, config=config)),
            "top_5": _s(self.concentration(5, cluster_adjusted=False, config=config)),
            "top_10": _s(self.concentration(10, cluster_adjusted=False, config=config)),
            "top_20": _s(self.concentration(20, cluster_adjusted=False, config=config)),
            "cluster_adjusted_top_10": _s(
                self.concentration(10, cluster_adjusted=True, config=config)
            ),
            **{
                f"{tag}_balance": str(self.tagged_balance(tag)) for tag in RISK_TAGS
            },
        }


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def apply_logs(
    ledger: HolderLedger,
    logs: Iterable[TransferLog],
    *,
    roles: dict[str, AddressRole] | None = None,
    observed_at: int | None = None,
) -> HolderLedger:
    """Fold transfers into balances.  Idempotent, and reorg-aware.

    A log already applied is skipped, so overlapping ranges and retried scans
    change nothing.  A log marked ``removed`` is reversed and forgotten, so the
    next scan of that range can apply the replacement.
    """

    balances = dict(ledger.balances)
    applied = set(ledger.applied)
    minted, burned = ledger.total_minted, ledger.total_burned
    last_block = ledger.last_block

    for log in sorted(logs, key=lambda item: (item.block_number, item.log_index)):
        identity = log.identity
        if log.removed:
            if identity not in applied:
                continue
            _move(balances, log.to_address, -log.value)
            _move(balances, log.from_address, log.value)
            if log.is_mint:
                minted -= log.value
            if log.is_burn:
                burned -= log.value
            applied.discard(identity)
            continue
        if identity in applied:
            continue
        _move(balances, log.from_address, -log.value)
        _move(balances, log.to_address, log.value)
        if log.is_mint:
            minted += log.value
        if log.is_burn:
            burned += log.value
        applied.add(identity)
        last_block = log.block_number if last_block is None else max(last_block, log.block_number)

    # Burn and zero addresses accumulate the counterparty side of every mint and
    # burn.  They are not balances anybody holds, so they are not kept.
    for address in BURN_ADDRESSES:
        balances.pop(address, None)

    return replace(
        ledger,
        balances={a: b for a, b in balances.items() if b != ZERO},
        applied=frozenset(applied),
        last_block=last_block,
        total_minted=minted,
        total_burned=burned,
        roles={**ledger.roles, **(roles or {})},
        observed_at=observed_at if observed_at is not None else ledger.observed_at,
    )


def _move(balances: dict[str, Decimal], address: object, delta: Decimal) -> None:
    key = normalise(address)
    if not key:
        return
    balances[key] = balances.get(key, ZERO) + delta


def rollback_to(
    ledger: HolderLedger, block_number: int, logs: Sequence[TransferLog]
) -> HolderLedger:
    """Undo everything above ``block_number``.  Used on a detected reorg.

    Takes the logs it previously applied rather than guessing: a ledger that
    tried to reverse a transfer it never saw would corrupt itself worse than
    the reorg did.
    """

    doomed = [item for item in logs if item.block_number > block_number]
    reversed_logs = [replace(item, removed=True) for item in doomed]
    rolled = apply_logs(ledger, reversed_logs)
    return replace(rolled, last_block=block_number)


# --- provider corroboration ---------------------------------------------------
HOLDER_DATA_CONFLICT = "HOLDER_DATA_CONFLICT"


@dataclass(frozen=True, slots=True)
class HolderComparison:
    """What we reconstructed against what a provider claims."""

    reconstructed: int
    provider: int | None = None
    conflict: bool = False
    detail: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "reconstructed": self.reconstructed,
            "provider_reported_holder_count": self.provider,
            "provider_vs_onchain_difference": (
                None if self.provider is None else self.provider - self.reconstructed
            ),
            "conflict": self.conflict,
            "code": HOLDER_DATA_CONFLICT if self.conflict else "",
            "detail": self.detail,
        }


def compare_with_provider(
    ledger: HolderLedger,
    provider_count: int | None,
    *,
    config: LedgerConfig = DEFAULT_LEDGER_CONFIG,
) -> HolderComparison:
    """A provider may corroborate the ledger; it may never overrule it.

    A modest gap is expected — providers count differently and lag — so only a
    material one is a conflict, and a conflict denies strong classification
    rather than picking the larger number.
    """

    reconstructed = len(ledger.economic_holders(config=config))
    if provider_count is None:
        return HolderComparison(reconstructed=reconstructed)
    gap = abs(provider_count - reconstructed)
    if gap <= config.provider_absolute_slack:
        return HolderComparison(reconstructed=reconstructed, provider=provider_count)
    if reconstructed <= 0:
        return HolderComparison(
            reconstructed=reconstructed,
            provider=provider_count,
            conflict=True,
            detail=(
                f"a provider reports {provider_count} holders and the chain ledger "
                "reconstructs none"
            ),
        )
    ratio = Decimal(provider_count) / Decimal(reconstructed)
    if ratio > config.provider_tolerance or ratio < (1 / config.provider_tolerance):
        return HolderComparison(
            reconstructed=reconstructed,
            provider=provider_count,
            conflict=True,
            detail=(
                f"a provider reports {provider_count} holders against {reconstructed} "
                f"reconstructed from Transfer logs ({ratio.quantize(CENT)}x)"
            ),
        )
    return HolderComparison(reconstructed=reconstructed, provider=provider_count)
