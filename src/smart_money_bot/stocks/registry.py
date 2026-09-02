"""The canonical set of Robinhood Stock Tokens, keyed by contract address.

Sourced from the documented first-party endpoint ``GET
https://api.robinhood.com/rhj/assets``, filtered to active deployments on
Robinhood Chain (chain id **4663**).  This module holds the *snapshot*; fetching
it is somebody else's job, because a registry that can make network calls is a
registry that can fail open.

Two rules do all the work here.

**A ticker is not an identity.**  ``NVDA`` is a string anyone can put in a token
name, a symbol, a description or a domain.  The only thing that makes a contract
*the* NVIDIA stock token is that Robinhood lists that exact address as an active
chain-4663 deployment.  So the index is by address, and the by-symbol lookup
exists only to answer "what does Robinhood call this address?" — never the
reverse.  There is deliberately no ``address_for_symbol``.

**A stale registry is a refusal, not a default.**  When the assets endpoint is
unavailable, the last-known-good snapshot keeps serving until its TTL expires
and then stops answering.  Continuing to vouch for addresses from a snapshot of
unknown age is how a delisted or re-deployed token keeps passing verification
long after it stopped being real.

Pure logic: no provider, no database, no signer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

#: Robinhood Chain.  Any deployment on another chain is a different asset and
#: is dropped rather than translated.
ROBINHOOD_CHAIN_ID = 4663

#: Statuses Robinhood may report.  Only the first is tradeable, and only the
#: first may back an anchor proof.
STATUS_ACTIVE = "active"


def normalise_address(value: object) -> str:
    """Case-fold an EVM address for keying.  Display keeps the original.

    An address written two ways is one address; a checksummed string and a
    lowercase string that differ only in case must never be treated as two
    different tokens, in either direction.
    """

    text = str(value or "").strip()
    return text.lower() if text.startswith("0x") else text.lower()


@dataclass(frozen=True, slots=True)
class StockToken:
    """One Robinhood Stock Token deployment on chain 4663."""

    address: str
    symbol: str = ""
    name: str = ""
    asset_id: str = ""
    status: str = ""
    #: Corporate-action multiplier.  Carried because a price either side of one
    #: is not comparable, never used to adjust anything silently.
    multiplier: str = ""
    updated_at: int | None = None
    #: The checksummed form exactly as Robinhood sent it, for display and links.
    display_address: str = ""

    @property
    def key(self) -> str:
        return normalise_address(self.address)

    @property
    def active(self) -> bool:
        return self.status.strip().lower() == STATUS_ACTIVE

    def to_json(self) -> dict[str, object]:
        return {
            "address": self.address,
            "display_address": self.display_address or self.address,
            "symbol": self.symbol,
            "name": self.name,
            "asset_id": self.asset_id,
            "status": self.status,
            "multiplier": self.multiplier,
            "updated_at": self.updated_at,
            "active": self.active,
        }


@dataclass(frozen=True, slots=True)
class StockRegistry:
    """A point-in-time snapshot of the canonical stock-token set.

    ``fetched_at`` and ``ttl_seconds`` are not bookkeeping: they are what stops
    an outage turning into a permanently trusted stale snapshot.
    """

    tokens: tuple[StockToken, ...] = field(default_factory=tuple)
    fetched_at: int | None = None
    #: How long this snapshot may vouch for an address after it was taken.
    ttl_seconds: int = 3_600
    #: Set when this snapshot is being served after a failed refresh.
    degraded: bool = False
    source: str = "api.robinhood.com/rhj/assets"

    @property
    def by_address(self) -> Mapping[str, StockToken]:
        return {token.key: token for token in self.tokens if token.active}

    def age_seconds(self, now: int) -> int | None:
        return None if self.fetched_at is None else max(0, now - self.fetched_at)

    def usable(self, now: int) -> bool:
        """Whether this snapshot may still back an anchor proof.

        A snapshot with no timestamp cannot show that it is fresh, and freshness
        is required, so it never backs a proof.
        """

        age = self.age_seconds(now)
        return age is not None and age <= self.ttl_seconds

    def resolve(self, address: object, *, now: int) -> StockToken | None:
        """The stock token at this exact address, or ``None``.

        Address in, token out.  There is no symbol-keyed counterpart to this
        method anywhere in the package, and that asymmetry is the core rule of
        the whole lane made structural rather than documented.
        """

        if not self.usable(now):
            return None
        return self.by_address.get(normalise_address(address))

    def describe(self, now: int) -> str:
        age = self.age_seconds(now)
        if age is None:
            return "registry never loaded"
        state = "DEGRADED — serving last known good" if self.degraded else "OK"
        expired = "" if self.usable(now) else " — EXPIRED, no longer vouching"
        return f"{len(self.by_address)} active tokens, {age}s old, {state}{expired}"

    def to_json(self, *, now: int) -> dict[str, object]:
        return {
            "source": self.source,
            "active_tokens": len(self.by_address),
            "fetched_at": self.fetched_at,
            "age_seconds": self.age_seconds(now),
            "ttl_seconds": self.ttl_seconds,
            "usable": self.usable(now),
            "degraded": self.degraded,
        }


def build_registry(
    payload: Iterable[Mapping[str, object]],
    *,
    fetched_at: int,
    ttl_seconds: int = 3_600,
    degraded: bool = False,
) -> StockRegistry:
    """Parse ``/rhj/assets`` rows into a registry, keeping only chain 4663.

    Deployments on other chains are dropped rather than translated: a token at
    the same address on a different chain is a different asset, and reusing one
    chain's assumptions for another is exactly the class of mistake that this
    package's chain-id filter exists to prevent.
    """

    tokens: dict[str, StockToken] = {}
    for row in payload:
        symbol = str(row.get("tokenSymbol") or row.get("symbol") or "")
        name = str(row.get("tokenName") or row.get("name") or "")
        status = str(row.get("status") or "")
        asset_id = str(row.get("id") or row.get("assetId") or "")
        multiplier = str(row.get("multiplier") or "")
        deployments = row.get("deployments") or ()
        if not isinstance(deployments, Iterable) or isinstance(deployments, str | bytes):
            continue
        for deployment in deployments:
            if not isinstance(deployment, Mapping):
                continue
            try:
                chain_id = int(deployment.get("chainId"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if chain_id != ROBINHOOD_CHAIN_ID:
                continue
            raw = str(deployment.get("contractAddress") or "")
            key = normalise_address(raw)
            if not key.startswith("0x") or len(key) != 42:
                continue
            token = StockToken(
                address=key,
                display_address=raw,
                symbol=symbol,
                name=name,
                asset_id=asset_id,
                status=status or STATUS_ACTIVE,
                multiplier=multiplier,
                updated_at=fetched_at,
            )
            tokens[key] = token
    return StockRegistry(
        tokens=tuple(tokens.values()),
        fetched_at=fetched_at,
        ttl_seconds=ttl_seconds,
        degraded=degraded,
    )
