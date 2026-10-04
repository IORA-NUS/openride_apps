"""Phase 2 is observationally invisible with the flag unset.  This is that guard.

SCOPE, so this does not silently duplicate its neighbour.
``tests/test_agent_transport_flag.py`` owns the WIRING side -- how
``OpenRideAgent`` / ``OpenRideScheduler`` / the three direct ``ORSimAgent``
subclasses apply the decision, and how they degrade against the installed orsim
v1.3.0.  This file owns the two ends that wiring sits between:

* ``apps/config.py`` -- does the flag get STAMPED onto ``messenger_backend``,
  which is the only channel that crosses the JSON-serialised celery task
  boundary; and
* ``apps/transport.factory`` -- given a settings dict, what is DECIDED.

The property both halves exist to protect: with ``OPENRIDE_AGENT_TRANSPORT``
unset, ``build_agent_transport`` returns ``None``, ``Messenger`` therefore builds
a ``PahoTransport`` on exactly today's code path, and the app-side regression
suite still reports its pre-phase-2 numbers.  An implementer who sees a
different pass count has changed observable behaviour with the flag unset, which
is the one thing phase 2 must not do.
"""

import os
import subprocess
import sys

import pytest

from apps.transport import build_agent_transport
from apps.transport.factory import KNOWN_TRANSPORTS, TRANSPORT_PAHO

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_child(code, env_overrides=None):
    """Evaluate ``code`` in a fresh interpreter, so env reads happen at import.

    ``apps.config`` reads ``os.getenv`` at MODULE SCOPE, so monkeypatching the
    environment inside this process would prove nothing about a worker that
    imported it at boot.
    """
    env = dict(os.environ, **(env_overrides or {}))
    env['PYTHONPATH'] = os.pathsep.join(
        p for p in (env.get('PYTHONPATH'), '/home/user/orsim', REPO_ROOT) if p)
    result = subprocess.run([sys.executable, '-c', code], cwd=REPO_ROOT,
                            env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, f'child failed:\n{result.stdout}\n{result.stderr}'
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# apps/config.py -- the stamp
# ---------------------------------------------------------------------------

def test_config_stamps_paho_when_the_env_var_is_unset():
    """The default that makes everything else inert."""
    env = dict(os.environ)
    env.pop('OPENRIDE_AGENT_TRANSPORT', None)
    result = subprocess.run(
        [sys.executable, '-c',
         'from apps.config import messenger_backend, agent_transport;'
         'print(agent_transport); print(messenger_backend["AGENT_TRANSPORT"])'],
        cwd=REPO_ROOT,
        env=dict(env, PYTHONPATH=os.pathsep.join(
            p for p in (env.get('PYTHONPATH'), '/home/user/orsim', REPO_ROOT) if p)),
        capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ['paho', 'paho']


def test_an_empty_env_var_still_stamps_paho_not_the_empty_string():
    """`OPENRIDE_AGENT_TRANSPORT=` in a unit file is "unset", not "unknown".

    Without the trailing ``or "paho"`` in ``apps/config.py`` this stamps ``''``,
    and any reader stricter than the factory would refuse to start the run.
    """
    out = _run_child(
        'from apps.config import messenger_backend as m; print(m["AGENT_TRANSPORT"])',
        {'OPENRIDE_AGENT_TRANSPORT': '   '},
    )
    assert out == 'paho'


def test_config_stamps_the_kafka_keys_the_transport_needs():
    """``KafkaTransport.__init__`` raises without ``KAFKA_BOOTSTRAP_SERVERS``.

    Stamping it here rather than re-reading ``kafka_config`` inside the worker
    is what keeps the launching process the single decider.
    """
    out = _run_child(
        'from apps.config import messenger_backend as m;'
        'print(m["KAFKA_BOOTSTRAP_SERVERS"], m["KAFKA_APP_PARTITIONS"],'
        ' m["KAFKA_REPLY_PARTITIONS"], m["KAFKA_LINGER_MS"],'
        ' m["KAFKA_MESSAGE_TIMEOUT_MS"], m["KAFKA_PRODUCER_RETRIES"])')
    bootstrap, app, reply, linger, msg_timeout, retries = out.split()
    assert ':' in bootstrap
    # linger=0: measured, n=3 per arm — linger=5 cost ~10 ms per step (two
    # single-record publishes per barrier, nothing to batch with) and bought
    # nothing. message.timeout.ms must be EXPLICIT: librdkafka's own default is
    # 300_000, which leaves the only error channel the design has blind for five
    # minutes. retries=0: retries without idempotence permit duplicates, and
    # nothing in ORSim dedupes on time_step.
    assert (int(app), int(reply), int(linger)) == (64, 8, 0)
    assert int(msg_timeout) == 10_000
    assert int(retries) == 0


def test_the_env_var_reaches_the_stamp_and_is_normalised():
    """Set once by the launcher, then carried in the payload -- never re-read."""
    out = _run_child(
        'from apps.config import messenger_backend as m; print(m["AGENT_TRANSPORT"])',
        {'OPENRIDE_AGENT_TRANSPORT': '  KaFkA  '},
    )
    assert out == 'kafka'


# ---------------------------------------------------------------------------
# apps/transport.factory -- the decision
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('settings', [
    None,
    {},
    {'AGENT_TRANSPORT': 'paho'},
    {'AGENT_TRANSPORT': 'PAHO'},
    {'AGENT_TRANSPORT': '  paho '},
    {'AGENT_TRANSPORT': ''},
    {'AGENT_TRANSPORT': None},
], ids=['none', 'empty', 'paho', 'upper', 'padded', 'blank', 'null'])
def test_build_agent_transport_returns_none_on_the_default_path(settings):
    """``None`` is not an error -- it is "no decision", i.e. today's code path.

    ``Messenger`` then constructs a ``PahoTransport`` itself, in the same order
    and with the same exception identity as before phase 2.
    """
    assert build_agent_transport(settings, 'run_20260911_054140', 'truck_0007') is None


def test_an_unknown_name_raises_rather_than_falling_back():
    """``OPENRIDE_AGENT_TRANSPORT=kakfa`` must not quietly run a 7-day sim on paho.

    A silent fallback would be a comparability lie with no log line to reveal
    it: the operator believes they measured the new transport and did not.
    """
    with pytest.raises(ValueError) as excinfo:
        build_agent_transport({'AGENT_TRANSPORT': 'kakfa'},
                              'run_20260911_054140', 'truck_0007')
    message = str(excinfo.value)
    assert 'kakfa' in message
    for name in KNOWN_TRANSPORTS:
        assert name in message


def test_the_decision_reads_the_settings_dict_not_the_environment(monkeypatch):
    """Celery workers are long-lived across runs.

    A worker that read ``OPENRIDE_AGENT_TRANSPORT`` itself could serve run A on
    paho and run B on kafka, or half-migrate a single run if the variable
    changed mid-flight.  The settings dict rides the JSON task payload, so the
    launching process decides once.
    """
    monkeypatch.setenv('OPENRIDE_AGENT_TRANSPORT', 'kafka')
    # The environment says kafka; the settings say paho.  Settings win, and no
    # Kafka machinery is touched (this test needs no broker).
    assert build_agent_transport({'AGENT_TRANSPORT': 'paho'}, 'run_x', 'truck_0') is None

    monkeypatch.setenv('OPENRIDE_AGENT_TRANSPORT', 'paho')
    with pytest.raises(ValueError):
        # ...and an unknown value in the SETTINGS is still an error even though
        # the environment holds a valid one.
        build_agent_transport({'AGENT_TRANSPORT': 'rabbit'}, 'run_x', 'truck_0')


def test_the_paho_constant_is_what_config_stamps():
    """One spelling of the default, in one place, checked against the other."""
    assert TRANSPORT_PAHO == 'paho'


# ---------------------------------------------------------------------------
# The package's own import-time promise
# ---------------------------------------------------------------------------

def test_importing_apps_transport_does_not_import_confluent_kafka():
    """``from apps.transport import build_agent_transport`` must be free.

    ``apps/transport/__init__.py`` says so, and it is only true while every
    ``confluent_kafka`` import in the package stays function-local.  A
    module-scope import added to ``factory`` or ``lane_admin`` would break it
    silently -- nothing would fail, the paho path would just start paying for a
    C extension it never uses, and the "cannot change import-time behaviour"
    claim would quietly stop being true.

    NOTE the sibling test in ``tests/test_agent_transport_flag.py`` checks the
    same property for the AGENT path (which also pulls in
    ``apps.utils.kafka_utils``, a module-scope importer).  This one is narrower:
    it isolates ``apps.transport`` itself.
    """
    out = _run_child(
        'import sys;'
        'from apps.transport import build_agent_transport;'
        'from apps.transport import lane_topics, factory;'
        "print(sorted(m for m in sys.modules if 'confluent_kafka' in m))")
    assert out == '[]', f'apps.transport pulled in confluent_kafka at import: {out}'


def test_lane_topics_is_importable_with_no_infrastructure_at_all():
    """The file a reviewer reads to check injectivity must be checkable offline."""
    out = _run_child(
        'from apps.transport.lane_topics import kafka_topic_for;'
        "print(kafka_topic_for('r/s/ORSimAgent', 'r'))")
    assert out == 'orsim_bcast_r'
