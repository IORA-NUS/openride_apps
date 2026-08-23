"""OpenRide unified command-line interface.

One verb-based CLI over the whole container-logistics stack: construct + maintain
scenarios, run simulations (headless or with the live geo streaming), and analyze
runs. It reuses the dashboard's machinery — it shells out to ``openride_control`` for
every scenario/service primitive and launches the *same* sim the dashboard does — so
results are identical, just driven from a terminal instead of the browser.

Verb groups: ``run``, ``scenario``, ``analyze``, ``runs``, ``services``, ``solver``.

Entry point: ``python -m openride``  (run with the pyjupenv interpreter, which has
rich/questionary/pymongo). Launcher: ``scripts/openride.sh``.
"""

__all__ = ["__version__"]
__version__ = "0.2.0"
