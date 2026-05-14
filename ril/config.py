from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import typer

CONFIG_DIR = Path.home() / ".config" / "ril"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_DATA_FOLDER = Path.home() / "ReadItLater"


def load_config() -> Optional[dict]:
    if not CONFIG_FILE.exists():
        return None
    with CONFIG_FILE.open() as f:
        return json.load(f)


def save_config(data_folder: Path) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with CONFIG_FILE.open("w") as f:
        json.dump({"data_folder": str(data_folder)}, f, indent=2)


def get_data_folder() -> Path:
    env_override = os.environ.get("RIL_DATA_FOLDER")
    if env_override:
        folder = Path(env_override).expanduser().resolve()
        _initialise_data_folder(folder)
        return folder
    config = load_config()
    if config is None:
        return Path(run_setup_wizard())
    return Path(config["data_folder"])


def run_setup_wizard() -> str:
    typer.echo("")
    typer.echo("Welcome to Read It Later (ril)!")
    typer.echo("No configuration found — let's set things up.\n")

    raw = typer.prompt(
        "Where should articles be stored?",
        default=str(DEFAULT_DATA_FOLDER),
    )
    folder = Path(raw).expanduser().resolve()

    _initialise_data_folder(folder)
    save_config(folder)

    typer.echo(f"\nConfiguration saved. Articles will be stored in: {folder}\n")
    return str(folder)


def _initialise_data_folder(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    articles_dir = folder / "articles"
    articles_dir.mkdir(exist_ok=True)

    index_file = folder / "index.json"
    if not index_file.exists():
        with index_file.open("w") as f:
            json.dump({"version": 1, "articles": []}, f, indent=2)
