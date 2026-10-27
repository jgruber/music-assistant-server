"""Constants for the jwmusic provider.

Each entry below is one of the publications surfaced at
jw.org/en/library/music-songs/ — mirroring the top-level cards shown
to any logged-out visitor there. The pub code (``osg``, ``sjj``, …) is
the same code the public pub-media endpoint keys off, so adding a new
album is a one-line change here. Keep this list in sync with the live
page.
"""

from __future__ import annotations

from typing import Final, NamedTuple


class Category(NamedTuple):
    """A jw.org music publication exposed as a browsable album."""

    pub: str
    name: str


#: Top-level pub-media endpoint for publicly-downloadable MP3 lists.
API_BASE: Final[str] = "https://b.jw-cdn.org/apis/pub-media/GETPUBMEDIALINKS"

#: Only the English catalog is wired up for the initial release. The API
#: supports every language jw.org publishes in — enough interest and we add
#: a config option; until then, keep the string centralized.
DEFAULT_LANG: Final[str] = "E"

#: In-memory per-publication cache TTL. The catalog changes rarely, so one
#: refresh per day keeps the live view fresh without any extra scheduling.
CATALOG_CACHE_TTL_SECONDS: Final[int] = 86_400

#: Attribution shown as the album artist for every track. The actual
#: performers vary from album to album; the publisher is the canonical
#: credit the site itself uses.
PUBLISHER: Final[str] = "Jehovah's Witnesses"

#: Mirror of jw.org/en/library/music-songs/ top-level entries that have an
#: MP3 release, in the order they appear on the page as of 2026-10. The
#: parent "Sing Out Joyfully to Jehovah" and "Sing to Jehovah" / "Sing to
#: Jehovah — New Songs" entries are printed-songbook publications and have
#: no audio of their own — their audio releases are the `sjjm/sjjc/sjji`
#: and `snv` siblings respectively, which are included below.
CATEGORIES: Final[tuple[Category, ...]] = (
    Category("sjjm", '"Sing Out Joyfully" to Jehovah — Meetings'),
    Category("sjjc", '"Sing Out Joyfully" to Jehovah — Vocals'),
    Category("sjji", '"Sing Out Joyfully" to Jehovah — Instrumental'),
    Category("pksjj", "Become Jehovah's Friend — Sing With Us"),
    Category("imc", "International Music"),
    Category("osg", "Original Songs"),
    Category("pkon", "Become Jehovah's Friend — Original Songs"),
    Category("gnjst1", "The Good News According to Jesus — Soundtrack 1"),
    Category("gnjst2", "The Good News According to Jesus — Soundtrack 2"),
    Category("gnjst3", "The Good News According to Jesus — Soundtrack 3"),
    Category("cywst", '"Commit Your Way to Jehovah" — Soundtrack'),
    Category("snv", "Sing to Jehovah — Chorus"),
)

#: Fast lookup from pub code to display name.
CATEGORY_BY_PUB: Final[dict[str, Category]] = {c.pub: c for c in CATEGORIES}
