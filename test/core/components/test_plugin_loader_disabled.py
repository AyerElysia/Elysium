"""插件 manifest 顶层停用开关契约测试。"""

from __future__ import annotations

import json

from src.core.components.loader import PluginLoader, load_manifest


def _write_manifest(path, *, enabled: bool) -> None:
    path.mkdir(parents=True)
    (path / "manifest.json").write_text(
        json.dumps(
            {
                "name": path.name,
                "enabled": enabled,
                "version": "1.0.0",
                "description": "test plugin",
                "author": "test",
                "dependencies": {"plugins": [], "components": []},
                "entry_point": "plugin.py",
                "min_core_version": "1.0.0",
            }
        ),
        encoding="utf-8",
    )


async def test_disabled_manifest_is_parsed_but_excluded_from_plan(tmp_path) -> None:
    enabled_dir = tmp_path / "enabled_plugin"
    disabled_dir = tmp_path / "disabled_plugin"
    _write_manifest(enabled_dir, enabled=True)
    _write_manifest(disabled_dir, enabled=False)

    enabled_manifest = await load_manifest(str(enabled_dir))
    disabled_manifest = await load_manifest(str(disabled_dir))
    assert enabled_manifest is not None and enabled_manifest.enabled is True
    assert disabled_manifest is not None and disabled_manifest.enabled is False

    order, manifests = await PluginLoader().plan_plugins(str(tmp_path))

    assert order == ["enabled_plugin"]
    assert set(manifests) == {"enabled_plugin"}
