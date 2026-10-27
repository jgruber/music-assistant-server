"""JW.org Music provider for Music Assistant.

Serves the public music catalog from jw.org as a browsable and searchable
set of albums and tracks. Fetches each publication's track list from the
official pub-media endpoint, caches it in memory for a day, and resolves
playback to the direct MP3 URLs the site itself publishes.

No account, no authentication, no scraping of HTML pages — just the same
JSON endpoint the site's download buttons use. Only factual metadata
(title, duration, bitrate, track number, URL) is returned.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.models.music_provider import MusicProvider
from music_assistant_models.enums import (
    AlbumType,
    ContentType,
    ImageType,
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
    MediaItemImage,
    MediaItemType,
    ProviderMapping,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from .constants import CATEGORIES, CATEGORY_BY_PUB, PUBLISHER
from .helpers import CatalogCache, JwAlbum, JwTrack, decode_track_id, encode_track_id

if TYPE_CHECKING:
    from collections.abc import Sequence

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest


SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize the provider instance with the given configuration."""
    return JwMusicProvider(mass, manifest, config, SUPPORTED_FEATURES)


class JwMusicProvider(MusicProvider):
    """MA music provider over the public jw.org pub-media endpoint."""

    _catalog: CatalogCache | None = None

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the set of features this provider supports."""
        return SUPPORTED_FEATURES

    @property
    def is_streaming_provider(self) -> bool:
        """We serve HTTP streams (direct MP3), not local files."""
        return True

    async def loaded_in_mass(self) -> None:
        """Create the catalog cache now that mass.http_session is ready."""
        self._catalog = CatalogCache(self.mass.http_session)

    def _cache(self) -> CatalogCache:
        if self._catalog is None:
            self._catalog = CatalogCache(self.mass.http_session)
        return self._catalog

    # ---------- browse ----------

    async def browse(
        self, path: str
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse into the provider.

        The root listing shows one album per publication (mirroring the cards
        at jw.org/library/music-songs/). An album path yields that album's
        tracks. Paths look like ``jwmusic://`` for the root and
        ``jwmusic://albums/<pub>`` for a specific album.
        """
        # path format: "<provider_instance_id>://..."
        _, _, sub = path.partition("://")
        if not sub or sub.strip("/") == "" or sub.strip("/") == "albums":
            # Return full Album objects (not ItemMappings) so album_type and
            # artwork render in the row. Albums are fetched concurrently and
            # cached, so later browses are instant.
            albums = await self._cache().get_many([c.pub for c in CATEGORIES])
            return [self._album_full(a) for a in albums]
        parts = sub.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "albums":
            pub = parts[1]
            album = await self._cache().get(pub)
            return [self._track_item(album, t) for t in album.tracks]
        # unknown subpath — show the top-level listing
        albums = await self._cache().get_many([c.pub for c in CATEGORIES])
        return [self._album_full(a) for a in albums]

    # ---------- search ----------

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 10,
    ) -> SearchResults:
        """
        Dynamic substring search over every album's track list.

        First call warms the cache for every publication (concurrent); every
        later call within the TTL filters cached entries in-memory. Album
        matches are returned too so the user can jump into a publication by
        name without first browsing.
        """
        query = search_query.strip().lower()
        if not query:
            return SearchResults()
        want_tracks = not media_types or MediaType.TRACK in media_types
        want_albums = not media_types or MediaType.ALBUM in media_types
        if not (want_tracks or want_albums):
            return SearchResults()
        albums = await self._cache().get_many([c.pub for c in CATEGORIES])
        results = SearchResults()
        if want_albums:
            matched_albums: list[Album] = []
            for album in albums:
                display = CATEGORY_BY_PUB.get(album.pub)
                display_name = display.name if display else album.name
                if query in display_name.lower() or query in album.name.lower():
                    matched_albums.append(self._album_full(album))
                if len(matched_albums) >= limit:
                    break
            results.albums = UniqueList(matched_albums)
        if want_tracks:
            matched_tracks: list[Track] = []
            for album in albums:
                for t in album.tracks:
                    if query in t.title.lower():
                        matched_tracks.append(self._track_full(album, t))
                        if len(matched_tracks) >= limit:
                            break
                if len(matched_tracks) >= limit:
                    break
            results.tracks = UniqueList(matched_tracks)
        return results

    # ---------- item lookups ----------

    async def get_album(self, prov_album_id: str) -> Album:
        """Return a full album object for the given pub code."""
        if prov_album_id not in CATEGORY_BY_PUB:
            raise MediaNotFoundError(f"Unknown album '{prov_album_id}'")
        album = await self._cache().get(prov_album_id)
        return self._album_full(album)

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Return every track in the given album, in track order."""
        if prov_album_id not in CATEGORY_BY_PUB:
            raise MediaNotFoundError(f"Unknown album '{prov_album_id}'")
        album = await self._cache().get(prov_album_id)
        return [self._track_full(album, t) for t in album.tracks]

    async def get_track(self, prov_track_id: str) -> Track:
        """Return a full track object, resolving against the album cache."""
        try:
            pub, docid = decode_track_id(prov_track_id)
        except ValueError as err:
            raise MediaNotFoundError(str(err)) from err
        if pub not in CATEGORY_BY_PUB:
            raise MediaNotFoundError(
                f"Unknown album '{pub}' on track '{prov_track_id}'"
            )
        album = await self._cache().get(pub)
        for t in album.tracks:
            if t.docid == docid:
                return self._track_full(album, t)
        raise MediaNotFoundError(f"Track '{prov_track_id}' not found")

    # ---------- streaming ----------

    async def get_stream_details(
        self, item_id: str, media_type: MediaType
    ) -> StreamDetails:
        """Resolve a track id to its direct MP3 URL."""
        if media_type != MediaType.TRACK:
            raise MediaNotFoundError(f"Unsupported media type {media_type}")
        try:
            pub, docid = decode_track_id(item_id)
        except ValueError as err:
            raise MediaNotFoundError(str(err)) from err
        album = await self._cache().get(pub)
        for t in album.tracks:
            if t.docid == docid:
                return StreamDetails(
                    item_id=item_id,
                    provider=self.instance_id,
                    audio_format=AudioFormat(
                        content_type=ContentType.try_parse(t.mimetype or "audio/mpeg"),
                        bit_rate=t.bitrate_kbps or 128,
                    ),
                    media_type=MediaType.TRACK,
                    stream_type=StreamType.HTTP,
                    path=t.url,
                    duration=t.duration or None,
                    size=t.filesize or None,
                    can_seek=True,
                    allow_seek=True,
                )
        raise MediaNotFoundError(f"Track '{item_id}' not found")

    # ---------- builders ----------

    def _album_display_name(self, album: JwAlbum) -> str:
        category = CATEGORY_BY_PUB.get(album.pub)
        return category.name if category else (album.name or album.pub)

    def _album_artist(self) -> Artist:
        """A single stable "artist" for every album — the publisher."""
        return Artist(
            item_id=self.domain,
            provider=self.instance_id,
            name=PUBLISHER,
            provider_mappings={
                ProviderMapping(
                    item_id=self.domain,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

    def _album_to_item_mapping(self, pub: str, display_name: str) -> ItemMapping:
        """Lightweight representation used in browse listings."""
        return ItemMapping(
            media_type=MediaType.ALBUM,
            item_id=pub,
            provider=self.instance_id,
            name=display_name,
            available=True,
        )

    def _album_full(self, album: JwAlbum) -> Album:
        """Build a full Album with provider mappings and image if present."""
        result = Album(
            item_id=album.pub,
            provider=self.instance_id,
            name=self._album_display_name(album),
            album_type=AlbumType.ALBUM,
            artists=UniqueList([self._album_artist()]),
            provider_mappings={
                ProviderMapping(
                    item_id=album.pub,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        if album.image_url:
            result.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=album.image_url,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )
        return result

    def _track_full(self, album: JwAlbum, track: JwTrack) -> Track:
        """Build a full Track pointing at the album and the publisher-artist."""
        item_id = encode_track_id(track.pub, track.docid)
        result = Track(
            item_id=item_id,
            provider=self.instance_id,
            name=track.title or f"Track {track.track_number}",
            duration=track.duration or None,
            artists=UniqueList([self._album_artist()]),
            provider_mappings={
                ProviderMapping(
                    item_id=item_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(
                        content_type=ContentType.try_parse(
                            track.mimetype or "audio/mpeg"
                        ),
                        bit_rate=track.bitrate_kbps or 128,
                    ),
                )
            },
        )
        result.track_number = track.track_number or None
        # track.album is an ItemMapping (lightweight pointer), not a full Album.
        result.album = ItemMapping(
            media_type=MediaType.ALBUM,
            item_id=album.pub,
            provider=self.instance_id,
            name=self._album_display_name(album),
            available=True,
        )
        if track.image_url:
            result.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=track.image_url,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )
        return result

    def _track_item(self, album: JwAlbum, track: JwTrack) -> ItemMapping:
        """Lightweight representation used in browse listings."""
        return ItemMapping(
            media_type=MediaType.TRACK,
            item_id=encode_track_id(track.pub, track.docid),
            provider=self.instance_id,
            name=track.title or f"Track {track.track_number}",
            available=True,
        )
