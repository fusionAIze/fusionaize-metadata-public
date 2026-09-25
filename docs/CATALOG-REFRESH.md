# Scheduled Catalog Refresh

## Purpose

The catalog (`providers/catalog.v1.json`) is regenerated from the provider
folders (`providers/<id>/index.json`) by `scripts/build-catalog.py`.  The
scheduled refresh ensures the checked-in catalog stays in sync with the
folder structure without requiring a human to remember to run the script.

When a provider folder is updated (new model, changed pricing, new provider),
the next scheduled run picks it up and opens a pull request with the diff.

## Workflow

**File:** `.github/workflows/catalog-refresh.yml`

- **Schedule:** Weekly, Monday 06:00 UTC (`schedule` event).
- **Manual trigger:** `workflow_dispatch` via the Actions tab.
- **Runner:** GitHub Actions (`if: ${{ github.server_url == 'https://github.com' }}`).
- **No-change behaviour:** zero diff → no branch, no PR, no noise.
- **Change behaviour:** a branch `chore/catalog-refresh/YYYY-MM-DD` is created
  and a pull request opened against the default branch.

## Routing Effect

A **routing effect** means the refresh changed one or more fields that
determine how a request reaches a provider:

| Field | Why it matters |
|---|---|
| `vendor` | Which organisation operates the endpoint |
| `model` | The model identifier sent in the request |
| `variant` | Disambiguates routes to the same model (e.g. high/low, claude/gpt4o) |
| `hop` | Intermediaries in the request path |
| `baseUrl` | The actual endpoint URL |
| `auth_modes` | How the provider authenticates requests |

When a routing effect is detected, the PR body includes a ⚠️ warning section
listing every entry and field that changed.  These PRs require extra scrutiny
before merging.

## Reviewing a Refresh PR

1. **Read the diff.**  Is every change expected given recent provider-folder
   edits?
2. **Check routing changes** (if marked).  Does the new vendor/model/hop
   match the actual endpoint?
3. **Run the tests.**  The `CI` workflow will run automatically on the PR:
   ```
   python3 -m pytest tests/
   ```
   Expected result: 67 passed (the same baseline as any other change).
4. **Verify idempotency.**  Running `scripts/build-catalog.py` twice must
   produce the same output.  The CI workflow validates this.
5. **Merge.**  If the PR is clean, merge it.  The next scheduled run will
   build on the new baseline.

## Rollback

Every refresh creates a dedicated branch and PR.  If a refresh introduces
a problem:

1. **Close the PR** without merging.
2. **The previous catalog** remains the checked-in state on the default
   branch — it was never overwritten.
3. **To restore an older state** from a previous refresh that was already
   merged, revert the merge commit:
   ```
   git revert -m 1 <merge-commit-sha>
   ```

Because each run produces a commit with a clean diff of `catalog.v1.json`
only, reverting is straightforward — no collateral changes.

## Design Constraints

- **Load on GitHub, not on a self-owned service.**  The entire workflow runs
  inside GitHub Actions.  No external cron, no polling endpoint, no
  self-hosted runner in the request path.
- **No silent writes.**  A change always goes through a pull request — never
  a direct push to the default branch.
- **Zero noise on no-change.**  If the generated catalog is identical to the
  checked-in version, the workflow exits without creating a branch or PR.
- **Rollbackable.**  The last valid state is always the default branch HEAD
  (unmodified by a failed refresh), and any merged refresh can be reverted
  with `git revert`.
