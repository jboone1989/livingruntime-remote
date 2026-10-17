from __future__ import annotations

import argparse
import getpass
import json
from typing import Sequence

import vault_client


def _csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Manage LivingRuntime Remote credentials locally without sending secrets through MCP."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    set_parser = sub.add_parser("set", help="Add or replace one local credential secret.")
    set_parser.add_argument("handle")
    set_parser.add_argument("--provider", required=True)
    set_parser.add_argument(
        "--capability",
        action="append",
        dest="capabilities",
        required=True,
        help="Allowed internal capability. Repeat for multiple capabilities.",
    )
    set_parser.add_argument("--projects", help="Optional comma-separated project allowlist.")
    set_parser.add_argument("--devices", help="Optional comma-separated device allowlist.")

    sub.add_parser("list", help="List credential handles and scopes; never prints secrets.")

    remove_parser = sub.add_parser("remove", help="Remove one local credential and revoke its leases.")
    remove_parser.add_argument("handle")

    args = parser.parse_args(argv)
    if args.command == "set":
        raise SystemExit(
            "Direct user-owned plaintext storage disabled. "
            "Use the secure_input_open ChatGPT App widget with the isolated vault."
        )

    if args.command == "list":
        print(json.dumps(vault_client.call("list_credentials"), ensure_ascii=False, indent=2))
        return

    if args.command == "remove":
        print(json.dumps(vault_client.call("delete_credential", handle=args.handle), ensure_ascii=False))
        return


if __name__ == "__main__":
    main()
