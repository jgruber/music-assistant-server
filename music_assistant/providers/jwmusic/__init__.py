"""JW.org Music provider package."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .provider import setup

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry

__all__ = ["setup"]


async def get_config_entries(*_: object, **__: object) -> tuple[ConfigEntry, ...]:
    """Return the config entries exposed in Music Assistant's setup UI.

    The provider has nothing to configure — it serves a public catalog over
    HTTP with no authentication — but the loader expects this symbol, so
    return an empty tuple.
    """
    return ()
