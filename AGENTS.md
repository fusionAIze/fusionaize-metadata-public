
## Where to work — Forgejo-first

- **Canonical origin:** `git.langevc.com/fusionaize/fusionaize-metadata-public` (Forgejo). **Do not push to GitHub** — it is a read-only mirror.
- **Local clone:** `~/Documents/repositories/forgejo/fusionaize/fusionaize-metadata-public`.
- **Remotes:** `origin` = Forgejo (canonical), `github` = GitHub mirror (read-only).
- **Pull requests:** open on Forgejo (`git.langevc.com/fusionaize/fusionaize-metadata-public/pulls`).
- **CI:** GitHub Actions workflows are guarded with `if: ${{ github.server_url == 'https://github.com' }}` so Forgejo skips them and the GitHub mirror runs them. `.forgejo/workflows/mirror.yml` (Forgejo-only) force-pushes every branch + tag back to GitHub.
