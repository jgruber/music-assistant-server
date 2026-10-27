"""
Translate ytmusicapi JSON payloads into Music Assistant media_items.

These are plain functions (not methods) that take the owning provider's `instance_id` and
`domain`, so they are trivially unit-testable. Logic mirrors the real ytmusic provider's
_parse_* methods, trimmed to what an anonymous feed actually returns (no favorite flags,
no library-upload branches).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote

from music_assistant_models.enums import AlbumType, ContentType, ImageType, MediaType
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    Track,
    UniqueList,
)

from .constants import VARIOUS_ARTISTS_YTM_ID, YTM_DOMAIN


def _artist_is_resolvable(artist: dict[str, Any]) -> bool:
    """Reject the id-less artist stubs that appear on some search results."""
    if artist.get("id") or artist.get("channelId"):
        return True
    return artist.get("name") == "Various Artists"


def _item_mapping(instance_id: str, media_type: MediaType, key: str, name: str) -> ItemMapping:
    return ItemMapping(media_type=media_type, item_id=key, provider=instance_id, name=name)


def _artist_item_mapping(instance_id: str, artist_obj: dict[str, Any]) -> ItemMapping:
    artist_id = artist_obj.get("id") or artist_obj.get("channelId")
    if not artist_id and artist_obj.get("name") == "Various Artists":
        artist_id = VARIOUS_ARTISTS_YTM_ID
    return _item_mapping(instance_id, MediaType.ARTIST, str(artist_id), artist_obj.get("name", ""))


def parse_thumbnails(
    instance_id: str, thumbnails: list[dict[str, Any]]
) -> UniqueList[MediaItemImage]:
    """Convert YTM thumbnail list to MediaItemImage list, biggest first."""
    out: UniqueList[MediaItemImage] = UniqueList()
    seen: set[str] = set()
    for img in sorted(thumbnails, key=lambda w: w.get("width", 0), reverse=True):
        url: str = img.get("url", "")
        if not url:
            continue
        width = img.get("width") or 0
        height = img.get("height") or 0
        if not width or not height:
            continue
        ratio = width / height
        image_type = ImageType.LANDSCAPE if ratio > 2.0 or "maxresdefault" in url else ImageType.THUMB
        # YT thumbnail URLs embed the size as "=wNNNN"; we can request a larger
        # version by rewriting that segment.
        if "=w" in url and width < 600:
            url = url.split("=w", maxsplit=1)[0] + "=w600-h600-p"
            image_type = ImageType.THUMB
        elif "=w" not in url and width < 400:
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(
            MediaItemImage(
                type=image_type,
                path=url,
                provider=instance_id,
                remotely_accessible=True,
            )
        )
    return out


def parse_artist(instance_id: str, domain: str, artist_obj: dict[str, Any]) -> Artist:
    artist_id = (
        artist_obj.get("channelId")
        or artist_obj.get("id")
        or artist_obj.get("browseId")
    )
    if not artist_id and artist_obj.get("name") == "Various Artists":
        artist_id = VARIOUS_ARTISTS_YTM_ID
    if not artist_id:
        raise ValueError("Artist has no resolvable id")

    artist = Artist(
        item_id=str(artist_id),
        name=artist_obj.get("name") or artist_obj.get("artist") or "",
        provider=instance_id,
        provider_mappings={
            ProviderMapping(
                item_id=str(artist_id),
                provider_domain=domain,
                provider_instance=instance_id,
                url=f"{YTM_DOMAIN}/channel/{artist_id}",
            )
        },
    )
    if description := artist_obj.get("description"):
        artist.metadata.description = description
    if thumbs := artist_obj.get("thumbnails"):
        artist.metadata.images = parse_thumbnails(instance_id, thumbs)
    return artist


def parse_album(
    instance_id: str,
    domain: str,
    album_obj: dict[str, Any],
    album_id: str | None = None,
) -> Album:
    album_id = album_id or album_obj.get("id") or album_obj.get("browseId")
    if not album_id:
        raise ValueError("Album has no resolvable id")

    name = album_obj.get("title") or album_obj.get("name") or ""
    audio_playlist_id = album_obj.get("audioPlaylistId")
    album = Album(
        item_id=str(album_id),
        name=name,
        provider=instance_id,
        provider_mappings={
            ProviderMapping(
                item_id=str(album_id),
                provider_domain=domain,
                provider_instance=instance_id,
                url=(
                    f"{YTM_DOMAIN}/playlist?list={audio_playlist_id}"
                    if audio_playlist_id
                    else f"{YTM_DOMAIN}/browse/{album_id}"
                ),
            )
        },
    )
    year = album_obj.get("year")
    if year and str(year).isdigit():
        album.year = int(year)
    if thumbs := album_obj.get("thumbnails"):
        album.metadata.images = parse_thumbnails(instance_id, thumbs)
    if description := album_obj.get("description"):
        album.metadata.description = unquote(description)
    if "isExplicit" in album_obj:
        album.metadata.explicit = album_obj["isExplicit"]
    if artists := album_obj.get("artists"):
        album.artists = UniqueList(
            _artist_item_mapping(instance_id, a) for a in artists if _artist_is_resolvable(a)
        )

    yt_type = album_obj.get("type")
    if yt_type == "Single":
        album.album_type = AlbumType.SINGLE
    elif yt_type == "EP":
        album.album_type = AlbumType.EP
    elif yt_type == "Album":
        album.album_type = AlbumType.ALBUM
    return album


def parse_playlist(instance_id: str, domain: str, playlist_obj: dict[str, Any]) -> Playlist:
    playlist_id = playlist_obj.get("id") or playlist_obj.get("playlistId") or playlist_obj.get("browseId")
    if not playlist_id:
        raise ValueError("Playlist has no resolvable id")

    name = playlist_obj.get("title") or playlist_obj.get("name") or ""
    # Anonymous access can't edit anything, so is_editable stays False and all mappings
    # are non-unique (every instance of this provider sees the same catalog).
    playlist = Playlist(
        item_id=str(playlist_id),
        provider=instance_id,
        name=name,
        provider_mappings={
            ProviderMapping(
                item_id=str(playlist_id),
                provider_domain=domain,
                provider_instance=instance_id,
                url=f"{YTM_DOMAIN}/playlist?list={playlist_id}",
            )
        },
        is_editable=False,
    )
    if description := playlist_obj.get("description"):
        playlist.metadata.description = description
    if thumbs := playlist_obj.get("thumbnails"):
        playlist.metadata.images = parse_thumbnails(instance_id, thumbs)

    author = playlist_obj.get("author")
    if isinstance(author, str):
        playlist.owner = author
    elif isinstance(author, list) and author:
        playlist.owner = author[0].get("name", "")
    elif isinstance(author, dict):
        playlist.owner = author.get("name", "")
    return playlist


def parse_track(
    instance_id: str,
    domain: str,
    track_obj: dict[str, Any],
    track_number: int = 0,
) -> Track:
    video_id = track_obj.get("videoId")
    if not video_id:
        raise ValueError("Track has no videoId")

    track = Track(
        item_id=str(video_id),
        provider=instance_id,
        name=track_obj.get("title", ""),
        provider_mappings={
            ProviderMapping(
                item_id=str(video_id),
                provider_domain=domain,
                provider_instance=instance_id,
                available=track_obj.get("isAvailable", True),
                url=f"{YTM_DOMAIN}/watch?v={video_id}",
                audio_format=AudioFormat(content_type=ContentType.M4A),
            )
        },
        disc_number=1,
        track_number=int(track_obj.get("trackNumber") or track_number or 0),
    )
    if artists := track_obj.get("artists"):
        track.artists = UniqueList(
            _artist_item_mapping(instance_id, a) for a in artists if _artist_is_resolvable(a)
        )
    if not track.artists:
        raise ValueError("Track has no resolvable artists")
    if thumbs := track_obj.get("thumbnails"):
        track.metadata.images = parse_thumbnails(instance_id, thumbs)
    album = track_obj.get("album")
    if isinstance(album, dict) and album.get("id"):
        track.album = _item_mapping(
            instance_id, MediaType.ALBUM, album["id"], album.get("name", "")
        )
    if "isExplicit" in track_obj:
        track.metadata.explicit = track_obj["isExplicit"]
    duration = track_obj.get("duration") or track_obj.get("duration_seconds")
    if duration is not None and str(duration).isdigit():
        track.duration = int(duration)
    elif isinstance(duration, str) and ":" in duration:
        track.duration = _hms_to_seconds(duration)
    return track


def _hms_to_seconds(s: str) -> int:
    """Convert "H:MM:SS" or "MM:SS" to seconds; 0 on anything unexpected."""
    parts = s.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        return 0
    return 0
