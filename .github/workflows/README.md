# Workflows are disabled in this fork

Every upstream GitHub Actions workflow in this directory is renamed to
`*.disabled` so GitHub ignores it (it only runs `*.yml` / `*.yaml`).
This is the same convention upstream uses for its own retired workflows
(`codespell.disabled`, `lint-backend.disabled`, ...).

They are disabled because they were written for `open-webui/open-webui`:
publishing Docker images to upstream's registry, cutting GitHub releases
and PyPI packages on every push to `main`, auto-labelling issues, and
running upstream's CI matrix. None of that should run on this fork.

**When rebasing on upstream, ignore this directory.** Do not re-enable a
workflow because upstream changed it, and do not resolve a conflict here
by taking upstream's version. Full procedure: `docs/ForkMaintenance.md`.
