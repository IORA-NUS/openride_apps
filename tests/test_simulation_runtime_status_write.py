"""Regression tests for the ``run_config`` status write (plan §14.4 R3-4 / R3-9 / R3-10 / R3-12).

Context: ``update_status`` used to collapse 412-after-retry, 500, 401, timeout and exception
into a single ``None``, and both call sites wrote ``... or self.run_record``. That is
fail-soft on a *state write*: a run that completed could record ``status: "In Progress"``
forever while the runtime proceeded as if the PATCH had landed. These tests pin the
outcome-typed replacement.

Stubbing seam: ``apps.simulation.simulation_runtime.get_http_session`` (the ``responses``
library is not installed in ``openride_apps/venv``). ``update_status`` itself is NEVER
stubbed — it is the code under test.
"""

import io
import logging
import re
import tokenize
from pathlib import Path

import pytest

from apps.simulation import simulation_runtime as sr
from apps.simulation.simulation_runtime import SimulationRuntime, StatusWrite

APPS_DIR = Path(__file__).resolve().parents[1] / "apps"
RUN_ID = "run_20260817_000000"
DOC_ID = "deadbeefdeadbeefdeadbeef"


# --------------------------------------------------------------------------- fakes


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or str(status_code)
        self.url = f"http://localhost:11654/run-config/{DOC_ID}"

    def json(self):
        return self._payload


class FakeSession:
    """Records every call so the tests can assert *how many* requests were made."""

    def __init__(self, patch_responses=None, get_responses=None, post_responses=None):
        self._patch_responses = list(patch_responses or [])
        self._get_responses = list(get_responses or [])
        self._post_responses = list(post_responses or [])
        self.patch_calls = []
        self.get_calls = []
        self.post_calls = []

    @staticmethod
    def _next(queue, kind):
        if not queue:
            raise AssertionError(f"unexpected extra {kind} request")
        return queue[0] if len(queue) == 1 else queue.pop(0)

    def patch(self, url, headers=None, data=None, timeout=None):
        self.patch_calls.append({"url": url, "headers": headers, "data": data})
        return self._next(self._patch_responses, "PATCH")

    def get(self, url, headers=None, timeout=None):
        self.get_calls.append({"url": url, "headers": headers})
        return self._next(self._get_responses, "GET")

    def post(self, url, headers=None, data=None, timeout=None):
        self.post_calls.append({"url": url, "headers": headers, "data": data})
        return self._next(self._post_responses, "POST")


class FakeUser:
    def get_headers(self, etag=None):
        headers = {"Authorization": "Basic x"}
        if etag is not None:
            headers["If-Match"] = etag
        return headers


class FakeRollup:
    max_step_index = -1
    max_step_ms = 0.0
    sum_step_ms = 0.0
    step_count = 0


def make_runtime(monkeypatch, session):
    """A SimulationRuntime with only the status-write collaborators wired.

    ``__new__`` avoids ORSimRuntime's constructor (kafka/celery/scenario); the status-write
    counters come from the production initialiser, not a test-side copy of it.
    """
    runtime = SimulationRuntime.__new__(SimulationRuntime)
    runtime.run_id = RUN_ID
    runtime.user = FakeUser()
    runtime.run_record = {"_id": DOC_ID, "_etag": "etag-stale"}
    runtime._terminal_status_publisher = None
    runtime._init_status_write_counters()
    monkeypatch.setattr(sr, "get_http_session", lambda: session)
    return runtime


@pytest.fixture
def sleeps(monkeypatch):
    """Capture backoff sleeps instead of paying them."""
    recorded = []
    monkeypatch.setattr(sr.time, "sleep", lambda s: recorded.append(s))
    return recorded


# --------------------------------------------------------------------------- R3-4 / R3-9


def test_412_retry_succeeds(monkeypatch, sleeps):
    """A stale etag is normal traffic (celery stamps meta.market concurrently), not a failure."""
    session = FakeSession(
        patch_responses=[
            FakeResponse(412, text="precondition failed"),
            FakeResponse(200, {"_id": DOC_ID, "_etag": "etag-new"}),
        ],
        get_responses=[FakeResponse(200, {"_id": DOC_ID, "_etag": "etag-fresh"})],
    )
    runtime = make_runtime(monkeypatch, session)

    record, outcome = runtime.update_status("In Progress", 12.5)

    assert outcome is StatusWrite.RETRIED_OK
    assert outcome.succeeded is True
    assert record == {"_id": DOC_ID, "_etag": "etag-new"}
    # Exactly one retry was needed, and it used the REFRESHED etag, not the cached one.
    assert len(session.patch_calls) == 2
    assert len(session.get_calls) == 1
    assert session.patch_calls[0]["headers"]["If-Match"] == "etag-stale"
    assert session.patch_calls[1]["headers"]["If-Match"] == "etag-fresh"

    # A success is applied to the call site and counted as a success, not a failure.
    runtime.run_record = {"_id": DOC_ID, "_etag": "etag-stale"}
    applied = runtime._apply_status_write((record, outcome))
    assert applied is StatusWrite.RETRIED_OK
    assert runtime.run_record == {"_id": DOC_ID, "_etag": "etag-new"}
    assert runtime.status_write_failures == 0
    assert runtime.status_write_outcomes == {"retried_ok": 1}


def test_412_twice_is_reported_not_swallowed(monkeypatch, sleeps, caplog):
    """Losing the etag race every time must be bounded, loud, and NOT mistaken for success."""
    session = FakeSession(
        patch_responses=[FakeResponse(412, text="precondition failed")],
        get_responses=[FakeResponse(200, {"_id": DOC_ID, "_etag": "etag-fresh"})],
    )
    runtime = make_runtime(monkeypatch, session)

    with caplog.at_level(logging.ERROR):
        record, outcome = runtime.update_status("In Progress", 1.0)

    assert outcome is StatusWrite.STALE_ETAG_EXHAUSTED
    assert outcome.succeeded is False
    assert record is None
    # Bounded (R3-9): it does not loop forever, and it does not stop after one try either.
    # The absolute bound is asserted as well as the derived count, so raising the constant
    # cannot make this test quietly re-certify an unbounded retry on the per-step path.
    assert 2 <= sr.STATUS_PATCH_MAX_ATTEMPTS <= 5
    assert len(session.patch_calls) == sr.STATUS_PATCH_MAX_ATTEMPTS
    assert len(session.get_calls) == sr.STATUS_PATCH_MAX_ATTEMPTS - 1
    # Backoff with jitter, one sleep between consecutive attempts, all within the cap.
    assert len(sleeps) == sr.STATUS_PATCH_MAX_ATTEMPTS - 1
    assert all(0.0 <= delay <= sr.STATUS_PATCH_BACKOFF_CAP_S for delay in sleeps)
    assert "etag race" in caplog.text

    # The call site must NOT keep pretending the write landed.
    before = dict(runtime.run_record)
    applied = runtime._apply_status_write((record, outcome))
    assert applied is StatusWrite.STALE_ETAG_EXHAUSTED
    assert runtime.run_record == before  # unchanged, and known-stale
    assert runtime.status_write_failures == 1
    assert runtime.consecutive_status_write_failures == 1
    assert runtime.status_write_outcomes == {"stale_etag_exhausted": 1}

    # And the rule is on the OUTCOME, not on the record being None: a record handed back
    # with a failing outcome must still be rejected. (``update_status`` returns None today;
    # this pins the contract so a future change cannot smuggle a stale record through.)
    runtime.run_record = {"_id": DOC_ID, "_etag": "etag-kept"}
    runtime._apply_status_write(({"_id": DOC_ID, "_etag": "etag-WRONG"}, StatusWrite.SERVER_ERROR))
    assert runtime.run_record == {"_id": DOC_ID, "_etag": "etag-kept"}
    runtime._apply_status_write(({"_id": DOC_ID, "_etag": "etag-ok"}, StatusWrite.OK))
    assert runtime.run_record == {"_id": DOC_ID, "_etag": "etag-ok"}


@pytest.mark.parametrize("status_code", [500, 401, 404])
def test_500_is_not_retried_and_is_surfaced(monkeypatch, sleeps, caplog, status_code):
    """A server error is a genuine failure of the write: classified, loud, never retried."""
    session = FakeSession(patch_responses=[FakeResponse(status_code, text="boom")])
    runtime = make_runtime(monkeypatch, session)

    with caplog.at_level(logging.ERROR):
        record, outcome = runtime.update_status("In Progress", 1.0)

    assert outcome is StatusWrite.SERVER_ERROR
    assert record is None
    # No retry and no etag refresh: a 500/401 is not a concurrency problem.
    assert len(session.patch_calls) == 1
    assert session.get_calls == []
    assert sleeps == []
    assert "Failed to update status" in caplog.text

    applied = runtime._apply_status_write((record, outcome))
    assert applied is StatusWrite.SERVER_ERROR
    assert runtime.status_write_failures == 1
    assert runtime.status_write_outcomes == {"server_error": 1}


def test_exception_is_classified_not_swallowed(monkeypatch, sleeps, caplog):
    """A timeout/connection reset is EXCEPTION, not silent success."""

    class ExplodingSession(FakeSession):
        def patch(self, *args, **kwargs):
            raise OSError("connection reset by peer")

    runtime = make_runtime(monkeypatch, ExplodingSession())

    with caplog.at_level(logging.ERROR):
        record, outcome = runtime.update_status("In Progress", 1.0)

    assert outcome is StatusWrite.EXCEPTION
    assert record is None
    assert "Exception in update_status" in caplog.text
    runtime._apply_status_write((record, outcome))
    assert runtime.status_write_failures == 1


def test_terminal_patch_failure_is_detectable(monkeypatch, sleeps, caplog):
    """A completed run whose terminal PATCH fails must not be silently 'In Progress'."""
    session = FakeSession(patch_responses=[FakeResponse(500, text="boom")])
    runtime = make_runtime(monkeypatch, session)

    published = []
    runtime._terminal_status_publisher = lambda reason, status=None: published.append(
        (reason, status)
    )

    with caplog.at_level(logging.ERROR):
        outcome = runtime._write_terminal_status(321.0)

    # 1. classified and retried with backoff (unlike the per-step path, the terminal write
    #    retries every failing outcome, because it decides discoverability).
    assert outcome is StatusWrite.SERVER_ERROR
    assert runtime.terminal_status_write is StatusWrite.SERVER_ERROR
    assert 2 <= sr.TERMINAL_STATUS_PATCH_MAX_ATTEMPTS <= 5, "the terminal write must retry"
    assert len(session.patch_calls) == sr.TERMINAL_STATUS_PATCH_MAX_ATTEMPTS
    assert len(sleeps) == sr.TERMINAL_STATUS_PATCH_MAX_ATTEMPTS - 1
    assert all(0.0 <= delay <= sr.TERMINAL_STATUS_BACKOFF_CAP_S for delay in sleeps)

    # 2. LOUD: an unmistakable error line, not an inference from a missing field.
    assert "TERMINAL STATUS WRITE FAILED" in caplog.text

    # 3. Still discoverable as finished: a terminal run_status is published.
    assert len(published) == 1
    reason, status = published[0]
    assert status == "COMPLETED"
    assert "terminal run_config PATCH failed" in reason

    # 4. SURFACED: the perf summary carries the counter, so the failure is visible without
    #    reading Mongo (where the run looks indistinguishable from one still in flight).
    payloads = []
    import apps.utils.perf_metrics as perf_metrics

    monkeypatch.setattr(
        perf_metrics,
        "publish_perf",
        lambda run_id, kind, payload, **kw: payloads.append((kind, payload)),
    )
    runtime.steps = 10
    runtime._step_wall_times_ms = []
    runtime._step_detail_sent_steps = set()
    runtime._perf_rollup = FakeRollup()
    runtime.perf_include_process = False
    runtime._publish_perf_summary(321.0)

    assert payloads, "perf summary was not published"
    kind, summary = payloads[0]
    assert kind == "summary"
    assert summary["status_write_failures"] == sr.TERMINAL_STATUS_PATCH_MAX_ATTEMPTS
    assert summary["terminal_status_write"] == "server_error"
    assert summary["status_write_outcomes"] == {
        "server_error": sr.TERMINAL_STATUS_PATCH_MAX_ATTEMPTS
    }


def test_terminal_patch_success_is_quiet(monkeypatch, sleeps, caplog):
    """Positive control: a healthy terminal write publishes nothing extra and counts no failure."""
    session = FakeSession(
        patch_responses=[FakeResponse(200, {"_id": DOC_ID, "_etag": "etag-final"})]
    )
    runtime = make_runtime(monkeypatch, session)
    published = []
    runtime._terminal_status_publisher = lambda reason, status=None: published.append(
        (reason, status)
    )

    with caplog.at_level(logging.ERROR):
        outcome = runtime._write_terminal_status(100.0)

    assert outcome is StatusWrite.OK
    assert runtime.terminal_status_write is StatusWrite.OK
    assert runtime.status_write_failures == 0
    assert published == []
    assert len(session.patch_calls) == 1
    assert "TERMINAL STATUS WRITE FAILED" not in caplog.text
    assert runtime.run_record == {"_id": DOC_ID, "_etag": "etag-final"}


# --------------------------------------------------------------------------- R3-12


def test_run_record_is_metadata_only(monkeypatch):
    """``self.run_record`` is an eve metadata stub, never the run document (plan §14.0)."""
    eve_stub = {
        "_id": DOC_ID,
        "_etag": "etag-0",
        "_updated": "Sun, 17 Aug 2026 00:00:00 GMT",
        "_created": "Sun, 17 Aug 2026 00:00:00 GMT",
        "_status": "OK",
        "_links": {"self": {"href": f"run-config/{DOC_ID}"}},
    }
    session = FakeSession(post_responses=[FakeResponse(201, eve_stub)])

    runtime = make_runtime(monkeypatch, session)
    runtime.run_record = None
    runtime.run_name = "unit-test-run"

    class FakeScenarioManager:
        def get_scenario_display_name(self):
            return "unit-test-scenario"

        def get_run_config_meta(self):
            return {"simulation_settings": {"COOPERATION": None}}

    runtime.scenario_manager = FakeScenarioManager()

    record = runtime.init_run_config()

    # BANDWIDTH_SAVER=True: no run fields come back. Reading run data off this stub is the
    # KeyError the old comment invited.
    assert set(record) <= sr.RUN_RECORD_METADATA_KEYS
    assert "meta" not in record
    assert "status" not in record
    assert "run_id" not in record
    assert record["_id"] == DOC_ID and record["_etag"] == "etag-0"

    # The two keys that ARE required must fail loudly, not at some later dereference.
    with pytest.raises(ValueError, match="_etag"):
        runtime._assert_run_record_shape({"_id": DOC_ID})
    with pytest.raises(ValueError, match="_id"):
        runtime._assert_run_record_shape({"_etag": "etag-0"})
    with pytest.raises(TypeError):
        runtime._assert_run_record_shape(["not", "a", "dict"])

    # If BANDWIDTH_SAVER were ever turned off, that is a warning, not a crash.
    full_doc = dict(eve_stub, run_id=RUN_ID, meta={}, status="In Progress")
    assert runtime._assert_run_record_shape(full_doc) is full_doc

    # The assertion must actually be WIRED INTO init_run_config, not merely available:
    # a POST that comes back without ``_etag`` has to fail here, not at the first PATCH.
    runtime2 = make_runtime(
        monkeypatch,
        FakeSession(post_responses=[FakeResponse(201, {"_id": DOC_ID, "_status": "OK"})]),
    )
    runtime2.run_record = None
    runtime2.run_name = "unit-test-run"
    runtime2.scenario_manager = FakeScenarioManager()
    with pytest.raises(ValueError, match="_etag"):
        runtime2.init_run_config()


# --------------------------------------------------------------------------- R3-10


def _apps_python_files():
    return [
        path
        for path in APPS_DIR.rglob("*.py")
        if "__pycache__" not in path.parts and "output" not in path.parts
    ]


def _code_only(text):
    """Blank out comments and string literals, preserving offsets and line numbers.

    Prose is allowed to NAME the antipattern (this module's own docstrings do); only real
    code may not contain it.
    """
    lines = [list(line) for line in text.splitlines(keepends=True)]
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return text
    for tok in tokens:
        if tok.type not in (tokenize.COMMENT, tokenize.STRING):
            continue
        (srow, scol), (erow, ecol) = tok.start, tok.end
        for row in range(srow, erow + 1):
            line = lines[row - 1]
            start = scol if row == srow else 0
            end = ecol if row == erow else len(line)
            for col in range(start, min(end, len(line))):
                if line[col] != "\n":
                    line[col] = " "
    return "".join("".join(line) for line in lines)


def test_no_unguarded_run_record_assignment_remains():
    """No ``self.run_record = self.update_status(...)`` survives anywhere under ``apps/``.

    That shape — with or without an ``or self.run_record`` tail — is what either blanked the
    record (aborting three 500-truck runs) or silently kept a stale one. Every remaining call
    must route through ``_apply_status_write``, which branches on the outcome. Also asserts
    the dead legacy runtime is gone (R3-10): its only reference in the tree was the
    commented-out ``start_simulation.sh:5``.
    """
    legacy = APPS_DIR / "distributed_openride_sim_randomised.py"
    assert not legacy.exists(), f"{legacy} carries the same unguarded pattern and is dead code"

    unguarded_assign = re.compile(r"self\.run_record\s*=\s*self\.update_status\s*\(")
    # Any ``... or self.run_record`` tail, whichever expression precedes it: that is the
    # swallow, and it reads as a harmless guard at every call site it has ever appeared in.
    swallow_tail = re.compile(r"\bor\s+self\.run_record\b")
    call = re.compile(r"self\.update_status\s*\(")

    offenders, swallows, unrouted = [], [], []
    call_sites = 0
    for path in _apps_python_files():
        raw = path.read_text(encoding="utf-8", errors="replace")
        if "update_status" not in raw:
            continue
        text = _code_only(raw)
        if unguarded_assign.search(text):
            offenders.append(str(path))
        if swallow_tail.search(text):
            swallows.append(str(path))
        for match in call.finditer(text):
            call_sites += 1
            preceding = text[max(0, match.start() - 160) : match.start()]
            if "_apply_status_write(" not in preceding:
                unrouted.append(f"{path}:{text.count(chr(10), 0, match.start()) + 1}")

    assert offenders == [], f"unguarded run_record assignment(s): {offenders}"
    assert swallows == [], f"``or self.run_record`` swallow(s) reintroduced: {swallows}"
    assert unrouted == [], f"update_status call(s) not routed through _apply_status_write: {unrouted}"
    # Guard against the assertions above passing vacuously if the call sites are ever renamed.
    assert call_sites >= 2, "expected the per-step and terminal status writes to be present"
