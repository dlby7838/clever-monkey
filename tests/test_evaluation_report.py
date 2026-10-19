import json
from pathlib import Path
import tempfile
import unittest
from video_understanding.report import compile_reports


class ReportTests(unittest.TestCase):
    def test_usage_cost_and_proxy_comparability(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder)
            model={'model_id':'doubao-seed-2-0-mini-260428','name':'Mini','input_price':.2,'output_price':2}
            sample={'sha256':'source','source_relative_path':'clip.MOV'}
            run=base/'run';run.mkdir()
            attempt={'sample':1,'requested_model':model['model_id'],'model':'Mini','status':'machine_usable',
                     'proxy_sha256':'proxy','usage':{'prompt_tokens':4000,'completion_tokens':600},
                     'list_price_cny':'0.002','elapsed_seconds':1,'assessment':{'machine_usable':True,'structure_valid':True,'json_valid':True,'language_status':'chinese','time_status':'within_bounds'}}
            (run/'report.json').write_text(json.dumps({'status':'finished','models':[model],'samples':[sample],'attempts':[attempt]}))
            (run/'sample-1-model-1.json').write_text(json.dumps({'choices':[{'message':{'content':'{"summary":"展示麦片"}'}}]}))
            result=compile_reports([run],base/'out')
            self.assertEqual(result['summary'][0]['known_cost_cny'],'0.002')
            self.assertEqual(result['summary'][0]['machine_pass'],1)
            self.assertIsNone(result['summary'][0]['content_accuracy'])
            with self.assertRaisesRegex(ValueError,'Duplicate'):
                compile_reports([run,run],base/'duplicate')
