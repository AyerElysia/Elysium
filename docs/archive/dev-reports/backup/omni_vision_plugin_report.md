# Omni Vision Plugin Report

**Date:** 2026-03-18
**Task:** 创建全模态视觉插件，允许主模型直接接收图片

## Summary

成功创建了 `omni_vision_plugin` 全模态视觉插件，实现了绕过 VLM 转译、让主模型直接处理图片的功能。

## Implementation Details

### Files Created

#### Plugin Directory (`plugins/omni_vision_plugin/`)
- `manifest.json` - 插件清单，定义插件名称、版本、组件列表
- `__init__.py` - 包入口文件
- `config.py` - Pydantic 配置类定义
- `plugin.py` - 主插件类 `OmniVisionPlugin` 和事件处理器 `OmniVisionHandler`
- `README.md` - 插件使用文档

#### Config Directory (`config/plugins/omni_vision_plugin/`)
- `config.toml` - 插件配置文件

### Key Code

#### Event Handler
```python
class OmniVisionHandler(BaseEventHandler):
    handler_name = "omni_vision_handler"
    init_subscribe = [EventType.ON_MESSAGE_RECEIVED]

    async def execute(self, event_name, params):
        if not self.config.settings.enable_omni_vision:
            return EventDecision.SUCCESS, params

        # 从消息对象中提取 stream_id
        message = params.get("message")
        stream_id = getattr(message, "stream_id", None)

        manager = get_media_manager()
        manager.skip_vlm_for_stream(stream_id)
```

### Implementation Note

事件处理器订阅 `ON_MESSAGE_RECEIVED` 事件，在消息处理流程早期注册跳过 VLM 识别。这样当 `MessageConverter` 调用 `MediaManager.recognize_media()` 时，会检测到 `skip_vlm` 标志并保留原始图片数据。

### Architecture

**Default Flow (Plugin Disabled):**
```
Image → MediaManager → VLM Transcription → Text Description → LLM
```

**Omni-Modal Flow (Plugin Enabled):**
```
Image → MediaManager → base64 preserved → Image Object → Multi-modal LLM
```

### Key Discovery

The existing infrastructure already supports bypassing VLM:
1. `MediaManager._skip_vlm_stream_ids` - Built-in bypass mechanism
2. `_payloads_to_openai_messages()` already converts `Image` objects to OpenAI-compatible `image_url` format
3. Message content with images is preserved in `Message.extra` for downstream use

## Configuration

### Enable Plugin
Add to `config/core.toml`:
```toml
[bot]
plugins = ["omni_vision_plugin"]
```

### Enable Omni-Vision
Edit `config/plugins/omni_vision_plugin/config.toml`:
```toml
[settings]
enable_omni_vision = true
```

### Model Requirement
Ensure the main model in `config/model.toml` supports multi-modal input (e.g., GPT-4o, Gemini, etc.)

## Testing Recommendations

1. Start bot with plugin enabled
2. Send an image message
3. Check logs for "跳过 VLM 识别" message
4. Verify LLM receives image data (check request inspector)

**Status:**
- Plugin import tested successfully (`Import OK`)
- Python syntax validated (`Syntax OK`)
- Ready for integration testing

## Notes

- Plugin follows existing Neo-MoFox architecture patterns
- Config file placed in subdirectory like other plugins (per user instruction)
- Default setting is `false` (VLM transcription enabled) for backward compatibility
- Token usage may increase with direct image processing

## Files Modified
- `plugins/omni_vision_plugin/manifest.json` - Fixed manifest format to match project convention
- `plugins/omni_vision_plugin/config.py` - Fixed config class: inherit from BaseConfig, use ClassVar, explicit section fields

## Lessons Learned / 经验总结

### Problem 1: Plugin Not Loading

**Issue:** Plugin was not appearing in the loaded plugins list (8 plugins loaded, omni_vision_plugin not among them).

**Root Cause:** The `manifest.json` file had two critical issues:

1. **Wrong field name:** Used `"plugins"` instead of `"dependencies"`
   - ❌ `"plugins": { "event_handlers": [...] }`
   - ✅ `"dependencies": { "plugins": [], "components": [] }`

2. **Missing required field:** Did not include `"entry_point"` field
   - The `load_manifest()` function in `src/core/components/loader.py` checks for required fields including `"entry_point"` (line 291)
   - Other plugins like `intent_plugin` and `diary_plugin` all have `"entry_point": "plugin.py"`

**Solution:** Updated manifest.json to follow the same structure as other working plugins.

### Problem 2: Config Loading Failed (Always Default Values)

**Issue:** Plugin loaded but config showed `enable_omni_vision = False` even though config.toml had `true`.

**Root Cause:** The `config.py` file had THREE critical issues:

1. **Wrong base class:** Inherited from `ConfigBase` instead of `BaseConfig`
   - `ConfigBase` does NOT have the `load_for_plugin()` method
   - The plugin manager calls `config_class.load_for_plugin()` which was not available
   - **Fix:** `from src.core.components.base.config import BaseConfig` and `class OmniVisionConfig(BaseConfig):`

2. **Missing ClassVar annotation:** `config_name` and `config_description` were not annotated with `ClassVar[str]`, causing Pydantic to treat them as model fields

3. **Missing explicit section field:** The `settings` section was not declared as an explicit field with `Field(default_factory=...)`

**Solution:** Fixed config.py structure:
```python
from typing import ClassVar
from src.core.components.base.config import BaseConfig, SectionBase, config_section, Field

class OmniVisionConfig(BaseConfig):  # Must inherit BaseConfig, not ConfigBase!
    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "全模态视觉插件配置"

    @config_section("settings")
    class SettingsSection(SectionBase):
        enable_omni_vision: bool = Field(default=False, description="...")

    # Must explicitly declare section field
    settings: SettingsSection = Field(default_factory=SettingsSection)
```

### How to Avoid This in Future

1. **Always copy manifest.json structure from working plugins** - Use `intent_plugin` or `diary_plugin` as reference
2. **Always copy config.py structure from working plugins** - Use `diary_plugin/config.py` as reference
3. **Required fields checklist for manifest.json:**
   - [x] `name` - Plugin identifier
   - [x] `version` - Version string
   - [x] `description` - Human-readable description
   - [x] `author` - Author name
   - [x] `dependencies` - NOT `plugins`! Contains `plugins` and `components` lists
   - [x] `include` - List of components with `component_type`, `component_name`, `dependencies`, `enabled`
   - [x] `entry_point` - Python file path (usually `"plugin.py"`)
   - [x] `min_core_version` - Minimum core version required
   - [x] `python_dependencies` - List of pip packages (can be empty)
   - [x] `dependencies_required` - Boolean flag
4. **Required fields checklist for config.py:**
   - [x] Import from `src.core.components.base.config` (not `src.kernel.config`)
   - [x] Inherit `BaseConfig` (not `ConfigBase`)
   - [x] Import `ClassVar` from `typing`
   - [x] Annotate `config_name: ClassVar[str] = "..."`
   - [x] Annotate `config_description: ClassVar[str] = "..."`
   - [x] Define config sections using `@config_section` decorator
   - [x] Explicitly declare each section as a field: `section_name: SectionClass = Field(default_factory=SectionClass)`

### Quick Validation Commands

```bash
# Test manifest loading
python -c "from src.core.components.loader import load_manifest; import asyncio; asyncio.run(load_manifest('plugins/your_plugin'))"

# Test config loading
python -c "from plugins.your_plugin.config import YourConfig; c = YourConfig.load_for_plugin('your_plugin'); print(c.settings)"

# Test plugin import and registration
python -c "from plugins.your_plugin import plugin; from src.core.components.loader import get_plugin_class; print('Registered:', get_plugin_class('your_plugin') is not None)"
```

### Key Takeaways

- **Follow existing patterns** - Neo-MoFox has strict conventions; copying working examples is the safest approach
- **Manifest validation is strict** - Missing or wrong fields cause silent failures during plugin discovery
- **Config class must inherit BaseConfig** - `ConfigBase` lacks `load_for_plugin()` method needed by plugin manager
- **Use ClassVar for class attributes** - Prevents Pydantic from treating them as model fields
- **Explicitly declare section fields** - Required for TOML parsing to work correctly
- **Test config loading** - If plugin loads but config is always default values, check:
  1. Is it inheriting from `BaseConfig`?
  2. Does it have `ClassVar` annotations?
  3. Are section fields explicitly declared?

## Related Systems
- `src/core/managers/media_manager.py` - VLM bypass registration
- `src/kernel/llm/payload/content.py` - Image class definition
- `src/kernel/llm/model_client/openai_client.py` - Multi-modal message formatting
