"""Haulier normalization + deterministic share distribution (pure, datagen-owned).

Moved out of ``scenario_config`` verbatim (a parity test guards the copy). A truck
may only serve orders from its own haulier; ``fleet_share``/``order_share`` are
percentages enforced to sum to 100.
"""

from __future__ import annotations

import re

HAULIER_SHARE_TOTAL = 100.0
_HAULIER_SHARE_TOLERANCE = 0.01

DEFAULT_HAULIERS = [
    {"id": "haulier", "name": "Haulier", "fleet_share": 100.0, "order_share": 100.0},
]


def haulier_slug(name, *, fallback="haulier"):
    slug = re.sub(r"[^a-z0-9]+", "_", str(name or "").strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug[:64] or fallback


def _validate_share_total(hauliers, key):
    total = sum(float(h[key]) for h in hauliers)
    if abs(total - HAULIER_SHARE_TOTAL) > _HAULIER_SHARE_TOLERANCE:
        breakdown = ", ".join(f"{h['id']}={h[key]:g}" for h in hauliers)
        raise ValueError(
            f"Haulier '{key}' percentages must sum to {HAULIER_SHARE_TOTAL:g} "
            f"(got {total:g} across {len(hauliers)} haulier(s): {breakdown})."
        )


def normalize_hauliers(raw):
    items = raw if isinstance(raw, (list, tuple)) else None
    if not items:
        return [dict(h) for h in DEFAULT_HAULIERS]

    out = []
    seen_ids = set()
    for idx, entry in enumerate(items):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("id") or f"Haulier {idx + 1}").strip()
        hid = haulier_slug(entry.get("id") or name, fallback=f"haulier_{idx}")
        if hid in seen_ids:
            suffix = 1
            base = hid
            while f"{base}_{suffix}" in seen_ids:
                suffix += 1
            hid = f"{base}_{suffix}"
        seen_ids.add(hid)

        def _share(key):
            raw_val = entry.get(key)
            if raw_val is None:
                return None
            try:
                val = float(raw_val)
            except (TypeError, ValueError):
                return None
            if val < 0:
                raise ValueError(
                    f"Haulier '{hid}' has a negative {key} ({val:g}); percentages must be >= 0."
                )
            return val

        out.append(
            {
                "id": hid,
                "name": name or hid,
                "fleet_share": _share("fleet_share"),
                "order_share": _share("order_share"),
            }
        )

    if not out:
        return [dict(h) for h in DEFAULT_HAULIERS]

    for key in ("fleet_share", "order_share"):
        if all(h[key] is None for h in out):
            equal = HAULIER_SHARE_TOTAL / len(out)
            for h in out:
                h[key] = equal
        else:
            for h in out:
                if h[key] is None:
                    h[key] = 0.0
        _validate_share_total(out, key)
    return out


# --- cooperation structures (which hauliers may share jobs) -----------------
#
# A structure is a list of undirected edge pairs ``[haulier_id, haulier_id]``.
# Self-loops are membership no-ops. Sharing is edge-direct (not transitive), but
# the *planner partition* is a connected component: pooled data extends across a
# component, pairing eligibility only across edges. Normalization is pure and
# deterministic: edges symmetrized + deduped + sorted, members validated against
# the scenario's haulier ids, components derived via union-find.

DEFAULT_STRUCTURE_ID = "no-coop"


def _cooperation_member(value, known_ids, where):
    member = haulier_slug(value, fallback="")
    if member not in known_ids:
        raise ValueError(
            f"{where}: unknown haulier id {str(value)!r} (normalized {member!r}); "
            f"known haulier ids: {sorted(known_ids)}."
        )
    return member


def _normalize_edges(raw_edges, known_ids, where):
    if raw_edges is None:
        return []
    if not isinstance(raw_edges, (list, tuple)):
        raise ValueError(f"{where}: 'edges' must be a list of [haulier_id, haulier_id] pairs.")
    edges = set()
    for idx, entry in enumerate(raw_edges):
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            raise ValueError(
                f"{where}: edge #{idx} must be a 2-element [haulier_id, haulier_id] pair "
                f"(got {entry!r})."
            )
        a = _cooperation_member(entry[0], known_ids, f"{where} edge #{idx}")
        b = _cooperation_member(entry[1], known_ids, f"{where} edge #{idx}")
        if a == b:
            continue  # self-loop: membership no-op, never a sharing link
        edges.add((min(a, b), max(a, b)))
    return [list(pair) for pair in sorted(edges)]


def adjacency_of(edges):
    """Symmetric partner adjacency {haulier_id: sorted [partner ids]} from edge pairs."""
    adj = {}
    for a, b in edges:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    return {hid: sorted(partners) for hid, partners in sorted(adj.items())}


def components_of(edges, all_ids):
    """Connected components over ``all_ids`` (isolated hauliers = singletons).

    One planner per component: this is the data partition (privacy-correct — never
    pools across a non-edge; lossless — cross-component pairs are infeasible anyway).
    """
    parent = {hid: hid for hid in all_ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    groups = {}
    for hid in all_ids:
        groups.setdefault(find(hid), []).append(hid)
    return sorted(sorted(members) for members in groups.values())


def structure_id_slug(value, *, fallback=""):
    """Public: the slug form of a structure id (used at compile AND when matching
    per-run override input, so '--cooperation-structure "Port Alliance"' finds the
    compiled 'port-alliance' structure instead of silently falling back)."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:64] or fallback


# Back-compat internal alias.
_structure_id_slug = structure_id_slug


# --- pools (the canonical sharing primitive; edges are authoring sugar) ------
#
# Shared-pool plan §2 D1 / §3.3: ``pools`` is what the runtime market consumes,
# ``edges`` stays a first-class authoring form. Both directions are deterministic:
#   edges -> pools : ONE 2-member pool per edge (preserves today's edge-direct,
#                    non-transitive eligibility exactly)
#   pools -> edges : all member pairs, canonical [min, max], deduped, sorted
# so ``adjacency``/``components`` (still derived from ``edges``) never change.

DERIVED_POOL_PREFIX = "p:"


def pool_id_slug(value, *, fallback=""):
    """Slug form of an author-supplied pool id (mirrors :func:`structure_id_slug`)."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:64] or fallback


def _derived_pool_id(members):
    """``p:acme+borax`` — deterministic, stable across recompiles, readable in logs."""
    return DERIVED_POOL_PREFIX + "+".join(sorted(members))


def derive_pools_from_edges(edges):
    """One 2-member pool per edge. Input is already-normalized edge pairs."""
    pools = {}
    for pair in edges:
        members = sorted({str(pair[0]), str(pair[1])})
        if len(members) < 2:
            continue
        pools[_derived_pool_id(members)] = members
    return [{"id": pid, "members": list(pools[pid])} for pid in sorted(pools)]


def derive_edges_from_pools(pools):
    """All member pairs of every pool, canonical ``[min, max]``, deduped and sorted."""
    edges = set()
    for pool in pools:
        members = sorted(set(pool.get("members") or []))
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                edges.add((min(a, b), max(a, b)))
    return [list(pair) for pair in sorted(edges)]


def _normalize_pools(raw_pools, known_ids, where):
    """Validate + canonicalize an authored ``pools`` list.

    Members are normalized haulier ids (unknown -> ``ValueError``, same message
    style as :func:`_cooperation_member`); a pool with < 2 distinct members is
    dropped silently (mirrors the self-loop rule); members are sorted; pools are
    sorted by id; duplicate author-supplied ids get ``-1``/``-2`` suffixes exactly
    like structure ids.
    """
    if raw_pools is None:
        return []
    if not isinstance(raw_pools, (list, tuple)):
        raise ValueError(f"{where}: 'pools' must be a list of {{id, members}} objects.")

    out = []
    seen_ids = set()
    for idx, entry in enumerate(raw_pools):
        pool_where = f"{where} pool #{idx}"
        if not isinstance(entry, dict):
            raise ValueError(
                f"{pool_where}: must be an object with 'members' (got {entry!r})."
            )
        raw_members = entry.get("members")
        if raw_members is None:
            raw_members = []
        if not isinstance(raw_members, (list, tuple)):
            raise ValueError(f"{pool_where}: 'members' must be a list of haulier ids.")
        members = sorted({_cooperation_member(m, known_ids, pool_where) for m in raw_members})
        if len(members) < 2:
            continue  # membership no-op, exactly like a self-loop edge

        raw_id = entry.get("id")
        pid = _derived_pool_id(members) if raw_id is None else pool_id_slug(
            raw_id, fallback=_derived_pool_id(members)
        )
        if pid in seen_ids:
            suffix = 1
            while f"{pid}-{suffix}" in seen_ids:
                suffix += 1
            pid = f"{pid}-{suffix}"
        seen_ids.add(pid)
        out.append({"id": pid, "members": members})

    return sorted(out, key=lambda p: p["id"])


def _raw_structure_entries(raw):
    """Tolerant twin of the ``structures_raw`` extraction in :func:`normalize_cooperation`.

    Never raises — it is only used by :func:`authored_pool_structure_ids`, which must
    work on any raw form (validation is normalize_cooperation's job, and the caller
    runs it first).
    """
    if raw is None or raw == {} or raw == []:
        return []
    if isinstance(raw, dict):
        structures_raw = raw.get("structures")
        return list(structures_raw) if isinstance(structures_raw, (list, tuple)) else []
    if isinstance(raw, (list, tuple)):
        first = raw[0] if raw else None
        is_edge_list = (
            isinstance(first, (list, tuple))
            and len(first) == 2
            and all(isinstance(x, str) for x in first)
        )
        return [list(raw)] if is_edge_list else list(raw)
    return []


def authored_pool_structure_ids(raw):
    """Slugified ids of the structures whose AUTHORED entry supplied ``pools``.

    Lets the recipe echo ``pools`` back only where the author wrote it, so an
    edges-authored scenario round-trips byte-identically (plan §6.10 / I4).
    Mirrors normalize_cooperation's id assignment (slug + ``-1``/``-2`` dedupe).
    """
    out = set()
    seen_ids = set()
    for idx, entry in enumerate(_raw_structure_entries(raw)):
        if isinstance(entry, dict):
            sid = _structure_id_slug(entry.get("id"), fallback=f"structure-{idx}")
        else:
            sid = f"structure-{idx}"
        if sid in seen_ids:
            suffix = 1
            while f"{sid}-{suffix}" in seen_ids:
                suffix += 1
            sid = f"{sid}-{suffix}"
        seen_ids.add(sid)
        if isinstance(entry, dict) and entry.get("pools") is not None:
            out.add(sid)
    return out


def normalize_cooperation(raw, haulier_ids):
    """Normalize a spec's ``cooperation`` block. Pure; raises ``ValueError`` on bad input.

    Accepts: absent/None (=> a single no-edge 'no-coop' structure — exactly
    today's no-collaboration behavior), a full ``{"active": ..., "structures": [...]}``
    dict, a bare list of structures, or a bare list of edge pairs (one anonymous
    structure). Each structure may be ``{"id", "edges"}``, ``{"id", "pools"}`` or a
    bare edge-pair list; authoring both ``pools`` and ``edges`` is only allowed when
    they agree exactly (else ``ValueError``).

    Returns ``{"active": <structure id>, "structures": [{"id", "edges", "adjacency",
    "components", "pools"}, ...]}`` with every derived field deterministic and sorted.
    ``adjacency``/``components`` are still computed from ``edges`` — unchanged.
    """
    known_ids = set(haulier_ids)
    if not known_ids:
        raise ValueError("cooperation: cannot normalize without haulier ids.")

    active_raw = None
    if raw is None or raw == {} or raw == []:
        structures_raw = []
    elif isinstance(raw, dict):
        structures_raw = raw.get("structures")
        if structures_raw is None:
            structures_raw = []
        if not isinstance(structures_raw, (list, tuple)):
            raise ValueError("cooperation: 'structures' must be a list.")
        active_raw = raw.get("active")
    elif isinstance(raw, (list, tuple)):
        # A bare list of edge pairs (first entry looks like [id, id]) is ONE
        # anonymous structure; otherwise it is a list of structures.
        first = raw[0] if raw else None
        is_edge_list = (
            isinstance(first, (list, tuple))
            and len(first) == 2
            and all(isinstance(x, str) for x in first)
        )
        structures_raw = [list(raw)] if is_edge_list else list(raw)
    else:
        raise ValueError("cooperation: must be an object or a list.")

    all_ids = sorted(known_ids)
    structures = []
    seen_ids = set()
    for idx, entry in enumerate(structures_raw):
        where = f"cooperation structure #{idx}"
        if isinstance(entry, dict):
            raw_pools = entry.get("pools")
            raw_edges = entry.get("edges")
            if raw_pools is not None:
                # Pools are canonical: edges are DERIVED from them. If the author
                # also wrote edges, they must agree exactly (never silently pick one).
                pools = _normalize_pools(raw_pools, known_ids, where)
                edges = derive_edges_from_pools(pools)
                if raw_edges is not None:
                    authored_edges = _normalize_edges(raw_edges, known_ids, where)
                    if edges != authored_edges:
                        raise ValueError(
                            f"{where}: 'pools' and 'edges' disagree; author one or the other."
                        )
            else:
                edges = _normalize_edges(raw_edges, known_ids, where)
                pools = derive_pools_from_edges(edges)
            sid = _structure_id_slug(entry.get("id"), fallback=f"structure-{idx}")
        elif isinstance(entry, (list, tuple)):
            edges = _normalize_edges(entry, known_ids, where)
            pools = derive_pools_from_edges(edges)
            sid = f"structure-{idx}"
        else:
            raise ValueError(f"{where}: must be an object with 'edges' or a list of edge pairs.")
        if sid in seen_ids:
            suffix = 1
            while f"{sid}-{suffix}" in seen_ids:
                suffix += 1
            sid = f"{sid}-{suffix}"
        seen_ids.add(sid)
        structures.append(
            {
                "id": sid,
                "edges": edges,
                "adjacency": adjacency_of(edges),
                "components": components_of(edges, all_ids),
                "pools": pools,
            }
        )

    # 'active' resolves against the DECLARED structures (default: the first one) —
    # the auto-inserted baseline below must never steal the default.
    declared = list(structures)

    # Always guarantee a no-edge baseline structure exists so per-run overrides can
    # always fall back to today's no-collaboration behavior.
    if not any(not s["edges"] for s in structures):
        baseline_id = DEFAULT_STRUCTURE_ID if DEFAULT_STRUCTURE_ID not in seen_ids else "baseline"
        structures.insert(
            0,
            {
                "id": baseline_id,
                "edges": [],
                "adjacency": {},
                "components": components_of([], all_ids),
                "pools": [],
            },
        )

    if active_raw is None:
        active_id = (declared[0] if declared else structures[0])["id"]
    elif isinstance(active_raw, bool):
        raise ValueError("cooperation: 'active' must be a structure id or index.")
    elif isinstance(active_raw, int):
        if not 0 <= active_raw < len(declared):
            raise ValueError(
                f"cooperation: 'active' index {active_raw} out of range "
                f"(have {len(declared)} declared structure(s))."
            )
        active_id = declared[active_raw]["id"]
    else:
        wanted = _structure_id_slug(active_raw, fallback="")
        by_id = {s["id"]: s for s in structures}
        if wanted not in by_id:
            raise ValueError(
                f"cooperation: 'active' structure {str(active_raw)!r} not found; "
                f"declared: {sorted(by_id)}."
            )
        active_id = wanted

    return {"active": active_id, "structures": structures}


def active_structure(cooperation):
    """The active structure dict from a normalized cooperation block (never None)."""
    by_id = {s["id"]: s for s in cooperation.get("structures", [])}
    return by_id[cooperation["active"]]


def distribute_by_share(n, hauliers, share_key):
    hauliers = normalize_hauliers(hauliers)
    n = max(0, int(n))
    if n == 0:
        return []
    if len(hauliers) == 1:
        return [hauliers[0]] * n

    weights = [max(0.0, float(h.get(share_key) or 0.0)) for h in hauliers]
    total = sum(weights)
    if total <= 0:
        weights = [1.0] * len(hauliers)
        total = float(len(hauliers))

    raw = [w / total * n for w in weights]
    counts = [int(x) for x in raw]
    remainder = n - sum(counts)
    order = sorted(range(len(hauliers)), key=lambda i: raw[i] - counts[i], reverse=True)
    for i in range(remainder):
        counts[order[i % len(order)]] += 1

    pools = [[hauliers[i]] * counts[i] for i in range(len(hauliers))]
    result = []
    while len(result) < n:
        for pool in pools:
            if pool:
                result.append(pool.pop())
                if len(result) == n:
                    break
    return result
