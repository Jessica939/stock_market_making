# Shared strategy infrastructure

This package contains strategy-neutral interfaces. Strategy signal, position-lifecycle,
and strategy-specific validation belong in the corresponding strategy directory.

- `market.py` and `depth_guard.py`: validated public-market views and price limits.
- `execution.py`: verified IOC execution used by directional/pair strategies.
- `quoting.py`: passive quoting interface used by baseline and hybrid. The original
  top-level modules remain compatibility implementations for notebooks and deployments.
- `simulation.py`: replay exchange and clock.
- `runner.py`: shared feed, journal, generic session lifecycle, and CLI plumbing.

`common` must not import `baseline`, `pair`, `b_cycle`, or `hybrid`. Strategies may
extend the generic session through its lifecycle hooks; for example, pair holding and
market-gap behavior live in `pair/session.py`.

The two execution styles are intentionally separate. Passive market making maintains
resting quotes, while IOC execution requires terminal-fill reconciliation and pair-leg
recovery. Sharing their public location does not make their order semantics interchangeable.
