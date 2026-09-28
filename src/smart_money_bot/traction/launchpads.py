"""Which launchpads this lane listens to, and why most of them ship switched off.

The Early Traction profile names five launchpads: Pump, Bags, Bonk, LiquidAF and
Heaven.  Exactly one of them — Pump — has a program address this repository has
already used in production and can therefore stand behind.  The other four do
not appear anywhere in this codebase, and this module will not invent them.

That is not caution for its own sake.  Subscribing to a wrong program address
does not fail loudly: the socket connects, the subscription succeeds, and the
lane goes quiet or — worse — decodes unrelated instructions into plausible
"launches".  A guessed address is therefore indistinguishable from a dead lane
right up to the point where it produces a confident alert about nothing.
:mod:`smart_money_bot.stocks.launchpads` already states the principle for the
Stonks lane ("a hardcoded address in a source file is a claim by whoever typed
it"), and the same reasoning applies here.

So an adapter is **configured or it is off**:

* Pump ships ``ENABLED`` because ``PUMP_PROGRAM_ID`` is already in this
  repository, already subscribed to by :mod:`smart_money_bot.pump_stream`, and
  already producing launches in production.
* Bags, Bonk, LiquidAF and Heaven ship ``NOT_CONFIGURED``.  Supplying
  ``TRACTION_LAUNCHPAD_BAGS_PROGRAM_ID`` (and so on) turns one on.
* An address that is not valid base58 of the right length is ``INVALID``, not
  silently dropped, so a typo in a Railway variable is visible rather than
  mysterious.

The registry is also the single place that maps a launchpad to its
**migration semantics**, because "pre-migration" means different things on
different venues and the alert has to say which one it observed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..token_identity import is_valid_mint

# --- adapter states ----------------------------------------------------------
#: Address supplied and structurally valid.  May be subscribed to.
ENABLED = "ENABLED"
#: No address supplied.  The default for every launchpad except Pump.
NOT_CONFIGURED = "NOT_CONFIGURED"
#: An address was supplied and it is not a plausible Solana program address.
INVALID = "INVALID_ADDRESS"
#: Configured, but the operator switched the lane off.
DISABLED = "DISABLED_BY_CONFIG"

STATES: tuple[str, ...] = (ENABLED, NOT_CONFIGURED, INVALID, DISABLED)

# --- canonical launchpad names ----------------------------------------------
PUMP = "PUMP"
BAGS = "BAGS"
BONK = "BONK"
LIQUIDAF = "LIQUIDAF"
HEAVEN = "HEAVEN"

#: The profile's launchpads, in the order the operator listed them.
PROFILE_LAUNCHPADS: tuple[str, ...] = (PUMP, BAGS, BONK, LIQUIDAF, HEAVEN)

#: Env-var suffix per launchpad, so the operator can find them predictably.
ENV_SUFFIX: dict[str, str] = {
    PUMP: "PUMP",
    BAGS: "BAGS",
    BONK: "BONK",
    LIQUIDAF: "LIQUIDAF",
    HEAVEN: "HEAVEN",
}

# --- migration semantics -----------------------------------------------------
#: Still on the launchpad's bonding curve.
PRE_MIGRATION = "PRE_MIGRATION"
#: Graduated to an AMM pool.
POST_MIGRATION = "POST_MIGRATION"
#: We have not established which.  Never rendered as either one.
MIGRATION_UNKNOWN = "MIGRATION_UNKNOWN"

MIGRATION_STATES: tuple[str, ...] = (
    PRE_MIGRATION,
    POST_MIGRATION,
    MIGRATION_UNKNOWN,
)


@dataclass(frozen=True, slots=True)
class LaunchpadAdapter:
    """One launchpad's configuration and whether it may be listened to."""

    name: str
    program_id: str = ""
    state: str = NOT_CONFIGURED
    #: True when the launchpad runs a bonding curve before migrating.
    has_bonding_curve: bool = True
    #: Why it is not enabled, in words an operator can act on.
    detail: str = ""

    @property
    def enabled(self) -> bool:
        return self.state == ENABLED and bool(self.program_id)

    @property
    def env_var(self) -> str:
        return f"TRACTION_LAUNCHPAD_{ENV_SUFFIX.get(self.name, self.name)}_PROGRAM_ID"

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "program_id": self.program_id,
            "state": self.state,
            "enabled": self.enabled,
            "has_bonding_curve": self.has_bonding_curve,
            "detail": self.detail,
            "env_var": self.env_var,
        }


def _is_program_address(value: str) -> bool:
    """Structural check only.

    A program address and a mint share the same base58 shape, so this borrows
    the mint validator.  It proves the string *could* be an address; it proves
    nothing about which program lives there.  Only the operator's own sourcing
    can do that, which is why the docstring above insists they supply it.
    """

    return is_valid_mint(value)


def build_adapter(
    name: str,
    *,
    program_id: str | None,
    enabled: bool = True,
    has_bonding_curve: bool = True,
    builtin_program_id: str = "",
) -> LaunchpadAdapter:
    """Resolve one launchpad from configuration.

    ``builtin_program_id`` is only ever non-empty for Pump, whose address this
    repository already uses in production.  For every other launchpad the
    operator supplies the address or the adapter stays off.
    """

    supplied = (program_id or "").strip()
    resolved = supplied or builtin_program_id

    if not resolved:
        return LaunchpadAdapter(
            name=name,
            state=NOT_CONFIGURED,
            has_bonding_curve=has_bonding_curve,
            detail=(
                f"No program address is configured for {name}. This codebase does "
                f"not ship one and will not guess: a wrong program address "
                f"subscribes successfully and then emits plausible nonsense. "
                f"Set TRACTION_LAUNCHPAD_{ENV_SUFFIX.get(name, name)}_PROGRAM_ID "
                f"to the address from the launchpad's own documentation."
            ),
        )

    if not _is_program_address(resolved):
        return LaunchpadAdapter(
            name=name,
            program_id=resolved[:64],
            state=INVALID,
            has_bonding_curve=has_bonding_curve,
            detail=(
                f"{name} program address is not valid base58 of program length; "
                "refusing to subscribe. Check the Railway variable for a typo or "
                "a pasted newline."
            ),
        )

    if not enabled:
        return LaunchpadAdapter(
            name=name,
            program_id=resolved,
            state=DISABLED,
            has_bonding_curve=has_bonding_curve,
            detail=f"{name} is configured but switched off by configuration.",
        )

    return LaunchpadAdapter(
        name=name,
        program_id=resolved,
        state=ENABLED,
        has_bonding_curve=has_bonding_curve,
        detail=(
            f"{name} subscribed at {resolved}."
            + (
                " Address is the one already used in production by this repository."
                if not supplied and builtin_program_id
                else " Address supplied by the operator."
            )
        ),
    )


@dataclass(frozen=True, slots=True)
class LaunchpadRegistry:
    """Every launchpad the lane knows about, enabled or not."""

    adapters: tuple[LaunchpadAdapter, ...] = ()

    def by_name(self, name: str) -> LaunchpadAdapter | None:
        target = name.strip().upper()
        for adapter in self.adapters:
            if adapter.name == target:
                return adapter
        return None

    def by_program(self, program_id: str) -> LaunchpadAdapter | None:
        for adapter in self.adapters:
            if adapter.program_id and adapter.program_id == program_id:
                return adapter
        return None

    @property
    def enabled(self) -> tuple[LaunchpadAdapter, ...]:
        return tuple(adapter for adapter in self.adapters if adapter.enabled)

    @property
    def program_ids(self) -> tuple[str, ...]:
        return tuple(adapter.program_id for adapter in self.enabled)

    def accepts(self, launchpad: str) -> bool:
        """Whether a launchpad name is one this lane is listening to.

        An unknown or unconfigured launchpad is refused rather than admitted:
        the profile says which venues it covers, and a token from somewhere else
        has not met it, however good it looks.
        """

        adapter = self.by_name(launchpad)
        return adapter is not None and adapter.enabled

    def status(self) -> dict[str, Any]:
        """Operator-facing summary, including what is off and why."""

        return {
            "enabled": [adapter.name for adapter in self.enabled],
            "unavailable": {
                adapter.name: adapter.detail
                for adapter in self.adapters
                if not adapter.enabled
            },
            "adapters": [adapter.to_json() for adapter in self.adapters],
            "listening_to_programs": list(self.program_ids),
        }


def build_registry(
    *,
    requested: tuple[str, ...] = PROFILE_LAUNCHPADS,
    program_ids: dict[str, str] | None = None,
    pump_program_id: str = "",
    disabled: frozenset[str] = frozenset(),
) -> LaunchpadRegistry:
    """Resolve the whole registry from configuration.

    ``requested`` is the operator's launchpad list, so the profile's venue set
    is itself configurable.  Anything named here that has no address resolves to
    ``NOT_CONFIGURED`` and is reported — a silently missing venue would look
    exactly like a quiet one.
    """

    supplied = {key.upper(): value for key, value in (program_ids or {}).items()}
    adapters: list[LaunchpadAdapter] = []
    for name in requested:
        canonical = name.strip().upper()
        if not canonical:
            continue
        adapters.append(
            build_adapter(
                canonical,
                program_id=supplied.get(canonical),
                enabled=canonical not in disabled,
                builtin_program_id=pump_program_id if canonical == PUMP else "",
            )
        )
    return LaunchpadRegistry(adapters=tuple(adapters))
