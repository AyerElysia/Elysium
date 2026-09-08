"""Explicit, lossless configuration extraction; never starts services or games."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import tempfile
import tomllib

from .config import MinecraftConfig


class MigrationConflict(ValueError):
    """Existing files cannot be reconciled without an explicit operator choice."""


def plan_migration(source: str, destination: str | None = None) -> tuple[str, str]:
    """Move only the game table and budget; prove all other parsed values equal."""

    original = tomllib.loads(source)
    section = original.get("minecraft")
    if section is None:
        if destination is None:
            raise MigrationConflict("NoLegacySectionOrIndependentConfig")
        MinecraftConfig.model_validate(tomllib.loads(destination))
        return source, destination
    if not isinstance(section, dict):
        raise MigrationConflict("LegacySectionMustBeTable")
    if any(isinstance(item, dict) for item in section.values()):
        raise MigrationConflict("NestedLegacyTablesRequireExplicitMigration")
    match = re.search(r"(?m)^\[minecraft\][ \t]*(?:#.*)?$", source)
    if match is None:
        raise MigrationConflict("LegacySectionHeaderNotRecognized")
    following = re.search(r"(?m)^\[", source[match.end():])
    end = match.end() + following.start() if following else len(source)
    migrated = "[settings]" + source[match.end():end]
    remaining = source[:match.start()] + source[end:]
    expected = dict(original)
    del expected["minecraft"]
    expected_settings = dict(section)
    history = dict(expected.get("history_retrieval", {}))
    budget = history.pop("minecraft_max_result_bytes", None)
    if budget is not None:
        expected["history_retrieval"] = history
        # Values remain exact; only comments directly attached to this old field
        # and the field itself are removed from the shared config.
        remaining, count = re.subn(
            r"(?m)(?:^#[^\n]*\n)*^minecraft_max_result_bytes[ \t]*=[^\n]*(?:\n|$)",
            "", remaining,
        )
        if count != 1:
            raise MigrationConflict("LegacyEvidenceBudgetNotUniquelyLocated")
        if "evidence_max_result_bytes" in expected_settings:
            if expected_settings["evidence_max_result_bytes"] != budget:
                raise MigrationConflict("EvidenceBudgetConflict")
        else:
            migrated = migrated.rstrip() + f"\nevidence_max_result_bytes = {int(budget)}\n"
            expected_settings["evidence_max_result_bytes"] = budget
    if tomllib.loads(remaining) != expected:
        raise MigrationConflict("UnrelatedConfigurationWouldChange")
    if tomllib.loads(migrated) != {"settings": expected_settings}:
        raise MigrationConflict("GameConfigurationWouldChange")
    parsed = MinecraftConfig.model_validate(tomllib.loads(migrated))
    if destination is not None:
        # Never overwrite a independently edited destination, including unknown
        # fields that a future plugin version may own.
        if tomllib.loads(destination) != tomllib.loads(migrated):
            raise MigrationConflict("IndependentConfigAlreadyDiffers")
        current = MinecraftConfig.model_validate(tomllib.loads(destination))
        if current != parsed:
            raise MigrationConflict("IndependentConfigAlreadyDiffers")
        migrated = destination
    return remaining, migrated


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def migrate_files(source: Path, destination: Path, *, apply: bool = False) -> str:
    """Back up exact source bytes, write destination first, then remove legacy keys.

    A crash after destination installation is safe to rerun: the still-present
    legacy section must match. A changed destination fails without overwriting it.
    This touches configuration only, never the subject workspace or event history.
    """

    source = source.resolve()
    destination = destination.resolve()
    if source == destination:
        raise MigrationConflict("SourceAndDestinationMustDiffer")
    original = source.read_bytes()
    previous = destination.read_bytes() if destination.exists() else None
    remaining, migrated = plan_migration(
        original.decode("utf-8"), previous.decode("utf-8") if previous is not None else None,
    )
    if "minecraft" not in tomllib.loads(original.decode("utf-8")):
        return "already_migrated"
    if not apply:
        return "ready"
    backup = source.with_name(source.name + ".minecraft-migration.bak")
    if backup.exists():
        if backup.read_bytes() != original:
            raise MigrationConflict("BackupAlreadyExistsWithDifferentContent")
    else:
        # Exclusive creation preserves an earlier rollback artifact unchanged.
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
    if source.read_bytes() != original:
        raise MigrationConflict("SourceChangedDuringMigration")
    current = destination.read_bytes() if destination.exists() else None
    if current != previous:
        raise MigrationConflict("DestinationChangedDuringMigration")
    if previous is None:
        _atomic_write(destination, migrated.encode("utf-8"))
    if source.read_bytes() != original:
        raise MigrationConflict("SourceChangedAfterDestinationInstall")
    _atomic_write(source, remaining.encode("utf-8"))
    return "migrated; backup_sha256=" + hashlib.sha256(original).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--life-config", type=Path, default=Path("config/plugins/life_engine/config.toml"))
    parser.add_argument("--minecraft-config", type=Path, default=Path("config/plugins/minecraft/config.toml"))
    parser.add_argument("--apply", action="store_true", help="write only validated configuration, with an exact source backup")
    args = parser.parse_args()
    print(migrate_files(args.life_config, args.minecraft_config, apply=args.apply))


if __name__ == "__main__":
    main()
