# Integration Scenarios

These examples show common SDK workflows. They were smoke-tested against the
local platform dev harness with org-user tokens.

## Read The Current User

Use `users.me()` to validate a token and discover the current actor.

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

## List Teams

List responses are Pydantic models. Access known fields as attributes, or call
`model_dump()` when you need a plain dictionary.

```python
teams = client.teams.list()

print(teams.model_dump(mode="json"))
```

## Create An Agent

Use `agents.create()` to provision an agent owned by the current user, org, or
team context. Store the returned `id` if you need to fetch or update it later.

```python
agent = client.agents.create(
    {
        "name": "Support triage",
        "identity": "You triage support requests and keep replies concise.",
    }
)

print(agent.id, agent.name)
```

For cleanup in tests and scripts:

```python
client.agents.delete(agent.id)
```

