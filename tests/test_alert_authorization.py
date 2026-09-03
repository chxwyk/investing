"""One door to Discord, and named gates instead of a score.

Two findings from the audit that opened this release, both of them the same
mistake at different scales.

**The guard covered one call site out of sixteen.**  v2.50 put the quality and
clone check inside ``_publish_fast_alert`` and called the problem solved. An
audit of ``engine.py`` found fifteen other paths reaching the notifier, and
three of them — ``_publish_trending``, ``_publish_trench`` and the trending
radar — called Discord *directly*, skipping the dedupe reservation as well as
the guard. Those are the cards the operator sees most.

A builder-specific check only ever protects the builders somebody remembered to
change. So authorization moved to the one place every card must physically pass
through, and the test below asserts that place is the only one.

**A score cannot answer four different questions.**  "Real but unsellable",
"moving but unsafe", "safe but too late" and "actually tradeable" collapse onto
one axis the moment they share a number, and a big enough momentum term buys its
way past a missing safety answer. The gate model refuses to let that happen:
named questions, PASS/FAIL/UNKNOWN, UNKNOWN never PASS, and no weight anywhere.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from decimal import Decimal

import smart_money_bot.engine as engine_module
import smart_money_bot.fast_alerts as fa
from smart_money_bot.lab.hardgates import (
    CANONICAL_TOKEN,
    CONCENTRATION_OK,
    CONTRACT_SAFETY_OK,
    EDGE_GATES,
    ENTRY_CANDIDATE,
    FAIL,
    FINAL_ALERT_GUARD_OK,
    GATES,
    MOMENTUM_OK,
    NO_ENTRY_NOW,
    NOT_LATE_OR_EXHAUSTED,
    PASS,
    REQUIRED_FOR_ENTRY,
    SAFETY_GATES,
    SELL_EVIDENCE_OK,
    SELL_ROUTE_OK,
    UNKNOWN,
    UNSAFE_MOMENTUM,
    VERIFIED_LIQUIDITY,
    WATCH_ONLY,
    GateResult,
    build_report,
    unknown,
)


def _code_only(source: str) -> str:
    """Source with docstrings removed, for tests that assert an absence.

    A module that documents why it refuses to do something will always contain
    the words for the thing it refuses to do. Grepping the prose finds the
    promise, not a violation of it.
    """

    import ast

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body.pop(0)
    return ast.unparse(tree)


NOW = 1_700_000_000
MINT = "GPR7Ax4kQ2mVn8hLdT6yWc3JbRfE9uZsXqM1oP5tH4dK"


def _all(answer: str = PASS, **overrides) -> list[GateResult]:
    results = []
    for gate in GATES:
        results.append(
            GateResult(
                gate=gate,
                answer=overrides.get(gate, answer),
                source="decoded chain state",
                observed_at=NOW,
                reason="fixture",
            )
        )
    return results


# ===========================================================================
# 1. The no-bypass architecture (spec section 3, tests 10 and 19)
# ===========================================================================


def test_exactly_one_place_in_the_codebase_can_send_a_card() -> None:
    """The structural claim, asserted rather than trusted.

    If this fails, a new builder has learned to reach Discord on its own and
    every rule in this repository is optional again.
    """

    source = inspect.getsource(engine_module)
    sites = re.findall(r"notifier\.on_fast_alert\(", source)
    assert len(sites) == 1, (
        f"{len(sites)} paths can send a card; exactly one (the dispatcher) may"
    )
    dispatcher = inspect.getsource(engine_module.SmartMoneyEngine._dispatch_card)
    assert "notifier.on_fast_alert(" in dispatcher


def test_the_dispatcher_guards_before_it_sends() -> None:
    dispatcher = inspect.getsource(engine_module.SmartMoneyEngine._dispatch_card)
    assert dispatcher.index("_guard_publication") < dispatcher.index(
        "notifier.on_fast_alert"
    )


def test_the_builders_that_used_to_call_discord_directly_no_longer_can() -> None:
    # These three were the live bypass: trending and trench cards, plus the
    # trending radar, none of which even reserved a dedupe row.
    for name in ("_publish_trending", "_publish_trench", "_run_trending_radar"):
        source = inspect.getsource(getattr(engine_module.SmartMoneyEngine, name))
        assert "notifier.on_fast_alert" not in source, f"{name} still bypasses the guard"
        assert "_dispatch_card" in source, f"{name} does not dispatch at all"


def test_the_promotion_and_hot_watch_paths_still_dispatch() -> None:
    # The NORMIE path. It must never regain a private route to Discord.
    for name in ("_publish_promotion", "_publish_fast_alert"):
        source = inspect.getsource(getattr(engine_module.SmartMoneyEngine, name))
        assert "notifier.on_fast_alert" not in source


def test_a_guarded_card_is_the_one_that_gets_sent() -> None:
    """Guarding a copy and sending the original would be a silent no-op."""

    sent: list[fa.FastAlert] = []

    class _Notifier:
        async def on_fast_alert(self, alert):
            sent.append(alert)
            return True

    engine = object.__new__(engine_module.SmartMoneyEngine)
    engine.notifier = _Notifier()
    engine._fast_alerts = {}
    engine._quality_scores = {}
    engine._clone_verdicts = {}
    engine._gate_reports = {}
    engine.gate_refusals = 0
    engine.refused_publications = 0

    alert = fa.FastAlert(
        kind=fa.EARLY_RUNNER,
        mint=MINT,
        alert_key=f"k:{MINT}",
        spec=fa.CardSpec(title="🚨 EARLY RUNNER — LOOK NOW", description="x"),
        ping=True,
        lane=fa.LANE_URGENT,
        token_mint=MINT,
    )
    from smart_money_bot.lab.clone import TokenFacts
    from smart_money_bot.lab.tokenquality import score_quality

    dead = TokenFacts(
        mint=MINT, name="X", symbol="X", age_seconds=600,
        liquidity_usd=Decimal("900"), market_cap_usd=Decimal("1000"),
        holder_count=3, buys=10, sells=90,
    )
    engine._quality_scores[MINT] = score_quality(dead)

    assert asyncio.run(engine._dispatch_card(alert)) is True
    assert len(sent) == 1
    # The notifier received the *guarded* card, not the original.
    assert sent[0].ping is False
    assert "LOOK NOW" not in sent[0].spec.title


# ===========================================================================
# 2. The gate model (spec section E, tests 33 and 34)
# ===========================================================================


def test_unknown_is_never_pass() -> None:
    """Test 34. A timeout, an outage and a missing field all fail closed."""

    for gate in sorted(REQUIRED_FOR_ENTRY):
        report = build_report(MINT, _all(PASS, **{gate: UNKNOWN}), now=NOW)
        assert report.classify() != ENTRY_CANDIDATE, f"{gate}=UNKNOWN produced an entry"
        assert gate in report.blocking()


def test_a_missing_gate_is_unknown_rather_than_absent() -> None:
    # A gate nobody answered must not quietly drop out of the requirement set.
    report = build_report(MINT, [], now=NOW)
    assert set(report.unresolved) == set(GATES)
    assert report.classify() != ENTRY_CANDIDATE
    assert unknown(CANONICAL_TOKEN).answer == UNKNOWN


def test_no_weighting_exists_anywhere_in_the_gate_model() -> None:
    """Test 33, enforced structurally rather than by trying to outscore it.

    There is no number to overrule a gate with, because the module contains no
    weights, no totals and no score.
    """

    import smart_money_bot.lab.hardgates as gates_module

    # Docstrings are stripped first. The module explains at length why there is
    # no score, and grepping raw source would flag the explanation rather than
    # an implementation — a lesson this suite learned in v2.45.
    source = _code_only(inspect.getsource(gates_module))
    for forbidden in ("weight", "score", "sum(", "Decimal", "import math"):
        assert forbidden not in source, f"a scoring path appeared in the gate model: {forbidden}"
    # And the public surface offers no number to rank by.
    from smart_money_bot.lab.hardgates import GateReport, GateResult

    for cls in (GateResult, GateReport):
        assert not any("score" in f or "weight" in f for f in cls.__slots__)


def test_every_gate_answer_carries_its_receipt() -> None:
    result = GateResult(
        gate=SELL_ROUTE_OK,
        answer=PASS,
        source="jupiter exact-in quote",
        observed_at=NOW,
        max_age_seconds=60,
        reason="sell quote returned a route",
        evidence=(("price_impact", "1.8%"), ("route", "raydium")),
    )
    payload = result.to_json()
    assert payload["source"] and payload["observed_at"] and payload["evidence"]


# --- the four answers, kept distinct ----------------------------------------


def test_moving_with_unproven_safety_is_unsafe_momentum_not_an_entry() -> None:
    report = build_report(MINT, _all(PASS, **{SELL_ROUTE_OK: UNKNOWN}), now=NOW)
    assert report.classify() == UNSAFE_MOMENTUM
    assert report.may_ping is False


def test_real_but_unsellable_is_watch_only() -> None:
    results = _all(PASS, **{SELL_EVIDENCE_OK: FAIL, MOMENTUM_OK: FAIL})
    assert build_report(MINT, results, now=NOW).classify() == WATCH_ONLY


def test_safe_but_late_is_no_entry_now() -> None:
    results = _all(PASS, **{NOT_LATE_OR_EXHAUSTED: FAIL})
    assert build_report(MINT, results, now=NOW).classify() == NO_ENTRY_NOW


def test_everything_passing_is_the_only_route_to_an_entry_candidate() -> None:
    report = build_report(MINT, _all(PASS), now=NOW)
    assert report.classify() == ENTRY_CANDIDATE
    assert report.may_ping is True
    assert report.blocking() == ()


def test_only_an_entry_candidate_can_ever_ping() -> None:
    for gate in (SELL_ROUTE_OK, VERIFIED_LIQUIDITY, CONTRACT_SAFETY_OK, CONCENTRATION_OK):
        assert build_report(MINT, _all(PASS, **{gate: FAIL}), now=NOW).may_ping is False


def test_safety_and_momentum_are_separate_axes() -> None:
    # The distinction that makes UNSAFE_MOMENTUM worth having: same safety
    # failure, different edge, different answer.
    moving = build_report(MINT, _all(PASS, **{CONTRACT_SAFETY_OK: FAIL}), now=NOW)
    still = build_report(
        MINT, _all(PASS, **{CONTRACT_SAFETY_OK: FAIL, MOMENTUM_OK: FAIL}), now=NOW
    )
    assert moving.classify() == UNSAFE_MOMENTUM
    assert still.classify() == WATCH_ONLY
    assert MOMENTUM_OK not in SAFETY_GATES
    assert MOMENTUM_OK in EDGE_GATES


# --- staleness ---------------------------------------------------------------


def test_a_stale_pass_decays_to_unknown_rather_than_staying_true() -> None:
    """"A route that existed five minutes ago is not proof that it exists now."""

    fresh = GateResult(
        gate=SELL_ROUTE_OK, answer=PASS, source="quote",
        observed_at=NOW - 10, max_age_seconds=60, reason="sell quote",
    )
    stale = GateResult(
        gate=SELL_ROUTE_OK, answer=PASS, source="quote",
        observed_at=NOW - 600, max_age_seconds=60, reason="sell quote",
    )
    assert fresh.at(NOW).answer == PASS
    assert stale.at(NOW).answer == UNKNOWN
    assert "expired" in stale.at(NOW).reason

    results = [r for r in _all(PASS) if r.gate != SELL_ROUTE_OK] + [stale]
    assert build_report(MINT, results, now=NOW).classify() == UNSAFE_MOMENTUM


def test_a_pass_with_no_timestamp_cannot_prove_it_is_fresh() -> None:
    undated = GateResult(
        gate=SELL_ROUTE_OK, answer=PASS, source="quote", max_age_seconds=60
    )
    assert undated.at(NOW).answer == UNKNOWN


def test_a_fail_does_not_expire() -> None:
    # Evidence that something was broken does not stop counting because time
    # passed. Only a claim that it *worked* has to keep being re-earned.
    broken = GateResult(
        gate=SELL_ROUTE_OK, answer=FAIL, source="quote",
        observed_at=NOW - 100_000, max_age_seconds=60, reason="honeypot",
    )
    assert broken.at(NOW).answer == FAIL


def test_an_immutable_fact_never_expires() -> None:
    creation = GateResult(
        gate=CANONICAL_TOKEN, answer=PASS, source="creation slot",
        observed_at=NOW - 100_000, max_age_seconds=None,
    )
    assert creation.at(NOW).answer == PASS


# --- explainability ----------------------------------------------------------


def test_the_report_says_which_gate_stopped_it_and_why() -> None:
    results = _all(PASS)
    results = [r for r in results if r.gate != VERIFIED_LIQUIDITY]
    results.append(
        GateResult(
            gate=VERIFIED_LIQUIDITY, answer=FAIL, source="on-chain reserves",
            observed_at=NOW, reason="vault reserves are dust against a $61K provider figure",
        )
    )
    report = build_report(MINT, results, now=NOW)
    assert VERIFIED_LIQUIDITY in report.failed
    joined = " ".join(report.reasons())
    assert "verified liquidity" in joined
    assert "dust" in joined


def test_a_later_answer_replaces_an_earlier_one_for_the_same_gate() -> None:
    early = GateResult(gate=SELL_ROUTE_OK, answer=PASS, source="q", observed_at=NOW - 5)
    later = GateResult(gate=SELL_ROUTE_OK, answer=FAIL, source="q", observed_at=NOW)
    report = build_report(MINT, [early, later], now=NOW)
    assert report.answer(SELL_ROUTE_OK) == FAIL


def test_the_final_guard_is_itself_a_required_gate() -> None:
    assert FINAL_ALERT_GUARD_OK in REQUIRED_FOR_ENTRY
    report = build_report(MINT, _all(PASS, **{FINAL_ALERT_GUARD_OK: UNKNOWN}), now=NOW)
    assert report.classify() != ENTRY_CANDIDATE


def test_the_gate_model_holds_no_provider_database_or_signer() -> None:
    import smart_money_bot.lab.hardgates as gates_module

    source = inspect.getsource(gates_module)
    for forbidden in ("import aiohttp", "aiosqlite", "from solders", "private_key", "cookies="):
        assert forbidden not in source


def test_an_invalid_answer_is_refused_at_construction() -> None:
    import pytest

    with pytest.raises(ValueError):
        GateResult(gate=SELL_ROUTE_OK, answer="PROBABLY")


# ===========================================================================
# 3. v2.53 — the guard moved to the object, not the call sites
# ===========================================================================


def test_every_card_notification_is_wrapped_rather_than_each_call_site() -> None:
    """Specification test 37, and the reason it is structural.

    v2.51 covered the FastAlert family. An audit for this release found
    thirteen further call sites reaching the notifier directly — runner alerts,
    callouts, watches, signals — every one a card an operator acts on. Fixing
    them individually would leave the fourteenth to be found later, which is
    the entire history of this bug.

    So the engine holds one notifier and it is the guarded one. A builder
    cannot opt out because there is nothing else to call.
    """

    import asyncio

    from smart_money_bot.engine import GUARDED_NOTIFICATIONS, GuardedNotifier

    calls: list[str] = []

    class _Inner:
        async def on_runner_alert(self, candidate):
            calls.append("on_runner_alert")
            return True

        async def on_coin_callout(self, callout):
            calls.append("on_coin_callout")
            return True

        async def on_error(self, context, error):
            calls.append("on_error")
            return True

    class _Engine:
        def __init__(self, refuse: bool) -> None:
            self._refuse = refuse

        def _refuses_publication(self, mint: str) -> bool:
            return self._refuse

    from types import SimpleNamespace

    candidate = SimpleNamespace(mint=MINT)

    allowed = GuardedNotifier(_Inner(), _Engine(refuse=False))
    assert asyncio.run(allowed.on_runner_alert(candidate)) is True
    assert calls == ["on_runner_alert"]

    calls.clear()
    refused = GuardedNotifier(_Inner(), _Engine(refuse=True))
    assert asyncio.run(refused.on_runner_alert(candidate)) is False
    assert asyncio.run(refused.on_coin_callout(candidate)) is False
    assert calls == [], "a refused mint must not reach the notifier at all"
    assert refused.refused["on_runner_alert"] == 1

    # Errors are never withheld: suppressing a failure report protects nobody.
    assert "on_error" not in GUARDED_NOTIFICATIONS
    assert asyncio.run(refused.on_error("ctx", RuntimeError("x"))) is True
    assert calls == ["on_error"]


def test_the_engine_holds_the_guarded_notifier_and_not_the_raw_one() -> None:
    import smart_money_bot.engine as engine_module

    source = inspect.getsource(engine_module.SmartMoneyEngine.__init__)
    assert "GuardedNotifier(" in source, "the engine can still hold an unguarded notifier"


def test_the_wrapper_identifies_a_token_by_address_never_by_symbol() -> None:
    from types import SimpleNamespace

    from smart_money_bot.engine import _mint_of

    assert _mint_of([SimpleNamespace(mint=MINT)]) == MINT
    assert _mint_of([SimpleNamespace(token_mint=MINT)]) == MINT
    # A symbol is not an identity, so an object carrying only one is not
    # matched — the wrapper gives up rather than guessing.
    assert _mint_of([SimpleNamespace(symbol="BONK")]) == ""
    assert _mint_of([]) == ""


def test_a_mint_nobody_measured_is_not_refused() -> None:
    """This blocks tokens we looked at and rejected, never ones we have not
    measured. Refusing the unmeasured would silence the bot on its first scan
    after every restart."""

    engine = object.__new__(engine_module.SmartMoneyEngine)
    engine._gate_reports = {}
    engine._quality_scores = {}
    assert engine._refuses_publication(MINT) is False
