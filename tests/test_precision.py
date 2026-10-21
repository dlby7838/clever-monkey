import json
import unittest
from unittest.mock import patch
from material_search.precision import opening_query, opening_evidence, search_precise


def hit(text, key='a'):
    return dict(sha256=key, summary=text, description=text, actions=text,
                evidence={'case': {'raw_output': json.dumps({'actions': [text]})}})


class PrecisionTests(unittest.TestCase):
    def test_opening_intent(self):
        self.assertTrue(opening_query('打开麦片袋'))
        self.assertTrue(opening_query('撕开包装'))
        self.assertFalse(opening_query('展示已经打开的麦片袋'))
        self.assertFalse(opening_query('不要打开麦片袋'))

    def test_event_not_state(self):
        with patch('material_search.precision.chunks', return_value=[]):
            for text in ['手持已经打开的麦片袋', '手持撕开的袋子展示', '没有打开麦片袋', '打开杯盖，旁边有麦片袋']:
                self.assertIsNone(opening_evidence(hit(text)), text)
            self.assertIsNotNone(opening_evidence(hit('双手拉开麦片袋密封条')))

    def test_timed_evidence_preferred(self):
        segment=dict(text='双手拉开麦片袋密封条', start=2, end=3, channel='timeline')
        with patch('material_search.precision.chunks', return_value=[segment]):
            self.assertEqual(opening_evidence(hit(segment['text'])), segment)

    def test_wider_recall_and_no_padding(self):
        candidates=[hit('手持已经打开的麦片袋', str(i)) for i in range(25)]
        candidates.append(hit('双手拉开麦片袋密封条', 'good'))
        class Reranker:
            def score(self, query, documents): return [3.0]*len(documents)
        with patch('material_search.precision.retrieve', return_value=candidates) as retrieve, patch('material_search.precision.chunks', return_value=[]):
            rows, info=search_precise(None,None,Reranker(),'打开麦片袋')
            self.assertEqual([r['sha256'] for r in rows], ['good'])
            self.assertEqual(retrieve.call_args.kwargs['limit'],100)
            self.assertTrue(info['reranked'])

    def test_invalid_scores(self):
        class Bad:
            def score(self,*args): return [float('nan')]
        with patch('material_search.precision.retrieve',return_value=[hit('麦片')]):
            with self.assertRaises(ValueError):search_precise(None,None,Bad(),'麦片')

    def test_explicit_fallback(self):
        with patch('material_search.precision.retrieve',return_value=[hit('麦片')]):
            rows,info=search_precise(None,None,None,'麦片')
            self.assertFalse(info['reranked'])
            self.assertEqual(len(rows),1)

    def test_uncertain_general_query_preserves_recall(self):
        class Reranker:
            def score(self, query, documents): return [-4.2, -4.1]
        with patch('material_search.precision.retrieve', return_value=[hit('叠放早餐','a'),hit('餐食','b')]):
            rows,info=search_precise(None,None,Reranker(),'几份早餐摞在一起')
            self.assertEqual([r['sha256'] for r in rows],['a','b'])
            self.assertTrue(info['uncertain'])
            self.assertFalse(info['reranked'])
