"""Configuration-only migration preserves settings and exact rollback bytes."""
import tomllib

import pytest

from plugins.minecraft.config_migration import (
    MigrationConflict, migrate_files, plan_migration,
)


SOURCE = """# Keep my edits.
[settings]
enabled = true
workspace_path = '/private/subject'
[history_retrieval]
chat_max_result_bytes = 12345
# previous scene budget
minecraft_max_result_bytes = 4096
[minecraft]
enabled = true
bot_username = 'Elysia'
bot_server_port = 25567
[learning]
enabled = false
"""


def test_plan_preserves_non_game_configuration_and_custom_budget():
    remaining, game = plan_migration(SOURCE)
    parsed = tomllib.loads(remaining)
    assert "minecraft" not in parsed
    assert parsed["settings"]["workspace_path"] == "/private/subject"
    assert parsed["learning"] == {"enabled": False}
    assert parsed["history_retrieval"] == {"chat_max_result_bytes": 12345}
    assert "# Keep my edits." in remaining
    assert tomllib.loads(game)["settings"] == {
        "enabled": True, "bot_username": "Elysia", "bot_server_port": 25567,
        "evidence_max_result_bytes": 4096,
    }


def test_migration_dry_run_backup_and_idempotence(tmp_path):
    source, destination = tmp_path / "life.toml", tmp_path / "game/config.toml"
    source.write_bytes(SOURCE.encode())
    assert migrate_files(source, destination) == "ready"
    assert source.read_bytes() == SOURCE.encode()
    assert not destination.exists()
    assert migrate_files(source, destination, apply=True).startswith("migrated;")
    assert source.with_name("life.toml.minecraft-migration.bak").read_bytes() == SOURCE.encode()
    assert migrate_files(source, destination, apply=True) == "already_migrated"


def test_partial_destination_install_is_safe_to_resume(tmp_path):
    source, destination = tmp_path / "life.toml", tmp_path / "game.toml"
    source.write_text(SOURCE)
    _, game = plan_migration(SOURCE)
    destination.write_text(game)
    assert migrate_files(source, destination, apply=True).startswith("migrated;")


def test_conflicting_destination_and_backup_are_never_overwritten(tmp_path):
    source, destination = tmp_path / "life.toml", tmp_path / "game.toml"
    source.write_text(SOURCE)
    destination.write_text("[settings]\nenabled = false\n")
    with pytest.raises(MigrationConflict, match="AlreadyDiffers"):
        migrate_files(source, destination, apply=True)
    assert source.read_text() == SOURCE
    assert "false" in destination.read_text()
    destination.unlink()
    backup = source.with_name("life.toml.minecraft-migration.bak")
    backup.write_bytes(b"existing rollback")
    with pytest.raises(MigrationConflict, match="BackupAlreadyExists"):
        migrate_files(source, destination, apply=True)
    assert backup.read_bytes() == b"existing rollback"


def test_migration_does_not_silently_ignore_future_destination_fields():
    _, game = plan_migration(SOURCE)
    with pytest.raises(MigrationConflict, match="AlreadyDiffers"):
        plan_migration(SOURCE, game + "future_setting = 1\n")


def test_same_file_and_missing_configuration_fail_closed(tmp_path):
    source = tmp_path / "life.toml"
    source.write_text(SOURCE)
    with pytest.raises(MigrationConflict, match="MustDiffer"):
        migrate_files(source, source, apply=True)
    with pytest.raises(MigrationConflict, match="NoLegacy"):
        plan_migration("[settings]\nenabled = true\n")
