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

    def test_valid_release_id_queues_job(self, client):
        """A valid release ID should enqueue a print job (not block on subprocess)."""
        with patch.object(dt_server._print_queue, "put") as mock_put:
            response = client.post("/print",
                                   data=json.dumps({"release_id": "12345"}),
                                   content_type="application/json")
        mock_put.assert_called_once()
        assert response.status_code == 200

    def test_successful_print_returns_ok_queued(self, client):
        with patch.object(dt_server._print_queue, "put"):
            response = client.post("/print",
                                   data=json.dumps({"release_id": "12345"}),
                                   content_type="application/json")
        data = json.loads(response.data)
        assert data["ok"] is True
        assert data.get("queued") is True

    def test_dt_label_failure_returns_500(self, client):
        with patch.object(dt_server, "_run_dt_label", return_value=(1, "error output")):
            with dt_server.app.app_context():
                resp, status = dt_server._run_print("12345", "dk1247")
        assert status == 500
        data = json.loads(resp.data)
        assert data["ok"] is False

    def test_profile_passed_to_dt_label(self, client):
        with patch.object(dt_server, "_run_dt_label", return_value=(0, "")) as mock_run:
            with dt_server.app.app_context():
                dt_server._run_print("12345", "dk22243")
        args_used = mock_run.call_args[0][0]
        assert "dk22243" in args_used

    def test_split_flag_passed(self, client):
        with patch.object(dt_server, "_run_dt_label", return_value=(0, "")) as mock_run:
            with dt_server.app.app_context():
                dt_server._run_print("12345", "dk1247", split=True)
        args_used = mock_run.call_args[0][0]
        assert "--split" in args_used

    def test_no_bpm_flag_not_present_by_default(self, client):
        with patch.object(dt_server, "_run_dt_label", return_value=(0, "")) as mock_run:
            with dt_server.app.app_context():
                dt_server._run_print("12345", "dk1247")
        args_used = mock_run.call_args[0][0]
        assert "--no-bpm" not in args_used

    def test_no_bpm_flag_passed_when_requested(self, client):
        with patch.object(dt_server, "_run_dt_label", return_value=(0, "")) as mock_run:
            with dt_server.app.app_context():
                dt_server._run_print("12345", "dk1247", no_bpm=True)
        args_used = mock_run.call_args[0][0]
        assert "--no-bpm" in args_used

    def test_hide_bpm_queued_with_job(self, client):
        """hide_bpm=true in the POST payload should be forwarded into the print queue."""
        with patch.object(dt_server._print_queue, "put") as mock_put:
            client.post("/print",
                        data=json.dumps({"release_id": "12345", "hide_bpm": True}),
                        content_type="application/json")
        queued_args = mock_put.call_args[0][0]
        # Tuple is (release_id, profile, split, discs, no_bpm)
        assert queued_args[-1] is True  # no_bpm

    def test_hide_bpm_false_by_default_in_queue(self, client):
        with patch.object(dt_server._print_queue, "put") as mock_put:
            client.post("/print",
                        data=json.dumps({"release_id": "12345"}),
                        content_type="application/json")
        queued_args = mock_put.call_args[0][0]
        assert queued_args[-1] is False  # no_bpm defaults to False


# ─── /print preview mode ──────────────────────────────────────────────────────

class TestPreviewMode:
    def test_preview_mode_calls_dt_label_with_preview_flag(self, client, tmp_path):
        # _run_dt_label is mocked to create a fake PNG in PREVIEW_DIR
        # (the function first clears PREVIEW_DIR, then calls dt_label, then globs)
        def fake_run(args, *, capture=False):
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
        def fake_run(args, *, capture=False):
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
