from mm import score_side


def test_score_side_range_and_monotonicity():
    base = {
        "coverage": 0.8,
        "spread": 0.10,
        "target_size": 300,
        "best_size": 300,
        "discount_factor_bps": 5000,  # 0.5
        "period_reward": 100,
    }
    s = score_side("yes", base)
    assert 0 <= s <= 1000

    better = dict(base, coverage=0.9, spread=0.12, best_size=400, period_reward=150)
    s_b = score_side("yes", better)
    assert s_b >= s


