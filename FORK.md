# jgruber/music-assistant-server — a fork of music-assistant/server

A soft fork of [music-assistant/server](https://github.com/music-assistant/server)
that adds a single capability: the server can load providers from outside the
main source tree. Everything else is upstream, unchanged. The fork ships no
providers of its own.

## Why this fork exists

Music Assistant's provider list is fixed at build time by the directories
under `music_assistant/providers/`. Any new provider — a music source, a
player driver, a metadata or audio-analysis plugin, a generic plugin — needs
an upstream PR; upstream has stated it is not planning to support a
HACS-style custom-components mechanism
([discussion #6869](https://github.com/orgs/music-assistant/discussions/6869)).

This fork patches that gap at the server level, in the shape Home Assistant
core already uses for `custom_components/`: a well-known directory under the
operator's `storage_path` is scanned at startup, and every provider manifest
it finds loads alongside the in-tree ones. The fork takes no position on
which providers an operator should install, and ships none of its own.

## What the fork adds

- **Custom-provider loader** — `music_assistant/helpers/custom_providers.py`
  plus the hook points in `mass.py:__load_provider_manifests` and
  `TranslationController.setup`. Scans `<storage_path>/custom_providers/<domain>/`,
  extends the `music_assistant.providers` package `__path__` so custom
  manifests import as `music_assistant.providers.<domain>`, merges each
  custom provider's `strings.json` into the translation table, and emits one
  WARNING per loaded custom provider so operators see the trust-boundary
  implication (custom providers run with the full server's privileges). See
  [docs/custom_providers.md](docs/custom_providers.md).
- **`CONF_PUBLISH_BASE_URL`** — a stream-controller config entry that
  overrides the generated stream base URL, for operators running the server
  behind a reverse proxy that terminates TLS.
- **URL-search filter fix** — upstream
  [PR #7228](https://github.com/music-assistant/server/pull/7228). Carried
  here so the fix is already in effect; drops out on the first upstream
  merge that includes it.

The three additions are deliberately small, generic, and upstream-shaped so
any of them can be submitted upstream unmodified if upstream later changes
its position.

## Install

### Docker

```
docker pull ghcr.io/jgruber/music-assistant-server:latest
```

Same Compose/K8s shape as the official image — swap the image reference.
Mount a persistent directory at `/data`; any
`/data/custom_providers/<domain>/` subdirectory is loaded as a provider on
startup.

### Home Assistant OS / Supervised

A thin wrapper add-on is published at
[`jgruber/music-assistant-server-addon`](https://github.com/jgruber/music-assistant-server-addon).
It builds from this fork's Docker image and leaves the operator's choice of
custom providers to the add-on's own start-up script, which can
`git clone` provider repos into `/data/custom_providers/` before Music
Assistant launches. The add-on repo is one worked example of how a
Supervisor-managed install can opt in to any set of custom providers; others
are free to fork or template from it.

To install: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, paste
the add-on URL above, open the resulting entry and click **Install**.

### From source

```
git clone https://github.com/jgruber/music-assistant-server.git
cd music-assistant-server
# Same build steps as upstream.
```

## Installing a custom provider

Drop a provider directory into `<storage_path>/custom_providers/<domain>/`
with the same layout as an in-tree provider: `manifest.json`, `__init__.py`,
optional `strings.json`, optional `icon.svg` / `icon_monochrome.svg`.
Restart Music Assistant; the server scans the directory at startup and the
new provider shows up in the normal "Add Provider" UI as if it had shipped
in-tree.

The walker makes no distinction between provider types — `music`, `player`,
`metadata`, `audio_analysis`, and `plugin` all flow through the same
manifest loader. Any provider upstream Music Assistant can run in-tree, this
fork can run from `storage_path/custom_providers/`.

## Tracking upstream

- `main` carries the fork's three additions and is the default branch.
- `dev` tracks `music-assistant/server:dev` as a plain mirror.
- Upstream releases get merged (not rebased) into `main`.

## Scope

The fork does not change any existing user-facing API, add any providers, or
remove anything upstream ships. The custom-provider mechanism is additive;
every upstream feature continues to work as upstream intends.

## License

Apache-2.0, inherited from upstream. See [LICENSE](LICENSE).
