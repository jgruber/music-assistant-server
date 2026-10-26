"""Tests for the custom-providers discovery and translation-merge helpers."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from music_assistant.helpers.custom_providers import (
    EXTRA_PROVIDERS_PATH_ENV,
    discover_custom_provider_dirs,
    discover_custom_provider_roots,
    extend_providers_package_path,
    flatten_strings,
    load_custom_provider_source_strings,
    walk_provider_manifests,
    warn_about_custom_provider,
)

if TYPE_CHECKING:
    import pytest


def _write_provider(root: Path, domain: str, strings: dict | None = None) -> Path:
    provider_dir = root / domain
    provider_dir.mkdir(parents=True)
    (provider_dir / "__init__.py").write_text("")
    (provider_dir / "manifest.json").write_text(json.dumps({"domain": domain, "name": domain}))
    if strings is not None:
        (provider_dir / "strings.json").write_text(json.dumps(strings))
    return provider_dir


def test_discover_roots_only_storage_path_when_env_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When no env var is set, only the storage_path/custom_providers subdir is returned."""
    monkeypatch.delenv(EXTRA_PROVIDERS_PATH_ENV, raising=False)
    sub = tmp_path / "custom_providers"
    sub.mkdir()
    assert discover_custom_provider_roots(str(tmp_path)) == [str(sub.resolve())]


def test_discover_roots_drops_non_existent_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-existent paths from the env var are silently dropped."""
    sub = tmp_path / "custom_providers"
    sub.mkdir()
    missing = tmp_path / "does_not_exist"
    monkeypatch.setenv(EXTRA_PROVIDERS_PATH_ENV, str(missing))
    assert discover_custom_provider_roots(str(tmp_path)) == [str(sub.resolve())]


def test_discover_roots_env_var_adds_extra_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env var paths appear in the listed order after the storage_path subdir."""
    sub = tmp_path / "custom_providers"
    sub.mkdir()
    extra_a = tmp_path / "a"
    extra_b = tmp_path / "b"
    extra_a.mkdir()
    extra_b.mkdir()
    monkeypatch.setenv(EXTRA_PROVIDERS_PATH_ENV, f"{extra_a}{os.pathsep}{extra_b}")
    assert discover_custom_provider_roots(str(tmp_path)) == [
        str(sub.resolve()),
        str(extra_a.resolve()),
        str(extra_b.resolve()),
    ]


def test_discover_roots_dedupes_paths_pointing_to_same_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same real path listed twice (or via a symlink) is returned once."""
    sub = tmp_path / "custom_providers"
    sub.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(sub)
    monkeypatch.setenv(EXTRA_PROVIDERS_PATH_ENV, f"{sub}{os.pathsep}{link}")
    assert discover_custom_provider_roots(str(tmp_path)) == [str(sub.resolve())]


def test_discover_roots_excludes_in_tree_providers_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pointing the env var at the in-tree providers dir (or a symlink of it) is a no-op."""
    sub = tmp_path / "custom_providers"
    sub.mkdir()
    in_tree = tmp_path / "providers"
    in_tree.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(in_tree)
    monkeypatch.setenv(EXTRA_PROVIDERS_PATH_ENV, f"{in_tree}{os.pathsep}{alias}")
    assert discover_custom_provider_roots(str(tmp_path), exclude=[str(in_tree)]) == [
        str(sub.resolve())
    ]


def test_discover_dirs_skips_hidden_and_underscore_prefixed(tmp_path: Path) -> None:
    """Hidden (.) and dev-only (_) directory names are skipped."""
    _write_provider(tmp_path, "normal")
    _write_provider(tmp_path, "_dev_only")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "file.txt").write_text("not a dir")
    dirs = discover_custom_provider_dirs([str(tmp_path)])
    assert dirs == [("normal", str(tmp_path / "normal"))]


def test_discover_dirs_includes_underscore_in_dev_mode(tmp_path: Path) -> None:
    """``_``-prefixed directories are only visible when dev_mode is set."""
    _write_provider(tmp_path, "_dev_only")
    assert discover_custom_provider_dirs([str(tmp_path)]) == []
    assert discover_custom_provider_dirs([str(tmp_path)], dev_mode=True) == [
        ("_dev_only", str(tmp_path / "_dev_only"))
    ]


def test_discover_dirs_handles_unreadable_root() -> None:
    """A non-existent / unreadable root yields no entries, not an exception."""
    assert discover_custom_provider_dirs(["/does/not/exist"]) == []


def test_walk_marks_custom_shadowing_in_tree(tmp_path: Path) -> None:
    """A custom entry whose domain matches an in-tree one is flagged as shadowing."""
    in_tree = tmp_path / "in_tree"
    custom = tmp_path / "custom"
    _write_provider(in_tree, "foo")
    _write_provider(in_tree, "bar")
    _write_provider(custom, "foo")  # shadows
    _write_provider(custom, "qux")  # new only in custom
    entries = list(walk_provider_manifests(str(in_tree), [str(custom)]))
    # in-tree first (as discovered), then customs; custom 'foo' flagged shadows=True.
    custom_entries = {domain: shadows for domain, _, is_custom, shadows in entries if is_custom}
    assert custom_entries == {"foo": True, "qux": False}
    # in-tree 'bar' is still yielded, is_custom=False, shadows=False.
    in_tree_entries = {
        (domain, shadows) for domain, _, is_custom, shadows in entries if not is_custom
    }
    assert ("bar", False) in in_tree_entries


def test_extend_providers_package_path_is_idempotent(tmp_path: Path) -> None:
    """Repeated calls do not add the same path to __path__ twice."""
    import music_assistant.providers as providers_pkg  # noqa: PLC0415

    before = list(providers_pkg.__path__)
    try:
        extra = tmp_path / "x"
        extra.mkdir()
        extend_providers_package_path([str(extra)])
        extend_providers_package_path([str(extra)])
        assert providers_pkg.__path__.count(str(extra)) == 1
        # Custom root is prepended so it wins against the in-tree one on collision.
        assert providers_pkg.__path__[0] == str(extra)
    finally:
        providers_pkg.__path__[:] = before


def test_flatten_strings_nests_dotted_keys() -> None:
    """A nested dict becomes dotted keys; leaves survive, non-strings stringify."""
    out = flatten_strings(
        {
            "manifest": {"name": "Foo", "description": "bar"},
            "settings": {"option_a": {"label": "A"}},
            "count": 3,
        },
        prefix="provider.foo",
    )
    assert out == {
        "provider.foo.manifest.name": "Foo",
        "provider.foo.manifest.description": "bar",
        "provider.foo.settings.option_a.label": "A",
        "provider.foo.count": "3",
    }


def test_flatten_strings_tolerates_non_dict_root() -> None:
    """A non-dict argument returns an empty mapping without raising."""
    assert flatten_strings([1, 2, 3]) == {}  # type: ignore[arg-type]
    assert flatten_strings("hi") == {}  # type: ignore[arg-type]


async def test_load_custom_provider_source_strings_merges_existing_files(
    tmp_path: Path,
) -> None:
    """Each present strings.json is flattened under provider.<domain>; missing is skipped."""
    _write_provider(
        tmp_path, "with_strings", strings={"manifest": {"name": "N", "description": "D"}}
    )
    _write_provider(tmp_path, "without_strings")  # no strings.json
    dirs = discover_custom_provider_dirs([str(tmp_path)])
    merged = await load_custom_provider_source_strings(dirs)
    assert merged == {
        "provider.with_strings.manifest.name": "N",
        "provider.with_strings.manifest.description": "D",
    }


def test_warn_about_custom_provider_logs_at_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Trust warning is logged at WARNING with domain and provider_dir."""
    with caplog.at_level(logging.WARNING, logger="music_assistant"):
        warn_about_custom_provider("foo", str(tmp_path / "foo"))
    assert any(
        rec.levelno == logging.WARNING and "foo" in rec.getMessage() for rec in caplog.records
    )


def test_warn_about_custom_provider_names_shadowed_in_tree(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The shadowing case explicitly says the in-tree provider is being overwritten."""
    with caplog.at_level(logging.WARNING, logger="music_assistant"):
        warn_about_custom_provider("spotify", str(tmp_path / "spotify"), shadows=True)
    assert any("shadows" in rec.getMessage() for rec in caplog.records)
