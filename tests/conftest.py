"""Pytest configuration for discogstool2 tests.

Adds the parent directory (discogstool2) to sys.path so that ``import beatport``,
``import util``, etc. all resolve correctly regardless of where pytest is invoked.

Also points HOME at a throwaway directory for the whole session, so nothing the
suite does can reach the real ~/.discogstool.
"""

import atexit
import os
import pwd
import shutil
import sys
import tempfile

import pytest

# ── Isolate the whole session from the real ~/.discogstool ────────────────────
# Done at import, before any test module loads.  util.datadir() resolves "~"
# per call, so this redirects every config, cache and log path the code uses.
# Without it, the first BeatportMatcher() a test built attached a file handler
# to the real ~/.discogstool/beatport.log, and every run appended lines like
# "No Beatport match found for Discogs release 12345 (Test Artist - Test EP)"
# to the log you read to debug real matches.
REAL_HOME = pwd.getpwuid(os.getuid()).pw_dir
TEST_HOME = tempfile.mkdtemp(prefix="discogstool-test-home-")
os.environ["HOME"] = TEST_HOME
atexit.register(shutil.rmtree, TEST_HOME, ignore_errors=True)

# Ensure the package root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: live network tests requiring real API credentials "
        "(run with: pytest -m integration)",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip integration tests unless -m integration (or similar) is specified."""
    if config.option.markexpr and "integration" in config.option.markexpr:
        return
    skip_integration = pytest.mark.skip(reason="use -m integration to run live tests")
    for item in items:
        if item.get_closest_marker("integration"):
            item.add_marker(skip_integration)


# ─── Shared release fixtures ──────────────────────────────────────────────────

def _cheap_and_deep_data() -> dict:
    """Discogs r3854655 (Cheap And Deep – Cheap And Deep Rides Again),
    trimmed to the fields label rendering reads.

    Captured verbatim from the API because it exercises every place Discogs
    keeps credits: per-track `extraartists` (the two remixes, which share a
    title and are indistinguishable without them), release credits scoped by
    `tracks` ("A1, B1"), and unscoped release credits — packing/pressing roles
    that must not print, and a Producer credit naming the release's own artist.
    """
    return {
        "title": "Cheap And Deep Rides Again",
        "year": 2012,
        "country": "US",
        "artists": [{"name": "Cheap And Deep Productions", "anv": "Cheap And Deep",
                     "id": 1563065}],
        "labels": [{"name": "Modular Cowboy", "catno": "sand001"}],
        "styles": ["House", "Techno", "Acid"],
        "genres": ["Electronic"],
        "extraartists": [
            {"name": "Maike Verloh", "anv": "", "role": "Artwork, Design",
             "tracks": "", "id": 2922892},
            {"name": "Mark Richardson (3)", "anv": "MJR", "role": "Lacquer Cut By",
             "tracks": "", "id": 471429},
            {"name": "Cheap And Deep Productions", "anv": "Cheap And Deep",
             "role": "Producer", "tracks": "", "id": 1563065},
            {"name": "Jay Patrick Ahern", "anv": "Jay Ahern", "role": "Producer",
             "tracks": "", "id": 446855},
            {"name": "Goran Simonoski", "anv": "",
             "role": "Producer [Additional Production]", "tracks": "A1, B1",
             "id": 851471},
            {"name": "Uta Alder", "anv": "", "role": "Vocals", "tracks": "A1, B1",
             "id": 1563064},
        ],
        "tracklist": [
            {"position": "A1", "type_": "track", "duration": "",
             "title": "Words, Breaths & Pauses Remix",
             "extraartists": [{"name": "Norman Nodge", "anv": "", "role": "Remix",
                               "tracks": "", "id": 845344}]},
            {"position": "B1", "type_": "track", "duration": "",
             "title": "Words, Breaths & Pauses Remix",
             "extraartists": [{"name": "Jonsson/Alter", "anv": "", "role": "Remix",
                               "tracks": "", "id": 2953668}]},
            {"position": "B2", "type_": "track", "duration": "",
             "title": "Beautiful",
             "extraartists": [
                 {"name": "Morgan Packard", "anv": "",
                  "role": "Producer [Additional Production]", "tracks": "",
                  "id": 223854},
                 {"name": "Crissy Liu", "anv": "", "role": "Vocals", "tracks": "",
                  "id": 2911973}]},
        ],
    }


def make_release(data: dict):
    """A real client_interface.DiscogsRelease over *data*, with no API call.

    Mirrors DiscogsRelease.__init__ (which would fetch) so tests exercise the
    genuine accessors — getCredits(), resolveTrackScope() — rather than mocks
    of them.  No master_id, so getLabelYear() never reaches the network.
    """
    import client_interface
    rel = client_interface.DiscogsRelease.__new__(client_interface.DiscogsRelease)
    rel.rid = data.get("id", 3854655)
    rel.data = data
    rel.data["tracklist"] = [t for t in data["tracklist"]
                             if t["type_"] == "track" and t["title"] != ""]
    rel.totaltracks = len(rel.data["tracklist"])
    rel.imgdata = None
    return rel


@pytest.fixture
def cheap_and_deep():
    """r3854655 as a real DiscogsRelease — see _cheap_and_deep_data()."""
    return make_release(_cheap_and_deep_data())
