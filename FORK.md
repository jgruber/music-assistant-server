# jgruber/music-assistant-server — a fork of music-assistant/server

This is a soft fork of [music-assistant/server](https://github.com/music-assistant/server)
that adds **one capability**: Music Assistant can load providers from outside
the main source tree. Everything else is upstream, unchanged. It carries no
music providers of its own.

## Why this fork exists

Music Assistant's provider list is defined at build time by the directories
under `music_assistant/providers/`. Shipping a new provider — a music source,
a player driver, a metadata or audio-analysis plugin — requires an upstream PR,
and upstream has stated the project is not planning to support a HACS-style
custom-components mechanism
([discussion #6869](https://github.com/orgs/music-assistant/discussions/6869)).

This fork patches that gap at the server level, in the same shape Home Assistant
core uses for `custom_components/`: a well-known directory under the operator's
`storage_path` is scanned at startup for provider manifests, and anything it
finds loads alongside the in-tree ones. The fork takes **no position** on which
providers an operator should install.

## What the fork adds

- **Custom-provider loader** — `music_assistant/helpers/custom_providers.py`
  plus the two hook points in `mass.py:__load_provider_manifests` and
  `TranslationController.setup`. Reads `<storage_path>/custom_providers/<domain>/`,
  extends the `music_assistant.providers` package `__path__`, merges each
  custom provider's `strings.json` into the translation table, and emits one
  WARNING per loaded custom provider so operators see the trust-boundary
  implication. See [docs/custom_providers.md](docs/custom_providers.md).
- **`CONF_PUBLISH_BASE_URL`** — a stream-controller config entry that overrides
  the generated stream base URL, for operators running MA behind a reverse
  proxy that terminates TLS (e.g. an Amazon Echo custom skill, which rejects
  `http://IP:port` audio URLs).
- **URL-search filter fix** — upstream [PR #7228](https://github.com/music-assistant/server/pull/7228).
  Carried here so the fix is already in effect; drops out of the fork's
  history on the first upstream merge that includes it.

The three fork additions are deliberately generic and small. The intent is
that if upstream later changes its position on either the custom-provider
mechanism or any of the small patches, each one can be submitted upstream
unmodified.

## Install

### Docker

```
docker pull ghcr.io/jgruber/music-assistant-server:latest
```

Same Compose/K8s shape as the official image — swap the image reference. Mount
a persistent directory at `/data`; any `/data/custom_providers/<domain>/`
subdirectory is loaded as a provider on startup.

### Home Assistant OS/Supervised

Install the add-on at
[`jgruber/music-assistant-boombox-mixtape-sources`](https://github.com/jgruber/music-assistant-boombox-mixtape-sources).
That add-on's image is this fork; the add-on itself opinionates the install by
cloning a specific set of custom providers into `storage_path` on startup. The
fork image is upstream-shaped; the add-on is the opinionated layer.

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
Restart Music Assistant and the server scans the directory at startup. The
new provider shows up in the normal "Add Music Source" / "Add Player" UI as
if it had shipped in-tree.

Operator-written providers follow the same manifest shape and lifecycle as
in-tree providers; the walker makes no distinction between `music`, `player`,
`metadata`, `audio_analysis`, and `plugin` types.

## Tracking upstream

- `main` carries this fork's three additions and is the default branch.
- `dev` tracks `music-assistant/server:dev` as a plain mirror.
- Upstream releases get merged (not rebased) into `main`.

## Scope

The fork does not change existing user-facing APIs or ship additional
providers of any kind. Everything upstream Music Assistant does continues to
work as upstream intends; the custom-provider mechanism is additive.

## License

Apache-2.0, inherited from upstream. See [LICENSE](LICENSE).
