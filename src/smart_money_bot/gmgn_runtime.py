"""The GMGN research loop: poll, identify, persist, and hand off.

This is the coordinator between the read-only provider and the rest of the bot.
It deliberately does very little thinking of its own — scoring, promotion and
alerting already exist and are better tested — so its job is:

1. poll the documented feeds within one shared budget;
2. put every row through the **exact-mint** boundary before it becomes a
   candidate (v2.43.1, section 2);
3. fold each observation into that mint's **single lifecycle** so a token that
   changes stage keeps its history (section 10);
4. hand the interesting ones to the existing hot-watch and promotion machinery,
   attributed to the family that found them (section 64).

The one thing it will not do is let a provider outage become a market opinion.
Every failure is recorded against provider health and the loop continues: GMGN
is the professional source, our own on-chain engine is the independent one, and
neither is allowed to be a single point of failure (sections 8, 41, 58).
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .gmgn import (
    BOARD_FINAL_STRETCH,
    BOARD_MIGRATED,
    BOARD_NEW_PAIRS,
    TRENCH_COMPLETED,
    TRENCH_NEAR_COMPLETION,
    TRENCH_NEW_CREATION,
    GmgnClient,
    GmgnError,
    GmgnParticipant,
    GmgnToken,
    TokenLifecycle,
    advance,
    lifecycle_from_json,
    open_lifecycle,
)
from .gmgn import lifecycle as stages
from .lab.shadow import (
    FAMILY_GMGN_HOT_SEARCH,
    FAMILY_GMGN_KOL,
    FAMILY_GMGN_MARKET_SIGNAL,
    FAMILY_GMGN_SMART_MONEY,
    FAMILY_GMGN_TRENCH_FINAL_STRETCH,
    FAMILY_GMGN_TRENCH_MIGRATED,
    FAMILY_GMGN_TRENCH_NEW,
    FAMILY_GMGN_TRENDING,
)
from .token_identity import assert_exact_propagation, is_valid_mint

logger = logging.getLogger(__name__)

#: GMGN trench section → our lifecycle stage.  The mapping is explicit because
#: the vendor's three buckets and our ten stages are not the same vocabulary,
#: and pretending they are is how a "completed" row silently becomes "trending".
TRENCH_STAGE: dict[str, str] = {
    TRENCH_NEW_CREATION: stages.NEW_PAIR,
    TRENCH_NEAR_COMPLETION: stages.FINAL_STRETCH,
    TRENCH_COMPLETED: stages.RECENTLY_MIGRATED,
}

TRENCH_FAMILY: dict[str, str] = {
    TRENCH_NEW_CREATION: FAMILY_GMGN_TRENCH_NEW,
    TRENCH_NEAR_COMPLETION: FAMILY_GMGN_TRENCH_FINAL_STRETCH,
    TRENCH_COMPLETED: FAMILY_GMGN_TRENCH_MIGRATED,
}


@dataclass(frozen=True, slots=True)
class GmgnCandidate:
    """One exact mint GMGN reported, with where it came from attached."""

    mint: str
    token: GmgnToken
    family: str
    stage: str
    section: str = ""
    interval: str = ""

    @property
    def market_cap_usd(self) -> Decimal | None:
        return self.token.market_cap_usd

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "family": self.family,
            "stage": self.stage,
            "section": self.section,
            "interval": self.interval,
            "token": self.token.to_json(),
        }


@dataclass(frozen=True, slots=True)
class GmgnScanResult:
    """What one poll produced, and what it could not."""

    at: int = 0
    candidates: tuple[GmgnCandidate, ...] = ()
    smart_money_wallets: int = 0
    kol_wallets: int = 0
    signals: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_json(self) -> dict[str, object]:
        return {
            "at": self.at,
            "candidates": len(self.candidates),
            "smart_money_wallets": self.smart_money_wallets,
            "kol_wallets": self.kol_wallets,
            "signals": self.signals,
            "errors": list(self.errors),
        }


class GmgnRuntime:
    """Owns the client, the lifecycles and the wallet directories.

    Constructed even when GMGN is unconfigured: an unconfigured runtime reports
    ``AUTH_MISSING`` and returns empty results, which is the behaviour the rest
    of the bot needs in order to keep working without it.
    """

    def __init__(
        self,
        client: GmgnClient,
        *,
        database: Any = None,
        settings: Any = None,
    ) -> None:
        self.client = client
        self.database = database
        self.settings = settings
        self._lifecycles: dict[str, TokenLifecycle] = {}
        #: wallet → provider tag.  Directories change slowly; they are refreshed
        #: on their own cadence rather than per scan.
        self.smart_money: dict[str, GmgnParticipant] = {}
        self.kols: dict[str, GmgnParticipant] = {}
        self.last_scan: GmgnScanResult | None = None
        self.last_directory_refresh_at = 0
        self.scans = 0
        self.candidates_seen = 0

    # ---- identity boundary --------------------------------------------

    def _accept(self, token: GmgnToken) -> bool:
        """A row becomes a candidate only if it carries a usable exact mint."""

        if not is_valid_mint(token.mint):
            return False
        # The parser took the mint from the row; this asserts nothing swapped it
        # between parsing and here, which is the invariant v2.43.1 exists for.
        assert_exact_propagation(token.mint, token.mint, stage="gmgn row → candidate")
        return True

    # ---- lifecycle -----------------------------------------------------

    async def observe(
        self,
        mint: str,
        *,
        stage: str,
        at: int,
        market_cap_usd: Decimal | None,
        source: str,
    ) -> TokenLifecycle:
        """Fold one observation into this mint's single lifecycle."""

        current = self._lifecycles.get(mint)
        if current is None and self.database is not None:
            with contextlib.suppress(Exception):
                row = await self.database.token_lifecycle_row(mint)
                if row:
                    current = lifecycle_from_json(row)
        if current is None:
            current = open_lifecycle(
                mint, stage=stage, at=at, market_cap_usd=market_cap_usd, source=source
            )
        else:
            current = advance(
                current, stage=stage, at=at, market_cap_usd=market_cap_usd, source=source
            )
        self._lifecycles[mint] = current
        if self.database is not None:
            with contextlib.suppress(Exception):
                await self.database.save_token_lifecycle(current.to_json())
        return current

    def lifecycle_for(self, mint: str) -> TokenLifecycle | None:
        return self._lifecycles.get(mint)

    def board(self) -> dict[str, tuple[TokenLifecycle, ...]]:
        """The operator's three sections (section 44)."""

        return stages.group_board(tuple(self._lifecycles.values()))

    # ---- polling -------------------------------------------------------

    async def scan(self, *, now: int | None = None) -> GmgnScanResult:
        """One poll of every enabled feed.  Partial failure is still a result.

        Each feed is attempted independently and its failure recorded, because
        "trenches timed out" must not cost us the trending board that answered
        fine two hundred milliseconds earlier.
        """

        moment = now if now is not None else int(time.time())
        if not self.client.configured:
            self.last_scan = GmgnScanResult(
                at=moment, errors=("gmgn not configured",)
            )
            return self.last_scan

        candidates: list[GmgnCandidate] = []
        errors: list[str] = []
        settings = self.settings

        intervals = tuple(getattr(settings, "gmgn_trending_intervals", ("1m", "5m", "1h")))
        limit = int(getattr(settings, "gmgn_trending_limit", 50))
        for interval in intervals:
            try:
                rows = await self.client.trending(interval=interval, limit=limit)
            except GmgnError as exc:
                errors.append(f"rank {interval}: {exc}")
                continue
            for token in rows:
                if not self._accept(token):
                    continue
                candidates.append(
                    GmgnCandidate(
                        mint=token.mint,
                        token=token,
                        family=FAMILY_GMGN_TRENDING,
                        stage=stages.TRENDING,
                        interval=interval,
                    )
                )

        if getattr(settings, "gmgn_trenches_enabled", True):
            try:
                sections = await self.client.trenches(
                    limit=int(getattr(settings, "gmgn_trenches_limit", 60))
                )
            except GmgnError as exc:
                errors.append(f"trenches: {exc}")
            else:
                for name, rows in sections.items():
                    stage = TRENCH_STAGE.get(name, stages.UNKNOWN)
                    family = TRENCH_FAMILY.get(name, FAMILY_GMGN_TRENCH_NEW)
                    for token in rows:
                        if not self._accept(token):
                            continue
                        candidates.append(
                            GmgnCandidate(
                                mint=token.mint,
                                token=token,
                                family=family,
                                stage=stage,
                                section=name,
                            )
                        )

        signals = 0
        if getattr(settings, "gmgn_market_signals_enabled", True):
            try:
                rows = await self.client.market_signals()
            except GmgnError as exc:
                errors.append(f"signals: {exc}")
            else:
                signals = len(rows)
                for signal in rows:
                    if not signal.demand or not is_valid_mint(signal.mint):
                        # A Dex ad is paid placement, not demand.  It is
                        # persisted for the record but never made a candidate.
                        await self._persist_signal(signal, at=moment)
                        continue
                    await self._persist_signal(signal, at=moment)
                    candidates.append(
                        GmgnCandidate(
                            mint=signal.mint,
                            token=GmgnToken(
                                mint=signal.mint,
                                symbol=signal.symbol,
                                market_cap_usd=signal.market_cap_usd,
                                source="gmgn_signal",
                            ),
                            family=FAMILY_GMGN_MARKET_SIGNAL,
                            stage=stages.UNKNOWN,
                            section=signal.signal_name,
                        )
                    )

        if getattr(settings, "gmgn_hot_search_enabled", True):
            try:
                rows = await self.client.hot_searches()
            except GmgnError as exc:
                errors.append(f"hot_searches: {exc}")
            else:
                for token in rows:
                    if not self._accept(token):
                        continue
                    # Attention only.  It surfaces the mint; it never promotes
                    # it on its own (section 18).
                    candidates.append(
                        GmgnCandidate(
                            mint=token.mint,
                            token=token,
                            family=FAMILY_GMGN_HOT_SEARCH,
                            stage=stages.UNKNOWN,
                            interval=token.interval,
                        )
                    )

        await self._refresh_directories(moment, errors)

        deduped: dict[str, GmgnCandidate] = {}
        for candidate in candidates:
            # First writer wins per mint per scan, and the ordering above is the
            # discovery priority from section 8: trending, then trenches, then
            # signals, then attention.
            deduped.setdefault(candidate.mint, candidate)

        for candidate in deduped.values():
            await self.observe(
                candidate.mint,
                stage=candidate.stage,
                at=moment,
                market_cap_usd=candidate.market_cap_usd,
                source=candidate.family,
            )
            await self._persist_candidate(candidate, at=moment)

        self.scans += 1
        self.candidates_seen += len(deduped)
        self.last_scan = GmgnScanResult(
            at=moment,
            candidates=tuple(deduped.values()),
            smart_money_wallets=len(self.smart_money),
            kol_wallets=len(self.kols),
            signals=signals,
            errors=tuple(errors),
        )
        return self.last_scan

    async def _refresh_directories(self, moment: int, errors: list[str]) -> None:
        """Smart-money and KOL directories change slowly; refresh them hourly.

        ``0`` means never refreshed, which is not "refreshed at the epoch" — the
        elapsed-time check alone would skip the very first load on any clock
        earlier than 1970 plus an hour, i.e. every test and every fresh boot.
        """

        if self.last_directory_refresh_at and moment - self.last_directory_refresh_at < 3_600:
            return
        settings = self.settings
        if getattr(settings, "gmgn_smart_money_enabled", True):
            try:
                for wallet in await self.client.smart_money():
                    self.smart_money[wallet.wallet] = wallet
            except GmgnError as exc:
                errors.append(f"smartmoney: {exc}")
        if getattr(settings, "gmgn_kol_enabled", True):
            try:
                for wallet in await self.client.kols():
                    self.kols[wallet.wallet] = wallet
            except GmgnError as exc:
                errors.append(f"kol: {exc}")
        self.last_directory_refresh_at = moment

    async def _persist_candidate(self, candidate: GmgnCandidate, *, at: int) -> None:
        if self.database is None:
            return
        with contextlib.suppress(Exception):
            await self.database.record_gmgn_observation(
                # Derived from the observation, so re-polling the same board row
                # in the same minute records nothing new.
                observation_id=(
                    f"{candidate.family}:{candidate.mint}:{candidate.interval}:{at // 60}"
                ),
                mint=candidate.mint,
                kind=candidate.family,
                observed_at=at,
                label=candidate.token.symbol,
                interval=candidate.interval,
                rank=candidate.token.rank,
                market_cap_usd=(
                    float(candidate.market_cap_usd)
                    if candidate.market_cap_usd is not None
                    else None
                ),
                payload=candidate.to_json(),
            )

    async def _persist_signal(self, signal: Any, *, at: int) -> None:
        if self.database is None:
            return
        with contextlib.suppress(Exception):
            await self.database.record_gmgn_observation(
                observation_id=(
                    f"{FAMILY_GMGN_MARKET_SIGNAL}:{signal.mint}:{signal.signal_name}:"
                    f"{signal.triggered_at or at}"
                ),
                mint=signal.mint,
                kind=FAMILY_GMGN_MARKET_SIGNAL,
                observed_at=at,
                label=signal.signal_name,
                market_cap_usd=(
                    float(signal.market_cap_usd) if signal.market_cap_usd is not None else None
                ),
                payload=signal.to_json(),
            )

    # ---- participants --------------------------------------------------

    def classify_wallet(self, wallet: str) -> tuple[str, ...]:
        """What GMGN calls this wallet.  Never merged with our own reputation."""

        tags: list[str] = []
        if wallet in self.smart_money:
            tags.append(FAMILY_GMGN_SMART_MONEY)
        if wallet in self.kols:
            tags.append(FAMILY_GMGN_KOL)
        return tuple(tags)

    async def token_participants(
        self, mint: str, *, limit: int = 20
    ) -> tuple[tuple[GmgnParticipant, ...], tuple[GmgnParticipant, ...]]:
        """Top holders and top traders for one exact mint (sections 25, 29)."""

        holders: tuple[GmgnParticipant, ...] = ()
        traders: tuple[GmgnParticipant, ...] = ()
        if not getattr(self.settings, "gmgn_holders_enabled", True):
            return holders, traders
        with contextlib.suppress(GmgnError):
            holders = await self.client.top_holders(mint, limit=limit)
        with contextlib.suppress(GmgnError):
            traders = await self.client.top_traders(mint, limit=limit)
        return holders, traders

    def status(self) -> dict[str, Any]:
        board = self.board()
        return {
            **self.client.usage_snapshot(),
            "scans": self.scans,
            "candidates_seen": self.candidates_seen,
            "lifecycles": len(self._lifecycles),
            "smart_money_wallets": len(self.smart_money),
            "kol_wallets": len(self.kols),
            "last_scan": self.last_scan.to_json() if self.last_scan else None,
            "board": {
                BOARD_NEW_PAIRS: len(board.get(BOARD_NEW_PAIRS, ())),
                BOARD_FINAL_STRETCH: len(board.get(BOARD_FINAL_STRETCH, ())),
                BOARD_MIGRATED: len(board.get(BOARD_MIGRATED, ())),
            },
        }


def independent_provider_wallets(
    participants: Sequence[GmgnParticipant],
    *,
    clusters: dict[str, str] | None = None,
) -> int:
    """Count provider-tagged wallets after collapsing clusters (section 32).

    Twenty wallets from one funder are one actor whichever provider labelled
    them, so the same collapse this codebase already applies to its own notable
    wallets is applied to GMGN's — a provider tag does not exempt a sybil group
    from being counted once.
    """

    clusters = clusters or {}
    actors: set[str] = set()
    for participant in participants:
        if not (participant.is_smart_money or participant.is_kol):
            continue
        actors.add(clusters.get(participant.wallet) or f"wallet:{participant.wallet}")
    return len(actors)
