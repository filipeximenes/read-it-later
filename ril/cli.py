from __future__ import annotations

import os
import subprocess
from pathlib import Path

import typer
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from ril.config import get_data_folder
from ril.extractor import fetch_and_extract
from ril.models import Article
from ril.storage import (
    delete_article,
    get_article_path,
    load_index,
    save_article,
    update_article,
)

app = typer.Typer(
    name="ril",
    help="Read It Later — save articles to read offline.",
    no_args_is_help=True,
)
console = Console()
err_console = Console(stderr=True)


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


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

@app.command(name="list")
def list_articles(
    all_articles: bool = typer.Option(False, "--all", "-a", help="Show all articles (read and unread)"),
    read: bool = typer.Option(False, "--read", "-r", help="Show only read articles"),
):
    """List saved articles. Shows only unread articles by default."""
    data_folder = _data_folder()
    index = load_index(data_folder)

    if all_articles:
        articles = index.articles
    elif read:
        articles = [a for a in index.articles if a.read]
    else:
        articles = [a for a in index.articles if not a.read]

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
    total = len(index.articles)
    unread_count = sum(1 for a in index.articles if not a.read)
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
    article_path = get_article_path(data_folder, article)

    editor = os.environ.get("EDITOR", "vi")
    subprocess.run([editor, str(article_path)])

    if not no_mark and not article.read:
        article.mark_read()
        update_article(data_folder, article)
        console.print(f"[green]Marked as read:[/green] {article.title}")


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

    delete_article(data_folder, article)
    console.print(f"[red]Deleted:[/red] {article.title}")


if __name__ == "__main__":
    app()
