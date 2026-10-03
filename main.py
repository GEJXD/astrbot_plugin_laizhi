from __future__ import annotations

import asyncio
import inspect
from datetime import datetime
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .media import (
    MediaError,
    MediaManager,
    MediaSourceError,
    MediaTooLarge,
    UnsupportedMedia,
    extract_media_references,
)
from .permission import allowed, is_admin
from .recall import RecallManager
from .storage import Storage, normalize_tag
from .webui import TagWebUI

PLUGIN_NAME = "astrbot_plugin_laizhi"
DEFAULT_MAX_SIZE_MB = 10
DEFAULT_RECALL_SECONDS = 120
DEFAULT_MAX_OUTSTANDING = 3
DEFAULT_COOLDOWN_SECONDS = 3
DEFAULT_DOWNLOAD_TIMEOUT = 30


class LaizhiPlugin(Star):
    """收集群友逆天素材，按标签归档并随机发送。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config: Any = config or {}

        # StarTools keeps persistent data outside the plugin source directory,
        # so updating the plugin does not erase the collected media.
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.storage = Storage(
            self.data_dir / "laizhi.db",
            self.data_dir,
        )
        self.media = MediaManager(
            self.storage.tmp_dir,
            max_size_mb=_config_int(
                self.config.get("max_size_mb"),
                DEFAULT_MAX_SIZE_MB,
                minimum=1,
                maximum=100,
            ),
            allow_arbitrary_file=_config_bool(
                self.config.get("allow_arbitrary_file"),
                False,
            ),
            download_timeout=_config_int(
                self.config.get("download_timeout"),
                DEFAULT_DOWNLOAD_TIMEOUT,
                minimum=1,
                maximum=600,
            ),
        )
        self.recall = RecallManager(
            self.storage,
            recall_after_seconds=_config_int(
                self.config.get("recall_after_seconds"),
                DEFAULT_RECALL_SECONDS,
                minimum=0,
                maximum=7 * 24 * 60 * 60,
            ),
            max_outstanding=_config_int(
                self.config.get("max_outstanding"),
                DEFAULT_MAX_OUTSTANDING,
                minimum=1,
                maximum=100,
            ),
            cooldown_seconds=_config_int(
                self.config.get("cooldown_seconds"),
                DEFAULT_COOLDOWN_SECONDS,
                minimum=0,
                maximum=24 * 60 * 60,
            ),
            notify_on_throttle=_config_bool(
                self.config.get("notify_on_throttle"),
                False,
            ),
        )
        self.webui = TagWebUI(
            self.storage,
            self.media,
            gc_orphan=_config_bool(self.config.get("gc_orphan"), True),
        )
        self.webui.register(context)
        self._closed = False

    async def initialize(self) -> None:
        """Prepare temporary storage and restore delayed recalls."""

        await asyncio.to_thread(self.storage.clear_tmp)
        await self.media.initialize()
        await self.recall.initialize()
        await self._bind_existing_bot()
        logger.info(
            "[LAIZHI] 插件已初始化，数据目录：%s",
            self.data_dir,
        )

    async def _bind_existing_bot(self) -> None:
        """Use an already-loaded aiocqhttp client for restart compensation."""

        getter = getattr(self.context, "get_platform", None)
        if not callable(getter):
            return
        try:
            platform = getter("aiocqhttp")
            if platform is None:
                return
            client_getter = getattr(platform, "get_client", None)
            if not callable(client_getter):
                return
            client = client_getter()
            if inspect.isawaitable(client):
                client = await client
            await self.recall.bind_bot(client)
        except Exception as exc:
            logger.debug("[LAIZHI] 启动时绑定 aiocqhttp bot 失败：%s", exc)

    @filter.command("添加")
    async def cmd_add(self, event: AstrMessageEvent, tag: str = ""):
        """Reply to a media message and add it to a tag."""

        if not self._allowed(event, "add"):
            return
        tag_name = normalize_tag(tag)
        if tag_name is None:
            event.stop_event()
            yield event.plain_result(
                "用法：添加 <标签名>（标签名不能包含空格或路径符号）"
            )
            return

        try:
            references = await extract_media_references(event)
        except Exception:
            logger.exception("[LAIZHI] 提取添加素材失败")
            event.stop_event()
            yield event.plain_result("添加失败：无法读取被回复的媒体消息。")
            return
        if not references:
            event.stop_event()
            yield event.plain_result(
                "请回复一条图片、GIF、视频或音频，再发送 添加 <标签名>。",
            )
            return

        media = None
        try:
            media = await self.media.fetch(references[0])
            file_record = await asyncio.to_thread(
                self.storage.store_file,
                media.temp_path,
                file_hash=media.file_hash,
                kind=media.kind,
                ext=media.ext,
                mime=media.mime,
                size=media.size,
                width=media.width,
                height=media.height,
            )
            # store_file either atomically moves or removes the temp path.
            media = None
            raw_tag = await asyncio.to_thread(
                self.storage.get_or_create_tag,
                tag_name,
                self._sender_id(event),
            )
            resolved_tag = await asyncio.to_thread(
                self.storage.resolve_tag,
                tag_name,
            )
            if resolved_tag is None:
                raise RuntimeError("标签别名关系无效")
            status = await asyncio.to_thread(
                self.storage.attach,
                file_record.id,
                resolved_tag.id,
                added_by=self._sender_id(event),
                group_id=self._group_id(event) or None,
            )
            count = await asyncio.to_thread(
                self.storage.count_for_tag,
                resolved_tag.id,
            )
            suffix = (
                f"（检测到 {len(references)} 个媒体，已取第 1 个）"
                if len(references) > 1
                else ""
            )
            if status == "duplicate":
                text = f"这份素材已经在「{raw_tag.name}」标签里了。当前共 {count} 个。{suffix}"
                logger.info(
                    "[LAIZHI] 重复添加 hash=%s tag=%s",
                    file_record.hash,
                    raw_tag.name,
                )
            else:
                text = f"已加入「{raw_tag.name}」，当前共 {count} 个。{suffix}"
                logger.info(
                    "[LAIZHI] 添加 hash=%s kind=%s tag=%s",
                    file_record.hash,
                    file_record.kind,
                    raw_tag.name,
                )
            event.stop_event()
            yield event.plain_result(text)
        except MediaTooLarge as exc:
            event.stop_event()
            yield event.plain_result(f"添加失败：{exc}。")
        except UnsupportedMedia as exc:
            event.stop_event()
            yield event.plain_result(f"添加失败：{exc}。")
        except MediaSourceError as exc:
            event.stop_event()
            yield event.plain_result(f"添加失败：{exc}。")
        except MediaError as exc:
            event.stop_event()
            yield event.plain_result(f"添加失败：{exc}。")
        except Exception:
            logger.exception("[LAIZHI] 添加素材失败")
            event.stop_event()
            yield event.plain_result("添加失败：发生了未预期的错误，已记录日志。")
        finally:
            MediaManager.cleanup(media)

    @filter.command("来只", alias={"来张", "来个"})
    async def cmd_lai(self, event: AstrMessageEvent, tag: str = ""):
        """Randomly send one file from a tag."""

        if not self._allowed(event, "lai"):
            return
        tag_name = normalize_tag(tag)
        if tag_name is None:
            event.stop_event()
            yield event.plain_result("用法：来只 <标签名>")
            return

        try:
            resolved_tag = await asyncio.to_thread(self.storage.resolve_tag, tag_name)
            if resolved_tag is None:
                event.stop_event()
                yield event.plain_result(
                    f"还没有「{tag_name}」这个标签，请先用 添加 {tag_name} 添加素材。",
                )
                return

            record = await self._pick_existing_file(resolved_tag.id)
            if record is None:
                event.stop_event()
                yield event.plain_result(f"「{tag_name}」里还没有可发送的素材。")
                return

            result = await self.recall.send_media(event, record)
            if result.throttled:
                if self.recall.notify_on_throttle:
                    outstanding = self.recall.outstanding(self._group_id(event))
                    event.stop_event()
                    yield event.plain_result(
                        f"来只太频繁了，请稍后再试（当前有 {outstanding} 张素材未撤回）。",
                    )
                else:
                    event.stop_event()
                return
            if result.fallback_component is not None:
                event.stop_event()
                yield event.chain_result([result.fallback_component])
                return

            logger.info(
                "[LAIZHI] 发送 tag=%s file=%s message_id=%s",
                tag_name,
                record.hash,
                result.message_id,
            )
            event.stop_event()
        except Exception:
            logger.exception("[LAIZHI] 来只素材失败")
            event.stop_event()
            yield event.plain_result("发送失败：发生了未预期的错误，已记录日志。")

    @filter.command("删除")
    async def cmd_delete(self, event: AstrMessageEvent, tag: str = ""):
        """Remove the replied media from one tag."""

        if not self._allowed(event, "del"):
            return
        tag_name = normalize_tag(tag)
        if tag_name is None:
            event.stop_event()
            yield event.plain_result("用法：删除 <标签名>（必须回复要删除的媒体）")
            return

        try:
            references = await extract_media_references(event, reply_only=True)
        except Exception:
            logger.exception("[LAIZHI] 提取删除素材失败")
            event.stop_event()
            yield event.plain_result("删除失败：无法读取被回复的媒体消息。")
            return
        if not references:
            event.stop_event()
            yield event.plain_result(
                "请回复要删除的图片、GIF、视频或音频，再发送 删除 <标签名>。"
            )
            return

        media = None
        try:
            media = await self.media.fetch(references[0])
            file_record = await asyncio.to_thread(
                self.storage.find_file_by_hash,
                media.file_hash,
            )
            if file_record is None:
                event.stop_event()
                yield event.plain_result("这份素材不在库里。")
                return
            resolved_tag = await asyncio.to_thread(
                self.storage.resolve_tag,
                tag_name,
            )
            if resolved_tag is None:
                event.stop_event()
                yield event.plain_result(f"还没有「{tag_name}」这个标签。")
                return
            relation_exists, added_by = await asyncio.to_thread(
                self.storage.get_file_tag_relation,
                file_record.id,
                resolved_tag.id,
            )
            if not relation_exists:
                event.stop_event()
                yield event.plain_result(f"这份素材不在「{tag_name}」里。")
                return
            admin = is_admin(event)
            sender_id = self._sender_id(event)
            if not admin and (not sender_id or added_by != sender_id):
                event.stop_event()
                yield event.plain_result(
                    "只有添加这份素材的人或管理员可以删除。",
                )
                return
            if admin:
                detached = await asyncio.to_thread(
                    self.storage.detach,
                    file_record.id,
                    resolved_tag.id,
                )
            else:
                detached = await asyncio.to_thread(
                    self.storage.detach_owned,
                    file_record.id,
                    resolved_tag.id,
                    sender_id,
                )
            if not detached:
                event.stop_event()
                yield event.plain_result("删除失败：素材关系刚刚发生变化，请重试。")
                return

            remaining = await asyncio.to_thread(
                self.storage.count_for_tag,
                resolved_tag.id,
            )
            other_tags = await asyncio.to_thread(
                self.storage.list_tag_file_names,
                file_record.id,
                resolved_tag.id,
            )
            if not other_tags and _config_bool(self.config.get("gc_orphan"), True):
                await asyncio.to_thread(
                    self.storage.gc_orphan_file,
                    file_record.id,
                )
            if other_tags:
                location = f"（仍在「{'、'.join(other_tags)}」中）"
            else:
                location = ""
            suffix = (
                f"（检测到 {len(references)} 个媒体，已取第 1 个）"
                if len(references) > 1
                else ""
            )
            event.stop_event()
            yield event.plain_result(
                f"已从「{tag_name}」移除，标签剩余 {remaining} 个。{location}{suffix}",
            )
        except (MediaTooLarge, UnsupportedMedia, MediaSourceError, MediaError) as exc:
            event.stop_event()
            yield event.plain_result(f"删除失败：{exc}。")
        except Exception:
            logger.exception("[LAIZHI] 删除素材失败")
            event.stop_event()
            yield event.plain_result("删除失败：发生了未预期的错误，已记录日志。")
        finally:
            MediaManager.cleanup(media)

    @filter.command("alias")
    async def cmd_alias(self, event: AstrMessageEvent, src: str = "", dst: str = ""):
        """Merge one tag into another while retaining the source as an alias."""

        if not self._allowed(event, "alias"):
            return
        source_name = normalize_tag(src)
        target_name = normalize_tag(dst)
        if source_name is None or target_name is None:
            event.stop_event()
            yield event.plain_result("用法：alias <源标签> <目标标签>")
            return
        if source_name.casefold() == target_name.casefold():
            event.stop_event()
            yield event.plain_result("源标签和目标标签不能相同。")
            return

        try:
            source = await asyncio.to_thread(self.storage.get_tag, source_name)
            if source is None:
                event.stop_event()
                yield event.plain_result(f"找不到源标签「{source_name}」。")
                return
            target = await asyncio.to_thread(
                self.storage.get_or_create_tag,
                target_name,
                self._sender_id(event),
            )
            result = await asyncio.to_thread(
                self.storage.merge_tag,
                source.id,
                target.id,
            )
            count = await asyncio.to_thread(
                self.storage.count_for_tag,
                result.target.id,
            )
            logger.info(
                "[LAIZHI] 合并标签 %s -> %s，新增 %d，重复 %d",
                result.source.name,
                result.target.name,
                result.migrated,
                result.duplicates,
            )
            event.stop_event()
            yield event.plain_result(
                f"已将「{result.source.name}」合并进「{result.target.name}」："
                f"新增 {result.migrated} 个，重复 {result.duplicates} 个，"
                f"目标标签当前共 {count} 个。「{result.source.name}」已保留为别名。",
            )
        except ValueError as exc:
            event.stop_event()
            yield event.plain_result(f"合并失败：{exc}。")
        except Exception:
            logger.exception("[LAIZHI] 合并标签失败")
            event.stop_event()
            yield event.plain_result("合并失败：发生了未预期的错误，已记录日志。")

    @filter.command("标签")
    async def cmd_tags(self, event: AstrMessageEvent, tag: str = ""):
        """List tags or show one tag's details."""

        if not self._allowed(event, "tags"):
            return
        try:
            if tag:
                tag_name = normalize_tag(tag)
                if tag_name is None:
                    event.stop_event()
                    yield event.plain_result("用法：标签 [标签名]")
                    return
                resolved = await asyncio.to_thread(self.storage.resolve_tag, tag_name)
                if resolved is None:
                    event.stop_event()
                    yield event.plain_result(f"还没有「{tag_name}」这个标签。")
                    return
                count = await asyncio.to_thread(self.storage.count_for_tag, resolved.id)
                recent = await asyncio.to_thread(
                    self.storage.recent_files,
                    resolved.id,
                    5,
                )
                if recent:
                    recent_text = "、".join(
                        f"{item.ext} ({datetime.fromtimestamp(added_at).strftime('%m-%d %H:%M')})"
                        for item, added_at in recent
                    )
                else:
                    recent_text = "暂无"
                event.stop_event()
                yield event.plain_result(
                    f"标签「{tag_name}」：{count} 个。最近添加：{recent_text}",
                )
                return

            summaries = await asyncio.to_thread(self.storage.list_tag_summaries)
            if not summaries:
                event.stop_event()
                yield event.plain_result(
                    "还没有任何标签。回复素材发送 添加 <标签名> 来创建。"
                )
                return
            lines: list[str] = []
            for summary in summaries[:100]:
                if summary.tag.alias_of is not None:
                    lines.append(
                        f"- {summary.tag.name}（别名 → {summary.effective_tag.name}）：{summary.count} 个",
                    )
                else:
                    lines.append(f"- {summary.tag.name}：{summary.count} 个")
            if len(summaries) > 100:
                lines.append(f"……还有 {len(summaries) - 100} 个标签未显示。")
            event.stop_event()
            yield event.plain_result("标签列表：\n" + "\n".join(lines))
        except Exception:
            logger.exception("[LAIZHI] 查看标签失败")
            event.stop_event()
            yield event.plain_result("查看标签失败：发生了未预期的错误，已记录日志。")

    async def _pick_existing_file(self, tag_id: int):
        """Pick a file and self-heal stale DB relations a few times."""

        for _ in range(3):
            record = await asyncio.to_thread(self.storage.random_file, tag_id)
            if record is None:
                return None
            if record.absolute_path.is_file():
                return record
            logger.warning(
                "[LAIZHI] 发现丢失素材，移除关系 file_id=%s path=%s",
                record.id,
                record.absolute_path,
            )
            await asyncio.to_thread(
                self.storage.detach_missing_file,
                record.id,
                tag_id,
            )
        return None

    def _allowed(self, event: AstrMessageEvent, command: str) -> bool:
        if allowed(event, self.config, command):
            return True
        logger.info(
            "[LAIZHI] 静默拒绝 command=%s sender=%s group=%s",
            command,
            self._sender_id(event),
            self._group_id(event),
        )
        event.stop_event()
        return False

    @staticmethod
    def _group_id(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_group_id", None)
        if callable(getter):
            try:
                return str(getter() or "").strip()
            except Exception:
                return ""
        return ""

    @staticmethod
    def _sender_id(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_sender_id", None)
        if callable(getter):
            try:
                return str(getter() or "").strip()
            except Exception:
                return ""
        return ""

    async def terminate(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.recall.shutdown()
        await self.media.close()
        await asyncio.to_thread(self.storage.close)
        logger.info("[LAIZHI] 插件已停止")


def _config_int(
    value: object,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        result = int(value) if value is not None else default
    except (TypeError, ValueError):
        result = default
    return max(minimum, min(maximum, result))


def _config_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return default


__all__ = ["LaizhiPlugin"]
