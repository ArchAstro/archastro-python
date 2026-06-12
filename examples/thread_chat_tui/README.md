# Thread Chat TUI

This example shows the websocket part of the Python SDK with a small
terminal chat UI around it.

The important SDK flow is visible in `main.py`:

1. Create an `AsyncPlatformClient` with the publishable API key and access token.
2. Open a websocket with `await client.open_socket()`.
3. Join a thread with `ApiChatChannel.join_user_thread(...)` or
   `ApiChatChannel.join_team_thread(...)`.
4. Wrap the generated channel in `ThreadChatSession`.
5. Pass the session into `ThreadChatTui`.
6. Keep websocket operations in `ThreadChatSession` and terminal rendering in
   `ThreadChatTui`.

## Run

```bash
export ARCHASTRO_API_KEY=pk_...
export ARCHASTRO_ACCESS_TOKEN=sat_...
uv run python examples/thread_chat_tui/main.py thr_...
```

For a team-scoped thread:

```bash
uv run python examples/thread_chat_tui/main.py thr_... --team team_...
```

For local development or another environment:

```bash
export ARCHASTRO_PLATFORM_BASE_URL=http://localhost:4000
uv run python examples/thread_chat_tui/main.py thr_...
```

Inside the UI, press Enter to send and Ctrl-D or Ctrl-C to exit.
