"""Run ids for the phase-2 transport tests that the ops sweep can actually collect.

A lane topic is only ever collected by ``reap_lane_topics_older_than``, which
dates a topic from the ``run_YYYYMMDD_HHMMSS`` stamp inside its own name
(``lane_admin._RUN_ID_STAMP``) and SKIPS -- forever, by design -- anything it
cannot date.  That stamp is strictly stricter than the delete filter
(``_REAPABLE``), so a run id like ``run_fail_12345_678`` passes the filter,
fails the stamp, and leaves three topics no code path can ever delete.

The suite did exactly that: the broker was found carrying
``orsim_{bcast,reply,app}_run_probe6_2823712`` and friends, left by these tests'
own fixtures, with the sweep logging ``skipping ...: run id carries no parseable
timestamp`` on every pass, forever.

``_RUN_ID_STAMP`` is not anchored at the end, so a disambiguating suffix AFTER
the stamp is allowed -- which is what lets a test id be both unique and
sweepable.
"""

import os
import time
from datetime import datetime


def dated_run_id(suffix: str = 'test') -> str:
    """``run_YYYYMMDD_HHMMSS_<suffix>_<pid>_<ms>`` -- unique AND sweepable.

    The stamp is the real current time, so the aged sweep collects the debris
    once it is older than ``MIN_SWEEP_AGE_HOURS`` instead of never.
    """
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    unique = f'{os.getpid()}{int(time.time() * 1000) % 100000}'
    safe = ''.join(ch for ch in str(suffix) if ch.isalnum() or ch in '-._')
    return f'run_{stamp}_{safe}_{unique}'
