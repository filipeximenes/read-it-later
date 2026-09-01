# Agent Guidelines for read-it-later

## Protecting Production Data

The `ril` tool stores real article data in a user-configured folder (typically `~/ReadItLater`).
When running any `ril` command during development or testing, you MUST set the
`RIL_DATA_FOLDER` environment variable to a temporary directory:

```bash
RIL_DATA_FOLDER=/tmp/ril-dev ril <command>
```

This overrides the real config and keeps all test operations isolated.
Never run `ril` commands without this prefix unless the user explicitly asks
you to operate on their real data.

**`RIL_DATA_FOLDER` does not isolate sync.** The sync URL and token live in
`~/.config/ril`, so any command that syncs — `ril serve` (one quiet sync at
startup), `add`, `delete`, `mark`, `sync run` — still talks to the user's real
hosted copy, and will push whatever is in the temporary folder up to it and
pull the real library down into it. To run the server against throwaway data,
skip the CLI and start the app directly:

```bash
python -m uvicorn --host 127.0.0.1 --port 8931 mydemo:app   # mydemo.py: app = build_app(Path("/tmp/ril-dev"))
```

## Installing latest version

After making code changes, reinstall the tool with:

```bash
make install
```

`uv tool install` writes to `~/.local/share/uv/tools/`, which is outside the workspace.
In sandboxed environments you must run `make install` with full filesystem permissions
(e.g. `required_permissions: ["all"]` in shell tool calls), otherwise the command will
fail with "Operation not permitted".

## Module responsibilities

| Module | Role |
|--------|------|
| `ril/models.py` | Pydantic models: `Article`, `Index` |
| `ril/storage.py` | All disk I/O: `load_index`, `save_index`, `update_article`, `save_article`, `delete_article`, `get_article_path` |
| `ril/extractor.py` | Fetch and parse articles from URLs (`fetch_and_extract`) |
| `ril/archive.py` | Backup zips: `create_archive`, `inspect_archive`, `restore_archive` |
| `ril/cli.py` | Typer CLI commands: `add`, `list`, `open`, `mark`, `delete`, `refresh`, `export`, `import pocket`, `import backup`, `serve` (plus `backup`, a hidden deprecated alias of `export`) |
| `ril/server.py` | FastAPI web server; serves the reader page and the JSON API |
| `ril/web/` | The reader front end: `index.html` plus three stylesheets and eight ES modules (see below) |

## Markdown file format

Every saved article lives at `{data_folder}/articles/{filename}.md`. The file starts with
a YAML front matter block that must be stripped before processing the body:

```python
import re
_FRONT_MATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n?", re.DOTALL)
body = _FRONT_MATTER_RE.sub("", raw_content, count=1).lstrip()
```

Important: `read` state, `read_at`, and `description` are stored **only** in `index.json`
(via the `Article` model), not in the `.md` file.

## Web server (`ril serve`)

The `serve` command starts a blocking uvicorn process. To test the server without the CLI,
use FastAPI's `TestClient` (backed by `httpx`) for in-process testing:

```python
from pathlib import Path
from fastapi.testclient import TestClient
from ril.server import build_app

client = TestClient(build_app(Path("/tmp/ril-dev")))
response = client.get("/api/articles?filter=unread")
```

`serve` runs one quiet sync before it starts uvicorn, and the page offers a Sync
button that calls `POST /api/sync/run`. Both are no-ops when sync is not set up.
The page asks `GET /api/sync/config` first and hides the button unless a remote
is configured — the hosted copy serves this same page and has nowhere to sync
to. `POST /api/sync/run` requires an `X-RIL-Sync: run` header, the same trick
the import endpoint uses to keep a cross-origin page out.

## The front end (`ril/web/`)

Plain files, no build step, no framework: hand-written HTML, CSS and ES modules,
plus marked.js from a CDN (with a plain-text fallback in `reader.js` for when the
CDN cannot be reached).

| File | Role |
|------|------|
| `index.html` | All the markup, and one inline script that replays the saved reading preferences before the first paint |
| `theme.css` | Design tokens and the four colour schemes (auto / light / sepia / dark) |
| `app.css` | Shell, library pane, menus, sheets, toast, statistics |
| `reader.css` | The reading screen: chrome, prose, dock, settings sheet |
| `ui.js` | `$`, escaping, formatting, and the shared menu / overlay / toast helpers |
| `api.js` | Every call to the server; failures arrive as `ApiError` with a message fit to show |
| `store.js` | The shared state and a small event bus, so no two screens import each other |
| `prefs.js` | Reading preferences — the only place that knows what a preference means in CSS |
| `library.js` | Tabs, search, the list, saving a URL |
| `reader.js` | The reading screen: content, actions, chrome, progress, scroll memory |
| `stats.js` | The statistics sheet and the activity chart |
| `transfer.js` | Sync, export, import |
| `main.js` | One delegated `data-action` handler, the keyboard shortcuts, start-up |

Behaviour is wired by `data-action` attributes handled in `main.js`, never by
inline `onclick` — a module's functions are not global. Screens are shown and
hidden with the `hidden` property (`show()` in `ui.js`), which `[hidden]
{ display: none !important }` in `app.css` makes stick.

`server.py` reads these through `importlib.resources`, so an installed wheel finds
them exactly as a checkout does, and **caches each one for the life of the
process: after editing anything under `ril/web/`, restart the server.**
`GET /static/{name}` serves them, and only names matching
`^[a-z0-9][a-z0-9_-]*\.(css|js)$` that exist in the folder — nothing else is
reachable, whatever a request spells.

They are data files, not modules, so they only reach a wheel because
`[tool.setuptools.package-data]` in `pyproject.toml` names their extensions. A new
kind of file under `ril/web/` has to be named there, and in `_ASSET_TYPES`, too.

The CSS is **mobile-first**: the base rules describe the single-column phone layout
(the library and the reader are two full screens that swap via `body.reading`, each
with its own header — the filter tabs can never appear over an article), and the
single `@media (min-width: 860px)` block at the end of each stylesheet adds the
two-pane desktop layout. Put new rules in the base block and only override in the
media query.

## Destructive operations

`restore_archive` replaces the whole data folder. It validates the zip before
writing anything, writes a
`ril-pre-restore-*.zip` snapshot next to the data folder, and stages the new files
before swapping them in so a failure leaves the folder usable. Both entry points —
`ril import backup` and `POST /api/import` — preview first (`dry_run`) and require
explicit confirmation. The HTTP endpoint additionally requires an
`X-RIL-Confirm: replace-all` header, which a cross-origin page cannot send.

An imported archive is untrusted input. `inspect_archive` rejects member paths
that are absolute or traverse (`..`), symlink members, archives with no
`index.json` or one that does not parse, entry/size limits, and — importantly —
any `Article.filename` in the index that is not a plain filename. That last one
matters because `filename` is joined onto the data folder to read and delete
files: a crafted index could otherwise delete arbitrary paths. `get_article_path`
enforces the same rule again as defense in depth and raises `ValueError`, which
callers must handle.
