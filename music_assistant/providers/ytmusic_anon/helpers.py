"""
Async wrappers around ytmusicapi and yt-dlp for the anonymous YouTube Music provider.

ytmusicapi and yt-dlp are both blocking; Music Assistant is fully async. Every call goes
through asyncio.to_thread so the event loop stays free. Nothing here holds a long-lived
YTMusic client: ytmusicapi is cheap to construct (it hits one InnerTube config endpoint
on first use and then caches internally), and keeping it per-call keeps the module free
of shared mutable state.

All functions here are unauthenticated. See the project memory for why ([[project_goal]]).
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from typing import Any, Literal

import ytmusicapi

from .constants import QUALITY_FORMAT_SELECTORS, YT_DOMAIN

YTMSearchFilter = Literal["artists", "albums", "songs", "playlists", "videos"]

_LOGGER = logging.getLogger(__name__)


def _client(language: str) -> ytmusicapi.YTMusic:
    """Return an unauthenticated YTMusic client for the given language code."""
    return ytmusicapi.YTMusic(language=language)


async def ytm_search(
    query: str,
    language: str,
    ytm_filter: YTMSearchFilter | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search YouTube Music anonymously."""

    def _run() -> list[dict[str, Any]]:
        # ytmusicapi 1.12.3 matches the result shelf title against English literals,
        # so filtered searches return nothing in most non-English locales. We force
        # English here regardless of the display language.
        ytm = ytmusicapi.YTMusic(language="en")
        results = ytm.search(query=query, filter=ytm_filter, limit=limit)
        # Normalize artist/playlist id fields so parsers can look up a single key.
        for r in results:
            rtype = r.get("resultType")
            if rtype == "artist":
                if r.get("artists"):
                    r["id"] = r["artists"][0]["id"]
                    r["name"] = r["artists"][0]["name"]
                elif r.get("browseId"):
                    r["id"] = r["browseId"]
                    r["name"] = r.get("artist", r.get("name", ""))
            elif rtype == "playlist":
                r["id"] = r.get("playlistId") or r.get("browseId")
        return results[:limit]

    return await asyncio.to_thread(_run)


async def ytm_get_artist(prov_artist_id: str, language: str) -> dict[str, Any]:
    """Fetch artist details by channel id."""

    def _run() -> dict[str, Any]:
        ytm = _client(language)
        try:
            artist = ytm.get_artist(channelId=prov_artist_id)
        except KeyError:
            # Non-music channels (uploaders without an "Artist" page) fall back to get_user.
            try:
                user = ytm.get_user(channelId=prov_artist_id)
                artist = {"channelId": prov_artist_id, "name": user["name"]}
            except KeyError:
                artist = {"channelId": prov_artist_id, "name": "Unknown"}
        # get_artist drops the channelId it was called with when the backend returns a
        # redirected one, so pin it back.
        artist["channelId"] = prov_artist_id
        return artist

    return await asyncio.to_thread(_run)


async def ytm_get_artist_albums(
    prov_artist_id: str, language: str, params: str | None = None
) -> list[dict[str, Any]]:
    """Fetch all albums for an artist. Uses the artist page's embedded list by default."""

    def _run() -> list[dict[str, Any]]:
        ytm = _client(language)
        artist = ytm.get_artist(channelId=prov_artist_id)
        albums_section = artist.get("albums") or {}
        if albums_section.get("browseId") and albums_section.get("params"):
            # Full discography endpoint when the artist has more albums than fit on the page.
            return ytm.get_artist_albums(
                channelId=albums_section["browseId"],
                params=albums_section["params"],
            )
        return albums_section.get("results", [])

    return await asyncio.to_thread(_run)


async def ytm_get_album(prov_album_id: str, language: str) -> dict[str, Any]:
    """Fetch album details and tracks by browseId."""

    def _run() -> dict[str, Any]:
        ytm = _client(language)
        return ytm.get_album(browseId=prov_album_id)

    return await asyncio.to_thread(_run)


async def ytm_get_track(prov_track_id: str, language: str) -> dict[str, Any] | None:
    """Fetch song details by videoId. Returns None when the video is gone."""

    def _run() -> dict[str, Any] | None:
        ytm = _client(language)
        song = ytm.get_song(videoId=prov_track_id)
        if "videoDetails" not in song:
            return None
        details = song["videoDetails"]
        track: dict[str, Any] = {
            "videoId": details["videoId"],
            "title": details["title"],
            "duration": details.get("lengthSeconds"),
            "artists": [{"channelId": details["channelId"], "name": details["author"]}],
            "isAvailable": song.get("playabilityStatus", {}).get("status") == "OK",
        }
        thumbs: list[dict[str, Any]] = []
        micro = song.get("microformat", {}).get("microformatDataRenderer", {})
        if micro_thumbs := micro.get("thumbnail", {}).get("thumbnails"):
            thumbs.extend(micro_thumbs)
        if detail_thumbs := details.get("thumbnail", {}).get("thumbnails"):
            thumbs.extend(detail_thumbs)
        if thumbs:
            track["thumbnails"] = thumbs
        return track

    return await asyncio.to_thread(_run)


async def ytm_get_playlist(
    prov_playlist_id: str, language: str, limit: int | None = None
) -> dict[str, Any]:
    """Fetch a playlist and its tracks."""

    def _run() -> dict[str, Any]:
        ytm = _client(language)
        pl = ytm.get_playlist(playlistId=prov_playlist_id, limit=limit)
        # ytmusicapi sometimes omits the id in the response.
        pl.setdefault("id", prov_playlist_id)
        return pl

    return await asyncio.to_thread(_run)


async def ytm_get_watch_playlist(
    prov_video_id: str, language: str, limit: int = 25
) -> dict[str, Any]:
    """Fetch the "radio" watch playlist for a video id — used for similar tracks."""

    def _run() -> dict[str, Any]:
        ytm = _client(language)
        return ytm.get_watch_playlist(
            videoId=prov_video_id,
            playlistId=f"RDAMVM{prov_video_id}",
            limit=limit,
            radio=True,
        )

    return await asyncio.to_thread(_run)


async def ytm_get_home(language: str, limit: int = 3) -> list[dict[str, Any]]:
    """Fetch the YouTube Music home feed (anonymous, country-shaped by the language code)."""

    def _run() -> list[dict[str, Any]]:
        ytm = _client(language)
        return ytm.get_home(limit=limit)

    return await asyncio.to_thread(_run)


async def ytm_get_mood_categories(language: str) -> dict[str, Any]:
    """Fetch the mood & genre categories for browse."""

    def _run() -> dict[str, Any]:
        ytm = _client(language)
        return ytm.get_mood_categories()

    return await asyncio.to_thread(_run)


_YT_DLP_MOD: Any = None


def _get_yt_dlp() -> Any:
    """Import yt_dlp lazily — the module is heavy and only needed at playback time."""
    global _YT_DLP_MOD
    if _YT_DLP_MOD is None:
        _YT_DLP_MOD = importlib.import_module("yt_dlp")
    return _YT_DLP_MOD


async def extract_stream_format(
    video_id: str,
    quality: str = "best",
    po_token_server_url: str | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    """Resolve the best anonymous audio stream for a videoId.

    Returns the yt-dlp format dict for the chosen stream (keys: url, abr, asr,
    audio_channels, audio_ext, ...). Caller is responsible for extracting the url
    and expiry — we never cache the result here because the signed URL expires.

    Anonymous access notes (YouTube, 2026-ish):
      * Every modern yt-dlp client (web, web_music, tv_simply, mweb) now
        requires a GVS PO Token to get playable audio formats — without one
        YouTube only serves image/thumbnail entries, which the format selector
        then rejects. The baseline anonymous path therefore requires a PO
        token sidecar that signs each request. We integrate with
        `bgutil-ytdlp-pot-provider` (ships in the upstream MA addon repo as
        "YouTube Music PO Token Generator"); if the user hasn't configured
        one we fall back to tv_simply + mweb anyway, which lets `yt-dlp`
        surface the real "PO required" error to them clearly.
      * YouTube signs stream URLs with a JS ``n`` and signature challenge;
        yt-dlp needs a JS runtime (deno) available on PATH to solve them.
        Deno is baked into the add-on image, but the EJS challenge-solver
        feature has to be enabled explicitly in ``ydl_opts``.
    """

    def _run() -> dict[str, Any]:
        yt_dlp = _get_yt_dlp()
        selector_expr = QUALITY_FORMAT_SELECTORS.get(quality, QUALITY_FORMAT_SELECTORS["best"])
        extractor_args: dict[str, Any] = {
            "youtube": {
                "skip": ["translated_subs", "dash"],
                "player_client": (
                    # When a PO token server is wired up, use web_music — same
                    # client the upstream MA ytmusic provider uses and the one
                    # that reliably yields opus/m4a. Without one, fall back to
                    # tv_simply/mweb so at least some uploads can play.
                    ["web_music"]
                    if po_token_server_url
                    else ["tv_simply", "mweb"]
                ),
            },
        }
        if po_token_server_url:
            # bgutil-ytdlp-pot-provider's extractor-arg key.
            extractor_args["youtubepot-bgutilhttp"] = {"base_url": [po_token_server_url]}

        ydl_opts: dict[str, Any] = {
            "quiet": not verbose,
            "verbose": verbose,
            "extractor_args": extractor_args,
            # Opt-in to yt-dlp's EJS challenge solver. The option is called
            # `remote_components` and takes a set of ``<component>:<source>``
            # strings. "ejs:github" fetches the solver from yt-dlp-ejs on
            # first use (cached under the venv), then runs it under our baked
            # deno for YouTube's n/signature challenges.
            "remote_components": {"ejs:github"},
        }
        url = f"{YT_DOMAIN}/watch?v={video_id}"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            selector = ydl.build_format_selector(selector_expr)
            fmt = next(selector({"formats": info["formats"]}), None)
            if not fmt:
                raise RuntimeError(f"No playable audio format for {video_id}")
            return fmt

    return await asyncio.to_thread(_run)
