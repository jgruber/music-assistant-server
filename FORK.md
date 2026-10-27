# jgruber/music-assistant-server — a fork of music-assistant/server

This is a soft fork of [music-assistant/server](https://github.com/music-assistant/server).
Upstream rejected the two anonymous-access music providers below and the
HACS-style custom-provider mechanism that would let anyone load providers like
them out of tree ([discussion #6869](https://github.com/orgs/music-assistant/discussions/6869)).
This fork carries all three.

## What this fork adds on top of upstream

- **`music_assistant.providers.ytmusic_anon`** — Boombox: anonymous public
  YouTube Music catalog, no account required. yt-dlp resolves audio.
- **`music_assistant.providers.spotify`** — Mixtape: Spotify's public open.spotify.com
  catalog for search and playlist metadata, YouTube for audio. **Replaces** the
  upstream Spotify provider (same domain) — if you have a Spotify Premium
  subscription, stay on upstream.
- **`music_assistant.providers.jwmusic`** — JW.org Music: the publicly-published
  music catalog at [jw.org/library/music-songs/](https://www.jw.org/en/library/music-songs/),
  with direct MP3 playback from the site's own download URLs.
- **Pluggable custom providers**, Home Assistant `custom_components/` style —
  drop a provider directory into the storage path and the server loads it
  alongside the in-tree ones. See [docs/custom_providers.md](docs/custom_providers.md).
- **`CONF_PUBLISH_BASE_URL`** — override the base URL in stream URLs handed to
  players, for stream servers sitting behind a reverse proxy that terminates
  TLS (Amazon Echo custom skills need this).
- **URL-search filter fix** — pasting a Spotify/Qobuz/Deezer link in the search
  box no longer returns the same resolved item once per section. Submitted
  upstream as [PR #7228](https://github.com/music-assistant/server/pull/7228);
  carried here so the fork works today.

## Install

### Docker (standalone)

```
docker pull ghcr.io/jgruber/music-assistant-server:latest
```

Same Compose/K8s shape as the official image — swap the image reference.

### Home Assistant OS/Supervised

Install from the add-on repo
[`jgruber/music-assistant-boombox-mixtape-sources`](https://github.com/jgruber/music-assistant-boombox-mixtape-sources).
That add-on's image is built from this fork — all three providers come baked
in; no overlay patches needed.

### From source

```
git clone https://github.com/jgruber/music-assistant-server.git -b main
cd music-assistant-server
# Same build steps as upstream
```

## Migrating off this fork, back to upstream

- **Boombox / JW.org Music**: uninstall the provider from the Music Assistant
  settings. These two have no counterpart upstream — history and queue entries
  for their items will show as unavailable.
- **Mixtape**: uninstall it before switching. Upstream's Spotify provider owns
  the same domain, so a direct switch would see the Mixtape config and refuse
  to load it.
- **`publish_base_url` and `custom_providers`** have no upstream equivalent;
  unset them before switching.

Then point your container at `ghcr.io/music-assistant/server:<tag>` and resume.

## Tracking upstream

- `main` carries this fork's work and is the default branch.
- `dev` tracks `music-assistant/server:dev` as a plain mirror.
- Upstream releases get merged (not rebased) into `main`.

## Scope

This fork does not change existing user-facing APIs. All upstream providers,
players, and metadata providers continue to work as upstream intends — except
the upstream Spotify provider, which is replaced by Mixtape. If upstream
accepts the URL-search filter fix (PR #7228) in a release, that commit will be
dropped from the fork's history on the next upstream merge.

## License

Apache-2.0, inherited from upstream. See [LICENSE](LICENSE).
