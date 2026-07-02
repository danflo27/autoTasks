"""Entry point for the single "tellor_alert" script trigger.

The Monitor pipes {"monitor_match": {...}, "args": [...]} on stdin and checks
only the exit code (custom-notification scripts are fire-and-forget). Dispatch
is by monitor name -> handlers.HANDLERS; the handler formats and delivers the
alert itself (Discord + logs/alerts.log).

NOTE: the Monitor executes this file's *content* via `python3 -c` with cwd
/app, so shared modules are imported from the config mount via sys.path.
"""

import json
import sys

sys.path.insert(0, "config/triggers/scripts")

from handlers import HANDLERS  # noqa: E402
from tellor_lib import Match  # noqa: E402


def main():
    match = Match(json.load(sys.stdin))
    handler = HANDLERS.get(match.monitor_name)
    if handler is None:
        raise KeyError(
            f"no handler for monitor {match.monitor_name!r}; known: {sorted(HANDLERS)}"
        )
    handler(match)


try:
    main()
except Exception as e:
    print(f"alert script failed: {e!r}", file=sys.stderr)
    sys.exit(1)
