import json
from pathlib import Path
from contextlib import contextmanager
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from video_understanding.pipeline import AnalysisError, analyze, ark_result, mock_result, validate


class UnderstandingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.video = self.root / 'sample.mp4'
        self.video.write_bytes(b'local test')
        self.db = self.root / 'p1.db'
        mock = patch('video_understanding.pipeline.probe', return_value={'duration': '8'})
        @contextmanager
        def fake_compression(video, *args):
            yield video
        compression = patch('video_understanding.pipeline.compressed_video', side_effect=fake_compression)
        compression.start()
        self.addCleanup(compression.stop)
        mock.start()
        self.addCleanup(mock.stop)

    def test_mock_offline_and_cached(self):
        with patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')):
            first = analyze(self.video, self.db)
            second = analyze(self.video, self.db)
        self.assertFalse(first['cached'])
        self.assertTrue(second['cached'])
        self.assertEqual(first['provider'], 'mock')
        self.assertIn('模拟', first['result']['summary'])

    def test_cache_invalidates_when_content_changes(self):
        analyze(self.video, self.db)
        self.video.write_bytes(b'changed local sample')
        self.assertFalse(analyze(self.video, self.db)['cached'])

    def test_ark_requires_credentials(self):
        with patch.dict('os.environ', {}, clear=True), self.assertRaises(AnalysisError):
            analyze(self.video, self.db, provider='ark', model='test')

    def test_schema_rejects_bad_timeline(self):
        for start, end in [(0, 9), (2, 1), (True, 2), (0, float('nan'))]:
            data = mock_result(8)
            data['timeline'][0].update(start=start, end=end)
            with self.subTest(start=start, end=end), self.assertRaises(AnalysisError):
                validate(data, 8)
        data = mock_result(8)
        data['timeline'].append({'start': 3, 'end': 5, 'description': 'overlap'})
        with self.assertRaises(AnalysisError):
            validate(data, 8)

    def test_schema_rejects_missing_field(self):
        data = mock_result(8)
        del data['visual_description']
        with self.assertRaises(AnalysisError):
            validate(data, 8)

    def test_ark_success_and_cache(self):
        with patch.dict('os.environ', {'ARK_API_KEY': 'test-key'}), \
                patch('video_understanding.pipeline.ark_result', return_value=(mock_result(8), {'total_tokens': 100})) as provider:
            result = analyze(self.video, self.db, provider='ark', model='endpoint-test')
            self.assertEqual(result['usage']['total_tokens'], 100)
            self.assertTrue(analyze(self.video, self.db, provider='ark', model='endpoint-test')['cached'])
            self.assertFalse(analyze(self.video, self.db, provider='ark', model='endpoint-other')['cached'])
            self.assertEqual(provider.call_count, 2)

    def test_http_error_sanitized_no_retry(self):
        with patch.dict('os.environ', {'ARK_API_KEY': 'test-key'}), \
                patch('urllib.request.urlopen', side_effect=HTTPError('url', 429, 'secret', {}, None)) as request:
            with self.assertRaisesRegex(AnalysisError, 'HTTP 429'):
                ark_result(self.video, 'test', 1, 60)
            self.assertEqual(request.call_count, 1)

    def test_http_payload_and_response(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(mock_result(8))}}],
            'usage': {'total_tokens': 12}})
        with patch.dict('os.environ', {'ARK_API_KEY': 'test-key'}), \
                patch('urllib.request.urlopen', return_value=response) as request:
            result, usage = ark_result(self.video, 'test-model', 2, 60)
        payload = json.loads(request.call_args.args[0].data)
        self.assertEqual(payload['messages'][0]['content'][0]['video_url']['fps'], 2)
        self.assertEqual(usage['total_tokens'], 12)
        validate(result, 8)

    def test_duration_limit_and_invalid_configuration(self):
        with self.assertRaises(AnalysisError):
            analyze(self.video, self.db, max_duration=5)
        with self.assertRaises(AnalysisError):
            analyze(self.video, self.db, fps=float('inf'))
        with self.assertRaises(AnalysisError):
            analyze(self.video, self.video)
