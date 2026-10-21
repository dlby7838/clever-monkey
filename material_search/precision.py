"""Evidence-aware candidate filtering and local cross-encoder precision ranking."""
from collections import OrderedDict
import hashlib
import json
import math
from pathlib import Path
import re
import threading

from nas_scanner.safety import local_output
from .semantic import SemanticUnavailable, retrieve, chunks

MODEL = 'BAAI/bge-reranker-base'
PROFILE = 'query-document-opening-evidence-v1'


class LocalReranker:
    def __init__(self, cache_dir='data/models', download=False):
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
        except ImportError as error:
            raise SemanticUnavailable('请安装semantic依赖后准备精排模型') from error
        cache=local_output(Path(cache_dir));cache.mkdir(parents=True,exist_ok=True)
        try:
            self.model=TextCrossEncoder(MODEL,cache_dir=str(cache),threads=2,local_files_only=not download)
        except Exception as error:
            raise SemanticUnavailable('精排模型未准备好，可运行 material-search prepare-reranker') from error
        digest=hashlib.sha256()
        for name in ('onnx/model.onnx','tokenizer.json','tokenizer_config.json'):
            path=Path(self.model.model._model_dir)/name
            if path.is_file():
                with path.open('rb') as stream:digest.update(hashlib.file_digest(stream,'sha256').digest())
        self.identity=MODEL+':'+PROFILE+':'+digest.hexdigest()
        self.lock=threading.Lock();self.cache=OrderedDict()

    def score(self,query,documents):
        key=hashlib.sha256(json.dumps([query,documents],ensure_ascii=False).encode()).hexdigest()
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key][:]
            scores=list(map(float,self.model.rerank(query,documents,batch_size=4)))
            if len(scores)!=len(documents) or not all(math.isfinite(s) for s in scores):
                raise ValueError('精排模型输出无效')
            self.cache[key]=scores
            while len(self.cache)>64:self.cache.popitem(last=False)
            return scores[:]


OPEN_VERBS = r'打开|撕开|拆开|拉开|掀开|扯开|揭开'
BAG_OBJECT = r'包装袋|麦片袋|燕麦袋|袋口|袋子|袋身|自封袋|密封条|自封拉链|封条|食品袋'
NEGATED = r'未|没有|没能|并非|不是|不曾|无法确认|无法判断|看不清|不确定|未拍摄'


def opening_query(query):
    # State requests ("展示已打开的袋子") and negative requests are not opening-action requests.
    if re.search(r'已(?:经)?打开|打开的|开口的|未打开|不要|没有|不需要|不打开',query):
        return False
    return bool((re.search(OPEN_VERBS,query) and re.search(r'袋|包装|密封条|拉链',query))
                or re.search(r'开袋|拆封|开封',query))


def opening_evidence(hit):
    """Return a grounded text span, not an inference that a visible open bag was opened."""
    try:parsed=json.loads(hit['evidence']['case']['raw_output'])
    except (ValueError,TypeError,KeyError):parsed={}
    if not isinstance(parsed,dict):parsed={}
    actions=parsed.get('actions',[])
    fields=[(a,None,None,'actions') for a in actions if isinstance(a,str)] if isinstance(actions,list) else []
    fields = [(c['text'],c['start'],c['end'],c['channel']) for c in chunks(hit) if c['channel']=='timeline'] + fields
    fields += [(hit['summary'],None,None,'summary'),(hit['description'],None,None,'visual_description')]
    for text,start,end,channel in fields:
        for clause in re.split(r'[。；;\n]',text):
            for verb in re.finditer(OPEN_VERBS,clause):
                before=clause[max(0,verb.start()-14):verb.start()]
                after=clause[verb.end():verb.end()+22]
                # Reject attributive/static/negative language, including "手持撕开的袋子".
                if re.search(NEGATED,before) or re.match(r'的|状态|状',after):continue
                if re.search(r'已(?:经)?[^，,]{0,5}$',before):continue
                window=before+verb.group()+after
                if not re.search(BAG_OBJECT,window):continue
                if re.match(r'(?:杯|瓶|罐|盒|电脑|电视)',after):continue
                if re.search(r'无法|未见|未拍摄|不确定',clause):continue
                return {'text':clause.strip(),'start':start,'end':end,'channel':channel}
    return None


def document(hit,evidence=None):
    # Query-relevant evidence first; keep full underlying descriptions in the original record.
    match=evidence or hit.get('match') or {}
    return '\n'.join(('匹配记录：'+str(match.get('text',''))[:220],
                      '摘要：'+hit['summary'][:120], '实际动作：'+hit['actions'][:200],
                      '画面描述：'+hit['description'][:140]))


def search_precise(db,encoder,reranker,query,limit=20,mode='hybrid',**filters):
    if mode!='hybrid' or not query.strip():
        return retrieve(db,encoder,query,limit=limit,mode=mode,**filters),{'engine':mode,'reranked':False}
    # Widen recall before filtering; never restrict precision work to the old displayed top 20.
    candidates=retrieve(db,encoder,query,limit=100,mode=mode,**filters)
    before=len(candidates);is_opening=opening_query(query)
    kept=[]
    for hit in candidates:
        evidence=opening_evidence(hit) if is_opening else None
        if is_opening and evidence is None:continue
        if evidence:
            hit['action_evidence']=evidence
            # Don't show a static "已打开" segment as the reason for an opening action match.
            hit['match']=evidence
        kept.append(hit)
    candidates=kept[:50]
    info={'engine':'evidence+reranker' if reranker else 'evidence+hybrid',
          'reranked':reranker is not None,'recalled':before,'eligible':len(kept),
          'action_filter':'opening' if is_opening else None}
    if not candidates:return [],info
    if reranker is None:return candidates[:limit],info
    scores=reranker.score(query,[document(h,h.get('action_evidence')) for h in candidates])
    if len(scores)!=len(candidates) or not all(math.isfinite(s) for s in scores):
        raise ValueError('精排结果数量或分值无效')
    # Negative logits are not calibrated rejection probabilities. Preserve semantic recall
    # on uncertain general queries; an opening query already has explicit event evidence.
    if not is_opening and max(scores) < 0:
        info['uncertain'] = True
        info['reranked'] = False
        info['engine'] = 'hybrid-uncertain'
        return candidates[:limit], info
    ranked=[]
    for hit,score in zip(candidates,scores):
        # Raw cross-encoder logits, not probabilities. Initial floor; not a validated accuracy metric.
        if score < (-4 if is_opening else 0):continue
        hit['rerank_score']=round(score,5);hit['rank']=-score
        hit['reranker']=getattr(reranker,'identity',MODEL)
        hit['retrieval_mode']='hybrid'
        ranked.append(hit)
    return sorted(ranked,key=lambda h:(h['rank'],h['sha256']))[:limit],info
