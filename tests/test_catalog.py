"""隔离测试登记移动及失败恢复，不使用正式 PDF。"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fiona.ingestion.catalog import apply_catalog_update, plan_catalog_update


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.raw = self.root / 'raw'
        self.pending = self.raw / 'unregistered'
        self.registered = self.raw / 'registered'
        self.pending.mkdir(parents=True)
        self.config = self.root / 'config.json'

    def plan(self):
        return plan_catalog_update(self.raw, self.config)

    def test_move_nested_preserves_bytes_and_empty_pending(self):
        (self.pending / 'papers').mkdir()
        source = self.pending / 'papers' / 'CIKM-2023-STAEformer.PDF'
        source.write_bytes(b'pdf fixture contents')
        plan = self.plan()
        self.assertTrue(source.exists())  # 预检没有副作用
        self.assertFalse(self.config.exists())
        apply_catalog_update(plan)
        target = self.registered / 'papers' / source.name
        self.assertEqual(target.read_bytes(), b'pdf fixture contents')
        self.assertTrue(self.pending.is_dir())
        self.assertEqual(list(self.pending.iterdir()), [])
        self.assertEqual(json.loads(self.config.read_text()),
                         {'CIKM-2023-STAEformer': 'registered/papers/CIKM-2023-STAEformer.PDF'})
        stamp = self.config.stat().st_mtime_ns
        again = self.plan()
        self.assertEqual(again.pdf_count, 0)
        apply_catalog_update(again)
        self.assertEqual(self.config.stat().st_mtime_ns, stamp)

    def test_never_scans_registered(self):
        self.registered.mkdir()
        (self.registered / 'Unlisted.pdf').touch()
        (self.pending / 'New.pdf').touch()
        self.config.write_text('{"Earlier": "registered/Missing.pdf"}')
        plan = self.plan()
        self.assertEqual(plan.pdf_count, 1)
        self.assertNotIn('Unlisted', plan.mapping)
        self.assertEqual(plan.mapping['Earlier'], 'registered/Missing.pdf')

    def test_existing_id_or_destination_conflict(self):
        source = self.pending / 'Paper.pdf'
        source.write_bytes(b'new')
        self.config.write_text('{"Paper": "registered/Paper.pdf"}')
        with self.assertRaisesRegex(ValueError, '冲突'):
            self.plan()
        self.config.unlink()
        self.registered.mkdir()
        target = self.registered / source.name
        target.write_bytes(b'original')
        with self.assertRaisesRegex(ValueError, '不能覆盖'):
            self.plan()
        self.assertEqual(source.read_bytes(), b'new')
        self.assertEqual(target.read_bytes(), b'original')

    def test_duplicate_pending_ids_reject_whole_batch(self):
        for name in ['Paper.pdf', 'paper.PDF']:
            (self.pending / name).touch()
        with self.assertRaisesRegex(ValueError, '冲突'):
            self.plan()
        self.assertFalse(self.config.exists())
        self.assertEqual(len(list(self.pending.iterdir())), 2)

    def test_non_pdf_is_retained(self):
        (self.pending / 'notes.txt').write_text('keep')
        (self.pending / 'Paper.pdf').touch()
        plan = self.plan()
        self.assertEqual(plan.skipped, ('notes.txt',))
        apply_catalog_update(plan)
        self.assertEqual([p.name for p in self.pending.iterdir()], ['notes.txt'])

    def test_write_failure_rolls_back_entire_batch(self):
        self.config.write_text('{"Earlier": "registered/Earlier.pdf"}')
        original = self.config.read_bytes()
        for name in ['A.pdf', 'B.pdf']:
            (self.pending / name).write_bytes(name.encode())
        with patch('fiona.ingestion.catalog.write_catalog', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                apply_catalog_update(self.plan())
        self.assertEqual(self.config.read_bytes(), original)
        for name in ['A.pdf', 'B.pdf']:
            self.assertEqual((self.pending / name).read_bytes(), name.encode())
            self.assertFalse((self.registered / name).exists())

    def test_late_move_conflict_rolls_back_without_overwrite(self):
        for name in ['A.pdf', 'B.pdf']:
            (self.pending / name).touch()
        plan = self.plan()
        self.registered.mkdir()
        (self.registered / 'B.pdf').write_bytes(b'newly created target')
        with self.assertRaises(FileExistsError):
            apply_catalog_update(plan)
        self.assertTrue((self.pending / 'A.pdf').exists())
        self.assertTrue((self.pending / 'B.pdf').exists())
        self.assertFalse((self.registered / 'A.pdf').exists())
        self.assertEqual((self.registered / 'B.pdf').read_bytes(), b'newly created target')
        self.assertFalse(self.config.exists())

    def test_changed_config_aborts_before_moving(self):
        (self.pending / 'A.pdf').touch()
        plan = self.plan()
        self.config.write_text('{}')
        with self.assertRaisesRegex(ValueError, '发生变化'):
            apply_catalog_update(plan)
        self.assertTrue((self.pending / 'A.pdf').exists())

    def test_bad_config_and_symlink_leave_files(self):
        (self.pending / 'Paper.pdf').touch()
        for value in ['{', '[]', '{"A":"a.pdf","A":"b.pdf"}', '{"A":"../a.pdf"}']:
            self.config.write_text(value)
            with self.assertRaises(ValueError):
                self.plan()
            self.assertEqual(self.config.read_text(), value)
        self.config.unlink()
        (self.pending / 'outside.pdf').symlink_to(self.root / 'outside.pdf')
        with self.assertRaisesRegex(ValueError, '符号链接'):
            self.plan()

    def test_missing_pending_is_initialized_only_on_apply(self):
        self.pending.rmdir()
        plan = self.plan()
        self.assertFalse(self.pending.exists())
        apply_catalog_update(plan)
        self.assertTrue(self.pending.is_dir())
        self.assertTrue(self.registered.is_dir())
        self.assertFalse(self.config.exists())


if __name__ == '__main__':
    unittest.main()
