# Copyright (c) 2026 ArchAstro Inc. All Rights Reserved.

from __future__ import annotations

import os

from archastro.platform import PlatformClient

DEFAULT_PLATFORM_BASE_URL = "https://platform.archastro.ai"


def main() -> None:
    token = os.environ.get("ARCHASTRO_ACCESS_TOKEN")
    if not token:
        raise SystemExit(
            "Set ARCHASTRO_ACCESS_TOKEN to a system-user token created by archagent or archastro."
        )

    base_url = (
        _env("ARCHASTRO_PLATFORM_BASE_URL", "ARCHASTRO_BASE_URL") or DEFAULT_PLATFORM_BASE_URL
    )
    with PlatformClient(base_url=base_url, access_token=token) as client:
        user = client.users.me()
    print(f"Authenticated as {user['id']}")

    is_system_user = user.get("is_system_user")
    if is_system_user is not None:
        print(f"System user: {is_system_user}")
    if user.get("org_role"):
        print(f"Org role: {user['org_role']}")
    if user.get("sandbox_id"):
        print(f"Sandbox: {user['sandbox_id']}")


def _env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


if __name__ == "__main__":
    main()
