from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

try:
    from .storage import FileRecord, PendingRecall, Storage
except ImportError:  # pragma: no cover - allows direct module tests
    from storage import FileRecord, PendingRecall, Storage

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover - direct unit-test fallback
    logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SendResult:
    """Outcome of sending one stored file."""

    direct_sent: bool = False
    message_id: str | None = None
    fallback_component: Any | None = None
    throttled: bool = False
    error: str | None = None


class RecallManager:
    """Own per-group send slots and the delayed delete lifecycle."""

    def __init__(
        self,
        storage: Storage,
        *,
        recall_after_seconds: int = 120,
        max_outstanding: int = 3,
        cooldown_seconds: int = 3,
        notify_on_throttle: bool = False,
    ) -> None:
        self.storage = storage
        self.recall_after_seconds = max(0, int(recall_after_seconds))
        self.max_outstanding = max(1, int(max_outstanding))
        self.cooldown_seconds = max(0, int(cooldown_seconds))
        self.notify_on_throttle = bool(notify_on_throttle)

        self._lock = asyncio.Lock()
        self._outstanding: dict[str, int] = defaultdict(int)
        self._cooldown_until: dict[str, float] = {}
        self._reservations: dict[str, tuple[str, bool]] = {}
        self._pending: dict[str, PendingRecall] = {}
        self._scheduled: dict[str, asyncio.Task[Any]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._bot: Any = None

    async def initialize(self) -> None:
        """Load persisted recalls; scheduling waits until a bot is available."""

        pending = await asyncio.to_thread(self.storage.list_pending_recalls)
        async with self._lock:
            for item in pending:
                if item.message_id in self._pending:
                    continue
                self._pending[item.message_id] = item
                group_id = item.group_id or item.session_id
                self._outstanding[group_id] += 1
        if pending:
            logger.info(
                "[LAIZHI] 已加载 %d 条待撤回记录，等待消息平台连接后补偿",
                len(pending),
            )

    async def bind_bot(self, bot: Any) -> None:
        """Bind the current aiocqhttp bot and schedule restored records."""

        if bot is None or not callable(getattr(bot, "call_action", None)):
            return
        async with self._lock:
            self._bot = bot
            for message_id, pending in self._pending.items():
                if message_id in self._scheduled:
                    continue
                self._schedule_locked(pending, bot)

    async def shutdown(self) -> None:
        """Cancel in-memory tasks without deleting DB rows.

        Keeping rows on cancellation lets a hot reload attempt compensation
        from the new plugin instance rather than silently losing a recall.
        """

        async with self._lock:
            tasks = list(self._tasks)
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._lock:
            self._tasks.clear()
            self._scheduled.clear()
            self._bot = None

    async def send_media(self, event: Any, record: FileRecord) -> SendResult:
        """Send directly through OneBot when possible, else return a fallback."""

        component = _build_component(record)
        group_id = _event_group_id(event)
        platform = _event_platform(event)
        bot = getattr(event, "bot", None)
        call_action = getattr(bot, "call_action", None)

        if (
            not group_id
            or platform != "aiocqhttp"
            or not callable(call_action)
            or not record.absolute_path.is_file()
        ):
            return SendResult(fallback_component=component)

        await self.bind_bot(bot)
        reservation = await self._reserve(group_id)
        if reservation is None:
            return SendResult(throttled=True)

        try:
            payload = {
                "type": _onebot_type(record.kind),
                "data": {"file": record.absolute_path.resolve().as_uri()},
            }
            action_kwargs: dict[str, Any] = {
                "group_id": _numeric_or_text(group_id),
                "message": [payload],
            }
            self_id = _event_self_id(event)
            if self_id:
                action_kwargs["self_id"] = _numeric_or_text(self_id)
            response = await call_action("send_group_msg", **action_kwargs)
            message_id = _extract_message_id(response)
            if not message_id:
                raise RuntimeError("协议端未返回 message_id")
            await self._mark_sent(
                reservation,
                message_id=message_id,
                session_id=group_id,
                group_id=group_id,
            )
            return SendResult(direct_sent=True, message_id=message_id)
        except asyncio.CancelledError:
            await self._release_reservation(reservation)
            raise
        except Exception as exc:
            await self._release_reservation(reservation)
            logger.warning("[LAIZHI] OneBot 发送失败，降级为框架发送：%s", exc)
            return SendResult(
                fallback_component=component,
                error=str(exc),
            )

    async def _reserve(self, group_id: str) -> str | None:
        now = time.monotonic()
        async with self._lock:
            for key, deadline in list(self._cooldown_until.items()):
                if deadline <= now:
                    self._cooldown_until.pop(key, None)
            deadline = self._cooldown_until.get(group_id)
            if deadline is not None and deadline > now:
                return None

            counted = self.recall_after_seconds > 0
            if counted and self._outstanding[group_id] >= self.max_outstanding:
                return None
            token = uuid.uuid4().hex
            self._reservations[token] = (group_id, counted)
            if counted:
                self._outstanding[group_id] += 1
            return token

    async def _release_reservation(self, token: str) -> None:
        async with self._lock:
            reservation = self._reservations.pop(token, None)
            if reservation is None:
                return
            group_id, counted = reservation
            if counted:
                self._outstanding[group_id] = max(
                    0,
                    self._outstanding[group_id] - 1,
                )

    async def _mark_sent(
        self,
        token: str,
        *,
        message_id: str,
        session_id: str,
        group_id: str,
    ) -> None:
        async with self._lock:
            reservation = self._reservations.pop(token, None)
            if reservation is None:
                raise RuntimeError("发送名额已失效")
            if self.cooldown_seconds > 0:
                self._cooldown_until[group_id] = (
                    time.monotonic() + self.cooldown_seconds
                )
            counted = reservation[1]

        if not counted:
            return

        recall_at = int(time.time()) + self.recall_after_seconds
        pending = PendingRecall(
            message_id=str(message_id),
            session_id=str(session_id),
            group_id=str(group_id),
            recall_at=recall_at,
        )
        try:
            await asyncio.to_thread(
                self.storage.add_pending_recall,
                pending.message_id,
                pending.session_id,
                pending.group_id,
                pending.recall_at,
            )
        except Exception:
            # The message is already sent. Release the in-memory slot rather
            # than leaving it permanently occupied if SQLite is unavailable.
            async with self._lock:
                self._outstanding[group_id] = max(
                    0,
                    self._outstanding[group_id] - 1,
                )
            raise

        async with self._lock:
            self._pending[pending.message_id] = pending
            bot = self._bot
            if bot is not None:
                self._schedule_locked(pending, bot)

    def _schedule_locked(self, pending: PendingRecall, bot: Any) -> None:
        if pending.message_id in self._scheduled:
            return
        task = asyncio.create_task(self._recall_one(pending, bot))
        self._scheduled[pending.message_id] = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _recall_one(self, pending: PendingRecall, bot: Any) -> None:
        cancelled = False
        try:
            delay = max(0, pending.recall_at - int(time.time()))
            if delay:
                await asyncio.sleep(delay)
            message_id: Any = _numeric_or_text(pending.message_id)
            await bot.call_action("delete_msg", message_id=message_id)
        except asyncio.CancelledError:
            cancelled = True
            return
        except Exception as exc:
            logger.warning(
                "[LAIZHI] 撤回消息 %s 失败：%s",
                pending.message_id,
                exc,
            )
        finally:
            if not cancelled:
                try:
                    await asyncio.to_thread(
                        self.storage.remove_pending_recall,
                        pending.message_id,
                    )
                except Exception as exc:
                    logger.warning("[LAIZHI] 清理待撤回记录失败：%s", exc)
                async with self._lock:
                    self._pending.pop(pending.message_id, None)
                    self._scheduled.pop(pending.message_id, None)
                    group_id = pending.group_id or pending.session_id
                    self._outstanding[group_id] = max(
                        0,
                        self._outstanding[group_id] - 1,
                    )

    def outstanding(self, group_id: str) -> int:
        """Return a snapshot useful for optional throttle notifications."""

        return self._outstanding.get(str(group_id), 0)


def _event_platform(event: Any) -> str:
    getter = getattr(event, "get_platform_name", None)
    if callable(getter):
        try:
            return str(getter() or "")
        except Exception:
            return ""
    return ""


def _event_group_id(event: Any) -> str:
    getter = getattr(event, "get_group_id", None)
    if callable(getter):
        try:
            return str(getter() or "").strip()
        except Exception:
            return ""
    return ""


def _event_self_id(event: Any) -> str:
    getter = getattr(event, "get_self_id", None)
    if callable(getter):
        try:
            return str(getter() or "").strip()
        except Exception:
            return ""
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    return str(raw.get("self_id") or "").strip() if isinstance(raw, dict) else ""


def _numeric_or_text(value: str) -> int | str:
    text = str(value)
    return int(text) if text.isdigit() else text


def _extract_message_id(response: Any) -> str | None:
    candidates = [response]
    if isinstance(response, dict):
        for key in ("data", "result"):
            value = response.get(key)
            if isinstance(value, dict):
                candidates.append(value)
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in ("message_id", "messageId"):
            value = candidate.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
    return None


def _onebot_type(kind: str) -> str:
    return {
        "image": "image",
        "gif": "image",
        "video": "video",
        "audio": "record",
        "file": "file",
    }.get(kind, "file")


def _build_component(record: FileRecord) -> Any:
    """Build a framework message component for non-OneBot fallbacks."""

    import astrbot.api.message_components as components

    path = str(record.absolute_path)
    if record.kind in {"image", "gif"}:
        return components.Image.fromFileSystem(path)
    if record.kind == "video":
        return components.Video.fromFileSystem(path)
    if record.kind == "audio":
        return components.Record.fromFileSystem(path)
    name = f"laizhi_{record.hash[:12]}.{record.ext}"
    return components.File(name=name, file=path)


__all__ = ["RecallManager", "SendResult"]
