import json
from pathlib import Path
import tempfile
import unittest

from material_search.store import MODEL, connect, import_results, search
from nas_scanner.safety import StorageSafetyError


class MaterialSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.db = connect(self.base/'index.sqlite')
        self.addCleanup(self.db.close)
        self.sha = 'a'*64
        self.sample = {'sha256': self.sha, 'source_relative_path': 'clip.mov',
                       'local_relative_path': 'videos/clip.mov', 'metadata': {'width': 1920}}
        self.case = {'sha256': self.sha, 'source': 'clip.mov', 'requested_model': MODEL,
                     'raw_output': json.dumps({'summary': '手将燕麦倒入碗中', 'subjects': ['燕麦', '碗'],
                         'actions': ['倒入碗中'], 'visible_text': ['背景电视独家字幕'],
                         'timeline': [{'start': 0, 'end': 0}]}),
                     'duration_seconds': 6, 'assessment': {'machine_usable': False, 'time_status': 'boundary_error'}}
        self.report = {'cases': [self.case], 'attempt_history': [self.case, {'requested_model': MODEL, 'status': 'http_error'}]}
        self.run = {'samples': [self.sample], 'prompt_version': 'test', 'compression_profile': '720p'}
        self.manifest = {'source_root': str(self.base/'nas'), 'files': [self.sample.copy()]}

    def load(self):
        return import_results(self.db, self.report, self.run, self.manifest, self.base/'testset')

    def test_idempotence_provenance_and_no_source_access(self):
        # Source files/directories do not exist: importer must not open videos or NAS.
        self.assertEqual(self.load()['imported'], 1)
        self.assertEqual(self.load()['changes'], 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM search_attempts').fetchone()[0], 2)
        hit = search(self.db, '燕麦 倒入')[0]
        self.assertEqual(hit['review_state'], 'needs_review')
        self.assertEqual(hit['evidence']['case']['raw_output'], self.case['raw_output'])
        self.assertEqual(len(hit['locations']), 2)
        self.assertEqual(search(self.db, '电视'), [])
        self.assertEqual(len(search(self.db, '电视', include_ocr=True)), 1)
        self.assertEqual(len(search(self.db, '碗')), 1)
        self.assertEqual(search(self.db, '燕麦', max_duration=5), [])
        self.assertEqual(len(search(self.db, '', subject='碗', action='倒入', review='needs_review')), 1)
        self.assertEqual(search(self.db, '" OR *'), [])

    def test_invalid_json_and_new_revision_retained(self):
        self.load()
        self.case['raw_output'] = '{broken'
        self.assertEqual(self.load()['imported'], 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM search_analyses').fetchone()[0], 2)
        self.assertEqual(search(self.db, '燕麦'), [])
        self.assertEqual(search(self.db, '')[0]['review_state'], 'needs_review')

    def test_mapping_error_rolls_back(self):
        self.sample['source_relative_path'] = '../escape.mov'
        with self.assertRaises(ValueError):
            self.load()
        self.assertEqual(self.db.execute('SELECT count(*) FROM search_assets').fetchone()[0], 0)

    def test_scanner_hash_join(self):
        self.load()
        self.db.execute('CREATE TABLE files(root_id INTEGER,path TEXT,status TEXT,sha256 TEXT)')
        self.db.execute('INSERT INTO files VALUES(1,?,?,?)', ('moved.mov', 'ready', self.sha))
        self.assertEqual(search(self.db, '燕麦')[0]['scanner_files'][0]['path'], 'moved.mov')

    def test_database_in_source_is_rejected(self):
        self.manifest['source_root'] = str(self.base)
        with self.assertRaises(StorageSafetyError):
            self.load()
