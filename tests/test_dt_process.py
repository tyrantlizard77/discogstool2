"""Tests for dt_process — pure helper functions.

Covers:
  - get_possible_positions: position string variant generation
  - get_wav_regions: consecutive-marker region building
  - get_wav_regions_from_markers: ltxt-length-based region building
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ── Load dt_process as a module (it has no .py extension) ────────────────────

_DT_PROCESS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dt_process"
)

_loader = importlib.machinery.SourceFileLoader("dt_process", _DT_PROCESS_PATH)
_spec   = importlib.util.spec_from_loader("dt_process", _loader)
dt_process = importlib.util.module_from_spec(_spec)
sys.modules["dt_process"] = dt_process
_loader.exec_module(dt_process)

get_possible_positions        = dt_process.get_possible_positions
get_wav_regions               = dt_process.get_wav_regions
get_wav_regions_from_markers  = dt_process.get_wav_regions_from_markers
normalize_loudnorm            = dt_process.normalize_loudnorm
ConversionException           = dt_process.ConversionException


# ─── get_possible_positions ───────────────────────────────────────────────────

class TestGetPossiblePositions:
    def test_basic_a1_includes_itself(self):
        result = get_possible_positions("A1")
        assert "A1" in result

    def test_a1_includes_a(self):
        """A1 might be stored as just A on Discogs."""
        result = get_possible_positions("A1")
        assert "A" in result

    def test_b1_includes_aa1(self):
        """B-side tracks may be written as AA on some labels."""
        result = get_possible_positions("B1")
        assert "AA1" in result or any("AA" in r for r in result)

    def test_a_includes_a1(self):
        """Pure side A might be stored as A1."""
        result = get_possible_positions("A")
        assert "A1" in result

    def test_includes_dot_variant(self):
        """Some releases use 'A1.' with a trailing dot."""
        result = get_possible_positions("A1")
        assert "A1." in result

    def test_returns_list(self):
        assert isinstance(get_possible_positions("A1"), list)

    def test_no_empty_strings(self):
        result = get_possible_positions("A1")
        assert all(r != "" for r in result)

    def test_b2_includes_itself(self):
        result = get_possible_positions("B2")
        assert "B2" in result

    def test_numeric_position(self):
        """Multi-digit numeric positions are handled correctly."""
        result = get_possible_positions("11")
        assert "11" in result

    def test_reversed_position_format(self):
        """Some releases use '1B' instead of 'B1'."""
        result = get_possible_positions("B1")
        assert "1B" in result


# ─── get_wav_regions ─────────────────────────────────────────────────────────

class TestGetWavRegions:
    """get_wav_regions(markers, rate, min_len) builds regions from consecutive markers."""

    RATE = 44100

    def test_basic_two_regions(self):
        # Three markers → two regions
        markers = [0, 44100 * 60, 44100 * 120]  # 0s, 60s, 120s at 44100
        regions = get_wav_regions(markers, self.RATE, min_len=10)
        assert len(regions) == 2

    def test_region_boundaries(self):
        markers = [0, 44100 * 60, 44100 * 120]
        regions = get_wav_regions(markers, self.RATE, min_len=10)
        assert regions[0] == (0, 44100 * 60)
        assert regions[1] == (44100 * 60, 44100 * 120)

    def test_min_len_filters_short_regions(self):
        # 1-second gap → duration 1.0 < min_len 30
        markers = [0, self.RATE, self.RATE + self.RATE * 120]
        regions = get_wav_regions(markers, self.RATE, min_len=30)
        # First region is 1s (too short), second is 120s (ok)
        assert len(regions) == 1
        assert regions[0] == (self.RATE, self.RATE + self.RATE * 120)

    def test_empty_markers_empty_regions(self):
        assert get_wav_regions([], self.RATE, min_len=10) == []

    def test_single_marker_no_regions(self):
        assert get_wav_regions([0], self.RATE, min_len=10) == []

    def test_all_short_returns_empty(self):
        # All gaps are 1 second → below min_len=30
        markers = [i * self.RATE for i in range(5)]
        regions = get_wav_regions(markers, self.RATE, min_len=30)
        assert regions == []

    def test_returns_list_of_tuples(self):
        markers = [0, self.RATE * 60, self.RATE * 120]
        regions = get_wav_regions(markers, self.RATE, min_len=10)
        for r in regions:
            assert isinstance(r, tuple)
            assert len(r) == 2


# ─── get_wav_regions_from_markers ─────────────────────────────────────────────

class TestGetWavRegionsFromMarkers:
    """get_wav_regions_from_markers builds regions from ltxt-style marker dicts."""

    RATE = 44100

    def _marker(self, position, length):
        return {"position": position, "length": length}

    def test_basic_region(self):
        markers = [self._marker(0, self.RATE * 60)]
        regions = get_wav_regions_from_markers(markers, self.RATE * 120, self.RATE, min_len=10)
        assert len(regions) == 1
        assert regions[0] == (0, self.RATE * 60)

    def test_multiple_regions(self):
        markers = [
            self._marker(0, self.RATE * 60),
            self._marker(self.RATE * 60, self.RATE * 90),
        ]
        regions = get_wav_regions_from_markers(markers, self.RATE * 200, self.RATE, min_len=10)
        assert len(regions) == 2

    def test_min_len_filtering(self):
        markers = [
            self._marker(0, self.RATE * 5),    # 5s — too short
            self._marker(self.RATE * 5, self.RATE * 60),  # 60s — ok
        ]
        regions = get_wav_regions_from_markers(markers, self.RATE * 70, self.RATE, min_len=30)
        assert len(regions) == 1

    def test_zero_length_marker_skipped(self):
        markers = [{"position": 0, "length": 0}]
        regions = get_wav_regions_from_markers(markers, self.RATE * 60, self.RATE, min_len=10)
        assert regions == []

    def test_missing_length_key_skipped(self):
        """Markers without a 'length' key have implicit length 0 and are skipped."""
        markers = [{"position": 0}]  # no 'length' key
        regions = get_wav_regions_from_markers(markers, self.RATE * 60, self.RATE, min_len=10)
        assert regions == []

    def test_empty_markers_returns_empty(self):
        regions = get_wav_regions_from_markers([], self.RATE * 60, self.RATE, min_len=10)
        assert regions == []

    def test_region_end_is_start_plus_length(self):
        start  = self.RATE * 10
        length = self.RATE * 50
        markers = [self._marker(start, length)]
        regions = get_wav_regions_from_markers(markers, self.RATE * 200, self.RATE, min_len=10)
        assert regions[0] == (start, start + length)


# ─── normalize_loudnorm ───────────────────────────────────────────────────────

class TestNormalizeLoudnorm:
    """Tests for the two-pass EBU R128 normalize_loudnorm() function.

    All subprocess calls are mocked so no real ffmpeg is required.
    """

    def _pass1_result(self, normalization_type: str = "linear") -> MagicMock:
        """Return a fake subprocess.run result for loudnorm pass 1."""
        stats = {
            "input_i":            "-20.00",
            "input_tp":           "-10.00",
            "input_lra":          "5.00",
            "input_thresh":       "-30.00",
            "output_i":           "-14.00",
            "output_tp":          "-1.00",
            "output_lra":         "5.00",
            "output_thresh":      "-24.00",
            "normalization_type": normalization_type,
            "target_offset":      "-0.50",
        }
        result = MagicMock()
        result.returncode = 0
        result.stderr = f"[Parsed_loudnorm_0 @ 0x0]\n{json.dumps(stats)}\n"
        return result

    def test_two_pass_happy_path(self):
        """Pass 1 returns linear stats; pass 2 ffmpeg is called with linear=true."""
        with patch("subprocess.run", return_value=self._pass1_result("linear")), \
             patch("subprocess.call", return_value=0) as mock_call:
            normalize_loudnorm("/tmp/input.wav", "/tmp/output.aiff")

        # Verify pass 2 included linear=true in the loudnorm filter argument
        call_args = mock_call.call_args[0][0]
        assert any("linear=true" in arg for arg in call_args)

    def test_dynamic_fallback_to_peak(self):
        """When normalization_type=dynamic, normalize_legacy is called instead."""
        dt_process.worker_config["format"] = "alac"  # ensure FORMAT_CONFIG lookup succeeds
        with patch("subprocess.run", return_value=self._pass1_result("dynamic")), \
             patch.object(dt_process, "normalize_legacy") as mock_legacy, \
             patch("subprocess.call", return_value=0):
            normalize_loudnorm("/tmp/input.wav", "/tmp/output.aiff")

        mock_legacy.assert_called_once_with("/tmp/input.wav")

    def test_pass2_failure_raises(self):
        """Non-zero returncode from pass 2 (encoding) raises ConversionException."""
        dt_process.worker_config["format"] = "alac"
        with patch("subprocess.run", return_value=self._pass1_result("linear")), \
             patch("subprocess.call", return_value=1):
            with pytest.raises(ConversionException, match="Loudnorm normalization failed"):
                normalize_loudnorm("/tmp/input.wav", "/tmp/output.aiff")

    def test_pass1_failure_raises(self):
        """Non-zero returncode from pass 1 raises ConversionException."""
        result = MagicMock()
        result.returncode = 1
        result.stderr = ""
        with patch("subprocess.run", return_value=result):
            with pytest.raises(ConversionException, match="pass 1 failed"):
                normalize_loudnorm("/tmp/input.wav", "/tmp/output.aiff")

    def test_no_loudnorm_marker_raises(self):
        """Missing [Parsed_loudnorm marker in pass 1 output raises ConversionException."""
        result = MagicMock()
        result.returncode = 0
        result.stderr = "Some output without the marker\n{}\n"
        with patch("subprocess.run", return_value=result):
            with pytest.raises(ConversionException, match="loudnorm stats not found"):
                normalize_loudnorm("/tmp/input.wav", "/tmp/output.aiff")

    def test_malformed_json_raises(self):
        """[Parsed_loudnorm present but broken JSON raises ConversionException."""
        result = MagicMock()
        result.returncode = 0
        result.stderr = "[Parsed_loudnorm_0 @ 0x0]\n{not valid json\n"
        with patch("subprocess.run", return_value=result):
            with pytest.raises(ConversionException, match="Failed to parse loudnorm"):
                normalize_loudnorm("/tmp/input.wav", "/tmp/output.aiff")
