from __future__ import annotations

import importlib.util
from pathlib import Path


def load_example_module():
    module_path = Path(__file__).parents[2] / "examples" / "create_agent_cli" / "main.py"
    spec = importlib.util.spec_from_file_location("create_agent_cli_main", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parse_args_builds_minimal_agent_input():
    module = load_example_module()
    args = module.parse_args(
        [
            "--name",
            "Demo Agent",
            "--identity",
            "You are a concise support agent.",
        ]
    )

    assert module.build_agent_input(args) == {
        "name": "Demo Agent",
        "identity": "You are a concise support agent.",
    }


def test_build_agent_input_includes_optional_scope_and_metadata():
    module = load_example_module()
    args = module.parse_args(
        [
            "--name",
            "Demo Agent",
            "--identity",
            "You help with onboarding.",
            "--model",
            "openai/gpt-4.1-mini",
            "--org",
            "org_123",
            "--team",
            "team_123",
            "--lookup-key",
            "demo-agent",
            "--metadata-json",
            '{"source":"python-sdk-example"}',
        ]
    )

    assert module.build_agent_input(args) == {
        "name": "Demo Agent",
        "identity": "You help with onboarding.",
        "model": "openai/gpt-4.1-mini",
        "org": "org_123",
        "team": "team_123",
        "lookup_key": "demo-agent",
        "metadata": {"source": "python-sdk-example"},
    }


def test_create_agent_uses_env_token_base_url_and_built_payload(monkeypatch):
    module = load_example_module()
    created_payloads = []
    clients = []

    class FakeAgents:
        def create(self, payload):
            created_payloads.append(payload)
            return {"id": "agt_123", "name": payload["name"]}

    class FakePlatformClient:
        def __init__(self, *, api_key, access_token, base_url):
            self.api_key = api_key
            self.base_url = base_url
            self.access_token = access_token
            self.agents = FakeAgents()
            self.closed = False
            clients.append(self)

        @classmethod
        def with_token(cls, api_key, access_token, *, base_url=None):
            return cls(api_key=api_key, access_token=access_token, base_url=base_url)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self.closed = True

    monkeypatch.setenv("ARCHASTRO_ACCESS_TOKEN", "sat_test")
    monkeypatch.setenv("ARCHASTRO_API_KEY", "pk_test")
    monkeypatch.setattr(module, "PlatformClient", FakePlatformClient)
    args = module.parse_args(
        [
            "--name",
            "Demo Agent",
            "--identity",
            "You help users.",
            "--base-url",
            "http://localhost:4000",
        ]
    )

    result = module.create_agent(args)

    assert result == {"id": "agt_123", "name": "Demo Agent"}
    assert clients[0].api_key == "pk_test"
    assert clients[0].base_url == "http://localhost:4000"
    assert clients[0].access_token == "sat_test"
    assert clients[0].closed is True
    assert created_payloads == [{"name": "Demo Agent", "identity": "You help users."}]
