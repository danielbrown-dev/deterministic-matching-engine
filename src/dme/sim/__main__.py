"""Command-line entry point.

    python -m dme.sim --seed 7 --steps 100000
    python -m dme.sim --seeds 0:100 --steps 5000 --faults all
    python -m dme.sim --seeds 0:20 --steps 5000 --reference
    python -m dme.sim --bugs                    # plant each known bug, show it is caught
    python -m dme.sim --seed 0 --bug no-dedup   # plant one

Prints one line per seed. On the first failure, prints a report with the command
that reproduces it and exits with status 1.
"""

from __future__ import annotations

import argparse
import sys
import time

from dme.sim.bugs import BY_NAME, CATALOGUE
from dme.sim.network import FAULT_PRESETS
from dme.sim.run import SimConfig, SimulationFailure, run

BUG_DEMO_STEPS = 50_000


def _seed_range(text: str) -> range:
    start, _, stop = text.partition(":")
    if not stop:
        raise argparse.ArgumentTypeError("expected START:STOP, e.g. 0:100")
    return range(int(start), int(stop))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m dme.sim", description=__doc__.split("\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--seed", type=int, help="run a single seed")
    mode.add_argument("--seeds", type=_seed_range, help="run seeds START:STOP (STOP exclusive)")
    mode.add_argument(
        "--bugs", action="store_true", help="plant each catalogued bug and show it is caught"
    )
    parser.add_argument("--steps", type=int, default=10_000, help="requests to log per seed")
    parser.add_argument(
        "--faults",
        choices=sorted(FAULT_PRESETS),
        help="inject network faults, exchange crashes, or both (default: none, or the "
        "planted bug's own mode)",
    )
    parser.add_argument(
        "--reference", action="store_true", help="also compare against the reference model (slow)"
    )
    parser.add_argument("--bug", choices=sorted(BY_NAME), help="plant a bug from the catalogue")
    args = parser.parse_args(argv)

    if args.bugs:
        return _demo_bugs()

    faults = args.faults or (BY_NAME[args.bug].faults if args.bug else "none")
    config = SimConfig(faults=faults, reference=args.reference, planted_bug=args.bug)
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


def _demo_bugs() -> int:
    """Plant each bug in turn on seed 0 and report where the checks caught it."""
    print(f"{'bug':<22}{'faults':<8}{'caught at':>10}  check that fired")
    missed = 0
    for bug in CATALOGUE:
        config = SimConfig(faults=bug.faults, planted_bug=bug.name)
        try:
            run(0, BUG_DEMO_STEPS, config)
        except SimulationFailure as failure:
            cause = str(failure.cause)[:60]
            print(f"{bug.name:<22}{bug.faults:<8}{failure.step:>10,}  {cause}")
            continue
        missed += 1
        print(
            f"{bug.name:<22}{bug.faults:<8}{'MISSED':>10}  not caught in {BUG_DEMO_STEPS:,} steps"
        )
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())
