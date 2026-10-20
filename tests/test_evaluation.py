import unittest
from urllib.error import URLError
from video_understanding.evaluation import price_usage
from video_understanding.pipeline import visual_duration


class PriceUsageTests(unittest.TestCase):
    def test_uncached_and_cached(self):
        model = {'model_id':'doubao-seed-2-0-mini-260428','input_price':.2,'output_price':2}
        self.assertEqual(price_usage({'prompt_tokens':4000,'completion_tokens':600}, model), '0.002')
        self.assertEqual(price_usage({'prompt_tokens':4000,'completion_tokens':600,'prompt_tokens_details':{'cached_tokens':1000}}, model), '0.00184')
        self.assertEqual(price_usage({'prompt_tokens':4000,'completion_tokens':600}, model, 'flex'), '0.001')
        self.assertEqual(price_usage({'prompt_tokens':4000,'completion_tokens':600,'prompt_tokens_details':{'cached_tokens':1000}}, model, 'flex'), '0.00094')
        self.assertIsNone(price_usage({}, model))
        self.assertIsNone(price_usage({'prompt_tokens':4000,'completion_tokens':600}, model, 'fast'))

    def test_long_context_tier(self):
        model = {'model_id':'doubao-seed-2-0-lite-260428','input_price':.6,'output_price':3.6}
        self.assertEqual(price_usage({'prompt_tokens':40000,'completion_tokens':1000}, model), '0.0414')


class VisualDurationTests(unittest.TestCase):
    def test_audio_tail_is_not_visual_duration(self):
        metadata = {'duration': '56.764', 'raw': {'streams': [
            {'codec_type': 'video', 'duration': '56.278833'},
            {'codec_type': 'audio', 'duration': '56.764'}]}}
        self.assertEqual(visual_duration(metadata), 56.278833)
        self.assertEqual(visual_duration({'duration': '8'}), 8)


class ResumeTests(unittest.TestCase):
    def test_invalid_budget_rejected_before_loading_credentials(self):
        from unittest.mock import patch
        from pathlib import Path
        from video_understanding.evaluation import run
        for budget in ('0', '-1', 'NaN', 'Infinity'):
            with self.subTest(budget=budget), patch('video_understanding.evaluation.load_local_config') as load:
                with self.assertRaises(ValueError):
                    run(Path('unused'), Path('unused'), Path('unused'), budget_cny=budget)
                load.assert_not_called()

    def test_resume_never_repeats_completed_paid_requests(self):
        import hashlib
        import json
        import tempfile
        from contextlib import contextmanager
        from pathlib import Path
        from unittest.mock import patch
        from video_understanding.evaluation import run
        from video_understanding.pipeline import mock_result
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source = base / 'testset'
            source.mkdir()
            files = []
            for name in ('袋中展示/', 'R搅拌酸奶/', 'R包装配料营养成分gi/', 'A模特拿产品展示/', '沉浸式倒牛奶/'):
                video = source / name / 'clip.mp4'
                video.parent.mkdir()
                video.write_bytes(b'fixture')
                files.append({'source_relative_path': name + 'clip.mp4', 'local_relative_path': name + 'clip.mp4',
                              'metadata': {'duration': '8'}, 'sha256': hashlib.sha256(b'fixture').hexdigest()})
            (source / 'manifest.json').write_text(json.dumps({'files': files}))
            model = {'model_id': 'doubao-seed-2-0-mini-260428', 'name': 'Mini', 'role': 'cost_baseline', 'input_price': .2, 'output_price': 2}
            catalog = base / 'catalog.json'
            catalog.write_text(json.dumps({'models': [model]}))
            output = base / 'run'
            output.mkdir()
            report = {'samples': files, 'models': [model], 'known_list_price_cny': '0.001',
                      'attempts': [{'sample': i, 'requested_model': model['model_id'], 'status': 'schema_valid'} for i in range(1, 5)]}
            (output / 'report.json').write_text(json.dumps(report))
            @contextmanager
            def proxy(video, **kwargs):
                yield video
            body = {'usage': {'prompt_tokens': 1000, 'completion_tokens': 10},
                    'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(mock_result(8))}}]}
            with patch('video_understanding.evaluation.load_local_config'), patch.dict('os.environ', {'ARK_API_KEY': 'test-only'}), \
                    patch('video_understanding.evaluation.compressed_video', side_effect=proxy), \
                    patch('video_understanding.evaluation.probe', return_value={'duration': '8'}), \
                    patch('video_understanding.evaluation.request_response', return_value={'http_status': 200, 'body': body}) as request, \
                    patch('builtins.print'):
                run(source, output, catalog, resume=True, service_tier='default')
            self.assertEqual(request.call_count, 1)
            completed = json.loads((output / 'report.json').read_text())
            self.assertEqual(len(completed['attempts']), 5)
            self.assertEqual(completed['status'], 'finished')


class AccountRecoveryTests(unittest.TestCase):
    def test_account_overdue_stops_and_requires_explicit_recovery(self):
        import json, hashlib, tempfile
        from pathlib import Path
        from contextlib import contextmanager
        from unittest.mock import patch
        from video_understanding.evaluation import run
        from video_understanding.pipeline import mock_result
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); source=root/'testset';source.mkdir()
            video=source/'clip.mp4';video.write_bytes(b'fixture')
            row={'source_relative_path':'clip.mp4','local_relative_path':'clip.mp4',
                 'sha256':hashlib.sha256(b'fixture').hexdigest(),'metadata':{'duration':'8'}}
            (source/'manifest.json').write_text(json.dumps({'files':[row]}))
            catalog=root/'catalog.json'
            catalog.write_text(json.dumps({'models':[{'model_id':'doubao-seed-2-0-mini-260428','name':'Mini','role':'baseline','input_price':.2,'output_price':2}]}))
            output=root/'results'
            @contextmanager
            def proxy(path, **kwargs):
                yield path
            with patch('video_understanding.evaluation.load_local_config'), patch.dict('os.environ',{'ARK_API_KEY':'test-only'}), patch('video_understanding.evaluation.compressed_video',side_effect=proxy), patch('video_understanding.evaluation.probe',return_value={'duration':'8'}), patch('builtins.print'), patch('video_understanding.evaluation.request_response',return_value={'http_status':403,'error_code':'AccountOverdueError'}) as request:
                run(source,output,catalog,all_samples=True)
                self.assertEqual(request.call_count,1)
                self.assertEqual(json.loads((output/'report.json').read_text())['status'],'stopped')
                with self.assertRaisesRegex(ValueError,'Account recovery'):
                    run(source,output,catalog,resume=True,all_samples=True)
                self.assertEqual(request.call_count,1)
                request.return_value={'http_status':200,'body':{'usage':{'prompt_tokens':1000,'completion_tokens':10},'choices':[{'finish_reason':'stop','message':{'content':json.dumps(mock_result(8))}}]}}
                run(source,output,catalog,resume=True,all_samples=True,retry_rejected=True)
                report=json.loads((output/'report.json').read_text())
                self.assertEqual(len(report['attempts']),2)
                self.assertEqual(report['attempts'][0]['error_code'],'AccountOverdueError')
                self.assertEqual(report['status'],'finished')

    def test_manual_mini_transport_recovery_preserves_unknown_cost(self):
        import json, hashlib, tempfile
        from pathlib import Path
        from contextlib import contextmanager
        from unittest.mock import patch
        from video_understanding.evaluation import run
        from video_understanding.pipeline import mock_result
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); source=root/'testset';source.mkdir()
            video=source/'clip.mp4';video.write_bytes(b'fixture')
            row={'source_relative_path':'clip.mp4','local_relative_path':'clip.mp4',
                 'sha256':hashlib.sha256(b'fixture').hexdigest(),'metadata':{'duration':'8'}}
            (source/'manifest.json').write_text(json.dumps({'files':[row]}))
            catalog=root/'catalog.json'
            catalog.write_text(json.dumps({'models':[{'model_id':'doubao-seed-2-0-mini-260428','name':'Mini','role':'baseline','input_price':.2,'output_price':2}]}))
            output=root/'results'
            @contextmanager
            def proxy(path, **kwargs):
                yield path
            with patch('video_understanding.evaluation.load_local_config'), patch.dict('os.environ',{'ARK_API_KEY':'test-only'}), patch('video_understanding.evaluation.compressed_video',side_effect=proxy), patch('video_understanding.evaluation.probe',return_value={'duration':'8'}), patch('builtins.print'), patch('video_understanding.evaluation.request_response',side_effect=URLError('fixture network interruption')) as request:
                run(source,output,catalog,all_samples=True)
                self.assertEqual(request.call_count,1)
                self.assertEqual(json.loads((output/'report.json').read_text())['status'],'stopped')
                with self.assertRaisesRegex(ValueError,'Ambiguous billed'):
                    run(source,output,catalog,resume=True,all_samples=True)
                self.assertEqual(request.call_count,1)
                request.side_effect=None
                request.return_value={'http_status':200,'body':{'usage':{'prompt_tokens':1000,'completion_tokens':10},'choices':[{'finish_reason':'stop','message':{'content':json.dumps(mock_result(8))}}]}}
                run(source,output,catalog,resume=True,all_samples=True,retry_mini_transport_once=True)
                report=json.loads((output/'report.json').read_text())
                self.assertEqual(len(report['attempts']),2)
                self.assertEqual(report['attempts'][0]['status'],'transport_error_cost_unknown')
                self.assertEqual(report['unknown_cost_reserve_cny'],'0.2208')
                self.assertIn('manual_retry_resolution',report['attempts'][0])
                self.assertEqual(report['attempts'][1]['manual_recovery_of_attempt_index'],0)
                self.assertEqual(report['status'],'finished')
