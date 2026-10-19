import json
import unittest
from video_understanding.assessment import assess_text
from video_understanding.pipeline import mock_result, build_prompt


class AssessmentTests(unittest.TestCase):
    def test_rounding_is_not_an_action_error(self):
        d = mock_result(3.8)
        d.update(shot_type='特写', camera_motion='固定机位')
        a = assess_text(json.dumps(d), 3.6)
        self.assertEqual(a['time_status'], 'rounding_warning')
        self.assertEqual(a['content_status'], 'not_reviewed')
        self.assertTrue(a['machine_usable'])
        self.assertEqual(d['timeline'][0]['end'], 3.8)

    def test_large_time_error_is_still_not_a_content_verdict(self):
        d = mock_result(4.1)
        a = assess_text(json.dumps(d), 3.6)
        self.assertEqual(a['time_status'], 'boundary_error')
        self.assertEqual(a['content_status'], 'not_reviewed')

    def test_schema_and_json_separate(self):
        self.assertFalse(assess_text('{bad', 4)['json_valid'])
        d = mock_result(4)
        del d['timeline'][0]['end']
        a = assess_text(json.dumps(d), 4)
        self.assertTrue(a['json_valid'])
        self.assertFalse(a['structure_valid'])
        self.assertEqual(a['time_status'], 'not_evaluated')

    def test_language_and_known_duration(self):
        d = mock_result(4)
        d['summary'] = 'English only'
        self.assertEqual(assess_text(json.dumps(d), 4)['language_status'], 'requires_review')
        prompt = build_prompt(3.6)
        self.assertIn('简体中文', prompt)
        self.assertIn('3.600000', prompt)
        self.assertIn('"end"', prompt)


    def test_other_field_error_does_not_hide_time_bounds(self):
        d = mock_result(4)
        d['subjects'] = '字符串不是数组'
        a = assess_text(json.dumps(d), 4)
        self.assertFalse(a['structure_valid'])
        self.assertEqual(a['time_status'], 'within_bounds')
        self.assertFalse(a['machine_usable'])


    def test_language_is_inspected_despite_missing_time_field(self):
        d = mock_result(4)
        d.update(shot_type='特写', camera_motion='固定')
        del d['timeline'][0]['end']
        a = assess_text(json.dumps(d), 4)
        self.assertEqual(a['language_status'], 'chinese')
        self.assertEqual(a['time_status'], 'not_evaluated')
