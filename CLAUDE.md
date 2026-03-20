# discogstool2 — CLAUDE.md

Reference guide for AI-assisted development on this codebase.

---

## Project Purpose

A Python toolkit for digitising vinyl records and managing a Discogs collection. The core workflow:

1. Record vinyl sides in Reaper, exporting WAV files with region markers at track boundaries
2. Run `dt_process` to split by region, fetch metadata from Discogs, EBU-R128 normalise, convert to AIFF/ALAC, embed cover art and tags
3. Optionally run `dt_label` (or trigger it via the Firefox extension + `dt_server`) to print a Brother thermal sleeve label
4. Use `dt_collection` to compare local files against a Discogs collection export and update stale metadata
5. Use `dt_find` to identify an unknown record via voice/text description through an LLM + Discogs search agent loop

---

## Repository Layout

```
dt_server          Flask HTTP bridge (localhost:5679) for Firefox extension
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

firefox-ext/          WebExtension (popup.html/js, background.js, manifest.json)
tests/                pytest suite (one file per module)
install_server.sh     macOS launchd plist installer for dt_server
sign_extension.sh     AMO signing for Firefox extension
requirements.txt      Python dependencies
```

All entry-point scripts have no `.py` extension. Tests load them via `importlib.machinery.SourceFileLoader`.

---

## Architecture

### dt_server ↔ Firefox extension

The extension popup calls `http://localhost:5679/status` (health) and `POST /print` (release_id, profile, preview, split, discs). The server delegates to `dt_label` as a subprocess, serves preview PNGs from a temp dir, and reports Discogs/Beatport/Anthropic/LLM credential status in the `/status` response. The popup footer displays four connection dots: Discogs, Beatport, Anthropic (used by the Beatport matcher), and Finder (the dt_find LLM backend, labelled "Claude" or "Local LLM" depending on the configured backend).

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
| `~/.discogstool/find_config` | `key=value` | dt_find LLM backend settings |

### beatport_auth.json keys

```json
{
  "username": "...",
  "password": "...",
  "access_token": "...",
  "refresh_token": "...",
  "expires_at": 1234567890.0,
  "anthropic_api_key": "sk-ant-...",
  "anthropic_model": "claude-haiku-4-5-20251001",
  "llm_url": "http://host:11434/api/generate",
  "llm_model": "llama3"
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

### Firefox extension
- Dev: `about:debugging` → Load Temporary Add-on → `firefox-ext/manifest.json`
- Prod: `./sign_extension.sh` (requires npm + AMO API credentials in `~/.discogstool/amo_auth`)

**Important**: The `version` field in `firefox-ext/manifest.json` **must be incremented** whenever any extension file is changed (`popup.html`, `popup.js`, `background.js`, `manifest.json` itself, etc.). AMO rejects re-signing with an already-used version number, so `sign_extension.sh` will fail with an error if you forget to bump it.

### dt_find LLM backend

`dt_find` supports two backends for the Discogs search agent, selected via `find_config` or `--backend`:

**`backend=local`** (default) — OpenAI-compatible local LLM (MLX, llama.cpp, vLLM, etc.):
```bash
./dt_find --backend local --wintermute http://host:8000/v1 --model my-model
./dt_find "the blue Miles Davis one"
```
Settings saved to `~/.discogstool/find_config`. `check_available` pings `{llm_url}/models`.

**`backend=anthropic`** — Claude Haiku via Anthropic API (recommended; uses native tool use):
```bash
./dt_find --backend anthropic
./dt_find "the one with the triangle"
```
Reads `anthropic_api_key` from `~/.discogstool/beatport_auth.json` or `ANTHROPIC_API_KEY` env var. An optional `find_anthropic_model` key in `find_config` overrides the default model (`claude-haiku-4-5-20251001`).

**Architecture**: `LLMBackend` is an abstract base class; `LocalLLMBackend` and `AnthropicBackend` inherit from it. `create_backend(config)` is the factory used by `main()`. `ANTHROPIC_TOOLS` is the Anthropic-format equivalent of `TOOLS` (uses `input_schema` instead of `parameters`). Thinking-tag stripping (`<think>…</think>`) is only applied in `LocalLLMBackend` since it is specific to reasoning-model outputs.

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
| `dk1247` | Brother DK-1247 die-cut | 1200×1822 px | Standard 12" sleeve, ≤12 tracks |
| `dk22243` | Brother DK-22243 continuous 102mm | 1164×variable px | Long releases, auto-height, ≤18 tracks |

Continuous labels use binary search to pack tracks into ≤11.5" chunks (≤3600 px height).

---

## Notable Quirks

- **macOS-only features**: `install_server.sh` (launchd), `dt_find` speech recognition, Arial font paths. The rest works on Linux.
- **Beatport client ID**: scraped from a docs page JS bundle via regex. If Beatport updates their page structure this will break; run `python3 beatport.py --setup` to force a re-auth which will re-scrape it.
- **WAV regions**: Reaper writes track boundaries into the WAV `smpl` chunk as loop points. If region count doesn't match the Discogs tracklist, `dt_process` falls back to consecutive cue positions with a dynamic minimum-duration heuristic.
- **ID3 version**: writes ID3v2.3 (encoding=3, UTF-8), not v2.4.
- **Discogs consumer keys**: the OAuth consumer key/secret are hardcoded public demo credentials. They are not secret.
- **AnthropicMatcher validation**: the model's returned Beatport ID is validated against the candidate list presented in the prompt. IDs not in the list are silently discarded to prevent hallucination.
