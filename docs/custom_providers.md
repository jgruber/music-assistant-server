# Custom providers

Music Assistant can load additional provider directories from locations outside
the installed `music_assistant.providers` package, mirroring the
`custom_components/` pattern Home Assistant core has for integrations.

**Upstream does not ship, list, link to or endorse any custom provider.** The
mechanism is a loading hook only; a custom provider runs with the full
server's privileges and the operator is responsible for it. A separate
community project may offer a discovery / install UX on top.

## Where custom providers are loaded from

At startup the server scans:

1. `<storage_path>/custom_providers/` — always scanned, created on first use.
2. Each path listed in the `MASS_EXTRA_PROVIDERS_PATH` environment variable
   (colon-separated), in listed order. Non-existent paths are silently dropped.

Each entry must be an immediate subdirectory of one of the roots and look just
like an in-tree provider:

```
<root>/
└── mymusic/
    ├── __init__.py          # defines setup() and the provider class
    ├── manifest.json        # name, domain, requirements, config_entries, ...
    ├── strings.json         # optional; English source strings
    ├── icon.svg             # optional; themed icon fallback
    └── icon_monochrome.svg  # optional; monochrome icon variant
```

The subdirectory name is the provider's domain. A custom provider sharing a
domain with an in-tree one shadows the in-tree one — the same policy HA core
applies to `custom_components`. Each custom provider gets one `WARNING` at
load time naming the domain and the directory it was loaded from, so the
operator can see what is in effect.

## Dependencies

Dependencies declared in `manifest.json` → `requirements` are installed into
the server's venv on first load, through the same path as in-tree providers.
Pin versions (`"foo==1.2.3"`); unpinned requirements are ignored.

An install step at load time is slow and needs network, so the recommended
operator setup is one of:

- Bake dependencies into the container image (preferred for HA addons: the
  Supervisor recreates the container on every restart, and a baked image boots
  without network).
- Preseed the venv once and keep the container.

## Translations

If the provider ships a `strings.json`, its entries are merged into the
in-memory translation table at startup under `provider.<domain>.*`, so the
provider's `manifest.name`, `manifest.description` and `config_entries.*`
labels work with no further action.

The merge is runtime-only; the in-tree build-time Lokalise pipeline
(`scripts/build_translations.py`) covers only in-tree providers.

## Safety

The server does not sandbox custom providers. A provider loaded this way can
read and write every file the server can, make any outbound request, and read
any state it has access to. Only load custom providers from sources you trust.
