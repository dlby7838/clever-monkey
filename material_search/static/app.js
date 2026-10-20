const $ = (id) => document.getElementById(id);
let token, selected, activeQuery = '', searchVersion = 0;
const form = $('search-form');
function node(tag, text, cls) { const e = document.createElement(tag); if(text !== undefined) e.textContent = text; if(cls) e.className = cls; return e; }
async function api(url, options = {}) {
  const response = await fetch(url, {...options, headers: {'X-Preview-Token': token || '', ...options.headers}});
  if(!response.ok) { const data = await response.json(); throw new Error(data.error || '请求失败'); }
  return response;
}
function clearPreview() {
  const old = $('preview-video'); if(old) { old.pause(); old.removeAttribute('src'); old.load(); }
}
function filename(hit) { return hit.evidence.sample.source_relative_path; }
function parsed(hit) { try { const p = JSON.parse(hit.evidence.case.raw_output); return p && typeof p === 'object' ? p : {}; } catch { return {}; } }
function select(hit, button) {
  clearPreview(); selected = hit.id;
  document.querySelectorAll('.result').forEach(e => {e.classList.toggle('selected',e === button); e.setAttribute('aria-pressed',String(e === button));});
  const pane = $('detail'); pane.replaceChildren();
  const area = node('div', undefined, 'video-area'); const play = node('button', '播放预览 ▷', 'primary');
  area.append(play, node('p', '流式快速预览 · 480p · 无音频 · 不生成磁盘副本')); pane.append(area);
  function loadPreview(start=0) {
    clearPreview();
    const requestedAt=performance.now();
    const video = node('video'); video.id='preview-video'; video.controls=true; video.playsInline=true;
    video.preload='auto'; video.autoplay=true;
    const state=node('p', '正在加载首帧…');
    video.src=`/api/preview?id=${hit.id}&token=${encodeURIComponent(token)}&start=${start}`;
    area.replaceChildren(video,state);
    video.addEventListener('loadeddata',()=>{video.dataset.readyMs=(performance.now()-requestedAt).toFixed(1);state.textContent=`从原片 ${start.toFixed(1)} 秒开始 · 无音频 · 选择时间段可快速跳转`;});
    video.addEventListener('timeupdate',()=>{state.textContent=`原片 ${(start+video.currentTime).toFixed(1)} / ${Number(hit.duration || 0).toFixed(1)} 秒 · 无音频`;});
    video.addEventListener('error',()=>{state.textContent='预览未能加载，请重试；本地副本可能暂不可用。';const retry=node('button','重试预览','primary');retry.onclick=()=>loadPreview(start);area.append(retry);});
    video.play().catch(()=>{});
  }
  play.onclick = () => loadPreview();
  const body = node('div', undefined, 'detail-body'); pane.append(body);
  body.append(node('h3', filename(hit)),node('p', hit.summary || '此素材暂缺可用摘要。'));
  if(hit.match && hit.match.channel === 'timeline') {
    body.append(node('div','匹配到的内容','section-label'),node('p',hit.match.text));
  }
  const p = parsed(hit);
  body.append(node('div', '动作与时间段', 'section-label'));
  const timeline = node('div', undefined, 'timeline');
  const segments = Array.isArray(p.timeline) ? p.timeline : [];
  if(!segments.length) timeline.append(node('p', '暂无时间段记录。'));
  for(const segment of segments) {
    if(!segment || typeof segment !== 'object') continue;
    const valid = Number.isFinite(segment.start) && Number.isFinite(segment.end) && segment.start >= 0 && segment.end > segment.start;
    const t = node('button'); t.type='button'; t.disabled=!valid;
    t.append(node('span',`${segment.start}–${segment.end} 秒${valid ? '' : ' · 时间记录异常'}`),node('div',segment.description || segment.action || '未提供描述'));
    t.onclick = () => {if(valid) loadPreview(Math.min(segment.start, Math.max(0,hit.duration-0.05)));};
    timeline.append(t);
  }
  body.append(timeline);
  const raw = node('details'); raw.append(node('summary','原始分析与校验记录'),node('pre',JSON.stringify(hit.evidence,null,2))); body.append(raw);

}
async function runSearch(event) {
  if(event) event.preventDefault();
  const version=++searchVersion; const params = new URLSearchParams(new FormData(form));
  $('status').textContent='正在检索…';
  try {
    const data=await (await api('/api/search?'+params)).json(); if(version !== searchVersion) return;
    activeQuery=params.get('q') || '';
    clearPreview();selected=null;$('results').replaceChildren();
    $('assets').textContent=data.assets; $('search-mode').textContent=({hybrid:'语义＋关键词',semantic:'语义检索',keyword:'关键词检索'})[data.mode] || data.mode;
    $('result-heading').textContent=`${activeQuery ? '检索结果' : '素材列表'} · ${data.results.length}${data.results.length===100?'（最多显示100条）':''}`;
    $('status').textContent='';
    if(!data.results.length) {
      const empty=node('div',undefined,'placeholder');empty.append(node('h3','没有找到匹配素材'),node('p','减少关键词或放宽筛选条件再试一次。'));$('results').append(empty);
      $('detail').replaceChildren(node('div','暂无可预览的结果。','placeholder'));return;
    }
    for(const hit of data.results) {
      const button=node('button',undefined,'result');button.type='button';
      const top=node('div',undefined,'result-top');top.append(node('span',filename(hit),'file'),node('span',`${Number(hit.duration || 0).toFixed(1)} 秒`,'duration'));
      const tags=node('div',undefined,'tags'); tags.append(node('span',hit.review_state==='needs_review'?'基础校验异常':'基础校验通过',hit.review_state==='needs_review'?'tag warn':'tag'));
      const info=parsed(hit); if(typeof info.shot_type==='string') tags.append(node('span',info.shot_type,'tag'));
      if(hit.match && hit.match.start !== null) tags.append(node('span',`匹配片段 ${hit.match.start.toFixed(1)} 秒`,'tag'));
      button.append(top,node('p',hit.summary || '暂无摘要'),tags);button.onclick=()=>select(hit,button);$('results').append(button);
    }
    select(data.results[0],$('results').firstChild);
  } catch(error) { if(version===searchVersion) $('status').textContent=error.message; }
}
form.addEventListener('submit',runSearch);
window.addEventListener('pagehide',clearPreview);
api('/api/session').then(r=>r.json()).then(data=>{token=data.token;if(!data.semantic_ready) $('status').textContent=data.semantic_error || '语义模型尚未准备好';return runSearch();}).catch(e=>$('status').textContent=e.message);
