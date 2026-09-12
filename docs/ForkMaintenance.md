# Fork maintenance

This repository (`K-Data-Analyst/open-webui`) is a fork of
`open-webui/open-webui`. Fork changes are kept as a short series of
commits on top of an upstream release tag and are periodically rebased
onto the next tag (see the `rebase based on latest tag ...` commits).

## GitHub Actions: all upstream workflows are disabled

Every upstream workflow in `.github/workflows/` is renamed to
`*.disabled`. GitHub only runs files ending in `.yml` / `.yaml`, so the
renamed files are inert. Upstream uses the same trick for its own retired
workflows, which is why some `*.disabled` files predate the fork.

| Upstream file | In this fork | What it would do on the fork |
|---------------|--------------|------------------------------|
| `backend.yaml` | `backend.disabled` | Python CI matrix on every push / PR |
| `frontend.yaml` | `frontend.disabled` | Frontend build on every push / PR |
| `docker.yaml` | `docker.disabled` | Build and push multi-arch Docker images (base, CUDA, Ollama variants) to GHCR on every push to `main` |
| `release.yml` | `release.disabled` | Create a GitHub release from `CHANGELOG.md` on every push to `main` |
| `release-pypi.yml` | `release-pypi.disabled` | Build and publish the `open-webui` package to PyPI on every push to `main` |
| `issue-label.yaml` | `issue-label.disabled` | Auto-label issues (expects upstream's label set) |
| `codespell.disabled`, `lint-backend.disabled`, `lint-frontend.disabled` | unchanged | Already disabled upstream |

`.github/dependabot.yml` is left as is. Dependabot does not run on forks
unless it is switched on in the repository settings, so it is inert
without any file change.

### Rebase rule: ignore `.github/workflows/`

When rebasing onto a new upstream tag:

1. **Do not re-enable anything.** If upstream edits a workflow, git's
   rename detection normally carries the edit into the `*.disabled` file
   with no conflict. That is fine; the file stays inert.
2. **If a conflict lands in `.github/workflows/`, keep the fork side.**
   Resolve with the fork's `*.disabled` name and never leave a `.yml` /
   `.yaml` behind:

   ```bash
   # from inside the rebase, for each conflicted workflow
   git rm -q .github/workflows/<name>.yaml 2>/dev/null   # drop upstream's live copy if git re-added it
   git checkout --theirs -- .github/workflows/<name>.disabled 2>/dev/null || true
   git add .github/workflows
   git rebase --continue
   ```

   (During a rebase, `--theirs` is the commit being replayed, that is,
   the fork's commit.)
3. **If upstream adds a brand-new workflow**, rename it in the
   fork-side commit that disables workflows:

   ```bash
   git mv .github/workflows/<new>.yaml .github/workflows/<new>.disabled
   ```

4. Before pushing, confirm nothing runnable is left:

   ```bash
   ls .github/workflows/*.y*ml 2>/dev/null && echo "STILL ENABLED" || echo "all workflows disabled"
   ```

If the fork ever needs its own CI, add it under a fork-specific name
(for example `fork-ci.yaml`) so it never collides with an upstream file.
