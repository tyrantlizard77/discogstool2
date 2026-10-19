# discogstool2 — CLAUDE.md

Reference guide for AI-assisted development on this codebase.

---

## Project Purpose

A Python toolkit for digitising vinyl records and managing a Discogs collection. The core workflow:

1. Record vinyl sides in Reaper, exporting WAV files with region markers at track boundaries
2. Run `dt_process` to split by region, fetch metadata from Discogs, EBU-R128 normalise, convert to AIFF/ALAC, embed cover art and tags
3. Optionally run `dt_label` (or trigger it via the browser extension + `dt_server`) to print a Brother thermal sleeve label
4. Use `dt_collection` to compare local files against a Discogs collection export and update stale metadata
5. Use `dt_find` to identify an unknown record via voice/text description through an LLM + Discogs search agent loop

---

## Repository Layout

```
dt_server          Flask HTTP bridge (localhost:5679) for the browser extensions
dt_process         Audio processing pipeline (split → normalise → convert → tag)
dt_label           Label renderer + printer
dt_find            LLM-driven record identification (voice or typed)
dt_collection      Collection scanner and metadata update tool

client_interface.py   Discogs API wrapper (OAuth, release/track objects)
beatport.py           Beatport BPM lookup (matching, caching, AnthropicMatcher)
database.py           Discogs response cache (SQLite + pickle)
libtags.py            Audio tag I/O (Mutagen — ID3, MP4, FLAC)
wavfile.py            WAV reader with region/loop support (Reaper smpl chunk)
util.py               Shared helpers (paths, file discovery, collection CSV)

ext/shared/           WebExtension sources shared by both browsers
ext/firefox/          Firefox manifest.json (MV2)
ext/chrome/           Chrome manifest.json (MV3)
build_ext.sh          Assembles firefox-ext/ and chrome-ext/ (both gitignored)
tests/                pytest suite (one file per module)
install_server.sh     macOS launchd plist installer for dt_server
sign_extension.sh     AMO signing for the Firefox build
publish_chrome.sh     Chrome Web Store upload/publish (unlisted)
docs/                 Chrome Web Store setup guide
requirements.txt      Python dependencies
```

All entry-point scripts have no `.py` extension. Tests load them via `importlib.machinery.SourceFileLoader`.

---

## Architecture

### dt_server ↔ browser extension

The extension ships for Firefox and Chrome from one set of sources. Everything except `manifest.json` is shared: `background.js` reconciles the two runtimes itself (`browser` vs `chrome`, `browserAction` vs `action`, `menus` vs `contextMenus`). Run `./build_ext.sh` to assemble `firefox-ext/` and `chrome-ext/`; both are generated and gitignored, so **edit `ext/`, never the build output**. `sign_extension.sh` builds before signing.

Chrome requires MV3, which makes the background script a service worker that Chrome terminates after ~30 s idle — so the 1 s `setInterval` badge poll cannot survive there, and `chrome.alarms` has a 60 s floor. The badge is therefore refreshed at the moments that matter (job queued, popup opened, worker woken) with a 1-minute alarm as a backstop; the popup polls at 500 ms whenever it is open, which is when anyone is actually watching. Firefox keeps the 1 s interval, since an MV2 background page persists.

CORS on dt_server echoes back only `moz-extension://` and `chrome-extension://` origins — never a wildcard, and never an `http(s)` origin, since the server binds to localhost where any visited page could otherwise reach it.

The extension popup calls `http://localhost:5679/status` (health) and `POST /print` (release_id, profile, preview, split, discs). The server delegates to `dt_label` as a subprocess, serves preview PNGs from a temp dir, and reports Discogs/Beatport/Anthropic credential status in the `/status` response. The popup footer displays three connection dots: Discogs, Beatport, and Anthropic. There was a fourth ("Finder") for the dt_find backend; once the local LLM option went away its check was byte-identical to the Anthropic one — the same key serves both the Beatport matcher and dt_find — so two dots could never disagree and one was removed. `tests/test_dt_server.py` pins the `/status` service key set.

**Popup settings are split by lifetime.** The port and the label profile persist in `storage.local`; the three option checkboxes (split by disc, hide BPM, preview only) do not. They start cleared each time the popup opens and `clearOptions()` resets them — along with the discs field — once a real print is enqueued. A preview deliberately does *not* clear them, because previews come in runs and re-ticking the same boxes to re-render the same label is worse than the risk it avoids. This also means `background.js`'s context-menu handler has nothing to inherit: it always sends a plain print (`preview: false, split: false, hide_bpm: false`).

### Job queue and progress reporting

`POST /print` **enqueues** and returns immediately with `{job_id, queued}`; a single worker thread drains the queue so jobs never race at the printer. Preview stays synchronous because its response carries the PNG URLs.

Because printing is async, errors no longer ride back on the `/print` response — they live on the job record. A failed job does **not** halt the queue; it is retained with its error so the UI can show it.

**The popup's status line is derived from `/progress`, never captured from the `POST /print` response.** Writing it at POST time produced a line frozen on "Queued." while the label was already printing. `describeJob()` in popup.js is the single place that decides the text; `holdStatus` is the only override, reserved for terminal messages (preview opened, request failed) that must outlive job state. A finished job clears from the panel after `RECENT_GRACE_S`, so reopening the popup later doesn't look like work is still in flight.

`tests/js/popup_harness.js` stubs `document` plus the extension API, evaluates the real popup.js, and asserts the status text across job states. `tests/test_extensions.py` runs it under **both** the `browser` and `chrome` namespaces, and also pins manifest consistency (versions in step, MV2 vs MV3 keys, no host permissions beyond localhost).

`GET /progress` returns the running job, queued jobs, `depth`, and `failures`. `POST /jobs/clear` drops finished/failed history. The popup polls `/progress` every 500 ms while open; `background.js` polls once per second only while work is outstanding, driving the toolbar badge (queue depth in orange, failure count in red).

**Progress protocol.** `dt_label --progress` writes JSON lines to stdout prefixed with `@@DTP@@ `; `_run_dt_label` streams these via `Popen` and folds them into the job with `_apply_event`. Non-progress output is accumulated and used as the error message if the process exits non-zero. Keeping events on stdout (rather than a side channel) means `dt_label --progress` is directly debuggable from a terminal.

Stages, in display order: `lookup`, `samples`, `bpm`, `render`, `print`. Event states are `start`, `progress` (with `done`/`total`), `done`, `skip`, and `error`. `dt_label.STAGES` and `dt_server.STAGES` must stay in sync.

The `samples` and `bpm` stages come from a callback threaded through `BeatportMatcher.find_bpms(progress=…)` into `_verify_bpms`, which counts only tracks that actually need work — a fully cached release reports nothing and skips the (slow) essentia import entirely.

### Printing

`_send_raster()` opens a TCP connection, writes the raster, and returns. There is **no completion readback**: brother_ql's `helpers.send()` declines to wait on the network backend, and although the backend does expose `_read()`, the QL-1110NWB was probed for 10 s on port 9100 and returned zero bytes. It does not report status over the network. "Sent to printer" is therefore the last observable step, and the stage list ends there.

An earlier revision implemented full status-packet readback (`_send_and_await`, `_status_frames`, `probe_printer`, `--probe-printer`). It is in git history if a future printer needs re-evaluating. Two traps it documented are worth remembering before reviving it:

- `interpret_response` raises **`NameError`** — not `ValueError` — for a short buffer or bad header. brother_ql's own handler catches `ValueError`, so it is dead code; an uncaught `NameError` killed the print job *after* the label had printed.
- Status packets are exactly 32 bytes, but TCP may split one across reads or coalesce several. Framing must buffer rather than assume one `recv()` is one packet.

**Timeouts.** `_SEND_TIMEOUT` (default 300 s) is not a network-latency budget — it is *how long the printer may take to accept a whole label*. A QL printer buffers raster and drains it as it physically prints, so TCP backpressure keeps the write blocked for roughly as long as printing takes. brother_ql's network backend hardcodes 10 s in its own `_write()`, which is shorter than a long continuous label, so a queued batch would time out partway through. That is why `_send_raster()` drives the socket directly instead of going through `BrotherQLBackendNetwork` — the backend exists only to wrap connect+sendall and offers no way to override that 10 s. Both `connect_timeout` and `send_timeout` are overridable in `label_config`.

A completed `sendall` also paces the queue: it returns only once the printer has taken the whole label. `dt_server` additionally waits `_INTER_JOB_SETTLE_S` between print jobs, because acceptance still isn't the same as the paper having stopped.

**Retry and recovery.** A `sendall()` timeout leaves the printer holding a half-received job; anything sent next — the following queued label, or a manual reprint — is read as the missing raster and printed as static. So `print_label` never retries a mid-transfer failure (see `_partially_written`), and instead sends `_RESET_SEQUENCE` (200 nulls + `ESC @`, the same preamble `convert()` prepends) to flush the printer's command buffer. Nulls are no-ops in command mode and blank filler mid-raster, so the worst case is a short blank feed rather than a sheet of static. It is best-effort — nothing is acknowledged over 9100 — and the error message says whether the flush got through.

### dt_process pipeline

File patterns:
- `[r12345678].wav` — multi-track, regions parsed from WAV `smpl`/`cue` chunks
- `12345678A1.flac` — single pre-split track

Pipeline per track (multiprocessing.Pool, one worker per CPU):
1. Split WAV at region markers
2. Fetch DiscogsRelease (cached 7 days in `~/.discogstool/discogs.db`)
3. EBU R128 loudnorm (2-pass ffmpeg, I=−14 LUFS, TP=−1 dBTP, LRA=11)
4. Convert → 44.1 kHz / 16-bit AIFF or ALAC; embed artwork + tags; rename

Output filename: `{ARTIST} - {TITLE} {TRACK_NUM} [{LABEL}].{ext}`

### Beatport BPM lookup

`BeatportMatcher().find_bpms(discogs_release)` returns `{track_idx: {"bpm": int, "duration_ms": int}}`.

Three matchers tried in order:
1. **CatnoMatcher** — searches `<catno> <artist>`, scores by `_catno_similarity` + `_title_similarity`
2. **TitleMatcher** — searches `<title> <artist>`, same scoring
3. **AnthropicMatcher** — collects up to 10 Beatport candidates across all query strategies, sends to Claude Haiku with the Discogs release metadata, asks it to pick the correct one

Year handling: beyond 3-year difference → hard reject. Within 3 years → multiply score by `0.85 ** year_diff`. This reflects digital releases appearing on Beatport later than vinyl Discogs dates.

Catno normalisation strips: Unicode combining marks, zero-width spaces (Cf category), spaces, hyphens, and trailing format suffixes after a digit (`D`, `LP`, `EP`, `CD`). So `BLKRTZ050D`, `BLKRTZ050LP`, and `BLKRTZ050` all normalise to `BLKRTZ050`.

**BPM verification**: After track matching, `_verify_bpms()` downloads each matched track's Beatport preview MP3 (`sample_url`) and runs Essentia's `RhythmExtractor2013(method='multifeature')` locally. If the detected BPM diverges from Beatport's declared value by more than 5% (and confidence is ≥ 2.5), the local detection overrides. Octave errors (detected ≈ 2× or 0.5× declared) are detected and the declared value is kept. Results are cached permanently in the `bpm_verified` table in `beatport.db`, keyed by Beatport track ID. CLI flags: `--no-verify` skips verification, `--reverify` ignores cached results and re-analyzes.

### DiscogsRelease / DiscogsTrack

`DiscogsRelease(release_id)` is lazy — data is fetched on first access and pickled into `discogs.db`. `DiscogsTrack` wraps a 0-based index into the release tracklist. Both are used throughout `dt_process`, `dt_label`, and `beatport.py`.

---

## Data Storage

| File | Format | Purpose |
|------|--------|---------|
| `~/.discogstool/discogs_auth` | `TOKEN\|SECRET` | Discogs OAuth tokens |
| `~/.discogstool/beatport_auth.json` | JSON | Beatport + Anthropic credentials |
| `~/.discogstool/discogs.db` | SQLite | Discogs release cache (7-day TTL, pickled) |
| `~/.discogstool/beatport.db` | SQLite | Beatport release cache + match/nomatch + bpm_verified |
| `~/.discogstool/beatport.log` | Rotating text | Debug log for every Beatport matching decision |
| `~/.discogstool/label_config` | `key=value` | Printer address, model, default profile |
| `~/.discogstool/find_config` | `key=value` | dt_find model override (`find_anthropic_model`) |

### beatport_auth.json keys

```json
{
  "username": "...",
  "password": "...",
  "access_token": "...",
  "refresh_token": "...",
  "expires_at": 1234567890.0,
  "anthropic_api_key": "sk-ant-...",
  "anthropic_model": "claude-haiku-4-5-20251001"
}
```

`anthropic_api_key` also falls back to the `ANTHROPIC_API_KEY` environment variable.

### beatport.db tables

- `release_cache` — Beatport release JSON, 90-day TTL, keyed by Beatport release ID
- `matches` — confirmed Discogs→Beatport ID mappings, permanent
- `nomatches` — releases with no Beatport match, retried after 30 days
- `bpm_verified` — Essentia-verified BPMs, keyed by Beatport track ID, permanent (use `--reverify` to re-analyze)

---

## External APIs

| API | Auth | Notes |
|-----|------|-------|
| Discogs | OAuth (consumer key hardcoded, user tokens in `discogs_auth`) | Rate-limited manually (1.1s delay) |
| Beatport v4 | Username/password → OAuth access token (auto-refreshed) | Client ID scraped from `api.beatport.com/v4/docs/` HTML |
| Anthropic | API key in `beatport_auth.json` or `ANTHROPIC_API_KEY` env var | Used by `AnthropicMatcher` (Beatport fallback) and `AnthropicBackend` (dt_find) |

---


### Test conventions worth keeping

**Assert on output, not absence of exceptions.** The label tests render to a canvas and check pixels (`_ink()` counts non-white pixels; `_bpm_zone()`/`_qr_zone()` crop the regions of interest). Two tests previously called `render_label` and asserted nothing — they were named as if they verified BPM and artist rendering but would have passed with that drawing removed entirely. That blind spot is why a QR printing over the track listing survived three fixes.

**libtags tests use real files, not mocked mutagen.** `mutagen.File()` tags a generated WAV with genuine ID3 frames (`_WaveID3` subclasses `ID3`), so `AudioFile` round-trips are tested against the real library. `rename_file(dryrun=True)` returns the computed destination without touching the filesystem, making the naming logic directly testable.

**Characterization before refactor.** `tests/test_config_parsing.py` was written by *running* the pre-refactor parser against edge cases and recording what it did, then asserting every caller still matches. Prefer that to reading the code and writing what it looks like it should do.

## Running Tests

```bash
pytest tests/              # full suite
pytest tests/ -x -v        # stop on first failure, verbose
pytest tests/test_beatport.py  # single module
```

Tests must be run from a directory _above_ the project root (or with the project on `sys.path`) because the entry-point scripts have no `.py` extension. The `conftest.py` handles `sys.path` setup.

All external APIs (Discogs, Beatport, Anthropic, ffmpeg, printer) are mocked. Tests use in-memory SQLite (`:memory:`) for cache tests. No network calls in the test suite.

---

## Development

### Setting dt_label config

`--printer`, `--model`, and `--label-profile` persist to `~/.discogstool/label_config` and can be used without a release ID, which saves and exits:

```bash
./dt_label --printer tcp://brn94ddf8a9c192.local:9100
./dt_label --model QL-1110NWB --label-profile dk22243
```

Config is loaded and saved *before* the release-argument validation runs, so a config-only invocation isn't rejected for lacking a release ID. Keys merge — setting one leaves the others intact.

### Running dt_server manually
```bash
python3 dt_server                # defaults to port 5679
python3 dt_server --port 5680
```

### Installing as macOS login agent
```bash
./install_server.sh              # installs launchd plist, starts on login
./install_server.sh --unload     # removes it
launchctl kickstart -k gui/$(id -u)/com.discogstool.server  # restart
```

### Browser extensions

```bash
./build_ext.sh              # both
./build_ext.sh chrome       # one
```

- Firefox dev: `about:debugging` → Load Temporary Add-on → `firefox-ext/manifest.json`
- Firefox prod: `./sign_extension.sh` (builds first; needs npm + AMO credentials in `~/.discogstool/amo_auth`)
- Chrome dev: `chrome://extensions` → Developer mode → Load unpacked → `chrome-ext/`
- Chrome prod: `./publish_chrome.sh` (builds, zips, uploads, submits for review)

Chrome has no equivalent of AMO's unlisted signing — Linux is the only platform where Chrome installs extensions hosted outside the Web Store — so on macOS an **unlisted Web Store item** is the only route to a signed, auto-updating extension. Unlisted is not a review exemption: Public, Unlisted and Private all go through the same review.

The item and the OAuth credentials must be created by hand once; see `docs/chrome-webstore-setup.md`. Two traps documented there are worth repeating: the API cannot create an item, and it refuses to publish after a manual visibility change until you have published once with that visibility — so the unlisted setting must be applied *and* published manually before the script will work.

`sign_extension.sh` and `publish_chrome.sh` both run `build_ext.sh` first, so a source change cannot be left out of a release. Credentials live in `~/.discogstool/amo_auth` and `~/.discogstool/cws_auth` (mode 600).

**Important**: The `version` field in `ext/firefox/manifest.json` (and `ext/chrome/manifest.json`, which a test asserts stays in step) **must be incremented** whenever any extension file is changed (anything under `ext/`). AMO rejects re-signing with an already-used version number, so `sign_extension.sh` will fail with an error if you forget to bump it.

### dt_find LLM backend

`dt_find` runs its Discogs search agent on Claude Haiku via the Anthropic API, using native tool use:

```bash
./dt_find "the one with the triangle"
./dt_find --model claude-haiku-4-5-20251001   # saved to find_config
```

Reads `anthropic_api_key` from `~/.discogstool/beatport_auth.json` or `ANTHROPIC_API_KEY` env var. An optional `find_anthropic_model` key in `find_config` overrides the default model (`claude-haiku-4-5-20251001`); `--model` writes that key.

**Architecture**: `AnthropicBackend` is the only backend; `create_backend(config)` builds it from `find_config` and is what `main()` calls. `TOOLS` is written directly in Anthropic native format (`input_schema`, not `parameters`).

There used to be a second backend, `LocalLLMBackend`, speaking the OpenAI API to a local server (MLX/llama.cpp/vLLM). It is gone, and with it:

- the `LLMBackend` ABC, which existed only to hold two implementations,
- `_openai_to_anthropic()` and the OpenAI-shaped `TOOLS`/`ANTHROPIC_TOOLS` pair — with one dialect left there is nothing to convert,
- `_strip_think()`/`_extract_think()`, specific to reasoning-model `<think>` output,
- the `backend`, `llm_url` and `llm_model` keys and the `--backend`/`--wintermute` flags. `--model` now means the Anthropic model.

Stale `backend=local` / `llm_url` / `llm_model` lines in an existing `find_config` are simply ignored (pinned by a test) — no migration needed.

Separately, `beatport.py` carried an `LLMMatcher` that posted to an Ollama endpoint (`llm_url` in `beatport_auth.json`). It was never in the default matcher chain — `[CatnoMatcher(), TitleMatcher(), AnthropicMatcher()]` — so it was dead code, and it was removed with the rest. The three JSON-parsing edge cases its tests uniquely covered moved onto `AnthropicMatcher._parse_response`, which shares the extraction logic.

### Beatport credential setup
```bash
python3 beatport.py --setup      # interactive: username, password, Anthropic key
python3 beatport.py --release 12345678          # test a lookup
python3 beatport.py --release 12345678 --force  # bypass nomatch cache
python3 beatport.py --release 12345678 --no-verify   # skip BPM verification
python3 beatport.py --release 12345678 --reverify    # re-analyze preview audio
python3 beatport.py --clear-match 12345678      # remove cached result
```

### System dependencies (macOS)
- `ffmpeg` — audio encoding, loudnorm filter
- `flac` — FLAC decoding
- Brother QL driver — `pip install brother-ql-inventree` (for physical printing)

---

## Key Code Conventions

**One tool dispatcher in `dt_find`**: `_dispatch_tool()` executes every tool call, separate from the wire-format handling in `AnthropicBackend.run_agent`. It was introduced when two backends each carried a copy of the same decisions; with one backend left it still keeps tool semantics out of the transport loop.

**Shared helpers in `util.py`**: `load_kv_config`/`save_kv_config` own the `key=value` dotfile format used by `label_config` and `find_config` — dt_label, dt_find and dt_server previously each carried a byte-identical copy of the parser. Its quirks (last duplicate key wins, `#` only starts a comment at line start, split on first `=` only) are load-bearing for existing files and pinned by `tests/test_config_parsing.py`, which asserts every caller produces identical output. `resolve_anthropic_key()` is the single Anthropic key lookup (auth file, then `ANTHROPIC_API_KEY`); it replaced four copies.

**Module paths are resolved per call, not at import**: `util.datadir()` and `beatport.auth_file()`/`cache_file()`/`log_file()` are functions. `beatport.CACHE_FILE` was also a default argument (`db_path: str = CACHE_FILE`), which evaluates at import no matter how lazy the constant is. Historical attribute names still resolve via module `__getattr__`.

**`util.datadir()` is lazy**: `~/.discogstool` is resolved and created per call, not at import. It used to be a module constant, so importing `util` created a directory as a side effect and froze `$HOME` before any test could redirect it — patching `HOME` in a test had no effect and writes escaped to the real home directory. `util.datapath` still works via a module `__getattr__`. When testing config code, patch `util.userfile` or set `HOME`; both now work.

**Type annotations**: `from __future__ import annotations` everywhere. TypedDict used for all API response shapes. Not all modules are fully annotated.

**Error handling**: Custom exceptions (`ClientException`, `TagsException`, `ConversionException`, `BeatportError`). Network calls retry with backoff. Subprocess failures checked via `returncode`. Beatport failures degrade gracefully (label prints without BPM).

**Logging**: `beatport.py` uses a rotating file handler attached on first `BeatportMatcher()` instantiation. Other modules use minimal stdout + tqdm progress bars. Pass `-v` to CLI tools for `logging.DEBUG`.

**Multiprocessing**: `dt_process` uses `multiprocessing.Pool` with spawn context. Workers receive config via `worker_init` + globals (not inheritance). `db_lock` (multiprocessing.Lock) protects SQLite writes.

**Lazy loading**: `DiscogsRelease.getData()` fetches from API on first call, caches in SQLite. Subsequent calls return the cached pickle.

**Comment tag encoding**: `libtags.py` encodes release metadata in the audio comment field as `{LABEL} [{CATNO}] Discogs: {RELEASE_ID}`, which is regex-extracted on re-read to map files back to releases.

---

## Label Profiles

| Profile | Media | Canvas | Use case |
|---------|-------|--------|----------|
| `dk1247` | Brother DK-1247 die-cut | 1200×1822 px | Standard 12" sleeve, ≤12 tracks per label |
| `dk22243` | Brother DK-22243 continuous 102mm | 1164×variable px | Long releases, auto-height, ≤18 tracks |

`_chunk_tracks()` packs tracks onto labels by **measured** height for both feeds, using binary search over the probe (below). Continuous labels fill up to 11.5" (≤3600 px hard limit). Die-cut labels fill their fixed canvas with the QR still clearing the last row, capped at `max_tracks`. Die-cut used to be packed by count alone, 12 per label. That assumed one-line rows and two sides, and across a 1000-release collection it already overran the QR on 48 labels (by up to 148 px) before credit lines existed. `--split` chunks each disc the same way, measuring the first chunk with its "Disc N of M" line. An oversized disc gets `disc2_cont1` etc. (`_` sorts after `.`, so previews list it after `disc2.png`), and `_select_discs()` keeps continuations with their disc for `--discs`.

### QR placement — height is measured, not predicted

The QR is anchored to the canvas bottom (`qr_top = H - M - _QR_SIZE`), so it lands correctly only if `H` accounts for everything above it.

`H` used to be *predicted* by a second implementation of the layout (`_continuous_height` + `_measure_wrap_extra_px`) that re-derived header, side-header and wrapped-line heights from constants. Keeping two implementations in agreement failed three separate times, always the same way: they disagreed about **what text gets laid out**, a longer string wrapped to a second line, the canvas came up short, and the QR printed over the tracks. The last instance was the Beatport `duration_ms` fallback — the renderer appended a duration the measurer knew nothing about, wrapping two titles on r4884361, 100 px short.

There is now one implementation. `render_label(..., probe=True)` runs the real header/track layout and returns the content-bottom `y` instead of an image; `_layout_height()` adds `_NOTES_GAP + _QR_SIZE + margin` to it. Clearance is therefore exactly `_NOTES_GAP` by construction, and the old safety buffer is gone (labels are ~50 px shorter).

Two invariants make the probe valid, both pinned by tests:

- Content layout never reads `H` — only the bottom-anchored QR and the die-cut ruled-line fill do, and neither runs in probe mode. That is why a probe can use a 1 px scratch canvas: Pillow clips the draws and the arithmetic is unchanged.
- `_resolve_track_text()` is the single source of truth for a row's title text, and `_track_credits()` for its credit lines. Anything affecting row content belongs in one of those, so probe and render can't diverge. (`_track_credits()` runs inside `render_label`, which *is* the probe, so the two always see the same credits.)

`_position_column_width()` sizes the position column to the widest entry present rather than assuming a fixed 90 px. Most releases use short labels ("A1"), but continuous DJ mixes use timestamps ("14:03", ~97 px) and some sides are double letters ("AA1") — those used to overprint the title (r34333258). It is computed inside `render_label` so probe and render agree: the column feeds `TITLE_MAX_W`, which decides wrapping, which decides height.

`render_label()` still warns via `log.warning` if content crosses `qr_top`. That should now be unreachable; it is a tripwire, not a safety net.

### Header

Artist and release title both **wrap** (up to `_HEADER_MAX_LINES` = 3) rather than truncate with an ellipsis. The artist used to truncate while the title wrapped; across the collection 16 artists and 20 titles need two lines, and none need three. The font is chosen from the whole string *before* wrapping, so breaks are measured in the face that is drawn. The continuation header stays a single truncated line on purpose, since it is meant to be compact.

`_strip_disambig()` removes Discogs " (2)" suffixes before a ` / ` separator as well as at the end. Multi-artist strings come from `compileListData` joined with " / ", so "Caterina Barbieri (2) / … / Lucy (12)" used to keep the inner suffix. Truncation hid that; wrapping would not. Separators are left alone, because "Jonsson/Alter" is one duo's name.

### Credits

Labels print **only the credits Discogs lists under each track** — the track's own `extraartists`, via `DiscogsTrack.getTrackCredits()`. Release credits are never printed, not even ones scoped to particular tracks. An earlier version also printed release credits, with any credit covering every track hoisted into a line under the release title, but on real labels that line was mostly a producer's alias or real name ("Producer [Uncredited]: Brenton Smith" on Daze, r6628547). Credits now only ever appear under a track. A credit shared by every track repeats on each row (3 releases in the collection).

The data layer still resolves release credits for any other caller. `DiscogsTrack.getCredits()` merges track credits with release credits whose `tracks` field covers the track, and `DiscogsRelease.resolveTrackScope()` turns `tracks` into indices. It handles positions, `&`, `X to Y` ranges, bare sides (`"B"`), sides as range endpoints (`"A to B2"`), `"All …"` and `"All … except …"`, and ignores unresolvable tokens (`"C3 outro"`) rather than guessing.

`_track_credits(release, track)` decides what prints, and every rule came from a tally of roles across the 1000-release cache:

- **Roles** — `_CREDIT_RULES`: Remix, Edit, DJ Mix, Featuring, Vocals, Voice, Producer, Co-producer, Add. Production. Remix is by far the most common track credit. Excluded: pressing and packaging roles (Lacquer Cut By, Mastered By, Artwork), songwriting, "Mixed By" (a mix engineer, not a DJ mix), backing vocals, and `Edited By [Digital Editing]` (mastering, not a re-edit). Qualifiers are matched by rule, e.g. `Producer [Additional…]` → "Add. Production".
- **Already in the title → skipped.** 74% of Remix credits repeat the title ("Peels A Tangerine (Regis Remix)" + "Remix: Regis"). Matching is word-bounded and case-insensitive.
- **Own artist → skipped**, except version credits (`_VERSION_LABELS`: Remix, Edit, DJ Mix). "Producer: <the artist>" is noise, but an artist remixing their own track is what tells two versions apart.

Layout: `_credit_groups()` groups by role in `_CREDIT_ORDER`, and `wrap_credits()` breaks **between** groups, never inside one. The first render split "Add. Production:" from its name. Continuous labels get up to 2 lines, die-cut 1, with an ellipsis when groups are left over. Each line adds `_CREDIT_LINE_H` to the row, and the probe measures that like everything else.

`tests/conftest.py` has r3854655 as a shared fixture (`cheap_and_deep`), captured verbatim from the API: two remixes with identical titles that only their track credits tell apart, plus release credits (scoped and unscoped) that must not print. `make_release(data)` builds a real `DiscogsRelease` without the network, so credit tests exercise the genuine accessors rather than mocks.

### Font fallback

Labels default to Arial (macOS) / Liberation Sans (Linux), neither of which covers
non-Latin scripts. `_font_for(style, size, text)` therefore checks *actual glyph
coverage* rather than guessing from Unicode ranges: `_covers()` rasterises a
character and compares it against the face's `.notdef` box, probed with U+FFFE (a
permanent noncharacter no font may map). Coverage is size-independent, so it is
probed once at `_PROBE_SIZE` and cached per `(font path, char)`.

If the primary face is missing glyphs, `_FALLBACK_ORDER` (`cjk`, `devanagari`,
`thai`, `arabic`, `hebrew`, `unicode`) is walked and the first face covering the
**entire** string wins. Whole-string coverage matters because Pillow renders a run
in a single face — a fallback that fixes the Devanagari but drops the Latin is no
improvement. If nothing covers everything, the best face is used and a one-time
warning listing the offending codepoints goes to stderr.

Consequence: a track row containing non-Latin text renders entirely in the
fallback face, so that row's Latin text will not match Arial elsewhere on the label.

**Complex scripts need shaping, not just glyphs.** Devanagari reorders matras
(`ि` is stored after its consonant but renders before it) and forms conjuncts;
Arabic needs contextual joining. Pillow only does this when built with
Raqm/HarfBuzz, which it selects automatically when available. Verify with:

```bash
.venv/bin/python3 -c "from PIL import features; print(features.check('raqm'))"
```

If this is False, non-Latin titles render with correct glyphs in the wrong order —
subtly wrong rather than obviously broken.

---

## Notable Quirks

- **macOS-only features**: `install_server.sh` (launchd), `dt_find` speech recognition, Arial font paths. The rest works on Linux.
- **Font fallback is best-effort**: it can only pick from faces installed on the machine. A script with no installed font prints boxes plus a stderr warning rather than failing the job. Bold/condensed styles are not preserved when falling back — script fallback faces are regular weight only.
- **Beatport client ID**: scraped from a docs page JS bundle via regex. If Beatport updates their page structure this will break; run `python3 beatport.py --setup` to force a re-auth which will re-scrape it.
- **WAV regions**: Reaper writes track boundaries into the WAV `smpl` chunk as loop points. If region count doesn't match the Discogs tracklist, `dt_process` falls back to consecutive cue positions with a dynamic minimum-duration heuristic.
- **ID3 version**: writes ID3v2.3 (encoding=3, UTF-8), not v2.4.
- **Missing fields read as `""`, never `None`**: Discogs sends `null` for fields it has nothing for. `country` is null on ~1% of releases and used to print as "None" on labels, because a key holding `null` defeats `.get(key, "")`. Every `DiscogsRelease`/`DiscogsTrack` text accessor goes through `_text()`, so null or missing becomes `""`. `getYear()` also treats `0`, Discogs' placeholder for "unknown", as blank. As `"0"` it tagged files with year 0000 and made Beatport's year gate (`if year and bp_year`) reject every candidate. `compileListData()` skips blank entries rather than joining them as "None" or a dangling " / ". The label header joins only the year/country and label/catno parts that exist, so a blank one leaves no gap or misaligned text.
- **Empty `labels`/`artists` lists**: Discogs returns `labels: []` for some releases (r3060397, a self-released CD). `compileListData()` returns `""` for an empty or missing list; it used to raise `IndexError` and crash `dt_label`. The label line then shows only what exists. The audio comment tag becomes `" [] Discogs: N"`, which is ugly but still matches `comment_regex`, so it is left alone because that format is load-bearing for re-reads.
- **Discogs consumer keys**: the OAuth consumer key/secret are hardcoded public demo credentials. They are not secret.
- **dt_server's worker thread starts in `main()`, not at import**: importing dt_server (as the test suite does) must not spawn a thread that races the caller for queued jobs or launches real dt_label subprocesses. Tests call `_execute_job` directly.
- **Flask sorts JSON keys**, so `/progress` returns `stages` alphabetically rather than in pipeline order. The popup renders in DOM order and ignores this; the canonical order is also returned as a `stages` array.
- **AnthropicMatcher validation**: the model's returned Beatport ID is validated against the candidate list presented in the prompt. IDs not in the list are silently discarded to prevent hallucination.
