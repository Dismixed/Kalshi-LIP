from mm import LIPBot


def test_order_capital_required(bot_factory):
    bot, _ = bot_factory(balance=100)
    # price = 0.40
    assert bot.order_capital_required("yes", "buy", 0.40, 1) == 0.40
    assert bot.order_capital_required("no", "buy", 0.40, 1) == 0.60
    assert bot.order_capital_required("yes", "sell", 0.40, 1) == 0.60
    assert bot.order_capital_required("no", "sell", 0.40, 1) == 0.40
    # fees add linearly
    assert bot.order_capital_required("yes", "buy", 0.40, 2, fee_per_contract=0.01) == 0.80 + 0.02


def test_max_affordable_size(bot_factory):
    bot, _ = bot_factory(balance=100)
    # spendable = 100 * (1 - 0.15) = 85; per-market = 85 * 0.25 = 21.25
    # unit = 0.40 -> floor(21.25 / 0.40) = 53
    size = bot.max_affordable_size("yes", "buy", 0.40,
                                   balance_reserve_frac=0.15,
                                   per_market_budget_frac=0.25,
                                   fee_per_contract=0.00)
    assert size == 53


def test_compute_desired_size_respects_capacity_and_balance(bot_factory):
    bot, _ = bot_factory(balance=10, max_position=50)
    # With low balance, affordable size should cap desired size
    desired = bot.compute_desired_size("yes", "buy", price=0.50, spread=0.02, inventory=0, min_order_size=1)
    assert desired >= 1
    assert desired <= 50


