"""Neo-MoFox 主入口

启动 Neo-MoFox Bot 应用。
"""
import asyncio
from pathlib import Path
import tomllib

from src.app.runtime.console_ui import UILevel
from src.app.runtime.single_instance import AlreadyRunningError, SingleInstanceLock


_INSTANCE_LOCK_PATH = Path("data/runtime/elysium.lock")


def load_ui_level_from_config(config_path: str = "config/core.toml") -> UILevel:
    """从配置文件加载 UI 级别

    Args:
        config_path: 配置文件路径

    Returns:
        UILevel: UI 级别枚举值
    """
    level_map = {
        "minimal": UILevel.MINIMAL,
        "standard": UILevel.STANDARD,
        "verbose": UILevel.VERBOSE,
    }

    path = Path(config_path)
    if not path.exists():
        return UILevel.STANDARD

    with path.open("rb") as f:
        config = tomllib.load(f)

    ui_level = config.get("bot", {}).get("ui_level", "standard")
    if not isinstance(ui_level, str):
        raise ValueError("bot.ui_level must be a string")

    ui_level_key = ui_level.lower()
    if ui_level_key not in level_map:
        valid_levels = ", ".join(level_map)
        raise ValueError(
            f"Invalid bot.ui_level '{ui_level}'. Expected one of: {valid_levels}"
        )

    return level_map[ui_level_key]


async def main() -> None:
    """主函数"""
    from src.app.runtime import Bot

    # 从配置文件读取 UI 级别
    ui_level = load_ui_level_from_config("config/core.toml")

    # 创建 Bot 实例
    bot = Bot(
        config_path="config/core.toml",
        plugins_dir="plugins",
        log_dir="logs",
        ui_level=ui_level,
    )

    # 启动 Bot（包含初始化、运行和关闭）
    await bot.start()


if __name__ == "__main__":
    try:
        # 运行异步主函数
        with SingleInstanceLock(_INSTANCE_LOCK_PATH):
            asyncio.run(main())
    except AlreadyRunningError as exc:
        print(f"\n[Startup refused: {exc}]")
        raise SystemExit(2) from exc
    except KeyboardInterrupt:
        # 用户中断（Ctrl+C）
        print("\n[Interrupted by user]")
    except Exception as e:
        # 捕获并显示其他异常
        print(f"\n[Fatal error: {e}]")
        raise
