from __future__ import annotations

import os
import subprocess
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich import box
from rich.console import Console
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn
from rich.table import Table
from rich.text import Text

from ril import pocket
from ril.archive import (
    ArchiveError,
    create_archive,
    export_filename,
    restore_archive,
)
from ril.config import (
    get_backup_folder,
    get_data_folder,
    get_sync_url,
    save_backup_folder,
    save_sync_url,
)
from ril.extractor import fetch_and_extract
from ril.models import Article
from ril.remote import (
    CREDENTIALS_FILE,
    Remote,
    RemoteClient,
    RemoteError,
    RemoteNotConfiguredError,
    RemoteStatus,
    forget_token,
    load_remote,
    normalise_url,
    save_token,
)
from ril.storage import (
    delete_article,
    get_article_path,
    load_index,
    refresh_article,
    save_article,
    update_article,
)
from ril.sync import sync_is_stale, sync_once, sync_preview

app = typer.Typer(
    name="ril",
    help="Read It Later — save articles to read offline.",
    no_args_is_help=True,
)
console = Console()
err_console = Console(stderr=True)

import_app = typer.Typer(help="Import from external sources.", no_args_is_help=True)
app.add_typer(import_app, name="import")

sync_app = typer.Typer(help="Sync with a hosted copy of this library.", no_args_is_help=True)
app.add_typer(sync_app, name="sync")


def _data_folder() -> Path:
    return get_data_folder()


def _resolve_article(data_folder: Path, article_id: str) -> Article:
    index = load_index(data_folder)
    article = index.find_by_id(article_id)
    if article is None:
        err_console.print(f"[red]No article found with ID:[/red] {article_id}")
        raise typer.Exit(1)
    return article


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


@app.command()
def add(
    url: str = typer.Argument(..., help="URL of the article to save"),
    read: bool = typer.Option(False, "--read", "-r", help="Mark the article as already read"),
):
    """Fetch and save an article for later reading."""
    data_folder = _data_folder()

    # Duplicate check
    index = load_index(data_folder)
    existing = index.find_by_url(url)
    if existing is not None:
        err_console.print(
            f"[yellow]Article already saved[/yellow] (id: [bold]{existing.id}[/bold]): "
            f"{existing.title}"
        )
        raise typer.Exit(0)

    with console.status("[bold cyan]Fetching article…[/bold cyan]"):
        extracted = fetch_and_extract(url)

    if extracted.fetch_failed:
        err_console.print(
            f"[yellow]Warning:[/yellow] Could not fetch the article — "
            f"saving a stub entry.\n  {extracted.error}"
        )

    article = save_article(data_folder, extracted, read=read)

    status_label = "[green]read[/green]" if read else "[dim]unread[/dim]"
    console.print(
        f"[bold green]Saved[/bold green] ({status_label}) "
        f"[bold]{article.title}[/bold]  [dim](id: {article.id})[/dim]"
    )
    _sync_quietly(data_folder)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@app.command(name="list")
def list_articles(
    all_articles: bool = typer.Option(
        False, "--all", "-a", help="Show all articles (read and unread)"
    ),
    read: bool = typer.Option(False, "--read", "-r", help="Show only read articles"),
):
    """List saved articles. Shows only unread articles by default."""
    data_folder = _data_folder()
    # Reading is worth a pull, but not on every single command.
    if sync_is_stale(data_folder):
        _sync_quietly(data_folder)
    index = load_index(data_folder)

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None)

    if all_articles:
        articles = sorted(index.live, key=lambda a: _naive(a.saved_at), reverse=True)
    elif read:
        articles = sorted(
            (a for a in index.live if a.read),
            key=lambda a: _naive(a.read_at or a.saved_at),
            reverse=True,
        )
    else:
        articles = sorted(
            (a for a in index.live if not a.read),
            key=lambda a: _naive(a.saved_at),
        )

    if not articles:
        if read:
            console.print("[dim]No read articles.[/dim]")
        elif all_articles:
            console.print(
                "[dim]No articles saved yet. Use [bold]ril add <url>[/bold] to get started.[/dim]"
            )
        else:
            console.print("[dim]No unread articles.[/dim]")
        return

    table = Table(box=box.SIMPLE_HEAD, show_footer=False, highlight=True)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Title")
    table.add_column("Author", style="dim")
    table.add_column("Date", style="dim", no_wrap=True)
    if all_articles:
        table.add_column("Status", no_wrap=True)

    for article in articles:
        title_text = Text(article.title, style="bold" if not article.read else "")
        if article.fetch_failed:
            title_text.append("  [fetch failed]", style="red dim")

        row = [
            article.id,
            title_text,
            article.author or "—",
            article.published_date or "—",
        ]
        if all_articles:
            status = Text("✓ read", style="green") if article.read else Text("unread", style="dim")
            row.append(status)

        table.add_row(*row)

    console.print(table)
    total = len(index.live)
    unread_count = sum(1 for a in index.live if not a.read)
    console.print(f"[dim]{total} article(s)  •  {unread_count} unread[/dim]")


# ---------------------------------------------------------------------------
# open
# ---------------------------------------------------------------------------


@app.command(name="open")
def open_article(
    article_id: str = typer.Argument(..., help="Article ID (from ril list)"),
    no_mark: bool = typer.Option(False, "--no-mark", help="Open without marking as read"),
):
    """Open an article in $EDITOR and mark it as read."""
    data_folder = _data_folder()
    article = _resolve_article(data_folder, article_id)
    try:
        article_path = get_article_path(data_folder, article)
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    editor = os.environ.get("EDITOR", "vi")
    subprocess.run([editor, str(article_path)])

    if not no_mark and not article.read:
        article.mark_read()
        update_article(data_folder, article)
        console.print(f"[green]Marked as read:[/green] {article.title}")
    _sync_quietly(data_folder)


# ---------------------------------------------------------------------------
# mark
# ---------------------------------------------------------------------------


@app.command()
def mark(
    article_id: str = typer.Argument(..., help="Article ID (from ril list)"),
    unread: bool = typer.Option(False, "--unread", "-u", help="Mark as unread instead of read"),
):
    """Toggle read/unread status of an article."""
    data_folder = _data_folder()
    article = _resolve_article(data_folder, article_id)

    if unread:
        article.mark_unread()
        update_article(data_folder, article)
        console.print(f"[dim]Marked as unread:[/dim] {article.title}")
    else:
        article.mark_read()
        update_article(data_folder, article)
        console.print(f"[green]Marked as read:[/green] {article.title}")


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@app.command()
def delete(
    article_id: str = typer.Argument(..., help="Article ID (from ril list)"),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation prompt"),
):
    """Delete an article from the index and filesystem."""
    data_folder = _data_folder()
    article = _resolve_article(data_folder, article_id)

    if not force:
        confirmed = typer.confirm(f'Delete "{article.title}"?', default=False)
        if not confirmed:
            console.print("[dim]Aborted.[/dim]")
            raise typer.Exit(0)

    try:
        delete_article(data_folder, article)
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[red]Deleted:[/red] {article.title}")
    _sync_quietly(data_folder)


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------


@app.command()
def refresh(
    article_id: str = typer.Argument(..., help="Article ID to re-fetch"),
):
    """Re-fetch a saved article and update its content."""
    data_folder = _data_folder()
    article = _resolve_article(data_folder, article_id)

    with console.status("[bold cyan]Fetching article…[/bold cyan]"):
        extracted = fetch_and_extract(article.url)

    if extracted.fetch_failed:
        err_console.print(
            f"[yellow]Warning:[/yellow] Could not fetch the article — "
            f"updating stub entry.\n  {extracted.error}"
        )

    refresh_article(data_folder, article, extracted)
    console.print(
        f"[bold green]Refreshed[/bold green] "
        f"[bold]{article.title}[/bold]  [dim](id: {article.id})[/dim]"
    )
    _sync_quietly(data_folder)


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def _resolve_export_path(output: Optional[Path]) -> Path:
    """Turn --output into a full .zip path, asking for a folder on first use."""
    if output is not None:
        destination = Path(output).expanduser().resolve()
        # An existing directory, or a path without a .zip suffix, means "put it in here".
        if destination.is_dir() or destination.suffix.lower() != ".zip":
            return destination / export_filename()
        return destination

    destination = get_backup_folder()
    if destination is None:
        raw = typer.prompt(
            "Where should exports be saved?",
            default=str(Path.home() / "Desktop"),
        )
        destination = Path(raw).expanduser().resolve()
        save_backup_folder(destination)
        console.print("[dim]Export location saved to config.[/dim]")

    return Path(destination).expanduser().resolve() / export_filename()


def _write_export(output: Optional[Path]) -> None:
    data_folder = _data_folder()
    zip_path = _resolve_export_path(output)

    with console.status("[bold cyan]Creating archive\u2026[/bold cyan]"):
        create_archive(data_folder, zip_path)

    size_mb = zip_path.stat().st_size / (1024 * 1024)
    article_count = len(load_index(data_folder).live)
    console.print(
        f"[bold green]Exported[/bold green] {article_count} article(s) "
        f"[dim]({size_mb:.1f} MB)[/dim] \u2192 {zip_path}"
    )


@app.command(name="export")
def export_data(
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Destination .zip file, or a directory to write a timestamped file into",
    ),
):
    """Export every article and the index into a single .zip archive."""
    _write_export(output)


@app.command(hidden=True)
def backup(
    output: Optional[Path] = typer.Option(
        None, "--output", "-o", help="Directory to save the backup file"
    ),
):
    """Deprecated name for `ril export`."""
    console.print("[dim]`ril backup` is now `ril export`.[/dim]")
    _write_export(output)


# ---------------------------------------------------------------------------
# import pocket
# ---------------------------------------------------------------------------


@import_app.command("pocket")
def import_pocket(
    csv_path: Path = typer.Argument(
        ...,
        help="Pocket export CSV (for example part_000000.csv)",
    ),
):
    """Import articles from a Pocket CSV export."""
    data_folder = _data_folder()
    path = csv_path.expanduser().resolve()
    if not path.is_file():
        err_console.print(f"[red]Not a file:[/red] {path}")
        raise typer.Exit(1)

    rows = list(pocket.pocket_rows(path))
    if not rows:
        err_console.print("[yellow]No rows found in CSV[/yellow] (header only or empty file).")
        raise typer.Exit(0)

    saved = skipped_dup = skipped_invalid = errors = 0

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("[cyan]Importing…[/cyan]", total=len(rows))
        for row_index, row in enumerate(rows, start=2):
            url = row.get("url", "").strip()
            if not url:
                err_console.print(f"[yellow]Skipping row {row_index}:[/yellow] empty url")
                skipped_invalid += 1
                progress.advance(task)
                continue

            try:
                index = load_index(data_folder)
                existing = index.find_by_url(url)
                if existing is not None:
                    err_console.print(
                        f"[yellow]Already saved[/yellow] (id: [bold]{existing.id}[/bold]): "
                        f"{existing.title}"
                    )
                    skipped_dup += 1
                else:
                    saved_at, time_fallback = pocket.parse_time_added(row.get("time_added"))
                    if time_fallback:
                        err_console.print(
                            f"[yellow]Warning:[/yellow] row {row_index} — invalid or missing "
                            f"time_added for {url}; using current UTC time instead."
                        )

                    extracted = fetch_and_extract(url)
                    extracted = pocket.apply_pocket_title_fallback(row.get("title"), url, extracted)

                    save_article(
                        data_folder,
                        extracted,
                        read=pocket.status_to_read(row.get("status")),
                        saved_at=saved_at,
                    )
                    saved += 1
            except Exception as exc:
                err_console.print(
                    f"[red]Error[/red] row {row_index} {url}: {type(exc).__name__}: {exc}"
                )
                errors += 1

            progress.advance(task)

    parts = (
        f"[bold]Done[/bold] — saved: [green]{saved}[/green]; "
        f"skipped duplicate: {skipped_dup}; "
        f"skipped invalid: {skipped_invalid}; "
    )
    if errors:
        console.print(parts + f"errors: [red bold]{errors}[/red bold]")
    else:
        console.print(parts + f"errors: {errors}")


# ---------------------------------------------------------------------------
# import backup
# ---------------------------------------------------------------------------


@import_app.command("backup")
def import_backup(
    zip_path: Path = typer.Argument(..., help="A .zip archive produced by `ril export`"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would be restored without changing anything"
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Skip the confirmation prompt"),
    no_snapshot: bool = typer.Option(
        False, "--no-snapshot", help="Do not save a snapshot of the current data first"
    ),
):
    """Rebuild the library from a backup zip, replacing all existing data."""
    data_folder = _data_folder()
    path = zip_path.expanduser().resolve()

    try:
        preview = restore_archive(data_folder, path, dry_run=True)
    except ArchiveError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    summary = preview.summary
    console.print(f"[bold]Archive:[/bold] {path}")
    console.print(
        f"  [green]{summary.article_count}[/green] article(s) in the index, "
        f"{summary.file_count} markdown file(s)"
    )
    if summary.missing_files:
        console.print(
            f"  [yellow]{len(summary.missing_files)} indexed article(s) have no file[/yellow] "
            "and will be restored without content"
        )
    if summary.orphan_files:
        console.print(
            f"  [yellow]{len(summary.orphan_files)} file(s) are not in the index[/yellow] "
            "and will be ignored"
        )
    if summary.skipped_entries:
        console.print(f"  [dim]{len(summary.skipped_entries)} unrelated entry(ies) skipped[/dim]")

    console.print(f"[bold]Target:[/bold] {data_folder}")
    console.print(
        f"  [red]{preview.replaced_articles} existing article(s) will be deleted[/red] "
        "and replaced by the archive contents"
    )

    if dry_run:
        console.print("\n[dim]Dry run \u2014 nothing was changed.[/dim]")
        raise typer.Exit(0)

    if not force:
        console.print("")
        confirmed = typer.confirm(
            f"Replace ALL data in {data_folder} with this archive?",
            default=False,
        )
        if not confirmed:
            console.print("[dim]Aborted.[/dim]")
            raise typer.Exit(0)

    try:
        with console.status("[bold cyan]Restoring\u2026[/bold cyan]"):
            result = restore_archive(data_folder, path, make_snapshot=not no_snapshot)
    except ArchiveError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    console.print(
        f"[bold green]Restored[/bold green] {result.summary.article_count} article(s) "
        f"into {data_folder}"
    )
    if result.snapshot_path is not None:
        console.print(f"[dim]Previous data saved to: {result.snapshot_path}[/dim]")


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


@app.command()
def serve(
    port: int = typer.Option(8484, "--port", "-p", help="Port to listen on"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't open browser automatically"),
):
    """Start a local web server and open the reader UI in your browser."""
    import threading
    import webbrowser

    import uvicorn

    from ril.server import build_app

    data_folder = _data_folder()
    fastapi_app = build_app(data_folder)

    if not no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(f"http://localhost:{port}")).start()

    console.print(
        f"[bold cyan]Starting server at[/bold cyan] http://localhost:{port}  "
        "[dim](Ctrl+C to stop)[/dim]"
    )
    uvicorn.run(fastapi_app, host="127.0.0.1", port=port, log_level="warning")


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


@contextmanager
def _remote_failure_exits():
    """Turn any failure to reach the remote into a message and a non-zero exit."""
    try:
        yield
    except RemoteError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


@sync_app.command("login")
def sync_login(
    url: Optional[str] = typer.Option(
        None, "--url", help="Base URL of the hosted instance, e.g. https://example.com/ril"
    ),
    token: Optional[str] = typer.Option(
        None,
        "--token",
        help="Sync token. Omit to be prompted without echo, which keeps it out of shell history.",
    ),
):
    """Store the credential for a hosted instance, after checking it works."""
    raw_url = url or get_sync_url() or typer.prompt("Base URL of the hosted instance")
    # Checked before the token is asked for, so a bad URL is not found out late.
    with _remote_failure_exits():
        checked_url = normalise_url(raw_url)

    secret = (token or typer.prompt("Sync token", hide_input=True)).strip()
    if not secret:
        err_console.print("[red]A sync token is required.[/red]")
        raise typer.Exit(1)

    # Checked before anything is written, so a wrong credential is never stored.
    console.print(f"[dim]Checking {checked_url} …[/dim]")
    with _remote_failure_exits(), RemoteClient(Remote(checked_url, secret)) as client:
        status = client.probe()

    # Every status means the gateway accepted the token, so the credential is
    # worth keeping even when the library behind it is down or out of date.
    save_sync_url(checked_url)
    save_token(secret)
    console.print(f"[green]Signed in to[/green] {checked_url}")
    if status is not RemoteStatus.READY:
        console.print(f"[yellow]Note:[/yellow] {status.value}.")
    console.print(f"[dim]Token stored in {CREDENTIALS_FILE} (readable by you only).[/dim]")


@sync_app.command("status")
def sync_status():
    """Show where sync points and whether the stored credential still works."""
    with _remote_failure_exits():
        remote = load_remote()
        console.print(f"[bold]Remote:[/bold] {remote.url}")
        with RemoteClient(remote) as client:
            status = client.probe()
    console.print(f"[green]Authorised[/green] — {status.value}")


@sync_app.command("run")
def sync_run(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be sent, and change nothing."
    ),
):
    """Exchange changes with the hosted copy."""
    data_folder = _data_folder()
    if dry_run:
        console.print(
            f"[dim]Would send {sync_preview(data_folder)} record(s). Nothing was changed.[/dim]"
        )
        return

    with _remote_failure_exits():
        remote = load_remote()
        with RemoteClient(remote) as client:
            report = sync_once(data_folder, client)

    if report.quiet:
        console.print("[dim]Already up to date.[/dim]")
        return
    console.print(
        f"[green]Synced[/green] — sent {report.sent}, received {report.received}, "
        f"bodies out {report.bodies_sent}, bodies in {report.bodies_received}"
    )


def _sync_quietly(data_folder: Path) -> None:
    """Sync in the background of another command, and never get in its way.

    A command that saved or changed something has already succeeded by the
    time this runs. Sync is how that reaches the other side eventually, so a
    server that is asleep, slow or unreachable must not turn a working command
    into a failing one. The cursors are only moved on success, so whatever was
    missed goes with the next sync.
    """
    try:
        remote = load_remote()
    except RemoteNotConfiguredError:
        return
    except RemoteError as exc:
        err_console.print(f"[dim]Sync skipped: {exc}[/dim]")
        return

    try:
        with RemoteClient(remote) as client:
            report = sync_once(data_folder, client)
    except RemoteError as exc:
        err_console.print(f"[dim]Sync skipped: {exc}[/dim]")
        return
    if not report.quiet:
        console.print(f"[dim]Synced — sent {report.sent}, received {report.received}.[/dim]")


@sync_app.command("logout")
def sync_logout():
    """Forget the stored sync token. The URL setting is left in place."""
    if forget_token():
        console.print("[green]Sync token removed.[/green]")
    else:
        console.print("[dim]No sync token was stored.[/dim]")


if __name__ == "__main__":
    app()
