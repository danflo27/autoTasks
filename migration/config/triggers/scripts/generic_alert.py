"""Generic quickstart alert renderer for a generated function monitor."""

import json
import re
import sys

sys.path.insert(0, "config/triggers/scripts")

from tellor_lib import Match, function_match_succeeded, now_utc, send_alert  # noqa: E402


PLACEHOLDER_RE = re.compile(r"\$\{([^{}]+)\}")
DYNAMIC_MARKDOWN_RE = re.compile(r"([\\*_{}\[\]()#+\-.!|>~<])")


def escape_dynamic(value):
    value = DYNAMIC_MARKDOWN_RE.sub(r"\\\1", str(value).replace("`", "ˋ"))
    return value.replace("\r", r"\r").replace("\n", r"\n")


def message_variables(match):
    transaction = match.transaction
    args = match.arg_map()
    signature = match.signature
    variables = {
        "monitor.name": match.monitor_name,
        "network.slug": match.network,
        "transaction.hash": match.tx_hash,
        "transaction.from": transaction.get("from", "n/a"),
        "transaction.to": transaction.get("to", "n/a"),
        "transaction.value": transaction.get("value", "0"),
        "observed.utc": now_utc(),
        "function.signature": signature,
        "functions.0.signature": signature,
    }
    for name, value in args.items():
        safe_value = escape_dynamic(value)
        variables["function.args." + name] = safe_value
        variables["functions.0.args." + name] = safe_value
    summary = "`{}`".format(signature)
    if args:
        summary += "\n" + "\n".join(
            "- `{}`: `{}`".format(name, escape_dynamic(value)) for name, value in args.items()
        )
    variables["functions"] = summary
    return variables


def render_message(template, match):
    variables = message_variables(match)
    return PLACEHOLDER_RE.sub(lambda found: str(variables.get(found.group(1), "n/a")), template)


def main():
    payload = json.load(sys.stdin)
    arguments = payload.get("args") or []
    if not arguments:
        raise ValueError("generic alert trigger is missing its message template")
    template = arguments[0]
    webhook_env = arguments[1] if len(arguments) > 1 else "CUSTOM_DISCORD_WEBHOOK_URL"
    match = Match(payload)
    if not function_match_succeeded(match):
        print("skipping failed function call {}".format(match.tx_hash), file=sys.stderr)
        return
    send_alert(match, render_message(template, match), webhook_env=webhook_env)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("generic alert failed: {}".format(error), file=sys.stderr)
        sys.exit(1)
