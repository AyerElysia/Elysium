"""Manual, owned lifecycle for the complete livestream data plane."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.kernel.concurrency import get_task_manager
from src.kernel.concurrency.task_info import TaskInfo
from src.kernel.logger import get_logger

from .consciousness import LivestreamConsciousnessManager
from .director import DirectorSettings, LifeChatterDeliberator, LivestreamDirector
from .domain import HealthSnapshot, PerformancePlan, PlatformEvent
from .ledger import LivestreamLedger
from .memory_bridge import LivestreamMemoryBridge, RunningLifeEventPublisher
from .performance import (
    AudioArtifactStore,
    HttpTTSClient,
    PerformanceRuntime,
    PerformanceSettings,
)
from .platform.factory import create_platform_adapter
from .stage import StageHub, StageUnavailableError

logger = get_logger("livestream.runtime", display="直播运行时")


class LivestreamRuntime:
    """Own every livestream resource; never starts outside an operator action."""

    def __init__(
        self,
        config: Any,
        stage: StageHub,
        *,
        adapter_factory: Callable[[Any], Any] = create_platform_adapter,
        tts_factory: Callable[..., Any] = HttpTTSClient,
        deliberator_factory: Callable[..., Any] = LifeChatterDeliberator,
        consciousness_factory: Callable[..., Any] = LivestreamConsciousnessManager,
        publisher_factory: Callable[..., Any] = RunningLifeEventPublisher,
    ) -> None:
        self.config = config
        self.stage = stage
        self._adapter_factory = adapter_factory
        self._tts_factory = tts_factory
        self._deliberator_factory = deliberator_factory
        self._consciousness_factory = consciousness_factory
        self._publisher_factory = publisher_factory
        self._state = "stopped"
        self._running = False
        self._lock = asyncio.Lock()
        self._session_id: str | None = None
        self._ledger: LivestreamLedger | None = None
        self._adapter: Any = None
        self._tts: Any = None
        self._director: LivestreamDirector | None = None
        self._performance: PerformanceRuntime | None = None
        self._memory: LivestreamMemoryBridge | None = None
        self._consciousness: Any = None
        self._tasks: list[TaskInfo] = []
        self._director_wakeup = asyncio.Event()
        self._performance_wakeup = asyncio.Event()
        self._memory_wakeup = asyncio.Event()
        self._presence_wakeup = asyncio.Event()
        self._errors: dict[str, str] = {}
        self._last_decision_at: float | None = None
        self._last_playback_at: float | None = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def start(self) -> str:
        """Start or resume only after an authenticated manual request."""

        async with self._lock:
            if self._state == "running" and self._session_id:
                return self._session_id
            if self._state not in {"stopped", "failed"}:
                raise RuntimeError(f"livestream transition already in progress: {self._state}")
            if self._state == "failed" and self._has_owned_resources():
                raise RuntimeError(
                    "livestream still owns resources from a failed shutdown; stop again"
                )
            if self.stage.primary_client_id is None:
                raise StageUnavailableError(
                    "connect an OBS/browser stage before starting livestream"
                )
            self._state = "starting"
            self._errors.clear()
            self._last_decision_at = None
            self._last_playback_at = None
            try:
                await self._start_components()
            except BaseException:
                self._running = False
                cleanup_errors = await self._shutdown_components(
                    reason="startup failure",
                    record_stop=False,
                )
                if cleanup_errors:
                    self._errors["startup_cleanup"] = "; ".join(cleanup_errors)
                self._state = "failed"
                raise
            self._state = "running"
            assert self._session_id is not None
            return self._session_id

    async def stop(self, *, reason: str = "manual stop") -> None:
        """Stop idempotently with bounded cancellation and explicit failure."""

        async with self._lock:
            if self._state == "stopped":
                return
            self._state = "stopping"
            self._running = False
            errors = await self._shutdown_components(reason=reason, record_stop=True)
            self._state = "failed" if errors else "stopped"
            if errors:
                raise RuntimeError("livestream shutdown failed: " + "; ".join(errors))

    async def manual_say(self, text: str) -> str:
        """Append an authenticated operator speech request to the same outbox."""

        normalized = text.strip()
        if not normalized:
            raise ValueError("manual speech text must not be empty")
        if len(normalized) > self.config.server.operator_text_max_chars:
            raise ValueError("manual speech text exceeds configured limit")
        ledger, session_id = self._require_running()
        request_id = uuid4().hex
        utterance_id = uuid4().hex[:24]
        await ledger.append(
            record_id=f"operator-say:{request_id}",
            session_id=session_id,
            kind="operator.say.requested",
            source="livestream.operator",
            payload={"request_id": request_id, "text": normalized},
            correlation_id=utterance_id,
        )
        plan = PerformancePlan(
            should_speak=True,
            reason="Authenticated operator requested an explicit stage utterance.",
            speech_text=normalized,
        )
        await ledger.append(
            record_id=f"performance-plan:{utterance_id}",
            session_id=session_id,
            kind="performance.planned",
            source="livestream.operator",
            payload={
                "utterance_id": utterance_id,
                "decision_id": f"operator:{request_id}",
                "plan": plan.model_dump(mode="json"),
            },
            correlation_id=utterance_id,
            causation_id=f"operator-say:{request_id}",
        )
        self._performance_wakeup.set()
        return utterance_id

    async def interrupt(self, *, reason: str = "operator interrupt") -> bool:
        performance = self._performance
        utterance_id = performance.current_utterance_id if performance else None
        if not utterance_id or not performance.current_interruptible:
            return False
        await self.stage.interrupt(utterance_id, reason)
        return True

    async def health(self) -> HealthSnapshot:
        ledger = self._ledger
        session_id = self._session_id
        event_backlog = 0
        performance_backlog = 0
        if ledger is not None and session_id is not None:
            director_cursor = await ledger.get_cursor(
                session_id, "livestream.director.v1"
            )
            performance_cursor = await ledger.get_cursor(
                session_id, "livestream.performance.v1"
            )
            event_backlog = await ledger.count_after(
                director_cursor,
                session_id=session_id,
                kind="platform.event",
            )
            performance_backlog = await ledger.count_after(
                performance_cursor,
                session_id=session_id,
                kind="performance.planned",
            )
        platform_health = getattr(self._adapter, "health", None)
        last_platform_event_at = (
            getattr(platform_health, "last_event_at", None) if platform_health else None
        )
        degraded = list(self._errors.values())
        platform_error = getattr(platform_health, "last_error", "") if platform_health else ""
        if platform_error:
            degraded.append(platform_error)
        return HealthSnapshot(
            status=self._state,
            session_id=session_id,
            platform_connected=bool(
                getattr(platform_health, "connected", False)
            ),
            stage_clients=self.stage.client_count,
            primary_stage_connected=self.stage.primary_client_id is not None,
            event_backlog=event_backlog,
            performance_backlog=performance_backlog,
            current_utterance_id=(
                self._performance.current_utterance_id if self._performance else None
            ),
            last_platform_event_at=last_platform_event_at,
            last_decision_at=self._last_decision_at,
            last_playback_completed_at=self._last_playback_at,
            degraded_reasons=degraded,
        )

    async def _start_components(self) -> None:
        ledger = LivestreamLedger(Path(self.config.storage.ledger_path))
        await ledger.start()
        self._ledger = ledger
        self._session_id = await self._choose_session_id(ledger)
        session_id = self._session_id

        consciousness = self._consciousness_factory(self.config, session_id)
        await consciousness.activate()
        self._consciousness = consciousness

        deliberator = self._deliberator_factory(
            room_id=self.config.platform.room_id,
            platform=self.config.platform.platform_type,
            model_task=self.config.director.model_task,
            timeout_seconds=self.config.director.timeout_seconds,
            consciousness=consciousness,
        )
        ensure_available = getattr(deliberator, "ensure_available", None)
        if callable(ensure_available):
            ensure_available()
        self._director = LivestreamDirector(
            ledger,
            deliberator,
            session_id=session_id,
            settings=DirectorSettings(batch_limit=self.config.director.batch_limit),
        )

        self._tts = self._tts_factory(
            self.config.tts.tts_endpoint,
            speed=self.config.tts.speed,
            volume=self.config.tts.volume,
            timeout_seconds=self.config.tts.timeout_seconds,
            retry_count=self.config.tts.retry_count,
            max_audio_bytes=self.config.tts.max_audio_bytes,
        )
        await self._tts.start()
        self._performance = PerformanceRuntime(
            ledger,
            self._tts,
            self.stage,
            AudioArtifactStore(self.config.storage.audio_artifact_path),
            session_id=session_id,
            settings=PerformanceSettings(
                sentence_delimiters=self.config.tts.sentence_delimiters.replace("\\n", "\n"),
                max_chunk_chars=self.config.tts.max_sentence_length,
                playback_timeout_seconds=self.config.tts.playback_timeout_seconds,
            ),
        )
        publisher = self._publisher_factory()
        require_service = getattr(publisher, "require_service", None)
        if callable(require_service):
            require_service()
        self._memory = LivestreamMemoryBridge(
            ledger,
            publisher,
            session_id=session_id,
        )

        self._running = True
        manager = get_task_manager()
        self._tasks = [
            manager.create_task(
                self._director_loop(),
                name=f"livestream-director-{session_id}",
                daemon=True,
            ),
            manager.create_task(
                self._performance_loop(),
                name=f"livestream-performance-{session_id}",
                daemon=True,
            ),
            manager.create_task(
                self._memory_loop(),
                name=f"livestream-memory-{session_id}",
                daemon=True,
            ),
            manager.create_task(
                self._presence_loop(),
                name=f"livestream-presence-{session_id}",
                daemon=True,
            ),
        ]
        self._adapter = self._adapter_factory(self.config)
        self._adapter.on_event(self._on_platform_event)
        await self._adapter.connect()
        await consciousness.report_state("B站直播间已连接，导演与舞台闭环正在运行")

    async def _choose_session_id(self, ledger: LivestreamLedger) -> str:
        latest = await ledger.get_latest_record("session.started")
        if latest is not None:
            stopped = await ledger.get_record(f"session-stopped:{latest.session_id}")
            if stopped is None:
                await ledger.append(
                    record_id=f"session-resumed:{latest.session_id}:{uuid4().hex}",
                    session_id=latest.session_id,
                    kind="session.resumed",
                    source="livestream.runtime",
                    payload={"reason": "manual crash recovery"},
                )
                return latest.session_id
        session_id = uuid4().hex
        await ledger.append(
            record_id=f"session-started:{session_id}",
            session_id=session_id,
            kind="session.started",
            source="livestream.runtime",
            payload={
                "platform": self.config.platform.platform_type,
                "room_id": self.config.platform.room_id,
                "start_mode": "manual",
            },
        )
        return session_id

    async def _on_platform_event(self, event: PlatformEvent) -> None:
        ledger, session_id = self._require_running()
        appended = await ledger.append_platform_event(session_id, event)
        if not appended.inserted:
            return
        self._director_wakeup.set()
        self._memory_wakeup.set()
        try:
            await self.stage.send_control(
                "audience.event",
                {
                    "event_id": event.event_id,
                    "kind": event.kind,
                    "user_name": event.user_name,
                    "content": event.content,
                },
            )
            self._errors.pop("stage", None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._errors["stage"] = (
                "stage output failed; raw events remain durable: "
                f"{type(exc).__name__}: {exc}"
            )

    async def _director_loop(self) -> None:
        assert self._director is not None
        await self._consumer_loop(
            "director",
            self._director.run_once,
            self._director_wakeup,
            self._on_director_progress,
        )

    async def _performance_loop(self) -> None:
        assert self._performance is not None
        await self._consumer_loop(
            "performance",
            self._performance.run_once,
            self._performance_wakeup,
            self._on_performance_progress,
        )

    async def _memory_loop(self) -> None:
        assert self._memory is not None
        await self._consumer_loop(
            "memory",
            self._memory.run_once,
            self._memory_wakeup,
            None,
        )

    async def _presence_loop(self) -> None:
        """Renew the active presence lease without creating autonomous startup."""

        interval = float(self.config.server.presence_lease_seconds) / 3.0
        while self._running:
            self._presence_wakeup.clear()
            try:
                await asyncio.wait_for(
                    self._presence_wakeup.wait(),
                    timeout=interval,
                )
            except TimeoutError:
                pass
            if not self._running:
                return
            consciousness = self._consciousness
            renew = getattr(consciousness, "renew", None)
            if not callable(renew):
                self._errors["presence"] = (
                    "livestream consciousness cannot renew its presence lease"
                )
                continue
            try:
                await renew()
                self._errors.pop("presence", None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._errors["presence"] = (
                    f"presence: {type(exc).__name__}: {exc}"
                )
                logger.error(  # noqa: G201
                    "直播意识实例续租失败",
                    exc_info=True,
                )

    async def _consumer_loop(
        self,
        name: str,
        run_once: Callable[[], Any],
        wakeup: asyncio.Event,
        on_progress: Callable[[Any], Awaitable[None]] | None,
    ) -> None:
        error_delay = 1.0
        while self._running:
            wakeup.clear()
            try:
                result = await run_once()
                self._errors.pop(name, None)
                error_delay = 1.0
                if result:
                    if on_progress is not None:
                        await on_progress(result)
                    continue
                try:
                    await asyncio.wait_for(wakeup.wait(), timeout=0.5)
                except TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            # Each consumer is a plugin boundary. Preserve its durable cursor,
            # expose the error, and retry instead of losing the managed task.
            except Exception as exc:
                self._errors[name] = f"{name}: {type(exc).__name__}: {exc}"
                logger.error(f"直播{name}循环失败", exc_info=True)  # noqa: G201
                await asyncio.sleep(error_delay)
                error_delay = min(
                    float(self.config.director.retry_max_seconds),
                    error_delay * 2,
                )

    async def _on_director_progress(self, result: Any) -> None:
        self._last_decision_at = time.time()
        performance = self._performance
        plan = getattr(result, "plan", None)
        if (
            plan is not None
            and bool(getattr(plan, "interrupt_current", False))
            and performance is not None
            and performance.current_utterance_id
            and performance.current_interruptible
        ):
            try:
                await self.stage.interrupt(
                    performance.current_utterance_id,
                    "livestream director chose to interrupt current performance",
                )
                self._errors.pop("stage", None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._errors["stage"] = (
                    "director interruption could not reach stage: "
                    f"{type(exc).__name__}: {exc}"
                )
        self._performance_wakeup.set()

    async def _on_performance_progress(self, result: Any) -> None:
        if result == "performance.completed":
            self._last_playback_at = time.time()
        self._memory_wakeup.set()

    async def _shutdown_components(
        self,
        *,
        reason: str,
        record_stop: bool,
    ) -> list[str]:
        errors: list[str] = []
        shutdown_timeout = float(self.config.server.shutdown_timeout_seconds)
        self._running = False
        for wakeup in (
            self._director_wakeup,
            self._performance_wakeup,
            self._memory_wakeup,
            self._presence_wakeup,
        ):
            wakeup.set()
        if self._adapter is not None:
            try:
                await asyncio.wait_for(
                    self._adapter.disconnect(),
                    timeout=shutdown_timeout,
                )
            except TimeoutError:
                errors.append("platform: shutdown timed out")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"platform: {exc}")
            else:
                self._adapter = None

        tasks = list(self._tasks)
        for info in tasks:
            if info.task is None or info.task.done():
                continue
            info.cancel()
        remaining_tasks: list[TaskInfo] = []
        for info in tasks:
            if info.task is None:
                continue
            try:
                await asyncio.wait_for(info.task, timeout=shutdown_timeout)
            except asyncio.CancelledError:
                pass
            except TimeoutError:
                errors.append(f"task timeout: {info.name}")
                remaining_tasks.append(info)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"task {info.name}: {exc}")
        self._tasks = remaining_tasks

        if self._tts is not None:
            try:
                await asyncio.wait_for(
                    self._tts.stop(),
                    timeout=shutdown_timeout,
                )
            except TimeoutError:
                errors.append("tts: shutdown timed out")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"tts: {exc}")
            else:
                self._tts = None
        if self._consciousness is not None:
            try:
                await asyncio.wait_for(
                    self._consciousness.suspend(reason=reason),
                    timeout=shutdown_timeout,
                )
            except TimeoutError:
                errors.append("consciousness: shutdown timed out")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"consciousness: {exc}")
            else:
                self._consciousness = None
        if record_stop and self._ledger is not None and self._session_id is not None:
            try:
                stop_record_id = f"session-stopped:{self._session_id}"
                if await self._ledger.get_record(stop_record_id) is None:
                    await asyncio.wait_for(
                        self._ledger.append(
                            record_id=stop_record_id,
                            session_id=self._session_id,
                            kind="session.stopped",
                            source="livestream.runtime",
                            payload={"reason": reason},
                        ),
                        timeout=shutdown_timeout,
                    )
            except TimeoutError:
                errors.append("session stop record: write timed out")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"session stop record: {exc}")
        if self._ledger is not None:
            try:
                await asyncio.wait_for(
                    self._ledger.stop(),
                    timeout=shutdown_timeout,
                )
            except TimeoutError:
                errors.append("ledger: shutdown timed out")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"ledger: {exc}")
            else:
                self._ledger = None
        if not self._tasks:
            self._director = None
            self._performance = None
            self._memory = None
        if not self._has_owned_resources():
            self._session_id = None
        return errors

    def _has_owned_resources(self) -> bool:
        return any(
            (
                self._ledger is not None,
                self._adapter is not None,
                self._tts is not None,
                self._consciousness is not None,
                bool(self._tasks),
            )
        )

    def _require_running(self) -> tuple[LivestreamLedger, str]:
        if not self._running or self._ledger is None or self._session_id is None:
            raise RuntimeError("livestream is not running")
        return self._ledger, self._session_id
