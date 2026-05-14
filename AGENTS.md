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
| `ril/cli.py` | Typer CLI commands: `add`, `list`, `open`, `mark`, `delete`, `backup`, `serve` |
| `ril/server.py` | FastAPI web server + embedded single-page app (HTML/CSS/JS as a string constant) |

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

The entire frontend (HTML, CSS, vanilla JS, marked.js CDN) is a single string constant
`_HTML` inside `ril/server.py`. There are no separate static files — all UI changes
must be made there.
