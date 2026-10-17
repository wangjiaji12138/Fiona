"""隔离测试登记移动及失败恢复，不使用正式 PDF。"""
import json
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fiona.ingestion.catalog import apply_catalog_update, filename_metadata, plan_catalog_update


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
        self.manifest = self.root / 'source_manifest.jsonl'

    def seed_existing(self, doc_id='Earlier', filename='registered/Earlier.pdf'):
        self.config.write_text(json.dumps({doc_id: filename}))
        self.manifest.write_text(json.dumps({
            'doc_id': doc_id, 'raw_path': f'raw/{filename}',
            'sha256': hashlib.sha256(b'existing').hexdigest(),
            'metadata': {'authors': ['Verified author']}, 'title': 'Verified title',
        }) + '\n')

    def plan(self):
        return plan_catalog_update(self.raw, self.config)

    def test_move_nested_preserves_bytes_and_empty_pending(self):
        (self.pending / 'papers').mkdir()
        source = self.pending / 'papers' / 'CIKM-2023-STAEformer.PDF'
        source.write_bytes(b'pdf fixture contents')
        plan = self.plan()
        self.assertTrue(source.exists())  # 预检没有副作用
        self.assertFalse(self.config.exists())
        self.assertFalse(self.manifest.exists())
        apply_catalog_update(plan)
        target = self.registered / 'papers' / source.name
        self.assertEqual(target.read_bytes(), b'pdf fixture contents')
        self.assertTrue(self.pending.is_dir())
        self.assertEqual(list(self.pending.iterdir()), [])
        self.assertEqual(json.loads(self.config.read_text()),
                         {'CIKM-2023-STAEformer': 'registered/papers/CIKM-2023-STAEformer.PDF'})
        stamp = self.config.stat().st_mtime_ns
        manifest_stamp = self.manifest.stat().st_mtime_ns
        record = json.loads(self.manifest.read_text())
        self.assertEqual(record['sha256'], hashlib.sha256(b'pdf fixture contents').hexdigest())
        self.assertEqual(record['raw_path'], 'raw/registered/papers/CIKM-2023-STAEformer.PDF')
        self.assertEqual(record['status'], 'downloaded')
        self.assertNotIn('downloaded_at', record)
        self.assertNotIn('notes', record)
        self.assertEqual(record['language'], 'en')
        self.assertEqual(record['title'], 'STAEformer')
        self.assertEqual(record['metadata']['year'], 2023)
        self.assertEqual(record['metadata']['venue'], 'CIKM')
        again = self.plan()
        self.assertEqual(again.pdf_count, 0)
        apply_catalog_update(again)
        self.assertEqual(self.config.stat().st_mtime_ns, stamp)
        self.assertEqual(self.manifest.stat().st_mtime_ns, manifest_stamp)

    def test_never_scans_registered(self):
        self.registered.mkdir()
        (self.registered / 'Unlisted.pdf').touch()
        (self.pending / 'New.pdf').touch()
        self.seed_existing(filename='registered/Missing.pdf')
        plan = self.plan()
        self.assertEqual(plan.pdf_count, 1)
        self.assertNotIn('Unlisted', plan.mapping)
        self.assertEqual(plan.mapping['Earlier'], 'registered/Missing.pdf')

    def test_existing_id_or_destination_conflict(self):
        source = self.pending / 'Paper.pdf'
        source.write_bytes(b'new')
        self.seed_existing('Paper', 'registered/Paper.pdf')
        with self.assertRaisesRegex(ValueError, '冲突'):
            self.plan()
        self.config.unlink()
        self.manifest.unlink()
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
        self.seed_existing()
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
            (self.pending / name).write_bytes(name.encode())
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

    def test_same_content_in_batch_or_existing_manifest_is_rejected(self):
        (self.pending / 'A.pdf').write_bytes(b'existing')
        (self.pending / 'B.pdf').write_bytes(b'existing')
        with self.assertRaisesRegex(ValueError, '重复内容'):
            self.plan()
        self.assertFalse(self.config.exists())
        self.assertFalse(self.manifest.exists())
        (self.pending / 'B.pdf').unlink()
        self.seed_existing()
        with self.assertRaisesRegex(ValueError, '重复内容'):
            self.plan()
        self.assertTrue((self.pending / 'A.pdf').exists())

    def test_manifest_write_failure_restores_both_outputs_and_pdfs(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    self.seed_existing()
                original_config = self.config.read_bytes() if self.config.exists() else None
                original_manifest = self.manifest.read_bytes() if self.manifest.exists() else None
                (self.pending / 'New.pdf').write_bytes(b'new')
                with patch('fiona.ingestion.catalog.write_manifest', side_effect=OSError('disk full')):
                    with self.assertRaisesRegex(OSError, 'disk full'):
                        apply_catalog_update(self.plan())
                self.assertEqual(self.config.read_bytes() if self.config.exists() else None, original_config)
                self.assertEqual(self.manifest.read_bytes() if self.manifest.exists() else None, original_manifest)
                self.assertEqual((self.pending / 'New.pdf').read_bytes(), b'new')
                self.assertFalse((self.registered / 'New.pdf').exists())

    def test_changed_manifest_aborts_before_moving(self):
        (self.pending / 'New.pdf').write_bytes(b'new')
        plan = self.plan()
        self.manifest.write_text('\n')
        with self.assertRaisesRegex(ValueError, '来源清单在预检后发生变化'):
            apply_catalog_update(plan)
        self.assertTrue((self.pending / 'New.pdf').exists())
        self.assertFalse(self.config.exists())

    def test_changed_pdf_aborts_and_moves_back(self):
        source = self.pending / 'New.pdf'
        source.write_bytes(b'old')
        plan = self.plan()
        source.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'PDF 在预检后发生变化'):
            apply_catalog_update(plan)
        self.assertEqual(source.read_bytes(), b'changed')
        self.assertFalse((self.registered / source.name).exists())
        self.assertFalse(self.config.exists())
        self.assertFalse(self.manifest.exists())

    def test_existing_metadata_preserved_when_appending(self):
        self.seed_existing()
        before = json.loads(self.manifest.read_text())
        (self.pending / 'New.pdf').write_bytes(b'new')
        apply_catalog_update(self.plan())
        records = [json.loads(line) for line in self.manifest.read_text().splitlines()]
        self.assertEqual(records[0], before)
        self.assertEqual(len(records), 2)

    def test_inconsistent_or_invalid_manifest_is_rejected(self):
        (self.pending / 'New.pdf').write_bytes(b'new')
        self.seed_existing()
        valid = self.manifest.read_text()
        for content in (None, '{', '[]', valid + valid,
                        valid.replace('raw/registered/', 'raw/wrong/'),
                        valid.replace(hashlib.sha256(b'existing').hexdigest(), 'bad')):
            with self.subTest(content=content):
                if content is None:
                    self.manifest.unlink(missing_ok=True)
                else:
                    self.manifest.write_text(content)
                with self.assertRaises(ValueError):
                    self.plan()
                self.assertTrue((self.pending / 'New.pdf').exists())
        self.manifest.write_text(valid)
        self.config.unlink()
        with self.assertRaisesRegex(ValueError, '与配置不一致'):
            self.plan()

    def test_manifest_path_constraints(self):
        (self.pending / 'New.pdf').write_bytes(b'new')
        for path in (self.config, self.raw / 'manifest.jsonl', self.root / 'subdir/manifest.jsonl'):
            with self.assertRaisesRegex(ValueError, '来源清单必须'):
                plan_catalog_update(self.raw, self.config, path)
        link = self.root / 'link.jsonl'
        link.symlink_to(self.root / 'outside.jsonl')
        with self.assertRaisesRegex(ValueError, '来源清单必须'):
            plan_catalog_update(self.raw, self.config, link)

    def test_custom_raw_name_and_manifest_name(self):
        custom_raw = self.root / 'papers'
        (custom_raw / 'unregistered').mkdir(parents=True)
        (custom_raw / 'unregistered/Example.pdf').write_bytes(b'pdf')
        manifest = self.root / 'sources.jsonl'
        apply_catalog_update(plan_catalog_update(custom_raw, self.config, manifest))
        record = json.loads(manifest.read_text())
        self.assertEqual(record['raw_path'], 'papers/registered/Example.pdf')
        self.assertEqual(plan_catalog_update(custom_raw, self.config, manifest).pdf_count, 0)

    def test_filename_fields_preserve_title_hyphens_and_unknowns(self):
        cases = {
            'KDD-2024-ST-Norm - A Spatial-Temporal Method':
                ('ST-Norm - A Spatial-Temporal Method', 2024, 'KDD'),
            'arXiv-2020-Traffic Forecasting': ('Traffic Forecasting', 2020, 'arXiv'),
            'Unknown-Unknown-A Title': ('A Title', None, None),
            'CIKM-Unknown-A Title': ('A Title', None, 'CIKM'),
            'Some Unformatted Title': ('Some Unformatted Title', None, None),
            'CIKM-20-Bad Year': ('CIKM-20-Bad Year', None, None),
            'CIKM-2023- ': ('CIKM-2023- ', None, None),
        }
        for doc_id, expected in cases.items():
            with self.subTest(doc_id=doc_id):
                self.assertEqual(filename_metadata(doc_id), expected)

    def seed_legacy_metadata(self):
        doc_id = 'CIKM-2023-STAEformer - A Spatial-Temporal Model'
        self.seed_existing(doc_id, f'registered/{doc_id}.pdf')
        record = json.loads(self.manifest.read_text())
        record.update(title=doc_id, language=None, notes='old notes', downloaded_at=None)
        record['metadata'].update(year=None, venue=None)
        self.manifest.write_text(json.dumps(record) + '\n')
        return record

    def test_refresh_existing_metadata_without_reading_or_moving_pdfs(self):
        before = self.seed_legacy_metadata()
        original = self.manifest.read_bytes()
        config_stamp = self.config.stat().st_mtime_ns
        with patch('fiona.ingestion.catalog.sha256_file', side_effect=AssertionError('must not read PDF')):
            plan = plan_catalog_update(self.raw, self.config, refresh_metadata=True)
            self.assertEqual(plan.metadata_updates, 1)
            self.assertEqual(plan.pdf_count, 0)
            self.assertEqual(self.manifest.read_bytes(), original)
            apply_catalog_update(plan)
        record = json.loads(self.manifest.read_text())
        self.assertEqual(record['title'], 'STAEformer - A Spatial-Temporal Model')
        self.assertEqual(record['metadata']['year'], 2023)
        self.assertEqual(record['metadata']['venue'], 'CIKM')
        self.assertEqual(record['metadata']['authors'], ['Verified author'])
        self.assertEqual(record['sha256'], before['sha256'])
        self.assertEqual(record['raw_path'], before['raw_path'])
        self.assertEqual(record['language'], 'en')
        self.assertNotIn('notes', record)
        self.assertNotIn('downloaded_at', record)
        self.assertEqual(self.config.stat().st_mtime_ns, config_stamp)
        stamp = self.manifest.stat().st_mtime_ns
        plan = plan_catalog_update(self.raw, self.config, refresh_metadata=True)
        self.assertEqual(plan.metadata_updates, 0)
        apply_catalog_update(plan)
        self.assertEqual(self.manifest.stat().st_mtime_ns, stamp)

    def test_refresh_preserves_verified_fields_and_restores_on_write_failure(self):
        record = self.seed_legacy_metadata()
        record['title'] = 'Verified title'
        record['metadata'].update(year=2022, venue='Verified venue', doi='example')
        self.manifest.write_text(json.dumps(record) + '\n')
        original = self.manifest.read_bytes()
        config_stamp = self.config.stat().st_mtime_ns
        plan = plan_catalog_update(self.raw, self.config, refresh_metadata=True)
        self.assertEqual(plan.records[0]['title'], 'Verified title')
        self.assertEqual(plan.records[0]['metadata'], record['metadata'])
        with patch('fiona.ingestion.catalog.write_manifest', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                apply_catalog_update(plan)
        self.assertEqual(self.manifest.read_bytes(), original)
        self.assertEqual(self.config.stat().st_mtime_ns, config_stamp)


if __name__ == '__main__':
    unittest.main()
