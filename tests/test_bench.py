from __future__ import annotations

import pytest

from dme.bench import main, workload


def test_workload_is_deterministic() -> None:
    assert workload(seed=3, ops=1_000) == workload(seed=3, ops=1_000)
    assert workload(seed=3, ops=1_000) != workload(seed=4, ops=1_000)


def test_benchmark_runs(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--ops", "2000"]) == 0
    out = capsys.readouterr().out
    for name in ("OrderBook", "Engine", "Exchange (gateway)", "ReferenceBook (oracle)"):
        assert name in out
