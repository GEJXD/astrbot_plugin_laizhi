from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import mimetypes
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

try:  # Dependencies are installed by requirements.txt in AstrBot.
    import aiohttp
except ImportError:  # pragma: no cover - makes non-runtime unit tests importable
    aiohttp = None  # type: ignore[assignment]

try:
    import filetype
except ImportError:  # pragma: no cover - fallback magic sniffing is retained
    filetype = None  # type: ignore[assignment]


IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "bmp"}
GIF_EXTENSIONS = {"gif"}
VIDEO_EXTENSIONS = {"mp4", "mov", "mkv", "webm"}
AUDIO_EXTENSIONS = {"mp3", "wav", "amr", "ogg", "m4a"}


class MediaError(ValueError):
    """Base class for user-facing media validation failures."""


class MediaTooLarge(MediaError):
    """Raised when a source exceeds the configured byte limit."""


class UnsupportedMedia(MediaError):
    """Raised when magic-byte sniffing does not produce an allowed kind."""


class MediaSourceError(MediaError):
    """Raised when a media component cannot be resolved to a readable source."""


@dataclass(frozen=True, slots=True)
class MediaReference:
    """A source extracted from an AstrBot message component."""

    source: str
    name: str | None = None


@dataclass(frozen=True, slots=True)
class DownloadedMedia:
    """A verified media file waiting to be moved into blob storage."""

    temp_path: Path
    file_hash: str
    kind: str
    ext: str
    mime: str | None
    size: int
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True, slots=True)
class _SniffedType:
    ext: str
    mime: str | None
    kind: str | None = None


class MediaManager:
    """Resolve, stream, sniff, hash, and stage media files."""

    def __init__(
        self,
        tmp_dir: Path | str,
        *,
        max_size_mb: int = 10,
        allow_arbitrary_file: bool = False,
        download_timeout: int = 30,
    ) -> None:
        self.tmp_dir = Path(tmp_dir).resolve()
        self.max_size_bytes = max(1, int(max_size_mb)) * 1024 * 1024
        self.allow_arbitrary_file = bool(allow_arbitrary_file)
        self.download_timeout = max(1, int(download_timeout))
        self._session: Any = None

    async def initialize(self) -> None:
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def fetch(self, reference: MediaReference) -> DownloadedMedia:
        """Stage a source into ``tmp/`` and return verified metadata."""

        source = str(reference.source).strip()
        if not source:
            raise MediaSourceError("媒体来源为空")
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        temp_path = self.tmp_dir / f"media_{uuid.uuid4().hex}.tmp"
        try:
            scheme = urlsplit(source).scheme.lower()
            if scheme in {"http", "https"}:
                await self._download_http(source, temp_path)
            elif scheme == "base64":
                await self._decode_base64(source[9:], temp_path)
            elif source.lower().startswith("data:"):
                await self._decode_data_uri(source, temp_path)
            else:
                local_path = _source_to_local_path(source)
                await asyncio.to_thread(
                    _copy_local_file,
                    local_path,
                    temp_path,
                    self.max_size_bytes,
                )

            sniffed = await asyncio.to_thread(self._sniff_file, temp_path, reference.name)
            file_hash = await asyncio.to_thread(_sha256_file, temp_path)
            size = temp_path.stat().st_size
            return DownloadedMedia(
                temp_path=temp_path,
                file_hash=file_hash,
                kind=sniffed.kind or _kind_for_extension(sniffed.ext),
                ext=sniffed.ext,
                mime=sniffed.mime,
                size=size,
            )
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

    async def _ensure_session(self) -> Any:
        if aiohttp is None:
            raise MediaSourceError("未安装 aiohttp，无法下载网络媒体")
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.download_timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _download_http(self, url: str, temp_path: Path) -> None:
        session = await self._ensure_session()
        try:
            async with session.get(url) as response:
                if response.status < 200 or response.status >= 300:
                    raise MediaSourceError(
                        f"下载媒体失败：HTTP {response.status}",
                    )
                content_length = response.content_length
                if content_length is not None and content_length > self.max_size_bytes:
                    raise MediaTooLarge(
                        f"媒体超过 {self.max_size_bytes // (1024 * 1024)} MB 上限",
                    )
                await _write_http_stream(
                    response.content,
                    temp_path,
                    self.max_size_bytes,
                )
        except asyncio.TimeoutError as exc:
            raise MediaSourceError("下载媒体超时") from exc
        except MediaError:
            raise
        except Exception as exc:
            raise MediaSourceError(f"下载媒体失败：{exc}") from exc

    async def _decode_base64(self, encoded: str, temp_path: Path) -> None:
        value = "".join(encoded.split())
        # A base64 payload is about 4/3 the decoded size. This early guard
        # prevents an oversized string from being expanded before validation.
        if len(value) > ((self.max_size_bytes + 2) * 4 // 3) + 16:
            raise MediaTooLarge(
                f"媒体超过 {self.max_size_bytes // (1024 * 1024)} MB 上限",
            )
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise MediaSourceError("Base64 媒体格式无效") from exc
        if len(decoded) > self.max_size_bytes:
            raise MediaTooLarge(
                f"媒体超过 {self.max_size_bytes // (1024 * 1024)} MB 上限",
            )
        await asyncio.to_thread(_write_bytes, decoded, temp_path)

    async def _decode_data_uri(self, source: str, temp_path: Path) -> None:
        header, separator, payload = source.partition(",")
        if not separator:
            raise MediaSourceError("data URI 格式无效")
        if ";base64" in header.lower():
            await self._decode_base64(payload, temp_path)
            return
        decoded = unquote(payload).encode("utf-8")
        if len(decoded) > self.max_size_bytes:
            raise MediaTooLarge(
                f"媒体超过 {self.max_size_bytes // (1024 * 1024)} MB 上限",
            )
        await asyncio.to_thread(_write_bytes, decoded, temp_path)

    def _sniff_file(self, path: Path, original_name: str | None) -> _SniffedType:
        with path.open("rb") as file_obj:
            head = file_obj.read(8192)
        if not head:
            raise UnsupportedMedia("媒体内容为空")

        detected: _SniffedType | None = None
        if filetype is not None:
            try:
                kind = filetype.guess(head)
            except Exception:
                kind = None
            if kind is not None:
                ext = str(getattr(kind, "extension", "") or "").lower().lstrip(".")
                mime = getattr(kind, "mime", None)
                if ext:
                    detected = _SniffedType(ext=ext, mime=mime)

        fallback = _fallback_sniff(head)
        if detected is None:
            detected = fallback
        elif fallback is not None and (
            detected.ext == "mp4"
            and fallback.ext == "m4a"
            or _kind_for_extension(detected.ext) == "file"
            and _kind_for_extension(fallback.ext) != "file"
        ):
            # Some magic databases label M4A as generic MP4 or otherwise
            # return a less specific extension than our fallback detector.
            # Preserve the more useful allowed-media kind.
            detected = fallback
        if detected is None:
            if not self.allow_arbitrary_file:
                raise UnsupportedMedia(
                    "无法识别媒体格式，仅支持图片、GIF、视频和音频",
                )
            detected = _SniffedType(
                ext=_extension_from_name(original_name) or "bin",
                mime=None,
                kind="file",
            )

        ext = detected.ext.lower().lstrip(".")
        kind = detected.kind or _kind_for_extension(ext)
        if kind == "file" and not self.allow_arbitrary_file:
            raise UnsupportedMedia(
                "不支持的媒体格式，仅支持图片、GIF、视频和音频",
            )
        if not ext:
            ext = _extension_from_name(original_name) or "bin"
        mime = detected.mime or mimetypes.guess_type(f"file.{ext}")[0]
        return _SniffedType(ext=ext, mime=mime, kind=kind)

    @staticmethod
    def cleanup(media: DownloadedMedia | None) -> None:
        if media is not None:
            media.temp_path.unlink(missing_ok=True)


def _copy_local_file(source: str, target: Path, max_bytes: int) -> None:
    path = Path(source).expanduser()
    if not path.is_file():
        raise MediaSourceError(f"找不到媒体文件：{path}")
    total = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("rb") as source_file, target.open("wb") as target_file:
            while True:
                chunk = source_file.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise MediaTooLarge(f"媒体超过 {max_bytes // (1024 * 1024)} MB 上限")
                target_file.write(chunk)
            target_file.flush()
            os.fsync(target_file.fileno())
    except Exception:
        target.unlink(missing_ok=True)
        raise


async def _write_http_stream(content: Any, target: Path, max_bytes: int) -> None:
    total = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("wb") as target_file:
            async for chunk in content.iter_chunked(64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise MediaTooLarge(f"媒体超过 {max_bytes // (1024 * 1024)} MB 上限")
                target_file.write(chunk)
            target_file.flush()
            os.fsync(target_file.fileno())
    except Exception:
        target.unlink(missing_ok=True)
        raise


def _write_bytes(data: bytes, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("wb") as target_file:
            target_file.write(data)
            target_file.flush()
            os.fsync(target_file.fileno())
    except Exception:
        target.unlink(missing_ok=True)
        raise


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_to_local_path(source: str) -> str:
    if source.lower().startswith("file://"):
        parsed = urlsplit(source)
        path = unquote(parsed.path)
        if parsed.netloc and parsed.netloc not in {"", "localhost"}:
            path = f"//{parsed.netloc}{path}"
        if os.name == "nt" and len(path) >= 3 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        return path
    return unquote(source)


def _kind_for_extension(ext: str) -> str:
    normalized = ext.lower().lstrip(".")
    if normalized in GIF_EXTENSIONS:
        return "gif"
    if normalized in IMAGE_EXTENSIONS:
        return "image"
    if normalized in VIDEO_EXTENSIONS:
        return "video"
    if normalized in AUDIO_EXTENSIONS:
        return "audio"
    return "file"


def _extension_from_name(name: str | None) -> str | None:
    if not name:
        return None
    suffix = Path(str(name).replace("\\", "/")).suffix.lower().lstrip(".")
    if suffix and suffix.isalnum() and len(suffix) <= 12:
        return suffix
    return None


def _fallback_sniff(head: bytes) -> _SniffedType | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return _SniffedType("png", "image/png")
    if head.startswith(b"\xff\xd8\xff"):
        return _SniffedType("jpg", "image/jpeg")
    if head.startswith((b"GIF87a", b"GIF89a")):
        return _SniffedType("gif", "image/gif")
    if head.startswith(b"BM"):
        return _SniffedType("bmp", "image/bmp")
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return _SniffedType("webp", "image/webp")
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12].lower()
        if brand in {b"qt  ", b"m4v ", b"m4a ", b"M4A ".lower()}:
            if brand in {b"m4a ", b"m4a ".lower()}:
                return _SniffedType("m4a", "audio/mp4")
            return _SniffedType("mov", "video/quicktime")
        return _SniffedType("mp4", "video/mp4")
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        if b"webm" in head[:128].lower():
            return _SniffedType("webm", "video/webm")
        return _SniffedType("mkv", "video/x-matroska")
    if head.startswith(b"ID3") or _looks_like_mp3_frame(head):
        return _SniffedType("mp3", "audio/mpeg")
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return _SniffedType("wav", "audio/wav")
    if head.startswith(b"OggS"):
        return _SniffedType("ogg", "audio/ogg")
    if head.startswith(b"#!AMR"):
        return _SniffedType("amr", "audio/amr")
    return None


def _looks_like_mp3_frame(head: bytes) -> bool:
    if len(head) < 2:
        return False
    for index in range(min(len(head) - 1, 128)):
        if head[index] == 0xFF and (head[index + 1] & 0xE0) == 0xE0:
            return True
    return False


def _field(component: Any, name: str) -> Any:
    if isinstance(component, dict):
        if name in component:
            return component[name]
        data = component.get("data")
        if isinstance(data, dict) and name in data:
            return data[name]
        return None
    try:
        return getattr(component, name, None)
    except Exception:
        return None


def _component_type(component: Any) -> str:
    value = _field(component, "type")
    if value is None:
        value = component.__class__.__name__ if component is not None else ""
    value = getattr(value, "value", value)
    return str(value).lower()


def _is_reply(component: Any) -> bool:
    return _component_type(component) in {"reply", "repl"} or (
        component.__class__.__name__.lower() == "reply" if component is not None else False
    )


def _is_media_component(component: Any) -> bool:
    component_type = _component_type(component)
    if component_type in {"image", "video", "record", "audio", "file"}:
        return True
    class_name = component.__class__.__name__.lower() if component is not None else ""
    return class_name in {"image", "video", "record", "file"}


def _source_from_component(component: Any) -> MediaReference | None:
    if not _is_media_component(component):
        return None
    candidates: list[Any] = []
    for field_name in ("url", "file_", "file", "path"):
        value = _field(component, field_name)
        if value is not None and value not in candidates:
            candidates.append(value)
    source: str | None = None
    for value in candidates:
        if isinstance(value, str) and value.strip():
            source = value.strip()
            if source.startswith(("http://", "https://", "file://", "base64://", "data:")):
                break
            if os.path.exists(source):
                break
    if not source:
        return None
    name_value = _field(component, "name") or _field(component, "filename")
    name = str(name_value).strip() if name_value else None
    return MediaReference(source=source, name=name)


def _collect_media(components: Any, *, skip_replies: bool = False) -> list[MediaReference]:
    if not isinstance(components, (list, tuple)):
        return []
    result: list[MediaReference] = []
    visited: set[int] = set()

    def visit(value: Any) -> None:
        if not isinstance(value, (list, tuple)):
            return
        for component in value:
            if component is None:
                continue
            if _is_reply(component):
                if skip_replies:
                    continue
                nested = _field(component, "chain")
                visit(nested)
                continue
            reference = _source_from_component(component)
            if reference is not None:
                result.append(reference)
                continue
            identifier = id(component)
            if identifier in visited:
                continue
            visited.add(identifier)
            for nested_name in ("chain", "content", "nodes"):
                nested = _field(component, nested_name)
                if isinstance(nested, (list, tuple)):
                    visit(nested)

    visit(components)
    return result


def _find_reply(components: Any) -> Any | None:
    if not isinstance(components, (list, tuple)):
        return None
    for component in components:
        if _is_reply(component):
            return component
    return None


def _event_chain(event: Any) -> list[Any]:
    getter = getattr(event, "get_messages", None)
    if callable(getter):
        try:
            chain = getter()
            if isinstance(chain, (list, tuple)):
                return list(chain)
        except Exception:
            pass
    message_obj = getattr(event, "message_obj", None)
    chain = getattr(message_obj, "message", None)
    if isinstance(chain, (list, tuple)):
        return list(chain)
    raw = getattr(message_obj, "raw_message", None)
    if isinstance(raw, dict) and isinstance(raw.get("message"), list):
        return raw["message"]
    return []


def _raw_reply_chain(response: Any) -> list[Any]:
    if not isinstance(response, dict):
        return []
    candidates: list[Any] = [response]
    data = response.get("data")
    if isinstance(data, dict):
        candidates.append(data)
    for candidate in candidates:
        message = candidate.get("message")
        if isinstance(message, list):
            return message
    return []


async def extract_media_references(
    event: Any,
    *,
    reply_only: bool = False,
) -> list[MediaReference]:
    """Extract media with reply-first priority and OneBot ``get_msg`` fallback."""

    chain = _event_chain(event)
    reply = _find_reply(chain)
    if reply is not None:
        reply_chain = _field(reply, "chain")
        references = _collect_media(reply_chain)
        if not references:
            reply_id = _field(reply, "id")
            bot = getattr(event, "bot", None)
            call_action = getattr(bot, "call_action", None)
            if reply_id is not None and callable(call_action):
                try:
                    response = await call_action("get_msg", message_id=str(reply_id))
                    references = _collect_media(_raw_reply_chain(response))
                except Exception:
                    references = []
        if references:
            return references
        if reply_only:
            return []
    elif reply_only:
        return []

    return _collect_media(chain, skip_replies=True)


__all__ = [
    "DownloadedMedia",
    "MediaError",
    "MediaManager",
    "MediaReference",
    "MediaSourceError",
    "MediaTooLarge",
    "UnsupportedMedia",
    "extract_media_references",
]
