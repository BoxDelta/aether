"""Put the dispatcher's machine-user token into its env file (ADR-041).

Reads the token from standard input only (never an argument, so it is not in the
process list or shell history), checks with GitHub that it belongs to the machine
user and has exactly the ``public_repo`` scope, then replaces
``DISPATCH_GITHUB_TOKEN`` in the dispatcher's env file (mode 600, atomic write).
Prints only the login, the scope check and the expiry date, never the token.

Owner, in fish:

    read -s -P 'boxdelta-dispatch token: ' tok
    and printf '%s' $tok | python3 -I ~/Projects/aether/scripts/dispatch_set_token.py
    set -e tok
"""

import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

VARIABLE = "DISPATCH_GITHUB_TOKEN"
DEFAULT_ENV_FILE = Path("~/.config/aether-dispatch/dispatch.env")
BOT_LOGIN = "boxdelta-dispatch"
API = "https://api.github.com/user"


def env_file() -> Path:
    raw = os.environ.get("DISPATCH_ENV_FILE")
    return (Path(raw) if raw else DEFAULT_ENV_FILE).expanduser()


def check(token: str) -> tuple[str, str, str]:
    """(login, scopes, expiry) of the token, from GitHub."""
    request = urllib.request.Request(
        API,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "aether-dispatch",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            login = str(json.loads(response.read()).get("login") or "")
            scopes = response.headers.get("x-oauth-scopes", "")
            expires = response.headers.get("github-authentication-token-expiration", "")
    except urllib.error.HTTPError as error:
        raise SystemExit(f"GitHub refused the token (HTTP {error.code})") from None
    except urllib.error.URLError:
        raise SystemExit("could not reach GitHub") from None
    return login, scopes, expires


def replace(text: str, token: str) -> str:
    lines = [line for line in text.splitlines() if not line.startswith(f"{VARIABLE}=")]
    return "\n".join([f"{VARIABLE}={token}", *lines]) + "\n"


def write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".dispatch-env-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main() -> int:
    if sys.stdin.isatty():
        raise SystemExit("pipe the token in on standard input (see the docstring)")
    token = sys.stdin.read().strip()
    if not token.startswith("ghp_") or any(c.isspace() for c in token):
        raise SystemExit("that is not a classic personal access token (ghp_...)")
    login, scopes, expires = check(token)
    if login != BOT_LOGIN:
        raise SystemExit(f"the token belongs to {login or 'nobody'}, not {BOT_LOGIN}")
    if {s.strip() for s in scopes.split(",")} != {"public_repo"}:
        raise SystemExit("the token's scopes must be exactly public_repo")
    path = env_file()
    write(path, replace(path.read_text("utf-8"), token))
    print(f"{VARIABLE} replaced in {path}: {login}, public_repo only, expires {expires or '?'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
