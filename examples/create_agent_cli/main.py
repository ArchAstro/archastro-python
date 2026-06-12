# Copyright (c) 2026 ArchAstro Inc. All Rights Reserved.

from __future__ import annotations

import argparse
import json
import os
from typing import Any

from pydantic import BaseModel

from archastro.platform import PlatformClient

DEFAULT_PLATFORM_BASE_URL = "https://platform.archastro.ai"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a basic ArchAstro agent using the Python SDK."
    )
    parser.add_argument("--name", required=True, help="Agent display name.")
    parser.add_argument("--identity", required=True, help="Identity prompt for the agent.")
    parser.add_argument("--model", help="Default model identifier for the agent.")
    parser.add_argument("--org", help="Org id that should own the agent.")
    parser.add_argument("--team", help="Team id that should own the agent.")
    parser.add_argument("--user", help="User id that should own the agent.")
    parser.add_argument("--lookup-key", help="Stable lookup key for idempotent external scripts.")
    parser.add_argument("--template", help="Existing template id or lookup key to provision from.")
    parser.add_argument("--originator", help="Free-form source label for the created agent.")
    parser.add_argument(
        "--metadata-json",
        type=_json_object,
        help='Optional metadata object, for example \'{"source":"python-sdk-example"}\'.',
    )
    parser.add_argument(
        "--base-url",
        default=_env("ARCHASTRO_PLATFORM_BASE_URL", "ARCHASTRO_BASE_URL")
        or DEFAULT_PLATFORM_BASE_URL,
        help=(
            "Platform base URL. Defaults to ARCHASTRO_PLATFORM_BASE_URL, "
            "ARCHASTRO_BASE_URL, or production."
        ),
    )
    return parser.parse_args(argv)


def build_agent_input(args: argparse.Namespace) -> dict[str, object]:
    fields = {
        "name": args.name,
        "identity": args.identity,
        "model": args.model,
        "org": args.org,
        "team": args.team,
        "user": args.user,
        "lookup_key": args.lookup_key,
        "template": args.template,
        "originator": args.originator,
        "metadata": args.metadata_json,
    }
    return {key: value for key, value in fields.items() if value is not None}


def create_agent(args: argparse.Namespace) -> dict[str, Any]:
    api_key = _required_env("ARCHASTRO_API_KEY")
    token = _required_env("ARCHASTRO_ACCESS_TOKEN")

    with PlatformClient.with_token(api_key, token, base_url=args.base_url) as client:
        agent = client.agents.create(build_agent_input(args))
    return _plain(agent)


def _json_object(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("--metadata-json must be a JSON object")
    return parsed


def _env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _required_env(name: str) -> str:
    value = _env(name)
    if not value:
        raise SystemExit(f"Set {name} before running this example.")
    return value


def _plain(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    return {"result": value}


def main() -> None:
    agent = create_agent(parse_args())
    print(json.dumps(agent, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
