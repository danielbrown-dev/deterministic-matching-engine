"""Command-line entry point.

    python -m dme.sim --seed 7 --steps 100000
    python -m dme.sim --seeds 0:100 --steps 5000 --faults all
    python -m dme.sim --seeds 0:20 --steps 5000 --reference

Prints one line per seed. On the first failure, prints a report with the command
that reproduces it and exits with status 1.
"""

from __future__ import annotations

import argparse
import sys
import time

from dme.sim.network import FAULT_PRESETS
from dme.sim.run import SimConfig, SimulationFailure, run


def _seed_range(text: str) -> range:
    start, _, stop = text.partition(":")
    if not stop:
        raise argparse.ArgumentTypeError("expected START:STOP, e.g. 0:100")
    return range(int(start), int(stop))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m dme.sim", description=__doc__.split("\n")[0])
    seeds = parser.add_mutually_exclusive_group(required=True)
    seeds.add_argument("--seed", type=int, help="run a single seed")
    seeds.add_argument("--seeds", type=_seed_range, help="run seeds START:STOP (STOP exclusive)")
    parser.add_argument("--steps", type=int, default=10_000, help="engine commands per seed")
    parser.add_argument(
        "--faults",
        choices=sorted(FAULT_PRESETS),
        default="none",
        help="inject network faults, exchange crashes, or both (default: none)",
    )
    parser.add_argument(
        "--reference", action="store_true", help="also compare against the reference model (slow)"
    )
    args = parser.parse_args(argv)

    config = SimConfig(faults=args.faults, reference=args.reference)
    seed_list = [args.seed] if args.seed is not None else list(args.seeds)
    for seed in seed_list:
        started = time.perf_counter()
        try:
            result = run(seed, args.steps, config)
        except SimulationFailure as failure:
            print(failure, file=sys.stderr)
            return 1
        elapsed = time.perf_counter() - started
        counts = result.counts
        print(
            f"seed={seed} steps={result.steps} sim_time={result.sim_time}us "
            f"trades={counts.get('Trade', 0)} dropped={counts['net:dropped']} "
            f"crashes={counts['node:crashes']} digest={result.digest[:16]} ({elapsed:.2f}s)"
        )
    if len(seed_list) > 1:
        print(f"{len(seed_list)} seeds passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
