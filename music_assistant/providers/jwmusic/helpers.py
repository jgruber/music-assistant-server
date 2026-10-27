"""Thin async client over the jw.org pub-media endpoint.

One call per publication fetches that publication's full MP3 track list —
title, duration, bitrate, filesize, mimetype, direct URL — in a single JSON
response. The server stamps each entry with the pub code and docid, which
are what we encode into Music Assistant item ids.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiohttp import ClientTimeout

from .constants import API_BASE, CATALOG_CACHE_TTL_SECONDS, DEFAULT_LANG

if TYPE_CHECKING:
    from aiohttp import ClientSession


@dataclass(frozen=True)
class JwTrack:
    """One MP3 track as served by the pub-media endpoint."""

    pub: str
    docid: str
    track_number: int
    title: str
    url: str
    duration: int  # seconds
    filesize: int  # bytes
    bitrate_kbps: int | None
    mimetype: str
    image_url: str | None


@dataclass(frozen=True)
class JwAlbum:
    """A publication (album) and its tracks."""

    pub: str
    name: str  # the API's pubName, in English
    image_url: str | None
    tracks: tuple[JwTrack, ...]


def _parse_int(value: object, default: int = 0) -> int:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _parse_duration(value: object) -> int:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _parse_bitrate(value: object) -> int | None:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse_entry(pub: str, index: int, entry: dict) -> JwTrack | None:
    """Flatten one API `files.E.MP3[]` entry into a JwTrack."""
    file_obj = entry.get("file")
    if not isinstance(file_obj, dict):
        return None
    url = file_obj.get("url")
    if not isinstance(url, str) or not url:
        return None
    track_image = entry.get("trackImage")
    image_url: str | None = None
    if isinstance(track_image, dict):
        candidate = track_image.get("url")
        if isinstance(candidate, str) and candidate:
            image_url = candidate
    # Some publications (International Music is one) report docid=0 for every
    # track, so we fall back to the track number for a stable per-track id.
    # The "t" prefix keeps the two id spaces apart so a future docid=42 and a
    # track-number 42 never collide.
    track_number = _parse_int(entry.get("track"), default=index)
    raw_docid = entry.get("docid")
    docid = str(raw_docid) if raw_docid else f"t{track_number}"
    return JwTrack(
        pub=pub,
        docid=docid,
        track_number=track_number,
        title=str(entry.get("title") or "").strip(),
        url=url,
        duration=_parse_duration(entry.get("duration")),
        filesize=_parse_int(entry.get("filesize")),
        bitrate_kbps=_parse_bitrate(entry.get("bitRate")),
        mimetype=str(entry.get("mimetype") or "audio/mpeg"),
        image_url=image_url,
    )


async def fetch_album(session: ClientSession, pub: str) -> JwAlbum:
    """Fetch a publication's full MP3 track list."""
    params = {
        "pub": pub,
        "langwritten": DEFAULT_LANG,
        "txtCMSLang": DEFAULT_LANG,
        "fileformat": "MP3",
        "output": "json",
    }
    async with session.get(
        API_BASE, params=params, timeout=ClientTimeout(total=30)
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
    pub_name = str(data.get("pubName") or "").strip()
    pub_image_url: str | None = None
    pub_image = data.get("pubImage")
    if isinstance(pub_image, dict):
        candidate = pub_image.get("url")
        if isinstance(candidate, str) and candidate:
            pub_image_url = candidate
    files_by_lang = data.get("files") or {}
    mp3_entries = ((files_by_lang.get(DEFAULT_LANG) or {}).get("MP3")) or []
    tracks: list[JwTrack] = []
    for index, entry in enumerate(mp3_entries, start=1):
        if not isinstance(entry, dict):
            continue
        if parsed := _parse_entry(pub, index, entry):
            tracks.append(parsed)
    return JwAlbum(
        pub=pub, name=pub_name, image_url=pub_image_url, tracks=tuple(tracks)
    )


class CatalogCache:
    """In-memory per-publication cache with a simple TTL.

    One lock per pub de-dupes concurrent cold fetches so a burst of search
    requests still only hits the pub-media endpoint once per publication per
    cache window.
    """

    def __init__(self, session: ClientSession) -> None:
        self._session = session
        self._entries: dict[str, tuple[float, JwAlbum]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _fresh(self, pub: str) -> JwAlbum | None:
        entry = self._entries.get(pub)
        if entry is None:
            return None
        timestamp, album = entry
        if time.monotonic() - timestamp >= CATALOG_CACHE_TTL_SECONDS:
            return None
        return album

    async def get(self, pub: str) -> JwAlbum:
        """Return the album, fetching and caching on cold miss."""
        if cached := self._fresh(pub):
            return cached
        lock = self._locks.setdefault(pub, asyncio.Lock())
        async with lock:
            if cached := self._fresh(pub):
                return cached
            album = await fetch_album(self._session, pub)
            self._entries[pub] = (time.monotonic(), album)
            return album

    async def get_many(self, pubs: list[str]) -> list[JwAlbum]:
        """Fetch many publications concurrently; skip pubs whose fetch fails.

        A single pub-media endpoint returning 404 (the publication has no MP3
        release, or jw.org retired it) must not take down the whole browse.
        We log at WARNING and drop that one pub from the returned list.
        """
        results = await asyncio.gather(
            *(self.get(p) for p in pubs), return_exceptions=True
        )
        out: list[JwAlbum] = []
        for pub, result in zip(pubs, results, strict=True):
            if isinstance(result, BaseException):
                logging.getLogger("music_assistant.jwmusic").warning(
                    "Skipping publication %r — fetch failed: %s", pub, result
                )
                continue
            out.append(result)
        return out

    def invalidate(self) -> None:
        """Drop all cached entries (operator-visible via a provider reload)."""
        self._entries.clear()


def encode_track_id(pub: str, docid: str) -> str:
    """Pack the pub code and docid into a stable per-track id."""
    return f"{pub}:{docid}"


def decode_track_id(item_id: str) -> tuple[str, str]:
    """Unpack ``encode_track_id`` — raises ``ValueError`` on malformed input."""
    pub, _, docid = item_id.partition(":")
    if not pub or not docid:
        raise ValueError(f"invalid jwmusic track id: {item_id!r}")
    return pub, docid
