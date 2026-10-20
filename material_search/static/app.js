const $ = (id) => document.getElementById(id);
let token, selected, videoUrl, activeQuery = '', activeFilters = {}, previewRequest, searchVersion = 0, feedbackTotal = 0;
const form = $('search-form');
function node(tag, text, cls) { const e = document.createElement(tag); if(text !== undefined) e.textContent = text; if(cls) e.className = cls; return e; }
async function api(url, options = {}) {
  const response = await fetch(url, {...options, headers: {'X-Preview-Token': token || '', ...options.headers}});
  if(!response.ok) { const data = await response.json(); throw new Error(data.error || '请求失败'); }
  return response;
}
function clearPreview() {
  if(previewRequest) previewRequest.abort();
  previewRequest = null;
  const old = $('preview-video'); if(old) { old.pause(); old.removeAttribute('src'); old.load(); }
  if(videoUrl) URL.revokeObjectURL(videoUrl);
  videoUrl = null;
}
function filename(hit) { return hit.evidence.sample.source_relative_path; }
function parsed(hit) { try { const p = JSON.parse(hit.evidence.case.raw_output); return p && typeof p === 'object' ? p : {}; } catch { return {}; } }
function select(hit, button) {
  clearPreview(); selected = hit.id;
  document.querySelectorAll('.result').forEach(e => {e.classList.toggle('selected',e === button); e.setAttribute('aria-pressed',String(e === button));});
  const pane = $('detail'); pane.replaceChildren();
  const area = node('div', undefined, 'video-area'); const play = node('button', '准备视频预览 ▷', 'primary');
  area.append(play, node('p', '点击后准备720p本地预览，无音频。\n预览准备完成即删除临时文件。')); pane.append(area);
  play.onclick = async () => {
    play.disabled = true; play.textContent = '正在准备预览…';
    const controller = new AbortController(); previewRequest = controller;
    try {
      const response = await api(`/api/preview?id=${hit.id}`, {signal: controller.signal});
      const blob = await response.blob(); if(controller.signal.aborted || selected !== hit.id) return;
      videoUrl = URL.createObjectURL(blob);
      const video = node('video'); video.id = 'preview-video'; video.controls = true; video.playsInline = true; video.preload = 'metadata'; video.src = videoUrl;
      area.replaceChildren(video, node('p', '本地预览 · 无音频 · 点击时间段可跳转'));
      video.addEventListener('error', () => area.append(node('p', '浏览器未能播放此预览，请重新选择素材重试。')));
    } catch(error) { if(error.name !== 'AbortError' && selected === hit.id) {play.disabled=false;play.textContent='重新准备预览'; area.lastChild.textContent=error.message;} }
  };
  const body = node('div', undefined, 'detail-body'); pane.append(body);
  body.append(node('h3', filename(hit)),node('p', hit.summary || '此素材暂缺可用摘要。'));
  const p = parsed(hit);
  body.append(node('div', '动作与时间段', 'section-label'));
  const timeline = node('div', undefined, 'timeline');
  const segments = Array.isArray(p.timeline) ? p.timeline : [];
  if(!segments.length) timeline.append(node('p', '暂无时间段记录。'));
  for(const segment of segments) {
    if(!segment || typeof segment !== 'object') continue;
    const valid = Number.isFinite(segment.start) && Number.isFinite(segment.end) && segment.start >= 0 && segment.end > segment.start;
    const t = node('button'); t.type='button'; t.disabled=!valid;
    t.append(node('span',`${segment.start}–${segment.end} 秒${valid ? '' : ' · 时间待复核'}`),node('div',segment.description || segment.action || '未提供描述'));
    t.onclick = () => {const video = $('preview-video'); if(valid && video && Number.isFinite(video.duration)) video.currentTime=Math.min(segment.start,video.duration);};
    timeline.append(t);
  }
  body.append(timeline);
  const raw = node('details'); raw.append(node('summary','原始分析与校验记录'),node('pre',JSON.stringify(hit.evidence,null,2))); body.append(raw);
  const feedback = node('div', undefined, 'feedback'); feedback.append(node('h3','这个镜头符合搜索需求吗？'));
  const feedbackQuery = activeQuery; const feedbackFilters = {...activeFilters};
  feedback.append(node('p',feedbackQuery ? `对应查询：${feedbackQuery}` : '当前为浏览素材；反馈将按空查询保存，不计入特定检索需求。'));
  const verdict = node('select'); verdict.setAttribute('aria-label','相关性判断');
  [['relevant','符合需求'],['irrelevant','不符合需求'],['unsure','暂不确定']].forEach(([v,t])=>{const o=node('option',t);o.value=v;verdict.append(o);});
  verdict.value='unsure';
  const note=node('textarea');note.placeholder='可选：缺少哪个动作，或哪里描述有误？';note.maxLength=2000;note.setAttribute('aria-label','反馈备注');
  const save=node('button','保存反馈','primary');save.type='button';const state=node('div','', 'feedback-status');state.setAttribute('role','status');
  save.onclick=async()=>{save.disabled=true;try {await api('/api/feedback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({analysis_id:hit.id,query:feedbackQuery,filters:feedbackFilters,verdict:verdict.value,note:note.value})});state.textContent='反馈已保存，模型原文未修改。';feedbackTotal++;$('feedback-count').textContent=`已记录 ${feedbackTotal} 条人工反馈`;}catch(e){state.textContent=e.message;}finally{save.disabled=false;}};
  feedback.append(verdict,note,save,state);body.append(feedback);
}
async function runSearch(event) {
  if(event) event.preventDefault();
  const version=++searchVersion; const params = new URLSearchParams(new FormData(form));
  $('status').textContent='正在检索…';
  try {
    const data=await (await api('/api/search?'+params)).json(); if(version !== searchVersion) return;
    activeQuery=params.get('q') || ''; activeFilters=Object.fromEntries([...params].filter(([k])=>k!=='q'));
    clearPreview();selected=null;$('results').replaceChildren();
    feedbackTotal=data.feedback_count; $('assets').textContent=data.assets; $('feedback-count').textContent=`已记录 ${data.feedback_count} 条人工反馈`;
    $('result-heading').textContent=`${activeQuery ? '检索结果' : '素材列表'} · ${data.results.length}${data.results.length===100?'（最多显示100条）':''}`;
    $('status').textContent='';
    if(!data.results.length) {
      const empty=node('div',undefined,'placeholder');empty.append(node('h3','没有找到匹配素材'),node('p','减少关键词或放宽筛选条件再试一次。'));$('results').append(empty);
      $('detail').replaceChildren(node('div','暂无可预览的结果。','placeholder'));return;
    }
    for(const hit of data.results) {
      const button=node('button',undefined,'result');button.type='button';
      const top=node('div',undefined,'result-top');top.append(node('span',filename(hit),'file'),node('span',`${Number(hit.duration || 0).toFixed(1)} 秒`,'duration'));
      const tags=node('div',undefined,'tags'); tags.append(node('span',hit.review_state==='needs_review'?'机械校验待复核':'内容尚未人工验收',hit.review_state==='needs_review'?'tag warn':'tag'));
      const info=parsed(hit); if(typeof info.shot_type==='string') tags.append(node('span',info.shot_type,'tag'));
      button.append(top,node('p',hit.summary || '暂无摘要'),tags);button.onclick=()=>select(hit,button);$('results').append(button);
    }
    select(data.results[0],$('results').firstChild);
  } catch(error) { if(version===searchVersion) $('status').textContent=error.message; }
}
form.addEventListener('submit',runSearch);
$('export').onclick=async()=>{try{const blob=await(await api('/api/feedback')).blob();const url=URL.createObjectURL(blob);const a=node('a');a.href=url;a.download='素材检索反馈.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){$('status').textContent=e.message;}};
window.addEventListener('pagehide',clearPreview);
api('/api/session').then(r=>r.json()).then(data=>{token=data.token;return runSearch();}).catch(e=>$('status').textContent=e.message);
