"""The SAFETY block: computed for every alert, and never a gate.

The operator's Axiom screen has top-10, dev holding, insider, bundler and holder
count all left blank, so the Early Traction profile does not filter on them.  It
does not follow that those numbers are uninteresting — the operator wants to see
the token *and* see what is wrong with it.  Seeing a candidate with 62% in the
top ten wallets and deciding against it is a different and better outcome than
never seeing it.

So this module exists to be **loud and powerless**.  It assembles the risk
picture into its own labelled block, and nothing in it can suppress a candidate.
The separation is structural rather than a matter of discipline:
:func:`build_safety` returns a report with no boolean anybody could gate on, and
:mod:`smart_money_bot.traction.profile` — the only code that decides pass or fail
— does not import this module at all.

Two rules keep the block honest.

**Unknown is printed as unknown.**  A missing top-10 percentage renders as
``unknown``, never as ``0%``.  The whole value of this block is that the operator
can see which risks were actually measured, and a comfortable default destroys
exactly that.

**Nothing here accuses anybody.**  "Insider" and "bundler" are provider
classifications of on-chain patterns, reproduced with attribution, not findings
of fact about a person.  The wording stays observational.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

ZERO = Decimal("0")

#: How a single metric was sourced, so the card can say.
SOURCE_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class SafetyMetric:
    """One risk number, its provenance, and an honest absence."""

    name: str
    value: Decimal | None = None
    source: str = SOURCE_UNKNOWN
    #: Percent metrics render with a % suffix; counts do not.
    is_percent: bool = True

    @property
    def known(self) -> bool:
        return self.value is not None

    def render(self) -> str:
        if self.value is None:
            return "unknown"
        if self.is_percent:
            return f"{self.value:.1f}%"
        return f"{int(self.value)}"

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": None if self.value is None else str(self.value),
            "source": self.source,
            "known": self.known,
            "rendered": self.render(),
        }


@dataclass(frozen=True, slots=True)
class DeveloperHistory:
    """The deployer's prior launches, when we happen to know them."""

    wallet: str = ""
    tokens_created: int | None = None
    graduated: int | None = None
    collapsed: int | None = None
    source: str = SOURCE_UNKNOWN

    @property
    def known(self) -> bool:
        return self.tokens_created is not None

    def render(self) -> str:
        if not self.known:
            return "no prior-launch record for this deployer"
        parts = [f"{self.tokens_created} prior launch(es)"]
        if self.graduated is not None:
            parts.append(f"{self.graduated} graduated")
        if self.collapsed is not None:
            parts.append(f"{self.collapsed} collapsed")
        return ", ".join(parts)

    def to_json(self) -> dict[str, Any]:
        return {
            "wallet": self.wallet,
            "tokens_created": self.tokens_created,
            "graduated": self.graduated,
            "collapsed": self.collapsed,
            "source": self.source,
            "known": self.known,
            "rendered": self.render(),
        }


@dataclass(frozen=True, slots=True)
class SafetyReport:
    """Every risk number we could establish, and which we could not.

    Deliberately exposes no ``passed``, ``safe`` or ``blocked`` property.  There
    is nothing here for a gate to read, which is the point: adding one later
    would require adding it here first, and this docstring is where somebody
    would have to argue for it.
    """

    mint: str
    top10_percent: SafetyMetric = field(
        default_factory=lambda: SafetyMetric("top10_percent")
    )
    dev_holding_percent: SafetyMetric = field(
        default_factory=lambda: SafetyMetric("dev_holding_percent")
    )
    insider_percent: SafetyMetric = field(
        default_factory=lambda: SafetyMetric("insider_percent")
    )
    bundler_percent: SafetyMetric = field(
        default_factory=lambda: SafetyMetric("bundler_percent")
    )
    holder_count: SafetyMetric = field(
        default_factory=lambda: SafetyMetric("holder_count", is_percent=False)
    )
    developer: DeveloperHistory = field(default_factory=DeveloperHistory)
    #: Named observations worth surfacing, e.g. mint authority still live.
    notes: tuple[str, ...] = ()
    enriched_at: int | None = None

    @property
    def metrics(self) -> tuple[SafetyMetric, ...]:
        return (
            self.top10_percent,
            self.dev_holding_percent,
            self.insider_percent,
            self.bundler_percent,
            self.holder_count,
        )

    @property
    def known_count(self) -> int:
        return sum(1 for metric in self.metrics if metric.known)

    @property
    def completeness(self) -> str:
        return f"{self.known_count}/{len(self.metrics)} measured"

    @property
    def pending(self) -> bool:
        """True while enrichment has not returned.

        The card is sent before this is known, so it must be able to say "safety
        pending" rather than imply the risks were checked and found absent.
        """

        return self.enriched_at is None

    def render_lines(self) -> tuple[str, ...]:
        """The block as the card prints it, one metric per line."""

        if self.pending:
            return (
                "⏳ enrichment in flight — these are not yet measured",
                "top-10: unknown · dev: unknown · insiders: unknown",
                "bundlers: unknown · holders: unknown",
            )
        lines = [
            f"top-10 holders: {self.top10_percent.render()}",
            f"dev holding: {self.dev_holding_percent.render()}",
            f"insiders: {self.insider_percent.render()}",
            f"bundlers: {self.bundler_percent.render()}",
            f"holders: {self.holder_count.render()}",
            f"deployer: {self.developer.render()}",
            f"({self.completeness}; unmeasured values print as unknown, never 0)",
        ]
        lines.extend(f"• {note}" for note in self.notes[:4])
        return tuple(lines)

    def to_json(self) -> dict[str, Any]:
        return {
            "mint": self.mint,
            "pending": self.pending,
            "enriched_at": self.enriched_at,
            "completeness": self.completeness,
            "metrics": [metric.to_json() for metric in self.metrics],
            "developer": self.developer.to_json(),
            "notes": list(self.notes),
        }


def _metric(
    name: str, value: Any, *, source: str, is_percent: bool = True
) -> SafetyMetric:
    if value is None:
        return SafetyMetric(name, None, SOURCE_UNKNOWN, is_percent)
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception:
        return SafetyMetric(name, None, SOURCE_UNKNOWN, is_percent)
    if not decimal_value.is_finite():
        return SafetyMetric(name, None, SOURCE_UNKNOWN, is_percent)
    return SafetyMetric(name, decimal_value, source, is_percent)


def build_safety(
    mint: str,
    *,
    top10_percent: Any = None,
    dev_holding_percent: Any = None,
    insider_percent: Any = None,
    bundler_percent: Any = None,
    holder_count: Any = None,
    developer: DeveloperHistory | None = None,
    notes: tuple[str, ...] = (),
    source: str = SOURCE_UNKNOWN,
    enriched_at: int | None = None,
) -> SafetyReport:
    """Assemble the block from whatever enrichment returned.

    Every argument is optional because every one of them genuinely may be
    missing, and the report is expected to say so rather than to wait for a
    complete picture that may never arrive.
    """

    return SafetyReport(
        mint=mint,
        top10_percent=_metric("top10_percent", top10_percent, source=source),
        dev_holding_percent=_metric(
            "dev_holding_percent", dev_holding_percent, source=source
        ),
        insider_percent=_metric("insider_percent", insider_percent, source=source),
        bundler_percent=_metric("bundler_percent", bundler_percent, source=source),
        holder_count=_metric(
            "holder_count", holder_count, source=source, is_percent=False
        ),
        developer=developer or DeveloperHistory(),
        notes=notes,
        enriched_at=enriched_at,
    )
