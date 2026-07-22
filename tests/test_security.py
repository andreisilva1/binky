"""Security pass: worktree-name validation (shell/path safety) and the inline-secret
warning. Threat model is local single-user — see SECURITY.md."""

import pytest

from binky import ops
from binky.config import load_config_data, security_warnings
from binky.rpc import RpcFault


@pytest.mark.parametrize("name", ["feat", "feat/login-fix", "v1.2_x", "a-b", "release/2026"])
def test_valid_worktree_names(name: str) -> None:
    ops._validate_name(name)  # does not raise


@pytest.mark.parametrize(
    "name", ["feat; rm -rf ~", "a b", "$(whoami)", "../etc", "/abs", "a..b", "a$b", "-x", "a&b"]
)
def test_invalid_worktree_names(name: str) -> None:
    with pytest.raises(RpcFault):
        ops._validate_name(name)


def test_inline_admin_credential_is_warned() -> None:
    cfg = load_config_data(
        {
            "project": {"name": "acme"},
            "services": {"db": {"adapter": "postgres", "admin_url": "postgres://u:secret@h/postgres"}},
        }
    )
    assert any("admin_url" in w for w in security_warnings(cfg))


def test_env_admin_url_is_not_warned() -> None:
    cfg = load_config_data(
        {
            "project": {"name": "acme"},
            "services": {"db": {"adapter": "postgres", "admin_url": "${env.BINKY_DB_ADMIN_URL}"}},
        }
    )
    assert security_warnings(cfg) == []
