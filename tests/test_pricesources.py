"""The plug-in interface for optional graded-price sources."""
from pokedeals import pricesources as ps
from pokedeals.pricesources import GradedPrice, SourceError


class Fake:
    def __init__(self, name, prices=(), ready=True, fail=False):
        self.name, self._prices, self._ready, self._fail = name, list(prices), ready, fail

    def available(self):
        return self._ready

    def graded_prices(self, card):
        if self._fail:
            raise SourceError("rate limited")
        return self._prices


def test_only_configured_and_ready_sources_are_used(monkeypatch):
    monkeypatch.setattr(ps, "_REGISTRY", {})
    ps.register("a", lambda: Fake("a"))
    ps.register("idle", lambda: Fake("idle", ready=False))
    ps.register("broken", lambda: (_ for _ in ()).throw(RuntimeError("bad plug-in")))
    assert [s.name for s in ps.enabled_sources(("a", "idle", "broken", "unknown"))] == ["a"]
    assert ps.enabled_sources(()) == []                                    # nothing is on by default
    assert ps.registered() == ["a", "broken", "idle"]


def test_the_first_source_with_the_grade_wins_and_failures_are_skipped():
    psa10 = GradedPrice("PSA", "10", 3400.0, sales=12)
    sources = [Fake("down", fail=True), Fake("none"), Fake("ok", [GradedPrice("BGS", "9.5", 900.0), psa10])]
    assert ps.graded_price(sources, card=None, grader="psa", grade="10") == psa10
    assert ps.graded_price(sources, card=None, grader="CGC", grade="10") is None
