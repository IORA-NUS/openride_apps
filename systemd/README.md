# systemd user units

Templates for the seven `openride-*` user units. `scripts/start_openride.sh`
installs them into `~/.config/systemd/user/`, substituting `@WORKSPACE@` for the
workspace root (the directory holding `openride_apps/` and `openride_server/`).

They were previously **only** on the development box and in no repository, while
`start_openride.sh` — which CLAUDE.md §8 names as the way to start the stack —
ran `systemctl --user enable --now <unit>` under `set -euo pipefail`. On any
machine that had never had them hand-installed, that call failed and the script
aborted on the first unit.

Regenerate a template from a live unit with:

    sed 's|/home/user|@WORKSPACE@|g' ~/.config/systemd/user/openride-x.service \
      > systemd/openride-x.service.in

`openride-kpi-duckdb-sink` is retired (stopped and disabled 2026-08-12); its
template is kept so the retirement stays reversible, and `start_openride.sh`
installs but does not enable it.
