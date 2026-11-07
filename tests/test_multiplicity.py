from gateadapt import SaffronPool


class TestSaffronPool:
    def test_equal_quarter_allocation(self):
        p = SaffronPool(wealth=0.10, looks=4)
        assert p.allocate() == 0.025

    def test_looks_left_decrements_on_allocate(self):
        p = SaffronPool(wealth=0.10, looks=4)
        assert p.looks_left == 4
        p.allocate()
        p.allocate()
        assert p.looks_left == 2

    def test_spend_drains_remaining_wealth(self):
        p = SaffronPool(wealth=0.10, looks=4)
        a = p.allocate()
        p.spend(a)
        assert p.remaining_wealth == 0.10 - a

    def test_exhausted_pool_allocates_nothing(self):
        p = SaffronPool(wealth=0.10, looks=4)
        for _ in range(4):
            a = p.allocate()
            p.spend(a)
        assert p.allocate() == 0.0

    def test_decided_look_refunds_quarter(self):
        p = SaffronPool(wealth=0.10, looks=4, refund_decided=True)
        a = p.allocate()
        p.spend(a)
        p.refund(a)
        assert p.remaining_wealth == 0.10

    def test_no_refund_ablation_consumes_every_look(self):
        p = SaffronPool(wealth=0.10, looks=4, refund_decided=False)
        a = p.allocate()
        p.spend(a)
        p.refund(a)
        assert p.remaining_wealth == 0.10 - a

    def test_wealth_gates_looks_not_count(self):
        # a small pool cannot fund four full quarters even though looks remain
        p = SaffronPool(wealth=0.05, looks=4)
        assert p.allocate() == 0.0125
        p.spend(0.0125)
        assert p.allocate() == 0.0125
        p.spend(0.0125)
        assert p.allocate() == 0.0125
        p.spend(0.0125)
        assert p.allocate() == 0.0125  # look 4, full quarter still there
        p.spend(0.0125)
        assert p.allocate() == 0.0

    def test_decisive_family_keeps_testing(self):
        # refund on decision. Four decided looks cost net zero, and a fifth
        # look still funds, wealth, not look count, is the gate
        p = SaffronPool(wealth=0.10, looks=4, refund_decided=True)
        for _ in range(4):
            a = p.allocate()
            p.spend(a)
            p.refund(a)
        assert p.remaining_wealth == 0.10
        assert p.allocate() == 0.025
