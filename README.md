# Karaoke

Party karaoke on your own hardware. Guests search a song on their phone and queue it; the
hub downloads it, detects its key, separates the vocals with
[demucs](https://github.com/adefossez/demucs) and aligns the lyrics word by word against the
sung vocals. The player screen shows the lyrics with a syllable wipe while the instrumental plays.

Search uses the Deezer catalogue (no account needed), audio comes from YouTube via
[yt-dlp](https://github.com/yt-dlp/yt-dlp), lyrics from [LRCLIB](https://lrclib.net).

## Requirements

- [uv](https://docs.astral.sh/uv/)
- FFmpeg and Node.js on the hub (used by yt-dlp)
- A GPU on the hub is recommended (CUDA, ROCm or Apple Silicon); the CPU works too

Loading a 3 min song takes about 15 s on a Radeon 8060S (ROCm) and 22 s on the CPU of a
Ryzen AI MAX+ 395.

## Installation

```bash
cp .env.example .env              # and fill it in, see the comments there
```

The hub needs the audio stack; pick the torch build that fits the machine:

```bash
uv sync --extra rocm              # AMD GPU via ROCm 7.2 (e.g. Strix Halo / gfx1151)
uv sync --extra cuda              # NVIDIA GPU
uv sync --extra cpu               # CPU only, also Apple Silicon (uses MPS)
```

The player and guest node need none of that, a plain `uv sync` is enough there.

The ROCm wheel bundles its own runtime and supports gfx1151 natively, no
`HSA_OVERRIDE_GFX_VERSION` needed. Your user needs access to `/dev/kfd` and `/dev/dri`.
Set `DEMUCS_DEVICE=cpu` to force the CPU on a GPU machine.

YouTube changes often, so keep yt-dlp fresh on the hub:

```bash
uv lock --upgrade-package yt-dlp && uv sync --extra rocm
```

## Usage

```bash
uv run karaoke                    # = karaoke hub, or whatever ROLE in .env says
uv run karaoke hub
uv run karaoke player
uv run karaoke guest
uv run karaoke rescan-scales      # detect the key of every downloaded song again
```

On one machine the hub does everything and serves three web UIs:

- **Guests** – `http://127.0.0.1:8082` (`GUEST_PORT`): search songs with autocomplete and queue
  them with a mode. No control over the song that is playing.
- **Player** – `http://127.0.0.1:8081` (`PLAYER_PORT`): open it on the screen people sing from.
  It plays the song and shows the lyrics full screen.
- **Admin** – `http://127.0.0.1:8080` (`ADMIN_PORT`): everything guests can do plus play / pause /
  skip / restart, removing and reordering queued songs, and the live mix, lyrics timing and key of
  the current song.

When queuing a song you pick how it is played:

| Mode | Plays |
| --- | --- |
| 🎤 Karaoke | Instrumental with quiet guide vocals (−18 dB) |
| 🔥 Hardcore | Instrumental only, no guidance |
| 😴 Boring | The original song as recorded |
| 🍻 Drunk | Full vocals, no instrumental |

The levels can be fine-tuned under *Advanced*. They are gains in dB relative to the recording:
0 dB plays a track as recorded, so instrumental and vocals both at 0 dB sound like the original
song (not twice as loud); negative values are quieter, positive values louder.

Queued songs are prepared in the background in queue order: find the YouTube upload whose
duration matches the track, download it, detect the key, separate vocals and fetch and align the
lyrics. A song only becomes ready once its lyrics are aligned (also in *Boring* mode, aligning
against the separated vocals is much more reliable than against the mix). Everything is cached in
`storage/`, so songs that were sung before are ready immediately and marked *Ready* in the search.

Each song waits on the player screen until someone presses **Space** or clicks, or the admin
presses play.

To let guests queue songs from their phones, set `HTTP_HOST=0.0.0.0` (everyone in your network can
then reach all three UIs, including admin).

To let guests queue songs from their phones, set `HTTP_HOST=0.0.0.0` (everyone in your network can
then reach all three UIs, including admin) or use the split setup below.

### Split setup: hub, player and guest node

For a party the three parts can live on three machines, connected by
[Tailscale](https://tailscale.com/). The command (or `ROLE` in `.env`) picks what a machine does.

```
  VPS (Docker)            PC (hub)                   Laptop (player)
  ┌──────────────────┐    ┌─────────────────────┐    ┌──────────────────┐
  │ public page      │─ts▶│ queue + session     │◀ts─│ admin UI         │
  │ code + limits    │    │ worker: demucs,     │    │ player UI        │
  └──────────────────┘    │ lyrics, key         │    │ prefetched cache │
   the only part          │ storage/tracks/     │    └──────────────────┘
   on the internet        └─────────────────────┘
```

- **hub** (`uv run karaoke hub`) - the strong machine. It keeps the queue, does every download,
  separation and alignment, and stores the songs. Nothing about it is public: it only listens on
  its Tailscale address. `HUB_TOKEN` is a shared secret the other two have to send; requests from
  the hub itself never need it.
- **player** (`uv run karaoke player`) - the laptop at the screen. It serves the admin and
  player UI and forwards every API call to the hub, but plays from its own disk: the current song
  and the next `PREFETCH_COUNT` (3) queued ones are copied over as soon as the hub has them.
  If the hub or Tailscale drops out, what is buffered keeps playing and the admin UI says
  *Hub offline*; `CACHE_MAX_GB` (20) caps the cache and the oldest songs are dropped first.
- **guest** (`uv run karaoke guest`, or the Docker setup in `deploy/vps/`) - the only public
  part. See below.

Minimal `.env` on the laptop:

```bash
ROLE=player
HUB_HOST=brick           # Tailscale name or IP of the hub
HUB_TOKEN=...            # the same secret as on the hub
CACHE_DIR=~/karaoke-cache
PUBLIC_GUEST_URL=https://karaoke.example.com
GUEST_INVITE_SECRET=...  # the same secret as on the guest node
```

The player shows a QR code of the guest page in the corner of the lyrics screen, and large when
the queue runs empty (the admin UI has it under *Invite QR*). It leads to
`PUBLIC_GUEST_URL/k/<code>`, where the code is derived from `GUEST_INVITE_SECRET` and the clock
and changes every `GUEST_CODE_MINUTES` (10, `0` keeps it fixed). Laptop and guest node compute it
independently, so their clocks only need to be roughly right. A photo of the screen that is sent
around therefore stops working after a while, but only entering needs a current code: whoever
scanned once stays in through the guest cookie (`GUEST_COOKIE_DAYS`, 1 day).

### The public guest page

The guest node serves the page where everybody adds songs and forwards search and queue calls to
the hub. Playback control, the audio files and the admin API are not reachable from it.

Guests get in by scanning the QR code on the player (see above) or, if `GUEST_ACCESS_CODE` is
set, through the fixed link `https://<your-domain>/k/<GUEST_ACCESS_CODE>` for printing.
The code is exchanged for a signed cookie that identifies the guest from then on, which is what
the limits are counted per:

| Setting | Default | Meaning |
| --- | --- | --- |
| `GUEST_MAX_OPEN` | 10 | Songs one guest may have waiting in the queue at once |
| `GUEST_ADD_PER_MIN` | 5 | Songs added per minute |
| `GUEST_ADD_PER_HOUR` | 40 | Songs added per hour |
| `GUEST_SEARCH_PER_MIN` | 30 | Searches per minute |
| `GUEST_STATE_PER_MIN` | 120 | Queue polls per minute |

Every limit is counted against the cookie *and* against the client IP, so neither clearing the
cookie nor sharing one gets past it. Request bodies over 4 KB are refused outright.

To run it on a VPS, use `deploy/vps/docker-compose.yml`. It pulls the image that CI pushes (see
below), so it works as a [Komodo](https://komo.do) stack as it is: point the stack at that file
(or paste it) and put the variables of `deploy/vps/.env.example` into the stack's environment,
`GUEST_IMAGE` included. Without Komodo, copy `deploy/vps/.env.example` to `deploy/vps/.env`,
fill it in and run `docker compose up -d` there. To build the image by hand instead:
`docker build -f deploy/vps/Dockerfile -t <image> .` from the repository root.

That starts a Tailscale sidecar and the guest container, which shares the sidecar's network
namespace: its only way out is the tailnet and its only way in is `127.0.0.1:${BIND_PORT}` on the
VPS, which your own reverse proxy terminates TLS for. The image carries no torch, no demucs and
no ffmpeg. Set `GUEST_PROXY_HOPS` to the number of proxies in front of it so the client IP the
limits use is the real one.

### CI

[.woodpecker.yml](.woodpecker.yml) runs on every push and pull request: `uv sync` of the base
install only (exactly what the guest image carries), ruff, the tests, and an import of the player and
guest node, which fails as soon as one of them starts to depend on something heavy. On `main` the
guest image is built from `deploy/vps/Dockerfile` and pushed as `:latest`. Komodo rolls it out on
its next redeploy (or with *Poll for updates* / *Auto update* on the stack). Required Woodpecker
secrets:

| Secret                         | Value                              |
| ------------------------------ | ---------------------------------- |
| `registry_url`                 | Registry host                      |
| `registry_username`            | Registry user                      |
| `registry_password`            | Registry password or token         |
| `registry_image_karaoke_guest` | Image repo for the guest node      |

### Lyrics

Lyrics are fetched from [LRCLIB](https://lrclib.net) and shown with a karaoke word wipe:

1. The line-synced LRC version is shown immediately.
2. In the background the lyrics are force-aligned against the separated vocal track
   ([MMS forced aligner](https://huggingface.co/MahmoudAshraf/mms-300m-1130-forced-aligner),
   ~1.3 GB, downloaded on first use) to get word-level timestamps. This also corrects a
   different intro length between the downloaded audio and the version LRCLIB timed.
3. Words are hyphenated with [Pyphen](https://github.com/Kozea/Pyphen) (German or English,
   guessed from the lyrics) and the wipe runs syllable by syllable, so a held note on one
   syllable no longer stretches the whole word evenly.

Alignment takes ~2 s on the Radeon 8060S and ~15 s on the CPU per song and is cached in
`storage/tracks/<isrc>.lyrics.json`. Set `LYRICS_ALIGN=0` to only use line-synced lyrics.
If the timing is off, use `,` / `.` (or the −/+ buttons) to shift lyrics by 0.1 s.

### Classical works and missing lyrics

LRCLIB rarely has classical texts, so the German texts of these works are bundled in
`src/karaoke/hub/librettos/`:

- Johann Sebastian Bach: Weihnachtsoratorium BWV 248 and Johannes-Passion BWV 245
  (texts from [bach-cantatas.com](https://www.bach-cantatas.com/))
- Felix Mendelssohn Bartholdy: Elias op. 70
- Johannes Brahms: Ein deutsches Requiem op. 45
  (text from [Wikisource](https://de.wikisource.org/wiki/Ein_deutsches_Requiem))

Track titles like *Weihnachtsoratorium, BWV 248, Pt. 1: No. 8, Großer Herr, o starker König* are
matched to their movement by its opening words, or by the movement number. These texts list
repeated lines only once, so for unsynced lyrics the alignment may jump back to earlier lines
and follow da capo arias and repeated choruses.

For any other song without (good) lyrics, use *Edit lyrics* in the admin UI (on the current song
and on every queued song): paste the text, one sung line per line, and it is synced to the vocals
again. Repeated lines only need to be listed once.

## Project layout

```
src/karaoke/
  cli.py, config.py      command line and settings (.env)
  common/                hub client, invite codes, Flask helpers - shared, no audio dependencies
  hub/                   session and queue, song download/separation/key, lyrics, librettos
  player/                player node with its local song cache
  guest/                 public guest node with rate limits
  templates/, static/    the web UIs
tests/                   tests of the light modules: uv run pytest
deploy/vps/              guest image and Docker Compose stack
```

Everything the hub produces lives in `storage/` (`KARAOKE_STORAGE`), so songs that were sung
before are ready immediately.

## Development

```bash
uv sync --extra rocm              # or cpu / cuda; the dev tools are included by default
uv run pytest
uv run ruff check src tests
```

## Credits and legal

This started as a fork of [mijorus/spotify-karaoke](https://github.com/mijorus/spotify-karaoke)
and has since been rewritten into a standalone party setup.

Audio is downloaded from YouTube for private use at your own party. Make sure this is allowed where
you live and respect the terms of the services involved.
