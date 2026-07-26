# Security policy

## Reporting a vulnerability

Do not publish credentials, sensitive prompts, or exploit details in a public issue. If the hosting
repository or package distribution publishes a private vulnerability-reporting channel, use that
channel. If no private channel is published, open a minimal public issue asking the maintainers to
establish private contact; include no vulnerability details until that private channel exists.

Include the affected version, operating system, reproduction prerequisites, impact, and the
smallest safe reproduction. Remove provider credentials, real prompts, and unrelated local data.

## Trust boundary

A debate configuration can name local executables. Treat configuration files as executable code
and run only trusted configurations. Agent prompts and outputs are untrusted, and coding agents may
have access to files, credentials, tools, or networks outside this package's control.

The generated workflow uses Codex read-only sandboxes. Other configured adapters may not provide
equivalent isolation: Kimi 0.29.1 headless mode is explicitly full-access only, and every generic
command is treated as unsafe regardless of its declared permission. Generic commands cannot run in
parallel stages. Use external containment for sensitive data or untrusted requirements.

Version 0.1 officially supports POSIX platforms (Linux and macOS) only. Windows is not supported.
See `docs/security.md` for the complete model and limitations.
