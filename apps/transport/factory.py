"""The one place that decides which transport an agent or scheduler gets.

THE FLAG TRAVELS IN ``messenger_settings``, NOT IN ``os.environ``
-----------------------------------------------------------------
``build_agent_transport`` reads ``messenger_settings['AGENT_TRANSPORT']`` and
deliberately does NOT read the environment.  Celery workers here are long-lived
across runs: a worker that read ``OPENRIDE_AGENT_TRANSPORT`` itself could serve
run A on paho and run B on kafka, or half-migrate a single run if the variable
changed mid-flight.  ``apps/config.py`` stamps the value onto
``messenger_backend``, which is what ``ORSimEnv.set_backend()`` receives inside
the worker -- the only channel that crosses the JSON-serialised celery task
boundary.  So the LAUNCHING process decides once, and the choice rides the task
payload to every agent of that run.
``tests/test_agent_transport_flag_default.py`` asserts the resolution reads the
settings dict.

RETURNING ``None`` IS THE DEFAULT, AND IT IS THE WHOLE INERTNESS ARGUMENT
-------------------------------------------------------------------------
``None`` means "no transport supplied", which is what ``Messenger`` already
receives today -- it then builds a ``PahoTransport`` on exactly today's code
path, with the same construction order and the same exception identity.  Phase 2
is observationally invisible with the flag unset because the default answer here
is literally the absence of a decision.

AN UNKNOWN NAME RAISES.  Falling back to paho on a typo would mean
``OPENRIDE_AGENT_TRANSPORT=kakfa`` runs a whole 7-day simulation on the old
transport while the operator believes they measured the new one -- a
comparability lie that no log line would reveal.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

#: The value that means "change nothing".  Also the value ``apps/config.py``
#: stamps when ``OPENRIDE_AGENT_TRANSPORT`` is unset.
TRANSPORT_PAHO = 'paho'

#: The phase-2 implementation, inert until someone asks for it by name.
TRANSPORT_KAFKA = 'kafka'

KNOWN_TRANSPORTS = (TRANSPORT_PAHO, TRANSPORT_KAFKA)


def build_agent_transport(messenger_settings: Optional[dict], run_id: str, identity: str):
    """Return a transport for ``identity``, or ``None`` to keep today's paho path.

    Args:
        messenger_settings (dict | None): ``ORSimEnv.messenger_settings`` inside a
            worker, or ``apps.config.messenger_backend`` in the launching
            process.  ``None`` and ``{}`` both mean paho.
        run_id (str): scopes the lane topics and the lane classification.
        identity (str): the agent's ``unique_id``; the reply-lane routing key.

    Returns:
        ``MessengerTransport`` or ``None``.

    Raises:
        ValueError: for any name that is not in :data:`KNOWN_TRANSPORTS`.
    """
    settings = messenger_settings or {}
    name = str(settings.get('AGENT_TRANSPORT', TRANSPORT_PAHO) or TRANSPORT_PAHO).strip().lower()

    if name == TRANSPORT_PAHO:
        return None

    if name == TRANSPORT_KAFKA:
        # Imported HERE, not at module scope: `apps.transport` must cost nothing
        # to import on the paho path, and `confluent_kafka` is a C extension.
        from apps.transport.kafka_transport import KafkaTransport

        logger.debug('building KafkaTransport for %s (run %s)', identity, run_id)
        return KafkaTransport(settings, run_id, identity)

    raise ValueError(
        f'unknown AGENT_TRANSPORT {name!r}; expected one of {KNOWN_TRANSPORTS}. '
        'Refusing to fall back to paho: a typo would silently run the whole '
        'simulation on the transport you thought you had replaced.')
