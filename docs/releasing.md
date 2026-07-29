# Release process

Releases are made from a clean, reviewed commit after every required CI job passes. A version tag is
not a substitute for verification.

## Repository preflight

1. Confirm the version and changelog describe the same release.
2. Confirm the maintainer has the right to publish every new source, prompt, fixture, document, and
   asset. Review `docs/provenance.md`.
3. Confirm `.agent-debate/`, credentials, real prompts, local paths, databases, and private records
   are absent from tracked files and the release diff.
4. Run:

   ```bash
   make check
   make build
   python -m twine check dist/*
   ```

5. Install the wheel in a fresh environment and verify:

   ```bash
   agent-debate --version
   agent-debate --help
   agent-debate validate --config examples/architecture-review/debate.yaml
   ```

6. Review the GitHub Actions quality matrix and package job. Both are required.

## GitHub governance preflight

Before announcing a release:

- protect the default branch and require the CI quality and package checks;
- enable private vulnerability reporting;
- keep workflow permissions read-only unless a reviewed release job needs narrower write access;
- verify the issue and pull-request templates do not solicit secrets or private run artifacts.

These settings live on GitHub and cannot be proven by repository files alone.

## Publish

Create an annotated `vMAJOR.MINOR.PATCH` tag only after the preflight passes. Build distributions
from that exact tag or its clean checkout, publish release notes from `CHANGELOG.md`, and attach only
artifacts produced by the CI package job. Do not attach local `.agent-debate/` directories.

Publishing to PyPI is a separate, explicitly authorized operation. TestPyPI should be used before
the first production publication.
