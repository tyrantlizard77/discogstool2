# discogstool2

A Python toolkit for digitizing vinyl records and managing your music collection with automatic metadata tagging from Discogs.

## Overview

discogstool2 helps you process audio recordings from vinyl records and automatically tag them with accurate metadata from the Discogs database. It can split multi-track WAV files, normalize audio levels, convert between formats, and organize your collection.

## Features

- **Automatic Metadata Tagging**: Fetches release and track information from Discogs API
- **Multi-track WAV Splitting**: Automatically splits single WAV files into individual tracks using regions
- **Audio Processing**: EBU R128 loudness normalization (immune to vinyl pops) and converts to AIFF format
- **Collection Management**: Compare your local files against your Discogs collection
- **Cover Art**: Automatically downloads and embeds album artwork
- **Format Support**: Handles WAV, FLAC, MP3, M4A, AAC, and AIFF files
- **Local Caching**: SQLite database caches Discogs API responses to minimize API calls

## Prerequisites

### System Dependencies

You need the following command-line tools installed:

- **ffmpeg**: Audio conversion and normalization (`brew install ffmpeg` on macOS, `apt install ffmpeg` on Ubuntu)
- **flac**: FLAC decoding (`brew install flac` on macOS, `apt install flac` on Ubuntu)
- **normalize-audio** or **normalize**: (Optional) Only needed if using `--legacy-normalize` flag (`brew install normalize` on macOS, `apt install normalize-audio` on Ubuntu)

### Python Dependencies

```bash
pip install -r requirements.txt
```

Required packages:
- mutagen (audio metadata handling)
- tqdm (progress bars)
- python3-discogs-client (Discogs API integration)
- numpy (audio data processing)
- urllib3 (HTTP requests)

### Discogs Account

You need a Discogs account. On first run, the tool will:
1. Display an authorization URL
2. Ask you to visit the URL and authorize the application
3. Prompt you to enter the verification code
4. Store your credentials in `~/.discogstool/discogs_auth`

## Installation

1. Clone this repository
2. Install system dependencies (ffmpeg, normalize, flac)
3. Install Python dependencies: `pip install -r requirements.txt`
4. Make the scripts executable: `chmod +x dt_process dt_collection`

## Recording Workflow

**Important**: This tool does NOT record audio from your turntable. It's a post-processing tool that works with audio files you've already captured using [Reaper](https://www.reaper.fm/).

### Step 1: Record the Vinyl

1. Connect your turntable to your computer via audio interface/USB
2. Create a new Reaper project and set up your audio input
3. Record the entire side of the vinyl as a single take
4. Stop recording when the side finishes

### Step 2: Create Regions for Track Boundaries

**Important**: Use **Regions**, not just markers. Regions define both the start AND end of each track, allowing for precise boundaries even when there's silence between tracks.

1. Select the audio range for the first track by clicking and dragging
2. Press **Shift+R** (or right-click → Create Region from Selection)
3. Repeat for each track on the recording
4. Region names are optional - the tool only uses the boundaries

#### Why Regions Matter

If you only place markers at track starts (without defining regions), the tool will assume each track runs until the next marker. This causes problems when there's silence between tracks:

- **Without regions**: A 4-minute track followed by 1 minute of silence would be exported as 5 minutes
- **With regions**: The track is cut precisely at the region boundary you defined

### Step 3: Export the Audio

1. **File → Render** (or Ctrl+Alt+R)
2. Set Output Format to **WAV**
3. Name the file: `[r{RELEASE_ID}].wav`
   - Example: `[r12345678].wav`
   - Find the release ID from the Discogs URL: `https://www.discogs.com/release/12345678-...`
4. Under **"Embed metadata"** or **"Include"**, ensure these are enabled:
   - **"Write markers + regions"** - Critical for track splitting
5. Click **Render**

The regions are stored in the WAV file's `smpl` chunk as loop points, which dt_process reads to determine track boundaries.

### Step 4: Process with dt_process

Now use this tool to split, normalize, tag, and organize:

```bash
./dt_process -o ~/Music/Processed '[r12345678].wav'
```

The tool will:
- Split the WAV using your regions
- Fetch metadata from Discogs
- Normalize audio levels
- Convert to AIFF format
- Embed cover art
- Tag with all metadata (artist, title, year, label, etc.)
- Rename files appropriately

### Alternative: Individual Track Files

Instead of using regions, you can export each track separately:

1. Select the audio for each track individually
2. Export each as: `{RELEASE_ID}{POSITION}.wav`
   - Examples: `12345678A1.wav`, `12345678A2.wav`, `12345678B1.wav`
3. No regions needed with this method

## Usage

### Processing Audio Files (dt_process)

Convert and tag audio files with Discogs metadata.

#### File Naming Convention

Files must follow specific naming patterns to identify the Discogs release and track position:

**For individual tracks:**
```
{RELEASE_ID}{POSITION}.{EXTENSION}
```
Examples:
- `12345678A1.wav` - Release 12345678, track A1
- `12345678B.flac` - Release 12345678, track B
- `12345678A2.mp3` - Release 12345678, track A2

**For multi-track WAV files (to be split):**
```
[r{RELEASE_ID}].wav
```
Example:
- `[r12345678].wav` - Will be split into individual tracks based on cue markers

#### Basic Usage

```bash
./dt_process -o /path/to/output/directory file1.wav file2.flac [r12345].wav
```

#### Options

- `-o, --outdir` (required): Output directory for processed files
- `-v, --verbose`: Enable debug messages
- `-j, --jobs N`: Number of parallel normalization jobs (default: CPU count)
- `--legacy-normalize`: Use legacy peak normalization instead of EBU R128 (see Normalization below)

#### Examples

Process a single track:
```bash
./dt_process -o ~/Music/Processed 12345678A1.wav
```

Process multiple files:
```bash
./dt_process -o ~/Music/Processed 12345678A1.wav 12345678A2.wav 12345678B1.flac
```

Split and process a multi-track WAV file:
```bash
./dt_process -o ~/Music/Processed '[r12345678].wav'
```

#### What dt_process Does

1. **For WAV files with regions** (named `[rXXXX].wav`):
   - Reads regions from the `smpl` chunk (Reaper exports regions as loop points)
   - Falls back to consecutive cue marker positions if no regions found
   - Splits into individual tracks (minimum 30 seconds, or 6 seconds for releases with many short tracks)
   - Creates temporary files named `{RELEASE_ID}.{POSITION}.wav`

2. **For all audio files**:
   - Fetches metadata from Discogs (artist, album, track title, year, genre, label, etc.)
   - For WAV/FLAC: Normalizes audio using EBU R128 loudnorm, converts to 44.1kHz/16-bit AIFF
   - For MP3: Copies and tags without re-encoding
   - Embeds cover artwork
   - Renames files to: `{ARTIST} - {TITLE} {TRACK_NUM} [{LABEL}].{ext}`

### Normalization

By default, dt_process uses **EBU R128 loudness normalization** (via ffmpeg's loudnorm filter), which is ideal for vinyl digitization:

- **Target**: -14 LUFS integrated loudness (standard for electronic/DJ music)
- **True Peak Limit**: -1 dBTP (prevents digital clipping)
- **Two-pass processing**: Analyzes first, then applies precise normalization
- **Linear mode**: No dynamic compression, only gain adjustment + peak limiting

**Why this matters for vinyl**: Traditional peak normalization can be fooled by vinyl pops/clicks. A single loud pop becomes the "peak", preventing the actual music from being normalized properly. EBU R128 measures integrated loudness over time, so short transients (pops) don't affect the calculation. The true peak limiter catches any pops that would clip, while the music passes through with only gain adjustment.

**Legacy mode**: Use `--legacy-normalize` to use the old peak-based normalization (requires normalize-audio/normalize utility). This normalizes to -1.5dB peak, which may result in quieter tracks if vinyl pops are present.

### Managing Your Collection (dt_collection)

Analyze your music collection and compare it against your Discogs collection.

#### Basic Usage

```bash
./dt_collection /path/to/music/directory
```

#### Options

- `-c, --collection FILE`: CSV file exported from your Discogs collection
- `-u, --update-metadata`: Refresh metadata/images from Discogs for all found files
- `-n, --dry-run`: Show what would be done without making changes
- `-v, --verbose`: Output diagnostic messages
- `-Y, --min-year YEAR`: Ignore releases older than specified year

**Report options** (requires `-c`):
- `-a, --all-reports`: Generate all reports
- `-M, --missing`: Report tracks found locally but not in Discogs collection
- `-D, --discogs-missing`: Report releases in Discogs collection not found locally
- `-P, --partially-recorded`: Report releases that are only partially recorded

#### Examples

Scan a directory and update metadata:
```bash
./dt_collection -u ~/Music/Vinyl
```

Compare local files to Discogs collection:
```bash
./dt_collection -c ~/Downloads/collection.csv ~/Music/Vinyl
```

Generate all reports:
```bash
./dt_collection -c ~/Downloads/collection.csv -a ~/Music/Vinyl
```

Find what you haven't recorded yet:
```bash
./dt_collection -c ~/Downloads/collection.csv -D ~/Music/Vinyl
```

Dry run to see what would be updated:
```bash
./dt_collection -n -u ~/Music/Vinyl
```

#### Exporting Your Discogs Collection

1. Go to https://www.discogs.com/settings/exports
2. Request a new export
3. Download the CSV file when ready
4. Use this file with the `-c` option

## How It Works

### File Format and Metadata

Audio files are tagged with metadata stored in the comment field:
```
{LABEL} [{CATALOG_NUMBER}] Discogs: {RELEASE_ID}
```

This allows the tool to:
- Identify which Discogs release a file belongs to
- Refresh metadata when the release data changes
- Verify track counts match the release

### Data Storage

The tool creates a directory at `~/.discogstool/` containing:
- `discogs_auth`: OAuth tokens for Discogs API
- `discogs.db`: SQLite database caching API responses (7-day default cache)
- Cover art images (hashed by URI)

### Position Matching

The tool handles various track position formats:
- Standard: A1, A2, B1, B2
- Without numbers: A, B (treated as A1, B1)
- Double-sided: AA1, AA2 (for side B on some pressings)
- Alternative formats: 1B instead of B1
- With periods: A1., B2.

## Limitations

- **Regions**: Multi-track WAV splitting requires regions exported from Reaper (stored in the `smpl` chunk)
- **API Rate Limiting**: Discogs API has rate limits (the tool includes delays to handle this)
- **File Format**: Only processes files in supported formats (WAV, FLAC, MP3, M4A, AAC, AIFF)

## Troubleshooting

**"missing normalize utility"**
- This only occurs when using `--legacy-normalize`
- Install normalize-audio (Ubuntu) or normalize (macOS)
- Or remove the `--legacy-normalize` flag to use the default EBU R128 normalization (recommended)

**"Release XXXXX not found"**
- Verify the release ID exists on Discogs
- Check your internet connection
- The release may have been deleted or merged on Discogs

**"Unexpected region count"**
- The number of detected regions doesn't match the track count on Discogs
- Ensure you created regions (Shift+R), not just markers
- Check that the Discogs tracklist is accurate
- Run with `-v` to see which regions are being detected and their durations

**Tracks include unwanted silence at the end**
- You're using markers instead of regions - the tool is using the next marker as the track end
- In Reaper: use regions (Shift+R) to define precise start and end points for each track
- Make sure "Write markers + regions" is enabled when rendering

**"Couldn't find position X in release Y"**
- The track position in your filename doesn't exist in the Discogs release
- Check the tracklist on Discogs and verify your position labels

**Rate limiting errors**
- The tool includes automatic retry logic and delays
- If persistent, wait a few minutes and try again

## License

See LICENSE file for details.