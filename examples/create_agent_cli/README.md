# Create Agent CLI

This example shows how to wrap the Python SDK in a small CLI for creating an
agent.

The SDK call is intentionally direct:

```python
with PlatformClient.with_token(api_key, token, base_url=base_url) as client:
    agent = client.agents.create({...})
```

## Run

```bash
export ARCHASTRO_API_KEY=pk_...
export ARCHASTRO_ACCESS_TOKEN=sat_...

uv run python examples/create_agent_cli/main.py \
  --name "Demo Agent" \
  --identity "You are a concise assistant for onboarding users."
```

To create the agent under a specific org or team:

```bash
uv run python examples/create_agent_cli/main.py \
  --name "Team Demo Agent" \
  --identity "You help the team answer support questions." \
  --org org_... \
  --team team_...
```

For local development or another environment:

```bash
export ARCHASTRO_PLATFORM_BASE_URL=http://localhost:4000
uv run python examples/create_agent_cli/main.py \
  --name "Local Demo Agent" \
  --identity "You are running from the Python SDK example."
```
