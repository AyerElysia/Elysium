from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from scripts import deployment, deployment_schema_check


def _model_template(api_key: str = "${ELYSIUM_TEST_MODEL_KEY}") -> str:
    task_names = sorted(deployment.REQUIRED_MODEL_TASKS)
    tasks = "\n".join(f'[tasks.{name}]\nmodels = ["test-model"]' for name in task_names)
    return (
        "[providers.Test]\n"
        'base_url = "http://127.0.0.1:3000/v1"\n'
        f'api_key = "{api_key}"\n'
        'client_type = "openai"\n\n'
        '[models."test-model"]\n'
        'provider = "Test"\n'
        'id = "test-model"\n\n'
        f"{tasks}\n"
    )


def _core_template() -> str:
    return (
        "[storage]\n"
        'backend = "local"\n\n'
        "[database]\n"
        'sqlite_path = "data/Elysium.db"\n\n'
        "[http_router]\n"
        "enable_http_router = false\n"
        'http_router_host = "127.0.0.1"\n'
        "http_router_port = 8000\n"
        "enable_app_api_v1 = false\n"
    )


def _repository(path: Path) -> Path:
    (path / "config").mkdir(parents=True)
    for name in ("AGENTS.md", "main.py", "pyproject.toml", "uv.lock"):
        (path / name).write_text("# fixture\n", encoding="utf-8")
    (path / "config/core.toml.example").write_text(_core_template(), encoding="utf-8")
    (path / "config/models.toml.example").write_text(
        _model_template(), encoding="utf-8"
    )
    return path


def _write_subject_authority(root: Path) -> dict[str, bytes]:
    workspace = root / "data/life_engine_workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    contents = {
        "SOUL.md": "主体内容\n".encode(),
        "USER.md": b"",
        "MEMORY.md": "记忆内容\n".encode(),
    }
    for name, value in contents.items():
        path = workspace / name
        path.write_bytes(value)
        path.chmod(0o600)
    return contents


def _secure_generated_configuration(root: Path) -> None:
    if os.name == "nt":
        return
    for path in (root / "config").rglob("*.toml"):
        path.chmod(0o600)


def test_initialize_configuration_is_private_create_only_and_idempotent(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path / "repo with spaces")

    created, preserved = deployment.bootstrap(
        root,
        with_dev=False,
        config_only=True,
    )

    assert "config/core.toml" in created
    assert "config/models.toml" in created
    assert not preserved
    assert not (root / "data/life_engine_workspace/SOUL.md").exists()
    if os.name != "nt":
        for relative in created:
            mode = stat.S_IMODE((root / relative).stat().st_mode)
            assert mode == 0o600

    core_path = root / "config/core.toml"
    custom = b"operator-owned bytes\n"
    core_path.write_bytes(custom)
    second_created, second_preserved = deployment.bootstrap(
        root,
        with_dev=False,
        config_only=True,
    )

    assert not second_created
    assert set(second_preserved) == set(created)
    assert core_path.read_bytes() == custom


def test_shell_entrypoint_works_from_unrelated_cwd_and_unicode_path(
    tmp_path: Path,
) -> None:
    project_root = deployment.repository_root()
    root = _repository(tmp_path / "仓 库")
    (root / "scripts").mkdir()
    shutil.copy2(project_root / "deploy.sh", root / "deploy.sh")
    shutil.copy2(project_root / "scripts/deployment.py", root / "scripts/deployment.py")

    result = subprocess.run(
        [str(root / "deploy.sh"), "bootstrap", "--config-only"],
        cwd=tmp_path,
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (root / "config/core.toml").is_file()
    assert (root / "config/models.toml").is_file()
    assert not (root / "data/life_engine_workspace/SOUL.md").exists()


def test_initialize_configuration_rejects_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path / "repo")
    external = tmp_path / "external.toml"
    external.write_bytes(b"outside\n")
    (root / "config/core.toml").symlink_to(external)

    with pytest.raises(deployment.DeploymentError, match="不是普通文件"):
        deployment.initialize_configuration(root)

    assert external.read_bytes() == b"outside\n"
    assert not (root / "config/models.toml").exists()


def test_concurrent_configuration_creation_leaves_complete_files(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path / "repo")

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(lambda _: deployment.initialize_configuration(root), range(4))
        )

    expected = deployment._configuration_sources(root)
    for relative, content in expected.items():
        assert (root / relative).read_text(encoding="utf-8") == content
    claimed_created = [relative for created, _ in results for relative in created]
    assert sorted(claimed_created) == sorted(expected)


def test_initialize_configuration_rejects_symlinked_parent(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path / "repo")
    external = tmp_path / "external-config"
    external.mkdir()
    (root / "config/plugins").symlink_to(external, target_is_directory=True)

    with pytest.raises(deployment.DeploymentError, match="路径不安全"):
        deployment.initialize_configuration(root)

    assert not any(external.iterdir())


def test_doctor_missing_subject_authority_is_read_only(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    deployment.initialize_configuration(root)
    before = sorted(path.relative_to(root) for path in root.rglob("*"))

    report = deployment.doctor_repository(
        root,
        environment={"ELYSIUM_TEST_MODEL_KEY": "sentinel-secret"},
        check_dependencies=False,
        check_processes=False,
    )

    after = sorted(path.relative_to(root) for path in root.rglob("*"))
    assert not report.ready
    assert before == after
    assert {
        check.name
        for check in report.checks
        if check.status == "failed" and check.name.startswith("subject.")
    } == {"subject.SOUL.md", "subject.USER.md", "subject.MEMORY.md"}


def test_doctor_rejects_subject_authority_behind_ancestor_symlink(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path / "repo")
    deployment.initialize_configuration(root)
    external_data = tmp_path / "external-data"
    external_workspace = external_data / "life_engine_workspace"
    external_workspace.mkdir(parents=True)
    expected = {
        "SOUL.md": b"external subject\n",
        "USER.md": b"",
        "MEMORY.md": b"external memory\n",
    }
    for name, value in expected.items():
        (external_workspace / name).write_bytes(value)
    (root / "data/runtime").rmdir()
    (root / "data").rmdir()
    (root / "data").symlink_to(external_data, target_is_directory=True)

    report = deployment.doctor_repository(
        root,
        environment={"ELYSIUM_TEST_MODEL_KEY": "configured"},
        check_dependencies=False,
        check_processes=False,
    )

    assert not report.ready
    assert {
        check.name
        for check in report.checks
        if check.status == "failed" and check.name.startswith("subject.")
    } == {"subject.SOUL.md", "subject.USER.md", "subject.MEMORY.md"}
    for name, value in expected.items():
        assert (external_data / "life_engine_workspace" / name).read_bytes() == value


def test_doctor_validates_subject_bytes_without_rewriting_or_leaking_secrets(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _repository(tmp_path / "repo")
    deployment.initialize_configuration(root)
    expected = _write_subject_authority(root)
    secret = "do-not-print-this-secret"

    report = deployment.doctor_repository(
        root,
        environment={"ELYSIUM_TEST_MODEL_KEY": secret},
        check_dependencies=False,
        check_processes=False,
    )
    deployment._print_report(report, as_json=True)
    output = capsys.readouterr().out

    assert report.ready
    assert secret not in output
    assert json.loads(output)["state"] == "manual_start_required"
    for name, value in expected.items():
        assert (root / "data/life_engine_workspace" / name).read_bytes() == value


@pytest.mark.parametrize(
    "name,value",
    [
        ("SOUL.md", b""),
        ("USER.md", b"\xff"),
        ("MEMORY.md", b"\x80"),
    ],
)
def test_doctor_fails_closed_on_invalid_subject_files(
    tmp_path: Path,
    name: str,
    value: bytes,
) -> None:
    root = _repository(tmp_path / "repo")
    deployment.initialize_configuration(root)
    expected = _write_subject_authority(root)
    expected[name] = value
    (root / "data/life_engine_workspace" / name).write_bytes(value)

    report = deployment.doctor_repository(
        root,
        environment={"ELYSIUM_TEST_MODEL_KEY": "configured"},
        check_dependencies=False,
        check_processes=False,
    )

    assert not report.ready
    assert any(
        check.name == f"subject.{name}" and check.status == "failed"
        for check in report.checks
    )
    for subject_name, subject_value in expected.items():
        assert (
            root / "data/life_engine_workspace" / subject_name
        ).read_bytes() == subject_value


def test_doctor_rejects_model_placeholder_without_echoing_it(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    (root / "config/models.toml.example").write_text(
        _model_template("replace-with-private-token"), encoding="utf-8"
    )
    deployment.initialize_configuration(root)
    _write_subject_authority(root)

    report = deployment.doctor_repository(
        root,
        environment={},
        check_dependencies=False,
        check_processes=False,
    )
    rendered = json.dumps(report.as_dict(), ensure_ascii=False)

    assert not report.ready
    assert "replace-with-private-token" not in rendered
    assert any(
        check.name == "models.secrets" and check.status == "failed"
        for check in report.checks
    )


def test_doctor_rejects_plaintext_model_secret_without_echoing_it(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path / "repo")
    plaintext = "a-real-looking-secret-value"
    (root / "config/models.toml.example").write_text(
        _model_template(plaintext), encoding="utf-8"
    )
    deployment.initialize_configuration(root)
    _secure_generated_configuration(root)
    _write_subject_authority(root)

    report = deployment.doctor_repository(
        root,
        environment={},
        check_dependencies=False,
        check_processes=False,
    )
    rendered = json.dumps(report.as_dict(), ensure_ascii=False)

    assert not report.ready
    assert plaintext not in rendered
    assert "${ENV_VAR}" in rendered


def test_doctor_rejects_insecure_plugin_config_permission(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX permission contract")
    root = _repository(tmp_path / "repo")
    deployment.initialize_configuration(root)
    _secure_generated_configuration(root)
    _write_subject_authority(root)
    target = root / "config/plugins/feishu_adapter/config.toml"
    target.chmod(0o644)

    report = deployment.doctor_repository(
        root,
        environment={"ELYSIUM_TEST_MODEL_KEY": "configured"},
        check_dependencies=False,
        check_processes=False,
    )

    assert not report.ready
    assert any(
        check.name == "config.permissions" and check.status == "failed"
        for check in report.checks
    )


def test_doctor_rejects_nested_config_directory_symlink(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    deployment.initialize_configuration(root)
    _secure_generated_configuration(root)
    _write_subject_authority(root)
    external = tmp_path / "external-config"
    external.mkdir()
    (external / "secret.toml").write_text("token = 'outside'\n", encoding="utf-8")
    (root / "config/external").symlink_to(external, target_is_directory=True)

    report = deployment.doctor_repository(
        root,
        environment={"ELYSIUM_TEST_MODEL_KEY": "configured"},
        check_dependencies=False,
        check_processes=False,
    )

    assert not report.ready
    assert any(
        check.name == "config.boundary" and check.status == "failed"
        for check in report.checks
    )


def test_doctor_rejects_invalid_mysql_pool_lifetime(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    deployment.initialize_configuration(root)
    (root / "config/core.toml").write_text(
        "[storage]\n"
        'backend = "mysql"\n'
        'backend_generation = "verified-generation"\n\n'
        "[database]\n"
        'mysql_password = "${ELYSIUM_TEST_MYSQL_PASSWORD}"\n'
        "mysql_pool_recycle_seconds = 1800\n"
        "mysql_idle_session_timeout_seconds = 180\n\n"
        "[http_router]\n"
        "enable_http_router = false\n"
        "enable_app_api_v1 = false\n",
        encoding="utf-8",
    )
    _secure_generated_configuration(root)

    report = deployment.doctor_repository(
        root,
        environment={
            "ELYSIUM_TEST_MODEL_KEY": "configured",
            "ELYSIUM_TEST_MYSQL_PASSWORD": "configured",
        },
        check_dependencies=False,
        check_processes=False,
    )

    assert not report.ready
    assert any(
        check.name == "core.mysql_pool_lifetime" and check.status == "failed"
        for check in report.checks
    )


def test_process_detection_capability_failure_is_not_reported_as_no_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repository(tmp_path / "repo")
    deployment.initialize_configuration(root)
    _secure_generated_configuration(root)
    _write_subject_authority(root)
    monkeypatch.setattr(deployment, "_processes_with_psutil", lambda root: None)
    monkeypatch.setattr(deployment, "_processes_from_proc", lambda root: None)

    report = deployment.doctor_repository(
        root,
        environment={"ELYSIUM_TEST_MODEL_KEY": "configured"},
        check_dependencies=False,
        check_processes=True,
    )

    assert not report.ready
    assert any(
        check.name == "runtime.instance" and check.status == "failed"
        for check in report.checks
    )


def test_windows_acl_contract_rejects_inheritance_or_other_readers() -> None:
    identity = "HOST\\elysium"
    private = {
        "Owner": identity,
        "Protected": True,
        "Rules": [
            {"Identity": identity, "Type": "Allow", "Inherited": False},
        ],
    }

    assert deployment._windows_acl_payload_is_private(private, identity)
    inherited = json.loads(json.dumps(private))
    inherited["Rules"][0]["Inherited"] = True
    assert not deployment._windows_acl_payload_is_private(inherited, identity)
    broad = json.loads(json.dumps(private))
    broad["Rules"].append(
        {"Identity": "BUILTIN\\Users", "Type": "Allow", "Inherited": False}
    )
    assert not deployment._windows_acl_payload_is_private(broad, identity)
    wrong_owner = json.loads(json.dumps(private))
    wrong_owner["Owner"] = "BUILTIN\\Administrators"
    assert not deployment._windows_acl_payload_is_private(wrong_owner, identity)


def test_sync_dependencies_uses_only_locked_uv_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repository(tmp_path / "repo")
    commands: list[list[str]] = []
    monkeypatch.setattr(deployment.shutil, "which", lambda name: "/fake/uv")

    def fake_run(
        command: list[str], *, root: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        del root, timeout
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "")

    monkeypatch.setattr(deployment, "_run_quiet", fake_run)

    deployment.sync_dependencies(root, with_dev=False)

    assert commands == [
        ["/fake/uv", "sync", "--locked", "--no-dev"],
        ["/fake/uv", "pip", "check"],
    ]
    flattened = " ".join(part for command in commands for part in command)
    assert "main.py" not in flattened
    assert "systemctl" not in flattened
    assert "docker" not in flattened
    assert "cron" not in flattened


def test_run_refuses_to_exec_when_doctor_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repository(tmp_path / "repo")
    report = deployment.DoctorReport(
        checks=[deployment.Check("subject.SOUL.md", "failed", "missing")]
    )
    executed = False

    def forbidden_exec(*args: object) -> None:
        nonlocal executed
        executed = True

    monkeypatch.setattr(deployment, "doctor_repository", lambda root: report)
    monkeypatch.setattr(deployment.os, "execvpe", forbidden_exec)

    assert deployment.run_foreground(root) == 2
    assert not executed


def test_writer_frozen_backup_refuses_running_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repository(tmp_path / "repo")
    deployment.initialize_configuration(root)
    monkeypatch.setattr(
        deployment,
        "find_elysium_processes",
        lambda root: [deployment.ProcessOwner(42, 1, "python", str(root))],
    )

    with pytest.raises(deployment.DeploymentError, match="writer 已冻结"):
        deployment.create_backup(
            root,
            output=tmp_path / "backup",
            writer_frozen=True,
            mysql_url_env="ELYSIUM_MYSQL_URL",
        )


def test_backup_refuses_existing_output_before_running_helper(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    output = tmp_path / "existing-backup"
    output.mkdir()

    with pytest.raises(deployment.DeploymentError, match="目标已存在"):
        deployment.create_backup(
            root,
            output=output,
            writer_frozen=False,
            mysql_url_env="ELYSIUM_MYSQL_URL",
        )


def test_backup_requires_owner_only_parent(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX mode contract")
    root = _repository(tmp_path / "repo")
    broad_parent = tmp_path / "broad-backups"
    broad_parent.mkdir(mode=0o755)

    with pytest.raises(deployment.DeploymentError, match="owner-only"):
        deployment.create_backup(
            root,
            output=broad_parent / "snapshot",
            writer_frozen=False,
            mysql_url_env="ELYSIUM_MYSQL_URL",
        )

    assert not (broad_parent / "snapshot").exists()


def test_local_backup_uses_validated_core_sqlite_and_private_precreated_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repository(tmp_path / "repo")
    deployment.initialize_configuration(root)
    (root / "data/Elysium.db").touch()
    commands: list[list[str]] = []
    monkeypatch.setattr(deployment, "_venv_python", lambda root: Path("/fake/python"))

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, check
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(deployment.subprocess, "run", fake_run)
    output = tmp_path / "new-backup"

    result = deployment.create_backup(
        root,
        output=output,
        writer_frozen=False,
        mysql_url_env="ELYSIUM_MYSQL_URL",
    )

    assert result == 0
    assert output.is_dir()
    if os.name != "nt":
        assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert len(commands) == 1
    command = commands[0]
    core_path_index = command.index("--core-sqlite-relative") + 1
    assert command[core_path_index] == "Elysium.db"
    assert "--precreated-output" in command


def test_local_backup_rejects_core_sqlite_leaf_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repository(tmp_path / "repo")
    deployment.initialize_configuration(root)
    real_database = root / "data/real.db"
    real_database.touch()
    linked_database = root / "data/Elysium.db"
    try:
        linked_database.symlink_to(real_database)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")
    monkeypatch.setattr(deployment, "_venv_python", lambda root: Path("/fake/python"))

    with pytest.raises(deployment.DeploymentError, match="普通 SQLite"):
        deployment.create_backup(
            root,
            output=tmp_path / "backup",
            writer_frozen=False,
            mysql_url_env="ELYSIUM_MYSQL_URL",
        )

    assert real_database.is_file()
    assert not (tmp_path / "backup").exists()


def test_mysql_backup_uses_private_precreated_output_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repository(tmp_path / "repo")
    deployment.initialize_configuration(root)
    core_path = root / "config/core.toml"
    core_path.write_text('[storage]\nbackend = "mysql"\n', encoding="utf-8")
    if os.name != "nt":
        core_path.chmod(0o600)
    monkeypatch.setenv("ELYSIUM_MYSQL_URL", "mysql+asyncmy://user:secret@db/elysium")
    monkeypatch.setattr(deployment, "_venv_python", lambda root: Path("/fake/python"))
    commands: list[list[str]] = []

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, check
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(deployment.subprocess, "run", fake_run)
    output = tmp_path / "mysql-backup"

    assert (
        deployment.create_backup(
            root,
            output=output,
            writer_frozen=False,
            mysql_url_env="ELYSIUM_MYSQL_URL",
        )
        == 0
    )

    assert output.is_dir()
    assert len(commands) == 1
    assert "--precreated-output" in commands[0]


def test_committed_engineering_templates_match_current_schemas(
    tmp_path: Path,
) -> None:
    from plugins.ayla_adapter.config import AylaAdapterConfig
    from plugins.emoji.config import EmojiConfig
    from plugins.feishu_adapter.config import FeishuAdapterConfig
    from plugins.kook_adapter.config import KookAdapterConfig
    from plugins.life_engine.core.config import LifeEngineConfig
    from plugins.livestream.config import LivestreamConfig
    from plugins.napcat_adapter.config import NapcatAdapterConfig
    from plugins.neko_surface.config import NekoSurfaceConfig
    from plugins.skill_manager.config import SkillManagerConfig
    from plugins.tts_voice_plugin.config import TTSVoiceConfig
    from plugins.voice_live.config import VoiceLiveConfig
    from plugins.werewolf_game.config import WerewolfConfig
    from src.core.config.core_config import CoreConfig
    from src.core.config.mcp_config import MCPConfig
    from src.kernel.config.models_loader import ModelsConfig

    project_root = deployment.repository_root()
    core = CoreConfig.load(project_root / "config/core.toml.example")
    models = ModelsConfig(project_root / "config/models.toml.example")
    assert core.storage.backend == "local"
    assert core.http_router.http_router_port == 8000
    assert core.plugin_deps.enabled is False
    assert (
        core.database.mysql_pool_recycle_seconds
        < core.database.mysql_idle_session_timeout_seconds
    )
    models.require_tasks(deployment.REQUIRED_MODEL_TASKS)

    config_types = {
        "config/mcp.toml": MCPConfig,
        "config/plugins/ayla_adapter/config.toml": AylaAdapterConfig,
        "config/plugins/emoji/config.toml": EmojiConfig,
        "config/plugins/feishu_adapter/config.toml": FeishuAdapterConfig,
        "config/plugins/kook_adapter/config.toml": KookAdapterConfig,
        "config/plugins/life_engine/config.toml": LifeEngineConfig,
        "config/plugins/Livestream/config.toml": LivestreamConfig,
        "config/plugins/napcat_adapter/config.toml": NapcatAdapterConfig,
        "config/plugins/neko_surface/config.toml": NekoSurfaceConfig,
        "config/plugins/skill_manager/config.toml": SkillManagerConfig,
        "config/plugins/tts_voice_plugin/config.toml": TTSVoiceConfig,
        "config/plugins/Voice-Live/config.toml": VoiceLiveConfig,
        "config/plugins/werewolf_game/config.toml": WerewolfConfig,
    }
    for relative, config_type in config_types.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(deployment.PLUGIN_CONFIGS[relative], encoding="utf-8")
        config_type.load(target, auto_update=False)


def test_schema_helper_loads_every_generated_plugin_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repository(tmp_path / "repo")
    shutil.copy2(
        deployment.repository_root() / "config/models.toml.example",
        root / "config/models.toml.example",
    )
    deployment.initialize_configuration(root)
    monkeypatch.setenv("ELYSIUM_NEXUS_API_KEY", "configured")

    assert deployment_schema_check.main(root) == 0

    (root / "config/plugins/neko_surface/config.toml").write_text(
        '[plugin]\nenabled = "not-a-boolean"\n',
        encoding="utf-8",
    )
    assert deployment_schema_check.main(root) == 2


def test_repository_has_no_elysium_autostart_assets_or_legacy_script_secrets() -> None:
    project_root = deployment.repository_root()
    for relative in (
        "Dockerfile",
        "docker-compose.yml",
        "elysium.service",
        ".github/workflows/docker-image.yml",
    ):
        assert not (project_root / relative).exists()

    script_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (project_root / "scripts").glob("*")
        if path.is_file() and path.suffix in {".py", ".sh"}
    )
    assert "/root/Elysia/Elysium" not in script_text
    assert "asyncmy.connect(host=" not in script_text
    assert "export ELYSIUM_MYSQL_PASSWORD=" not in script_text
    assert re.search(r"(?<![\w-])-p[^\s\"']+", script_text) is None


def test_windows_legacy_entrypoint_forwards_to_manual_deployment_run() -> None:
    start_entrypoint = (deployment.repository_root() / "start.bat").read_text(
        encoding="utf-8"
    )

    assert "cleanup_leases" not in start_entrypoint
    assert 'deploy.ps1" run' in start_entrypoint
    assert "/root/" not in start_entrypoint
    assert "export ELYSIUM_MYSQL_PASSWORD=" not in start_entrypoint
