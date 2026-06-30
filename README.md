# fusionAIze Metadata (Public)

[![CI](https://github.com/fusionAIze/fusionaize-metadata-public/actions/workflows/ci.yml/badge.svg)](https://github.com/fusionAIze/fusionaize-metadata-public/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Public AI provider catalog: **providers**, **models**, and **offerings** with
pricing, capabilities, and freshness metadata. Consumed by fusionAIze products
(Gate, Grid, Lens, Fabric) and any community tool that wants a curated catalog
of LLM provider data.

> Looking for product-specific overlays, routing heuristics or operator
> packages? Those live in the **private** companion repo
> [fusionaize-metadata](https://github.com/fusionAIze/fusionaize-metadata)
> (access by request).

## Layout

```text
fusionaize-metadata-public/
  README.md
  LICENSE
  schemas/
    provider-catalog.v1.schema.json
    model-catalog.v1.schema.json
    offering-catalog.v1.schema.json
  providers/
    catalog.v1.json    # Provider entries (Anthropic, OpenAI, DeepSeek, …)
    sources.v1.json    # Source tracking & freshness metadata
  models/
    catalog.v1.json    # Model definitions with capability matrix
  offerings/
    catalog.v1.json    # Concrete model × provider × pricing offerings
```

## Catalog Files

### `providers/catalog.v1.json`
Per-provider metadata: pricing rates per 1M tokens, recommended model,
aliases, volatility flags, evidence level, signup/source URLs, freshness
status, and tier info (`active`, `deprecated`, `expired`, `preview`).

### `models/catalog.v1.json`
Model-level definitions: canonical IDs, display names, families,
capabilities (context window, modalities, tool support, JSON mode),
typical use cases.

### `offerings/catalog.v1.json`
Concrete offerings — the cartesian product of `model_id × provider_id`
with pricing, region, and availability per offering.

### `providers/sources.v1.json`
Endpoint URLs and freshness/check-tracking for the provider source pages
that catalog editors monitor.

## Consumption

### Direct fetch (anonymous, public)
```bash
curl -fsSL https://raw.githubusercontent.com/fusionAIze/fusionaize-metadata-public/main/providers/catalog.v1.json
```

### Local checkout
```bash
git clone https://github.com/fusionAIze/fusionaize-metadata-public.git
export FAIGATE_PROVIDER_METADATA_DIR=$(pwd)/fusionaize-metadata-public
```

### Gate runtime sync (planned)
faigate's upcoming `MetadataCatalogSync` pulls this catalog over HTTPS
with `If-None-Match`/ETag conditional GET, caches at
`~/.cache/faigate/metadata/`, falls back to a bundled default. See the
faigate repo's `docs/CATALOG-UPDATER.md` for setup.

## Schema Versioning

Each catalog file declares its own `schema_version`. Schemas live in
`schemas/`. Versioning is **additive within a major version**: minor bumps
add optional fields without breaking older consumers.

## Quality & Security

- **CI Validation**: JSON syntax + duplicate-ID checks on every push
- **CodeQL**: security scanning over JSON/YAML
- **Security Audit**: gitleaks scan for secrets
- **Scheduled refresh**: weekly validation cron

## Maintenance

To update pricing, models, or providers:

1. Edit the relevant `catalog.v1.json` file
2. Bump `last_reviewed` to today
3. Bump `pricing.refreshed_at` if pricing changed
4. Open a PR — CI will validate JSON + duplicate IDs
5. Merge → consumers refresh on their next sync (default 24h)

## Contributing

PRs welcome for:
- Pricing corrections (cite the provider's pricing page in the PR body)
- New providers / models / offerings
- Schema improvements (must remain backward compatible)

Out of scope: routing heuristics, evaluations, scoring rationale,
operator-specific quotas — those belong in the private companion repo.

## License

Apache-2.0 — see [LICENSE](LICENSE).

## Repository & Contributing

Canonical repository: **self-hosted Forgejo** — `git.langevc.com/fusionaize/fusionaize-metadata-public`
(`git clone git@git.langevc.com:fusionaize/fusionaize-metadata-public.git`). Develop against the Forgejo
clone and open pull requests there. The GitHub copy is a read-only mirror.
