"""Constants for the spotube (Spotify-catalog, YouTube-playback) provider."""

from __future__ import annotations

CONF_MARKET = "market"

# Spotify's track preview URL is 30s; we don't use it for playback (we go through
# YouTube) but keep the constant for the parsers.
SPOTIFY_URL = "https://open.spotify.com"
