# Read It Later (`ril`)

A command-line tool to save, store, and track articles for later reading. Articles are fetched, extracted, and converted to Markdown files on your filesystem. No database required — everything lives in plain files.

## Features

- Fetch any article URL and save it as a clean Markdown file
- Extracts title, author, published date, tags, and description automatically
- Stores one file per article with a timestamp + ID in the filename (auto-ordered in file managers)
- Tracks read/unread status in a local `index.json`
- Gracefully handles unreachable URLs (saves a stub entry)
- Prevents duplicate saves of the same URL
- Mobile-first web reader (`ril serve`) that also works as a two-pane desktop app
- Export the whole library to a `.zip`, and restore it on another machine

## Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Installation

### With uv (recommended)

```bash
git clone https://github.com/yourname/read-it-later.git
cd read-it-later
uv sync
uv pip install -e .
```

Then run `ril` via:

```bash
uv run ril
# or activate the venv:
source .venv/bin/activate
ril
```

### With pip

```bash
pip install -e .
```

## First Run

On the first run, `ril` will ask where you want to store your articles:

```
Welcome to Read It Later (ril)!
No configuration found — let's set things up.

Where should articles be stored? [~/ReadItLater]: 
```

The config is saved to `~/.config/ril/config.json`. The data folder will contain:

```
~/ReadItLater/
├── index.json          ← article catalog
└── articles/
    ├── 20260514T180000Z_ed6348d6_build-it-yourself.md
    └── ...
```

## Usage

### Save an article

```bash
ril add https://example.com/some-article
```

### Save an article already marked as read

```bash
ril add --read https://example.com/some-article
```

### List all saved articles

```bash
ril list
```

```
  ID         Title                  Author           Date         Status
 ─────────────────────────────────────────────────────────────────────────
  ed6348d6   Build It Yourself      Armin Ronacher   2025-01-24   unread
  5bad7d6c   Leaky Abstractions     Joel Spolsky     2002-11-11   ✓ read

2 article(s)  •  1 unread
```

### List only unread articles

```bash
ril list --unread
```

### Open an article in your editor (and mark it as read)

```bash
ril open ed6348d6
```

Uses `$EDITOR`. Pass `--no-mark` to open without marking as read.

### Mark an article as read or unread

```bash
ril mark ed6348d6            # mark as read
ril mark --unread ed6348d6   # mark as unread
```

### Delete an article

```bash
ril delete ed6348d6           # prompts for confirmation
ril delete --force ed6348d6   # skip confirmation
```

### Export a backup

```bash
ril export                              # writes to your saved export folder
ril export -o ~/Dropbox                 # a directory: timestamped file inside it
ril export -o ~/Dropbox/my-library.zip  # an exact file path
```

The archive holds `index.json` plus every article `.md`, so it is a complete,
portable copy of your library.

`ril backup` is the old name for this command. It still works and does exactly
the same thing, but `ril export` is the name to use.

You can also export from the web reader: **⋯ → Export backup (.zip)**.

### Restore from a backup

```bash
ril import backup ~/Dropbox/ril-export-2026-08-30_09-14-22.zip
```

This **replaces** your library — every article not in the archive is deleted, so
the result matches the archive exactly rather than merging with what you have.
Because of that:

- It always shows what will change and asks for confirmation. Use `--dry-run` to
  see the report and stop, or `--force` to skip the prompt in a script.
- Before touching anything it saves a snapshot of your current data next to your
  data folder (`ril-pre-restore-<timestamp>.zip`), so the operation can be undone
  by importing that snapshot. Pass `--no-snapshot` to skip it.
- Running the same import twice leaves the library in the same state.
- The archive is validated before anything is written: unsafe paths, corrupt
  zips, and archives that are not ril backups are rejected without touching your
  library.

```bash
ril import backup backup.zip --dry-run   # report only, changes nothing
ril import backup backup.zip --force     # no confirmation prompt
```

The web reader has the same flow under **⋯ → Import backup…**, which previews the
archive and requires an explicit confirmation before replacing anything.

## Article File Format

Each saved article is a Markdown file with YAML front matter:

```markdown
---
id: ed6348d6
title: Build It Yourself
url: https://lucumr.pocoo.org/2025/1/24/build-it-yourself/
author: Armin Ronacher
published_date: 2025-01-24
saved_at: 2026-05-14T18:02:03Z
tags: [thoughts, rust]
---

# Build It Yourself

Article body in Markdown...
```

Filenames follow the pattern `{UTC_TIMESTAMP}_{ID}_{TITLE_SLUG}.md`, which ensures chronological ordering in any file manager.

## How It Works

1. `ril add <url>` fetches the page with `httpx`
2. [trafilatura](https://trafilatura.readthedocs.io/) extracts the article metadata and body, outputting clean Markdown
3. If trafilatura cannot extract content, [markdownify](https://github.com/matthewwithanm/python-markdownify) is used as a fallback to convert the raw HTML
4. The article is saved as a `.md` file and an entry is added to `index.json`
5. Each article's ID is the first 8 hex characters of the SHA-256 hash of its URL — stable, short, and guaranteed unique per URL

## Syncing with a hosted copy

`ril` can point at a hosted instance of itself. The command line stays the
place articles are fetched, because it runs where the browser cookies are and
can get past a paywall; the hosted copy is for reading on other devices.

Sign in once, with a token the server issues:

```bash
ril sync login --url https://example.com/ril
# prompts for the token without echoing it
ril sync status         # where it points, and whether the token still works
ril sync run            # exchange changes now
ril sync run --dry-run  # show what would be sent, change nothing
ril sync logout         # forget the token
```

### When it runs

`add`, `mark`, `delete` and `refresh` sync once when they finish. One exchange
carries both directions, so that single call pushes what you just did and pulls
whatever the hosted copy has. `list` syncs first, but only if the last one was
over five minutes ago.

**Sync never makes a command fail.** The article is already saved by the time
it runs, so a server that is asleep, slow or unreachable prints one dim line
and nothing more. The cursors only move on success, so whatever was missed goes
with the next sync — a laptop that was offline for a month catches up in one go.

### How changes are reconciled

Article ids are a digest of the URL, so both sides give the same article the
same id without arranging anything. A record is then merged in three parts,
each settling on its own:

| Part | Rule |
| --- | --- |
| Body and metadata | A fetched body beats a paywall stub, however new the stub is. Between two real bodies, the newer wins |
| Read or unread | The newer change wins |
| Deleted or not | The newer change wins, so saving a URL again undoes an older delete |

The first rule is the point of the whole arrangement. The command line runs
where your browser cookies are, so its copy of a paywalled article is the real
one, and the hosted copy's later, emptier attempt must not overwrite it. If the
server saved a stub, `ril refresh <id>` on your machine replaces it everywhere.

Both sides run identical rules, so the order of syncs does not matter and
neither does how long one side was offline. The rules are commutative,
idempotent and associative, and the tests check that over thousands of
randomised pairs.

**Deletes leave a tombstone** — the row stays, with the body file removed. A row
that simply vanished cannot be told apart from one the other side has not sent
yet, and would come back on the next sync. Tombstones are never listed, counted
or opened.

The token is **scoped**: it opens the hosted instance's sync endpoints and
nothing else — not the hub it sits behind, and not the import endpoint that
would replace the whole data folder. Getting one is a server-side step.

`ril sync login` checks the credential against the server before storing it, so
a wrong token is never written to disk. It still stores a token the server
accepted even when the library behind the gateway is down or too old to sync —
those say nothing about whether the credential is good.

Two rules worth knowing:

- **https is required.** A bearer token sent over plain `http` can be read in
  transit, so anything else is refused. `http://localhost` is allowed, because
  it never leaves the machine.
- **The token is not kept with the settings.** It goes in
  `~/.config/ril/credentials.json`, created readable by you only (`0600`), in a
  directory set to `0700`. `RIL_SYNC_URL` and `RIL_SYNC_TOKEN` override both
  when set, which is how to run without touching the config at all.

## Configuration

Config file: `~/.config/ril/config.json`

```json
{
  "data_folder": "/Users/you/ReadItLater",
  "backup_folder": "/Users/you/Desktop",
  "sync_url": "https://example.com/ril"
}
```

`backup_folder` is where `ril export` and `ril backup` write when no `--output`
is given. You are asked for it the first time and it is remembered afterwards.

`sync_url` is set by `ril sync login`. Only non-secret settings live here; the
sync token is kept separately, in `~/.config/ril/credentials.json`.
