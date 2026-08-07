"""
media_api 示例脚本

展示媒体 API 的保存与查询能力。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[5]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.app.plugin_system.api import media_api
from src.core.config import get_core_config, init_core_config, init_model_config
from src.core.utils.schema_sync import enforce_database_schema_consistency
from src.kernel.db import init_database_from_config
from src.kernel.scheduler import get_unified_scheduler


async def main() -> None:
    """演示 media_api 的基础功能。"""
    init_core_config(str(REPO_ROOT / "config" / "core.toml"))
    init_model_config(str(REPO_ROOT / "config" / "models.toml"))
    core_cfg = get_core_config()
    db_cfg = core_cfg.database
    await init_database_from_config(
        database_type="mysql" if core_cfg.storage.backend == "mysql" else "sqlite",
        sqlite_path=db_cfg.sqlite_path,
        mysql_host=db_cfg.mysql_host,
        mysql_port=db_cfg.mysql_port,
        mysql_database=db_cfg.mysql_database,
        mysql_user=db_cfg.mysql_user,
        mysql_password=db_cfg.mysql_password,
        mysql_charset=db_cfg.mysql_charset,
        mysql_ssl_mode=db_cfg.mysql_ssl_mode,
        mysql_ssl_ca=db_cfg.mysql_ssl_ca,
        mysql_ssl_cert=db_cfg.mysql_ssl_cert,
        mysql_ssl_key=db_cfg.mysql_ssl_key,
        connection_pool_size=db_cfg.connection_pool_size,
        connection_timeout=db_cfg.connection_timeout,
        echo=db_cfg.echo,
    )
    await enforce_database_schema_consistency()
    await get_unified_scheduler().start()

    suffix = int(time.time())
    media_hash = f"demo_media_hash_{suffix}"
    await media_api.save_media_info(
        media_hash=media_hash,
        media_type="image",
        file_path=f"data/media_cache/demo_image_{suffix}.png",
        description="demo image",
        vlm_processed=False,
    )

    info = await media_api.get_media_info(media_hash)
    print(f"媒体信息: {info}")


if __name__ == "__main__":
    asyncio.run(main())
