# Source and dependency provenance

This repository is maintained as a standalone project rather than as a source-code fork. The MIT
license covers material that the repository's contributors have the right to license; it does not
relicense provider CLIs, remote models, user repositories, or debate inputs and outputs.

## Contribution policy

Every contributor is responsible for having the right to submit their code, prompts, fixtures,
documentation, datasets, and media. Do not submit:

- employer-confidential or client-owned material;
- copied book, course, exam, dataset, template, or model output without compatible rights;
- credentials, personal records, real private prompts, or provider transcripts;
- generated run artifacts containing source excerpts, local paths, or user data.

AI-assisted work has the same review and provenance requirements as manually written work. A model
cannot grant rights to third-party material, and generated text is not evidence of originality.

## Dependencies and providers

Runtime Python dependencies are declared in `pyproject.toml` and resolved in `uv.lock`. They are
installed as separate packages and are not vendored into this repository. Before a release,
maintainers must review the resolved dependency licenses and any newly introduced assets.

Codex, Kimi, and Generic executables are external programs invoked through adapter contracts. They
are not distributed by this package, and their names do not imply affiliation or endorsement.
Users remain responsible for the providers' terms, credentials, retention policies, and runtime
permissions.

## Repository assets and examples

- Architecture diagrams keep their editable Draw.io source beside exported images.
- Bundled architecture tasks and role prompts are project-owned synthetic examples.
- `.agent-debate/` is ignored because real runs can contain private inputs and machine-local data.
- `examples/sanitized-decision/` is illustrative and is not represented as a verbatim model run.

If a contribution cannot establish a material asset's origin and compatible reuse rights, remove or
replace that asset before merging.
