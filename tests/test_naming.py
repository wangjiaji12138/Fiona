"""已登记名称迁移：验证引用完整性、内容保护与失败恢复。"""

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fiona.ingestion.catalog import _write_bytes
from fiona.ingestion.naming import normalize_registered_names


class NamingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.raw = self.root / 'raw'
        self.registered = self.raw / 'registered'
        self.registered.mkdir(parents=True)
        self.old = 'ESWA-2023-DAGCRN - Graph convolutional network'
        self.new = 'ESWA-2023-DAGCRN-Graph convolutional network'
        self.pdf = self.registered / f'{self.old}.pdf'
        self.pdf.write_bytes(b'original PDF bytes')
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({self.old: f'registered/{self.old}.pdf'}))
        self.manifest = self.root / 'source_manifest.jsonl'
        self.manifest.write_text(json.dumps({
            'doc_id': self.old, 'raw_path': f'raw/registered/{self.old}.pdf',
            'sha256': hashlib.sha256(self.pdf.read_bytes()).hexdigest(),
            'related_doc_ids': [self.old, 'External'], 'title': 'Original paper title',
            'metadata': {'authors': ['An author']},
        }) + '\n')
        (self.root / 'parsed').mkdir()
        self.parsed = self.root / 'parsed' / f'{self.old}.jsonl'
        self.page = {'doc_id': self.old, 'page_number': 3, 'text': 'Keep - original text', 'status': 'extracted'}
        self.parsed.write_text(json.dumps(self.page) + '\n')
        self.backup = self.root / 'backup'

    def run_update(self, dry_run=False):
        return normalize_registered_names(self.raw, self.config, self.backup, dry_run=dry_run)

    def test_preflight_migration_preserves_evidence_and_is_idempotent(self):
        original = {p: p.read_bytes() for p in (self.config, self.manifest, self.pdf, self.parsed)}
        self.assertEqual(self.run_update(True), {self.old: self.new})
        self.assertFalse(self.backup.exists())
        self.assertTrue(all(p.read_bytes() == content for p, content in original.items()))
        self.run_update()
        target = self.registered / f'{self.new}.pdf'
        self.assertEqual(target.read_bytes(), original[self.pdf])
        self.assertFalse(self.pdf.exists())
        record = json.loads(self.manifest.read_text())
        self.assertEqual(record['related_doc_ids'], [self.new, 'External'])
        self.assertEqual(record['metadata']['authors'], ['An author'])
        self.assertEqual(record['title'], 'Original paper title')
        page = json.loads(self.parsed.with_name(f'{self.new}.jsonl').read_text())
        self.assertEqual(page, {**self.page, 'doc_id': self.new})
        self.assertFalse(self.parsed.exists())
        self.assertEqual(json.loads(self.config.read_text()), {self.new: f'registered/{self.new}.pdf'})
        self.assertTrue((self.backup / 'plan.json').exists())
        stamp = self.config.stat().st_mtime_ns
        self.assertEqual(self.run_update(), {})
        self.assertEqual(self.config.stat().st_mtime_ns, stamp)

    def test_conflicts_or_changed_hash_abort_without_modifying_inputs(self):
        target = self.registered / f'{self.new}.pdf'
        target.write_bytes(b'keep target')
        with self.assertRaisesRegex(ValueError, '目标已存在'):
            self.run_update()
        self.assertTrue(self.pdf.exists())
        self.assertEqual(target.read_bytes(), b'keep target')
        self.assertFalse(self.backup.exists())
        target.unlink()
        self.pdf.write_bytes(b'changed bytes')
        with self.assertRaisesRegex(ValueError, '哈希不一致'):
            self.run_update()
        self.assertFalse(self.backup.exists())

    def test_normalization_collision_aborts(self):
        mapping = {self.old: f'registered/{self.old}.pdf', self.new: f'registered/{self.new}.pdf'}
        record = json.loads(self.manifest.read_text())
        second = {**record, 'doc_id': self.new, 'raw_path': f'raw/registered/{self.new}.pdf',
                  'sha256': hashlib.sha256(b'other').hexdigest()}
        self.config.write_text(json.dumps(mapping))
        self.manifest.write_text(json.dumps(record) + '\n' + json.dumps(second) + '\n')
        with self.assertRaisesRegex(ValueError, 'ID 冲突'):
            self.run_update()
        self.assertTrue(self.pdf.exists())
        self.assertFalse(self.backup.exists())

    def test_write_failure_restores_pdf_and_all_outputs(self):
        original = {p: p.read_bytes() for p in (self.config, self.manifest, self.pdf, self.parsed)}
        calls = 0

        def fail_manifest_once(path, content):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('disk full')
            _write_bytes(path, content)

        with patch('fiona.ingestion.naming._write_bytes', side_effect=fail_manifest_once):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.run_update()
        self.assertTrue(all(p.read_bytes() == content for p, content in original.items()))
        self.assertFalse((self.registered / f'{self.new}.pdf').exists())
        self.assertFalse(self.parsed.with_name(f'{self.new}.jsonl').exists())

    def test_downstream_or_wrong_page_id_is_rejected(self):
        (self.root / 'chunks').mkdir()
        chunk = self.root / 'chunks/existing.jsonl'
        chunk.write_text('has references')
        with self.assertRaisesRegex(ValueError, '下游产物'):
            self.run_update()
        chunk.unlink()
        self.parsed.write_text(json.dumps({**self.page, 'doc_id': 'Wrong'}) + '\n')
        with self.assertRaisesRegex(ValueError, 'ID 不匹配'):
            self.run_update()
        self.assertFalse(self.backup.exists())


if __name__ == '__main__':
    unittest.main()
