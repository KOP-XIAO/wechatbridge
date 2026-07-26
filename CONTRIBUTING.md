# Contributing

## Development workflow

1. Fork the repository and create a feature branch from `main`.
2. Make your changes. Keep them focused — one change per branch.
3. Test locally: `python -m py_compile wechatbridge/*.py`.
4. Update `CHANGELOG.md` under `[Unreleased]`.
5. Open a pull request against `main`. Include a summary of what changed and why.

## Code style

- Python 3.10+.
- Keep functions short and focused on one task.
- Avoid adding dependencies without discussion.
- Log with context — include `user_id`, prompt summary, or error details.

## Version and compatibility strategy

### Semantic versioning

This project follows [Semantic Versioning 2.0.0](https://semver.org/):

- **MAJOR** — incompatible API, behaviour, or configuration changes (deleting a config key, changing message handling semantics, breaking existing deployments).
- **MINOR** — backward-compatible new features (new slash command, new message type support, new config option).
- **PATCH** — backward-compatible bug fixes.

Breaking changes MUST bump MAJOR. They must not be smuggled into MINOR or PATCH releases.

### Deprecated environment variables

When renaming an environment variable, add the old name → new name mapping to `wechatbridge/config.py`'s `_DEPRECATED_ENV` dict. The old name will continue to work with a deprecation warning for at least **one minor version** before removal.

### Feature flags

Experimental or optional features are gated behind `WECHATBRIDGE_ENABLE_*` environment variables. Follow the existing pattern (e.g. `WECHATBRIDGE_ENABLE_MCP`, `WECHATBRIDGE_ENABLE_SUBAGENT`):

- Default to `true` for stable features that most users want.
- Default to `false` for truly experimental features.
- The flag should be checked at the entry point of the feature, and the feature should degrade gracefully when disabled.

### Prefs structure migrations

User preferences (per-user, per-backend) are stored as nested dicts and normalized in `runner_common.py`'s `normalize_prefs()`. If you change the prefs structure (add/rename/remove keys), update `normalize_prefs()` so that old persisted prefs are migrated automatically on load. This avoids manual migration steps during upgrades.

### Version source of truth

The canonical version string lives in `wechatbridge/__init__.py` as `__version__`. Since 1.3.0, `pyproject.toml` uses `dynamic = ["version"]` and reads it via `[tool.setuptools.dynamic]` (`version = {attr = "wechatbridge.__version__"}`). Only ever change the version in `__init__.py`.

## Release process

Releases are semi-automated. A maintainer only needs to:

1. **Update the version** — change `__version__` in `wechatbridge/__init__.py` to the new version.
2. **Update CHANGELOG** — rename `[Unreleased]` to `[X.Y.Z] - YYYY-MM-DD` and add a fresh empty `[Unreleased]` heading above it.
3. **Tag and push** — create an annotated tag `vX.Y.Z` and push it:

   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```

The tag push triggers the `.github/workflows/release.yml` workflow which:

- Verifies the tag matches `__version__`.
- Builds the package and publishes it to **PyPI** via [trusted publishing](https://docs.pypi.org/trusted-publishers/).
- Extracts the `[X.Y.Z]` section from `CHANGELOG.md` and creates a **GitHub Release** with it.

### One-time PyPI setup (already configured 2026-07-26 — reference for re-setup)

If the trusted publisher or GitHub environment ever needs to be recreated:

1. On [PyPI project settings](https://pypi.org/manage/project/wechatbridge-cli/settings/) (for the very first publish of a new project, use the account-level [pending publisher](https://pypi.org/manage/account/publishing/) page instead):
   - Add a **trusted publisher** with:
     - Repository: `dorokuma/wechatbridge`
     - Workflow: `release.yml` — must match the workflow **file name** exactly (`release.yml`, not `release.yaml`, not the display name `Release`)
     - Environment: `pypi`
2. On GitHub repository:
   - Created an environment called `pypi` (no secrets needed — trusted publishing uses OIDC).

### Recommended upgrade path for users (document in CHANGELOG if breaking)

```bash
pipx upgrade wechatbridge-cli && sudo systemctl restart wechatbridge
```

Or via the deploy script:

```bash
curl -fsSL https://raw.githubusercontent.com/dorokuma/wechatbridge/main/deploy/update.sh | sudo bash
```

If the service runs as a dedicated system user (e.g. `wechatbridge`), set `WECHATBRIDGE_USER` so the script upgrades that user's pipx installation.

If the release contains breaking changes, write a clear **Migration** subsection in the CHANGELOG entry.
