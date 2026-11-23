# Standalone Update Notices Workflow

To broadcast a message to existing CLI installations without cutting a new PyPI release, `auto_update.py` fetches `https://raw.githubusercontent.com/googlecolab/google-colab-cli/refs/tags/notices/notices.json` once per day (and on `colab update`). See [docs/07_update_notices.md](../docs/07_update_notices.md) for full details. Follow this workflow ONLY when explicitly publishing, updating, or retracting a CLI notice:

1. **Inspect / preview**: Run `./tools/notices.py list [--version X.Y.Z]` to fetch `refs/tags/notices` and preview matching banners.
2. **Publish / update (decoupled from `main` and PyPI)**: Run `./tools/notices.py add --affects "<PEP 440 specifier>" --message "<text>" --push` (or `./tools/notices.py set <file.json> --push`). The script validates inputs and writes `notices.json` directly onto `refs/tags/notices` via Git plumbing without touching `HEAD` or the working tree. Because `cloudbuild.yaml` triggers only on `refs/tags/v*`, pushing `refs/tags/notices` never triggers a PyPI release.
3. **Retract**: Run `./tools/notices.py clear --push` (writes `[]`) or `./tools/notices.py clear --delete-tag --push` (deletes `refs/tags/notices`; HTTP 404 silently clears cached `update_notices` on clients' next check).
