"""FOMO PRE-TREND INTELLIGENCE.

The question this package exists to answer, and to answer falsifiably:

    Using only information available at moment ``T``, can we identify a
    repeatable behavioural state that occurs BEFORE an exact Solana mint enters
    FOMO Trending — early enough to matter, and with few enough false alerts to
    be useful?

It is built so that the answer is allowed to be "no".  Every layer carries the
sample size behind it, every rate carries the base rate it is measured against,
every split is chronological, and every feature is computed by one function that
both training and production call.  The modules, in dependency order:

``identity``      exact-mint joins with explicit match states
``groundtruth``   FOMO_TREND_ENTER: the canonical, write-once label event
``windows``       level / velocity / acceleration, bounded at the decision time
``activity``      the FOMO-native tape and its rolling features
``affinity``      pre-trend affinity with shrinkage, lift and intervals
``independence``  ten buyers, or one buyer and nine followers?
``cascade``       which signal moved first — measured, not assumed
``cohorts``       market-cap / age cohorts and relative attention percentiles
``features``      the one feature pipeline
``labels``        TREND_2M / 5M / 10M / 20M, with censoring
``controls``      matched negatives, so winners are compared to look-alikes
``leakage``       machinery that tries to prove the rest of this is cheating
``model``         heuristic baseline, logistic regression, boosted trees
``validation``    walk-forward only; PR-AUC, Brier, base rate, lift, alert rate
``replay``        the past, run through the production path, one tick at a time
``states``        the token state machine and the alert budget
``forensics``     what was knowable before the entry, reconstructed honestly
``providers``     adapters to whatever the deployment is actually allowed to read
"""

from __future__ import annotations

from .features import FEATURE_VERSION

__all__ = ["FEATURE_VERSION"]
