"""EARLY TRACTION: the operator's Axiom Discover screen, in code, at speed.

This package replicates one specific screen the operator already runs by hand:

    Solana · launchpads {Pump, Bags, Bonk, LiquidAF, Heaven} · pre- and
    post-migration · age <= 25m · market cap >= $8,000 · volume >= $5,000 ·
    has a Twitter/X link · Dex-paid not required

and it is defined as much by what it refuses to do as by what it does.

**It adds no safety gate.**  Top-10, dev holding, insider, bundler and holder
count are blank on the operator's screen, so this package computes them, prints
them loudly in their own block, and lets the token through regardless.  The split
is structural: :mod:`.profile` decides pass or fail and does not import
:mod:`.safety` at all, and :class:`~.safety.SafetyReport` exposes no boolean a
gate could read.

**It never blends the blocks.**  Momentum ("is it moving?"), quality ("is the
move in proportion?") and safety ("what is wrong with it?") are three separate
fields.  One number would let a strong momentum reading cancel a 70% top-ten
holding and produce a confident figure describing neither.

**It refuses to invent a program address.**  Pump is the only launchpad here with
an address this repository already uses in production; the other four ship off
and say which variable turns them on.  A guessed address subscribes successfully
and then emits plausible nonsense, which is worse than a quiet lane.

**It is read-only.**  Nothing in this package imports an executor or a signer,
and an architecture test keeps it that way.

The modules, in dependency order:

``launchpads``  which venues are listened to, and the migration semantics
``profile``     the thresholds, and the deliberate absence of safety gates
``xlink``       X-link classification and cross-mint reuse, with no network call
``safety``      the display-only risk block: loud and powerless
``candidate``   momentum and quality, computed and rendered separately
``latency``     creation → detection → alert at p50, p95 and max

SQL lives in :mod:`smart_money_bot.traction_store`, the pipeline in
:mod:`smart_money_bot.traction_runtime`, and the card in
:mod:`smart_money_bot.traction_cards` -- the same pure/impure split the rest of
this repository uses.
"""

from __future__ import annotations
