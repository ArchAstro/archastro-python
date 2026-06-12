# ArchAstro Python SDK

Python SDK for the ArchAstro Platform API and ArchAgents runtime APIs.

```bash
uv add archastro-sdk
# or
pip install archastro-sdk
```

The clients default to the production API gateway, `https://platform.archastro.ai`.
Set `ARCHASTRO_PLATFORM_BASE_URL` only when targeting local development,
staging, or another non-production environment.

## Getting Started

Choose the auth path that matches how your Python process should run.

### ArchAgents Org Bot or Worker

Use this path for ArchAgents bots, background workers, cron jobs, ingestion
jobs, and integrations that should act as an org-owned system user. Your Python
process only needs a system-user access token:

```bash
export ARCHASTRO_ACCESS_TOKEN=sat_...
```

Create that token with `archagent` while logged in as an org admin. Replace
`user@company.com` with your ArchAgents login email. The setup is grouped as
one shell block so GitHub's copy button copies the full sequence:

```bash
archagent auth login user@company.com

export ARCHASTRO_ORG_ID="$(
  archagent describe me --json |
  jq -er '.session.org'
)"

export ARCHASTRO_SYSTEM_USER_ID="$(
  archagent --json create user \
    --system-user \
    --name "Python SDK Bot" \
    --org "$ARCHASTRO_ORG_ID" \
    --org-role member |
  jq -r '.id'
)"

export ARCHASTRO_ACCESS_TOKEN="$(
  archagent --json create usertoken \
    --user "$ARCHASTRO_SYSTEM_USER_ID" \
    --name "python-sdk-service" |
  jq -r '.token'
)"
```

Use the sync client for scripts and CLIs:

```python
import os

from archastro.platform import PlatformClient

with PlatformClient(access_token=os.environ["ARCHASTRO_ACCESS_TOKEN"]) as client:
    user = client.users.me()

print(user["id"], user.get("is_system_user"))
```

Use the async client inside async services or workers:

```python
import asyncio
import os

from archastro.platform import AsyncPlatformClient


async def main() -> None:
    async with AsyncPlatformClient(
        access_token=os.environ["ARCHASTRO_ACCESS_TOKEN"],
    ) as client:
        user = await client.users.me()

    print(user["id"], user.get("is_system_user"))


asyncio.run(main())
```

See [`examples/org_system_user_token`](examples/org_system_user_token) for the
complete system-user walkthrough.

### Developer App Auth

Use this path when you already have a publishable API key and a user access
token from a developer app login flow.

```bash
export ARCHASTRO_API_KEY=pk_...
export ARCHASTRO_ACCESS_TOKEN=sat_...
```

```python
import os

from archastro.platform import PlatformClient

client = PlatformClient.with_token(
    os.environ["ARCHASTRO_API_KEY"],
    os.environ["ARCHASTRO_ACCESS_TOKEN"],
)

with client:
    teams = client.teams.list()
```

Async setup uses the same factory:

```python
import asyncio
import os

from archastro.platform import AsyncPlatformClient


async def main() -> None:
    async with AsyncPlatformClient.with_token(
        os.environ["ARCHASTRO_API_KEY"],
        os.environ["ARCHASTRO_ACCESS_TOKEN"],
    ) as client:
        teams = await client.teams.list()
        print(teams)


asyncio.run(main())
```

## Examples

- [`examples/org_system_user_token`](examples/org_system_user_token) — run the
  SDK as an ArchAgents org-owned system user.
- [`examples/create_agent_cli`](examples/create_agent_cli) — wrap the sync SDK
  in a small CLI that creates an agent.
- [`examples/thread_chat_tui`](examples/thread_chat_tui) — chat in an existing
  thread from a terminal UI using the async websocket helpers.

## Packages

All public code lives under the single top-level `archastro` package:

- **`archastro.platform`** — typed REST + channel SDK generated from
  the canonical OpenAPI spec at
  [`ArchAstro/archastro-openapi`](https://github.com/ArchAstro/archastro-openapi).
  Pydantic models, async channel classes, auth helpers.
- **`archastro.phx_channel`** — the hand-written Phoenix Channels
  client the generated channel classes run on top of. WebSocket
  transport, join / reply / push / leave, heartbeat, reconnect, and a
  `HarnessServiceClient` for driving the
  [`@archastro/channel-harness`](https://www.npmjs.com/package/@archastro/channel-harness)
  service from Python tests.

## Development

This repo contains:

- Python SDK (`src/archastro/`) installed via `uv`
- JS tooling (`package.json`) — the channel-harness subprocess that
  powers the channel contract tests, plus the Prism mock server that
  backs the REST contract tests. Installed via `npm ci`.

### Setup

```bash
npm ci --ignore-scripts  # channel-harness + prism (for contract tests)
uv sync --locked --all-extras
```

### Running tests

```bash
# Unit tests only (no external services needed)
uv run pytest tests/test_http_client.py src/archastro/phx_channel/tests/test_unit.py

# Example smoke/unit tests
uv run pytest tests/examples

# REST contract tests (spawns Prism mock server)
uv run pytest tests/contract

# REST + channel contract tests (also spawns channel-harness subprocess)
ARCHASTRO_RUN_CHANNEL_CONTRACT_TESTS=1 uv run pytest tests/contract
```

### Regenerating the SDK

The typed SDK — `src/archastro/platform/` and `tests/contract/` — is
regenerated from the canonical OpenAPI spec by
[`@archastro/sdk-generator`](https://www.npmjs.com/package/@archastro/sdk-generator).
Don't hand-edit files with the `auto-generated by @archastro/sdk-generator`
header; they'll be overwritten.

```bash
./scripts/regenerate_sdk.sh
```

The script fetches the spec from `ArchAstro/archastro-openapi@main` and
runs the generator locked in `package-lock.json`. Knobs:

- `ARCHASTRO_OPENAPI_REF=some-branch ./scripts/regenerate_sdk.sh` — pull
  the spec from a non-default ref (useful when a spec change is on a
  branch awaiting merge).

After regenerating, review the diff, run the full test suite, and commit.

## Release

```bash
# bump version in pyproject.toml, then:
uv sync --locked --all-extras
uv build --no-build-isolation
uv publish
```
