"""Scenario registry — local file + best-effort MongoDB mirror, with lifecycle state.

Single source of truth for *what scenarios exist and their state* (plan §14.2):

    draft  — only spec.json (not compiled, not runnable)
    ready  — scenario.json present and up to date (runnable)
    stale  — spec.json edited after scenario.json (runnable on old data; tracked, not auto-acted)
    error  — last compile failed (message stored)

State for draft/ready/stale is **derived from disk** (authoritative); ``error`` is
persisted. The local ``_registry.json`` is the on-disk mirror (works with no DB); a
``container_logistics_scenario_registry`` Mongo collection mirrors it best-effort so
the dashboard can query state without filesystem access. Mongo down => local still works.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Optional

from .scenario_bundle import BUNDLE_FILENAME, bundle_exists

REGISTRY_FILENAME = "_registry.json"
SPEC_FILENAME = "spec.json"
MONGO_COLLECTION = "container_logistics_scenario_registry"

DRAFT, READY, STALE, ERROR = "draft", "ready", "stale", "error"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def derive_state(scenario_dir: str) -> Optional[str]:
    """State from disk (draft/ready/stale), or None if not a scenario folder."""
    spec = os.path.join(scenario_dir, SPEC_FILENAME)
    has_spec = os.path.isfile(spec)
    has_bundle = bundle_exists(scenario_dir)
    if not has_spec and not has_bundle:
        return None
    if has_bundle:
        if has_spec:
            try:
                if os.path.getmtime(spec) > os.path.getmtime(
                    os.path.join(scenario_dir, BUNDLE_FILENAME)
                ) + 1:  # 1s slack
                    return STALE
            except OSError:
                pass
        return READY
    return DRAFT


class ScenarioRegistry:
    def __init__(self, root: str):
        self.root = root
        self._path = os.path.join(root, REGISTRY_FILENAME)

    # ---- local store ------------------------------------------------------

    def _load(self) -> dict[str, dict]:
        if not os.path.isfile(self._path):
            return {}
        try:
            with open(self._path, encoding="utf-8") as fp:
                data = json.load(fp)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict[str, dict]) -> None:
        os.makedirs(self.root, exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(data, fp, indent=2, sort_keys=True)
            fp.write("\n")
        os.replace(tmp, self._path)

    def _dir(self, slug: str) -> str:
        return os.path.join(self.root, slug)

    # ---- mongo mirror (best-effort) --------------------------------------

    @staticmethod
    def _mongo_collection():
        try:
            from pymongo import MongoClient

            from apps.config import kpi_sink_settings as s

            uri = s.get("mongo_uri")
            client = (
                MongoClient(uri, serverSelectionTimeoutMS=800)
                if uri
                else MongoClient(s["mongo_host"], int(s["mongo_port"]), serverSelectionTimeoutMS=800)
            )
            return client[s.get("mongo_db", "OpenRoadDB")][MONGO_COLLECTION]
        except Exception:
            return None

    def _mongo_upsert(self, record: dict) -> None:
        col = self._mongo_collection()
        if col is None:
            return
        try:
            col.replace_one({"_id": record["slug"]}, {"_id": record["slug"], **record}, upsert=True)
        except Exception:
            pass

    def _mongo_delete(self, slug: str) -> None:
        col = self._mongo_collection()
        if col is None:
            return
        try:
            col.delete_one({"_id": slug})
        except Exception:
            pass

    # ---- operations -------------------------------------------------------

    def register(self, slug: str, *, name: str = "", source: str = "spec", counts: Optional[dict] = None) -> dict:
        data = self._load()
        rec = data.get(slug, {})
        rec.update(
            {
                "slug": slug,
                "name": name or rec.get("name") or slug,
                "source": source,
                "createdAt": rec.get("createdAt") or _now(),
                "updatedAt": _now(),
                "state": derive_state(self._dir(slug)) or DRAFT,
                "counts": counts or rec.get("counts") or {},
                "error": None,
            }
        )
        data[slug] = rec
        self._save(data)
        self._mongo_upsert(rec)
        return rec

    def mark_compiled(self, slug: str, *, counts: Optional[dict] = None) -> dict:
        data = self._load()
        rec = data.get(slug, {"slug": slug, "createdAt": _now()})
        rec.update({"slug": slug, "state": derive_state(self._dir(slug)) or READY, "updatedAt": _now(), "error": None})
        if counts:
            rec["counts"] = counts
        data[slug] = rec
        self._save(data)
        self._mongo_upsert(rec)
        return rec

    def mark_error(self, slug: str, message: str) -> dict:
        data = self._load()
        rec = data.get(slug, {"slug": slug, "createdAt": _now()})
        rec.update({"slug": slug, "state": ERROR, "error": str(message)[:500], "updatedAt": _now()})
        data[slug] = rec
        self._save(data)
        self._mongo_upsert(rec)
        return rec

    def delete(self, slug: str) -> None:
        data = self._load()
        if slug in data:
            del data[slug]
            self._save(data)
        self._mongo_delete(slug)

    def state(self, slug: str) -> Optional[str]:
        """Authoritative state from disk, overlaid with a persisted error."""
        disk = derive_state(self._dir(slug))
        if disk is None:
            return None
        rec = self._load().get(slug) or {}
        if rec.get("state") == ERROR and disk == DRAFT:
            return ERROR
        return disk

    def list(self) -> list[dict]:
        """All scenario folders with their derived state (self-healing)."""
        if not os.path.isdir(self.root):
            return []
        persisted = self._load()
        out: list[dict] = []
        for name in sorted(os.listdir(self.root)):
            path = os.path.join(self.root, name)
            if not os.path.isdir(path) or name.startswith(".") or name.endswith(".tmp"):
                continue
            st = derive_state(path)
            if st is None:
                continue
            rec = dict(persisted.get(name) or {})
            rec["slug"] = name
            rec["state"] = ERROR if (rec.get("state") == ERROR and st == DRAFT) else st
            rec.setdefault("name", name)
            out.append(rec)
        return out
