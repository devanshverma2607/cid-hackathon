"""SOCMINT offline validation suite.

Plain runnable harness (no pytest) that exercises the correlation engine and the
new signal/calibration logic against labelled synthetic identities, reporting
precision / recall / F1 plus a set of hard invariants.

Run inside the api (or worker_python) container::

    docker compose exec -T api python -m api.validation.run
"""
