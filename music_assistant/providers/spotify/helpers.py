"""
Async wrappers around spotapi + ytmusicapi for the spotube provider.

spotapi (https://pypi.org/project/spotapi/) hits Spotify's internal pathfinder
GraphQL endpoints anonymously — no client_id, no login, no PO-token-style
sidecar. Its public surface is `spotapi.Public` for direct lookups and
searches, and `spotapi.Artist` for artist discography.

Because spotapi returns Spotify's raw GraphQL shape (nested
`item.data.__typename`, `artists.items[].profile.name`, etc.) rather than the
official Web API shape, each wrapper here normalises the response into the
flat dict shape that `parsers.py` already knows how to handle. Keeping the
normalisers in one place means `parsers.py` is agnostic of the data source.

All spotapi calls are blocking; everything runs through `asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Iterator

import spotapi
import ytmusicapi

from .constants import SPOTIFY_URL
from .matcher import Candidate

_LOGGER = logging.getLogger(__name__)


# ---------------- normalisation helpers (spotapi GraphQL -> Web API shape) ----------------


def _id_from_uri(uri: str | None) -> str:
    """``spotify:album:ABC123`` -> ``ABC123``; empty string on anything else."""
    return uri.rsplit(":", 1)[-1] if uri and ":" in uri else ""


def _norm_images(sources: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Spotify's internal coverArt.sources is already {url,width,height}; just defensively copy."""
    out: list[dict[str, Any]] = []
    for s in sources or []:
        if s.get("url"):
            out.append(
                {
                    "url": s["url"],
                    "width": s.get("width") or 0,
                    "height": s.get("height") or 0,
                }
            )
    return out


def _norm_artists(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for a in items or []:
        aid = a.get("id") or _id_from_uri(a.get("uri"))
        name = (a.get("profile") or {}).get("name") or a.get("name") or ""
        if not aid:
            continue
        out.append({"id": aid, "name": name})
    return out


def _collect_artists(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull artist items from any of the three shapes spotapi uses.

    - search / playlist item: data["artists"]["items"]
    - song_info trackUnion:   data["firstArtist"]["items"] + data["otherArtists"]["items"]
    """
    items = (data.get("artists") or {}).get("items")
    if items:
        return _norm_artists(items)
    combined: list[dict[str, Any]] = []
    combined.extend((data.get("firstArtist") or {}).get("items") or [])
    combined.extend((data.get("otherArtists") or {}).get("items") or [])
    return _norm_artists(combined)


def _norm_track_from_track_data(data: dict[str, Any]) -> dict[str, Any]:
    """
    Convert spotapi's `Track` data dict (the ``data`` under an ``item``/``itemV2``
    wrapper, or the ``trackUnion`` under song_info) to a Web-API-shaped track.
    """
    album_of_track = data.get("albumOfTrack") or {}
    cover = album_of_track.get("coverArt") or {}
    # song_info returns duration under "duration"; playlist items return it under
    # "trackDuration". Shape is {"totalMilliseconds": int} in both cases.
    duration = data.get("duration") or data.get("trackDuration") or {}
    content_rating = data.get("contentRating") or {}
    tid = data.get("id") or _id_from_uri(data.get("uri"))
    return {
        "id": tid,
        "name": data.get("name") or "",
        "duration_ms": duration.get("totalMilliseconds"),
        "disc_number": data.get("discNumber") or 1,
        "track_number": data.get("trackNumber") or 0,
        "is_playable": (data.get("playability") or {}).get("playable", True),
        "explicit": content_rating.get("label") == "EXPLICIT",
        "external_urls": {"spotify": f"{SPOTIFY_URL}/track/{tid}"} if tid else {},
        "artists": _collect_artists(data),
        "album": {
            "id": _id_from_uri(album_of_track.get("uri")) or album_of_track.get("id") or "",
            "name": album_of_track.get("name") or "",
            "images": _norm_images(cover.get("sources")),
        }
        if album_of_track
        else None,
    }


def _norm_track_from_search_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """One entry from Public.song_search → Web-API shape, or None if we can't parse it."""
    data = ((item.get("item") or {}).get("data")) or {}
    if data.get("__typename") != "Track":
        return None
    return _norm_track_from_track_data(data)


def _norm_track_from_playlist_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """One entry from Public.playlist_info → Web-API shape, or None if not a Track."""
    wrap = (item.get("itemV2") or {}).get("data") or (item.get("item") or {}).get("data") or {}
    if wrap.get("__typename") != "Track":
        return None
    return _norm_track_from_track_data(wrap)


def _norm_artist(data: dict[str, Any]) -> dict[str, Any]:
    """Normalise an artist union / search hit to Web-API shape."""
    profile = data.get("profile") or {}
    visuals = data.get("visuals") or {}
    avatar = ((visuals.get("avatarImage") or {}).get("sources")) or []
    aid = data.get("id") or _id_from_uri(data.get("uri"))
    return {
        "id": aid,
        "name": profile.get("name") or data.get("name") or "",
        "external_urls": {"spotify": f"{SPOTIFY_URL}/artist/{aid}"} if aid else {},
        "images": _norm_images(avatar),
        "genres": profile.get("genres") or [],
    }


def _norm_album(data: dict[str, Any]) -> dict[str, Any]:
    cover = data.get("coverArt") or {}
    aid = data.get("id") or _id_from_uri(data.get("uri"))
    date_info = data.get("date") or {}
    release_year = date_info.get("year") or ""
    return {
        "id": aid,
        "name": data.get("name") or "",
        "external_urls": {"spotify": f"{SPOTIFY_URL}/album/{aid}"} if aid else {},
        "images": _norm_images(cover.get("sources")),
        "artists": _norm_artists((data.get("artists") or {}).get("items")),
        "release_date": str(release_year),
        "album_type": (data.get("type") or "album").lower(),
        "total_tracks": (data.get("tracksV2") or {}).get("totalCount")
        or (data.get("tracks") or {}).get("totalCount"),
    }


def _norm_playlist(data: dict[str, Any]) -> dict[str, Any]:
    """
    Normalise a Public.playlist_info payload to the Web-API playlist shape.

    spotapi yields a generator of pages; each page has an ``items`` list of
    track rows and a wrapping playlist header. We expect the caller to pass
    the full first-page dict including header fields such as ``name`` and
    ``ownerV2``.
    """
    pid = data.get("id") or _id_from_uri(data.get("uri"))
    images = data.get("images") or {}
    image_sources = (images.get("items") or [{}])[0].get("sources") if images else []
    owner = (data.get("ownerV2") or {}).get("data") or data.get("owner") or {}
    return {
        "id": pid,
        "name": data.get("name") or "",
        "description": data.get("description") or "",
        "external_urls": {"spotify": f"{SPOTIFY_URL}/playlist/{pid}"} if pid else {},
        "images": _norm_images(image_sources),
        "owner": {
            "display_name": owner.get("name") or owner.get("username") or "",
            "id": _id_from_uri(owner.get("uri")) or owner.get("id") or "",
        },
    }


# ---------------- Spotify (via spotapi) async wrappers ----------------


async def sp_search_tracks(query: str, limit: int = 10) -> list[dict[str, Any]]:
    def _run() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        # song_search yields PAGES (lists of ~100 hits each); flatten.
        for page in spotapi.Public.song_search(query):
            if not isinstance(page, list):
                continue
            for hit in page:
                if len(out) >= limit:
                    return out
                normalised = _norm_track_from_search_item(hit)
                if normalised and normalised.get("id"):
                    out.append(normalised)
            if len(out) >= limit:
                break
        return out

    return await asyncio.to_thread(_run)


async def sp_search_artists(query: str, limit: int = 10) -> list[dict[str, Any]]:
    def _run() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        # artist_search yields PAGES (lists); each entry is {__typename, data:{...Artist}}.
        for page in spotapi.Public.artist_search(query):
            if not isinstance(page, list):
                continue
            for hit in page:
                if len(out) >= limit:
                    return out
                data = hit.get("data") or {}
                if data.get("__typename") != "Artist":
                    continue
                normalised = _norm_artist(data)
                if normalised.get("id"):
                    out.append(normalised)
            if len(out) >= limit:
                break
        return out

    return await asyncio.to_thread(_run)


async def sp_track(track_id: str) -> dict[str, Any]:
    def _run() -> dict[str, Any]:
        resp = spotapi.Public.song_info(track_id)
        track_union = ((resp.get("data") or {}).get("trackUnion")) or {}
        return _norm_track_from_track_data(track_union)

    return await asyncio.to_thread(_run)


async def sp_album(album_id: str) -> dict[str, Any]:
    def _run() -> dict[str, Any]:
        # album_info can yield either dict pages (track lists) or an initial "header" page;
        # the shape varies between spotapi releases. Walk the generator and pick up both.
        album_header: dict[str, Any] = {}
        tracks: list[dict[str, Any]] = []
        for page in spotapi.Public.album_info(album_id):
            if not isinstance(page, dict):
                continue
            # albumUnion header (early pages)
            au = ((page.get("data") or {}).get("albumUnion")) or {}
            if au and au.get("__typename") == "Album" and not album_header:
                album_header = au
            # tracks can live under tracksV2.items on the header, or directly under items
            track_source = (
                (au.get("tracksV2") or {}).get("items")
                or page.get("items")
                or []
            )
            for item in track_source:
                td = (
                    ((item.get("track") or {}).get("data"))
                    or (item.get("track") or {})
                    or ((item.get("itemV2") or {}).get("data"))
                    or {}
                )
                if td.get("__typename") == "Track":
                    tracks.append(_norm_track_from_track_data(td))
        normalised = _norm_album(album_header) if album_header else {
            "id": album_id,
            "name": "",
            "artists": [],
            "images": [],
            "external_urls": {"spotify": f"{SPOTIFY_URL}/album/{album_id}"},
            "release_date": "",
            "album_type": "album",
        }
        normalised["tracks"] = tracks
        return normalised

    return await asyncio.to_thread(_run)


async def sp_playlist(playlist_id: str) -> dict[str, Any]:
    def _run() -> dict[str, Any]:
        pp = spotapi.PublicPlaylist(playlist_id)
        # One cheap call for the playlist header (name, description, owner, images, etc).
        header_resp = pp.get_playlist_info(limit=0)
        header = ((header_resp.get("data") or {}).get("playlistV2")) or {}
        header["id"] = header.get("id") or playlist_id
        normalised = _norm_playlist(header)
        if not normalised["name"]:
            normalised["name"] = f"Spotify playlist {playlist_id}"

        # Separate pass to collect all tracks via the public pagination generator.
        tracks: list[dict[str, Any]] = []
        for page in spotapi.Public.playlist_info(playlist_id):
            if not isinstance(page, dict):
                continue
            for item in page.get("items") or []:
                parsed = _norm_track_from_playlist_item(item)
                if parsed:
                    tracks.append(parsed)
        normalised["tracks"] = tracks
        return normalised

    return await asyncio.to_thread(_run)


async def sp_artist(artist_id: str) -> dict[str, Any]:
    def _run() -> dict[str, Any]:
        a = spotapi.Artist()
        resp = a.get_artist(artist_id)
        au = ((resp.get("data") or {}).get("artistUnion")) or {}
        au["id"] = au.get("id") or artist_id
        return _norm_artist(au)

    return await asyncio.to_thread(_run)


async def sp_artist_albums(artist_id: str, limit: int = 50) -> list[dict[str, Any]]:
    def _run() -> list[dict[str, Any]]:
        a = spotapi.Artist()
        resp = a.get_artist_discography(
            artist_id, section="all", offset=0, limit=limit
        )
        au = ((resp.get("data") or {}).get("artistUnion")) or {}
        disco = au.get("discography") or {}
        out: list[dict[str, Any]] = []
        # section="all" puts everything under disco["all"].items[].releases.items[].
        # Older section-specific calls return disco["albums"|"singles"|"compilations"].items[].
        for section_name in ("all", "albums", "singles", "compilations"):
            section = disco.get(section_name) or {}
            for row in section.get("items") or []:
                for rel in (row.get("releases") or {}).get("items") or []:
                    # keep the release's own `type` field if present; it's "ALBUM"/"SINGLE"/...
                    out.append(_norm_album(rel))
        return out

    return await asyncio.to_thread(_run)


# ---------------- YouTube Music search (unchanged from the v1 code path) ----------------


_PAREN_RE = re.compile(r"[\(\[].*?[\)\]]")


def _normalise_track_name(name: str) -> str:
    return _PAREN_RE.sub(" ", name).strip()


async def ytm_search_candidates(
    spotify_track: dict[str, Any], limit: int = 10
) -> list[Candidate]:
    name = _normalise_track_name(spotify_track.get("name") or "")
    artists = [a.get("name") for a in (spotify_track.get("artists") or []) if a.get("name")]
    primary_artist = artists[0] if artists else ""
    query = f"{primary_artist} {name}".strip()

    def _run() -> list[Candidate]:
        ytm = ytmusicapi.YTMusic(language="en")
        results = ytm.search(query=query, filter="songs", limit=limit) or []
        out: list[Candidate] = []
        for r in results:
            vid = r.get("videoId")
            if not vid:
                continue
            channel_artists = tuple(
                (a.get("name") or "").casefold()
                for a in (r.get("artists") or [])
                if a.get("name")
            )
            duration = r.get("duration_seconds")
            if duration is None and isinstance(r.get("duration"), str):
                parts = r["duration"].split(":")
                try:
                    if len(parts) == 2:
                        duration = int(parts[0]) * 60 + int(parts[1])
                    elif len(parts) == 3:
                        duration = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                except ValueError:
                    duration = None
            out.append(
                Candidate(
                    video_id=str(vid),
                    title=r.get("title") or "",
                    channel_artists=channel_artists,
                    duration=duration,
                    raw=r,
                )
            )
        return out

    return await asyncio.to_thread(_run)
