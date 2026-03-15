# wavfile.py (Enhanced)
# Date: 20190213_2328 Joseph Ernest
#
# URL: https://gist.github.com/josephernest/3f22c5ed5dabf1815f16efa8fa53d476
# Source: scipy/io/wavfile.py
#
# Added:
# * read: also returns bitrate, cue markers + cue marker labels (sorted), loops, pitch
#         See https://web.archive.org/web/20141226210234/http://www.sonicspot.com/guide/wavefiles.html#labl
# * read: 24 bit & 32 bit IEEE files support (inspired from wavio_weckesser.py from Warren Weckesser)
# * read: added normalized (default False) that returns everything as float in [-1, 1]
# * read: added forcestereo that returns a 2-dimensional array even if input is mono
#
# * write: can write cue markers, cue marker labels, loops, pitch
# * write: 24 bit support
# * write: can write from a float normalized in [-1, 1]
# * write: 20180430_2335: bug fixed when size of data chunk is odd (previously, metadata could become unreadable because of this)
#
# * removed RIFX support (big-endian) (never seen one in 10+ years of audio production/audio programming), only RIFF (little-endian) are supported
# * removed read(..., mmap)
#
#
# Test:
# ..\wav\____wavfile_demo.py


"""
Module to read / write wav files using numpy arrays

Functions
---------
`read`: Return the sample rate (in samples/sec) and data from a WAV file.

`write`: Write a numpy array as a WAV file.

"""
from __future__ import annotations, division, print_function, absolute_import

import numpy
import numpy.typing as npt
import struct
import sys
import warnings
import collections
from operator import itemgetter
from typing import IO, TypedDict, Sequence

class WavFileWarning(UserWarning):
    pass


class WavMarker(TypedDict):
    position: int
    label: bytes
    length: int


# Type alias for possible wav data formats
WavData = (
    npt.NDArray[numpy.int16]
    | npt.NDArray[numpy.int32]
    | npt.NDArray[numpy.uint8]
    | npt.NDArray[numpy.float32]
)

_ieee = False

def _read_fmt_chunk(fid: IO[bytes]) -> tuple[int, int, int, int, int, int, int]:
    """Parse the fmt chunk body (file pointer must be immediately after the 'fmt ' id).

    Returns (size, comp, noc, rate, sbytes, ba, bits) where:
      size   — chunk data size in bytes (16 for PCM, >16 for extended)
      comp   — compression type: 1 = PCM, 3 = IEEE float
      noc    — number of channels
      rate   — sample rate in Hz
      sbytes — average bytes per second
      ba     — block alignment (bytes per sample frame)
      bits   — bits per sample

    Sets the module-level _ieee flag when comp==3 (IEEE float format) so that
    _read_data_chunk() knows to use float32 dtype instead of integer.  Extended
    fmt chunks (size > 16) have their extra bytes consumed here.
    """
    raw = fid.read(20)
    if len(raw) != 20:
        raise ValueError(f"Truncated WAV file: fmt chunk too short ({len(raw)} bytes)")
    res = struct.unpack("<IhHIIHH", raw)
    size, comp, noc, rate, sbytes, ba, bits = res
    if comp != 1 or size > 16:
        if comp == 3:
            global _ieee
            _ieee = True
            # warnings.warn("IEEE format not supported", WavFileWarning)
        else:
            warnings.warn("Unfamiliar format bytes", WavFileWarning)
        if size > 16:
            fid.read(size - 16)
    return size, comp, noc, rate, sbytes, ba, bits


def _read_data_chunk(fid: IO[bytes], noc: int, bits: int, normalized: bool = False) -> WavData:
    """Parse the data chunk body (file pointer must be immediately after the 'data' id).

    Handles 8, 16, 24, and 32-bit PCM as well as 32-bit IEEE float (when the
    module-level _ieee flag is set by _read_fmt_chunk).

    24-bit samples have no native numpy dtype, so they are read as packed uint8
    triplets and then sign-extended to int32 by bit-shifting the high byte.

    If the data chunk size is odd (which can happen with mono 8-bit recordings),
    the file pointer is advanced by one extra byte to maintain word alignment for
    any subsequent chunks.

    When normalized=True, samples are scaled to the range [-1, 1] as float32
    using 2^(bits-1) as the normalisation factor.
    """
    size = struct.unpack("<I", fid.read(4))[0]

    if bits == 8 or bits == 24:
        dtype = "u1"
        num_bytes = 1
    else:
        num_bytes = bits // 8
        dtype = "<i%d" % num_bytes

    if bits == 32 and _ieee:
        dtype = "float32"
    # print("size bytes", size, num_bytes)
    data = numpy.fromfile(fid, dtype=dtype, count=size // num_bytes)

    if bits == 24:
        a = numpy.empty((len(data) // 3, 4), dtype="u1")
        a[:, :3] = data.reshape((-1, 3))
        a[:, 3:] = (a[:, 3 - 1 : 3] >> 7) * 255
        data = a.view("<i4").reshape(a.shape[:-1])

    if noc > 1:
        data = data.reshape(-1, noc)

    if bool(size & 1):  # if odd number of bytes, move 1 byte further (data chunk is word-aligned)
        fid.seek(1, 1)

    if normalized:
        normfactor = 2 ** (bits - 1)  # works for 8, 16, 24 and gracefully for 32-float
        data = numpy.float32(data) * 1.0 / normfactor

    return data  # type: ignore[return-value]

def _skip_unknown_chunk(fid: IO[bytes]) -> None:
    """Read and discard an unrecognised chunk, respecting word-alignment padding."""
    data = fid.read(4)
    size = struct.unpack("<I", data)[0]
    if bool(size & 1):  # WAV chunks are always word-aligned; odd sizes have 1 padding byte
        size += 1
    fid.seek(size, 1)


def _read_riff_chunk(fid: IO[bytes]) -> int:
    """Validate the RIFF/WAVE file header and return the total file size in bytes.

    The RIFF header is 12 bytes: 'RIFF' (4) + file-size-minus-8 (4) + 'WAVE' (4).
    We return size+8 so that the read loop can use fid.tell() < fsize as its
    termination condition without needing to subtract the header offset.
    """
    str1 = fid.read(4)
    if str1 != b"RIFF":
        raise ValueError("Not a WAV file.")
    fsize = struct.unpack("<I", fid.read(4))[0] + 8
    str2 = fid.read(4)
    if str2 != b"WAVE":
        raise ValueError("Not a WAV file.")
    return fsize


def read(
    file: str | IO[bytes],
    readmarkers: bool = False,
    readmarkerlabels: bool = False,
    readmarkerslist: bool = False,
    readloops: bool = False,
    readpitch: bool = False,
    normalized: bool = False,
    forcestereo: bool = False,
) -> tuple[int, WavData, int] | tuple[int, WavData, int, list[int]] | tuple[int, WavData, int, list[WavMarker]] | tuple[int, WavData, int, list[list[int]]] | tuple[int, WavData, int, list[int], list[WavMarker], list[list[int]]]:
    """
    Return the sample rate (in samples/sec) and data from a WAV file

    Parameters
    ----------
    file : file
        Input wav file.

    Returns
    -------
    rate : int
        Sample rate of wav file
    data : numpy array
        Data read from wav file

    Notes
    -----

    * The file can be an open file or a filename.

    * The returned sample rate is a Python integer
    * The data is returned as a numpy array with a
      data-type determined from the file.

    """
    def _read_bytes(fid: IO[bytes], n: int) -> bytes:
        """Read exactly n bytes, raising ValueError on truncated files."""
        data = fid.read(n)
        if len(data) != n:
            raise ValueError(f"Truncated WAV file: expected {n} bytes, got {len(data)}")
        return data

    fid: IO[bytes]
    opened = isinstance(file, str)
    if opened:
        fid = open(file, "rb")
    else:
        fid = file

    try:
        fsize = _read_riff_chunk(fid)
        noc = 1
        bits = 8
        # _cue = []
        # _cuelabels = []
        _markersdict: collections.defaultdict[int, dict[str, int | bytes]] = collections.defaultdict(
            lambda: {"position": -1, "label": b"", "length": 0}
        )
        loops: list[list[int]] = []
        pitch: float = 0.0
        data: WavData = numpy.empty(0, dtype=numpy.int16)
        rate: int = 0
        while fid.tell() < fsize:
            # read the next chunk
            chunk_id = fid.read(4)
            if len(chunk_id) < 4:
                break  # end of file mid-chunk; stop gracefully
            if chunk_id == b"fmt ":
                size, comp, noc, rate, sbytes, ba, bits = _read_fmt_chunk(fid)
            elif chunk_id == b"data":
                data = _read_data_chunk(fid, noc, bits, normalized)
            elif chunk_id == b"cue ":
                str1 = _read_bytes(fid, 8)
                size, numcue = struct.unpack("<ii", str1)
                for c in range(numcue):
                    str1 = _read_bytes(fid, 24)
                    id, position, datachunkid, chunkstart, blockstart, sampleoffset = struct.unpack(
                        "<iiiiii", str1
                    )
                    # _cue.append(position)
                    _markersdict[id]["position"] = position  # needed to match labels and markers

            elif chunk_id == b"LIST":
                str1 = _read_bytes(fid, 8)
                size, type_ = struct.unpack("<ii", str1)
            elif chunk_id in [b"ICRD", b"IENG", b"ISFT", b"ISTJ"]:  # see http://www.pjb.com.au/midi/sfspec21.html#i5
                _skip_unknown_chunk(fid)
            elif chunk_id == b"labl":
                str1 = _read_bytes(fid, 8)
                size, id = struct.unpack("<Ii", str1)
                size = size + (size % 2)  # the size should be even, see WAV specfication, e.g. 16=>16, 23=>24
                label = fid.read(size - 4).rstrip(b"\x00")  # remove the trailing null characters
                # _cuelabels.append(label)
                _markersdict[id]["label"] = label  # needed to match labels and markers

            elif chunk_id == b"ltxt":
                str1 = _read_bytes(fid, 4)
                size = struct.unpack("<I", str1)[0]
                id = 0
                if size >= 4:
                    id = struct.unpack("<I", _read_bytes(fid, 4))[0]
                if size >= 8:
                    sample_length = struct.unpack("<I", _read_bytes(fid, 4))[0]
                    _markersdict[id]["length"] = sample_length  # region length in samples
                remaining = size - 8
                if remaining > 0:
                    remaining = remaining + (remaining % 2)  # word-aligned
                    fid.read(remaining)  # skip purpose, country, language, etc.

            elif chunk_id == b"smpl":
                str1 = _read_bytes(fid, 40)
                (
                    size,
                    manuf,
                    prod,
                    sampleperiod,
                    midiunitynote,
                    midipitchfraction,
                    smptefmt,
                    smpteoffs,
                    numsampleloops,
                    samplerdata,
                ) = struct.unpack("<iiiiiIiiii", str1)
                cents = midipitchfraction * 1.0 / (2**32 - 1)
                pitch = 440.0 * 2 ** ((midiunitynote + cents - 69.0) / 12)
                for i in range(numsampleloops):
                    str1 = _read_bytes(fid, 24)
                    cuepointid, type_, start, end, fraction, playcount = struct.unpack(
                        "<iiiiii", str1
                    )
                    loops.append([start, end])
            else:
                _skip_unknown_chunk(fid)
    finally:
        if opened:
            fid.close()

    if data.ndim == 1 and forcestereo:
        data = numpy.column_stack((data, data))  # type: ignore[assignment]

    _markerslist: list[WavMarker] = sorted(
        [_markersdict[l] for l in _markersdict], key=lambda k: k["position"]  # type: ignore[arg-type,return-value]
    )  # sort by position
    _cue = [m["position"] for m in _markerslist]
    _cuelabels = [m["label"] for m in _markerslist]

    if readmarkers and readmarkerslist and readloops:
        return (rate, data, bits, _cue, _markerslist, loops)
    if readmarkers:
        return (rate, data, bits, _cue)
    if readmarkerslist:
        return (rate, data, bits, _markerslist)
    if readloops:
        return (rate, data, bits, loops)
    return (rate, data, bits)



def write(
    filename: str,
    rate: int,
    data: npt.NDArray[numpy.int16 | numpy.int32 | numpy.uint8 | numpy.float32 | numpy.float64],
    bitrate: int | None = None,
    markers: Sequence[dict[str, bytes | int]] | Sequence[int] | None = None,
    loops: Sequence[Sequence[int]] | None = None,
    pitch: float | None = None,
    normalized: bool = False,
) -> None:
    """
    Write a numpy array as a WAV file

    Parameters
    ----------
    filename : file
        The name of the file to write (will be over-written).
    rate : int
        The sample rate (in samples/sec).
    data : ndarray
        A 1-D or 2-D numpy array of integer data-type.

    Notes
    -----
    * Writes a simple uncompressed WAV file.
    * The bits-per-sample will be determined by the data-type.
    * To write multiple-channels, use a 2-D array of shape
      (Nsamples, Nchannels).

    """

    # normalization and 24-bit handling
    if bitrate == 24:  # special handling of 24 bit wav, because there is no numpy.int24...
        if normalized:
            data[data > 1.0] = 1.0
            data[data < -1.0] = -1.0
            a32 = numpy.asarray(data * (2 ** 23 - 1), dtype=numpy.int32)
        else:
            a32 = numpy.array(data, dtype=numpy.int32)  # copy, not view — avoids mutating caller's array
        if a32.ndim == 1:
            a32 = a32.reshape(a32.shape + (1,))  # Convert to a 2D array with a single column.
        a8 = (a32.reshape(a32.shape + (1,)) >> numpy.array([0, 8, 16])) & 255  # By shifting first 0 bits, then 8, then 16, the resulting output is 24 bit little-endian.
        data = a8.astype(numpy.uint8)
    else:
        if normalized:  # default to 32 bit int
            data[data > 1.0] = 1.0
            data[data < -1.0] = -1.0
            data = numpy.asarray(data * (2 ** 31 - 1), dtype=numpy.int32)

    with open(filename, "wb") as fid:
        fid.write(b"RIFF")
        fid.write(b"\x00\x00\x00\x00")
        fid.write(b"WAVE")

        # fmt chunk
        fid.write(b"fmt ")
        if data.ndim == 1:
            noc = 1
        else:
            noc = data.shape[1]
        bits = data.dtype.itemsize * 8 if bitrate != 24 else 24
        sbytes = rate * (bits // 8) * noc
        ba = noc * (bits // 8)
        fid.write(struct.pack("<ihHIIHH", 16, 1, noc, rate, sbytes, ba, bits))

        # cue chunk
        if markers:  # != None and != []
            labels: list[bytes] = []
            marker_positions: list[int] = []
            if isinstance(markers[0], dict):  # then we have [{'position': 100, 'label': 'marker1'}, ...]
                for m in markers:
                    m_dict = m  # type: ignore[union-attr]
                    labels.append(m_dict["label"])  # type: ignore[index]
                    marker_positions.append(m_dict["position"])  # type: ignore[index]
            else:
                marker_positions = list(markers)  # type: ignore[arg-type]
                labels = [b"" for m in markers]

            fid.write(b"cue ")
            size = 4 + len(marker_positions) * 24
            fid.write(struct.pack("<II", size, len(marker_positions)))
            for i, c in enumerate(marker_positions):
                s = struct.pack(
                    "<iiiiii", i + 1, c, 1635017060, 0, 0, c
                )  # 1635017060 is struct.unpack('<i',b'data')
                fid.write(s)

            lbls = b""
            for i, lbl in enumerate(labels):
                lbls += b"labl"
                label = lbl + (b"\x00" if len(lbl) % 2 == 1 else b"\x00\x00")
                size_label = len(lbl) + 1 + 4  # because \x00
                lbls += struct.pack("<II", size_label, i + 1)
                lbls += label

            fid.write(b"LIST")
            size = len(lbls) + 4
            fid.write(struct.pack("<I", size))
            fid.write(
                b"adtl"
            )  # https://web.archive.org/web/20141226210234/http://www.sonicspot.com/guide/wavefiles.html#list
            fid.write(lbls)

        # smpl chunk
        if loops or pitch:
            loops_to_write: Sequence[Sequence[int]] = loops if loops else []
            if pitch:
                midiunitynote = 12 * numpy.log2(pitch * 1.0 / 440.0) + 69
                midipitchfraction = int((midiunitynote - int(midiunitynote)) * (2**32 - 1))
                midiunitynote = int(midiunitynote)
                # print(midipitchfraction, midiunitynote)
            else:
                midiunitynote = 0
                midipitchfraction = 0
            fid.write(b"smpl")
            size = 36 + len(loops_to_write) * 24
            sampleperiod = int(1000000000.0 / rate)

            fid.write(
                struct.pack(
                    "<IiiiiIiiii",
                    size,
                    0,
                    0,
                    sampleperiod,
                    midiunitynote,
                    midipitchfraction,
                    0,
                    0,
                    len(loops_to_write),
                    0,
                )
            )
            for i, loop in enumerate(loops_to_write):
                fid.write(struct.pack("<iiiiii", 0, 0, loop[0], loop[1], 0, 0))

        # data chunks
        fid.write(b"data")
        fid.write(struct.pack("<I", data.nbytes))

        if data.dtype.byteorder == ">" or (
            data.dtype.byteorder == "=" and sys.byteorder == "big"
        ):
            data = data.byteswap()

        data.tofile(fid)

        if (
            data.nbytes % 2 == 1
        ):  # add an extra padding byte if data.nbytes is odd: https://web.archive.org/web/20141226210234/http://www.sonicspot.com/guide/wavefiles.html#data
            fid.write(b"\x00")

        # Determine file size and place it in correct
        #  position at start of the file.
        size = fid.tell()
        fid.seek(4)
        fid.write(struct.pack("<I", size - 8))
