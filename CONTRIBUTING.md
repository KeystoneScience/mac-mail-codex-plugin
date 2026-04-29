# Contributing

Thanks for helping improve the Mac Mail Codex plugin.

## Development Rules

- Keep the default path local-first and credential-free.
- Do not write to Apple Mail's SQLite database.
- Do not add destructive Mail tools without a dry-run design, exact IDs, explicit confirmation, and tests.
- Do not add direct-send tools. Preserve the inspect-then-send gate.
- Keep search/read outputs bounded and avoid printing message bodies in tests, smoke scripts, or benchmarks.
- Keep installer/update paths explicit, local, and reversible; do not add remote script execution or unconfirmed background mutation.
- Prefer Python standard library code unless a dependency removes substantial risk.

## Local Checks

Run:

```bash
scripts/verify_release.sh
```

If bare `python3` is older than 3.10 locally, run:

```bash
PYTHON_BIN=/path/to/python3.12 scripts/verify_release.sh
```

On a Mac with Apple Mail configured, also run:

```bash
scripts/verify_release.sh --live
```

## Release Checklist

- Bump `SERVER_VERSION` in `scripts/mac_mail_mcp.py`.
- Bump `.codex-plugin/plugin.json` `version`.
- Update `CHANGELOG.md`.
- Update README install prompt and bootstrap/update docs if install behavior changed.
- Run `scripts/verify_release.sh --live` on macOS.
- Run `scripts/package_plugin.sh`.
- Verify the archive does not include local cache files, private body indexes, pycache, or `dist/`.
