"""Retrieval correctness using controlled vectors, no network/model download in tests."""
import json
from pathlib import Path
import tempfile
import unittest

from material_search.semantic import index, retrieve, SemanticUnavailable, pouring_prior
from material_search.store import connect


class Encoder:
    identity = 'test-model:fixed-revision'
    def __init__(self): self.calls=0
    def documents(self,texts):
        self.calls += len(texts)
        return [[1,0,0] if '购物车' in t else [0,1,0] if '舀' in t else [0,0,1] for t in texts]
    def query(self,text): return [1,0,0] if '采购' in text else [0,1,0] if '盛起' in text else [-1,-1,-1]


class SemanticTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.db=connect(Path(self.temp.name).resolve()/'search.sqlite');self.addCleanup(self.db.close)
        for i,(summary,action,ocr) in enumerate([('放到购物车','放入购物车',''),('碗里的酸奶','舀起酸奶',''),('普通包装','展示','购物车')],1):
            sha=str(i)*64
            self.db.execute('INSERT INTO search_assets VALUES(?,?)',(sha,'{}'))
            raw=json.dumps({'summary':summary,'actions':[action],'timeline':[{'start':1,'end':2,'description':action}]})
            evidence=json.dumps({'case':{'raw_output':raw},'sample':{'source_relative_path':f'{i}.mov'}})
            self.db.execute('''INSERT INTO search_analyses(id,sha256,evidence_hash,model,summary,subjects,actions,
                            description,ocr,duration,review_state,evidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                            (i,sha,str(i),'lite',summary,'人物',action,'背景购物车',ocr,i*3,'not_reviewed',evidence))
        self.db.commit();self.encoder=Encoder()

    def test_dense_recall_without_literal_query_and_incremental_index(self):
        first=index(self.db,self.encoder);calls=self.encoder.calls
        self.assertEqual(first['embedded'],7)
        self.assertEqual(index(self.db,self.encoder)['embedded'],0)
        self.assertEqual(self.encoder.calls,calls)
        self.assertEqual(retrieve(self.db,self.encoder,'采购',mode='semantic')[0]['id'],1)
        self.assertEqual(retrieve(self.db,self.encoder,'盛起',mode='hybrid')[0]['id'],2)
        self.assertEqual(retrieve(self.db,self.encoder,'采购',mode='keyword'),[])
        self.assertEqual(retrieve(self.db,self.encoder,'毫不相关'),[])
        self.assertEqual(retrieve(self.db,self.encoder,'采购',min_duration=4),[])
        self.assertEqual(retrieve(self.db,self.encoder,'采购',include_ocr=True)[1]['id'],3)

    def test_missing_index_model_mismatch_and_new_revision(self):
        with self.assertRaises(SemanticUnavailable): retrieve(self.db,None,'采购')
        with self.assertRaises(SemanticUnavailable): retrieve(self.db,self.encoder,'采购')
        index(self.db,self.encoder)
        self.encoder.identity='different-model'
        with self.assertRaises(SemanticUnavailable): retrieve(self.db,self.encoder,'采购')
        self.encoder.identity='test-model:fixed-revision'
        self.db.execute('''INSERT INTO search_analyses SELECT 4,sha256,'new-evidence',model,summary,subjects,
                         actions,description,ocr,duration,review_state,evidence FROM search_analyses WHERE id=1''')
        self.db.commit()
        with self.assertRaises(SemanticUnavailable): retrieve(self.db,self.encoder,'采购')
        index(self.db,self.encoder)
        self.assertEqual(retrieve(self.db,self.encoder,'采购')[0]['id'],4)

    def test_filter_validation_dimension_mismatch(self):
        index(self.db,self.encoder)
        with self.assertRaises(ValueError): retrieve(self.db,self.encoder,'采购',mode='bogus')
        with self.assertRaises(ValueError): retrieve(self.db,self.encoder,'采购',limit=0)
        self.db.execute('UPDATE search_vectors SET dimensions=10')
        with self.assertRaises(SemanticUnavailable): retrieve(self.db,self.encoder,'采购')


    def test_pouring_direction_lexical_prior_does_not_replace_dense_retrieval(self):
        def hit(actions):
            return {'evidence': {'case': {'raw_output': json.dumps({'actions':actions})}}}
        self.assertGreater(pouring_prior('把麦片倒进碗里',hit(['将混合燕麦倒入碗中'])),0)
        self.assertLess(pouring_prior('把麦片倒进碗里',hit(['倾倒白色浓稠乳品'])),0)
        self.assertEqual(pouring_prior('展示麦片',hit(['将混合燕麦倒入碗中'])),0)
        self.assertEqual(pouring_prior('把麦片倒进碗里',hit(['展示包装'])),0)
