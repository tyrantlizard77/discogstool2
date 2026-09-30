"""Tests for dt_server — Flask HTTP bridge.

Covers:
  - _split_args: --split / --discs argument fragment builder
  - _bpm_args: --no-bpm argument fragment builder
  - /status endpoint: JSON with ok+version+beatport fields
  - /print endpoint: invalid release ID rejection (400)
  - /print endpoint: valid release ID dispatches to _run_dt_label
  - /print endpoint: hide_bpm flag passed through to subprocess
  - /preview/<filename> endpoint: serves files from PREVIEW_DIR
  - OPTIONS preflight: 204 response
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

# ── Load dt_server as a module (it has no .py extension) ─────────────────────

_DT_SERVER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dt_server"
)

_loader = importlib.machinery.SourceFileLoader("dt_server", _DT_SERVER_PATH)
_spec   = importlib.util.spec_from_loader("dt_server", _loader)
dt_server = importlib.util.module_from_spec(_spec)
sys.modules["dt_server"] = dt_server
_loader.exec_module(dt_server)

_split_args = dt_server._split_args


def _drain_one():
    """Pop the next queued job off the queue.

    Safe because dt_server only starts its worker thread from main(), so
    nothing competes with the test for queue items.
    """
    return dt_server._job_queue.get_nowait()


@pytest.fixture(autouse=True)
def _isolate_jobs():
    """Reset queue and job registry so tests don't leak state into each other."""
    while True:
        try:
            dt_server._job_queue.get_nowait()
        except Exception:
            break
    dt_server._jobs.clear()
    yield
    while True:
        try:
            dt_server._job_queue.get_nowait()
        except Exception:
            break
    dt_server._jobs.clear()
_bpm_args   = dt_server._bpm_args
app         = dt_server.app

# Configure Flask test mode
app.config["TESTING"] = True


@pytest.fixture
def client():
    return app.test_client()


# ─── _split_args ──────────────────────────────────────────────────────────────

class TestSplitArgs:
    def test_no_split(self):
        assert _split_args(False, None) == []

    def test_split_no_discs(self):
        assert _split_args(True, None) == ["--split"]

    def test_split_with_discs(self):
        result = _split_args(True, [1, 2])
        assert "--split" in result
        assert "--discs" in result
        assert "1" in result
        assert "2" in result

    def test_discs_without_split_ignored(self):
        """--discs should only appear when --split is also requested."""
        result = _split_args(False, [1, 2])
        assert "--discs" not in result

    def test_discs_converted_to_strings(self):
        result = _split_args(True, [3])
        assert "3" in result
        # Should not contain the integer 3 — must be a string
        assert 3 not in result

    def test_single_disc(self):
        result = _split_args(True, [2])
        assert "2" in result
        assert "--discs" in result


# ─── _bpm_args ────────────────────────────────────────────────────────────────

class TestBpmArgs:
    def test_no_bpm_false_returns_empty(self):
        assert _bpm_args(False) == []

    def test_no_bpm_true_returns_flag(self):
        assert _bpm_args(True) == ["--no-bpm"]


# ─── /status endpoint ─────────────────────────────────────────────────────────

class TestStatusEndpoint:
    def test_status_returns_200(self, client):
        response = client.get("/status")
        assert response.status_code == 200

    def test_status_ok_true(self, client):
        data = json.loads(response_data := client.get("/status").data)
        assert data["ok"] is True

    def test_status_has_version(self, client):
        data = json.loads(client.get("/status").data)
        assert "version" in data

    def test_status_has_beatport(self, client):
        data = json.loads(client.get("/status").data)
        assert "beatport" in data

    def test_status_beatport_is_dict(self, client):
        data = json.loads(client.get("/status").data)
        assert isinstance(data["beatport"], dict)

    def test_status_service_keys(self, client):
        """One dot per service in the popup footer.

        The 'llm' entry went away with the local dt_find backend: its check had
        collapsed into _check_anthropic (both just look for an API key), so the
        popup was drawing two dots that could never disagree.
        """
        data = json.loads(client.get("/status").data)
        services = set(data) - {"ok", "version"}
        assert services == {"discogs", "beatport", "anthropic"}

    def test_status_content_type_json(self, client):
        response = client.get("/status")
        assert "application/json" in response.content_type


# ─── OPTIONS preflight ────────────────────────────────────────────────────────

class TestPreflight:
    def test_print_options(self, client):
        response = client.options("/print")
        assert response.status_code == 204

    def test_status_options(self, client):
        response = client.options("/status")
        assert response.status_code == 204

    def test_preview_options(self, client):
        response = client.options("/preview")
        assert response.status_code == 204


# ─── /print endpoint ──────────────────────────────────────────────────────────

class TestPrintEndpoint:
    def test_missing_release_id_returns_400(self, client):
        response = client.post("/print",
                               data=json.dumps({}),
                               content_type="application/json")
        assert response.status_code == 400

    def test_non_numeric_release_id_returns_400(self, client):
        response = client.post("/print",
                               data=json.dumps({"release_id": "abc"}),
                               content_type="application/json")
        assert response.status_code == 400

    def test_invalid_release_id_message(self, client):
        response = client.post("/print",
                               data=json.dumps({"release_id": "notanumber"}),
                               content_type="application/json")
        data = json.loads(response.data)
        assert data["ok"] is False
        assert "message" in data

    def test_valid_release_id_is_queued(self, client):
        """A valid release ID is accepted and queued, not run inline."""
        response = client.post("/print",
                               data=json.dumps({"release_id": "12345"}),
                               content_type="application/json")
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["ok"] is True
        assert "job_id" in data

    def test_successful_print_returns_ok(self, client):
        response = client.post("/print",
                               data=json.dumps({"release_id": "12345"}),
                               content_type="application/json")
        data = json.loads(response.data)
        assert data["ok"] is True

    def test_queue_depth_reported(self, client):
        first = json.loads(client.post(
            "/print", data=json.dumps({"release_id": "111"}),
            content_type="application/json").data)
        second = json.loads(client.post(
            "/print", data=json.dumps({"release_id": "222"}),
            content_type="application/json").data)
        assert second["queued"] >= first["queued"]

    def test_failure_recorded_on_job(self, client):
        """dt_label failing marks the job errored and keeps the message."""
        job = dt_server._new_job("12345", "print")
        job["args"] = ["--", "12345"]
        with patch.object(dt_server, "_run_dt_label",
                          return_value=(1, "boom")):
            dt_server._execute_job(job)
        assert job["state"] == "error"
        assert "boom" in job["error"]

    def test_failure_does_not_stop_later_jobs(self, client):
        """A failed job must not prevent the next one from running."""
        bad = dt_server._new_job("111", "print"); bad["args"] = ["--", "111"]
        good = dt_server._new_job("222", "print"); good["args"] = ["--", "222"]
        with patch.object(dt_server, "_run_dt_label", return_value=(1, "boom")):
            dt_server._execute_job(bad)
        with patch.object(dt_server, "_run_dt_label", return_value=(0, "")):
            dt_server._execute_job(good)
        assert bad["state"] == "error"
        assert good["state"] == "done"

    def test_profile_passed_to_dt_label(self, client):
        with patch.object(dt_server, "_run_dt_label", return_value=(0, "")) as mock_run:
            with dt_server.app.app_context():
                dt_server._queue_print("12345", "dk22243")
            job = _drain_one()
            dt_server._execute_job(job)
        assert "dk22243" in mock_run.call_args[0][0]

    def test_split_flag_passed(self, client):
        with patch.object(dt_server, "_run_dt_label", return_value=(0, "")) as mock_run:
            with dt_server.app.app_context():
                dt_server._queue_print("12345", "dk1247", split=True)
            dt_server._execute_job(_drain_one())
        assert "--split" in mock_run.call_args[0][0]

    def test_no_bpm_flag_not_present_by_default(self, client):
        with patch.object(dt_server, "_run_dt_label", return_value=(0, "")) as mock_run:
            with dt_server.app.app_context():
                dt_server._queue_print("12345", "dk1247")
            dt_server._execute_job(_drain_one())
        assert "--no-bpm" not in mock_run.call_args[0][0]

    def test_no_bpm_flag_passed_when_requested(self, client):
        with patch.object(dt_server, "_run_dt_label", return_value=(0, "")) as mock_run:
            with dt_server.app.app_context():
                dt_server._queue_print("12345", "dk1247", no_bpm=True)
            dt_server._execute_job(_drain_one())
        assert "--no-bpm" in mock_run.call_args[0][0]

    def test_hide_bpm_passed_to_dt_label(self, client):
        """hide_bpm=true in the POST payload should add --no-bpm to the job args."""
        client.post("/print",
                    data=json.dumps({"release_id": "12345", "hide_bpm": True}),
                    content_type="application/json")
        job = _drain_one()
        assert "--no-bpm" in job["args"]

    def test_hide_bpm_false_by_default(self, client):
        client.post("/print",
                    data=json.dumps({"release_id": "12345"}),
                    content_type="application/json")
        job = _drain_one()
        assert "--no-bpm" not in job["args"]


# ─── /print preview mode ──────────────────────────────────────────────────────

class TestPreviewMode:
    def test_preview_mode_calls_dt_label_with_preview_flag(self, client, tmp_path):
        # _run_dt_label is mocked to create a fake PNG in PREVIEW_DIR
        # (the function first clears PREVIEW_DIR, then calls dt_label, then globs)
        def fake_run(args, *, job=None):
            import pathlib
            (pathlib.Path(dt_server.PREVIEW_DIR) / "12345_label.png").write_bytes(b"fake")
            return (0, "")

        with patch.object(dt_server, "PREVIEW_DIR", str(tmp_path)), \
             patch.object(dt_server, "_run_dt_label", side_effect=fake_run):
            response = client.post("/print",
                                   data=json.dumps({"release_id": "12345", "preview": True}),
                                   content_type="application/json")

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["ok"] is True
        assert "preview_urls" in data

    def test_preview_no_bpm_flag_passed(self, client, tmp_path):
        """hide_bpm=true in a preview request should pass --no-bpm to dt_label."""
        def fake_run(args, *, job=None):
            import pathlib
            (pathlib.Path(dt_server.PREVIEW_DIR) / "label.png").write_bytes(b"fake")
            return (0, "")

        with patch.object(dt_server, "PREVIEW_DIR", str(tmp_path)), \
             patch.object(dt_server, "_run_dt_label", side_effect=fake_run) as mock_run:
            client.post("/print",
                        data=json.dumps({"release_id": "12345", "preview": True, "hide_bpm": True}),
                        content_type="application/json")

        args_used = mock_run.call_args[0][0]
        assert "--no-bpm" in args_used

    def test_preview_no_pngs_returns_500(self, client, tmp_path):
        """If dt_label exits 0 but produces no PNG, return 500."""
        # Empty tmp_path — no PNG files
        with patch.object(dt_server, "PREVIEW_DIR", str(tmp_path)), \
             patch.object(dt_server, "_run_dt_label", return_value=(0, "")):
            response = client.post("/print",
                                   data=json.dumps({"release_id": "12345", "preview": True}),
                                   content_type="application/json")

        assert response.status_code == 500


# ─── Progress events and job state ────────────────────────────────────────────

class TestProgressEvents:
    def test_apply_event_sets_stage_state(self):
        job = dt_server._new_job("1", "print")
        dt_server._apply_event(job, {"stage": "lookup", "state": "done",
                                     "title": "Various – Playbook001"})
        assert job["stages"]["lookup"]["state"] == "done"
        assert job["title"] == "Various – Playbook001"

    def test_apply_event_carries_counts(self):
        job = dt_server._new_job("1", "print")
        dt_server._apply_event(job, {"stage": "bpm", "state": "progress",
                                     "done": 2, "total": 4})
        assert job["stages"]["bpm"]["done"] == 2
        assert job["stages"]["bpm"]["total"] == 4

    def test_apply_event_records_error(self):
        job = dt_server._new_job("1", "print")
        dt_server._apply_event(job, {"stage": "render", "state": "error",
                                     "message": "too tall"})
        assert job["error"] == "too tall"

    def test_unknown_stage_ignored(self):
        job = dt_server._new_job("1", "print")
        dt_server._apply_event(job, {"stage": "bogus", "state": "done"})
        assert "bogus" not in job["stages"]

    def test_all_stages_start_pending(self):
        job = dt_server._new_job("1", "print")
        assert all(s["state"] == "pending" for s in job["stages"].values())
        assert list(job["stages"]) == list(dt_server.STAGES)


class TestProgressEndpoint:
    def test_progress_reports_depth(self, client):
        client.post("/print", data=json.dumps({"release_id": "12345"}),
                    content_type="application/json")
        data = json.loads(client.get("/progress").data)
        assert data["ok"] is True
        assert data["depth"] == 1

    def test_progress_lists_queued_job(self, client):
        client.post("/print", data=json.dumps({"release_id": "999"}),
                    content_type="application/json")
        data = json.loads(client.get("/progress").data)
        assert data["queued"][0]["release_id"] == "999"

    def test_progress_hides_internal_args(self, client):
        client.post("/print", data=json.dumps({"release_id": "999"}),
                    content_type="application/json")
        data = json.loads(client.get("/progress").data)
        assert "args" not in data["queued"][0]

    def test_progress_counts_failures(self, client):
        job = dt_server._new_job("1", "print")
        job["args"] = ["--", "1"]
        with patch.object(dt_server, "_run_dt_label", return_value=(1, "boom")):
            dt_server._execute_job(job)
        data = json.loads(client.get("/progress").data)
        assert data["failures"] == 1

    def test_clear_jobs_drops_finished(self, client):
        job = dt_server._new_job("1", "print")
        job["args"] = ["--", "1"]
        with patch.object(dt_server, "_run_dt_label", return_value=(1, "boom")):
            dt_server._execute_job(job)
        client.post("/jobs/clear")
        data = json.loads(client.get("/progress").data)
        assert data["failures"] == 0

    def test_progress_options_preflight(self, client):
        assert client.options("/progress").status_code == 204


# ─── CORS origin allowlist ────────────────────────────────────────────────────

class TestCorsOrigins:
    """Only browser-extension origins may talk to the server.

    dt_server binds to localhost, but any web page the user has open could
    otherwise POST to it, so the allowlist must not widen to http(s) origins.
    """

    def test_firefox_origin_allowed(self, client):
        r = client.get("/status", headers={"Origin": "moz-extension://abc123"})
        assert r.headers.get("Access-Control-Allow-Origin") == "moz-extension://abc123"

    def test_chrome_origin_allowed(self, client):
        r = client.get("/status", headers={"Origin": "chrome-extension://def456"})
        assert r.headers.get("Access-Control-Allow-Origin") == "chrome-extension://def456"

    def test_origin_is_echoed_not_wildcarded(self, client):
        r = client.get("/status", headers={"Origin": "chrome-extension://xyz"})
        assert r.headers.get("Access-Control-Allow-Origin") != "*"

    @pytest.mark.parametrize("origin", [
        "https://www.discogs.com",
        "http://localhost:8080",
        "http://evil.example.com",
        "file://",
        "",
    ])
    def test_non_extension_origins_rejected(self, client, origin):
        r = client.get("/status", headers={"Origin": origin})
        assert "Access-Control-Allow-Origin" not in r.headers

    def test_lookalike_origin_rejected(self):
        """Substring tricks must not pass the prefix check."""
        assert not dt_server._is_extension_origin("https://moz-extension://x")
        assert not dt_server._is_extension_origin("https://evil.com/chrome-extension://")

    def test_preflight_carries_cors_headers(self, client):
        r = client.options("/print", headers={"Origin": "chrome-extension://abc"})
        assert r.status_code == 204
        assert r.headers.get("Access-Control-Allow-Origin") == "chrome-extension://abc"
        assert "POST" in r.headers.get("Access-Control-Allow-Methods", "")
