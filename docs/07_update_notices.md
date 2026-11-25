---
log:
2026-11-23: Initial design and maintainer workflow documentation for standalone version-gated update notices (`refs/tags/notices` on GitHub CDN) and the `tools/notices.py` management script.
---

# Design & Operations: Standalone Update Notices (`refs/tags/notices`)

## Motivation

`colab update` and the daily background update check notify users when a newer
version is published on PyPI, but PyPI release metadata is immutable once
published. Maintainers need a lightweight way to surface targeted messages to
existing installations **in the future without cutting a new PyPI release** —
for example:

- Breaking CLI migrations (e.g., "`colab console` was removed; use `colab ssh`
  instead").
- Retroactive security advisories affecting older versions (e.g., `<0.8.0`).
- Upcoming Colab backend or authentication changes that affect **all** current
  releases (`<=X.Y.Z`), even when no newer PyPI version exists yet.

## Architecture

1. **Movable Git Tag (`refs/tags/notices`)**:
   - Active notices live in a single root file `notices.json` under the tag `refs/tags/notices`.
   - Avoids triggering a release build from `cloudbuild.yaml` which triggers on `refs/tags/v*`
2. **CDN Delivery (`raw.githubusercontent.com`)**:
   - Clients fetch `settings.notices_url`, which defaults to:
     `https://raw.githubusercontent.com/googlecolab/google-colab-cli/refs/tags/notices/notices.json`
   - Served by GitHub's Fastly CDN (`Cache-Control: max-age=300`, ~5-minute propagation window)
      with no authentication required
3. **Standalone Evaluation**:
   - Notices are evaluated and displayed via `announce_notices` **independently**
     of whether PyPI reports a newer version (`latest > current`). A user
     already on the latest PyPI version still sees any notice whose `affects`
     specifier matches their installed version.

## Payload Schema

`notices.json` at the root of `refs/tags/notices` is a UTF-8 JSON array (or an
object `{"notices": [...]}`) of rule objects:

```json
[
  {
    "affects": "<0.8.0",
    "message": "Versions <0.8.0 logged credentials to ~/.config/colab-cli/colab.log; upgrade and delete old logs."
  },
  {
    "affects": "<0.9.0",
    "message": "colab console was removed; use colab ssh instead."
  }
]
```

| Field | Type | Rules |
|---|---|---|
| `affects` | `str` | Non-empty PEP 440 version specifier parsed with `packaging.specifiers.SpecifierSet(..., prereleases=True)`. Examples: `<0.9.0`, `<=0.8.0`, `>=0.7.0,<0.8.0`, `==0.8.2`, `>=0.0.0`. |
| `message` | `str` | Plain-text notice (`<= 500` characters). Clients strip ANSI escape sequences and control characters, deduplicate identical messages, and display at most `3` matching notices. |

Unknown JSON fields are ignored so future schema additions remain backward
compatible with older CLI installations.

## Maintainer Workflow (`tools/notices.py`)

`tools/notices.py` manages `refs/tags/notices` using low-level Git plumbing
(`git hash-object`, `git mktree`, `git commit-tree`, `git tag -fa`). It **never**
checks out branches or modifies your `HEAD`, staging area, or working directory,
and it chains each update onto the previous `refs/tags/notices` commit so
`git log notices` preserves a linear audit history.

By default, commands update only your local `refs/tags/notices` tag so you can
inspect and preview first; pass `--push` to publish immediately to `origin`.

### 1. List Active Notices & Preview Client Output

```bash
# Fetch refs/tags/notices from origin and display active JSON rules:
./tools/notices.py list

# Preview the exact banner a user on version 0.8.0 will see:
./tools/notices.py list --version 0.8.0
```

### 2. Add a Notice

```bash
# Stage locally first, then preview:
./tools/notices.py add \
  --affects "<0.9.0" \
  --message "colab console was removed; use colab ssh instead."
./tools/notices.py list --no-fetch --version 0.8.5

# Or validate, commit to refs/tags/notices, and push in one step:
./tools/notices.py add \
  --affects "<0.9.0" \
  --message "colab console was removed; use colab ssh instead." \
  --push
```

`tools/notices.py` rejects empty/invalid PEP 440 specifiers, messages containing
ANSI/control characters, and messages over 500 characters before creating the
tag.

### 3. Replace the Full Notice List from a File

When editing or reordering multiple notices at once:

```bash
./tools/notices.py set path/to/notices.json --push
```

(Pass `-` instead of a file path to read JSON from `stdin`.)

### 4. Clear / Retract Notices

There are two equivalent ways to retract all notices:

```bash
# Option 1 (recommended): publish an empty [] list, keeping git log history on refs/tags/notices
./tools/notices.py clear --push

# Option 2: delete refs/tags/notices from origin (HTTP 404 also clears client caches)
./tools/notices.py clear --delete-tag --push
```

## Client Behavior & Caching

- **Schedule**: Fetched once every 24 hours alongside PyPI during non-suppressed
  commands (`run_background_check`) and unconditionally on `colab update`.
- **Persistence**: Cached as `update_notices` in `~/.config/colab-cli/settings.json`.
  - **HTTP 200**: Replaces `update_notices` with the newly fetched list.
  - **HTTP 404**: Silently sets `update_notices = []` with **no** warning even on
    explicit `colab update` (both before the `notices` tag is ever created and
    after tag deletion).
  - **Network / 5xx Error**: Preserves the previously cached `update_notices`.
- **Post-Upgrade Suppression**: `maybe_show_cached_banner` re-evaluates
  `affects` against `get_app_version()` on every run, so once a user upgrades
  past the affected range, cached notices stop displaying immediately without
  waiting for the next 24-hour fetch.
- **Banner Formatting**:
  ```text
  [colab] Notice: colab console was removed; use colab ssh instead.
  [colab] To silence this check, set "enable_update_check": false in ~/.config/colab-cli/settings.json
  ```
  - Explicit `colab update` omits the `"To silence this check..."` line.
  - When both an upgrade banner and standalone notices match on an unsolicited
    check, the silence hint appears only once at the bottom.
  - Setting `"enable_update_check": false` in `~/.config/colab-cli/settings.json`
    disables both the PyPI check and standalone notices.
  - Pipeable and machine-parsed subcommands in `_AUTO_UPDATE_SUPPRESSED` (and
    non-TTY `ssh`) in `cli.py` never print notices.

## Testing Strategy

- **Runtime Unit Tests (`tests/test_update.py`, `tests/test_state.py`)**:
  Standalone notice display when up to date (explicit, background, and cached),
  combined upgrade + notice rendering without duplicate silence hints, PEP 440
  range filtering across skipped versions, immediate cache suppression after
  upgrade, silent HTTP 404 clearing, transient error cache preservation, ANSI
  and length sanitization, and `enable_update_check=False` suppression.
- **Tool Unit Tests (`tests/test_notices_tool.py`)**:
  Verifies `list`, `list --version`, `add`, `set`, `clear`, and
  `clear --delete-tag` against temporary bare `origin` + multi-clone Git
  repositories, ensuring `HEAD` and dirty working-tree files remain untouched
  and force-moved tag history chains cleanly across clones.
- **End-to-End Integration Test (`integration/repro_update_notices/test.sh`)**:
  Verifies live PyPI + live GitHub CDN 404 handling, local HTTP notice serving,
  offline throttled cache playback, suppressed subcommands, and 404 retraction.
