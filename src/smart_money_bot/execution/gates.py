"""Layered opt-in for live trading.  Every layer defaults to off.

One boolean guarding real money is one typo away from spending it.  So this is
three independent switches, each of which must be explicitly true, named so that
setting any one of them by accident achieves nothing:

* ``LIVE_TRADING_ENABLED`` — the account-wide switch.
* ``GMGN_LIVE_TRADING_ENABLED`` — this provider specifically.
* ``AUTO_TRADE_ENABLED`` — unattended execution, as opposed to a human
  confirming each order.

:meth:`LiveTradingGates.blocked_by` returns every gate that is closed rather
than the first, because an operator turning this on eventually deserves the
whole list, not a game of whack-a-mole.
"""

from __future__ import annotations

from dataclasses import dataclass

GATE_LIVE_TRADING = "LIVE_TRADING_ENABLED"
GATE_GMGN_LIVE = "GMGN_LIVE_TRADING_ENABLED"
GATE_AUTO_TRADE = "AUTO_TRADE_ENABLED"

GATE_NAMES: tuple[str, ...] = (GATE_LIVE_TRADING, GATE_GMGN_LIVE, GATE_AUTO_TRADE)


@dataclass(frozen=True, slots=True)
class LiveTradingGates:
    """Three switches, all false.  This release ships them false and uses none."""

    live_trading_enabled: bool = False
    gmgn_live_trading_enabled: bool = False
    auto_trade_enabled: bool = False

    @property
    def all_open(self) -> bool:
        return (
            self.live_trading_enabled
            and self.gmgn_live_trading_enabled
            and self.auto_trade_enabled
        )

    def blocked_by(self) -> tuple[str, ...]:
        """Every closed gate, so the answer is complete rather than the first no."""

        closed: list[str] = []
        if not self.live_trading_enabled:
            closed.append(GATE_LIVE_TRADING)
        if not self.gmgn_live_trading_enabled:
            closed.append(GATE_GMGN_LIVE)
        if not self.auto_trade_enabled:
            closed.append(GATE_AUTO_TRADE)
        return tuple(closed)

    def to_json(self) -> dict[str, object]:
        return {
            GATE_LIVE_TRADING: self.live_trading_enabled,
            GATE_GMGN_LIVE: self.gmgn_live_trading_enabled,
            GATE_AUTO_TRADE: self.auto_trade_enabled,
            "all_open": self.all_open,
            "blocked_by": list(self.blocked_by()),
        }


def gates_from_settings(settings: object) -> LiveTradingGates:
    """Read the gates from deployment settings, defaulting every one to closed.

    A missing attribute reads as ``False``.  An absent switch is not an open
    one, and a deployment that has never heard of these variables must not be
    able to trade.
    """

    return LiveTradingGates(
        live_trading_enabled=bool(getattr(settings, "live_trading_enabled", False)),
        gmgn_live_trading_enabled=bool(
            getattr(settings, "gmgn_live_trading_enabled", False)
        ),
        auto_trade_enabled=bool(getattr(settings, "auto_trade_enabled", False)),
    )
