from __future__ import annotations

import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FileRecord:
    """A content-addressed file stored by the plugin."""

    id: int
    hash: str
    rel_path: str
    kind: str
    ext: str
    mime: str | None
    size: int
    width: int | None
    height: int | None
    created_at: int
    absolute_path: Path


@dataclass(frozen=True, slots=True)
class TagRecord:
    """A tag row. ``alias_of`` is set when this name is an alias."""

    id: int
    name: str
    alias_of: int | None
    created_by: str | None
    created_at: int


@dataclass(frozen=True, slots=True)
class TagSummary:
    tag: TagRecord
    effective_tag: TagRecord
    count: int


@dataclass(frozen=True, slots=True)
class PendingRecall:
    message_id: str
    session_id: str
    group_id: str | None
    recall_at: int


@dataclass(frozen=True, slots=True)
class MergeResult:
    source: TagRecord
    target: TagRecord
    migrated: int
    duplicates: int


_TAG_INVALID_RE = re.compile(r"[\\/:*?\"<>|\s]")


def normalize_tag(raw: object) -> str | None:
    """Normalize and validate a user-provided tag name.

    Tags intentionally use exact matching. Unicode NFKC normalization makes
    visually equivalent full-width forms consistent, while the length and
    character checks keep names safe to display and use in command arguments.
    """

    if raw is None:
        return None
    name = unicodedata.normalize("NFKC", str(raw).strip())
    if not name or len(name) > 32:
        return None
    if name in {".", ".."} or _TAG_INVALID_RE.search(name):
        return None
    return name


class Storage:
    """SQLite storage and content-addressed blob management.

    SQLite operations are deliberately synchronous and protected by a
    re-entrant lock. Callers handling messages can use ``asyncio.to_thread``
    around the methods when the operation is not already on a worker thread.
    The database itself is kept independent of AstrBot message objects.
    """

    def __init__(self, db_path: Path | str, data_dir: Path | str) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.db_path = Path(db_path).resolve()
        self.blobs_dir = self.data_dir / "blobs"
        self.tmp_dir = self.data_dir / "tmp"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
            self._create_schema_locked()

    def _create_schema_locked(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS files (
                id         INTEGER PRIMARY KEY,
                hash       TEXT NOT NULL UNIQUE,
                rel_path   TEXT NOT NULL,
                kind       TEXT NOT NULL,
                ext        TEXT NOT NULL,
                mime       TEXT,
                size       INTEGER NOT NULL,
                width      INTEGER,
                height     INTEGER,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tags (
                id         INTEGER PRIMARY KEY,
                name       TEXT NOT NULL COLLATE NOCASE UNIQUE,
                alias_of   INTEGER REFERENCES tags(id) ON DELETE SET NULL,
                created_by TEXT,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS file_tags (
                file_id   INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                tag_id    INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                added_by  TEXT,
                group_id  TEXT,
                added_at  INTEGER NOT NULL,
                PRIMARY KEY (file_id, tag_id)
            );

            CREATE INDEX IF NOT EXISTS idx_ft_tag ON file_tags(tag_id);
            CREATE INDEX IF NOT EXISTS idx_ft_file ON file_tags(file_id);

            CREATE TABLE IF NOT EXISTS pending_recalls (
                message_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                group_id   TEXT,
                recall_at  INTEGER NOT NULL
            );
            """,
        )

    @staticmethod
    def _now() -> int:
        return int(time.time())

    def _row_to_file(self, row: sqlite3.Row) -> FileRecord:
        return FileRecord(
            id=int(row["id"]),
            hash=str(row["hash"]),
            rel_path=str(row["rel_path"]),
            kind=str(row["kind"]),
            ext=str(row["ext"]),
            mime=row["mime"],
            size=int(row["size"]),
            width=int(row["width"]) if row["width"] is not None else None,
            height=int(row["height"]) if row["height"] is not None else None,
            created_at=int(row["created_at"]),
            absolute_path=(self.data_dir / str(row["rel_path"])).resolve(),
        )

    @staticmethod
    def _row_to_tag(row: sqlite3.Row) -> TagRecord:
        return TagRecord(
            id=int(row["id"]),
            name=str(row["name"]),
            alias_of=int(row["alias_of"]) if row["alias_of"] is not None else None,
            created_by=row["created_by"],
            created_at=int(row["created_at"]),
        )

    def _tag_by_id_locked(self, tag_id: int) -> TagRecord | None:
        row = self._conn.execute(
            "SELECT id, name, alias_of, created_by, created_at FROM tags WHERE id = ?",
            (int(tag_id),),
        ).fetchone()
        return self._row_to_tag(row) if row is not None else None

    def _resolve_tag_id_locked(self, tag_id: int) -> int | None:
        """Resolve at most one alias hop and defend against corrupt cycles."""

        current = int(tag_id)
        visited: set[int] = set()
        for _ in range(8):
            if current in visited:
                return None
            visited.add(current)
            row = self._conn.execute(
                "SELECT alias_of FROM tags WHERE id = ?",
                (current,),
            ).fetchone()
            if row is None:
                return None
            alias_of = row["alias_of"]
            if alias_of is None:
                return current
            current = int(alias_of)
        return None

    def _effective_tag_locked(self, tag: TagRecord) -> TagRecord | None:
        effective_id = self._resolve_tag_id_locked(tag.id)
        if effective_id is None:
            return None
        return self._tag_by_id_locked(effective_id)

    def get_tag(self, name: str) -> TagRecord | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, name, alias_of, created_by, created_at
                FROM tags
                WHERE name = ? COLLATE NOCASE
                """,
                (name,),
            ).fetchone()
            return self._row_to_tag(row) if row is not None else None

    def get_or_create_tag(self, name: str, creator: str | None = None) -> TagRecord:
        with self._lock:
            now = self._now()
            self._conn.execute(
                """
                INSERT OR IGNORE INTO tags(name, created_by, created_at)
                VALUES (?, ?, ?)
                """,
                (name, creator, now),
            )
            row = self._conn.execute(
                """
                SELECT id, name, alias_of, created_by, created_at
                FROM tags
                WHERE name = ? COLLATE NOCASE
                """,
                (name,),
            ).fetchone()
            if row is None:  # pragma: no cover - defensive database failure
                raise RuntimeError(f"无法创建标签：{name}")
            return self._row_to_tag(row)

    def resolve_tag(self, name: str) -> TagRecord | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, name, alias_of, created_by, created_at
                FROM tags
                WHERE name = ? COLLATE NOCASE
                """,
                (name,),
            ).fetchone()
            if row is None:
                return None
            tag = self._row_to_tag(row)
            return self._effective_tag_locked(tag)

    def store_file(
        self,
        temp_path: Path | str,
        *,
        file_hash: str,
        kind: str,
        ext: str,
        mime: str | None,
        size: int,
        width: int | None = None,
        height: int | None = None,
    ) -> FileRecord:
        """Move a verified temporary file into its content-addressed location.

        The atomic filesystem move happens before the database transaction is
        committed. A crash can therefore leave an orphan blob, which
        ``gc_orphans`` can safely remove, but cannot leave a committed row
        pointing to a partially written file.
        """

        temp = Path(temp_path)
        if not temp.is_file():
            raise FileNotFoundError(f"临时媒体不存在：{temp}")
        normalized_hash = str(file_hash).lower()
        normalized_ext = str(ext).lower().lstrip(".") or "bin"

        with self._lock:
            existing_row = self._conn.execute(
                "SELECT * FROM files WHERE hash = ?",
                (normalized_hash,),
            ).fetchone()
            if existing_row is not None:
                existing = self._row_to_file(existing_row)
                existing.absolute_path.parent.mkdir(parents=True, exist_ok=True)
                if not existing.absolute_path.exists():
                    existing.absolute_path.parent.mkdir(parents=True, exist_ok=True)
                    temp.replace(existing.absolute_path)
                else:
                    temp.unlink(missing_ok=True)
                return existing

            rel_path = f"blobs/{normalized_hash[:2]}/{normalized_hash}.{normalized_ext}"
            target = self.data_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            temp.replace(target)

            try:
                cursor = self._conn.execute(
                    """
                    INSERT INTO files(
                        hash, rel_path, kind, ext, mime, size, width, height, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_hash,
                        rel_path,
                        kind,
                        normalized_ext,
                        mime,
                        int(size),
                        width,
                        height,
                        self._now(),
                    ),
                )
                row = self._conn.execute(
                    "SELECT * FROM files WHERE id = ?",
                    (cursor.lastrowid,),
                ).fetchone()
            except Exception:
                # The moved blob is intentionally left for orphan GC if the
                # transaction fails. Do not move it back across directories.
                raise
            if row is None:  # pragma: no cover - defensive database failure
                raise RuntimeError("文件记录写入失败")
            return self._row_to_file(row)

    def find_file_by_hash(self, file_hash: str) -> FileRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM files WHERE hash = ?",
                (str(file_hash).lower(),),
            ).fetchone()
            return self._row_to_file(row) if row is not None else None

    def attach(
        self,
        file_id: int,
        tag_id: int,
        *,
        added_by: str | None = None,
        group_id: str | None = None,
    ) -> str:
        """Attach a file to a tag, returning ``added`` or ``duplicate``."""

        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO file_tags(
                    file_id, tag_id, added_by, group_id, added_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (int(file_id), int(tag_id), added_by, group_id, self._now()),
            )
            return "added" if cursor.rowcount == 1 else "duplicate"

    def detach(self, file_id: int, tag_id: int) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM file_tags WHERE file_id = ? AND tag_id = ?",
                (int(file_id), int(tag_id)),
            )
            return cursor.rowcount == 1

    def detach_missing_file(self, file_id: int, tag_id: int) -> bool:
        """Remove a relation when a selected blob was found missing on disk."""

        return self.detach(file_id, tag_id)

    def count_for_tag(self, tag_id: int) -> int:
        with self._lock:
            effective_id = self._resolve_tag_id_locked(int(tag_id))
            if effective_id is None:
                return 0
            row = self._conn.execute(
                "SELECT COUNT(DISTINCT file_id) AS count FROM file_tags WHERE tag_id = ?",
                (effective_id,),
            ).fetchone()
            return int(row["count"] if row is not None else 0)

    def random_file(self, tag_id: int) -> FileRecord | None:
        """Pick one file without sorting the entire tag table."""

        with self._lock:
            effective_id = self._resolve_tag_id_locked(int(tag_id))
            if effective_id is None:
                return None
            count_row = self._conn.execute(
                "SELECT COUNT(DISTINCT file_id) AS count FROM file_tags WHERE tag_id = ?",
                (effective_id,),
            ).fetchone()
            count = int(count_row["count"] if count_row is not None else 0)
            if count <= 0:
                return None
            # Import locally so storage remains cheap to import and easy to test.
            import random

            offset = random.randrange(count)
            row = self._conn.execute(
                """
                SELECT f.*
                FROM files AS f
                JOIN file_tags AS ft ON ft.file_id = f.id
                WHERE ft.tag_id = ?
                GROUP BY f.id
                LIMIT 1 OFFSET ?
                """,
                (effective_id, offset),
            ).fetchone()
            return self._row_to_file(row) if row is not None else None

    def list_tag_file_names(self, file_id: int, exclude_tag_id: int | None = None) -> list[str]:
        with self._lock:
            query = """
                SELECT t.name
                FROM tags AS t
                JOIN file_tags AS ft ON ft.tag_id = t.id
                WHERE ft.file_id = ?
            """
            params: list[object] = [int(file_id)]
            if exclude_tag_id is not None:
                query += " AND t.id != ?"
                params.append(int(exclude_tag_id))
            query += " ORDER BY t.name COLLATE NOCASE"
            rows = self._conn.execute(query, params).fetchall()
            return [str(row["name"]) for row in rows]

    def gc_orphan_file(self, file_id: int) -> bool:
        """Delete one unreferenced file row and its physical blob."""

        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM files WHERE id = ?",
                (int(file_id),),
            ).fetchone()
            if row is None:
                return False
            ref = self._conn.execute(
                "SELECT 1 FROM file_tags WHERE file_id = ? LIMIT 1",
                (int(file_id),),
            ).fetchone()
            if ref is not None:
                return False
            path = self.data_dir / str(row["rel_path"])
            self._conn.execute("DELETE FROM files WHERE id = ?", (int(file_id),))
            path.unlink(missing_ok=True)
            return True

    def gc_orphans(self) -> int:
        """Remove unreferenced file rows and untracked blob files."""

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT f.id, f.rel_path
                FROM files AS f
                LEFT JOIN file_tags AS ft ON ft.file_id = f.id
                WHERE ft.file_id IS NULL
                """,
            ).fetchall()
            if rows:
                self._conn.executemany(
                    "DELETE FROM files WHERE id = ?",
                    [(int(row["id"]),) for row in rows],
                )
            known_paths = {
                str(row["rel_path"])
                for row in self._conn.execute("SELECT rel_path FROM files").fetchall()
            }
            removed = len(rows)
            for row in rows:
                (self.data_dir / str(row["rel_path"])).unlink(missing_ok=True)

            # A crash between os.replace() and the INSERT can leave a blob
            # with no database row. Remove only files below our blobs dir that
            # are not referenced by the remaining rows.
            for path in self.blobs_dir.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    relative = path.relative_to(self.data_dir).as_posix()
                except ValueError:
                    continue
                if relative not in known_paths:
                    path.unlink(missing_ok=True)
                    removed += 1
            for directory in sorted(
                (path for path in self.blobs_dir.rglob("*") if path.is_dir()),
                key=lambda item: len(item.parts),
                reverse=True,
            ):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            return removed

    def list_tag_summaries(self) -> list[TagSummary]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, name, alias_of, created_by, created_at
                FROM tags
                ORDER BY name COLLATE NOCASE
                """,
            ).fetchall()
            counts: dict[int, int] = {}
            summaries: list[TagSummary] = []
            for row in rows:
                tag = self._row_to_tag(row)
                effective = self._effective_tag_locked(tag)
                if effective is None:
                    # A corrupt alias should not make the list command fail.
                    effective = tag
                if effective.id not in counts:
                    count_row = self._conn.execute(
                        """
                        SELECT COUNT(DISTINCT file_id) AS count
                        FROM file_tags WHERE tag_id = ?
                        """,
                        (effective.id,),
                    ).fetchone()
                    counts[effective.id] = int(
                        count_row["count"] if count_row is not None else 0,
                    )
                summaries.append(
                    TagSummary(tag=tag, effective_tag=effective, count=counts[effective.id]),
                )
            return summaries

    def recent_files(self, tag_id: int, limit: int = 5) -> list[tuple[FileRecord, int]]:
        with self._lock:
            effective_id = self._resolve_tag_id_locked(int(tag_id))
            if effective_id is None:
                return []
            rows = self._conn.execute(
                """
                SELECT f.*, MAX(ft.added_at) AS relation_added_at
                FROM files AS f
                JOIN file_tags AS ft ON ft.file_id = f.id
                WHERE ft.tag_id = ?
                GROUP BY f.id
                ORDER BY relation_added_at DESC
                LIMIT ?
                """,
                (effective_id, max(1, int(limit))),
            ).fetchall()
            return [
                (self._row_to_file(row), int(row["relation_added_at"]))
                for row in rows
            ]

    def merge_tag(self, source_id: int, target_id: int) -> MergeResult:
        """Merge source relations into target and retain source as an alias."""

        with self._lock:
            source = self._tag_by_id_locked(int(source_id))
            target = self._tag_by_id_locked(int(target_id))
            if source is None or target is None:
                raise ValueError("源标签或目标标签不存在")
            resolved_target_id = self._resolve_tag_id_locked(target.id)
            if resolved_target_id is None:
                raise ValueError("目标标签别名关系无效")
            if resolved_target_id == source.id:
                raise ValueError("不能将标签合并到自身或其别名")
            resolved_target = self._tag_by_id_locked(resolved_target_id)
            if resolved_target is None:
                raise ValueError("目标标签不存在")

            self._conn.execute("BEGIN IMMEDIATE")
            try:
                source_count_row = self._conn.execute(
                    "SELECT COUNT(*) AS count FROM file_tags WHERE tag_id = ?",
                    (source.id,),
                ).fetchone()
                source_count = int(
                    source_count_row["count"] if source_count_row is not None else 0,
                )
                duplicate_row = self._conn.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM file_tags AS source_ft
                    WHERE source_ft.tag_id = ?
                      AND EXISTS (
                          SELECT 1 FROM file_tags AS target_ft
                          WHERE target_ft.file_id = source_ft.file_id
                            AND target_ft.tag_id = ?
                      )
                    """,
                    (source.id, resolved_target.id),
                ).fetchone()
                duplicates = int(
                    duplicate_row["count"] if duplicate_row is not None else 0,
                )
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO file_tags(
                        file_id, tag_id, added_by, group_id, added_at
                    )
                    SELECT file_id, ?, added_by, group_id, added_at
                    FROM file_tags WHERE tag_id = ?
                    """,
                    (resolved_target.id, source.id),
                )
                self._conn.execute(
                    "DELETE FROM file_tags WHERE tag_id = ?",
                    (source.id,),
                )
                # Keep existing aliases pointing directly at the new root;
                # otherwise merging a tag that already has aliases would
                # create a multi-hop alias chain.
                self._conn.execute(
                    "UPDATE tags SET alias_of = ? WHERE alias_of = ? AND id != ?",
                    (resolved_target.id, source.id, resolved_target.id),
                )
                self._conn.execute(
                    "UPDATE tags SET alias_of = ? WHERE id = ?",
                    (resolved_target.id, source.id),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

            return MergeResult(
                source=source,
                target=resolved_target,
                migrated=max(0, source_count - duplicates),
                duplicates=duplicates,
            )

    def add_pending_recall(
        self,
        message_id: str,
        session_id: str,
        group_id: str | None,
        recall_at: int,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO pending_recalls(
                    message_id, session_id, group_id, recall_at
                ) VALUES (?, ?, ?, ?)
                """,
                (str(message_id), str(session_id), group_id, int(recall_at)),
            )

    def remove_pending_recall(self, message_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM pending_recalls WHERE message_id = ?",
                (str(message_id),),
            )

    def list_pending_recalls(self) -> list[PendingRecall]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT message_id, session_id, group_id, recall_at
                FROM pending_recalls ORDER BY recall_at
                """,
            ).fetchall()
            return [
                PendingRecall(
                    message_id=str(row["message_id"]),
                    session_id=str(row["session_id"]),
                    group_id=str(row["group_id"]) if row["group_id"] is not None else None,
                    recall_at=int(row["recall_at"]),
                )
                for row in rows
            ]

    def clear_tmp(self) -> None:
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        for path in list(self.tmp_dir.iterdir()):
            try:
                if path.is_dir():
                    import shutil

                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
            except OSError:
                # A stale temporary file should not prevent AstrBot from
                # starting; the next fetch will use another random name.
                continue

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None  # type: ignore[assignment]

    def __enter__(self) -> Storage:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


__all__ = [
    "FileRecord",
    "MergeResult",
    "PendingRecall",
    "Storage",
    "TagRecord",
    "TagSummary",
    "normalize_tag",
]
