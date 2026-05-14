# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Neo-MoFox is a refactored chatbot framework with a strict three-layer architecture:

- **kernel** - Basic capabilities layer providing platform-independent technical infrastructure (database, vector DB, scheduler, event bus, LLM, config, logger, concurrency, storage)
- **core** - Domain/mental layer implementing memory, conversation, and behavior using kernel capabilities (components, managers, prompt, transport, models)
- **app** - Application layer assembling kernel and core into a runnable Bot system with plugin extensions

See [MoFox 重构指导总览.md](MoFox 重构指导总览.md) for complete architecture documentation.

## Unified Consciousness Design

`life_chatter` is the single main consciousness of the project. Do not model
private chats, QQ groups, live rooms, games, terminals, or future channels as
separate minds or separate long-lived LLM sessions. Different streams are only
event sources, wake-up queues, and reply targets. The subject that perceives,
remembers, decides, and speaks is one shared `life_chatter` runtime.

Current implementation anchors:

- `plugins/life_engine/core/chatter.py` shares one global runtime through
  `_GLOBAL_RUNTIME`, `_GLOBAL_RUNTIME_LOCK`, and
  `LIFE_CHATTER_GLOBAL_CURSOR_KEY`.
- The global runtime lock is mandatory. Multiple stream loops may wake
  `life_chatter`, but only one source may advance the shared LLM payload chain
  at a time.
- The `life_chatter` system prompt must stay stream-agnostic. Platform-specific
  or scene-specific instructions belong in the current turn's user prompt or
  transient runtime context, not in the persistent system prompt.
- `life_engine` is the subconscious/runtime substrate. It observes events,
  maintains state, records memory, and supplies incremental context. It should
  not bypass the chatter/action layer to perform user-facing expression.

### Adding New Information Channels

When adding a new channel such as a live room, Minecraft, another game, browser
activity, screen state, or device telemetry:

1. Normalize incoming data into the unified life event model first.
   Use channel/source metadata (`channel`, `source`, `event_type`,
   `stream_id`, `reply_target`, `priority`, `salience`, `metadata`) instead of
   inventing a new memory/session model.
2. If the event is something the consciousness genuinely experienced
   (user text, danmaku, game chat, meaningful game state, tool result, operator
   result), record it as part of the unified event timeline. Do not hide real
   experiences only in transient context.
3. Use transient context only for derived or replaceable state: current screen
   snapshot, current game HUD, latest inventory summary, connection status,
   repeated operator instructions, response-format manuals, and other data that
   should not accumulate forever in the LLM payload chain.
4. High-frequency channels must be summarized, rate-limited, prioritized, or
   stored as current state before reaching `life_chatter`. Never dump every
   tick, frame, log line, or full game state into persistent chat history.
5. A channel bridge may create a `ChatStream`/`Message` when the data is
   conversation-like, but that stream still does not own a separate identity or
   private memory. It only tells the main consciousness where the event came
   from and where a reply should go.
6. Reply routing must preserve the original target. If the triggering event
   came from a QQ private chat, reply there; if it came from live chat, reply
   through the live/TTS path; if it came from a game operator bridge, return the
   structured decision to that bridge. Do not send through a generic adapter
   unless the platform target is known.

### Chat History vs Transient Runtime Context

`<chat_history>` is durable conversational history for the unified
consciousness. In unified mode it may merge visible messages from multiple
streams in chronological order, with stream labels when needed. It should
contain real external/user-facing dialogue and meaningful first-class events.

`<transient_life_context>` is a temporary attention/state block appended before
an LLM call and stripped afterward. It is for the latest runtime state, not for
durable memory. It must not become a second hidden chat history.

Keep these boundaries:

- Visible user/assistant messages, live danmaku, game chat, and meaningful
  external events may naturally accumulate in the unified history/event stream.
- Inner monologue, proactive triggers, follow-up triggers, tool boilerplate,
  operator manuals, repeated response schemas, and high-volume telemetry should
  not be inserted as normal chat history.
- If a channel needs both long-term experience and current-state awareness,
  split them: record the meaningful event durably, and expose the latest
  volatile state through transient context.

### Game and Tool Boundaries

For game integrations, keep concrete operation tools out of the main
consciousness unless they are genuinely part of expression. Prefer a separate
operator/worker agent for low-level controls. The operator reports state and
asks `life_chatter` for high-level decisions; `life_chatter` responds with the
decision; the operator executes it and reports the result back as an event.

For user-facing actions, expose tools narrowly to `life_chatter` through
`chatter_allow` and adapter capability checks. File sending, TTS/live speech,
message sending, image generation, and platform actions should always preserve
the active stream/reply target.

### Required Review Checklist

Before merging a new channel or life-related feature, check:

- Does it reuse the unified `life_chatter` runtime instead of creating a second
  long-lived consciousness/session?
- Are meaningful external events recorded once, with source metadata?
- Are repeated instructions and volatile state kept transient or summarized?
- Is the reply target explicit and adapter routing testable?
- Can high-frequency input apply backpressure, salience filtering, or
  summarization?
- Are tests or manual verification included for event ingestion, context
  rendering, transient stripping, and reply routing?

## Development Commands

### Dependency Management
The project uses `uv` for dependency management (Python >= 3.11 required).

```bash
# Add a new dependency
uv add <package_name>

# Install ruff for linting
uv tool install ruff
```

### Testing
```bash
# Run all tests
pytest

# Run a specific test file
pytest test/path/to/test_file.py

# Run with coverage
pytest --cov=src
```

Test coverage must reach 100% for all code in `src/`.

### Code Quality
```bash
# Run ruff for linting
ruff check src/

# Run ruff with auto-fix
ruff check --fix src/
```

## Architecture Highlights

### Kernel Layer (`src/kernel/`)

Provides low-level technical capabilities with minimal business logic:

- **db/** - Database abstraction with SQLAlchemy engines, CRUD operations, and query builder
- **vector_db/** - Vector database interface (currently ChromaDB-based)
- **scheduler/** - Unified task scheduler supporting time-based and custom triggers
- **event/** - Minimal Pub/Sub event bus
- **llm/** - Multi-vendor LLM interface with standardized payloads and response handling
- **config/** - Type-safe configuration system using Pydantic and TOML files
- **logger/** - Unified logging with color support and metadata tracking
- **concurrency/** - Async task management with TaskGroup and WatchDog monitoring
- **storage/** - Simple JSON-based local persistence

### Core Layer (`src/core/`)

Contains plugin components and their managers:

**Component Types** (in `components/base/`):
- Action - "Active" responses triggered by LLM tool calling (e.g., "send message")
- Tool - "Query" functions for LLM (e.g., calculator, translator)
- Adapter - Platform communication bridge following mofox-wire standard
- Chatter - Bot's intelligence core defining conversation logic
- Command - Command handlers (e.g., `/help`, `/mute`) with routing tree
- Collection - Nested groups of Actions/Tools for LLM discovery
- Config - Plugin configuration with hot-reload support
- EventHandler - Event subscriber for system/plugin events
- Service - Exposed functionality for inter-plugin communication
- Router - FastAPI HTTP route definitions
- Plugin - Root component containing all other plugin components

**Key Managers** (in `components/managers/`):
- action_manager - Schema generation, activation filtering, execution routing
- chatter_manager - Chatter lifecycle and LLMUsable filtering
- tool_manager - MCP adaptation, tool history, execution tracking
- plugin_manager - Plugin loading (folder/zip/.mfp) and lifecycle

### App Layer (`src/app/`)

- plugin_system/ - Plugin base classes and API exports
- built_in/ - Built-in plugins
- runtime/ - Bot runtime (bot.py)
- main.py - Application entry point

## Code Standards

From [代码规范.md](代码规范.md):

1. **PEP 8** style guide compliance
2. **Type annotations** required for all function parameters and return values
3. **Docstrings** required for all functions, classes, and file headers
4. **100% test coverage** for all `src/` code
5. No fallback mechanism abuse - ensure code robustness
6. No AI-generated commit messages without human review
7. Strict human review for all AI-generated code

## Component Signature Format

Components are identified by signatures in format: `plugin_name:component_type:component_name`

Example: `"my_plugin:action:send_emoji"`

## LLM Request/Response Pattern

The LLM module uses a chainable pattern:

```python
from src.kernel.llm import LLMRequest, LLMPayload, ROLE, Text, Tool

llm_request = LLMRequest(model_set, "my_request")
llm_request.add_payload(LLMPayload(ROLE.USER, Text("Hello")))

# Supports both streaming and non-streaming via unified interface
llm_response = await llm_request.send()
# OR: async for chunk in llm_request.send(): ...

# Response can chain back into requests
llm_response.add_payload(LLMPayload(ROLE.USER, Text("Follow up")))
result = await llm_response.send()
```

## Task Management

Use `task_manager` instead of `asyncio.create_task()` for all async operations:

```python
from src.kernel.concurrency import get_task_manager

tm = get_task_manager()

# Basic task
tm.create_task(func(), name="my_task")

# TaskGroup for scoped tasks
async with tm.group(name="group_name", timeout=30, cancel_on_error=True) as tg:
    tg.create_task(func1())
    tg.create_task(func2())
```

## Config System Pattern

Define configs using `ConfigBase` and `SectionBase`:

```python
from src.kernel.config import ConfigBase, SectionBase, config_section, Field

class MyConfig(ConfigBase):
    @config_section("general")
    class GeneralSection(SectionBase):
        enabled: bool = Field(default=True, description="Enable feature")

my_config = MyConfig.load("config/my_config.toml")
```

## Database Operations

```python
from src.kernel.db import CRUDBase, QueryBuilder

# CRUD operations
crud = CRUDBase(MyModel)
result = await crud.get_by(id=123)

# Query builder
result = await QueryBuilder(MyModel).filter(field="value").first()
```
