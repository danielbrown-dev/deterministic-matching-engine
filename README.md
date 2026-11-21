# deterministic-matching-engine

A price-time priority matching engine whose correctness is checked by a deterministic
simulation harness: every run is a pure function of a seed, so any failure found among
millions of simulated orders replays exactly.

> Work in progress.

## Development

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
```
