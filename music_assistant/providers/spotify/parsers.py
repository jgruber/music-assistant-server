"""Translate Spotify Web API JSON into Music Assistant media_items."""

from __future__ import annotations

from datetime import date
from typing import Any

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

from .constants import SPOTIFY_URL


def _item_mapping(instance_id: str, mt: MediaType, key: str, name: str) -> ItemMapping:
    return ItemMapping(media_type=mt, item_id=key, provider=instance_id, name=name)


def _artist_mapping(instance_id: str, artist: dict[str, Any]) -> ItemMapping:
    return _item_mapping(
        instance_id,
        MediaType.ARTIST,
        str(artist["id"]),
        artist.get("name") or "",
    )


def parse_images(instance_id: str, images: list[dict[str, Any]]) -> UniqueList[MediaItemImage]:
    """Convert Spotify's images array to MediaItemImage list, biggest first."""
    out: UniqueList[MediaItemImage] = UniqueList()
    seen: set[str] = set()
    for img in sorted(images or [], key=lambda i: (i.get("width") or 0), reverse=True):
        url = img.get("url") or ""
        if not url or url in seen:
            continue
        seen.add(url)
        width = img.get("width") or 0
        height = img.get("height") or 0
        itype = (
            ImageType.LANDSCAPE
            if width and height and (width / max(height, 1)) > 2.0
            else ImageType.THUMB
        )
        out.append(
            MediaItemImage(
                type=itype,
                path=url,
                provider=instance_id,
                remotely_accessible=True,
            )
        )
    return out


def parse_artist(instance_id: str, domain: str, obj: dict[str, Any]) -> Artist:
    if not obj.get("id"):
        raise ValueError("Spotify artist has no id")
    artist = Artist(
        item_id=str(obj["id"]),
        name=obj.get("name") or "",
        provider=instance_id,
        provider_mappings={
            ProviderMapping(
                item_id=str(obj["id"]),
                provider_domain=domain,
                provider_instance=instance_id,
                url=obj.get("external_urls", {}).get("spotify") or f"{SPOTIFY_URL}/artist/{obj['id']}",
            )
        },
    )
    if images := obj.get("images"):
        artist.metadata.images = parse_images(instance_id, images)
    if genres := obj.get("genres"):
        artist.metadata.genres = set(genres)
    return artist


def _album_type(obj: dict[str, Any]) -> AlbumType:
    kind = (obj.get("album_type") or obj.get("album_group") or "").lower()
    if kind == "single":
        return AlbumType.SINGLE
    if kind == "compilation":
        return AlbumType.COMPILATION
    if kind == "album":
        return AlbumType.ALBUM
    return AlbumType.UNKNOWN


def parse_album(instance_id: str, domain: str, obj: dict[str, Any]) -> Album:
    if not obj.get("id"):
        raise ValueError("Spotify album has no id")
    album = Album(
        item_id=str(obj["id"]),
        name=obj.get("name") or "",
        provider=instance_id,
        provider_mappings={
            ProviderMapping(
                item_id=str(obj["id"]),
                provider_domain=domain,
                provider_instance=instance_id,
                url=obj.get("external_urls", {}).get("spotify") or f"{SPOTIFY_URL}/album/{obj['id']}",
            )
        },
        album_type=_album_type(obj),
    )
    if images := obj.get("images"):
        album.metadata.images = parse_images(instance_id, images)
    if artists := obj.get("artists"):
        album.artists = UniqueList(_artist_mapping(instance_id, a) for a in artists if a.get("id"))
    if rd := obj.get("release_date"):
        # Either an official-API-style date string ("2023-01-15"), or the year number
        # ("1969") that our spotapi normaliser produces. First 4 chars cover both.
        try:
            album.year = int(str(rd)[:4])
        except ValueError:
            pass
    if label := obj.get("label"):
        album.metadata.label = label
    return album


def parse_track(instance_id: str, domain: str, obj: dict[str, Any]) -> Track:
    """Parse a Spotify track object.

    Handles both the standalone track shape and the "playlist item" wrapper
    (where the actual track sits under ``item['track']``).
    """
    if "track" in obj and isinstance(obj["track"], dict):
        obj = obj["track"]
    if obj.get("is_local"):
        raise ValueError("Skipping Spotify-local track (not resolvable on YouTube)")
    if not obj.get("id"):
        raise ValueError("Spotify track has no id")

    track = Track(
        item_id=str(obj["id"]),
        provider=instance_id,
        name=obj.get("name") or "",
        provider_mappings={
            ProviderMapping(
                item_id=str(obj["id"]),
                provider_domain=domain,
                provider_instance=instance_id,
                available=obj.get("is_playable", True),
                url=obj.get("external_urls", {}).get("spotify") or f"{SPOTIFY_URL}/track/{obj['id']}",
                # Streaming goes through YouTube/yt-dlp at get_stream_details time;
                # Spotify's track entity itself has no streamable format.
                audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
            )
        },
        disc_number=int(obj.get("disc_number") or 1),
        track_number=int(obj.get("track_number") or 0),
    )
    if duration_ms := obj.get("duration_ms"):
        track.duration = int(duration_ms / 1000)
    if artists := obj.get("artists"):
        track.artists = UniqueList(
            _artist_mapping(instance_id, a) for a in artists if a.get("id")
        )
    if not track.artists:
        raise ValueError("Spotify track has no resolvable artists")
    if album := obj.get("album"):
        if album.get("id"):
            track.album = _item_mapping(
                instance_id, MediaType.ALBUM, str(album["id"]), album.get("name") or ""
            )
            if images := album.get("images"):
                track.metadata.images = parse_images(instance_id, images)
    if "explicit" in obj:
        track.metadata.explicit = obj["explicit"]
    return track


def parse_playlist(instance_id: str, domain: str, obj: dict[str, Any]) -> Playlist:
    if not obj.get("id"):
        raise ValueError("Spotify playlist has no id")
    playlist = Playlist(
        item_id=str(obj["id"]),
        provider=instance_id,
        name=obj.get("name") or "",
        provider_mappings={
            ProviderMapping(
                item_id=str(obj["id"]),
                provider_domain=domain,
                provider_instance=instance_id,
                url=obj.get("external_urls", {}).get("spotify") or f"{SPOTIFY_URL}/playlist/{obj['id']}",
            )
        },
        # client_credentials auth can't edit anyone's playlists; stay read-only.
        is_editable=False,
    )
    if desc := obj.get("description"):
        playlist.metadata.description = desc
    if images := obj.get("images"):
        playlist.metadata.images = parse_images(instance_id, images)
    if owner := obj.get("owner"):
        playlist.owner = owner.get("display_name") or owner.get("id") or ""
    return playlist
