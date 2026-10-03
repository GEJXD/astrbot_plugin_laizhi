from __future__ import annotations

import asyncio
import inspect
import uuid
from pathlib import Path
from typing import Any

try:
    from .media import MediaManager, MediaReference
    from .storage import Storage, TagRecord, TagSummary, normalize_tag
except ImportError:  # pragma: no cover - allows direct module tests
    from media import MediaManager, MediaReference
    from storage import Storage, TagRecord, TagSummary, normalize_tag

try:
    from astrbot.api.web import error_response, file_response, json_response, request
except ImportError:  # pragma: no cover - AstrBot supplies these at runtime
    request = None

    def json_response(payload: Any) -> Any:
        return payload

    def error_response(message: str, status_code: int = 400) -> tuple[dict[str, Any], int]:
        return {"status": "error", "message": message}, status_code

    def file_response(*_: Any, **__: Any) -> Any:
        raise RuntimeError("当前 AstrBot 版本不支持 WebUI 文件下载")


PLUGIN_NAME = "astrbot_plugin_laizhi"


class TagWebUI:
    """Web APIs used by the plugin's AstrBot Dashboard page."""

    def __init__(
        self,
        storage: Storage,
        media: MediaManager,
        *,
        gc_orphan: bool = True,
    ) -> None:
        self.storage = storage
        self.media = media
        self.gc_orphan = bool(gc_orphan)

    def register(self, context: Any) -> bool:
        """Register the page APIs and return whether registration succeeded."""

        register = getattr(context, "register_web_api", None)
        if not callable(register):
            return False

        routes = (
            (f"/{PLUGIN_NAME}/tags", self.list_tags, ["GET"], "List 来只 tags"),
            (f"/{PLUGIN_NAME}/tags", self.create_tag, ["POST"], "Create a 来只 tag"),
            (f"/{PLUGIN_NAME}/tags/<tag_id>", self.get_tag, ["GET"], "Get a 来只 tag"),
            (
                f"/{PLUGIN_NAME}/tags/<tag_id>",
                self.update_tag,
                ["POST"],
                "Rename a 来只 tag",
            ),
            (
                f"/{PLUGIN_NAME}/tags/<tag_id>",
                self.delete_tag,
                ["DELETE"],
                "Delete a 来只 tag",
            ),
            (
                f"/{PLUGIN_NAME}/tags/<tag_id>/delete",
                self.delete_tag,
                ["POST"],
                "Delete a 来只 tag from the page",
            ),
            (
                f"/{PLUGIN_NAME}/tags/<tag_id>/files",
                self.list_files,
                ["GET"],
                "List files in a 来只 tag",
            ),
            (
                f"/{PLUGIN_NAME}/tags/<tag_id>/files",
                self.upload_file,
                ["POST"],
                "Upload a file to a 来只 tag",
            ),
            (
                f"/{PLUGIN_NAME}/tags/<tag_id>/files/<file_id>/detach",
                self.detach_file,
                ["POST"],
                "Remove a file from a 来只 tag",
            ),
            (
                f"/{PLUGIN_NAME}/files/<file_id>",
                self.download_file,
                ["GET"],
                "Download a 来只 file",
            ),
        )
        for route, handler, methods, description in routes:
            register(route, handler, methods, description)
        return True

    async def list_tags(self):
        query = str(request.query.get("q") or "").strip()  # type: ignore[union-attr]
        try:
            limit = _bounded_int(
                request.query.get("limit"),  # type: ignore[union-attr]
                200,
                1,
                1000,
            )
            offset = _bounded_int(
                request.query.get("offset"),  # type: ignore[union-attr]
                0,
                0,
                1_000_000,
            )
        except ValueError:
            return error_response("分页参数无效", status_code=400)

        summaries = await asyncio.to_thread(self.storage.list_tag_summaries)
        if query:
            folded = query.casefold()
            summaries = [
                item
                for item in summaries
                if folded in item.tag.name.casefold()
                or folded in item.effective_tag.name.casefold()
            ]
        total = len(summaries)
        visible = summaries[offset : offset + limit]
        return json_response(
            {
                "tags": [_tag_summary_payload(item) for item in visible],
                "total": total,
                "offset": offset,
                "limit": limit,
            },
        )

    async def create_tag(self):
        payload = await _json_payload()
        name = normalize_tag(payload.get("name"))
        if name is None:
            return error_response("标签名不能为空，且不能包含空格或路径符号", status_code=400)

        existing = await asyncio.to_thread(self.storage.get_tag, name)
        tag = await asyncio.to_thread(
            self.storage.get_or_create_tag,
            name,
            _request_username(),
        )
        return json_response(
            {
                "created": existing is None,
                "tag": _tag_payload(tag, tag, await _tag_count(self.storage, tag.id)),
            },
        )

    async def get_tag(self, tag_id: str):
        parsed_id = _parse_id(tag_id, "标签")
        if parsed_id is None:
            return error_response("标签 id 无效", status_code=400)
        tag = await asyncio.to_thread(self.storage.get_tag_by_id, parsed_id)
        if tag is None:
            return error_response("标签不存在", status_code=404)
        effective = await asyncio.to_thread(
            self.storage.resolve_tag_id,
            tag.id,
        )
        effective_tag = (
            await asyncio.to_thread(self.storage.get_tag_by_id, effective)
            if effective is not None
            else tag
        )
        return json_response(
            {
                "tag": _tag_payload(
                    tag,
                    effective_tag or tag,
                    await _tag_count(self.storage, tag.id),
                ),
            },
        )

    async def update_tag(self, tag_id: str):
        parsed_id = _parse_id(tag_id, "标签")
        if parsed_id is None:
            return error_response("标签 id 无效", status_code=400)
        payload = await _json_payload()
        name = normalize_tag(payload.get("name"))
        if name is None:
            return error_response("标签名不能为空，且不能包含空格或路径符号", status_code=400)
        try:
            tag = await asyncio.to_thread(self.storage.rename_tag, parsed_id, name)
        except ValueError as exc:
            status = 404 if str(exc) == "标签不存在" else 409
            return error_response(str(exc), status_code=status)
        effective_id = await asyncio.to_thread(self.storage.resolve_tag_id, tag.id)
        effective = (
            await asyncio.to_thread(self.storage.get_tag_by_id, effective_id)
            if effective_id is not None
            else tag
        )
        return json_response(
            {
                "tag": _tag_payload(
                    tag,
                    effective or tag,
                    await _tag_count(self.storage, tag.id),
                ),
            },
        )

    async def delete_tag(self, tag_id: str):
        parsed_id = _parse_id(tag_id, "标签")
        if parsed_id is None:
            return error_response("标签 id 无效", status_code=400)
        try:
            result = await asyncio.to_thread(self.storage.delete_tag, parsed_id)
        except ValueError as exc:
            status = 404 if str(exc) == "标签不存在" else 409
            return error_response(str(exc), status_code=status)
        return json_response(
            {
                "deleted": True,
                "tag": _tag_payload(result.tag, result.tag, 0),
                "detached": result.detached,
                "orphaned": result.orphaned,
            },
        )

    async def list_files(self, tag_id: str):
        parsed_id = _parse_id(tag_id, "标签")
        if parsed_id is None:
            return error_response("标签 id 无效", status_code=400)
        tag = await asyncio.to_thread(self.storage.get_tag_by_id, parsed_id)
        if tag is None:
            return error_response("标签不存在", status_code=404)
        try:
            page = _bounded_int(
                request.query.get("page"),  # type: ignore[union-attr]
                1,
                1,
                1_000_000,
            )
            limit = _bounded_int(
                request.query.get("limit"),  # type: ignore[union-attr]
                50,
                1,
                200,
            )
        except ValueError:
            return error_response("分页参数无效", status_code=400)
        query = str(request.query.get("q") or "").strip()  # type: ignore[union-attr]
        rows, total = await asyncio.to_thread(
            self.storage.list_files_for_tag,
            parsed_id,
            limit=limit,
            offset=(page - 1) * limit,
            query=query,
        )
        return json_response(
            {
                "tag": _tag_payload(
                    tag,
                    await _effective_tag(self.storage, tag),
                    await _tag_count(self.storage, tag.id),
                ),
                "files": [
                    _file_payload(record, added_by, added_at)
                    for record, added_by, added_at in rows
                ],
                "total": total,
                "page": page,
                "limit": limit,
            },
        )

    async def upload_file(self, tag_id: str):
        parsed_id = _parse_id(tag_id, "标签")
        if parsed_id is None:
            return error_response("标签 id 无效", status_code=400)
        tag = await asyncio.to_thread(self.storage.get_tag_by_id, parsed_id)
        effective_id = (
            await asyncio.to_thread(self.storage.resolve_tag_id, parsed_id)
            if tag is not None
            else None
        )
        if tag is None or effective_id is None:
            return error_response("标签不存在", status_code=404)

        files = await request.files()  # type: ignore[union-attr]
        upload = files.get("file") if files is not None else None
        if upload is None:
            return error_response("请选择要上传的文件", status_code=400)
        content_length = getattr(upload, "content_length", None)
        try:
            if content_length is not None and int(content_length) > self.media.max_size_bytes:
                return error_response(
                    f"文件超过 {self.media.max_size_bytes // (1024 * 1024)} MB 上限",
                    status_code=413,
                )
        except (TypeError, ValueError):
            pass

        filename = str(getattr(upload, "filename", "") or "upload.bin")
        upload_path = self.storage.tmp_dir / f"web_upload_{uuid.uuid4().hex}.tmp"
        media = None
        try:
            saved = upload.save(str(upload_path))
            if inspect.isawaitable(saved):
                await saved
            media = await self.media.fetch(
                MediaReference(str(upload_path), name=Path(filename).name),
            )
            record = await asyncio.to_thread(
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
            media = None
            status = await asyncio.to_thread(
                self.storage.attach,
                record.id,
                effective_id,
                added_by=_request_username(),
            )
            return json_response(
                {
                    "status": status,
                    "file": _file_payload(
                        record,
                        _request_username(),
                        int(record.created_at),
                    ),
                },
            )
        except (ValueError, OSError) as exc:
            return error_response(str(exc), status_code=400)
        finally:
            MediaManager.cleanup(media)
            upload_path.unlink(missing_ok=True)

    async def detach_file(self, tag_id: str, file_id: str):
        parsed_tag_id = _parse_id(tag_id, "标签")
        parsed_file_id = _parse_id(file_id, "文件")
        if parsed_tag_id is None or parsed_file_id is None:
            return error_response("标签或文件 id 无效", status_code=400)
        tag = await asyncio.to_thread(self.storage.get_tag_by_id, parsed_tag_id)
        effective_id = (
            await asyncio.to_thread(self.storage.resolve_tag_id, parsed_tag_id)
            if tag is not None
            else None
        )
        if tag is None or effective_id is None:
            return error_response("标签不存在", status_code=404)
        detached = await asyncio.to_thread(
            self.storage.detach,
            parsed_file_id,
            effective_id,
        )
        if not detached:
            return error_response("文件不在该标签中", status_code=404)
        orphaned = False
        if self.gc_orphan:
            orphaned = await asyncio.to_thread(
                self.storage.gc_orphan_file,
                parsed_file_id,
            )
        return json_response({"detached": True, "orphaned": orphaned})

    async def download_file(self, file_id: str):
        parsed_id = _parse_id(file_id, "文件")
        if parsed_id is None:
            return error_response("文件 id 无效", status_code=400)
        record = await asyncio.to_thread(self.storage.get_file, parsed_id)
        if record is None:
            return error_response("文件不存在", status_code=404)
        try:
            record.absolute_path.relative_to(self.storage.blobs_dir.resolve())
        except ValueError:
            return error_response("文件路径无效", status_code=404)
        if not record.absolute_path.is_file():
            return error_response("文件不存在", status_code=404)
        filename = f"laizhi_{record.hash[:12]}.{record.ext}"
        return file_response(
            record.absolute_path,
            filename=filename,
            content_type=record.mime or "application/octet-stream",
        )


async def _json_payload() -> dict[str, Any]:
    if request is None:
        return {}
    payload = await request.json(default={})
    return payload if isinstance(payload, dict) else {}


def _parse_id(raw: object, label: str) -> int | None:
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _bounded_int(raw: object, default: int, minimum: int, maximum: int) -> int:
    if raw in (None, ""):
        return default
    try:
        value = int(str(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError from exc
    return max(minimum, min(maximum, value))


def _request_username() -> str:
    value = getattr(request, "username", None) if request is not None else None
    return str(value).strip() if value and str(value).strip() else "webui"


async def _tag_count(storage: Storage, tag_id: int) -> int:
    return await asyncio.to_thread(storage.count_for_tag, tag_id)


async def _effective_tag(storage: Storage, tag: TagRecord) -> TagRecord:
    effective_id = await asyncio.to_thread(storage.resolve_tag_id, tag.id)
    if effective_id is None:
        return tag
    effective = await asyncio.to_thread(storage.get_tag_by_id, effective_id)
    return effective or tag


def _tag_payload(tag: TagRecord, effective: TagRecord, count: int) -> dict[str, Any]:
    return {
        "id": tag.id,
        "name": tag.name,
        "alias_of": tag.alias_of,
        "effective_id": effective.id,
        "effective_name": effective.name,
        "is_alias": tag.alias_of is not None,
        "created_by": tag.created_by,
        "created_at": tag.created_at,
        "count": count,
    }


def _tag_summary_payload(summary: TagSummary) -> dict[str, Any]:
    return _tag_payload(summary.tag, summary.effective_tag, summary.count)


def _file_payload(record: Any, added_by: str | None, added_at: int) -> dict[str, Any]:
    return {
        "id": record.id,
        "hash": record.hash,
        "kind": record.kind,
        "ext": record.ext,
        "mime": record.mime,
        "size": record.size,
        "width": record.width,
        "height": record.height,
        "created_at": record.created_at,
        "added_by": added_by,
        "added_at": added_at,
        "download_endpoint": f"files/{record.id}",
    }


__all__ = ["TagWebUI"]
