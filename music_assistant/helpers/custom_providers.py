"""
Discovery and startup wiring for out-of-tree ("custom") music providers.

Mirror of Home Assistant core's ``custom_components/`` pattern: a well-known
directory under the user's storage path, plus optional operator-supplied paths
from the ``MASS_EXTRA_PROVIDERS_PATH`` environment variable (colon-separated),
hold provider directories that look exactly like the in-tree providers
(``manifest.json``, module, optional ``strings.json`` and icons).

The server extends ``music_assistant.providers.__path__`` with these roots so
the ordinary module loader keeps working, walks their manifests alongside the
in-tree ones, and merges each custom provider's ``strings.json`` into the
translation table at startup. A custom provider shadows an in-tree provider of
the same domain — one WARNING per custom provider at load time names the
domain and the path it was loaded from.

Upstream MA does not ship, list, link to, or endorse any custom provider;
discovery and install UX are the operator's concern (e.g. a community HACS-
style add-on).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from music_assistant.helpers.json import load_json_dict

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

LOGGER = logging.getLogger(__name__)

#: Subdirectory under ``storage_path`` scanned for custom provider directories.
CUSTOM_PROVIDERS_SUBDIR = "custom_providers"

#: Environment variable with colon-separated extra provider roots to scan.
EXTRA_PROVIDERS_PATH_ENV = "MASS_EXTRA_PROVIDERS_PATH"


def discover_custom_provider_roots(storage_path: str, *, exclude: Iterable[str] = ()) -> list[str]:
    """
    Return extra provider directory roots to scan, in walk order.

    The storage-path well-known location is always first; operator-supplied
    paths from the environment variable follow in the order the operator
    listed them. Non-existent roots are dropped.

    ``exclude`` lists real-paths to omit — notably the in-tree providers
    directory, so an operator who points the environment variable at it (or at
    a symlink of it) does not get every in-tree provider loaded a second time
    with a spurious custom-provider warning.
    """
    excluded = {str(Path(p).resolve()) for p in exclude}
    roots: list[Path] = [Path(storage_path) / CUSTOM_PROVIDERS_SUBDIR]
    env = os.environ.get(EXTRA_PROVIDERS_PATH_ENV, "").strip()
    if env:
        roots.extend(Path(p) for p in env.split(os.pathsep) if p)
    seen: set[str] = set()
    out: list[str] = []
    for root in roots:
        real = str(root.resolve())
        if real in seen or real in excluded or not root.is_dir():
            continue
        seen.add(real)
        out.append(real)
    return out


def discover_custom_provider_dirs(
    roots: list[str], *, dev_mode: bool = False
) -> list[tuple[str, str]]:
    """
    Return ``[(domain, provider_dir), ...]`` for each immediate subdirectory of ``roots``.

    Hidden directories (``.``-prefixed) are always skipped; dev-only ones
    (``_``-prefixed) are skipped unless ``dev_mode`` is set — the same policy
    ``MusicAssistant.__load_provider_manifests`` applies to in-tree providers.
    A subdirectory without a ``manifest.json`` is still returned — the caller
    (e.g. the translations controller) only needs the directory to look for a
    ``strings.json``; the manifest-less case is harmless since nothing to
    flatten.
    """
    out: list[tuple[str, str]] = []
    for root in roots:
        try:
            entries = list(Path(root).iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.name.startswith("_") and not dev_mode:
                continue
            if not entry.is_dir():
                continue
            out.append((entry.name, str(entry)))
    return out


def walk_provider_manifests(
    providers_path: str,
    custom_roots: Iterable[str],
    *,
    dev_mode: bool = False,
) -> Iterator[tuple[str, str, bool, bool]]:
    """
    Yield ``(domain, provider_dir, is_custom, shadows_in_tree)`` for every provider dir.

    Walks the in-tree providers path first and records the domains it finds, so
    a custom provider yielded after can be flagged as shadowing an in-tree one
    of the same domain. Callers consume this in order: an entry yielded later
    with the same domain overwrites the earlier one, matching HA core's
    ``custom_components`` policy.
    """
    in_tree_domains: set[str] = set()
    buffered: list[tuple[str, str, bool]] = []
    for root in [providers_path, *custom_roots]:
        is_custom = root != providers_path
        for domain, provider_dir in discover_custom_provider_dirs([root], dev_mode=dev_mode):
            if not is_custom:
                in_tree_domains.add(domain)
            buffered.append((domain, provider_dir, is_custom))
    for domain, provider_dir, is_custom in buffered:
        shadows = is_custom and domain in in_tree_domains
        yield domain, provider_dir, is_custom, shadows


def extend_providers_package_path(roots: list[str]) -> None:
    """
    Extend ``music_assistant.providers.__path__`` with the custom provider roots.

    After this, ``importlib.import_module("music_assistant.providers.<domain>")``
    finds a custom provider's module just like an in-tree one. Custom roots are
    prepended so a custom provider wins against an in-tree one of the same
    domain (same policy as HA core ``custom_components``).
    """
    if not roots:
        return
    # Imported lazily so this helper stays importable before the providers
    # package itself (which some setup paths may touch) is wired up.
    import music_assistant.providers as providers_pkg  # noqa: PLC0415

    existing = list(providers_pkg.__path__)
    for root in roots:
        if root in existing:
            continue
        # prepend so custom wins over in-tree on name collision
        providers_pkg.__path__.insert(0, root)


def warn_about_custom_provider(domain: str, provider_dir: str, *, shadows: bool = False) -> None:
    """
    Emit the one-per-load trust-model WARNING for a custom provider.

    This is a plain WARNING: a custom provider loaded from an out-of-tree
    directory runs with the full server's privileges. The server does not
    sandbox it. Call once at manifest load time, named at the ``music_assistant``
    logger so the line reaches the operator's addon log.

    When ``shadows`` is true the message explicitly notes that an in-tree
    provider of the same domain is being overwritten, so the operator sees
    which provider is actually in effect.
    """
    logger = logging.getLogger("music_assistant")
    trailer = (
        "Runs with full server privileges (see docs/custom_providers.md). This provider "
        "is not shipped, listed or endorsed by Music Assistant upstream; the operator is "
        "responsible for it."
    )
    if shadows:
        logger.warning(
            "Custom provider '%s' loaded from %s shadows the in-tree '%s' provider — the "
            "custom one wins and the in-tree one is not loaded. %s",
            domain,
            provider_dir,
            domain,
            trailer,
        )
    else:
        logger.warning("Custom provider '%s' loaded from %s. %s", domain, provider_dir, trailer)


def flatten_strings(data: Any, prefix: str = "") -> dict[str, str]:
    """
    Flatten a nested ``strings.json`` dict into ``{"a.b.c": "value"}`` form.

    Matches ``scripts/build_translations.py``'s compile step: a nested dict
    becomes dotted keys, and leaf strings are kept as-is. Non-string / non-dict
    leaves are stringified so a mistyped source cannot crash startup.
    """
    out: dict[str, str] = {}
    if not isinstance(data, dict):
        return out
    for key, value in data.items():
        full = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
        if isinstance(value, dict):
            out.update(flatten_strings(value, full))
        elif value is not None:
            out[full] = str(value)
    return out


async def load_custom_provider_source_strings(
    provider_dirs: Iterable[tuple[str, str]],
) -> dict[str, str]:
    """
    Return ``{fq_key: en_source_string}`` for all custom providers.

    Walks each custom provider directory, loads its ``strings.json`` if present,
    and flattens it under ``provider.<domain>.*`` — matching how upstream bakes
    in-tree providers' strings into ``translations/en.json`` at build time.
    A missing or unreadable ``strings.json`` is silently skipped so a provider
    without translations still loads.
    """
    out: dict[str, str] = {}
    for domain, provider_dir in provider_dirs:
        strings_path = Path(provider_dir) / "strings.json"
        if not strings_path.is_file():
            continue
        try:
            raw = await load_json_dict(str(strings_path))
        except Exception as exc:
            LOGGER.warning(
                "Failed to load strings.json for custom provider '%s' from %s: %s",
                domain,
                provider_dir,
                exc,
            )
            continue
        out.update(flatten_strings(raw, prefix=f"provider.{domain}"))
    return out
