"""
Spotube music provider for Music Assistant.

Mirrors the team-spotube/spotube approach: catalog & playlists come from the
Spotify catalog; audio playback is YouTube, resolved on demand via yt-dlp.

Spotify access is anonymous via `spotapi` (no client_id, no login) — same
pattern spotdl ships. Playback goes through the ytmusic_anon provider's
yt-dlp pipeline, so the two providers share the tv_simply client config and
the deno runtime.

Pasted Spotify URLs or URIs in the search box short-circuit to a direct
lookup, so e.g. ``https://open.spotify.com/playlist/37i9…`` resolves that
playlist instead of a text search.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemType,
    Playlist,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.models.music_provider import MusicProvider

from .constants import CONF_MARKET
from .helpers import (
    sp_album,
    sp_artist,
    sp_artist_albums,
    sp_playlist,
    sp_search_artists,
    sp_search_tracks,
    sp_track,
    ytm_search_candidates,
)
from .matcher import pick_best
from .parsers import (
    parse_album,
    parse_artist,
    parse_playlist,
    parse_track,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

_LOGGER = logging.getLogger(__name__)

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
}

# Spotify URL and URI shapes we handle in search(). Captures (kind, id).
_SPOTIFY_URL_RE = re.compile(
    r"https?://open\.spotify\.com/(?:intl-[a-z]{2}/)?(track|album|playlist|artist)/([A-Za-z0-9]+)",
    re.IGNORECASE,
)
_SPOTIFY_URI_RE = re.compile(r"spotify:(track|album|playlist|artist):([A-Za-z0-9]+)")

# Default TTL for a resolved YouTube stream URL when the "expire" query param is absent.
DEFAULT_STREAM_URL_EXPIRATION = 3600


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialise the spotube provider — anonymous, no credentials required."""
    return SpotubeProvider(mass, manifest, config, SUPPORTED_FEATURES)


class SpotubeProvider(MusicProvider):
    """Spotify catalog (anonymous via spotapi), YouTube playback (via yt-dlp)."""

    @property
    def is_streaming_provider(self) -> bool:
        return True

    @property
    def market(self) -> str:
        return str(self.config.get_value(CONF_MARKET) or "US")

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        return (
            ConfigEntry(
                key=CONF_MARKET,
                type=ConfigEntryType.STRING,
                default_value="US",
                required=True,
                description=(
                    "Two-letter ISO country code for Spotify catalog availability "
                    "(e.g. US, GB, DE). Spotify derives the market from the request's "
                    "IP by default; this is informational for the UI."
                ),
                advanced=True,
            ),
        )

    # ---------------- search ----------------

    def _parse_spotify_url_or_uri(self, text: str) -> tuple[str, str] | None:
        text = text.strip()
        if m := _SPOTIFY_URL_RE.search(text):
            return m.group(1).lower(), m.group(2)
        if m := _SPOTIFY_URI_RE.search(text):
            return m.group(1).lower(), m.group(2)
        return None

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        results = SearchResults()

        # Pasted URL/URI: direct lookup. Respect the caller's media_types filter so
        # we don't surface the same item on every per-media-type search() call MA makes
        # (which otherwise shows the playlist 4x on a Spotify URL search).
        if parsed := self._parse_spotify_url_or_uri(search_query):
            kind, item_id = parsed
            kind_to_type = {
                "track": MediaType.TRACK,
                "album": MediaType.ALBUM,
                "playlist": MediaType.PLAYLIST,
                "artist": MediaType.ARTIST,
            }
            expected = kind_to_type.get(kind)
            if expected is None or expected not in media_types:
                return results  # URL is for a type the caller didn't ask for; stay empty.
            try:
                if kind == "track":
                    results.tracks = [await self.get_track(item_id)]
                elif kind == "album":
                    results.albums = [await self.get_album(item_id)]
                elif kind == "playlist":
                    results.playlists = [await self.get_playlist(item_id)]
                elif kind == "artist":
                    results.artists = [await self.get_artist(item_id)]
            except Exception as err:  # noqa: BLE001
                self.logger.debug("Spotify URL short-circuit failed for %s: %s", kind, err)
            return results

        # Anonymous spotapi only exposes track and artist text search; album/playlist
        # discovery via Spotify needs auth. Users paste album/playlist URLs instead.
        want_tracks = MediaType.TRACK in media_types
        want_artists = MediaType.ARTIST in media_types
        if not (want_tracks or want_artists):
            return results

        # Run both legs in parallel — spotapi's cold-start is slow enough that
        # running them sequentially sometimes trips MA's per-provider search timeout.
        track_task = (
            asyncio.create_task(sp_search_tracks(search_query, limit=limit))
            if want_tracks
            else None
        )
        artist_task = (
            asyncio.create_task(sp_search_artists(search_query, limit=limit))
            if want_artists
            else None
        )
        if track_task is not None:
            try:
                results.tracks = self._parse_many(parse_track, await track_task)
            except Exception as err:  # noqa: BLE001
                self.logger.debug("Spotube track search failed: %s", err)
        if artist_task is not None:
            try:
                results.artists = self._parse_many(parse_artist, await artist_task)
            except Exception as err:  # noqa: BLE001
                self.logger.debug("Spotube artist search failed: %s", err)
        return results

    def _parse_many(self, parser, items):
        out = []
        for item in items:
            try:
                out.append(parser(self.instance_id, self.domain, item))
            except ValueError as err:
                self.logger.debug("Skipping unparseable Spotify item: %s", err)
        return out

    # ---------------- item lookups ----------------

    async def get_artist(self, prov_artist_id: str) -> Artist:
        raw = await sp_artist(prov_artist_id)
        return parse_artist(self.instance_id, self.domain, raw)

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        raw = await sp_artist_albums(prov_artist_id)
        return self._parse_many(parse_album, raw)

    async def get_album(self, prov_album_id: str) -> Album:
        raw = await sp_album(prov_album_id)
        return parse_album(self.instance_id, self.domain, raw)

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        raw = await sp_album(prov_album_id)
        tracks: list[Track] = []
        for i, t in enumerate(raw.get("tracks") or [], start=1):
            try:
                parsed = parse_track(self.instance_id, self.domain, t)
                if not parsed.track_number:
                    parsed.track_number = i
                tracks.append(parsed)
            except ValueError:
                continue
        return tracks

    async def get_track(self, prov_track_id: str) -> Track:
        raw = await sp_track(prov_track_id)
        if not raw.get("id"):
            raise MediaNotFoundError(f"Spotify track {prov_track_id} not found")
        return parse_track(self.instance_id, self.domain, raw)

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        raw = await sp_playlist(prov_playlist_id)
        return parse_playlist(self.instance_id, self.domain, raw)

    async def get_playlist_tracks(
        self, prov_playlist_id: str, page: int = 0
    ) -> list[Track]:
        if page > 0:
            return []
        raw = await sp_playlist(prov_playlist_id)
        out: list[Track] = []
        for pos, item in enumerate(raw.get("tracks") or [], start=1):
            try:
                parsed = parse_track(self.instance_id, self.domain, item)
                parsed.position = pos
                out.append(parsed)
            except ValueError as err:
                self.logger.debug("Skipping playlist track at pos %d: %s", pos, err)
        return out

    # ---------------- streaming ----------------

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Resolve a YouTube stream for a Spotify track id.

        Flow (same as spotube's SourcedTrack):
          1. Fetch the Spotify track to get name/artists.
          2. Search YouTube Music for candidates with ``<primary_artist> <track_name>``.
          3. Score with the spotube additive heuristic and pick the top.
          4. Resolve the chosen video_id via ytmusic_anon.helpers.extract_stream_format
             (shared tv_simply client + deno + URL-expiry handling).
        """
        from music_assistant.providers.ytmusic_anon.helpers import extract_stream_format

        sp_track_obj = await sp_track(item_id)
        if not sp_track_obj.get("id"):
            raise MediaNotFoundError(f"Spotify track {item_id} not found")

        candidates = await ytm_search_candidates(sp_track_obj, limit=10)
        if not candidates:
            raise MediaNotFoundError(
                f"No YouTube match for Spotify track {item_id} "
                f"('{sp_track_obj.get('name')}')"
            )
        best = pick_best(sp_track_obj, candidates)
        if not best:
            raise MediaNotFoundError(
                f"Could not rank a best YouTube match for Spotify track {item_id}"
            )
        self.logger.debug(
            "Spotube match: spotify=%s(%s) -> yt=%s(%s)",
            sp_track_obj.get("name"),
            item_id,
            best.title,
            best.video_id,
        )

        fmt = await extract_stream_format(best.video_id)
        url = fmt["url"]
        expiration = DEFAULT_STREAM_URL_EXPIRATION
        if parsed_q := parse_qs(urlparse(url).query):
            if expire_ts := parsed_q.get("expire", [None])[0]:
                try:
                    expiration = max(60, int(expire_ts) - int(time.time()))
                except ValueError:
                    pass

        audio_format = AudioFormat(
            content_type=ContentType.try_parse(fmt.get("audio_ext") or "unknown"),
        )
        if (ch := fmt.get("audio_channels")) and str(ch).isdigit():
            audio_format.channels = int(ch)
        if asr := fmt.get("asr"):
            audio_format.sample_rate = int(asr)

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=audio_format,
            media_type=MediaType.TRACK,
            stream_type=StreamType.HTTP,
            path=url,
            can_seek=True,
            allow_seek=True,
            expiration=expiration,
            is_realtime=True,
        )

    # ---------------- browse ----------------

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        return []
