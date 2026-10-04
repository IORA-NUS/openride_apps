"""
Wiring that hands an ORSim scheduler/agent the transport the flag selected.

WHAT THIS IS, AND WHAT IT IS NOT
--------------------------------
The *decision* -- which transport, and what "unset" means -- belongs to
:func:`apps.transport.build_agent_transport`. This module does not re-implement
it and deliberately does not read ``os.environ``: the flag rides
``messenger_settings['AGENT_TRANSPORT']``, stamped by ``apps/config.py``, because
celery workers here are long-lived across runs and a worker that re-read the
environment could serve run A on paho and run B on kafka. See
``apps/transport/factory.py`` for that argument in full.

What lives here is the *application* of that decision to the two app-side
subclasses, plus the one concern the factory has no view of:

**Degrading against orsim v1.3.0.** The installed engine -- the one the running
celery workers import today -- is **v1.3.0**, which has no
``orsim.messenger.transport`` seam and whose ``ORSimScheduler.__init__`` /
``ORSimAgent.__init__`` have no ``transport`` parameter at all. So:

* on the default path the ``transport`` keyword is **not passed**, not passed as
  ``None`` -- the call is byte-identical to the pre-phase-2 one, which is the
  whole reason this can ship before orsim v1.4.0 is released; and
* when a transport IS selected, :func:`require_seam` fails with the actual
  remedy rather than letting ``TypeError: __init__() got an unexpected keyword
  argument 'transport'`` surface from inside a celery worker, where it reads
  like an app bug.

Everything here is a no-op while the flag is unset.
"""

from __future__ import annotations

import importlib
import inspect
from typing import Optional, Tuple

#: Kept as a doc pointer only -- the value is READ from ``messenger_settings``,
#: never from the environment. ``apps/config.py`` is the one place it is read.
TRANSPORT_ENV_VAR = "OPENRIDE_AGENT_TRANSPORT"

#: The key the flag travels under, across the celery task boundary.
TRANSPORT_SETTINGS_KEY = "AGENT_TRANSPORT"


class TransportSeamMissing(RuntimeError):
    """
    A transport was selected but the importable orsim has no seam to accept it.

    Distinct from anything ``apps.transport`` raises: this is about the ENGINE
    version, not about the flag or the Kafka implementation.
    """


# ---------------------------------------------------------------------------
# Seam availability (orsim v1.3.0 has none)
# ---------------------------------------------------------------------------

def seam_available() -> bool:
    """True when the importable orsim exposes the phase-1 transport seam."""
    try:
        importlib.import_module("orsim.messenger.transport")
    except Exception:
        return False
    return True


def orsim_accepts_transport(target) -> bool:
    """
    True when ``target`` (class or callable) declares a ``transport`` parameter.

    ``**kwargs`` counts: orsim's own constructors forward unknown keywords on.
    """
    func = target.__init__ if inspect.isclass(target) else target
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins/C callables
        return False
    if "transport" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def require_seam(context: str, target=None) -> None:
    """Assert the importable orsim can accept a transport, or explain why not."""
    if not seam_available():
        raise TransportSeamMissing(
            f"{context}: a non-paho transport was selected "
            f"({TRANSPORT_SETTINGS_KEY} in messenger_settings) but the importable "
            "orsim has no orsim.messenger.transport seam. orsim v1.4.0 is not "
            "released; shadow the working tree with PYTHONPATH=/home/user/orsim, "
            f"or leave {TRANSPORT_ENV_VAR} unset to run on RabbitMQ as before."
        )
    if target is not None and not orsim_accepts_transport(target):
        raise TransportSeamMissing(
            f"{context}: {getattr(target, '__name__', target)} does not accept a "
            "transport= argument, so the importable orsim predates the phase-1 "
            "seam. Shadow the working tree with PYTHONPATH=/home/user/orsim, or "
            f"leave {TRANSPORT_ENV_VAR} unset."
        )


# ---------------------------------------------------------------------------
# Delegation to the one decision point
# ---------------------------------------------------------------------------

def messenger_settings(explicit: Optional[dict] = None) -> Optional[dict]:
    """``explicit`` if given, else ``ORSimEnv.messenger_settings``.

    ``ORSimEnv.set_backend()`` has run by the time either subclass is
    constructed: the scheduler asserts it, and orsim's ``start_agent`` celery
    task calls it before instantiating the agent class.
    """
    if explicit is not None:
        return explicit
    try:
        from orsim.core import ORSimEnv
    except Exception:  # pragma: no cover - orsim is always importable
        return None
    return getattr(ORSimEnv, "messenger_settings", None)


def build_transport(run_id, identity, settings: Optional[dict] = None):
    """
    Ask :mod:`apps.transport` for a transport; ``None`` means "keep today's path".

    Imported lazily so that a caller on the paho path never pays for -- and
    never triggers -- the Kafka import chain. ``apps.transport`` is itself pure
    at module scope (its ``confluent_kafka`` import sits inside the kafka
    branch), so this is belt and braces rather than the only guard.
    """
    from apps.transport import build_agent_transport

    return build_agent_transport(messenger_settings(settings), run_id, identity)


# ---------------------------------------------------------------------------
# Agent-side application
# ---------------------------------------------------------------------------

#: Positional layout of ``ORSimAgent.__init__``, used only to recover an agent's
#: identity BEFORE ``super().__init__`` runs -- the transport has to exist by
#: then. orsim's celery task constructs agents as ``AgentClass(**kwargs)``, so
#: the positional path is really only exercised by tests; but getting it wrong
#: there would hand the transport a ``None`` run_id and silently mis-key its
#: reply-lane publishes.
ORSIM_AGENT_POSITIONAL: Tuple[str, ...] = (
    'unique_id', 'run_id', 'reference_time', 'init_time_step', 'scheduler', 'behavior',
)


def agent_identity(args, kwargs):
    """``(run_id, agent_id)`` from an ORSimAgent-shaped argument list."""
    bound = dict(zip(ORSIM_AGENT_POSITIONAL, args))
    bound.update(kwargs)
    return bound.get('run_id'), bound.get('unique_id')


def select_agent_transport(args, kwargs, owner='agent'):
    """
    Return ``kwargs`` with ``transport`` set per the flag -- or removed entirely.

    The pop-then-maybe-re-add shape is the whole point. On the default path
    nothing is added, so ``super().__init__`` receives exactly the arguments it
    received before phase 2 and the installed orsim v1.3.0 keeps working
    untouched. An explicit ``transport=None`` is dropped for the same reason.

    A transport INSTANCE cannot cross orsim's ``AgentClass(**kwargs)`` celery
    boundary, so it is built here, inside the worker process.
    """
    kwargs = dict(kwargs)
    transport = kwargs.pop('transport', None)
    built_here = False

    if transport is None:
        run_id, agent_id = agent_identity(args, kwargs)
        transport = build_transport(run_id, agent_id)
        built_here = transport is not None

    if transport is not None:
        from orsim.lifecycle import ORSimAgent

        # THE SEAM CHECK HAS TO CLEAN UP AFTER THE BUILD.  A KafkaTransport
        # RETAINS a process-level bus in its constructor, so raising between the
        # build and the agent's own teardown leaked one: ``release()`` never
        # ran, the refcount never returned to 0, the bus never reached its
        # ``BUS_LINGER_S`` self-reap, and its ``Consumer`` + native poll thread
        # + lane-partition assignments survived for the life of the long-lived
        # celery worker -- across every subsequent run. REPRODUCED with
        # ``seam_available()`` forced False (the installed-v1.3.0 state this
        # module is written for): five calls raised loudly, as intended, and
        # left a live bus with ``handles: 5`` and ``_reap_at: None``. Those
        # zombie buses also cross the hazard ``_reject_shrunken_lanes`` exists
        # to prevent, by holding assignments on lane topics a later aged sweep
        # deletes.
        try:
            require_seam(owner, ORSimAgent)
        except Exception:
            if built_here:
                try:
                    transport.disconnect()
                except Exception:  # pragma: no cover - teardown must not mask
                    import logging

                    logging.getLogger(__name__).warning(
                        'could not tear down the transport built for a run whose '
                        'orsim has no transport seam', exc_info=True)
            raise
        kwargs['transport'] = transport

    return kwargs


class TransportSelectingAgent:
    """
    Mixin applying :func:`select_agent_transport` ahead of ``ORSimAgent.__init__``.

    For the container_logistics agents that subclass ``ORSimAgent`` DIRECTLY
    rather than through ``OpenRideAgent`` -- currently ``AssignmentAgent``,
    ``OrderLifecycleAgent`` and ``AnalyticsAgentIndie``. They are ordinary
    citizens of the step barrier, so leaving them out would produce a
    MIXED-BROKER run: scheduler and trucks on Kafka, these three still on MQTT,
    never replying, never reaped (they are alive and well), and every single
    step burning STEP_TIMEOUT on exactly three unresponsive agents.

    With the flag unset this mixin is a no-op: it removes a ``transport`` key
    that is not there and forwards ``*args, **kwargs`` untouched.

    Declare it FIRST -- ``class X(TransportSelectingAgent, ORSimAgent)`` -- so it
    precedes ``ORSimAgent`` in the MRO.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **select_agent_transport(args, kwargs, type(self).__name__))


# ---------------------------------------------------------------------------
# Scheduler-side application
# ---------------------------------------------------------------------------

def scheduler_identity(run_id, scheduler_id) -> str:
    """
    The scheduler's own routing identity, mirroring orsim's credentials email
    (``f'{run_id}_{scheduler_id}_ORSimScheduler'``). The ``_ORSimScheduler``
    suffix is what keeps it out of the agent ``unique_id`` namespace.
    """
    return f"{scheduler_id}_ORSimScheduler"


def select_scheduler_transport(run_id, scheduler_id, settings: Optional[dict] = None):
    """A transport for the scheduler's own messenger, or ``None`` for paho."""
    return build_transport(run_id, scheduler_identity(run_id, scheduler_id), settings)
