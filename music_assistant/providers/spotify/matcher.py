"""
Spotube-style ranker that picks the best YouTube video for a given Spotify track.

Port of ``rankResults()`` in team-spotube/spotube's
``lib/services/sourced_track/sourced_track.dart``. Additive, order-insensitive
scoring; duration isn't used (YouTube uploads routinely trim/extend by a few
seconds, and spotube's own experience is that duration tolerance costs more
precision than it buys).

Scoring per candidate (summed):
  +1  per Spotify-track artist whose name is a channel-artist of the candidate
  +1  per Spotify-track artist name present in the candidate's title (case-insensitive)
  +3  if the candidate's title contains the Spotify track name
  +1  if the candidate's title matches the "official …" regex
  +2  bonus when BOTH the +3 title hit and the +1 "official" hit fire together
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

_OFFICIAL_RE = re.compile(
    r"\bofficial\s*(?:video|audio|music\s*video|lyric\s*video|visualizer)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Candidate:
    """A YouTube match candidate normalised for scoring.

    `channel_artists` is a tuple of lowercased names of the artists YouTube
    attributes the video to (one entry for a single-artist track, several for
    collabs). `title` is the raw video title.
    """

    video_id: str
    title: str
    channel_artists: tuple[str, ...]
    duration: int | None = None  # seconds, informational — not used for scoring
    raw: Any = None  # original search payload, for debugging


def _lc(s: str | None) -> str:
    return (s or "").strip().casefold()


def _artists_lower(artists: Iterable[dict[str, Any]]) -> list[str]:
    return [_lc(a.get("name")) for a in artists if a.get("name")]


def score_candidate(spotify_track: dict[str, Any], candidate: Candidate) -> int:
    """Score one candidate against a Spotify track; higher is better."""
    track_name_lc = _lc(spotify_track.get("name"))
    artist_names_lc = _artists_lower(spotify_track.get("artists") or [])
    cand_title_lc = _lc(candidate.title)
    cand_channels_lc = {a for a in candidate.channel_artists if a}

    score = 0
    # +1 per Spotify artist that is also a channel-artist of the candidate.
    score += sum(1 for a in artist_names_lc if a in cand_channels_lc)
    # +1 per Spotify artist name present anywhere in the candidate title.
    score += sum(1 for a in artist_names_lc if a and a in cand_title_lc)

    title_hit = bool(track_name_lc) and track_name_lc in cand_title_lc
    official_hit = bool(_OFFICIAL_RE.search(candidate.title or ""))

    if title_hit:
        score += 3
    if official_hit:
        score += 1
    if title_hit and official_hit:
        score += 2  # spotube's combined bonus
    return score


def pick_best(
    spotify_track: dict[str, Any], candidates: Iterable[Candidate]
) -> Candidate | None:
    """Return the highest-scored candidate, de-duplicated by video_id."""
    seen: set[str] = set()
    best: Candidate | None = None
    best_score = -(1 << 30)
    for c in candidates:
        if c.video_id in seen:
            continue
        seen.add(c.video_id)
        s = score_candidate(spotify_track, c)
        if s > best_score:
            best_score = s
            best = c
    return best
