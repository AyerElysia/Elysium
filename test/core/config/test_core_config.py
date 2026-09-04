"""测试 CoreConfig 配置模块。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.config.core_config import CoreConfig, get_core_config, init_core_config
from src.kernel.llm.policy import (
    RoundRobinPolicy,
    create_default_policy,
    set_default_policy_factory,
)


class TestBotSection:
    """测试运行时与聊天流预算配置。"""

    def test_default_stream_step_budget_covers_model_failover(self) -> None:
        config = CoreConfig.BotSection()

        assert config.stream_step_timeout == 300.0
        assert config.stream_restart_threshold > config.stream_step_timeout
        assert config.console_log_level == "ERROR"

    def test_restart_threshold_must_exceed_enabled_step_budget(self) -> None:
        with pytest.raises(
            ValueError,
            match="stream_restart_threshold must be greater than stream_step_timeout",
        ):
            CoreConfig.BotSection(
                stream_step_timeout=300.0,
                stream_restart_threshold=300.0,
            )

    @pytest.mark.parametrize("step_timeout", [0.0, -1.0])
    def test_disabled_step_budget_does_not_constrain_restart_threshold(
        self,
        step_timeout: float,
    ) -> None:
        config = CoreConfig.BotSection(
            stream_step_timeout=step_timeout,
            stream_restart_threshold=10.0,
        )

        assert config.stream_step_timeout == step_timeout
        assert config.stream_restart_threshold == 10.0


class TestChatSection:
    """测试聊天配置节。"""

    def test_default_chat_config(self):
        """测试默认聊天配置。"""
        config = CoreConfig.ChatSection()

        assert config.default_chat_mode == "normal"
        assert config.max_context_size == 20
        assert config.max_history_messages == 20
        assert not hasattr(config, "max_llm_messages")

    def test_custom_chat_config(self):
        """测试自定义聊天配置。"""
        config = CoreConfig.ChatSection(
            default_chat_mode="focus",
            max_history_messages=200,
            max_llm_messages=0,
        )

        assert config.default_chat_mode == "focus"
        assert config.max_history_messages == 200
        assert config.max_context_size == 200
        assert not hasattr(config, "max_llm_messages")

    def test_legacy_max_context_size_config(self):
        """旧 max_context_size 只映射聊天流历史保留。"""
        config = CoreConfig.ChatSection(max_context_size=200)

        assert config.max_context_size == 200
        assert config.max_history_messages == 200
        assert not hasattr(config, "max_llm_messages")


class TestLLMSection:
    """测试 LLM 配置节。"""

    def test_default_llm_config(self):
        """测试默认 LLM 配置。"""
        config = CoreConfig.LLMSection()

        assert config.default_policy == "failover"

    def test_custom_llm_config(self):
        """测试自定义 LLM 配置。"""
        config = CoreConfig.LLMSection(default_policy="round_robin")

        assert config.default_policy == "round_robin"


class TestDatabaseSection:
    """测试数据库配置节。"""

    def test_default_database_config(self):
        """数据库节只保存连接参数，不选择后端。"""
        config = CoreConfig.DatabaseSection()

        assert config.sqlite_path == "data/Elysium.db"
        assert not hasattr(config, "database_type")

    def test_global_storage_config(self):
        """全局存储节只允许 local 或 mysql。"""
        assert CoreConfig.StorageSection().backend == "local"
        assert CoreConfig.StorageSection(backend="mysql").backend == "mysql"


class TestPermissionSection:
    """测试权限配置节。"""

    def test_default_permission_config(self):
        """测试默认权限配置。"""
        config = CoreConfig.PermissionSection()

        assert config.owner_list == []
        assert config.default_permission_level == "user"
        assert config.allow_operator_promotion is False
        assert config.allow_operator_demotion is False
        assert config.max_operator_promotion_level == "operator"
        assert config.allow_command_override is True
        assert config.override_requires_owner_approval is False
        assert config.enable_permission_cache is True
        assert config.permission_cache_ttl == 300
        assert config.strict_mode is True
        assert config.log_permission_denied is True
        assert config.log_permission_granted is False

    def test_custom_owner_list(self):
        """测试自定义所有者列表。"""
        config = CoreConfig.PermissionSection(
            owner_list=["qq:123456", "telegram:789012"],
        )

        assert len(config.owner_list) == 2
        assert "qq:123456" in config.owner_list

    def test_enable_operator_promotion(self):
        """测试启用 operator 提升权限。"""
        config = CoreConfig.PermissionSection(
            allow_operator_promotion=True,
            max_operator_promotion_level="user",
        )

        assert config.allow_operator_promotion is True
        assert config.max_operator_promotion_level == "user"

    def test_permission_cache_settings(self):
        """测试权限缓存设置。"""
        config = CoreConfig.PermissionSection(
            enable_permission_cache=True,
            permission_cache_ttl=600,
        )

        assert config.enable_permission_cache is True
        assert config.permission_cache_ttl == 600

    def test_permission_logging_settings(self):
        """测试权限日志设置。"""
        config = CoreConfig.PermissionSection(
            log_permission_denied=False,
            log_permission_granted=True,
        )

        assert config.log_permission_denied is False
        assert config.log_permission_granted is True



class TestChatSectionLegacyKeys:
    """测试旧配置字段通过迁移与 auto-update 安全移除。"""

    def test_init_core_config_strips_legacy_context_validation_mode(self, temp_dir: Path) -> None:
        """旧配置里残留 context_validation_mode 不应导致加载失败，并应被自动移除。"""
        import src.core.config.core_config as core_config_module

        original_config = core_config_module._global_config
        core_config_module._global_config = None

        try:
            config_file = temp_dir / "core.toml"
            config_file.write_text(
                """
[chat]
default_chat_mode = \"focus\"
max_context_size = 150
context_validation_mode = \"repair\"
""".lstrip(),
                encoding="utf-8",
            )

            config = init_core_config(str(config_file))
            assert config.chat.default_chat_mode == "focus"
            assert config.chat.max_context_size == 150
            assert config.chat.max_history_messages == 150
            assert not hasattr(config.chat, "max_llm_messages")

            updated = config_file.read_text(encoding="utf-8")
            assert "context_validation_mode" not in updated
            assert "max_context_size" not in updated
            assert "max_history_messages" in updated
            assert "max_llm_messages" not in updated
        finally:
            core_config_module._global_config = original_config

    def test_init_core_config_strips_derived_authority_provider(
        self,
        temp_dir: Path,
    ) -> None:
        """旧 authority_provider 自动移除，运行时只由 backend 派生。"""

        import src.core.config.core_config as core_config_module

        original_config = core_config_module._global_config
        core_config_module._global_config = None

        try:
            config_file = temp_dir / "core.toml"
            config_file.write_text(
                """
[storage]
backend = "local"
backend_generation = "verified-mysql-v1"
authority_provider = "mysql"
""".lstrip(),
                encoding="utf-8",
            )

            config = init_core_config(str(config_file))

            assert config.storage.backend == "local"
            assert config.storage.backend_generation == "verified-mysql-v1"
            assert not hasattr(config.storage, "authority_provider")
            updated = config_file.read_text(encoding="utf-8")
            assert "authority_provider" not in updated
            assert 'backend = "local"' in updated
            assert 'backend_generation = "verified-mysql-v1"' in updated
        finally:
            core_config_module._global_config = original_config


class TestCoreConfig:
    """测试 CoreConfig 主配置类。"""

    def test_create_default_config(self):
        """测试创建默认配置。"""
        config = CoreConfig()

        assert isinstance(config.chat, CoreConfig.ChatSection)
        assert isinstance(config.llm, CoreConfig.LLMSection)
        assert isinstance(config.database, CoreConfig.DatabaseSection)
        assert isinstance(config.permissions, CoreConfig.PermissionSection)

    def test_chat_settings(self):
        """测试聊天配置设置。"""
        config = CoreConfig(
            chat=CoreConfig.ChatSection(
                default_chat_mode="proactive",
                max_history_messages=150,
                max_llm_messages=75,
            )
        )

        assert config.chat.default_chat_mode == "proactive"
        assert config.chat.max_history_messages == 150
        assert not hasattr(config.chat, "max_llm_messages")

    def test_database_settings(self):
        """测试数据库配置设置。"""
        config = CoreConfig(
            storage=CoreConfig.StorageSection(backend="mysql"),
            database=CoreConfig.DatabaseSection(mysql_host="db.internal"),
        )

        assert config.storage.backend == "mysql"
        assert config.database.mysql_host == "db.internal"

    def test_permission_settings(self):
        """测试权限配置设置。"""
        config = CoreConfig(
            permissions=CoreConfig.PermissionSection(
                owner_list=["qq:123"],
                default_permission_level="operator",
            ),
        )

        assert len(config.permissions.owner_list) == 1
        assert config.permissions.default_permission_level == "operator"

    def test_full_config(self):
        """测试完整配置。"""
        config = CoreConfig(
            chat=CoreConfig.ChatSection(
                default_chat_mode="priority",
                max_history_messages=200,
                max_llm_messages=0,
            ),
            llm=CoreConfig.LLMSection(default_policy="round_robin"),
            storage=CoreConfig.StorageSection(backend="mysql"),
            database=CoreConfig.DatabaseSection(mysql_host="db.internal"),
            permissions=CoreConfig.PermissionSection(
                owner_list=["qq:123", "telegram:456"],
                default_permission_level="operator",
                allow_operator_promotion=True,
                strict_mode=False,
            ),
        )

        assert config.chat.default_chat_mode == "priority"
        assert config.chat.max_history_messages == 200
        assert not hasattr(config.chat, "max_llm_messages")
        assert config.llm.default_policy == "round_robin"
        assert config.storage.backend == "mysql"
        assert config.database.mysql_host == "db.internal"
        assert len(config.permissions.owner_list) == 2


class TestGlobalCoreConfig:
    """测试全局 Core 配置管理。"""

    def test_init_core_config_default(self, temp_dir: Path):
        """测试使用默认配置初始化。"""
        import src.core.config.core_config as core_config_module
        original_config = core_config_module._global_config
        core_config_module._global_config = None

        try:
            config_path = temp_dir / "core.toml"
            config = init_core_config(str(config_path))
            assert config is not None
            assert isinstance(config, CoreConfig)
        finally:
            core_config_module._global_config = original_config

    def test_init_core_config_from_file(self, temp_dir: Path):
        """测试从文件加载配置。"""
        import src.core.config.core_config as core_config_module
        original_config = core_config_module._global_config
        core_config_module._global_config = None

        try:
            config_file = temp_dir / "core.toml"
            config_file.write_text(
                """
[chat]
default_chat_mode = "focus"
max_context_size = 150

[llm]
default_policy = "round_robin"

[database]
sqlite_path = "data/test.db"

[permissions]
owner_list = ["qq:123", "telegram:456"]
default_permission_level = "operator"
allow_operator_promotion = true
"""
            )

            config = init_core_config(str(config_file))
            assert config.chat.default_chat_mode == "focus"
            assert config.chat.max_context_size == 150
            assert config.chat.max_history_messages == 150
            assert not hasattr(config.chat, "max_llm_messages")
            assert config.llm.default_policy == "round_robin"
            assert config.storage.backend == "local"
            assert config.database.sqlite_path == "data/test.db"
            assert len(config.permissions.owner_list) == 2
            assert isinstance(create_default_policy(), RoundRobinPolicy)
        finally:
            set_default_policy_factory(None)
            core_config_module._global_config = original_config

    def test_legacy_mysql_selector_migrates_to_global_storage(self, temp_dir: Path):
        """旧 database_type 应自动迁移且从数据库节删除。"""
        import src.core.config.core_config as core_config_module

        original_config = core_config_module._global_config
        core_config_module._global_config = None
        try:
            config_file = temp_dir / "core.toml"
            config_file.write_text(
                '[database]\ndatabase_type = "mysql"\nmysql_host = "db.internal"\n',
                encoding="utf-8",
            )

            config = init_core_config(str(config_file))
            updated = config_file.read_text(encoding="utf-8")

            assert config.storage.backend == "mysql"
            assert config.database.mysql_host == "db.internal"
            assert "database_type" not in updated
            assert '[storage]' in updated
            assert 'backend = "mysql"' in updated
        finally:
            core_config_module._global_config = original_config

    def test_legacy_authority_snapshot_is_removed(self, temp_dir: Path):
        """旧 epoch/token 配置不得继续成为启动凭据。"""
        import src.core.config.core_config as core_config_module

        original_config = core_config_module._global_config
        core_config_module._global_config = None
        try:
            config_file = temp_dir / "core.toml"
            config_file.write_text(
                '[storage]\nbackend = "mysql"\n'
                'authority_epoch = 99\n'
                'fencing_token_env = "OLD_TOKEN"\n',
                encoding="utf-8",
            )

            config = init_core_config(str(config_file))
            updated = config_file.read_text(encoding="utf-8")

            assert config.storage.backend == "mysql"
            assert not hasattr(config.storage, "authority_epoch")
            assert not hasattr(config.storage, "fencing_token_env")
            assert "authority_epoch" not in updated
            assert "fencing_token_env" not in updated
        finally:
            core_config_module._global_config = original_config

    def test_conflicting_legacy_and_global_selectors_are_rejected(
        self, temp_dir: Path
    ):
        """双开关选择不一致时必须失败，不能静默混合。"""
        import src.core.config.core_config as core_config_module

        original_config = core_config_module._global_config
        core_config_module._global_config = None
        try:
            config_file = temp_dir / "core.toml"
            config_file.write_text(
                '[storage]\nbackend = "local"\n\n'
                '[database]\ndatabase_type = "mysql"\n',
                encoding="utf-8",
            )

            with pytest.raises(ValueError, match="Conflicting storage selection"):
                init_core_config(str(config_file))
        finally:
            core_config_module._global_config = original_config

    def test_get_core_config_before_init_raises(self):
        """测试未初始化时获取配置抛出异常。"""
        import src.core.config.core_config as core_config_module
        original_config = core_config_module._global_config
        core_config_module._global_config = None

        try:
            with pytest.raises(RuntimeError, match="Core config not initialized"):
                get_core_config()
        finally:
            core_config_module._global_config = original_config

    def test_get_core_config_after_init(self, temp_dir: Path):
        """测试初始化后获取配置。"""
        import src.core.config.core_config as core_config_module
        original_config = core_config_module._global_config
        core_config_module._global_config = None

        try:
            config_path = temp_dir / "core.toml"
            init_core_config(str(config_path))
            config = get_core_config()

            assert isinstance(config, CoreConfig)
        finally:
            core_config_module._global_config = original_config

    def test_init_core_config_multiple_times(self, temp_dir: Path):
        """测试多次初始化更新配置。"""
        import src.core.config.core_config as core_config_module
        original_config = core_config_module._global_config
        core_config_module._global_config = None

        try:
            config_path = temp_dir / "core.toml"
            config1 = init_core_config(str(config_path))
            config2 = init_core_config(str(config_path))

            # 第二次应该返回新创建的实例（因为重新初始化了）
            assert config1 is not config2
            assert config2 is not None
            assert isinstance(config2, CoreConfig)
            # get_core_config 应该返回第二次初始化的实例
            config3 = get_core_config()
            assert config3 is config2
        finally:
            core_config_module._global_config = original_config


class TestCoreConfigScenarios:
    """测试 Core 配置的实际使用场景。"""

    def test_minimal_config(self):
        """测试最小配置场景。"""
        config = CoreConfig()

        # 应该能使用所有默认值
        assert config.chat.default_chat_mode == "normal"
        assert config.storage.backend == "local"
        assert config.permissions.default_permission_level == "user"

    def test_strict_permissions_config(self):
        """测试严格权限配置场景。"""
        config = CoreConfig(
            permissions=CoreConfig.PermissionSection(
                owner_list=["qq:123"],
                default_permission_level="guest",
                allow_operator_promotion=False,
                allow_command_override=False,
                strict_mode=True,
                log_permission_denied=True,
            ),
        )

        assert config.permissions.default_permission_level == "guest"
        assert config.permissions.strict_mode is True
        assert config.permissions.allow_command_override is False

    def test_development_config(self):
        """测试开发环境配置。"""
        config = CoreConfig(
            chat=CoreConfig.ChatSection(
                default_chat_mode="normal",
                max_history_messages=50,
                max_llm_messages=25,
            ),
            permissions=CoreConfig.PermissionSection(
                owner_list=["qq:123"],
                default_permission_level="owner",
                strict_mode=False,
                log_permission_granted=True,
            ),
        )

        assert config.chat.max_history_messages == 50
        assert not hasattr(config.chat, "max_llm_messages")
        assert config.permissions.strict_mode is False
        assert config.permissions.log_permission_granted is True

    def test_production_config(self):
        """测试生产环境配置。"""
        config = CoreConfig(
            storage=CoreConfig.StorageSection(backend="mysql"),
            database=CoreConfig.DatabaseSection(mysql_host="db.internal"),
            chat=CoreConfig.ChatSection(
                default_chat_mode="priority",
                max_history_messages=200,
                max_llm_messages=0,
            ),
            permissions=CoreConfig.PermissionSection(
                owner_list=["qq:123", "telegram:456"],
                default_permission_level="user",
                enable_permission_cache=True,
                permission_cache_ttl=300,
                strict_mode=True,
            ),
        )

        assert config.storage.backend == "mysql"
        assert config.database.mysql_host == "db.internal"
        assert config.chat.max_history_messages == 200
        assert not hasattr(config.chat, "max_llm_messages")
        assert config.permissions.enable_permission_cache is True

    def test_multi_owner_config(self):
        """测试多所有者配置。"""
        config = CoreConfig(
            permissions=CoreConfig.PermissionSection(
                owner_list=[
                    "qq:123456",
                    "qq:789012",
                    "telegram:345678",
                    "discord:901234",
                ],
            ),
        )

        assert len(config.permissions.owner_list) == 4
        assert "qq:123456" in config.permissions.owner_list
