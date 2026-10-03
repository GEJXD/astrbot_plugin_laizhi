import tempfile
import unittest
from pathlib import Path

from media import MediaManager, MediaReference, MediaTooLarge, UnsupportedMedia
from storage import Storage, normalize_tag


class StorageTest(unittest.TestCase):
    def test_tags_are_exact_and_aliases_resolve(self) -> None:
        self.assertEqual(normalize_tag(" ＡＢＣ "), "ABC")
        self.assertIsNone(normalize_tag("a/b"))
        self.assertIsNone(normalize_tag("a b"))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = Storage(root / "laizhi.db", root)
            try:
                temp_file = root / "tmp" / "asset.tmp"
                temp_file.write_bytes(b"asset")
                file_record = storage.store_file(
                    temp_file,
                    file_hash="a" * 64,
                    kind="file",
                    ext="bin",
                    mime=None,
                    size=5,
                )
                source = storage.get_or_create_tag("猫猫", "user")
                target = storage.get_or_create_tag("动物", "admin")
                self.assertEqual(
                    storage.attach(file_record.id, source.id, added_by="user"),
                    "added",
                )
                self.assertEqual(storage.attach(file_record.id, source.id), "duplicate")
                self.assertEqual(
                    storage.get_file_tag_relation(file_record.id, source.id),
                    (True, "user"),
                )
                self.assertFalse(
                    storage.detach_owned(file_record.id, source.id, "other-user"),
                )

                merged = storage.merge_tag(source.id, target.id)
                self.assertEqual(merged.migrated, 1)
                self.assertEqual(merged.duplicates, 0)
                self.assertEqual(storage.resolve_tag("猫猫").id, target.id)
                self.assertEqual(storage.count_for_tag(target.id), 1)
                files, total = storage.list_files_for_tag(target.id, query="bin")
                self.assertEqual(total, 1)
                self.assertEqual(files[0][0].id, file_record.id)
                self.assertEqual(storage.get_file(file_record.id), file_record)

                managed = storage.get_or_create_tag("管理", "admin")
                renamed = storage.rename_tag(managed.id, "管理页")
                self.assertEqual(renamed.name, "管理页")
                deleted = storage.delete_tag(renamed.id)
                self.assertEqual(deleted.tag.id, managed.id)
                self.assertIsNone(storage.get_tag_by_id(managed.id))

                final_target = storage.get_or_create_tag("哺乳类", "admin")
                storage.merge_tag(target.id, final_target.id)
                # Existing aliases are retargeted directly, so one-hop
                # resolution remains sufficient after repeated merges.
                self.assertEqual(storage.resolve_tag("猫猫").id, final_target.id)

                orphan = root / "blobs" / "ff" / "orphan.bin"
                orphan.parent.mkdir(parents=True, exist_ok=True)
                orphan.write_bytes(b"orphan")
                self.assertGreaterEqual(storage.gc_orphans(), 1)
                self.assertFalse(orphan.exists())
            finally:
                storage.close()


class MediaTest(unittest.IsolatedAsyncioTestCase):
    async def test_local_magic_sniff_and_size_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "not_an_image.txt"
            source.write_bytes(b"\x89PNG\r\n\x1a\nminimal")
            manager = MediaManager(root / "tmp", max_size_mb=1)
            media = await manager.fetch(MediaReference(str(source)))
            try:
                self.assertEqual(media.kind, "image")
                self.assertEqual(media.ext, "png")
                self.assertEqual(media.size, source.stat().st_size)
            finally:
                manager.cleanup(media)
                await manager.close()

            unknown = root / "unknown.jpg"
            unknown.write_bytes(b"not a recognized media format")
            strict_manager = MediaManager(root / "strict_tmp", max_size_mb=1)
            with self.assertRaises(UnsupportedMedia):
                await strict_manager.fetch(MediaReference(str(unknown)))
            await strict_manager.close()

            too_large = root / "large.bin"
            too_large.write_bytes(b"x" * (1024 * 1024 + 1))
            with self.assertRaises(MediaTooLarge):
                await manager.fetch(MediaReference(str(too_large)))


if __name__ == "__main__":
    unittest.main()
