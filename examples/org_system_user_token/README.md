# Org System-User Token

Use this pattern when a Python process should act as an ArchAgents org-owned
service user: a bot, worker, cron job, ingestion process, or integration that
belongs to the org rather than to a human session.

The Python process only needs one secret:

```bash
export ARCHASTRO_ACCESS_TOKEN=sat_...
```

The backend derives the app and org context from that token, so this example
does not require `ARCHASTRO_API_KEY`.

The clients default to the production API gateway,
`https://platform.archastro.ai`.

For local development or another environment, override the base URL:

```bash
export ARCHASTRO_PLATFORM_BASE_URL=http://localhost:4000
```

## Use the Sync Client

Use `PlatformClient` for scripts, CLIs, cron jobs, and small workers that do not
already run an event loop.

```python
import os

from archastro.platform import PlatformClient

base_url = os.environ.get("ARCHASTRO_PLATFORM_BASE_URL", "https://platform.archastro.ai")
token = os.environ["ARCHASTRO_ACCESS_TOKEN"]

with PlatformClient(base_url=base_url, access_token=token) as client:
    user = client.users.me()
    orgs = client.users.orgs(user["id"])

print(user["id"], user.get("is_system_user"), orgs)
```

Run the complete sync example:

```bash
uv run python examples/org_system_user_token/main.py
```

## Use the Async Client

Use `AsyncPlatformClient` inside async web apps, async workers, or code that also
uses websocket channels.

```python
import asyncio
import os

from archastro.platform import AsyncPlatformClient


async def main() -> None:
    base_url = os.environ.get("ARCHASTRO_PLATFORM_BASE_URL", "https://platform.archastro.ai")
    token = os.environ["ARCHASTRO_ACCESS_TOKEN"]

    async with AsyncPlatformClient(base_url=base_url, access_token=token) as client:
        user = await client.users.me()
        orgs = await client.users.orgs(user["id"])

    print(user["id"], user.get("is_system_user"), orgs)


asyncio.run(main())
```

## If You Are an ArchAgents User

If you are already using ArchAgents and can log in as an org admin, create the
system user and token with `archagent`. Replace `user@company.com` with your
ArchAgents login email. GitHub adds a copy button to fenced code blocks, so this
setup is kept as one complete shell block.

```bash
archagent auth login user@company.com

export ARCHASTRO_ORG_ID="$(
  archagent describe me --json |
  jq -er '.session.org'
)"

export ARCHASTRO_SYSTEM_USER_ID="$(
  archagent --json create user \
    --system-user \
    --name "ArchAgents Python SDK Bot" \
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

The system user is org-scoped. `member` is the safest default role for most bots
and integrations. The raw token is shown once, so put
`ARCHASTRO_ACCESS_TOKEN` in your secret manager immediately.

Now run your Python service with `ARCHASTRO_ACCESS_TOKEN` in its environment.
Only set `ARCHASTRO_PLATFORM_BASE_URL` when targeting local development,
staging, or another non-production environment.

## If You Are a Platform Developer

Use `archastro` when you need to bootstrap the system user from a developer app
context instead of an ArchAgents org session. Run from an initialized ArchAstro
project, or pass `--app <app_id>` to each command. Replace `Example Org` with
the org name, slug, or domain you want to bootstrap.

```bash
archastro auth login

export ARCHASTRO_ORG_SEARCH="Example Org"
export ARCHASTRO_ORG_ID="$(
  archastro --json list orgs --search "$ARCHASTRO_ORG_SEARCH" |
  jq -er '.data[0].id'
)"

export ARCHASTRO_SYSTEM_USER_ID="$(
  archastro --json create user \
    --system-user \
    --name "ArchAgents Python SDK Bot" \
    --org "$ARCHASTRO_ORG_ID" \
    --org-role member |
  jq -r '.id'
)"

export ARCHASTRO_ACCESS_TOKEN="$(
  archastro --json create usertoken \
    --user "$ARCHASTRO_SYSTEM_USER_ID" \
    --name "python-sdk-service" |
  jq -r '.token'
)"
```

## Rotate or Revoke the Token

List active tokens:

```bash
archagent list usertokens --user "$ARCHASTRO_SYSTEM_USER_ID"
```

Revoke one token:

```bash
archagent revoke usertoken sat_... --user "$ARCHASTRO_SYSTEM_USER_ID"
```
