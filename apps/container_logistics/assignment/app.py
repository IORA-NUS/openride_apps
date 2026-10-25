from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Tuple

# Appended to an UnknownPolicyError raised agent-side (celery journal, not sim.log):
# a name the worker does not know is the one failure a stale worker explains.
_STALE_WORKER_HINT = (
    " If you just added this policy, restart the celery workers "
    "(systemctl --user restart openride-celery): agents import policy code once at worker start."
)

from apps.common.user_registry import UserRegistry
from apps.container_logistics.message_data_models import AssignedHaulTripPayload
from apps.container_logistics.rebate import RebateBook
from apps.container_logistics.statemachine import ContainerLogisticsActions, OrderStateMachine
from apps.container_logistics.policies.compat.run_stats import LegacyAppAccessors
from apps.container_logistics.policies.compat.wire import wire_fields
from apps.container_logistics.policies.framework import UnknownPolicyError
from orsim.lifecycle import ORSimApp

from . import accounting
from . import constraints as assign_constraints
from . import planning as assign_planning
from .manager import AssignmentManager

# Optional: run each (CPU-bound) per-group solve in a real worker thread so it yields
# the eventlet hub instead of blocking the other agents sharing the worker process.
# Off by default — with spatial matching the solve is small enough that this rarely
# matters; enable via the assignment profile (``solve_in_thread``) for very heavy
# scenarios. It reaches the solver as ``ctx.execute``. Guarded so non-eventlet
# contexts (tests, smoke) still work.
try:  # pragma: no cover - depends on runtime pool
    from eventlet import tpool as _eventlet_tpool
except Exception:  # pragma: no cover
    _eventlet_tpool = None


def _direct_call(fn, *args):
    """``ctx.execute`` without ``solve_in_thread``: a plain call."""
    return fn(*args)


class AssignmentApp(LegacyAppAccessors, ORSimApp):
    """Periodic assignment: match unassigned orders to feasible online trucks, then notify trucks."""

    def __init__(self, run_id: str, sim_clock: str, behavior: Dict[str, Any], messenger, agent_helper=None):
        super().__init__(
            run_id=run_id,
            sim_clock=sim_clock,
            behavior=behavior,
            messenger=messenger,
            agent_helper=agent_helper,
        )
        # Plug-and-play dispatch selection: the compiled ``policies.planner`` block,
        # or the legacy keys, resolves to a selection TREE of registered dispatch
        # policies. It is checked and BOUND HERE, eagerly, failing loud (final review
        # M12): ``_check_selection_at_start`` runs the sim main's own run-start check
        # (bundle format -- a round-1 flat block is refused with the sim main's
        # message --, resolution, mirrors, binding), then the tree is built and bound
        # exactly as every tick binds it. Any refusal raises out of ``__init__``, so
        # ORSim logs ONE error and marks the agent failed: the agent refuses to run
        # instead of logging the same error on every tick. An unknown name raises (no
        # fallback); only that error gets the stale-worker suffix -- a param or timing
        # error is not explained by it.
        prof = self.behavior.get("profile") or {}
        agent_id = str(getattr(agent_helper, "unique_id", None) or "assignment")
        try:
            self._check_selection_at_start(agent_id, prof)
            self._solver = self._build_dispatch_policy(prof)
        except UnknownPolicyError as exc:
            raise type(exc)(f"{exc}{_STALE_WORKER_HINT}") from exc
        self._solver_params = prof.get("solver_params") or {}
        # Logged once if haulier scoping blocks all matches (likely missing haulier_id).
        self._warned_haulier_block = False
        # (truck_id, order_id) -> collaboration tag for the current tick's matches.
        self._share_tags: Dict[Tuple[str, str], Dict[str, Any]] = {}
        # The eager bind: the same ``bind_profile`` the sim main's stamp binds with, the
        # runner every tick reuses while the profile is unchanged. Its ceiling is the run
        # start's: no runner built later may widen it (review N5).
        self._run_ceiling = self._dispatch_runner(prof).tree.ceiling

    @property
    def managed_statemachine(self):
        return None

    @property
    def interaction_ground_truth_list(self):
        return []

    @property
    def runtime_behavior_schema(self):
        return {
            "steps_per_action": {"type": "integer", "required": False},
            "response_rate": {"type": "number", "required": False},
            "step_only_on_events": {"type": "boolean", "required": False},
            "profile": {"type": "dict", "required": False, "allow_unknown": True},
        }

    def _create_user(self):
        return UserRegistry(self.sim_clock, self.credentials, role="admin")

    def _create_manager(self):
        return AssignmentManager(
            run_id=self.run_id,
            sim_clock=self.sim_clock,
            user=self.user,
            profile=self.behavior.get("profile") or {},
            persona=self.behavior.get("persona") or {},
        )

    def launch(self, sim_clock):
        super().launch(sim_clock)

    def handle_app_topic_messages(self, payload):
        pass

    def _assignment_profile(self) -> Dict[str, Any]:
        return self.behavior.get("profile") or {}

    # --- the dispatch slot (round 2: docs/plans/policy_slots_round2_whole_tick.md §2) ---
    #
    # Import-cycle rule (round-2 §4): ``policies.dispatch`` modules are imported ONLY
    # locally: in the two accessors below (``_build_dispatch_policy``,
    # ``_dispatch_runner``), in ``_tick_seed`` (the one seed formula,
    # ``dispatch.tick.tick_seed``), in ``_dispatch_tick`` (the clock value, the run
    # stats) and in ``close`` / ``_patch_run_config_meta`` (``dispatch.run_stats``).
    # ``policies.dispatch`` reaches back into this package (constraints, spatial,
    # pooled_planner), so a module-top import here would cycle through
    # ``assignment/__init__`` (tests/test_policy_import_cycles.py pins it). The leaf
    # ``policies.framework`` and ``assignment.accounting`` are safe at module top.

    @staticmethod
    def _check_selection_at_start(agent_id: str, prof: Dict[str, Any]) -> None:
        """The sim main's run-start check on this agent's profile (raises ``PolicyError``).

        ``dispatch.profile.assert_assignment_profiles_resolvable``: the
        ``policies_format`` marker (``compat.legacy.check_policies_format``, the sim
        main's check and message), resolution, the legacy mirrors (message 13) and the
        binding (messages 14, 23, 24), the agent id prefixed as at run start. A worker
        reached by a stale bundle (smoke/stress paths, an old scheduler) is refused
        here exactly as the sim main would have refused it.
        """
        from apps.container_logistics.policies.dispatch.profile import (  # local: cycle rule
            assert_assignment_profiles_resolvable,
        )

        assert_assignment_profiles_resolvable({agent_id: {"profile": prof}})

    @staticmethod
    def _build_dispatch_policy(prof: Dict[str, Any]):
        """Resolve the profile's dispatch selection and instantiate its tree (raises)."""
        from apps.container_logistics.policies.dispatch.profile import (  # local: cycle rule
            build_policy_tree,
            resolve_dispatch_for_profile,
        )

        return build_policy_tree(resolve_dispatch_for_profile(prof))

    def _dispatch_runner(self, prof: Dict[str, Any]):
        """The ``DispatchRunner`` for ``self._solver`` under ``prof``, cached on its inputs.

        Rebuilt (and re-bound) when ``_solver`` is swapped, a profile key the selection
        reads (``policies``, ``strategy``, ``solver_params``, ``use_spatial_matching``,
        ``planner``) is patched after construction (the tests that build the app via
        ``__new__`` rely on it), or a WORLD key the bound tree's ``bind`` read
        (``BoundTree.world_reads``, e.g. ``cooperation``) changed. ``_solver`` is fitted
        into the selection (``bind_instance``: reused as the top, or wrapped as the
        innermost leaf). When a reused top's composites were stale (a patched profile)
        and ``bind_instance`` rebuilt them around the same leaf, the rebuilt top becomes
        ``_solver``, so later injections (``_inject_rebate_book``) reach the tree that
        runs (review C3 #4). Raises ``PolicyError`` on a mismatch or a refused
        declaration.
        """
        from apps.container_logistics.policies.dispatch.runner import build_runner_for_app  # local: cycle rule

        def _key(runner):
            return (
                id(self._solver),
                json.dumps(prof.get("policies"), sort_keys=True, default=str),
                prof.get("strategy"),
                json.dumps(prof.get("solver_params"), sort_keys=True, default=str),
                prof.get("use_spatial_matching", True),
                json.dumps(prof.get("planner"), sort_keys=True, default=str),
                runner.tree.digest(prof),
            )

        cached_key, cached = getattr(self, "_dispatch_runner_cache", (None, None))
        if cached is not None and cached_key == _key(cached):
            return cached
        # A solver's notes may not set a field publish() writes from its own share tag
        # (``wire_fields``' keyword set) unless the top declared it (review L3).
        # The run-start cross-company ceiling (``__init__``'s bind) bounds every rebuilt
        # runner: a profile that widens it mid-run is refused (review N5).
        runner = build_runner_for_app(self._solver, prof, wire_fields=tuple(wire_fields({})),
                                      run_ceiling=getattr(self, "_run_ceiling", None))
        if runner.policy is not self._solver and runner.tree.leaf.instance is not self._solver:
            self._solver = runner.policy  # the stale top was rebuilt (bind_instance)
        self._dispatch_runner_cache = (_key(runner), runner)
        # The top's planning declaration, cached HERE (critique minor 2): the agent asks
        # for it on every step, and a step must not rebuild (or fail to rebuild) a runner.
        self._planning_decl = runner.tree.declaration.planning
        return runner

    def _order_publish_payload(self, order: Dict[str, Any]) -> Dict[str, Any]:
        """Shape expected by ``TruckApp.handle_assignment`` / smoke tests."""
        prof = order.get("profile") or {}
        pickup_facility_resource_id = self.manager.resolve_facility_resource_id(prof.get("pickup_facility_name"))
        dropoff_facility_resource_id = self.manager.resolve_facility_resource_id(prof.get("dropoff_facility_name"))
        return {
            "_id": order.get("_id"),
            "pickup_loc": order.get("pickup_loc") or prof.get("pickup_loc"),
            "dropoff_loc": order.get("dropoff_loc") or prof.get("dropoff_loc"),
            "pickup_service_time": order.get("pickup_service_time") or prof.get("pickup_service_time", 120),
            "dropoff_service_time": order.get("dropoff_service_time") or prof.get("dropoff_service_time", 120),
            "profile": prof,
            "pickup_facility_resource_id": pickup_facility_resource_id,
            "dropoff_facility_resource_id": dropoff_facility_resource_id,
        }

    # --- the solver seam (plan §3.2, §7 R-I1b) --------------------------------

    @staticmethod
    def _rebate_aware(prof: Dict[str, Any]) -> bool:
        """Whether the compiled profile opts a solver into rebate pricing.

        Read from the profile on every call (like the dispatch runner's cache key),
        so a behavior patched after construction takes effect. Defaults ``false`` — and no shipped scenario sets it — so the book
        below is constructed, and ``set_rebate_book`` is called, on **no** shipped
        path today (plan §3.2).
        """
        planner = (prof or {}).get("planner") or {}
        return bool(planner.get("rebate_aware", False))

    def _inject_rebate_book(self, prof: Dict[str, Any]) -> None:
        """Build and inject a :class:`RebateBook`, but ONLY when opted in.

        With ``planner.rebate_aware`` false (the default, and every shipped
        scenario's value), this is a no-op: no facility documents are fetched, no
        book is constructed, and ``set_rebate_book`` is never called — so this
        injection can never be the thing that perturbs an allocation (R-I1b). The
        existing truck/order candidate projections (``assign()`` above) are
        untouched by this method; it reads facility documents over a SEPARATE,
        narrowly-projected request, only when opted in.
        """
        if not self._rebate_aware(prof):
            return
        book = getattr(self, "_rebate_book_cache", None)
        if book is None:
            # Cached for the life of the app, and that is a correctness-neutral
            # choice: `profile.rebate` is COMPILED data stamped at generation and
            # never mutated during a run (unlike `avg_queue_wait_seconds`, which the
            # facility agent patches onto the same documents). Without the cache this
            # pages the whole facility collection over HTTP on EVERY assignment tick
            # — 300 documents × ~2500 ticks — which is precisely the per-arrival
            # HTTP cost the plan cited when rejecting a truck-side lookup (§3.1
            # option (b)). An opt-in seam must not smuggle that back in.
            try:
                docs = self.manager._paged_where(
                    self.manager._facility_url(),
                    {"run_id": self.manager.run_id},
                    projection={"_id": 1, "profile.rebate": 1},
                )
                book = RebateBook.from_facility_docs(docs)
            except Exception:  # pragma: no cover - never block a tick on this opt-in read
                # R2-9 / review F10: fall through to CLEARING the book, never `return`.
                # Returning left whatever book was injected on a previous tick attached
                # to the solver, so a mid-run read failure would silently keep pricing
                # from stale data — a solver would go on believing a schedule it can no
                # longer read. An explicit None is a legible "I do not know", and the
                # seam's own contract already defines None as "never injected".
                logging.exception(
                    "AssignmentApp: failed to build the opt-in RebateBook — clearing "
                    "any previously injected book rather than pricing from stale data"
                )
                book = None
            # Only a SUCCESSFUL read is cached (mirroring `_haulier_roster_cache`):
            # facilities may not be created yet on the first tick, and freezing an
            # empty book would silently disable pricing for the whole run. An empty
            # book from a successful read of a rebate-less scenario is still cached —
            # re-paging 300 documents every tick to rediscover "nothing" is the bug
            # this cache exists to prevent.
            if book is not None and docs:
                self._rebate_book_cache = book
        try:
            self._solver.set_rebate_book(book)
        except AttributeError:  # pragma: no cover - a third-party solver without it
            pass

    @staticmethod
    def _tick_seed(run_id: str, time_step: int) -> int:
        """Stable 64-bit per-tick seed (``dispatch.tick.tick_seed``). Identical inputs => identical awards (I-P5).

        Scope honesty: this makes the PLANNER deterministic for a given tick input.
        It does NOT make a whole run reproducible — agent scheduling is async.
        """
        from apps.container_logistics.policies.dispatch.tick import tick_seed  # local: cycle rule

        return tick_seed(run_id, time_step)

    def _patch_run_config_meta(self, key: str, value: Any) -> bool:
        """Write one run-stats snapshot onto ``run_config.meta.<key>`` (best-effort; ``dispatch.run_stats``)."""
        from apps.container_logistics.policies.dispatch import run_stats  # local: cycle rule

        return run_stats.patch_run_config_meta(self, key, value)

    def close(self, sim_clock):
        """FINAL FLUSH of the declared run stats before the agent leaves (R3-5).

        Without this the stamp was whatever the last scheduled push happened to
        catch — the review measured ``ticks: 15`` against a true 17. A provenance
        stamp that stops early is worse than none, because it reads exact.
        """
        try:
            from apps.container_logistics.policies.dispatch import run_stats  # local: cycle rule

            run_stats.flush(getattr(self, "_run_stats", None) or {}, self._patch_run_config_meta)
        except Exception:  # pragma: no cover - never block shutdown
            logging.debug("final run-stats flush failed", exc_info=True)
        return super().close(sim_clock)

    def assign(self, sim_clock: str, time_step: int = 0) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
        # Reset the collaboration tags up front so a mid-assign exception can never
        # leave a previous tick's tags to be paired with a later tick's matches.
        self._share_tags = {}
        prof = self._assignment_profile()
        # Opt-in solver seam (plan §3.2): a no-op unless planner.rebate_aware is
        # true, which no shipped scenario sets. See `_inject_rebate_book`.
        self._inject_rebate_book(prof)
        respect_online = bool(prof.get("respect_truck_online_state", True))
        reject_busy = bool(prof.get("reject_if_active_haul_trip", True))
        solve_in_thread = bool(prof.get("solve_in_thread", False))

        projection_truck = {"state": 1, "profile": 1, "persona": 1, "_id": 1, "meta": 1}
        projection_order = {
            "state": 1,
            "profile": 1,
            "pickup_loc": 1,
            "dropoff_loc": 1,
            "pickup_service_time": 1,
            "dropoff_service_time": 1,
            "order_size": 1,
            "_id": 1,
        }

        trucks = self.manager.list_trucks(projection=projection_truck, online_only=respect_online)
        orders = self.manager.list_unassigned_orders(projection=projection_order)
        # Eve ``order.state`` can lag behind haul creation (MQTT updates the order). Exclude any
        # order already referenced by an open haul so we do not assign the same order twice.
        claimed_order_ids = self.manager.order_ids_with_open_haul()
        orders = [o for o in orders if str(o.get("_id")) not in claimed_order_ids]
        busy_ids = {str(x) for x in self.manager.active_haul_truck_ids()} if reject_busy else set()

        candidates = assign_constraints.filter_assignable_trucks(
            trucks,
            busy_ids,
            respect_truck_online_state=respect_online,
            reject_if_active_haul_trip=reject_busy,
            online_state_name=self.manager.online_state_name(),
        )
        if prof.get("working_hours"):
            # F5: only a profile that compiled working hours filters, and only DISPATCH
            # ticks (a planning tick sees every online truck; plan D6).
            candidates = self._on_shift_trucks(prof["working_hours"], candidates, time_step)

        lookup = None
        if getattr(self, "_prefetch", None):
            # F3: the top asked for these orders on its previous tick. NO request is made
            # on any tick without a pending prefetch (every non-planning run).
            lookup, extra = self._prefetch_lookup(projection_order, claimed_order_ids, orders)
            orders = orders + extra

        assignment = self._dispatch_tick(prof, candidates, orders, sim_clock, time_step, solve_in_thread,
                                         lookup=lookup)

        # Haulier scoping is enforced (no fallback). If there were assignable trucks AND
        # unassigned orders but nothing matched, the data is almost certainly missing or
        # mismatched haulier_id — surface it loudly instead of silently running dead.
        # Not on a tick whose result (or a sub-tick's) the framework discarded or whose
        # solver noted an incident: that was already logged (message 18/19/20), the haulier text
        # would misdiagnose it, and the one-shot warning must stay armed (review C3 #6).
        if candidates and orders and not assignment and not self._warned_haulier_block \
                and not getattr(self, "_tick_contract_events", 0):
            missing_truck = sum(1 for t in candidates if assign_constraints.haulier_of(t) is None)
            missing_order = sum(1 for o in orders if assign_constraints.haulier_of(o) is None)
            logging.error(
                "Assignment produced 0 matches with %d assignable trucks and %d unassigned "
                "orders — haulier scoping blocked everything (trucks missing haulier_id: %d, "
                "orders missing haulier_id: %d). Regenerate behaviors so every truck/order "
                "carries a haulier_id.",
                len(candidates), len(orders), missing_truck, missing_order,
            )
            self._warned_haulier_block = True
        return assignment

    def _dispatch_tick(
        self,
        prof: Dict[str, Any],
        candidates: List[Dict[str, Any]],
        orders: List[Dict[str, Any]],
        sim_clock: str,
        time_step: int,
        solve_in_thread: bool,
        *,
        lookup: Dict[str, Dict[str, Any]] | None = None,
    ) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
        """Decide the whole tick with the selected policy; account the shares; record provenance.

        The FRAMEWORK half of a tick (plan ``plan_contract_final.md`` §2.7): the tick,
        the runner (solve -> check against ownership-or-declared and physical
        feasibility -> commit the pairs), then the solver-independent share accounting
        (priced with the tick's measurement, dials ``self._solver_params``) and, when
        the top declared run stats, folds the tick's notes into them
        (``dispatch.run_stats``). Returns the committed pairs.
        """
        from apps.container_logistics.policies.dispatch.tick import DispatchClock  # local: cycle rule

        runner = self._dispatch_runner(prof)
        use_tpool = solve_in_thread and _eventlet_tpool is not None
        tick = runner.tick(
            candidates=candidates,
            orders=orders,
            profile=prof,
            clock=DispatchClock(
                sim_clock=sim_clock,
                time_step=int(time_step or 0),
                horizon_steps=getattr(self, "_sim_horizon_steps", None),
            ),
            seed=self._tick_seed(str(getattr(self, "run_id", "")), int(time_step or 0)),
            execute=_eventlet_tpool.execute if use_tpool else _direct_call,
            dials=self._solver_params,
            **self._planning_tick_kwargs(runner, lookup),
        )
        events_before = runner.stats.get("violations", 0) + runner.stats.get("crashes", 0)
        self._tick_contract_events = 0
        outcome = runner.run(tick)
        self._tick_contract_events = (
            runner.stats.get("violations", 0) + runner.stats.get("crashes", 0) - events_before)
        # Review V-m1: a kept prefetch is the top's proof that its result was COMMITTED (its
        # next tick reads a non-empty lookup as "my pairs went out"). So it is staged only
        # once the accounting and the run stats below succeeded, and kept only when
        # ``publish`` then sent every pair (``_staged_prefetch``); any exception on the way
        # leaves the next tick without one (the top falls back to per-pair evidence).
        # ``_prefetch`` was already consumed by this tick's read. Empty on every default run.
        self._prefetch = ()
        self._staged_prefetch = ()
        mtt = prof.get("max_travel_time_pickup")

        def own_allowed(t, o):
            # The owner reserve's rule: the strict own-haulier feasibility.
            return assign_constraints.pair_allowed(t, o, max_travel_time_pickup=mtt)

        self._share_tags = accounting.render_share_tags(
            accounting.account_shares(outcome.pairs, candidates=candidates,
                                      cost=tick.measure.cost, own_allowed=own_allowed),
            fmt=runner.tag_format,
            solver_notes=outcome.solver_notes,
        )
        stats_cls = runner.tree.top.declaration.run_stats
        if stats_cls is not None and outcome.result is not None:
            # Solver provenance: only a top that declared run stats has them, and a
            # discarded tick (message 18) is never folded.
            from apps.container_logistics.policies.dispatch import run_stats  # local: cycle rule

            store = getattr(self, "_run_stats", None)
            if store is None:
                store = self._run_stats = {}
            run_stats.record_tick(store, stats_cls, outcome.tick_notes, tick.clock, self._patch_run_config_meta)
        self._staged_prefetch = self._result_prefetch(outcome)
        if self._share_tags:
            logging.info("Planner %s: %d cross-company assignment(s) this tick.",
                         runner.resolved.label, len(self._share_tags))
        return outcome.pairs

    # --- planning (NetworkFlowDay F1), prefetch (F3), working hours (F5) ---------------
    #
    # Every method below is reached ONLY when the bound top declared ``planning``, a
    # previous tick returned a non-empty ``Result.prefetch``, or the profile compiled
    # ``working_hours``. No shipped scenario does any of the three, so the default tick
    # makes exactly the requests it always made (pinned by tests/test_assignment_planning_app.py).

    #: Fields of an order document a lookup / prefetched order carries beyond the tick's own projection.
    _LOOKUP_EXTRA_FIELDS = ("truck", "meta")
    #: The key a planning tick's ``engaged`` order document carries its open haul under.
    ENGAGED_HAUL_KEY = "open_haul"
    #: The key a ``lookup`` document carries the truck of its open haul under (``None``: none).
    CLAIMED_BY_KEY = "claimed_by"

    def planning_spec(self):
        """The bound top's ``Declaration.planning`` (cached when the runner was built), or ``None``."""
        return getattr(self, "_planning_decl", None)

    def _step_seconds_value(self) -> int:
        """Sim seconds per step: set by the agent (``STEP_INTERVAL``); 240 if never handed over."""
        value = getattr(self, "_step_seconds", None)
        if not value:
            helper = getattr(self, "agent_helper", None)
            value = ((getattr(helper, "orsim_settings", None) or {}).get("STEP_INTERVAL")
                     if helper is not None else None)
        if not value:
            if not getattr(self, "_warned_step_seconds", False):
                logging.warning("AssignmentApp: STEP_INTERVAL was never handed over; assuming 240 s per step")
                self._warned_step_seconds = True
            value = 240
        return int(value)

    def _planning_schedule(self) -> "assign_planning.PlanningSchedule":
        sched = getattr(self, "_plan_schedule", None)
        if sched is None:
            sched = self._plan_schedule = assign_planning.PlanningSchedule()
        return sched

    def planning_due(self, time_step: Any, *, step_seconds: Any = None, horizon_steps: Any = None):
        """The ``planning.Due`` of ``time_step``, or ``None``: never when the top declared no planning."""
        spec = self.planning_spec()
        if not spec:
            return None
        if horizon_steps is None:
            horizon_steps = getattr(self, "_sim_horizon_steps", None)
        return self._planning_schedule().due(
            spec, time_step, step_seconds=step_seconds or self._step_seconds_value(),
            horizon_steps=horizon_steps)

    def _on_shift_trucks(self, record: Dict[str, Any], trucks: List[Dict[str, Any]], time_step: Any):
        """F5: the trucks whose haulier is inside its working hours at ``time_step`` (elapsed axis)."""
        second = assign_planning.seconds_of_day(int(time_step or 0), self._step_seconds_value())
        return [t for t in trucks
                if assign_planning.on_shift(record, assign_constraints.haulier_of(t), second)]

    def _lookup_projection(self, projection_order: Dict[str, Any]) -> Dict[str, Any]:
        proj = dict(projection_order)
        for key in self._LOOKUP_EXTRA_FIELDS:
            proj[key] = 1
        return proj

    def _claimed_by(self, rows: List[Dict[str, Any]]) -> Dict[str, str]:
        """``{order_id: truck_id}`` of the open hauls in ``rows``."""
        out: Dict[str, str] = {}
        for row in rows:
            oid, tid = row.get("order"), row.get("truck")
            if oid and tid:
                out[str(oid)] = str(tid)
        return out

    def _prefetch_lookup(
        self,
        projection_order: Dict[str, Any],
        claimed_order_ids,
        orders: List[Dict[str, Any]],
        *,
        haul_rows: List[Dict[str, Any]] | None = None,
    ) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
        """The pending prefetch read: ``(lookup, extra orders)`` (and the prefetch is consumed).

        ``lookup`` is ``{order_id: doc}`` for every requested order found (any state), each
        doc carrying ``claimed_by`` (the truck of its open haul, else ``None``: critique M4,
        a doc can still read ``unassigned`` while its haul exists). ``extra`` are the
        looked-up orders the tick may also match: ``unassigned``, not claimed, not already in
        ``orders``, in prefetch order AFTER the oldest-N. Every pair is still judged by
        ``permitted`` and the result check. A failed read logs and yields nothing (the tick
        then runs on the oldest-N alone, exactly like a tick without a prefetch).
        """
        ids = list(getattr(self, "_prefetch", ()) or ())
        self._prefetch = ()
        if not ids:
            return {}, []
        try:
            docs = self.manager.orders_by_ids(ids, projection=self._lookup_projection(projection_order))
            if haul_rows is None:
                haul_rows = self.manager.open_haul_rows()
        except Exception:
            logging.exception("AssignmentApp: the prefetch read of %d order(s) failed; "
                              "this tick runs without its lookup", len(ids))
            return {}, []
        claimed_by = self._claimed_by(haul_rows)
        by_id: Dict[str, Dict[str, Any]] = {}
        for doc in docs:
            oid = str(doc.get("_id"))
            if oid in by_id:
                continue
            item = dict(doc)
            item[self.CLAIMED_BY_KEY] = claimed_by.get(oid)
            by_id[oid] = item
        lookup = {oid: by_id[oid] for oid in dict.fromkeys(str(i) for i in ids) if oid in by_id}
        present = {str(o.get("_id")) for o in orders}
        claimed = {str(x) for x in (claimed_order_ids or ())}
        extra: List[Dict[str, Any]] = []
        for oid, doc in lookup.items():
            if (doc.get("state") == OrderStateMachine.unassigned.name and oid not in present
                    and oid not in claimed and doc.get(self.CLAIMED_BY_KEY) is None):
                extra.append(doc)
                present.add(oid)
        return lookup, extra

    def _planning_tick_kwargs(self, runner, lookup) -> Dict[str, Any]:
        """The planning members of an ordinary DISPATCH tick: none unless a lookup / a planning top."""
        kwargs: Dict[str, Any] = {}
        if lookup:
            kwargs["lookup"] = lookup
        if runner.tree.declaration.planning is not None:
            kwargs["run_id"] = str(getattr(self, "run_id", "") or "")
        return kwargs

    @staticmethod
    def _result_prefetch(outcome) -> tuple:
        """The top's ``Result.prefetch`` of ``outcome`` (empty when the result was discarded)."""
        result = getattr(outcome, "result", None)
        return tuple(getattr(result, "prefetch", ()) or ()) if result is not None else ()

    def _store_prefetch(self, outcome) -> None:
        """Keep the top's ``Result.prefetch`` for the next tick (empty when the result was discarded)."""
        self._prefetch = self._result_prefetch(outcome)

    def plan(self, sim_clock: str, time_step: int, due=None, *, hold_started_s: float | None = None):
        """Run one PLANNING tick (``kind="plan"``) for the bound top; commits and publishes nothing.

        - trucks: every online truck (no busy and no working-hours filter: plan D6);
        - ``engaged``: ``{truck_id: order doc}`` of the open hauls (one paged
          ``/truck/trip`` read with state + leg start stamps, then the orders by id); each
          doc carries the haul under ``open_haul`` (``_id``, ``state``, ``stats``);
        - orders: every ``created`` / ``unassigned`` order with
          ``meta.request_time_step < window.end_step`` (stable ``_id`` order), minus the
          orders of open hauls;
        - ``lookup``: the pending prefetch (as on a dispatch tick).

        The window: ``foresight == "day"`` runs to the end of the run day, ``"none"`` to
        ``time_step + 1``; ``start_step`` is the boundary being planned (a late plan has
        ``now_step > start_step``). Returns the ``TickOutcome``; the top's ``prefetch`` is
        kept for the next tick and its notes fold into the declared run stats.
        ``hold_started_s``: the agent's ``time.monotonic()`` when it published the hold
        (``PlanningWindow.hold_started_s``, review V-m2), else ``None``.
        """
        from apps.container_logistics.policies.dispatch.tick import DispatchClock, PlanningWindow  # local: cycle rule

        prof = self._assignment_profile()
        runner = self._dispatch_runner(prof)
        spec = runner.tree.declaration.planning
        step = int(time_step or 0)
        step_seconds = self._step_seconds_value()
        if due is None:
            due = self.planning_due(step, step_seconds=step_seconds)
        if due is None:  # a caller asked anyway: plan the step as an on-time interval
            spd = assign_planning.steps_per_day(step_seconds)
            due = assign_planning.Due("interval", step, step // spd, step, 0)
        spd = assign_planning.steps_per_day(step_seconds)
        end = (due.day + 1) * spd if spec.get("foresight") == "day" else step + 1
        window = PlanningWindow(reason=due.reason, day=due.day, start_step=due.boundary, end_step=end,
                                now_step=step, step_seconds=step_seconds, hold_started_s=hold_started_s)
        # Marked BEFORE the reads: a failing plan is not retried on every following step
        # (each retry would hold the barrier again); the next boundary plans again.
        self._planning_schedule().mark(due, spec, step_seconds=step_seconds)
        if due.late_steps > 0:
            logging.warning("AssignmentApp: LATE PLAN %s for day %d boundary step %d planned at step %d "
                            "(%d step(s) late)", due.reason, due.day, due.boundary, step, due.late_steps)

        respect_online = bool(prof.get("respect_truck_online_state", True))
        solve_in_thread = bool(prof.get("solve_in_thread", False))
        projection_truck = {"state": 1, "profile": 1, "persona": 1, "_id": 1, "meta": 1}
        projection_order = {
            "state": 1, "profile": 1, "pickup_loc": 1, "dropoff_loc": 1, "pickup_service_time": 1,
            "dropoff_service_time": 1, "order_size": 1, "_id": 1, "meta": 1,
        }
        trucks = self.manager.list_trucks(projection=projection_truck, online_only=respect_online)
        haul_rows = self.manager.open_haul_rows()
        claimed_by = self._claimed_by(haul_rows)
        engaged_ids = [str(r["order"]) for r in haul_rows if r.get("order") and r.get("truck")]
        engaged_docs = {
            str(d.get("_id")): d
            for d in (self.manager.orders_by_ids(engaged_ids, projection=self._lookup_projection(projection_order))
                      if engaged_ids else [])
        }
        engaged: Dict[str, Dict[str, Any]] = {}
        for row in haul_rows:
            oid, tid = row.get("order"), row.get("truck")
            doc = engaged_docs.get(str(oid)) if oid else None
            if doc is None or not tid:
                continue
            item = dict(doc)
            item[self.ENGAGED_HAUL_KEY] = {"_id": row.get("_id"), "state": row.get("state"),
                                           "stats": dict(row.get("stats") or {})}
            engaged[str(tid)] = item
        orders = [o for o in self.manager.list_open_orders_until(window.end_step, projection=projection_order)
                  if str(o.get("_id")) not in claimed_by]
        lookup = None
        if getattr(self, "_prefetch", None):
            lookup, _extra = self._prefetch_lookup(projection_order, set(claimed_by), orders,
                                                   haul_rows=haul_rows)
        use_tpool = solve_in_thread and _eventlet_tpool is not None
        tick = runner.tick(
            candidates=trucks,
            orders=orders,
            profile=prof,
            clock=DispatchClock(sim_clock=sim_clock, time_step=step,
                                horizon_steps=getattr(self, "_sim_horizon_steps", None)),
            seed=self._tick_seed(str(getattr(self, "run_id", "")), step),
            execute=_eventlet_tpool.execute if use_tpool else _direct_call,
            dials=self._solver_params,
            kind="plan",
            planning=window,
            engaged=engaged,
            lookup=lookup or None,
            run_id=str(getattr(self, "run_id", "") or ""),
        )
        outcome = runner.run(tick)
        self._prefetch = ()  # V-m1: kept only once the run stats below succeeded
        stats_cls = runner.tree.top.declaration.run_stats
        if stats_cls is not None and outcome.result is not None:
            from apps.container_logistics.policies.dispatch import run_stats  # local: cycle rule

            store = getattr(self, "_run_stats", None)
            if store is None:
                store = self._run_stats = {}
            run_stats.record_tick(store, stats_cls, outcome.tick_notes, tick.clock, self._patch_run_config_meta)
        self._store_prefetch(outcome)
        logging.info("AssignmentApp: planning tick %s day %d step %d: %d truck(s), %d engaged, %d order(s), "
                     "%d prefetch id(s)", due.reason, due.day, step, len(trucks), len(engaged), len(orders),
                     len(getattr(self, "_prefetch", ()) or ()))
        return outcome

    def publish(self, assignment: List[Tuple[Dict[str, Any], Dict[str, Any]]]) -> None:
        share_tags = getattr(self, "_share_tags", {}) or {}
        # V-m1: the dispatch tick's staged prefetch becomes the next tick's only once every
        # pair below went out (taken first, so a raise anywhere here drops it).
        staged = getattr(self, "_staged_prefetch", ())
        self._staged_prefetch = ()
        sent_all = True
        for truck, order in assignment:
            tid = truck.get("_id")
            if not tid:
                continue
            tag = share_tags.get((str(tid), str(order.get("_id")))) or {}
            # The share fields (and a solver's ``solver_notes``) come from the tag via
            # the compat wire table; an unshared pair gets the dataclass defaults.
            payload = AssignedHaulTripPayload(
                action=ContainerLogisticsActions.ASSIGNED_HAUL_TRIP,
                order=self._order_publish_payload(order),
                truck_id=tid,
                **wire_fields(tag),
            )
            topic = f"{self.run_id}/{tid}"
            try:
                self.messenger.client.publish(topic, json.dumps(payload.to_wire_dict(), default=str))
            except Exception:
                sent_all = False
                logging.exception("Failed to publish assignment to %s", topic)
        if staged and sent_all:
            self._prefetch = staged
