"""
YouTube Music (Anonymous) music provider for Music Assistant.

Mirrors the approach of the noize app (anandssm/noize): unauthenticated InnerTube access
via `ytmusicapi` for metadata, plus `yt-dlp` to resolve bestaudio stream URLs just in time.
No cookies, no OAuth, no Premium.

Library sync is intentionally not implemented — anonymous access has no concept of
"my library". Users keep their favorites in Music Assistant's own library layer.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncGenerator, Sequence
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
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
    RecommendationFolder,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.models.music_provider import MusicProvider

from .constants import (
    CONF_AUDIO_QUALITY,
    CONF_COUNTRY,
    CONF_LANGUAGE,
    CONF_PO_TOKEN_SERVER_URL,
    DEFAULT_PO_TOKEN_SERVER_URL,
    DEFAULT_STREAM_URL_EXPIRATION,
)
from .helpers import (
    YTMSearchFilter,
    extract_stream_format,
    ytm_get_album,
    ytm_get_artist,
    ytm_get_artist_albums,
    ytm_get_home,
    ytm_get_mood_categories,
    ytm_get_playlist,
    ytm_get_track,
    ytm_get_watch_playlist,
    ytm_search,
)
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


SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.RECOMMENDATIONS,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SIMILAR_TRACKS,
}

_LOGGER = logging.getLogger(__name__)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialise the anonymous YouTube Music provider."""
    return YTMusicAnonymous(mass, manifest, config, SUPPORTED_FEATURES)


class YTMusicAnonymous(MusicProvider):
    """YouTube Music provider that mirrors the noize app — no account required."""

    @property
    def is_streaming_provider(self) -> bool:
        return True

    @property
    def language(self) -> str:
        return str(self.config.get_value(CONF_LANGUAGE) or "en")

    @property
    def country(self) -> str:
        return str(self.config.get_value(CONF_COUNTRY) or "US")

    @property
    def quality(self) -> str:
        return str(self.config.get_value(CONF_AUDIO_QUALITY) or "best")

    @property
    def po_token_server_url(self) -> str:
        # Empty string disables the PO-token path; coerce None/"" to None for the helper.
        value = (self.config.get_value(CONF_PO_TOKEN_SERVER_URL) or "").strip()
        return value or ""

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        return (
            ConfigEntry(
                key=CONF_LANGUAGE,
                type=ConfigEntryType.STRING,
                default_value="en",
                required=True,
                description=(
                    "Two-letter language code used for the YouTube Music catalog "
                    "(e.g. 'en', 'de', 'fr')."
                ),
            ),
            ConfigEntry(
                key=CONF_COUNTRY,
                type=ConfigEntryType.STRING,
                default_value="US",
                required=True,
                description=(
                    "Two-letter country code shaping regional charts and trending. "
                    "Currently informational only — YouTube Music derives the region "
                    "from the IP address of the request."
                ),
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_AUDIO_QUALITY,
                type=ConfigEntryType.STRING,
                default_value="best",
                required=True,
                options=[
                    ConfigValueOption("best", "Best (default)"),
                    ConfigValueOption("medium", "Medium (<=128 kbps)"),
                    ConfigValueOption("low", "Low (worst audio)"),
                ],
                description=(
                    "Audio quality ceiling. Anonymous access caps around 128 kbps opus/m4a "
                    "regardless of this setting; the lower options exist for metered "
                    "connections."
                ),
            ),
            ConfigEntry(
                key=CONF_PO_TOKEN_SERVER_URL,
                type=ConfigEntryType.STRING,
                default_value=DEFAULT_PO_TOKEN_SERVER_URL,
                required=False,
                description=(
                    "URL of a bgutil-ytdlp-pot-provider server. The upstream Music "
                    "Assistant addon repo ships one as \"YouTube Music PO Token "
                    "Generator\"; install and start it, then leave this at the "
                    "default (both add-ons share host_network). Without a PO token "
                    "server, YouTube only returns image-only formats for most "
                    "tracks and playback will fail."
                ),
            ),
        )

    # ---------- search / browse ----------

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        results = SearchResults()
        # ytmusicapi only accepts a single filter per call; search once per type we care about.
        filter_map: dict[MediaType, YTMSearchFilter] = {
            MediaType.ARTIST: "artists",
            MediaType.ALBUM: "albums",
            MediaType.TRACK: "songs",
            MediaType.PLAYLIST: "playlists",
        }
        parsed_artists: list[Artist | ItemMapping] = []
        parsed_albums: list[Album | ItemMapping] = []
        parsed_tracks: list[Track | ItemMapping] = []
        parsed_playlists: list[Playlist | ItemMapping] = []

        for mt in media_types:
            ytm_filter = filter_map.get(mt)
            if not ytm_filter:
                continue
            raw = await ytm_search(search_query, self.language, ytm_filter=ytm_filter, limit=limit)
            for item in raw:
                rtype = item.get("resultType")
                try:
                    if rtype == "artist" and mt == MediaType.ARTIST:
                        parsed_artists.append(parse_artist(self.instance_id, self.domain, item))
                    elif rtype == "album" and mt == MediaType.ALBUM:
                        parsed_albums.append(parse_album(self.instance_id, self.domain, item))
                    elif rtype == "song" and mt == MediaType.TRACK:
                        parsed_tracks.append(parse_track(self.instance_id, self.domain, item))
                    elif rtype == "playlist" and mt == MediaType.PLAYLIST:
                        parsed_playlists.append(parse_playlist(self.instance_id, self.domain, item))
                except ValueError as err:
                    self.logger.debug("Skipping unparseable %s result: %s", rtype, err)

        results.artists = parsed_artists
        results.albums = parsed_albums
        results.tracks = parsed_tracks
        results.playlists = parsed_playlists
        return results

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse into the home feed sections and mood categories."""
        # path shape: "<domain>://" (root) | "<domain>://home" | "<domain>://moods"
        prefix = f"{self.instance_id}://"
        sub = path[len(prefix):] if path.startswith(prefix) else ""

        if sub in ("", "/"):
            return [
                BrowseFolder(
                    item_id="home",
                    provider=self.instance_id,
                    name="Home",
                    path=f"{prefix}home",
                ),
                BrowseFolder(
                    item_id="moods",
                    provider=self.instance_id,
                    name="Moods & Genres",
                    path=f"{prefix}moods",
                ),
            ]
        if sub == "home":
            return await self._home_as_browse_folders(prefix)
        if sub == "moods":
            return await self._moods_as_browse_folders(prefix)
        return []

    async def _home_as_browse_folders(self, prefix: str) -> list[BrowseFolder]:
        sections = await ytm_get_home(self.language, limit=5)
        out: list[BrowseFolder] = []
        for i, section in enumerate(sections):
            title = section.get("title") or f"Section {i + 1}"
            out.append(
                BrowseFolder(
                    item_id=f"home_{i}",
                    provider=self.instance_id,
                    name=title,
                    path=f"{prefix}home_{i}",
                )
            )
        return out

    async def _moods_as_browse_folders(self, prefix: str) -> list[BrowseFolder]:
        categories = await ytm_get_mood_categories(self.language)
        out: list[BrowseFolder] = []
        for group, items in categories.items():
            for item in items:
                out.append(
                    BrowseFolder(
                        item_id=item.get("params", item.get("title", "")),
                        provider=self.instance_id,
                        name=f"{group}: {item.get('title', '')}",
                        path=f"{prefix}mood_{item.get('params', '')}",
                    )
                )
        return out

    # ---------- recommendations ----------

    async def get_recommendations(self) -> list[RecommendationFolder]:
        # Row descriptors only; items fetched on demand via get_recommendation_items.
        home = await ytm_get_home(self.language, limit=1)
        folders: list[RecommendationFolder] = []
        for section in home:
            title = section.get("title")
            if not title:
                continue
            folders.append(
                RecommendationFolder(
                    item_id=f"{self.instance_id}_{title}",
                    provider=self.instance_id,
                    name=title,
                    icon="mdi:playlist-music",
                )
            )
        return folders

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        home = await ytm_get_home(self.language, limit=5)
        prefix = f"{self.instance_id}_"
        wanted_title = item_id[len(prefix):] if item_id.startswith(prefix) else item_id
        for section in home:
            if section.get("title") != wanted_title:
                continue
            return UniqueList(self._parse_home_section_items(section))
        return UniqueList()

    def _parse_home_section_items(
        self, section: dict
    ) -> list[MediaItemType | ItemMapping | BrowseFolder]:
        out: list[MediaItemType | ItemMapping | BrowseFolder] = []
        for item in section.get("contents", []):
            try:
                if item.get("videoId"):
                    out.append(parse_track(self.instance_id, self.domain, item))
                elif item.get("playlistId") or (
                    item.get("resultType") == "playlist"
                ):
                    out.append(parse_playlist(self.instance_id, self.domain, item))
                elif item.get("browseId") and item.get("type") in ("Album", "Single", "EP"):
                    out.append(parse_album(self.instance_id, self.domain, item))
                elif item.get("browseId"):
                    out.append(parse_artist(self.instance_id, self.domain, item))
            except ValueError as err:
                self.logger.debug("Skipping home item: %s", err)
        return out

    # ---------- item lookups ----------

    async def get_artist(self, prov_artist_id: str) -> Artist:
        raw = await ytm_get_artist(prov_artist_id, self.language)
        return parse_artist(self.instance_id, self.domain, raw)

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        raw = await ytm_get_artist_albums(prov_artist_id, self.language)
        out: list[Album] = []
        for album_obj in raw:
            try:
                out.append(parse_album(self.instance_id, self.domain, album_obj))
            except ValueError:
                continue
        return out

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        raw = await ytm_get_artist(prov_artist_id, self.language)
        songs = (raw.get("songs") or {}).get("results") or []
        out: list[Track] = []
        for song in songs:
            try:
                out.append(parse_track(self.instance_id, self.domain, song))
            except ValueError:
                continue
        return out

    async def get_album(self, prov_album_id: str) -> Album:
        raw = await ytm_get_album(prov_album_id, self.language)
        return parse_album(self.instance_id, self.domain, raw, album_id=prov_album_id)

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        raw = await ytm_get_album(prov_album_id, self.language)
        out: list[Track] = []
        for i, track_obj in enumerate(raw.get("tracks") or [], start=1):
            try:
                out.append(parse_track(self.instance_id, self.domain, track_obj, track_number=i))
            except ValueError:
                continue
        return out

    async def get_track(self, prov_track_id: str) -> Track:
        raw = await ytm_get_track(prov_track_id, self.language)
        if raw is None:
            raise MediaNotFoundError(f"Track {prov_track_id} not found on YouTube Music")
        return parse_track(self.instance_id, self.domain, raw)

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        raw = await ytm_get_playlist(prov_playlist_id, self.language, limit=0)
        return parse_playlist(self.instance_id, self.domain, raw)

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        # ytmusicapi has no page concept — fetch everything on page 0, nothing on later pages.
        if page > 0:
            return []
        raw = await ytm_get_playlist(prov_playlist_id, self.language, limit=None)
        out: list[Track] = []
        for i, track_obj in enumerate(raw.get("tracks") or [], start=1):
            try:
                parsed = parse_track(self.instance_id, self.domain, track_obj, track_number=i)
                parsed.position = i
                out.append(parsed)
            except ValueError:
                continue
        return out

    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        raw = await ytm_get_watch_playlist(prov_track_id, self.language, limit=limit)
        tracks_raw = raw.get("tracks") or []
        out: list[Track] = []
        for t in tracks_raw[1:]:  # first entry is the seed track itself
            # watch_playlist uses "thumbnail" (singular) and "length" instead of "duration"
            if "thumbnail" in t and "thumbnails" not in t:
                t["thumbnails"] = t.pop("thumbnail")
            if "length" in t and "duration" not in t:
                t["duration"] = t["length"]
            try:
                out.append(parse_track(self.instance_id, self.domain, t))
            except ValueError:
                continue
            if len(out) >= limit:
                break
        return out

    # ---------- streaming ----------

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Resolve the bestaudio URL for the given videoId.

        yt-dlp returns a signed URL that expires (usually within a few hours). The expiry
        is embedded in the `expire` query parameter — we propagate it so Music Assistant
        can re-resolve rather than caching a dead URL.
        """
        fmt = await extract_stream_format(
            item_id,
            quality=self.quality,
            po_token_server_url=self.po_token_server_url or None,
        )
        url = fmt["url"]

        expiration = DEFAULT_STREAM_URL_EXPIRATION
        if parsed := parse_qs(urlparse(url).query):
            if expire_ts := parsed.get("expire", [None])[0]:
                try:
                    expiration = max(60, int(expire_ts) - int(time.time()))
                except ValueError:
                    pass

        audio_format = AudioFormat(
            content_type=ContentType.try_parse(fmt.get("audio_ext") or "unknown"),
        )
        if (channels := fmt.get("audio_channels")) and str(channels).isdigit():
            audio_format.channels = int(channels)
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
            # YouTube paces delivery to real-time; mark the stream so MA doesn't
            # try to read ahead faster than playback.
            is_realtime=True,
        )
