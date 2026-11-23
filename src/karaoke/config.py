"""Settings, read once from the environment (and .env, see karaoke.cli).

Kept free of heavy imports: the player and guest node import it too.
"""

import os
from pathlib import Path


def _str(name, default=''):
    return os.getenv(name) or default


def _int(name, default):
    return int(os.getenv(name) or default)


def _float(name, default):
    return float(os.getenv(name) or default)


PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / 'templates'
STATIC_DIR = PACKAGE_DIR / 'static'

# Songs, stems, lyrics, library and queue. Relative paths start at the working directory,
# i.e. the project directory when started with `uv run karaoke ...`.
STORAGE_DIR = Path(_str('KARAOKE_STORAGE', 'storage')).expanduser().resolve()
TRACKS_DIR = STORAGE_DIR / 'tracks'
SEPARATED_DIR = TRACKS_DIR / 'separated'
# demucs writes into a directory named after its model.
STEMS_DIR = SEPARATED_DIR / 'htdemucs'

# --- Ports of the three UIs (hub and player node) ---
HTTP_HOST = _str('HTTP_HOST', '127.0.0.1')
ADMIN_PORT = _int('ADMIN_PORT', 8080)
PLAYER_PORT = _int('PLAYER_PORT', 8081)
GUEST_PORT = _int('GUEST_PORT', 8082)

# --- Hub: audio processing ---
# cpu | cuda | mps. Empty = auto-detect; ROCm GPUs show up as cuda.
DEMUCS_DEVICE = _str('DEMUCS_DEVICE')
LYRICS_ALIGN = _str('LYRICS_ALIGN', '1') != '0'
LYRICS_ALIGN_MODEL = _str('LYRICS_ALIGN_MODEL', 'MahmoudAshraf/mms-300m-1130-forced-aligner')

# --- Split setup (hub on the strong PC, player on the laptop, guest on the VPS) ---

# Tailscale name or IP of the hub, e.g. brick. Its three APIs are independent of the ports
# this node serves on - a node may well listen on 8080 itself and talk to 8080 over there.
HUB_HOST = _str('HUB_HOST')
HUB_ADMIN_PORT = _int('HUB_ADMIN_PORT', 8080)
HUB_PLAYER_PORT = _int('HUB_PLAYER_PORT', 8081)
HUB_GUEST_PORT = _int('HUB_GUEST_PORT', 8082)
# Shared secret between hub and the remote nodes. Requests from outside localhost need it.
HUB_TOKEN = _str('HUB_TOKEN')
# How long a remote node waits for the hub before it falls back to its cache.
HUB_TIMEOUT = _float('HUB_TIMEOUT', 5)
# Proxy used for hub calls only. A Tailscale sidecar in userspace mode does not route the
# container's traffic, it offers an outbound HTTP proxy instead - this is where it goes.
HUB_PROXY = _str('HUB_PROXY')

# --- Player node (laptop): local cache of the songs that are about to play ---
CACHE_DIR = Path(_str('CACHE_DIR', str(STORAGE_DIR / 'cache'))).expanduser().resolve()
# Number of queued songs prefetched ahead of the current one.
PREFETCH_COUNT = _int('PREFETCH_COUNT', 3)
CACHE_MAX_GB = _float('CACHE_MAX_GB', 20)

# --- Guest node (VPS): the public page where everybody can queue songs ---
# Fixed code for a printed invite link (/k/<code>). Optional once GUEST_INVITE_SECRET is set.
GUEST_ACCESS_CODE = _str('GUEST_ACCESS_CODE')
# Shared by the guest node and whoever shows the QR on the screen: the invite code on the
# screen is derived from it and changes every GUEST_CODE_MINUTES (0 = never). Only entering
# needs a current code; a guest who got in once stays in through the cookie.
GUEST_INVITE_SECRET = _str('GUEST_INVITE_SECRET')
GUEST_CODE_MINUTES = max(0, _int('GUEST_CODE_MINUTES', 10))
GUEST_COOKIE_SECRET = _str('GUEST_COOKIE_SECRET')
GUEST_COOKIE_DAYS = _int('GUEST_COOKIE_DAYS', 1)
# A guest may have at most this many songs waiting in the queue at the same time.
GUEST_MAX_OPEN = _int('GUEST_MAX_OPEN', 10)
GUEST_SEARCH_PER_MIN = _int('GUEST_SEARCH_PER_MIN', 30)
GUEST_ADD_PER_MIN = _int('GUEST_ADD_PER_MIN', 5)
GUEST_ADD_PER_HOUR = _int('GUEST_ADD_PER_HOUR', 40)
GUEST_STATE_PER_MIN = _int('GUEST_STATE_PER_MIN', 120)
# Number of reverse proxies in front of the guest node, for a trustworthy client IP.
GUEST_PROXY_HOPS = _int('GUEST_PROXY_HOPS', 1)
# Public URL of the guest page, shown as a QR code on the player and in the admin UI. With
# GUEST_INVITE_SECRET it is the bare address (https://karaoke.example.com) and the rotating
# /k/<code> is appended; without it, the full invite link.
PUBLIC_GUEST_URL = _str('PUBLIC_GUEST_URL')
