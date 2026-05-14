# Read It Later (`ril`)

A command-line tool to save, store, and track articles for later reading. Articles are fetched, extracted, and converted to Markdown files on your filesystem. No database required — everything lives in plain files.

## Features

- Fetch any article URL and save it as a clean Markdown file
- Extracts title, author, published date, tags, and description automatically
- Stores one file per article with a timestamp + ID in the filename (auto-ordered in file managers)
- Tracks read/unread status in a local `index.json`
- Gracefully handles unreachable URLs (saves a stub entry)
- Prevents duplicate saves of the same URL

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

## Configuration

Config file: `~/.config/ril/config.json`

```json
{
  "data_folder": "/Users/you/ReadItLater"
}
```
