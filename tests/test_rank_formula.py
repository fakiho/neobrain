"""Pure formula math for the deterministic rank engine (SPEC §4.2)."""
from __future__ import annotations

import math

import pytest

from neobrain import rank


def test_feedback_prior_and_signal_values():
    # No signals -> exactly the 0.5 prior.
    assert rank.feedback_score((0, 0, 0), 10.0) == pytest.approx(0.5)
    # (used, useful, noise): useful=1.0, used=0.6, noise=0.0.
    assert rank.feedback_score((0, 1, 0), 10.0) == pytest.approx(6.0 / 11.0)
    assert rank.feedback_score((1, 0, 0), 10.0) == pytest.approx((0.6 + 5.0) / 11.0)
    assert rank.feedback_score((0, 0, 1), 10.0) == pytest.approx(5.0 / 11.0)
    # prior_n=0 -> plain mean; empty stays neutral.
    assert rank.feedback_score((1, 1, 0), 0.0) == pytest.approx(0.8)
    assert rank.feedback_score((0, 0, 0), 0.0) == pytest.approx(0.5)
    # many noise signals drive the mean toward 0.
    assert rank.feedback_score((0, 0, 90), 10.0) == pytest.approx(5.0 / 100.0)


def test_recency_half_life():
    assert rank.recency_score(0.0, 21.0) == 1.0
    assert rank.recency_score(21.0, 21.0) == pytest.approx(0.5)
    assert rank.recency_score(42.0, 21.0) == pytest.approx(0.25)
    assert rank.recency_score(-5.0, 21.0) == 1.0  # future ts clamped
    assert rank.recency_score(10.0, 0.0) == 1.0   # degenerate half-life


def test_connectivity_normalisation():
    assert rank.connectivity_score(0, 5) == 0.0
    assert rank.connectivity_score(5, 5) == 1.0
    assert rank.connectivity_score(3, 3) == 1.0
    assert rank.connectivity_score(1, 3) == pytest.approx(math.log(2) / math.log(4))
    assert rank.connectivity_score(0, 0) == 0.0
    assert rank.connectivity_score(4, 3) == 1.0  # defensive: never above 1


def test_quality_is_the_product_of_three_bounded_factors():
    # quality = feedback * (decay_floor + (1-decay_floor)*recency)
    #                   * (conn_floor + (1-conn_floor)*connectivity)
    q = rank.quality_score
    kw = dict(decay_floor=0.35, conn_floor=0.5)
    assert q(0.5, 1.0, 0.0, **kw) == pytest.approx(0.5 * 1.0 * 0.5)
    assert q(1.0, 1.0, 1.0, **kw) == pytest.approx(1.0)
    assert q(0.0, 0.0, 0.0, **kw) == 0.0
    # recency and connectivity only ever remove a bounded fraction:
    assert q(1.0, 0.0, 1.0, **kw) == pytest.approx(0.35)
    assert q(1.0, 1.0, 0.0, **kw) == pytest.approx(0.5)
    assert q(0.5, 0.0, 0.0, **kw) == pytest.approx(0.5 * 0.35 * 0.5)
    assert rank.clamp01(-1.0) == 0.0
    assert rank.clamp01(2.0) == 1.0


def test_feedback_alone_can_reach_the_forgetting_threshold():
    """Calibration guard (S4 review): with the shipped defaults a flood of
    ``noise`` must push even a *fresh, well-connected* atom under the forgetting
    threshold. Rank is driven by model feedback, not only by ageing
    (SPEC §4.2/4.3: high exposure + low rank -> archived)."""
    p = rank.params()
    quality = rank.quality_score(
        rank.feedback_score((0, 0, 30), p.prior_n),  # ~0.125
        1.0,                                          # fresh
        1.0,                                          # maximally connected
        decay_floor=p.decay_floor,
        conn_floor=p.conn_floor,
    )
    assert quality < p.ignore_below


def test_exposure_and_classify_boundaries():
    assert rank.exposure_score(3, 2) == 3 + 3 * 2
    assert rank.exposure_score(0, 0) == 0
    # quality below threshold: exposure picks the state.
    assert rank.classify(0.10, 4, ignore_below=0.15, archive_exposure=5) == "ignored"
    assert rank.classify(0.10, 5, ignore_below=0.15, archive_exposure=5) == "archived"
    assert rank.classify(0.10, 9, ignore_below=0.15, archive_exposure=5) == "archived"
    # quality exactly at the threshold is NOT forgotten (strict <).
    assert rank.classify(0.15, 9, ignore_below=0.15, archive_exposure=5) == "active"
    assert rank.classify(0.149999, 0, ignore_below=0.15, archive_exposure=5) == "ignored"
