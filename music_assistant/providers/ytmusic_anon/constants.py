"""Constants for the anonymous YouTube Music provider."""

from __future__ import annotations

YTM_DOMAIN = "https://music.youtube.com"
YT_DOMAIN = "https://www.youtube.com"

# videoId used by YTM for the Various Artists channel. Imported from the official provider
# to keep the two in sync.
VARIOUS_ARTISTS_YTM_ID = "UCUTXlgdcKU5vfzFqHOWIvkA"

# Default TTL for a resolved stream URL when the "expire" query param is missing.
DEFAULT_STREAM_URL_EXPIRATION = 3600

CONF_LANGUAGE = "language"
CONF_COUNTRY = "country"
CONF_AUDIO_QUALITY = "audio_quality"
CONF_PO_TOKEN_SERVER_URL = "po_token_server_url"

# bgutil-ytdlp-pot-provider's addon (ytm_po_token_generator) ships with host_network,
# so it is reachable from this add-on's container at localhost:4416. Expose as the
# default; empty value disables PO entirely and we fall back to tv_simply/mweb.
DEFAULT_PO_TOKEN_SERVER_URL = "http://127.0.0.1:4416"

# yt-dlp format selectors keyed by the config value. "best" matches what the real provider
# uses; "medium"/"low" let users cap bandwidth without needing to think in format ids.
QUALITY_FORMAT_SELECTORS = {
    "best": "m4a/bestaudio",
    "medium": "bestaudio[abr<=128]/m4a",
    "low": "worstaudio[abr>=48]/worstaudio",
}
