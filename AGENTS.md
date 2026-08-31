## Where to work

- **Canonical origin:** a private upstream repository. **Do not push to GitHub** — it is a read-only mirror.
- **Local clone:** your local checkout of this repository.
- **Remotes:** `origin` = canonical upstream, `github` = GitHub mirror (read-only).
- **Pull requests:** open them against the canonical upstream, never against the GitHub mirror.
- **CI:** GitHub Actions workflows are guarded with `if: ${{ github.server_url == 'https://github.com' }}` so the upstream skips them and the GitHub mirror runs them. `.forgejo/workflows/mirror.yml` force-pushes every branch + tag back to GitHub.
