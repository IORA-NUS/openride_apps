import sys
import os

# Ensure the project root is in sys.path for pytest
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)


def pytest_configure(config):
    # Opt-in live tests: `pytest -m redis_live` (they also skip unless selected that way).
    config.addinivalue_line(
        "markers", "redis_live: needs the local Redis (db 15); run only with -m redis_live")
