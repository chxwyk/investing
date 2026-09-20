"""Ten buyers, or one buyer and nine followers?

This is the question that decides whether a convergence signal means anything.
Ten independent accounts reaching the same token separately is ten pieces of
evidence.  One account buying and nine copy-trading it six seconds later is one
piece of evidence and nine echoes — and if we count the echoes, every
copy-traded account in the market looks like a consensus.

Nothing here can prove independence; copying is not observable from arrival
times alone.  What *is* observable is the arrival pattern, and two patterns look
very different:

* Independent discovery produces irregular gaps with high entropy — people find
  a token when they find it.
* Copy-following produces a burst: one arrival, then a tight cluster inside a
  few seconds, repeatedly anchored to the same leader.

So this module measures the pattern and reports a *suspected* follow-cluster
count alongside the raw count, and the engine carries both.  The language
matters: ``possible_follow_cluster_count`` is a hypothesis about timing, not an
accusation about a person (section 28).

On-chain wallets get a second, stronger signal: a shared funding source.  Five
wallets funded by one address within a short window are one economic actor for
our purposes whatever their arrival times say, and the repository already tracks
those edges in ``wallet_funding_edges``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

ZERO = Decimal("0")
ONE = Decimal("1")

#: Arrivals within this many seconds of a preceding arrival are candidates for
#: being a follow rather than an independent discovery.
DEFAULT_FOLLOW_WINDOW_SECONDS = 20
#: A cluster needs at least this many arrivals before it is worth naming.
DEFAULT_MIN_CLUSTER = 3


@dataclass(frozen=True, slots=True)
class Arrival:
    """One actor's first action on one mint."""

    actor_id: str
    at: int
    amount_usd: Decimal | None = None
    #: Optional cluster label from an independent source (funding graph, etc.).
    cluster_id: str = ""
    #: The actor's measured pre-trend quality weight, when known.
    weight: Decimal = ONE


@dataclass(frozen=True, slots=True)
class FollowCluster:
    """A burst of arrivals tight enough to be one decision rather than several."""

    leader: str
    followers: tuple[str, ...]
    started_at: int
    ended_at: int

    @property
    def size(self) -> int:
        return 1 + len(self.followers)

    def to_json(self) -> dict[str, Any]:
        return {
            "leader": self.leader,
            "followers": list(self.followers),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "size": self.size,
        }


@dataclass(frozen=True, slots=True)
class IndependenceProfile:
    """The section-19 block: who arrived, how fast, and how independently."""

    mint: str
    at: int
    first_quality_buyer_at: int | None = None
    quality_buyers_30s: int = 0
    quality_buyers_1m: int = 0
    quality_buyers_3m: int = 0
    raw_buyers: int = 0
    independent_quality_buyers: int = 0
    possible_follow_cluster_count: int = 0
    clusters: tuple[FollowCluster, ...] = ()
    buyer_arrival_entropy: Decimal | None = None
    buyer_concentration: Decimal | None = None
    median_seconds_between_buyers: Decimal | None = None
    arrival_velocity: Decimal | None = None
    arrival_acceleration: Decimal | None = None
    #: How many distinct externally-supplied clusters (e.g. funding groups) the
    #: arrivals span.  ``None`` when no clustering source was available — which
    #: is different from "they were all independent".
    distinct_source_clusters: int | None = None

    @property
    def independence_ratio(self) -> Decimal | None:
        """Independent actors divided by raw actors.  ``None`` when unmeasurable."""

        if self.raw_buyers <= 0:
            return None
        return (
            Decimal(self.independent_quality_buyers) / Decimal(self.raw_buyers)
        ).quantize(Decimal("0.0001"))

    def to_json(self) -> dict[str, Any]:
        def s(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {
            "mint": self.mint,
            "at": self.at,
            "first_quality_buyer_at": self.first_quality_buyer_at,
            "quality_buyers_30s": self.quality_buyers_30s,
            "quality_buyers_1m": self.quality_buyers_1m,
            "quality_buyers_3m": self.quality_buyers_3m,
            "raw_buyers": self.raw_buyers,
            "independent_quality_buyers": self.independent_quality_buyers,
            "possible_follow_cluster_count": self.possible_follow_cluster_count,
            "clusters": [cluster.to_json() for cluster in self.clusters],
            "buyer_arrival_entropy": s(self.buyer_arrival_entropy),
            "buyer_concentration": s(self.buyer_concentration),
            "median_seconds_between_buyers": s(self.median_seconds_between_buyers),
            "arrival_velocity": s(self.arrival_velocity),
            "arrival_acceleration": s(self.arrival_acceleration),
            "distinct_source_clusters": self.distinct_source_clusters,
            "independence_ratio": s(self.independence_ratio),
        }


def detect_follow_clusters(
    arrivals: Sequence[Arrival],
    *,
    window_seconds: int = DEFAULT_FOLLOW_WINDOW_SECONDS,
    min_cluster: int = DEFAULT_MIN_CLUSTER,
) -> tuple[FollowCluster, ...]:
    """Find bursts where several arrivals trail one leader inside ``window_seconds``.

    The leader is simply whoever arrived first in the burst.  That is a labelling
    convenience, not a claim that the others copied them.
    """

    ordered = sorted(arrivals, key=lambda arrival: (arrival.at, arrival.actor_id))
    clusters: list[FollowCluster] = []
    index = 0
    while index < len(ordered):
        leader = ordered[index]
        followers: list[Arrival] = []
        cursor = index + 1
        # A follower must arrive within the window of the *leader*, not of the
        # previous follower — otherwise a slow trickle of independent buyers
        # chains into one enormous "cluster".
        while cursor < len(ordered) and ordered[cursor].at - leader.at <= window_seconds:
            followers.append(ordered[cursor])
            cursor += 1
        if len(followers) + 1 >= min_cluster:
            clusters.append(
                FollowCluster(
                    leader=leader.actor_id,
                    followers=tuple(item.actor_id for item in followers),
                    started_at=leader.at,
                    ended_at=followers[-1].at if followers else leader.at,
                )
            )
            index = cursor
        else:
            index += 1
    return tuple(clusters)


def arrival_entropy(arrivals: Sequence[Arrival], *, bucket_seconds: int = 15) -> Decimal | None:
    """Normalised Shannon entropy of arrival times, bucketed.

    1.0 means arrivals were spread evenly across the buckets they occupy; values
    near 0 mean they all landed in one bucket.  Fewer than two arrivals has no
    entropy to report, and ``None`` says so instead of returning 0, which would
    read as "maximally concentrated".
    """

    if len(arrivals) < 2:
        return None
    buckets: dict[int, int] = {}
    for arrival in arrivals:
        key = arrival.at // bucket_seconds
        buckets[key] = buckets.get(key, 0) + 1
    total = sum(buckets.values())
    if total <= 0 or len(buckets) < 2:
        return ZERO
    entropy = 0.0
    for count in buckets.values():
        p = count / total
        entropy -= p * math.log(p)
    maximum = math.log(len(buckets))
    if maximum <= 0:
        return ZERO
    return Decimal(str(round(entropy / maximum, 6)))


def concentration(arrivals: Sequence[Arrival]) -> Decimal | None:
    """Herfindahl share of notional taken by the single largest buyer.

    Returns ``None`` when no arrival carried an amount — an unknown
    concentration must not be rendered as a comfortable zero (section 67).
    """

    amounts = [
        arrival.amount_usd for arrival in arrivals if arrival.amount_usd is not None
    ]
    if not amounts:
        return None
    total = sum(amounts, ZERO)
    if total <= ZERO:
        return None
    return (max(amounts) / total).quantize(Decimal("0.0001"))


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return ((ordered[middle - 1] + ordered[middle]) / 2).quantize(Decimal("0.01"))


def build_independence(
    mint: str,
    arrivals: Sequence[Arrival],
    *,
    at: int,
    quality_threshold: Decimal = Decimal("1.5"),
    follow_window_seconds: int = DEFAULT_FOLLOW_WINDOW_SECONDS,
    min_cluster: int = DEFAULT_MIN_CLUSTER,
    cluster_lookup: Mapping[str, str] | None = None,
) -> IndependenceProfile:
    """Build the section-19 profile from arrivals at or before ``at``.

    ``quality_threshold`` is expressed in measured lift: an actor counts as
    "quality" when their pre-trend affinity beat the baseline by that factor.
    It is not a hand-assigned tier.
    """

    visible = sorted(
        (arrival for arrival in arrivals if arrival.at <= at),
        key=lambda arrival: (arrival.at, arrival.actor_id),
    )
    if not visible:
        return IndependenceProfile(mint=mint, at=at)

    quality = [arrival for arrival in visible if arrival.weight >= quality_threshold]
    clusters = detect_follow_clusters(
        quality, window_seconds=follow_window_seconds, min_cluster=min_cluster
    )

    # An actor is counted once per external cluster when a clustering source is
    # available, and once per follow-cluster otherwise.  Followers inside a
    # burst collapse into their leader.
    followers_in_clusters = {
        follower for cluster in clusters for follower in cluster.followers
    }
    if cluster_lookup:
        seen_clusters: set[str] = set()
        independent = 0
        for arrival in quality:
            if arrival.actor_id in followers_in_clusters:
                continue
            key = cluster_lookup.get(arrival.actor_id) or arrival.cluster_id
            if key:
                if key in seen_clusters:
                    continue
                seen_clusters.add(key)
            independent += 1
        distinct_clusters: int | None = len(
            {
                cluster_lookup.get(arrival.actor_id) or arrival.cluster_id or arrival.actor_id
                for arrival in quality
            }
        )
    else:
        independent = len(
            [item for item in quality if item.actor_id not in followers_in_clusters]
        )
        distinct_clusters = None

    gaps = [
        Decimal(visible[index].at - visible[index - 1].at)
        for index in range(1, len(visible))
    ]

    def within(seconds: int) -> int:
        return len([item for item in quality if at - seconds < item.at <= at])

    # Arrival velocity over the last minute, and the change against the minute
    # before it.
    recent = [item for item in visible if at - 60 < item.at <= at]
    prior = [item for item in visible if at - 120 < item.at <= at - 60]
    velocity = (Decimal(len(recent)) / Decimal(60)).quantize(Decimal("0.000001"))
    prior_velocity = (Decimal(len(prior)) / Decimal(60)).quantize(Decimal("0.000001"))

    return IndependenceProfile(
        mint=mint,
        at=at,
        first_quality_buyer_at=quality[0].at if quality else None,
        quality_buyers_30s=within(30),
        quality_buyers_1m=within(60),
        quality_buyers_3m=within(180),
        raw_buyers=len(visible),
        independent_quality_buyers=independent,
        possible_follow_cluster_count=len(clusters),
        clusters=clusters,
        buyer_arrival_entropy=arrival_entropy(visible),
        buyer_concentration=concentration(visible),
        median_seconds_between_buyers=_median(gaps),
        arrival_velocity=velocity,
        arrival_acceleration=velocity - prior_velocity,
        distinct_source_clusters=distinct_clusters,
    )
