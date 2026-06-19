# Authentication

The SDK supports two common runtime shapes. Pick the one that matches who the
Python process is acting as.

## User Session In An App

Use this when your application already has a publishable API key and a user
access token from an app login flow.

```python
import os

from archastro.platform import PlatformClient

client = PlatformClient.with_token(
    os.environ["ARCHASTRO_API_KEY"],
    os.environ["ARCHASTRO_ACCESS_TOKEN"],
)

with client:
    me = client.users.me()

print(me.id, me.email)
```

## Org Bot Or Worker

Use this when a backend process should act as an org-owned system user. The
token should be an app-scoped user token created for that bot or worker.

```python
import os

from archastro.platform import PlatformClient

with PlatformClient(access_token=os.environ["ARCHASTRO_ACCESS_TOKEN"]) as client:
    me = client.users.me()

print(me.id)
```

## Async Services

Use `AsyncPlatformClient` inside async services and workers.

```python
import asyncio
import os

from archastro.platform import AsyncPlatformClient


async def main() -> None:
    async with AsyncPlatformClient.with_token(
        os.environ["ARCHASTRO_API_KEY"],
        os.environ["ARCHASTRO_ACCESS_TOKEN"],
    ) as client:
        me = await client.users.me()
        print(me.id, me.email)


asyncio.run(main())
```

## Local Or Staging Targets

The SDK defaults to `https://platform.archastro.ai`. Override `base_url` only
when targeting local development, staging, or another non-production gateway.

```python
client = PlatformClient.with_token(
    os.environ["ARCHASTRO_API_KEY"],
    os.environ["ARCHASTRO_ACCESS_TOKEN"],
    base_url=os.environ["ARCHASTRO_PLATFORM_BASE_URL"],
)
```

