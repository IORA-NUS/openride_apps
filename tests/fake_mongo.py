"""A tiny hand-rolled in-memory Mongo stand-in for the order-lifecycle tests.

Deliberately NOT mongomock: the batch writer only ever uses a handful of operators
(``$set``, ``$nin``, ``$in``, ``$lte``, ``$lt``) plus dotted field paths, and a ~100-line fake
that implements exactly those keeps the tests honest about which semantics are load-bearing.
Anything outside that set raises, so a future query that needs more can't silently pass.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List


class _Result:
    def __init__(self, matched_count: int = 0, modified_count: int = 0):
        self.matched_count = matched_count
        self.modified_count = modified_count


def _dotted_get(doc: Dict[str, Any], path: str, default=None):
    cur: Any = doc
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _dotted_set(doc: Dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = doc
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


_MISSING = object()


def _match_clause(value: Any, clause: Any) -> bool:
    if isinstance(clause, dict) and clause and all(str(k).startswith("$") for k in clause):
        for op, operand in clause.items():
            if op == "$nin":
                if value in operand:
                    return False
            elif op == "$in":
                if value not in operand:
                    return False
            elif op == "$lte":
                if value is _MISSING or value is None or not value <= operand:
                    return False
            elif op == "$lt":
                if value is _MISSING or value is None or not value < operand:
                    return False
            else:
                raise NotImplementedError(f"FakeCollection: unsupported operator {op!r}")
        return True
    return value == clause


def _matches(doc: Dict[str, Any], flt: Dict[str, Any]) -> bool:
    for key, clause in flt.items():
        if key == "$or":
            if not any(_matches(doc, sub) for sub in clause):
                return False
            continue
        if str(key).startswith("$"):
            raise NotImplementedError(f"FakeCollection: unsupported top-level operator {key!r}")
        value = _dotted_get(doc, key, _MISSING)
        if value is _MISSING and not (isinstance(clause, dict) and clause and all(
            str(k).startswith("$") for k in clause
        )):
            # A plain equality clause against an absent field only matches an explicit None.
            if clause is not None:
                return False
            continue
        if not _match_clause(value, clause):
            return False
    return True


def _apply_update(doc: Dict[str, Any], update: Dict[str, Any]) -> bool:
    if set(update) - {"$set"}:
        raise NotImplementedError(f"FakeCollection: unsupported update {sorted(update)}")
    changed = False
    for path, value in (update.get("$set") or {}).items():
        if _dotted_get(doc, path, _MISSING) != value:
            changed = True
        _dotted_set(doc, path, value)
    return changed


class FakeCollection:
    """In-memory collection keyed by ``_id``."""

    def __init__(self, docs=None):
        self.docs: Dict[Any, Dict[str, Any]] = {}
        for doc in docs or []:
            self.docs[doc["_id"]] = copy.deepcopy(doc)
        self.bulk_write_calls: List[List[Any]] = []

    # -- reads ------------------------------------------------------------------

    def find(self, flt: Dict[str, Any], projection=None):
        out = []
        for doc in self.docs.values():
            if not _matches(doc, flt):
                continue
            if projection:
                picked = {"_id": doc.get("_id")}
                for key in projection:
                    if key == "_id":
                        continue
                    # Dotted projections ("meta.cancel_reason") keep their nesting, like Mongo.
                    value = _dotted_get(doc, key, _MISSING)
                    if value is not _MISSING:
                        _dotted_set(picked, key, copy.deepcopy(value))
                out.append(picked)
            else:
                out.append(copy.deepcopy(doc))
        return out

    def find_one(self, flt: Dict[str, Any], projection=None):
        found = self.find(flt, projection)
        return found[0] if found else None

    def distinct(self, field: str, flt: Dict[str, Any] | None = None):
        seen = []
        for doc in self.docs.values():
            if flt and not _matches(doc, flt):
                continue
            value = _dotted_get(doc, field, _MISSING)
            if value is not _MISSING and value not in seen:
                seen.append(value)
        return seen

    # -- writes -----------------------------------------------------------------

    def update_many(self, flt: Dict[str, Any], update: Dict[str, Any]) -> _Result:
        matched = modified = 0
        for doc in self.docs.values():
            if not _matches(doc, flt):
                continue
            matched += 1
            if _apply_update(doc, update):
                modified += 1
        return _Result(matched, modified)

    def bulk_write(self, ops, ordered=False) -> _Result:
        self.bulk_write_calls.append(list(ops))
        matched = modified = 0
        for op in ops:
            flt = getattr(op, "_filter")
            update = getattr(op, "_doc")
            for doc in self.docs.values():
                if not _matches(doc, flt):
                    continue
                matched += 1
                if _apply_update(doc, update):
                    modified += 1
                break  # UpdateOne
        return _Result(matched, modified)

    def insert_many(self, docs, ordered=False):
        for doc in docs:
            self.docs[doc["_id"]] = copy.deepcopy(doc)
        return _Result(len(docs), len(docs))
