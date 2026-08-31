"""Reaching a hosted copy of this library.

The command line stays the place articles are fetched, because it runs on a
machine with browser cookies and can get past a paywall. A hosted `ril serve`
cannot. This module is how the two find each other: where the server is, the
credential that opens it, and a client that turns an HTTP failure into
something a command can act on.

The credential is a scoped bearer token. It opens the sync endpoints of the
hosted instance and nothing else — not the hub it sits behind, not the import
endpoint that would replace the whole data folder.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from urllib.parse import urlparse

import httpx

from ril.config import CONFIG_DIR, get_sync_url

CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"

# Settings live in config.json; the token lives here, readable only by its
# owner. Keeping them apart means the settings file can stay ordinary.
_TOKEN_KEY = "sync_token"

# Loopback is the one place a token may travel in the clear, because it never
# reaches a network. Everywhere else a bearer token needs TLS.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class RemoteError(RuntimeError):
    """Base for every failure to reach or use the hosted instance."""


class RemoteNotConfiguredError(RemoteError):
    """No sync URL or no token. The user has not run `ril sync login`."""


class RemoteAuthError(RemoteError):
    """The server refused the credential. Retrying will not help."""


class RemoteUnavailableError(RemoteError):
    """The server could not be reached, or failed on its own side.

    Always transient as far as a caller is concerned: a command that meets
    this should carry on and let the next sync pick the work up.
    """


class RemoteProtocolError(RemoteError):
    """Reached and authorised, but the server did not answer as expected."""


class RemoteStatus(Enum):
    """What a probe found on the other end."""

    READY = "ready to sync"
    NO_SYNC_ENDPOINT = "this server has no sync endpoints yet"
    UPSTREAM_DOWN = "the library behind the gateway is not answering"


@dataclass(frozen=True)
class Remote:
    """Where the hosted instance is and what opens it."""

    url: str
    token: str

    def endpoint(self, path: str) -> str:
        return f"{self.url}/{path.lstrip('/')}"


def normalise_url(raw: str) -> str:
    """Check a sync URL and return it without a trailing slash.

    A bearer token sent in the clear is a token given away, so anything but
    https is refused unless it is talking to this same machine.
    """
    url = raw.strip().rstrip("/")
    if not url:
        raise RemoteError("A sync URL is required.")

    parsed = urlparse(url)
    if not parsed.scheme:
        raise RemoteError(f"Sync URL needs a scheme: try https://{url}")
    if parsed.scheme == "https":
        return url
    if parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS:
        return url
    raise RemoteError(
        f"Sync URL must use https (got {parsed.scheme}://). "
        "A bearer token sent over plain http can be read in transit."
    )


def load_token() -> Optional[str]:
    """The stored sync token, or None. `RIL_SYNC_TOKEN` wins when set."""
    override = (os.environ.get("RIL_SYNC_TOKEN") or "").strip()
    if override:
        return override
    if not CREDENTIALS_FILE.exists():
        return None
    try:
        with CREDENTIALS_FILE.open() as f:
            stored = json.load(f).get(_TOKEN_KEY)
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(stored, str):
        return None
    return stored.strip() or None


def save_token(token: str) -> None:
    """Write the token so only its owner can read it."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.chmod(stat.S_IRWXU)

    existing: dict = {}
    if CREDENTIALS_FILE.exists():
        try:
            with CREDENTIALS_FILE.open() as f:
                existing = json.load(f)
        except (OSError, json.JSONDecodeError):
            existing = {}
    existing[_TOKEN_KEY] = token.strip()

    # Created with the right mode from the start, so the token is never
    # briefly world-readable between the write and a chmod.
    descriptor = os.open(
        CREDENTIALS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR
    )
    with os.fdopen(descriptor, "w") as f:
        json.dump(existing, f, indent=2)
    CREDENTIALS_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)


def forget_token() -> bool:
    """Remove the stored token. True when there was one to remove."""
    if not CREDENTIALS_FILE.exists():
        return False
    try:
        with CREDENTIALS_FILE.open() as f:
            stored = json.load(f)
    except (OSError, json.JSONDecodeError):
        CREDENTIALS_FILE.unlink()
        return True
    if _TOKEN_KEY not in stored:
        return False
    del stored[_TOKEN_KEY]
    if stored:
        with CREDENTIALS_FILE.open("w") as f:
            json.dump(stored, f, indent=2)
    else:
        CREDENTIALS_FILE.unlink()
    return True


def load_remote() -> Remote:
    """The configured remote, or a `RemoteNotConfiguredError` saying what is missing."""
    url = get_sync_url()
    token = load_token()
    if not url and not token:
        raise RemoteNotConfiguredError("Sync is not set up. Run: ril sync login --url <url>")
    if not url:
        raise RemoteNotConfiguredError("No sync URL is set. Run: ril sync login --url <url>")
    if not token:
        raise RemoteNotConfiguredError("No sync token is stored. Run: ril sync login")
    return Remote(url=normalise_url(url), token=token)


class RemoteClient:
    """Talks to the hosted instance, and names its failures.

    Every response becomes either a body or one of the errors above, so a
    caller never has to read a status code to know what to do.
    """

    def __init__(self, remote: Remote, timeout: httpx.Timeout = _TIMEOUT) -> None:
        self._remote = remote
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            headers={
                "Authorization": f"Bearer {remote.token}",
                "Accept": "application/json",
                "User-Agent": "ril-sync",
            },
        )

    def __enter__(self) -> RemoteClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Reach the server and be let in, or say which of the two failed."""
        url = self._remote.endpoint(path)
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise RemoteUnavailableError(f"Could not reach {url}: {exc}") from exc

        if response.status_code in (401, 403):
            raise RemoteAuthError(
                "The server refused the sync token. Run `ril sync login` to store a new one."
            )
        # A redirect means the gateway wanted a browser session, so the token
        # was not honoured. Following it would post the body to a login form.
        if response.is_redirect:
            raise RemoteAuthError(
                f"The server redirected to {response.headers.get('location', 'a login page')!r} "
                "instead of accepting the token. Check the sync URL and the token."
            )
        return response

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """A sync call. Anything short of a usable answer is an error."""
        response = self._send(method, path, **kwargs)
        if response.status_code == 429:
            raise RemoteUnavailableError("The server is rate limiting. Try again later.")
        if response.status_code >= 500:
            raise RemoteUnavailableError(f"The server failed: {response.status_code}.")
        return response

    def probe(self) -> RemoteStatus:
        """Check the credential without changing anything.

        Every status here means the token was accepted, because the gateway
        refuses an unknown one before it proxies anything. So a 404 says the
        instance behind it is older than this client, and a 5xx says that
        instance is down — neither is a reason to distrust the credential.
        """
        response = self._send("GET", "/api/sync")
        if response.status_code >= 500:
            return RemoteStatus.UPSTREAM_DOWN
        if response.status_code == 429:
            raise RemoteUnavailableError("The server is rate limiting. Try again later.")
        # 404 from a server that has no such route, 405 from one old enough to
        # have had only the POST. Either way it cannot sync with this client.
        if response.status_code in (404, 405):
            return RemoteStatus.NO_SYNC_ENDPOINT
        if response.status_code >= 400:
            raise RemoteProtocolError(
                f"Unexpected answer from the sync endpoint: {response.status_code}."
            )
        return RemoteStatus.READY
