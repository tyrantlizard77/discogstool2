"""Tests for wavfile.py — enhanced WAV read/write with cue markers and loops.

Covers:
  - Basic write + read round-trip (16-bit mono and stereo)
  - Bit depth: 16-bit and 24-bit data integrity
  - Sample rate round-trip
  - Cue marker round-trip (write markers, read them back)
  - Loop round-trip (write loops, read them back)
  - normalized flag: float → int16 and back
  - forcestereo: mono input produces 2-channel output
"""

from __future__ import annotations

import os
import tempfile

import numpy
import pytest

import wavfile


# ─── Helper ───────────────────────────────────────────────────────────────────

def _write_read(data, rate=44100, **write_kwargs):
    """Write data to a temp WAV file, read it back, return (rate, read_data, bits)."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        path = f.name
    try:
        wavfile.write(path, rate, data, **write_kwargs)
        return wavfile.read(path)
    finally:
        os.unlink(path)


# ─── Basic round-trip ─────────────────────────────────────────────────────────

class TestBasicRoundTrip:
    def test_mono_16bit_data_preserved(self):
        data = numpy.array([0, 100, -100, 32767, -32768], dtype=numpy.int16)
        rate_out, data_out, bits = _write_read(data)
        numpy.testing.assert_array_equal(data_out, data)

    def test_stereo_16bit_data_preserved(self):
        data = numpy.array([[0, 100], [200, -200], [32767, -32768]], dtype=numpy.int16)
        rate_out, data_out, bits = _write_read(data)
        numpy.testing.assert_array_equal(data_out, data)

    def test_rate_preserved_44100(self):
        data = numpy.zeros(100, dtype=numpy.int16)
        rate_out, _, _ = _write_read(data, rate=44100)
        assert rate_out == 44100

    def test_rate_preserved_48000(self):
        data = numpy.zeros(100, dtype=numpy.int16)
        rate_out, _, _ = _write_read(data, rate=48000)
        assert rate_out == 48000

    def test_returns_bits_16(self):
        data = numpy.zeros(100, dtype=numpy.int16)
        _, _, bits = _write_read(data)
        assert bits == 16

    def test_mono_shape_preserved(self):
        data = numpy.zeros(100, dtype=numpy.int16)
        _, data_out, _ = _write_read(data)
        assert data_out.ndim == 1
        assert len(data_out) == 100

    def test_stereo_shape_preserved(self):
        data = numpy.zeros((100, 2), dtype=numpy.int16)
        _, data_out, _ = _write_read(data)
        assert data_out.ndim == 2
        assert data_out.shape == (100, 2)

    def test_all_zeros(self):
        data = numpy.zeros(256, dtype=numpy.int16)
        _, data_out, _ = _write_read(data)
        numpy.testing.assert_array_equal(data_out, data)

    def test_large_buffer(self):
        data = numpy.arange(44100, dtype=numpy.int16)  # 1 second of audio
        _, data_out, _ = _write_read(data)
        numpy.testing.assert_array_equal(data_out, data)


# ─── Bit depth ────────────────────────────────────────────────────────────────

class TestBitDepth:
    def test_24bit_roundtrip(self):
        """24-bit samples are stored and recovered with correct bit depth."""
        data = numpy.array([0, 100, -100, 8388607, -8388608], dtype=numpy.int32)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        try:
            wavfile.write(path, 44100, data, bitrate=24)
            rate, data_out, bits = wavfile.read(path)
        finally:
            os.unlink(path)
        assert bits == 24
        # 24-bit data is stored as 3 bytes; values should round-trip
        numpy.testing.assert_array_equal(data_out, data)


# ─── Cue markers ──────────────────────────────────────────────────────────────

class TestCueMarkers:
    def _roundtrip(self, data, markers):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        try:
            wavfile.write(path, 44100, data, markers=markers)
            return wavfile.read(path, readmarkers=True)
        finally:
            os.unlink(path)

    def test_single_marker_roundtrip(self):
        data = numpy.zeros(44100, dtype=numpy.int16)
        rate, data_out, bits, markers_out = self._roundtrip(data, [1000])
        assert 1000 in markers_out

    def test_multiple_markers_roundtrip(self):
        data = numpy.zeros(44100 * 3, dtype=numpy.int16)
        positions = [0, 44100, 88200]
        rate, data_out, bits, markers_out = self._roundtrip(data, positions)
        for pos in positions:
            assert pos in markers_out

    def test_no_markers_returns_empty(self):
        data = numpy.zeros(100, dtype=numpy.int16)
        rate, data_out, bits, markers_out = self._roundtrip(data, None)
        assert markers_out == []

    def test_markers_sorted(self):
        data = numpy.zeros(44100 * 5, dtype=numpy.int16)
        positions = [88200, 0, 44100]  # unsorted
        rate, data_out, bits, markers_out = self._roundtrip(data, positions)
        assert markers_out == sorted(positions)


# ─── Loops ────────────────────────────────────────────────────────────────────

class TestLoops:
    def _roundtrip(self, data, loops):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        try:
            wavfile.write(path, 44100, data, loops=loops)
            return wavfile.read(path, readloops=True)
        finally:
            os.unlink(path)

    def test_single_loop_roundtrip(self):
        data = numpy.zeros(44100 * 5, dtype=numpy.int16)
        loops_in = [[0, 44100]]
        rate, data_out, bits, loops_out = self._roundtrip(data, loops_in)
        assert [0, 44100] in loops_out

    def test_multiple_loops_roundtrip(self):
        data = numpy.zeros(44100 * 10, dtype=numpy.int16)
        loops_in = [[0, 44100], [88200, 132300]]
        rate, data_out, bits, loops_out = self._roundtrip(data, loops_in)
        assert len(loops_out) == 2


# ─── forcestereo ──────────────────────────────────────────────────────────────

class TestForceStereo:
    def test_mono_becomes_stereo(self):
        data = numpy.array([0, 100, -100, 32767], dtype=numpy.int16)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        try:
            wavfile.write(path, 44100, data)
            rate, data_out, bits = wavfile.read(path, forcestereo=True)
        finally:
            os.unlink(path)
        assert data_out.ndim == 2
        assert data_out.shape[1] == 2

    def test_stereo_stays_stereo(self):
        data = numpy.zeros((100, 2), dtype=numpy.int16)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        try:
            wavfile.write(path, 44100, data)
            rate, data_out, bits = wavfile.read(path, forcestereo=True)
        finally:
            os.unlink(path)
        assert data_out.shape[1] == 2


# ─── Malformed / truncated files ──────────────────────────────────────────────

class TestMalformedFiles:
    def test_not_a_wav_file(self):
        """Reading random bytes raises ValueError (not a WAV file)."""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(b"not a wav file at all")
            path = f.name
        try:
            with pytest.raises(ValueError, match="Not a WAV file"):
                wavfile.read(path)
        finally:
            os.unlink(path)

    def test_truncated_after_riff_header(self):
        """A file truncated right after the RIFF header returns empty data (no crash)."""
        import struct
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            # Write a valid RIFF/WAVE header (12 bytes) but nothing after
            f.write(b"RIFF")
            f.write(struct.pack("<I", 100))  # claimed size
            f.write(b"WAVE")
            path = f.name
        try:
            # Should complete without crash (returns empty data — no fmt/data chunks)
            rate, data, bits = wavfile.read(path)
            assert len(data) == 0
        finally:
            os.unlink(path)

    def test_truncated_cue_chunk_raises(self):
        """A cue chunk that is too short raises ValueError."""
        import struct
        import numpy

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name

        try:
            # Write a valid WAV first
            data = numpy.zeros(100, dtype=numpy.int16)
            wavfile.write(path, 44100, data)

            with open(path, "rb") as f:
                original = f.read()

            # Append a cue chunk that claims 2 cues but has no data
            patched = original + b"cue " + struct.pack("<II", 52, 2)  # says 2 cues, no data
            # Adjust the RIFF size
            new_size = len(patched) - 8
            patched = patched[:4] + struct.pack("<I", new_size) + patched[8:]

            with open(path, "wb") as f:
                f.write(patched)

            with pytest.raises((ValueError, struct.error)):
                wavfile.read(path)
        finally:
            os.unlink(path)
