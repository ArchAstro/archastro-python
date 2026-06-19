# ArchAstro Python SDK

Use the Python SDK to call the ArchAstro Platform API from scripts, CLIs,
workers, and async services.

## Start Here

1. Install the package.
2. Choose the authentication mode for your process.
3. Verify the token with `users.me()`.
4. Move to the resource-specific API reference when you know the method you
   need.

```bash
uv add archastro-sdk
# or
pip install archastro-sdk
```

## Documentation Map

- [Authentication](authentication.html): choose between app user sessions,
  org-worker tokens, and local/staging base URLs.
- [Integration scenarios](scenarios.html): smoke-tested snippets for reading the
  current user, listing teams, and creating an agent.
- [Platform API reference](archastro/platform.html): generated reference for the
  REST client, models, and channel wrappers.
- [Phoenix channel reference](archastro/phx_channel.html): lower-level realtime
  channel client.

## Minimal Example

```python
import os

from archastro.platform import PlatformClient

with PlatformClient.with_token(
    os.environ["ARCHASTRO_API_KEY"],
    os.environ["ARCHASTRO_ACCESS_TOKEN"],
) as client:
    me = client.users.me()

print(me.id, me.email)
```

