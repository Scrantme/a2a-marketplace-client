r"""A2A marketplace session-start hook: one line of context about what the marketplace can do.

Opt-in. Add it as a SessionStart hook in Claude Code or Codex, and at the start of each session
the model's context gains one line such as:

  The A2A marketplace can take these task types: data_cleaning (3 sellers). Before doing a
  matching subtask yourself, offer it to the user first (skill a2a-marketplace).

so the agent offers the marketplace without being asked, instead of quietly doing the work.
Nothing from your prompts, code or files is ever sent: the hook reads only the public
task-type list (GET /task-types, the same as the list_task_types tool), at most once a day,
and caches it.

Requires Python 3.10+ (standard library only). Configure it with environment variables:
  A2A_API_KEY          your marketplace API key (buyer or seller). Without it, the hook prints
                       nothing. It is sent only over https (or http to this machine), and never
                       printed, logged or cached.
  A2A_MARKETPLACE_URL  default https://a2a-production.onrender.com
  A2A_CACHE_DIR        default ~/.cache/a2a-marketplace (holds task-types.json, counts only)
  A2A_HOOK_DEBUG       set to 1 to see on stderr why nothing was printed

Claude Code: the a2a-marketplace plugin (this bundle's README) installs it for you, through
hooks/hooks.json. To set it up by hand instead, save this file as
~/.claude/hooks/a2a_task_types.py, then add to
~/.claude/settings.json (on Windows, write `py -3` instead of `python3`):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup",
        "hooks": [
          {"type": "command", "command": "python3 ~/.claude/hooks/a2a_task_types.py", "timeout": 10}
        ]
      }
    ]
  }
}
```

Codex: save this file as ~/.codex/hooks/a2a_task_types.py, then add to ~/.codex/hooks.json, and
trust the hook once with /hooks (Codex runs only hooks you have reviewed):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.codex/hooks/a2a_task_types.py",
            "commandWindows": "py -3 %USERPROFILE%\\.codex\\hooks\\a2a_task_types.py",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

How one run decides (run):
  1. No key: print nothing.
  2. A cache younger than FRESH_FOR_S (a day): use it; no request is made.
  3. Otherwise fetch GET /task-types within FETCH_TIMEOUT_S, cache it, use it. The marketplace
     may be asleep (a free host takes about a minute to wake), so a slow answer is given up on
     rather than holding the session; the request still wakes it for next time.
  4. The fetch failed: use a cache younger than STALE_LIMIT_S (a week), marked with its date,
     or print nothing.
NOTE: so the very first session usually prints nothing when the marketplace is asleep (no cache
to fall back on yet); its request wakes it, and the next session gets the line (seen in Claude
Code on Windows, 2026-10-21).
WARNING: task-type names are free text chosen by sellers, and this line goes into the model's
context with no "this is data" framing, so only names matching SAFE_TASK_TYPE (the Agent Id
rule: a-z, 0-9, ".", "_", "-", at most 64 characters) are ever printed; anything else, such as
a sentence that tries to give the model instructions, is skipped (decided 2026-10-21).
The hook never gets in the way: every failure ends with exit 0 and nothing on stdout.

Part of the A2A marketplace client bundle (ROADMAP M7, design/harness-integration.md). Copied
alone, so it imports nothing from the marketplace's code; tests/test_task_type_hook.py checks
it against the app's own TaskTypeList, and tests/test_client_bundle.py runs the plugin's
command for it in a real shell.
"""

import ipaddress
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlsplit

DEFAULT_URL = "https://a2a-production.onrender.com"
CACHE_FILE_NAME = "task-types.json"
FRESH_FOR_S = 24 * 3600
"""A cache younger than this is used without asking the marketplace."""
STALE_LIMIT_S = 7 * 24 * 3600
"""When a refresh fails, a cache younger than this is still used (marked with its date)."""
FETCH_TIMEOUT_S = 3.0
"""How long the hook waits for the marketplace, per connection step, before giving up."""
MAX_LISTED = 10
"""The most task types the line names; the rest are counted ("and 3 more")."""
SAFE_TASK_TYPE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
"""The only task-type names ever printed (see WARNING in the module docstring)."""
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class HookError(Exception):
    """Why this run prints nothing new: an unusable answer, a refused URL, a failed request.
    Its message is for A2A_HOOK_DEBUG and never contains the key."""


class Config(NamedTuple):
    """One run's settings, from the environment (config_from_env)."""

    api_key: str
    base_url: str
    cache_file: Path
    debug: bool


class CachedList(NamedTuple):
    """The cached GET /task-types answer and when it was fetched (timezone-aware, UTC)."""

    fetched_at: datetime
    body: Any


def config_from_env(env: Mapping[str, str]) -> Config | None:
    """The settings in `env` (see the module docstring), or None when there is no key. The base
    URL loses any trailing slash. Pure."""
    key = env.get("A2A_API_KEY", "").strip()
    if not key:
        return None
    base_url = (env.get("A2A_MARKETPLACE_URL") or DEFAULT_URL).strip().rstrip("/")
    cache_dir = env.get("A2A_CACHE_DIR")
    if cache_dir:
        directory = Path(cache_dir)
    else:
        home = env.get("HOME") or env.get("USERPROFILE")
        directory = (Path(home) if home else Path.home()) / ".cache" / "a2a-marketplace"
    return Config(key, base_url, directory / CACHE_FILE_NAME, env.get("A2A_HOOK_DEBUG") == "1")


def key_may_be_sent_to(url: str) -> bool:
    """True if the API key may go to `url`: https, or http to this machine, and no user
    credentials in the URL. Pure."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    if host is None or parts.username is not None or parts.password is not None:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and _is_local(host)


def _is_local(host: str) -> bool:
    """True for a name or address of this machine."""
    if host in LOCAL_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _counts(body: Any) -> list[tuple[str, int]]:
    """The (task_type, sellers) pairs of a GET /task-types answer, in its order. Raises
    HookError if the answer doesn't have that shape (bool is not accepted as a count)."""
    if not isinstance(body, dict) or not isinstance(body.get("task_types"), list):
        raise HookError("the answer has no task_types list")
    counts = []
    for item in body["task_types"]:
        if not isinstance(item, dict):
            raise HookError("a task_types item is not an object")
        name, sellers = item.get("task_type"), item.get("sellers")
        if not isinstance(name, str) or not isinstance(sellers, int) or isinstance(sellers, bool):
            raise HookError("a task_types item has no task_type text or sellers number")
        counts.append((name, sellers))
    return counts


def context_line(body: Any, as_of: datetime | None = None) -> str | None:
    """The line to print for a GET /task-types answer, or None when nothing is listable.

    Lists each named task type with at least one seller whose name matches SAFE_TASK_TYPE, in
    the answer's order (the server puts most sellers first), at most MAX_LISTED, then "and N
    more". Sellers counted only in any_task_type_sellers are ignored: they accept anything, so
    they say nothing about a given subtask (the skill's rule). `as_of` marks a stale list with
    its date. Raises HookError for an answer of the wrong shape. Pure.
    """
    listable = [
        (name, sellers)
        for name, sellers in _counts(body)
        if sellers >= 1 and SAFE_TASK_TYPE.fullmatch(name)
    ]
    if not listable:
        return None
    shown = ", ".join(
        f"{name} ({sellers} seller{'' if sellers == 1 else 's'})"
        for name, sellers in listable[:MAX_LISTED]
    )
    more = len(listable) - MAX_LISTED
    if more > 0:
        shown += f", and {more} more"
    dated = f" (list as of {as_of.date().isoformat()})" if as_of else ""
    # NOTE: stated as a direct instruction, not "consider it": in M7's exit-criterion run an
    # agent with the skill installed still did a matching task itself and told the user only
    # afterwards (decision 21).
    return (
        f"The A2A marketplace can take these task types{dated}: {shown}. Before doing a "
        "matching subtask yourself, offer it to the user first (skill a2a-marketplace)."
    )


def read_cache(path: Path) -> CachedList | None:
    """The cache at `path`, or None if it is missing, unreadable, not JSON, or lacks a valid
    timezone-aware `fetched_at`. The body's own shape is checked later, by context_line."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(data["fetched_at"])
        body = data["body"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if fetched_at.tzinfo is None:
        return None
    return CachedList(fetched_at, body)


def write_cache(path: Path, body: Any, now: datetime) -> None:
    """Store `body` with `now` as its fetch time, atomically (a temporary file, then a rename),
    so a run that stops halfway never leaves a broken cache. Raises OSError if it can't."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"fetched_at": now.isoformat(), "body": body}), encoding="utf-8"
    )
    os.replace(temporary, path)


def fetch_task_types(config: Config) -> Any:
    """GET <base_url>/task-types with the key, within FETCH_TIMEOUT_S, as parsed JSON. Raises
    HookError for a URL the key may not go to (before anything is sent), any HTTP error status,
    a network failure or timeout, or a body that isn't JSON."""
    if not key_may_be_sent_to(config.base_url):
        raise HookError("A2A_MARKETPLACE_URL must be https (or http to this machine)")
    request = urllib.request.Request(
        f"{config.base_url}/task-types",
        headers={"Authorization": f"Bearer {config.api_key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_S) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        raise HookError(f"GET /task-types answered HTTP {error.code}") from None
    except (urllib.error.URLError, OSError) as error:
        raise HookError(f"GET /task-types failed: {type(error).__name__}") from None
    try:
        return json.loads(raw)
    except ValueError:
        raise HookError("GET /task-types didn't answer JSON") from None


def run(
    env: Mapping[str, str],
    now: datetime,
    out: Callable[[str], object],
    err: Callable[[str], object],
) -> int:
    """One hook run (see "How one run decides" in the module docstring): writes at most one
    line with `out`, and with A2A_HOOK_DEBUG=1 explains with `err` why nothing new was used.
    Always returns 0. Side effects: at most one HTTP request; writes the cache."""
    config = config_from_env(env)
    if config is None:
        return 0

    def debug(message: str) -> None:
        if config.debug:
            err(f"a2a hook: {message}")

    cached = read_cache(config.cache_file)
    if cached is not None and now - cached.fetched_at < timedelta(seconds=FRESH_FOR_S):
        _print_line(cached.body, None, out, debug)
        return 0
    try:
        body = fetch_task_types(config)
        line = context_line(body)
    except HookError as error:
        debug(str(error))
        if cached is not None and now - cached.fetched_at < timedelta(seconds=STALE_LIMIT_S):
            _print_line(cached.body, cached.fetched_at, out, debug)
        return 0
    try:
        write_cache(config.cache_file, body, now)
    except OSError as error:
        debug(f"couldn't write the cache: {type(error).__name__}")
    if line is not None:
        out(line)
    return 0


def _print_line(
    body: Any, as_of: datetime | None, out: Callable[[str], object], debug: Callable[[str], None]
) -> None:
    """Print the line for a cached body, or explain with `debug` why there is none."""
    try:
        line = context_line(body, as_of)
    except HookError as error:
        debug(f"the cache is unusable: {error}")
        return
    if line is not None:
        out(line)


def main() -> int:
    """Run the hook on this process's environment. Exit 0 whatever happens: a session-start
    hook that fails would only show the user an error about something they can't act on.
    NOTE: an unexpected exception (a bug) is reported on stderr with A2A_HOOK_DEBUG=1, and
    otherwise dropped on purpose, for the same reason."""
    try:
        return run(os.environ, datetime.now(UTC), print, lambda line: print(line, file=sys.stderr))
    except Exception as error:  # every exception, on purpose: see the docstring
        if os.environ.get("A2A_HOOK_DEBUG") == "1":
            print(f"a2a hook: unexpected {type(error).__name__}: {error}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
