"""P3-01 挂载、耐久恢复、cursor 与签名值契约。"""

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI

from src.app.api.v1.auth_store import AuthStore
from src.app.api.v1.mount import mount_api_v1
from src.app.api.v1.policy import USER_FRONTEND_AUDIENCE
from src.app.api.v1.tokens import SignedValueCodec, SignedValueError
from src.core.config.core_config import CoreConfig

ENVIRONMENT = {
    "ELYSIUM_APP_API_V1_SIGNING_SECRET": "s" * 48,
    "ELYSIUM_INSTALLATION_ID": "installation-test",
}


def test_app_api_is_disabled_by_default_and_limits_are_bounded() -> None:
    config = CoreConfig.HttpRouterSection()
    assert not config.enable_app_api_v1
    assert config.app_api_v1_database_path.startswith("runtime/")
    assert config.app_api_v1_max_concurrency == 32
    assert config.app_api_v1_max_websocket_connections == 64
    with pytest.raises(ValueError):
        CoreConfig.HttpRouterSection(app_api_v1_max_concurrency=0)
    with pytest.raises(ValueError):
        CoreConfig.HttpRouterSection(app_api_v1_max_websocket_connections=0)


def test_mount_requires_stable_secret_installation_and_exact_origin(
    tmp_path: Path,
) -> None:
    app = FastAPI()
    with pytest.raises(RuntimeError, match="SIGNING_SECRET"):
        mount_api_v1(
            app,
            workspace_root=tmp_path,
            database_path="runtime/api/auth.sqlite3",
            allowed_origins=("http://localhost:5173",),
            max_concurrency=4,
            environ={},
        )
    with pytest.raises(RuntimeError, match="invalid exact Origin"):
        mount_api_v1(
            app,
            workspace_root=tmp_path,
            database_path="runtime/api/auth.sqlite3",
            allowed_origins=("http://*",),
            max_concurrency=4,
            environ=ENVIRONMENT,
        )
    with pytest.raises(RuntimeError, match="runtime"):
        mount_api_v1(
            app,
            workspace_root=tmp_path,
            database_path="outside.sqlite3",
            allowed_origins=("http://localhost:5173",),
            max_concurrency=4,
            environ=ENVIRONMENT,
        )


def test_mount_creates_owned_store_and_api_route(tmp_path: Path) -> None:
    app = FastAPI()
    mounted = mount_api_v1(
        app,
        workspace_root=tmp_path,
        database_path="runtime/api/auth.sqlite3",
        allowed_origins=("http://localhost:5173",),
        max_concurrency=4,
        environ=ENVIRONMENT,
    )
    assert any(getattr(route, "path", None) == "/api/v1" for route in app.routes)
    assert (tmp_path / "runtime" / "api" / "auth.sqlite3").exists()
    mounted.close()
    mounted.close()
    assert not any(getattr(route, "path", None) == "/api/v1" for route in app.routes)

    remounted = mount_api_v1(
        app,
        workspace_root=tmp_path,
        database_path="runtime/api/auth.sqlite3",
        allowed_origins=("http://localhost:5173",),
        max_concurrency=4,
        environ=ENVIRONMENT,
    )
    remounted.close()


def test_session_revocation_survives_store_reopen(tmp_path: Path) -> None:
    database = tmp_path / "auth.sqlite3"
    codec = SignedValueCodec("x" * 48)
    first = AuthStore(database, installation_id="installation-test")
    challenge = first.create_bootstrap_challenge(
        codec=codec,
        audience=USER_FRONTEND_AUDIENCE,
        origin="http://localhost:5173",
        scopes=("auth:session",),
    )
    session, access, _ = first.issue_session_from_bootstrap(
        challenge=challenge,
        audience=USER_FRONTEND_AUDIENCE,
        origin="http://localhost:5173",
        codec=codec,
        access_ttl=timedelta(minutes=5),
        refresh_ttl=timedelta(hours=1),
    )
    first.revoke_session(session.session_id)
    first.close()

    reopened = AuthStore(database, installation_id="installation-test")
    try:
        with pytest.raises(ValueError, match="session_revoked"):
            reopened.authenticate_access(access_token=access, codec=codec)
    finally:
        reopened.close()


def test_cursor_is_opaque_ledger_bound_and_tamper_evident() -> None:
    codec = SignedValueCodec("c" * 48)
    cursor = codec.encode_cursor(42, ledger="life-events")
    assert codec.decode_cursor(cursor, ledger="life-events") == 42
    with pytest.raises(SignedValueError, match="cursor_invalid"):
        codec.decode_cursor(cursor, ledger="other-ledger")
    altered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
    with pytest.raises(SignedValueError, match="signature_invalid"):
        codec.decode_cursor(altered, ledger="life-events")
