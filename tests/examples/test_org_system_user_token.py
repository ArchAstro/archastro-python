from __future__ import annotations

import importlib.util
from pathlib import Path


def load_example_module():
    module_path = Path(__file__).parents[2] / "examples" / "org_system_user_token" / "main.py"
    spec = importlib.util.spec_from_file_location("org_system_user_token_main", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_main_uses_access_token_env_and_prints_authenticated_user(monkeypatch, capsys):
    module = load_example_module()
    clients = []

    class FakeUsers:
        def me(self):
            return {
                "id": "usr_system",
                "is_system_user": True,
                "org_role": "member",
            }

    class FakePlatformClient:
        def __init__(self, *, base_url, access_token):
            self.base_url = base_url
            self.access_token = access_token
            self.users = FakeUsers()
            clients.append(self)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            pass

    monkeypatch.setenv("ARCHASTRO_ACCESS_TOKEN", "sat_test")
    monkeypatch.setenv("ARCHASTRO_PLATFORM_BASE_URL", "http://localhost:4000")
    monkeypatch.setattr(module, "PlatformClient", FakePlatformClient)

    module.main()

    assert clients[0].base_url == "http://localhost:4000"
    assert clients[0].access_token == "sat_test"
    assert capsys.readouterr().out.splitlines() == [
        "Authenticated as usr_system",
        "System user: True",
        "Org role: member",
    ]
