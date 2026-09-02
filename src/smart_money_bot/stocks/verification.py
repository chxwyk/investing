"""A name is never an anchor.  Only an address is.

The single rule this whole lane rests on, and the one the operator has been
asking for since the first fake coin: a meme token is linked to a stock **only**
when an address-level proof succeeds.

Exactly two proofs count:

1. **Launch record.**  A verified launchpad factory emitted a launch whose
   paired/anchor token address is an active chain-4663 deployment in Robinhood's
   ``/rhj/assets``.
2. **Canonical pool.**  The pool created by that verified launch holds two
   sides: the meme contract, and an active Robinhood Stock Token contract.

Nothing else is admissible.  Not the ticker, not the name, not the description,
not the logo, not the website, not an X account, not the creator's claim, not a
FOMO or terminal card, not a same-symbol token that happens to exist, and not
another coin that already got verified using a similar name.  Those may add
*context* to a card once the address proof has already passed, and they may
never create one.

The consequence is deliberate and worth stating plainly, because it is the exact
case the operator kept being shown: **a costume token with more liquidity, more
volume and a higher FDV than the genuine one is still rejected.**  Depth is not
evidence of a relationship that does not exist.

A note on the factory side.  A launch record only counts when it came from a
factory this bot has *verified* — see :mod:`.launchpads`.  An unverified or
lookalike contract can emit an event with any shape it likes, including a
perfectly formed one naming a real stock token, so trusting an event because it
parsed would hand anybody a way to mint proofs.

Pure logic: no provider, no database, no signer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .registry import StockRegistry, StockToken, normalise_address

# --- proof types, strongest first --------------------------------------------
#: The verified launch record named an anchor address that is in the registry.
PROOF_LAUNCH_RECORD = "LAUNCH_RECORD_ANCHOR"
#: The canonical pool from the verified launch pairs the meme with a stock token.
PROOF_POOL_PAIRING = "CANONICAL_POOL_PAIRING"
#: No address-level proof.  This is the default and it is not a near-miss.
NOT_STOCK_LINKED = "NOT_STOCK_LINKED"

PROOFS: tuple[str, ...] = (PROOF_LAUNCH_RECORD, PROOF_POOL_PAIRING)

HUMAN_PROOF: dict[str, str] = {
    PROOF_LAUNCH_RECORD: (
        "the launch record from a verified factory names this exact stock-token address"
    ),
    PROOF_POOL_PAIRING: (
        "the pool created by the verified launch holds this exact stock-token address"
    ),
    NOT_STOCK_LINKED: "no address-level link to any Robinhood Stock Token",
}

# --- why a claim failed, so a card can say it --------------------------------
REASON_NO_CLAIM = "NO_ANCHOR_CLAIMED"
REASON_UNVERIFIED_FACTORY = "FACTORY_NOT_VERIFIED"
REASON_ANCHOR_NOT_A_STOCK = "ANCHOR_ADDRESS_NOT_IN_REGISTRY"
REASON_ANCHOR_INACTIVE = "STOCK_TOKEN_NOT_ACTIVE"
REASON_REGISTRY_UNUSABLE = "STOCK_REGISTRY_STALE_OR_MISSING"
REASON_POOL_MISMATCH = "POOL_DOES_NOT_HOLD_THE_MEME"
REASON_NAME_ONLY = "TICKER_RESEMBLANCE_ONLY"

HUMAN_REASON: dict[str, str] = {
    REASON_NO_CLAIM: "the launch named no anchor token at all",
    REASON_UNVERIFIED_FACTORY: (
        "this launch came from a contract we have not verified — an unverified "
        "factory can emit any event it likes, including a convincing one"
    ),
    REASON_ANCHOR_NOT_A_STOCK: (
        "the anchor address is not an active chain-4663 Robinhood Stock Token"
    ),
    REASON_ANCHOR_INACTIVE: "Robinhood no longer lists that stock token as active",
    REASON_REGISTRY_UNUSABLE: (
        "the stock-token registry is missing or too old to vouch for any address"
    ),
    REASON_POOL_MISMATCH: "the pool does not hold this meme token",
    REASON_NAME_ONLY: (
        "only the ticker, name or branding suggests a stock — that is a claim, not a link"
    ),
}


@dataclass(frozen=True, slots=True)
class LaunchRecord:
    """One launch, exactly as decoded from a launchpad factory log.

    ``factory_verified`` travels with the record rather than being looked up
    later, so a record decoded from an unverified contract cannot be handed to
    a caller that forgot to check.
    """

    meme_address: str
    launchpad: str = ""
    factory_address: str = ""
    #: Whether the emitting factory passed bytecode/ABI verification.
    factory_verified: bool = False
    #: Anchor addresses the launch record itself supplies.  Pair V5 pairs a meme
    #: with one to five eligible stock tokens, so this is a list.
    anchor_addresses: tuple[str, ...] = ()
    pool_address: str = ""
    #: The two sides of the canonical pool, when read from chain.
    pool_token_addresses: tuple[str, ...] = ()
    transaction_hash: str = ""
    log_index: int | None = None
    block_number: int | None = None
    launched_at: int | None = None
    deployer: str = ""

    @property
    def identity(self) -> str:
        """Chain-unique launch identity, for deduplication across restarts."""

        return f"{self.transaction_hash}:{self.log_index}".lower()

    def to_json(self) -> dict[str, object]:
        return {
            "meme_address": self.meme_address,
            "launchpad": self.launchpad,
            "factory_address": self.factory_address,
            "factory_verified": self.factory_verified,
            "anchor_addresses": list(self.anchor_addresses),
            "pool_address": self.pool_address,
            "pool_token_addresses": list(self.pool_token_addresses),
            "transaction_hash": self.transaction_hash,
            "log_index": self.log_index,
            "block_number": self.block_number,
            "launched_at": self.launched_at,
            "deployer": self.deployer,
        }


@dataclass(frozen=True, slots=True)
class AnchorProof:
    """Whether this meme is genuinely linked to a stock, and how we know."""

    meme_address: str
    proof: str = NOT_STOCK_LINKED
    #: Every stock token proven, in registry order.  Pair launches can hold more
    #: than one, and dropping the extras would misreport the relationship.
    anchors: tuple[StockToken, ...] = ()
    reasons: tuple[str, ...] = field(default_factory=tuple)
    launchpad: str = ""
    transaction_hash: str = ""

    @property
    def verified(self) -> bool:
        return self.proof in PROOFS and bool(self.anchors)

    @property
    def primary(self) -> StockToken | None:
        return self.anchors[0] if self.anchors else None

    def human(self) -> str:
        return HUMAN_PROOF.get(self.proof, self.proof)

    def to_json(self) -> dict[str, object]:
        return {
            "meme_address": self.meme_address,
            "proof": self.proof,
            "human": self.human(),
            "verified": self.verified,
            "anchors": [token.to_json() for token in self.anchors],
            "reasons": [HUMAN_REASON.get(item, item) for item in self.reasons],
            "reason_codes": list(self.reasons),
            "launchpad": self.launchpad,
            "transaction_hash": self.transaction_hash,
        }


def verify_anchor(
    launch: LaunchRecord,
    registry: StockRegistry,
    *,
    now: int,
) -> AnchorProof:
    """Prove — or refuse to prove — that this meme is anchored to a real stock.

    Order matters.  The factory is checked before anything it said is read,
    because an unverified contract's event is not evidence of its own contents.
    """

    def fail(*codes: str) -> AnchorProof:
        return AnchorProof(
            meme_address=launch.meme_address,
            proof=NOT_STOCK_LINKED,
            reasons=codes,
            launchpad=launch.launchpad,
            transaction_hash=launch.transaction_hash,
        )

    if not launch.factory_verified:
        return fail(REASON_UNVERIFIED_FACTORY)
    if not registry.usable(now):
        return fail(REASON_REGISTRY_UNUSABLE)

    meme_key = normalise_address(launch.meme_address)

    # --- proof 1: the launch record named the anchor ------------------------
    named = _resolve_all(launch.anchor_addresses, registry, now=now, exclude=meme_key)
    if named:
        return AnchorProof(
            meme_address=launch.meme_address,
            proof=PROOF_LAUNCH_RECORD,
            anchors=named,
            launchpad=launch.launchpad,
            transaction_hash=launch.transaction_hash,
        )

    # --- proof 2: the canonical pool pairs it with one ----------------------
    if launch.pool_token_addresses:
        sides = {normalise_address(item) for item in launch.pool_token_addresses}
        if meme_key not in sides:
            # A pool that does not contain the meme is a pool for something
            # else, however impressive its other side looks.
            return fail(REASON_POOL_MISMATCH)
        paired = _resolve_all(
            tuple(launch.pool_token_addresses), registry, now=now, exclude=meme_key
        )
        if paired:
            return AnchorProof(
                meme_address=launch.meme_address,
                proof=PROOF_POOL_PAIRING,
                anchors=paired,
                launchpad=launch.launchpad,
                transaction_hash=launch.transaction_hash,
            )

    if launch.anchor_addresses or launch.pool_token_addresses:
        return fail(REASON_ANCHOR_NOT_A_STOCK)
    return fail(REASON_NO_CLAIM)


def _resolve_all(
    addresses: Sequence[str],
    registry: StockRegistry,
    *,
    now: int,
    exclude: str,
) -> tuple[StockToken, ...]:
    """Every supplied address that is an active stock token, deduplicated."""

    found: dict[str, StockToken] = {}
    for address in addresses:
        key = normalise_address(address)
        if not key or key == exclude:
            continue
        token = registry.resolve(key, now=now)
        if token is not None:
            found[token.key] = token
    return tuple(found.values())
