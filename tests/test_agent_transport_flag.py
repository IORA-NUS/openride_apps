"""
Unit tests for the app-side WIRING of the transport flag (RabbitMQ -> Kafka, phase 2).

Scope split, because two pieces of phase-2 work meet here:

* ``apps/transport/factory.py`` owns the DECISION -- what the flag means, and
  that it travels in ``messenger_settings['AGENT_TRANSPORT']`` rather than being
  re-read from ``os.environ`` inside a long-lived celery worker.
* ``apps/agent_transport.py`` (tested here) owns the APPLICATION of that
  decision to ``OpenRideScheduler``, ``OpenRideAgent`` and the three
  container_logistics agents that subclass ``ORSimAgent`` directly.

Three properties are pinned, and the first matters far more than the rest:

1. **With the flag unset, nothing changed.** Both subclasses call
   ``super().__init__`` with exactly the arguments they passed before phase 2 --
   with NO ``transport`` keyword, not even ``transport=None``. That is not
   pedantry: the INSTALLED orsim is **v1.3.0**, whose constructors have no such
   parameter, and it is what the live celery workers import today.
   ``confluent_kafka`` must not be imported on that path either, checked in a
   genuinely fresh interpreter rather than by reading a ``sys.modules`` that
   pytest has already polluted.

2. **With a transport selected, it is forwarded** -- to the scheduler (phase-1
   review's specific finding: the subclass used to swallow the parameter orsim
   had just grown), to OpenRideAgent, and to the three stragglers.

3. **On v1.3.0 a selected transport fails LOUDLY**, naming the remedy, rather
   than surfacing ``TypeError: unexpected keyword argument 'transport'`` from
   inside a celery worker.

The flag is injected here by passing a settings dict, never by setting the
environment variable -- which is exactly how production carries it.
"""

import os
import subprocess
import sys
import textwrap

import pytest

from apps import agent_transport
from apps.agent_transport import (
    TRANSPORT_SETTINGS_KEY,
    TransportSeamMissing,
    agent_identity,
    build_transport,
    messenger_settings,
    orsim_accepts_transport,
    require_seam,
    scheduler_identity,
    seam_available,
    select_agent_transport,
    select_scheduler_transport,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The kafka-path tests need the phase-1 seam, which is NOT in the installed
#: orsim v1.3.0 -- and selection there refuses on purpose (see
#: test_v130_refuses_a_selected_transport_with_a_useful_error). Run with
#: PYTHONPATH=/home/user/orsim to exercise them.
requires_seam = pytest.mark.skipif(
    not seam_available(),
    reason='orsim transport seam absent (installed v1.3.0); shadow with PYTHONPATH=/home/user/orsim',
)

PAHO_SETTINGS = {TRANSPORT_SETTINGS_KEY: 'paho'}


class FakeTransport:
    """Stand-in for the KafkaTransport whose internals this wiring must NOT know."""

    def __init__(self, settings=None, run_id=None, identity=None):
        self.settings = settings
        self.run_id = run_id
        self.identity = identity


def _select(monkeypatch, transport=FakeTransport):
    """
    Make selection yield ``transport`` (an instance) regardless of the flag.

    Patches this module's delegation point, NOT the factory: the factory's own
    behaviour is its author's test surface, and stubbing it here would couple
    these tests to its signature.
    """
    made = []

    def fake_build(run_id, identity, settings=None):
        t = transport(settings=settings, run_id=run_id, identity=identity)
        made.append(t)
        return t

    monkeypatch.setattr(agent_transport, 'build_transport', fake_build)
    return made


def _select_none(monkeypatch):
    """Make selection yield None -- i.e. the paho default -- deterministically."""
    monkeypatch.setattr(agent_transport, 'build_transport',
                        lambda run_id, identity, settings=None: None)


# ---------------------------------------------------------------------------
# Delegation: the decision is not re-implemented here
# ---------------------------------------------------------------------------

def test_paho_settings_yield_no_transport():
    assert build_transport('run_1', 'truck_7', PAHO_SETTINGS) is None


def test_absent_and_empty_settings_yield_no_transport():
    """A worker whose backend was never stamped must not be pushed onto kafka."""
    assert build_transport('run_1', 'truck_7', {}) is None


def test_unknown_transport_name_is_an_error_not_a_silent_paho_fallback():
    """Delegated to the factory, asserted here because the wiring must not swallow it."""
    with pytest.raises(ValueError) as exc:
        build_transport('run_1', 'truck_7', {TRANSPORT_SETTINGS_KEY: 'kakfa'})
    assert 'kakfa' in str(exc.value)


def test_the_flag_is_read_from_settings_not_from_the_environment(monkeypatch):
    """
    The design point from apps/transport/factory.py, pinned from the wiring side:
    celery workers here are long-lived across runs, so a worker that re-read
    OPENRIDE_AGENT_TRANSPORT could serve run A on paho and run B on kafka.
    """
    monkeypatch.setenv('OPENRIDE_AGENT_TRANSPORT', 'kafka')
    assert build_transport('run_1', 'truck_7', PAHO_SETTINGS) is None
    assert build_transport('run_1', 'truck_7', {}) is None


def test_messenger_settings_falls_back_to_orsimenv(monkeypatch):
    from orsim.core import ORSimEnv

    monkeypatch.setattr(ORSimEnv, 'messenger_settings', {'MARKER': 1}, raising=False)
    assert messenger_settings() == {'MARKER': 1}
    assert messenger_settings({'OTHER': 2}) == {'OTHER': 2}


def test_scheduler_identity_stays_out_of_the_agent_id_namespace():
    ident = scheduler_identity('run_1', 'sched_1')
    assert ident == 'sched_1_ORSimScheduler'


def test_agent_identity_from_kwargs_and_from_positionals():
    kw = dict(unique_id='truck_7', run_id='run_1', reference_time='t',
              init_time_step=0, scheduler={'id': 'sched_1'}, behavior={})
    assert agent_identity((), kw) == ('run_1', 'truck_7')
    assert agent_identity(('truck_7', 'run_1', 't', 0, {'id': 'sched_1'}, {}), {}) == (
        'run_1', 'truck_7')


# ---------------------------------------------------------------------------
# Graceful degradation against the installed orsim v1.3.0 (no seam)
# ---------------------------------------------------------------------------

def test_orsim_accepts_transport_detects_a_seamless_signature():
    class Old:
        def __init__(self, run_id, scheduler_id):
            pass

    class New:
        def __init__(self, run_id, scheduler_id, transport=None):
            pass

    class Forwarding:
        def __init__(self, *args, **kwargs):
            pass

    assert orsim_accepts_transport(Old) is False
    assert orsim_accepts_transport(New) is True
    assert orsim_accepts_transport(Forwarding) is True


def test_require_seam_reports_the_missing_parameter_not_a_bare_typeerror():
    class Old:
        def __init__(self, run_id):
            pass

    with pytest.raises(TransportSeamMissing) as exc:
        require_seam('OpenRideScheduler', Old)
    assert 'PYTHONPATH=/home/user/orsim' in str(exc.value)


def test_require_seam_reports_a_missing_seam_module(monkeypatch):
    monkeypatch.setattr(agent_transport, 'seam_available', lambda: False)
    with pytest.raises(TransportSeamMissing) as exc:
        require_seam('OpenRideAgent')
    assert 'orsim.messenger.transport' in str(exc.value)


# ---------------------------------------------------------------------------
# OpenRideScheduler wiring
# ---------------------------------------------------------------------------

def _patch_scheduler_super(monkeypatch):
    """Replace ORSimScheduler.__init__ with a recorder, keeping the subclass body runnable."""
    from apps.simulation import openride_scheduler as mod

    seen = {}

    def fake_init(self, run_id, scheduler_id, orsim_settings, **kwargs):
        seen['args'] = (run_id, scheduler_id, orsim_settings)
        seen['kwargs'] = dict(kwargs)
        self.run_id = run_id
        self.scheduler_id = scheduler_id
        self.orsim_settings = orsim_settings

    monkeypatch.setattr(mod.ORSimScheduler, '__init__', fake_init, raising=True)
    return mod, seen


def test_scheduler_on_the_default_path_passes_no_transport_keyword(monkeypatch):
    _select_none(monkeypatch)
    mod, seen = _patch_scheduler_super(monkeypatch)
    sched = mod.OpenRideScheduler('run_1', 'sched_1', {'SIMULATION_LENGTH_IN_STEPS': 10})
    assert seen['args'] == ('run_1', 'sched_1', {'SIMULATION_LENGTH_IN_STEPS': 10})
    # The ABSENCE is the point: orsim v1.3.0 would raise TypeError on the keyword.
    assert seen['kwargs'] == {'init_failure_handler': 'soft'}
    assert sched._reply_topic == 'run_1/sched_1/ORSimScheduler'


@requires_seam
def test_scheduler_forwards_an_explicitly_supplied_transport(monkeypatch):
    """Phase-1 review's finding: the subclass used to swallow this parameter."""
    mod, seen = _patch_scheduler_super(monkeypatch)
    sentinel = FakeTransport()
    mod.OpenRideScheduler('run_1', 'sched_1', {}, transport=sentinel)
    assert seen['kwargs']['transport'] is sentinel


@requires_seam
def test_scheduler_builds_and_forwards_the_selected_transport(monkeypatch):
    made = _select(monkeypatch)
    mod, seen = _patch_scheduler_super(monkeypatch)
    mod.OpenRideScheduler('run_1', 'sched_1', {})
    t = seen['kwargs']['transport']
    assert t is made[0]
    assert (t.run_id, t.identity) == ('run_1', 'sched_1_ORSimScheduler')


def test_scheduler_signature_now_carries_transport():
    import inspect

    from apps.simulation.openride_scheduler import OpenRideScheduler
    assert 'transport' in inspect.signature(OpenRideScheduler.__init__).parameters


def test_select_scheduler_transport_uses_the_scheduler_identity(monkeypatch):
    made = _select(monkeypatch)
    t = select_scheduler_transport('run_1', 'sched_1')
    assert t is made[0]
    assert (t.run_id, t.identity) == ('run_1', 'sched_1_ORSimScheduler')


# ---------------------------------------------------------------------------
# OpenRideAgent wiring
# ---------------------------------------------------------------------------

def _concrete(base):
    """
    OpenRideAgent is abstract (7 ORSimAgent hooks). Stub them from
    ``__abstractmethods__`` so this helper cannot drift when orsim adds one.
    """
    return type('ConcreteOpenRideAgent', (base,),
                {name: (lambda self, *a, **k: None) for name in base.__abstractmethods__})


def _patch_agent_super(monkeypatch):
    from apps.container_logistics import openride_agent as mod

    seen = {}

    def fake_init(self, *args, **kwargs):
        seen['args'] = args
        seen['kwargs'] = dict(kwargs)
        bound = dict(zip(agent_transport.ORSIM_AGENT_POSITIONAL, args))
        bound.update(kwargs)
        self.unique_id = bound.get('unique_id')
        self.run_id = bound.get('run_id')
        self.scheduler_id = (bound.get('scheduler') or {}).get('id')
        self.message_handlers = {}

    monkeypatch.setattr(mod.ORSimAgent, '__init__', fake_init, raising=True)
    return mod, seen, _concrete(mod.OpenRideAgent)


_AGENT_KWARGS = dict(
    unique_id='truck_7',
    run_id='run_1',
    reference_time='20200101080000',
    init_time_step=0,
    scheduler={'id': 'sched_1', 'orsim_settings': {}},
    behavior={},
)


def test_agent_on_the_default_path_passes_no_transport_keyword(monkeypatch):
    _select_none(monkeypatch)
    mod, seen, Agent = _patch_agent_super(monkeypatch)
    Agent(**_AGENT_KWARGS)
    assert seen['kwargs'] == _AGENT_KWARGS
    assert 'transport' not in seen['kwargs']


def test_agent_on_the_default_path_drops_an_explicit_none(monkeypatch):
    """transport=None must not be forwarded either -- v1.3.0 has no such parameter."""
    _select_none(monkeypatch)
    mod, seen, Agent = _patch_agent_super(monkeypatch)
    Agent(transport=None, **_AGENT_KWARGS)
    assert 'transport' not in seen['kwargs']


@requires_seam
def test_agent_builds_and_forwards_the_selected_transport(monkeypatch):
    made = _select(monkeypatch)
    mod, seen, Agent = _patch_agent_super(monkeypatch)
    Agent(**_AGENT_KWARGS)
    t = seen['kwargs']['transport']
    assert t is made[0]
    assert (t.run_id, t.identity) == ('run_1', 'truck_7')


@requires_seam
def test_agent_identity_survives_positional_construction(monkeypatch):
    """orsim's celery task uses **kwargs, but a positional caller must not get None topics."""
    made = _select(monkeypatch)
    mod, seen, Agent = _patch_agent_super(monkeypatch)
    Agent('truck_7', 'run_1', '20200101080000', 0, {'id': 'sched_1', 'orsim_settings': {}}, {})
    t = seen['kwargs']['transport']
    assert (t.run_id, t.identity) == ('run_1', 'truck_7')


@requires_seam
def test_agent_forwards_an_explicitly_supplied_transport(monkeypatch):
    mod, seen, Agent = _patch_agent_super(monkeypatch)
    sentinel = FakeTransport()
    Agent(transport=sentinel, **_AGENT_KWARGS)
    assert seen['kwargs']['transport'] is sentinel


def test_select_agent_transport_returns_a_copy_not_the_callers_dict(monkeypatch):
    _select_none(monkeypatch)
    original = dict(_AGENT_KWARGS)
    out = select_agent_transport((), original)
    assert out == _AGENT_KWARGS
    assert out is not original


# ---------------------------------------------------------------------------
# The stragglers: agents that subclass ORSimAgent DIRECTLY
# ---------------------------------------------------------------------------

AGENT_CLASS_PATHS = (
    'apps.container_logistics.truck:TruckAgent',
    'apps.container_logistics.order:OrderAgent',
    'apps.container_logistics.facility:FacilityAgent',
    'apps.container_logistics.order_lifecycle:OrderLifecycleAgent',
    'apps.container_logistics.assignment:AssignmentAgent',
    'apps.container_logistics.analytics:AnalyticsAgentIndie',
)


def _load_agent_class(path):
    import importlib

    module_name, _, attr = path.partition(':')
    return getattr(importlib.import_module(module_name), attr)


@pytest.mark.parametrize('path', AGENT_CLASS_PATHS)
def test_every_launched_agent_class_selects_a_transport(path):
    """
    Every ``agent_class`` in apps/simulation/container_logistics_wiring.py must
    pick its transport from the flag -- via OpenRideAgent or, for the three that
    subclass ORSimAgent directly, via TransportSelectingAgent.

    Miss one and a Kafka run is MIXED-BROKER: the scheduler publishes steps on
    Kafka while that agent still listens on MQTT, so it never replies, is never
    reaped (it is alive and well), and every step burns STEP_TIMEOUT waiting for
    it. This test is the guard against that returning when an agent is added.
    """
    from apps.agent_transport import TransportSelectingAgent
    from apps.container_logistics.openride_agent import OpenRideAgent

    cls = _load_agent_class(path)
    assert issubclass(cls, (OpenRideAgent, TransportSelectingAgent)), (
        f'{path} subclasses ORSimAgent without transport selection'
    )


def test_wiring_agent_class_list_is_still_the_one_under_test():
    """If wiring grows an agent, AGENT_CLASS_PATHS above must grow with it."""
    import re

    src = open(os.path.join(PROJECT_ROOT, 'apps', 'simulation',
                            'container_logistics_wiring.py')).read()
    declared = set(re.findall(r'"agent_class":\s*"([^"]+)"', src))
    covered = {p.replace(':', '.') for p in AGENT_CLASS_PATHS}
    assert declared == covered, f'wiring={sorted(declared)} covered={sorted(covered)}'


def _mixin_probe():
    """A TransportSelectingAgent whose 'ORSimAgent' base just records its kwargs."""
    from apps.agent_transport import TransportSelectingAgent

    seen = {}

    class RecordingBase:
        def __init__(self, *args, **kwargs):
            seen['args'] = args
            seen['kwargs'] = dict(kwargs)

    class Probe(TransportSelectingAgent, RecordingBase):
        pass

    return Probe, seen


def test_mixin_is_a_noop_on_the_default_path(monkeypatch):
    _select_none(monkeypatch)
    Probe, seen = _mixin_probe()
    Probe(**_AGENT_KWARGS)
    assert seen['kwargs'] == _AGENT_KWARGS
    assert 'transport' not in seen['kwargs']


@requires_seam
def test_mixin_builds_the_selected_transport(monkeypatch):
    made = _select(monkeypatch)
    Probe, seen = _mixin_probe()
    Probe(**_AGENT_KWARGS)
    t = seen['kwargs']['transport']
    assert t is made[0]
    assert (t.run_id, t.identity) == ('run_1', 'truck_7')


# ---------------------------------------------------------------------------
# Import hygiene + engine compatibility, in fresh interpreters
# ---------------------------------------------------------------------------

def _run_child(body: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop('OPENRIDE_AGENT_TRANSPORT', None)
    env['PYTHONPATH'] = os.pathsep.join(
        p for p in (env.get('PYTHONPATH', ''), PROJECT_ROOT) if p
    )
    return subprocess.run(
        [sys.executable, '-c', textwrap.dedent(body)],
        capture_output=True, text=True, cwd=PROJECT_ROOT, env=env, timeout=180,
    )


def _run_on_installed_orsim(body: str) -> subprocess.CompletedProcess:
    """A child WITHOUT the /home/user/orsim shadow -- the real installed engine."""
    env = dict(os.environ)
    env.pop('OPENRIDE_AGENT_TRANSPORT', None)
    env['PYTHONPATH'] = PROJECT_ROOT
    return subprocess.run(
        [sys.executable, '-c', textwrap.dedent(body)],
        capture_output=True, text=True, cwd=PROJECT_ROOT, env=env, timeout=180,
    )


def test_the_default_path_never_imports_confluent_kafka():
    """
    The transport-selection path must not drag in the Kafka client.

    A fresh interpreter, because sys.modules inside a pytest run is already
    polluted by other tests and would make this pass for the wrong reason.

    SCOPE, and it matters: this covers the modules this wiring owns plus
    ``apps.transport``'s own paho branch. It is NOT a process-wide claim --
    apps/utils/kafka_utils.py has imported confluent_kafka at module scope since
    long before phase 2. See the companion test below.
    """
    proc = _run_child(
        """
        import sys
        import apps.agent_transport as t
        import apps.transport                               # noqa: F401
        import apps.simulation.openride_scheduler           # noqa: F401
        import apps.container_logistics.openride_agent      # noqa: F401
        assert t.build_transport('r', 'a', {'AGENT_TRANSPORT': 'paho'}) is None
        assert t.build_transport('r', 'a', {}) is None
        leaked = [m for m in sys.modules if 'confluent_kafka' in m]
        assert not leaked, leaked
        print('CLEAN')
        """
    )
    assert proc.returncode == 0, proc.stderr
    assert 'CLEAN' in proc.stdout


def test_confluent_kafka_is_actually_installed_so_the_gate_means_something():
    """Guard against the previous test passing because the package is simply absent."""
    proc = _run_child("import confluent_kafka; print(confluent_kafka.version())")
    assert proc.returncode == 0, proc.stderr


def test_confluent_kafka_is_already_resident_via_the_existing_publishers():
    """
    Pins the PRE-EXISTING fact the gate above is scoped against, so nobody later
    reads that gate as "a celery worker has no Kafka client loaded". Importing
    some container_logistics agent packages loads confluent_kafka today, with
    the flag unset, through apps/utils/kafka_utils.py (KPI / trip_geo /
    run_status publishers). Measured per package: facility and analytics load
    it; truck, order, order_lifecycle and assignment do not. If this test ever
    starts FAILING, the gate above has become a genuine process-wide guarantee
    and its scope note can be relaxed.
    """
    proc = _run_child(
        """
        import sys
        import apps.container_logistics.facility  # noqa: F401
        assert any('confluent_kafka' in m for m in sys.modules)
        print('PREEXISTING')
        """
    )
    assert proc.returncode == 0, proc.stderr
    assert 'PREEXISTING' in proc.stdout


def test_app_constructs_unchanged_against_the_installed_orsim_v130():
    """
    The live celery workers import orsim **v1.3.0**, which has no transport
    seam. On the default path the app must construct exactly as before.
    """
    proc = _run_on_installed_orsim(
        """
        import inspect
        import orsim
        from orsim import ORSimScheduler

        assert orsim.__version__ == '1.3.0', orsim.__version__
        assert 'transport' not in inspect.signature(ORSimScheduler.__init__).parameters
        try:
            import orsim.messenger.transport
            raise AssertionError('seam unexpectedly present')
        except ImportError:
            pass

        from apps.agent_transport import build_transport, seam_available
        assert seam_available() is False
        assert build_transport('r', 'a', {'AGENT_TRANSPORT': 'paho'}) is None

        import apps.simulation.openride_scheduler as S
        import apps.container_logistics.openride_agent as A
        seen = {}
        def fake_sched(self, run_id, scheduler_id, orsim_settings, **kw):
            seen['sched'] = kw
            self.run_id, self.scheduler_id, self.orsim_settings = run_id, scheduler_id, orsim_settings
        def fake_agent(self, *a, **kw):
            seen['agent'] = kw
            self.unique_id, self.run_id, self.scheduler_id = 'x', 'r', 's'
            self.message_handlers = {}
        S.ORSimScheduler.__init__ = fake_sched
        A.ORSimAgent.__init__ = fake_agent

        S.OpenRideScheduler('r', 's', {})
        Concrete = type('C', (A.OpenRideAgent,), {
            n: (lambda self, *a, **k: None) for n in A.OpenRideAgent.__abstractmethods__
        })
        Concrete(unique_id='x', run_id='r', reference_time='20200101080000',
                 init_time_step=0, scheduler={'id': 's', 'orsim_settings': {}}, behavior={})
        assert 'transport' not in seen['sched'], seen['sched']
        assert 'transport' not in seen['agent'], seen['agent']
        print('V130_OK')
        """
    )
    assert proc.returncode == 0, proc.stderr
    assert 'V130_OK' in proc.stdout


def test_v130_refuses_a_selected_transport_with_a_useful_error():
    """
    The other half of graceful degradation. Default path on v1.3.0 = business as
    usual (above); a SELECTED transport on v1.3.0 must fail loudly at
    construction, naming the actual remedy, rather than raising
    ``TypeError: __init__() got an unexpected keyword argument 'transport'``
    from inside a celery worker where it reads like an app bug.
    """
    proc = _run_on_installed_orsim(
        """
        import orsim
        assert orsim.__version__ == '1.3.0', orsim.__version__

        import apps.agent_transport as t
        t.build_transport = lambda run_id, identity, settings=None: object()

        import apps.simulation.openride_scheduler as S
        try:
            S.OpenRideScheduler('r', 's', {})
        except t.TransportSeamMissing as exc:
            msg = str(exc)
            assert 'orsim.messenger.transport' in msg, msg
            assert 'PYTHONPATH=/home/user/orsim' in msg, msg
            print('REFUSED_CLEANLY')
        else:
            raise AssertionError('a transport was accepted on an orsim with no seam')
        """
    )
    assert proc.returncode == 0, proc.stderr
    assert 'REFUSED_CLEANLY' in proc.stdout


@requires_seam
def test_a_selected_transport_reaches_orsims_messenger():
    """
    End-to-end through the real orsim Messenger: what the wiring forwards is what
    ``messenger.client`` ends up holding, and the Messenger's own ABC enforcement
    accepts it. Skipped on the installed v1.3.0.
    """
    from orsim.messenger import Messenger
    from orsim.messenger.transport import MessengerTransport

    class DoubleTransport(MessengerTransport):
        def __init__(self):
            self.subscribed = []
            self.published = []

        def disconnect(self):
            pass

        def loop_start(self):
            pass

        def loop_stop(self, force=False):
            pass

        def publish(self, topic, payload=None, qos=0, retain=False):
            self.published.append((topic, payload))

        def subscribe(self, topic, qos=0):
            self.subscribed.append(topic)

        def unsubscribe(self, topic):
            pass

    t = DoubleTransport()
    m = Messenger({}, {'email': 'e', 'password': 'p'},
                  channel_id='run_1/sched_1/ORSimScheduler', transport=t)
    assert m.client is t
    assert t.subscribed == ['run_1/sched_1/ORSimScheduler']
