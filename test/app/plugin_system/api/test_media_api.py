"""media_api 的单元测试。

测试覆盖：
- recognize_media
- recognize_batch
- save_media_info
- get_media_info
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from src.app.plugin_system.api import media_api


class TestMediaAPI:
    """测试媒体 API。"""
    
    async def test_recognize_media(self) -> None:
        """测试识别媒体。"""
        with patch('src.app.plugin_system.api.media_api._get_media_manager') as mock_get_mgr:
            mock_manager = MagicMock()
            mock_manager.recognize_media = AsyncMock(return_value="A picture of a cat")
            mock_get_mgr.return_value = mock_manager
            
            result = await media_api.recognize_media("base64data", "image", use_cache=True)
            
            assert result == "A picture of a cat"
    
    async def test_recognize_batch(self) -> None:
        """测试批量识别媒体。"""
        with patch('src.app.plugin_system.api.media_api._get_media_manager') as mock_get_mgr:
            mock_manager = MagicMock()
            mock_manager.recognize_media = AsyncMock(side_effect=["Cat", "Dog"])
            mock_get_mgr.return_value = mock_manager
            
            media_list = [("base64_1", "image"), ("base64_2", "image")]
            result = await media_api.recognize_batch(media_list)
            
            assert len(result) == 2
            assert result[0] == (0, "Cat")
            assert result[1] == (1, "Dog")
            assert mock_manager.recognize_media.await_count == 2
    
    async def test_save_media_info(self) -> None:
        """测试保存媒体信息。"""
        with patch('src.app.plugin_system.api.media_api._get_media_manager') as mock_get_mgr:
            mock_manager = MagicMock()
            mock_manager.save_media_info = AsyncMock()
            mock_get_mgr.return_value = mock_manager
            
            await media_api.save_media_info(
                "hash123",
                "image",
                file_path="/path/to/image.jpg",
                description="Test image",
                vlm_processed=True
            )
            
            mock_manager.save_media_info.assert_called_once()
    
    async def test_get_media_info(self) -> None:
        """测试获取媒体信息。"""
        with patch('src.app.plugin_system.api.media_api._get_media_manager') as mock_get_mgr:
            mock_manager = MagicMock()
            media_info = {
                "hash": "hash123",
                "media_type": "image",
                "description": "Test image"
            }
            mock_manager.get_media_info = AsyncMock(return_value=media_info)
            mock_get_mgr.return_value = mock_manager
            
            result = await media_api.get_media_info("hash123")
            
            assert result is not None
            assert result["hash"] == "hash123"
