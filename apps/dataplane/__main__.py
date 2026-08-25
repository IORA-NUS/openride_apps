"""``python -m apps.dataplane`` — run the supervised dataplane process."""

from __future__ import annotations

from apps.dataplane.service import main

if __name__ == "__main__":
    raise SystemExit(main())
