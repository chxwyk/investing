"""The execution interface the bot will one day trade through — switched off.

The operator intends to trade real money eventually, and the worst time to
design an execution layer is the day you decide to turn it on.  So the shape is
built now, while the stakes are zero and the design can be argued with.

**Nothing here can spend money, and that is structural rather than conditional.**
The only provider implemented is :class:`ShadowExecutionProvider`, which records
an intent and returns.  There is no signer, no private key, no RPC submission
and no swap call anywhere in this package — so "the flag was wrong" cannot
become "it bought something", because there is nothing behind the flag.

What is designed here, deliberately, before it is needed:

* **Three modes**, of which one works.  ``SHADOW`` records; ``MANUAL_CONFIRM``
  and ``LIVE_AUTO`` are named so the state machine is complete, and both are
  refused (sections 78, 83).
* **Layered opt-in.**  Every gate in :class:`LiveTradingGates` must be
  explicitly true, and each defaults false. One misconfigured variable cannot
  reach a live order because no single variable is sufficient (section 77).
* **Idempotency by construction.**  A ``client_order_id`` is derived from the
  signal, the strategy and the mint, so a restart that replays a signal produces
  the *same* id rather than a second order (section 81).
* **Mode is persisted on the intent.**  A shadow entry carries ``SHADOW``
  forever; flipping a flag mid-flight cannot turn a recorded simulation into a
  real order, because the mode is a property of the record and not of the
  process (section 82).
"""

from __future__ import annotations

from .gates import (
    GATE_AUTO_TRADE,
    GATE_GMGN_LIVE,
    GATE_LIVE_TRADING,
    GATE_NAMES,
    LiveTradingGates,
    gates_from_settings,
)
from .provider import (
    MODE_LIVE_AUTO,
    MODE_MANUAL_CONFIRM,
    MODE_SHADOW,
    MODES,
    REJECT_LIVE_DISABLED,
    REJECT_MODE_UNSUPPORTED,
    REJECT_NO_PROVIDER,
    REJECT_PRECHECK_FAILED,
    ExecutionIntent,
    ExecutionProvider,
    ExecutionReceipt,
    LiveOrderPrecheck,
    ShadowExecutionProvider,
    client_order_id,
    evaluate_precheck,
)

__all__ = [
    "GATE_AUTO_TRADE",
    "GATE_GMGN_LIVE",
    "GATE_LIVE_TRADING",
    "GATE_NAMES",
    "MODES",
    "MODE_LIVE_AUTO",
    "MODE_MANUAL_CONFIRM",
    "MODE_SHADOW",
    "REJECT_LIVE_DISABLED",
    "REJECT_MODE_UNSUPPORTED",
    "REJECT_NO_PROVIDER",
    "REJECT_PRECHECK_FAILED",
    "ExecutionIntent",
    "ExecutionProvider",
    "ExecutionReceipt",
    "LiveOrderPrecheck",
    "LiveTradingGates",
    "ShadowExecutionProvider",
    "client_order_id",
    "evaluate_precheck",
    "gates_from_settings",
]
