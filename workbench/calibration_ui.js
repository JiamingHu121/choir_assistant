/* Version-bound whole-score calibration and source review, independent of selection. */
(() => {
  'use strict';
  const ruleLabels={'beam-type-duration':'连梁与时值矛盾','omr-glyph-duration':'符号形态与时值矛盾','omr-stem-duration':'符干与连梁时值矛盾','notation-duration':'记谱与时值矛盾','tuplet-without-ratio':'连音比例缺失','verified-pattern':'相似乐句节奏复核','source-verified':'原谱证据修订','structural-review':'声部结构复核'};
  const statusLabels={proposed:'候选',pending:'待核对',accepted:'已接受',applied:'已应用','auto-applied':'已自动应用',rejected:'已拒绝','auto-eligible':'原图依据已确认',blocked:'无法自动修订'};
  Object.assign(ruleLabels,{'tuplet-ratio-removal':'误连音比例复核','compound-duplet':'复拍子二连音复核','compound-quarter-three':'复拍子三音组复核'});
  const summaryLabels={measures:'小节数',notes:'音符数',candidates:'全部候选',candidateCount:'全部候选',autoApplied:'自动修订',pending:'待核对',blocking:'阻断问题',scannedMeasures:'已扫描小节',scannedSymbols:'已扫描符号',scannedVoices:'已扫描声部段',editableCandidates:'可修订候选',reviewRequired:'需复核小节',autoEligible:'证据充分候选',appliedEdits:'已修订字段'};
  summaryLabels.appliedSymbols='已修订符号';
  const escape = value => String(value ?? '').replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
  function evidenceURL(value) {
    if (typeof value !== 'string') return null;
    if (/^\/(?:assets|generated)\/[\w\-./%]+(?:#[\w\-]+)?$/.test(value) && !value.includes('..')) return value;
    try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) ? url.href : null; }
    catch (_) { return null; }
  }
  function decisionPayload(candidate, inputs) {
    const decision = inputs.decision;
    if (!['accept','reject','uncertain'].includes(decision)) throw new Error('请选择接受、拒绝或待核对');
    const reason = String(inputs.reason || '').trim();
    const evidence = String(inputs.evidence || '').split(/\n/).map(line => line.trim()).filter(Boolean);
    if (!reason) throw new Error('请填写本次校对决策的依据');
    if (decision === 'accept') {
      if (candidate.canApply === false || !Array.isArray(candidate.edits) || !candidate.edits.length) throw new Error('此候选没有可提交的修订，请保留待核对');
      if (!inputs.sourceChecked) throw new Error('接受修订前，请核对原谱并勾选确认');
      if (!evidence.length) throw new Error('接受修订需要填写依据和原谱证据链接');
      if (evidence.some(value => !/^\/(assets|generated)\//.test(value) || !evidenceURL(value))) throw new Error('接受修订需使用本次校对包的本地原谱证据链接');
    }
    if (evidence.some(value => !evidenceURL(value))) throw new Error('证据需使用原谱/审核包链接（http、https、/assets/ 或 /generated/）');
    return {candidateId:candidate.id, decision, reason, evidence};
  }
  function initCalibrationUI({fetcher=fetch, timer=setTimeout, cancelTimer=clearTimeout}={}) {
    const host = document.getElementById('calibration-host');
    if (!host) return null;
    if (host.calibrationController) return host.calibrationController;
    host.classList.add('panel', 'calibration-panel');
    host.innerHTML = '<h2>全谱校准工作流</h2><p class="hint">扫描整份曲谱，结合逐音记谱、连线与各声部节拍提出修订候选。无法确定的地方保留原图证据，进入校对队列。</p><button id="calibration-scan" class="primary" type="button">扫描全谱并生成校准候选</button><p id="calibration-status" class="hint" role="status" aria-live="polite">正在读取校准状态…</p><div id="calibration-summary" class="calibration-summary"></div><div id="calibration-links" class="calibration-links"></div><div class="calibration-filter"><label for="calibration-filter">校对队列</label><select id="calibration-filter"><option value="all">全部候选</option><option value="pending">待核对</option><option value="accepted">已接受</option><option value="rejected">已拒绝</option></select></div><div id="calibration-queue"></div><button id="calibration-more" class="text-button" hidden type="button">显示更多候选</button><button id="calibration-submit" class="primary" type="button" hidden>保存决策并重建乐谱与音频</button><button id="calibration-reload" class="text-button" type="button" hidden>修订已发布，刷新工作台</button>';
    const element = id => document.getElementById(id);
    let state = {status:'idle', candidates:[]}, busy = false, timeout = null, epoch = 0, limit = 20, disposed = false;
    const decisions = new Map();
    const revisionLabel = revision => revision ? '版本 ' + String(revision).slice(0,12) : '尚未读取版本';
    function setStatus(message, error=false) { element('calibration-status').textContent = message; element('calibration-status').classList.toggle('is-error', error); }
    function statusClass(candidate) {
      const status = String(candidate.status || 'pending');
      return ['accepted','applied','auto-applied'].includes(status) ? 'accepted' : ['rejected','reject'].includes(status) ? 'rejected' : 'pending';
    }
    function queue() {
      const filter = element('calibration-filter').value;
      const all = (state.candidates || []).filter(candidate => filter === 'all' || statusClass(candidate) === filter);
      const visible = all.slice(0,limit);
      element('calibration-queue').innerHTML = visible.map(candidate => {
        const links = (Array.isArray(candidate.evidenceUrls) ? candidate.evidenceUrls : []).map(evidenceURL).filter(Boolean);
        const saved = decisions.get(candidate.id) || {decision:'uncertain', reason:'', evidence:links.filter(url => url.startsWith('/')).join('\n'), sourceChecked:false};
        const measures = (candidate.measureIndices || []).join('、');
        const complete = statusClass(candidate) !== 'pending';
        const proposed = candidate.proposed ?? candidate.edits;
        const actionable = state.canApply !== false && candidate.canApply !== false && Array.isArray(candidate.edits) && candidate.edits.length > 0;
        return '<details class="calibration-candidate" data-candidate-id="' + escape(candidate.id) + '"><summary><span>第 ' + escape(measures || '—') + ' 小节 · ' + escape(ruleLabels[candidate.ruleId] || candidate.ruleId || '结构复核') + '</span><small>' + escape(statusLabels[candidate.status] || candidate.status || '待核对') + '</small></summary><div class="calibration-candidate-body"><p>' + escape(candidate.reason) + '</p><div class="calibration-comparison"><span>当前</span><pre>' + escape(JSON.stringify(candidate.current ?? {},null,2)) + '</pre><span>建议</span><pre>' + escape(JSON.stringify(proposed ?? {},null,2)) + '</pre></div>' + links.map((url,index) => '<a href="' + escape(url) + '" target="_blank" rel="noopener">查看原谱 / 证据 ' + (index+1) + ' ↗</a>').join('') + (complete ? '<p class="hint">此候选已记录决策。再次扫描会依据当前版本重新判断。</p>' : '<label>决策<select data-field="decision"><option value="uncertain"' + (saved.decision === 'uncertain' ? ' selected':'') + '>待核对</option><option value="accept"' + (saved.decision === 'accept' ? ' selected':'') + (actionable ? '' : ' disabled') + '>接受修订</option><option value="reject"' + (saved.decision === 'reject' ? ' selected':'') + '>拒绝建议</option></select></label><label>校对依据<textarea data-field="reason" rows="2" placeholder="说明原谱符号及判断依据">' + escape(saved.reason) + '</textarea></label><label>原谱证据链接<textarea data-field="evidence" rows="2" placeholder="每行一个原谱或审核包链接">' + escape(saved.evidence) + '</textarea></label><label class="check"><input data-field="sourceChecked" type="checkbox"' + (saved.sourceChecked ? ' checked':'') + '>已核对原谱符号，支持上述修订</label>') + '</div></details>';
      }).join('') || '<p class="hint">此队列暂无候选。</p>';
      element('calibration-more').hidden = visible.length >= all.length;
      element('calibration-submit').hidden = state.canApply === false || !state.runId || !(state.candidates || []).some(candidate => statusClass(candidate) === 'pending');
    }
    function render(data) {
      const previousRevision = state.revision;
      const previousRun = state.runId;
      state = {...data, candidates:Array.isArray(data.candidates) ? data.candidates : []};
      if ((previousRun && previousRun !== state.runId) || (previousRevision && previousRevision !== state.revision)) { decisions.clear(); limit=20; }
      busy = state.status === 'running';
      element('calibration-scan').disabled = busy || state.status === 'read-only';
      element('calibration-submit').disabled = busy;
      const messages = {idle:'尚未扫描。扫描将检查全谱，并保存可追溯的校准报告。',running:'正在扫描或重建，请保持此页打开。',ready:'校准报告已生成。请依据原谱核对修订候选。','needs-rescan':'修订已发布。后续校对请先刷新工作台，再扫描当前版本。',failed:state.error || '校准失败，请查看报告后重试。'};
      setStatus((messages[state.status] || state.status) + ' · ' + revisionLabel(state.revision), state.status === 'failed');
      const summary = state.summary || {};
      element('calibration-summary').innerHTML = Object.entries(summary).filter(([key,value]) => summaryLabels[key] && ['number','string'].includes(typeof value)).map(([key,value]) => '<div><span>' + escape(summaryLabels[key]) + '</span><strong>' + escape(value) + '</strong></div>').join('');
      element('calibration-links').innerHTML = [[state.reportUrl,'查看全谱校准报告'],[state.packetUrl,'下载校对包']].filter(([url]) => evidenceURL(url)).map(([url,label]) => '<a href="' + escape(evidenceURL(url)) + '" target="_blank" rel="noopener">' + label + ' ↗</a>').join('');
      if (state.reload || (previousRevision && previousRevision !== state.revision)) element('calibration-reload').hidden = false;
      queue();
      if (timeout) cancelTimer(timeout);
      timeout = busy ? timer(() => refresh(),1500) : null;
    }
    async function request(body) {
      const response = await fetcher('/api/calibration', body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {cache:'no-store'});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '校准接口不可用（' + response.status + '）');
      return data;
    }
    async function refresh() {
      const token = ++epoch;
      try { const data = await request(); if (!disposed && token === epoch) render(data); }
      catch (error) { if (!disposed && token === epoch) { setStatus(error.message,true); element('calibration-scan').disabled = false; busy=false; } }
    }
    async function post(body) {
      if (busy || disposed) return;
      busy=true; element('calibration-scan').disabled=true; element('calibration-submit').disabled=true;
      const token=++epoch;
      try { const data=await request(body); if (!disposed && token === epoch) { render(data); if (data.status !== 'running') await refresh(); } }
      catch(error) { if (!disposed && token === epoch) { busy=false; element('calibration-scan').disabled=false; element('calibration-submit').disabled=false; setStatus(error.message,true); } }
    }
    element('calibration-scan').onclick = () => {
      if (!state.revision) { setStatus('尚未读取当前曲谱版本，请刷新工作台后重试。',true); return; }
      return post({action:'scan',revision:state.revision});
    };
    element('calibration-filter').onchange = () => { limit=20; queue(); };
    element('calibration-more').onclick = () => { limit+=20; queue(); };
    element('calibration-queue').addEventListener('input', event => {
      const row=event.target.closest('[data-candidate-id]'); const field=event.target.dataset.field;
      if (!row || !field) return;
      const candidate=(state.candidates || []).find(item => item.id === row.dataset.candidateId);
      const value=decisions.get(row.dataset.candidateId) || {decision:'uncertain',reason:'',evidence:(candidate?.evidenceUrls || []).map(evidenceURL).filter(url => url?.startsWith('/')).join('\n'),sourceChecked:false};
      value[field]=field === 'sourceChecked' ? event.target.checked : event.target.value;
      decisions.set(row.dataset.candidateId,value);
    });
    element('calibration-submit').onclick = () => {
      try {
        if (state.canApply === false) throw new Error('此扫描不能提交决策，请扫描当前曲谱版本');
        const changed = [...decisions].map(([id,value]) => {
          const candidate=(state.candidates || []).find(item => item.id === id);
          if (!candidate) throw new Error('候选已变更，请重新扫描');
          return decisionPayload(candidate,value);
        });
        if (!changed.length) throw new Error('请先对候选填写一项校对决策');
        return post({action:'apply',revision:state.revision,runId:state.runId,decisions:changed});
      } catch(error) { setStatus(error.message,true); }
    };
    element('calibration-reload').onclick = () => location.reload();
    document.getElementById('workbench-console')?.addEventListener('consolechange',event => { if (event.detail?.panel === 'review' && !busy) refresh(); });
    const controller={refresh, get state(){return state;}, dispose(){disposed=true;++epoch;if(timeout)cancelTimer(timeout);}};
    host.calibrationController=controller;
    refresh();
    return controller;
  }
  if (typeof module !== 'undefined' && module.exports) module.exports={decisionPayload,evidenceURL,escape,initCalibrationUI};
  if (typeof window !== 'undefined') { window.initCalibrationUI=initCalibrationUI; initCalibrationUI(); }
})();
/* Private selection drafts never mutate the published model. */
(() => {
'use strict';
const durations={'32nd':.125,'16th':.25,eighth:.5,quarter:1,half:2,whole:4};
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels={queued:'已排队',validating:'检查修改',building:'重建曲谱',rendering:'合成预览',ready_to_publish:'预览完成，可发布',publishing:'发布中',published:'已发布',failed:'失败，公共版本未变',needs_input:'需要补充说明',conflict:'版本冲突',cancelled:'已取消',blocked_capability:'生成能力不可用，公共版本未变',running:'处理中',interpreting:'解析修改说明',rebuilding:'重建曲谱',synthesizing:'合成音频',needs_confirmation:'需要确认差异'};
Object.assign(labels,{'validate-input':'检查修改','render-score':'重绘曲谱','synthesize':'合成音频','verify':'核验预览',parsed:'说明已解析',complete:'完成',preview:'生成预览'});
const finished=new Set(['ready_to_publish','published','failed','needs_input','conflict','cancelled','blocked_capability','needs_confirmation','awaiting_confirmation','proposed']);
function createDraft(model){
 const originals=new Map((model.notes||[]).map(n=>[n.id,{...n}]));let operations=[],future=[];
 function values(){const notes=new Map([...originals].map(([id,n])=>[id,{...n}]));for(const op of operations)for(const id of op.noteIds){const n=notes.get(id);if(op.op==='pitch.delta')n.pitch=Number(n.pitch)+op.semitones;else{const d=durations[op.type]*(op.dots?1.5:1);n.end=Number(n.start)+(Number(n.end)-Number(n.start))*d/Number(n.duration);n.duration=d;n.notation={...n.notation,type:op.type,dots:op.dots};}}return notes;}
 return {originals,values,get operations(){return operations.map(o=>({...o,noteIds:[...o.noteIds]}));},get canUndo(){return !!operations.length;},get canRedo(){return !!future.length;},apply(op){
 if(!Array.isArray(op.noteIds)||!op.noteIds.length||op.noteIds.some(id=>!originals.has(id)))throw Error('请选择有效音符');
 if(op.op==='pitch.delta'){const notes=values();if(![-1,1].includes(op.semitones)||op.noteIds.some(id=>{const n=notes.get(id);return n.pitch===null||!Number.isFinite(Number(n.pitch))||Number(n.pitch)+op.semitones<0||Number(n.pitch)+op.semitones>127;}))throw Error('音高操作超出范围或包含无音高对象');}
 else if(op.op!=='duration.set'||!(op.type in durations)||![0,1].includes(op.dots))throw Error('无效音值');
 operations.push({...op,noteIds:[...op.noteIds]});future=[];},undo(){if(operations.length)future.push(operations.pop());},redo(){if(future.length)operations.push(future.pop());},reset(){operations=[];future=[];},beforeValues(){const out={};for(const op of operations)for(const id of op.noteIds){out[id]||={};const n=originals.get(id);if(op.op==='pitch.delta')out[id].pitch=n.pitch;else{out[id].duration=n.duration;if(n.notation)out[id].notation=n.notation;}}return out;}};
}
function samePublishedContext(model,state,job){return !!job?.revision&&state.revision===job.revision&&(!(job.publishedHead||job.candidateHead)||state.publishedHead===(job.publishedHead||job.candidateHead))&&(!model.work||model.work===state.work)&&(!model.movement||model.movement===state.movement);}
function workerReady(worker, now=Date.now()/1000){
 return !!worker && ['idle','busy'].includes(worker.status) && worker.ready===true
  && Number.isFinite(worker.lastSeen) && worker.lastSeen>=0 && worker.lastSeen<=now
  && Number.isInteger(worker.ageSeconds) && worker.ageSeconds>=0 && worker.ageSeconds<=30
  && now-worker.lastSeen<=30;
}
function jobSummary(job){
 const v=job.validation||{},delta=v.delta||v,issues=job.newIssues||delta.newIssues,remaining=job.remainingBaselineIssues||delta.remainingBaselineIssues;
 const count=Array.isArray(job.affectedNoteIds)?job.affectedNoteIds.length:delta.appliedSymbols;
 const parts=[];
 if(job.validation){parts.push(`<p><strong>${job.canPublish===true||delta.canPublish===true?'结构检查通过（不代表原谱识别已校准）':job.canPublish===false||delta.canPublish===false?'结构检查发现新增问题，暂不可发布':'结构检查结果已保存'}</strong></p>`);if(Number.isInteger(count))parts.push(`<p>影响 ${count} 个音符 / 记号</p>`);parts.push(`<p>新增问题：${Array.isArray(issues)?issues.length:'未提供'}；原有待核对问题：${Array.isArray(remaining)?remaining.length:'未提供'}</p>`);const respellings=v.respellings||[];if(respellings.length)parts.push(`<p>等音改写 ${respellings.length} 处：${respellings.slice(0,4).map(r=>`${esc(r.before)} → ${esc(r.after)}`).join('；')}${respellings.length>4?'…':''}</p>`);parts.push(`<details class="editor-technical"><summary>技术详情</summary><pre>${esc(JSON.stringify(job.validation,null,2))}</pre></details>`);}
 const stages=[...new Set((job.events||[]).map(e=>e.stage||e.status).filter(Boolean))];
 if(stages.length)parts.push(`<details class="editor-events"><summary>处理记录（${stages.length} 个阶段）</summary><ol>${stages.map(stage=>`<li>${esc(labels[stage]||'处理阶段')} </li>`).join('')}</ol></details>`);
 return parts.join('');
}
function taskTime(value){if(value===undefined||value===null)return '时间未提供';const date=new Date(typeof value==='number'?value*1000:value);return Number.isNaN(date.getTime())?'时间未提供':date.toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});}
function taskName(job){return `${String(job.reason||job.requestText||job.text||'曲谱校对').slice(0,56)} · ${labels[job.status]||'已保存'} · ${taskTime(job.created||job.createdAt)}`;}
function pitchText(value){const midi=Number(value);return value!==null&&Number.isInteger(midi)?['C','C♯','D','D♯','E','F','F♯','G','G♯','A','A♯','B'][((midi%12)+12)%12]+(Math.floor(midi/12)-1):'无定音高';}
function durationText(value){const names={'32nd':'32分音符','16th':'16分音符',eighth:'8分音符',quarter:'4分音符',half:'2分音符',whole:'全音符'};for(const [type,units] of Object.entries(durations)){if(Math.abs(Number(value)-units)<1e-8)return names[type];if(Math.abs(Number(value)-units*1.5)<1e-8)return '附点'+names[type];}return value+' 四分拍';}
function proposalSummary(patch,model){const notes=new Map((model?.notes||[]).map(n=>[n.id,n]));return (patch.typedOperations||patch.operations||[]).map(op=>`<p>${Array.isArray(op.noteIds)?op.noteIds.length:0} 个音符（${(op.noteIds||[]).slice(0,6).map(id=>{const n=notes.get(id);return n?`${esc(n.part)} 第${esc(n.measure)}小节 MIDI ${esc(n.pitch)}`:esc(id);}).join('；')}${(op.noteIds||[]).length>6?'…':''}）：${op.op==='pitch.delta'?(op.semitones>0?'升':'降')+' '+Math.abs(op.semitones)+' 半音':op.op==='duration.set'?`设为 ${esc({'32nd':'32 分音符','16th':'16 分音符',eighth:'8 分音符',quarter:'4 分音符',half:'2 分音符',whole:'全音符'}[op.type]||op.type)}${op.dots?'（附点）':''}`:'请查看技术详情'}</p>`).join('');}
function initSelectionEditor(model,hooks={},options={}){
 const host=document.getElementById('selection-editor-host');if(!host)return null;host.editorController?.dispose();
 const fetcher=options.fetcher||fetch,timer=options.timer||setTimeout,cancelTimer=options.cancelTimer||clearTimeout;
 const draft=createDraft(model);let selected=[],state={},activeJob=null,poll=null,busy=false,disposed=false,epoch=0,basePublishedHead=null,initialContext=null,draftChangedWhileJob=false;
 const jobDrafts=new Map(),ownProposals=new Set(),publishedJobs=new Set();
 let commitBusy=false,commitJob=null;
 const signature=()=>JSON.stringify(draft.operations);
 const ownsPublishedDraft=()=>activeJob?.status==='published'&&publishedJobs.has(activeJob.id||activeJob.jobId)&&jobDrafts.get(activeJob.id||activeJob.jobId)===signature();
 const staleContext=()=>!!((state.revision&&state.revision!==model.revision)||(initialContext&&state.publishedHead!==undefined&&state.publishedHead!==basePublishedHead)||(model.work&&state.work&&model.work!==state.work)||(model.movement&&state.movement&&model.movement!==state.movement));
 host.innerHTML=`<section class="panel editor-card editor-batch" aria-label="本次修改与待发布"><h2>本次修改 / 待发布</h2><p id="editor-batch-count" class="editor-batch-count"></p><p id="editor-worker" role="status" aria-live="polite" class="hint"></p><details id="editor-diff-details" class="editor-change-list" hidden><summary id="editor-diff-toggle">查看全部修改</summary><div id="editor-diff" class="editor-diff"></div></details><p id="editor-status" role="status" aria-live="polite">分次修改会自动累计，改完后统一预览和发布。</p><div class="editor-batch-actions"><button id="editor-preview" class="primary">生成本次全部修改的预览</button><button id="editor-publish" class="primary" hidden>发布本次全部修改</button><button id="editor-reload" hidden>验证新版本并刷新</button></div><div id="editor-job"></div><details class="editor-optional"><summary>添加校对说明（可选）</summary><label class="editor-field">校对依据 / 可选署名<input id="editor-reason" maxlength="500"></label></details></section><section class="panel editor-card" aria-label="所选音符调整"><h2>所选音符校准</h2><p id="editor-selection" class="hint"></p><div class="editor-buttons"><button data-edit="down">降半音 −1</button><button data-edit="up">升半音 +1</button></div><fieldset class="editor-values"><legend>将每个所选音符设为</legend>${Object.keys(durations).map((t,i)=>`<button data-duration="${t}" type="button">${['32 分','16 分','8 分','4 分','2 分','全音符'][i]}</button>`).join('')}<label class="check"><input id="editor-dotted" type="checkbox">附点（1.5 倍）</label></fieldset><div class="editor-buttons"><button data-edit="undo">撤销</button><button data-edit="redo">重做</button><button data-edit="reset">清空本次草稿</button><button data-edit="listen">试听当前选区</button></div><p class="hint">换选区会保留前面的修改。音值按每个所选音符设置；简音试听保留原起点，完整节奏由服务器预览重建。</p></section><section class="panel editor-card" aria-label="自然语言修改"><h2>自然语言修改</h2><label class="editor-field">说明所选音符需要如何修改<textarea id="editor-request" rows="3" maxlength="4000"></textarea></label><button id="editor-send">解析修改请求</button><div id="editor-proposal"></div><button id="editor-confirm" class="primary" hidden>确认差异并入本次修改，生成全部预览</button><p class="hint">先核对建议，再明确确认。本页提出的建议会与已有草稿合并；发布仍需单独点击。其他已保存任务不会自动并入。</p></section><section class="panel editor-card" aria-label="本曲任务与历史"><h2>本曲任务与历史</h2><details class="editor-optional"><summary>当前版本信息</summary><p id="editor-version" class="hint"></p></details><div id="editor-tasks"></div><details><summary>版本历史</summary><div id="editor-history"></div><button id="editor-restore-confirm" hidden>确认发布所选历史版本的恢复</button></details><button id="editor-refresh">刷新任务 / 历史</button></section>`;
 const el=id=>host.querySelector('#'+id),status=(text,error=false)=>{el('editor-status').textContent=text;el('editor-status').classList.toggle('is-error',error);};
 function hidePublish(){if(activeJob)draftChangedWhileJob=true;el('editor-publish').hidden=true;el('editor-confirm').hidden=!(activeJob?.proposedPatch&&ownProposals.has(activeJob.id||activeJob.jobId));}
 function renderDraft(){const notes=draft.values(),picked=selected.map(id=>notes.get(id)).filter(Boolean);el('editor-selection').textContent=picked.length?`已选 ${picked.length} 个音符 · 小节 ${[...new Set(picked.map(n=>n.measure))].join('、')} · ${[...new Set(picked.map(n=>n.part))].join(' / ')}`:'先在谱面选择音符。选区不会自动切换面板。';
 const changed=[...notes.values()].filter(n=>{const o=draft.originals.get(n.id);return n.pitch!==o.pitch||n.duration!==o.duration;});el('editor-batch-count').textContent=ownsPublishedDraft()?`已发布 ${changed.length} 个音符的本次修改；请刷新载入新版本`:changed.length?`累积修改 ${changed.length} 个音符 · ${draft.operations.length} 次操作`:(activeJob?'正在查看已保存任务；本地草稿暂无修改':'本地草稿暂无修改');el('editor-publish').textContent=draft.canUndo?'发布本次全部修改':'发布此已保存预览';el('editor-preview').textContent=draftChangedWhileJob?'更新预览（包含本次全部修改）':'生成本次全部修改的预览';el('editor-diff').innerHTML=changed.length?'<strong>原值 → 本次累积草稿</strong>'+changed.map(n=>{const o=draft.originals.get(n.id);const changes=[n.pitch!==o.pitch?`音高 ${esc(o.pitchName||pitchText(o.pitch))} → ${esc(pitchText(n.pitch))}`:'',n.duration!==o.duration?`音值 ${esc(durationText(o.duration))} → ${esc(durationText(n.duration))}`:''].filter(Boolean);return `<p>${esc(({S:'女高音',A:'女低音',T:'男高音',B:'男低音',Piano:'钢琴'})[n.part]||n.part)} · 第 ${esc(n.measure)} 小节：${changes.join('；')}</p>`;}).join(''):'<p>本地草稿暂无修改；已保存任务和预览见下方。</p>';
 el('editor-diff-details').hidden=!changed.length;el('editor-diff-toggle').textContent=`查看全部修改（${changed.length} 个音符）`;el('editor-preview').hidden=ownsPublishedDraft()||(activeJob?.status==='ready_to_publish'&&!draftChangedWhileJob&&!staleContext());
 host.querySelectorAll('[data-edit], [data-duration]').forEach(b=>{const a=b.dataset.edit;b.disabled=commitBusy||(a==='undo'?!draft.canUndo:a==='redo'?!draft.canRedo:a==='reset'?!draft.canUndo:!picked.length);});el('editor-preview').disabled=busy||!changed.length||state.enabled===false||staleContext();el('editor-send').disabled=busy||commitBusy||staleContext();el('editor-preview').disabled=el('editor-preview').disabled||commitBusy;el('editor-restore-confirm').disabled=commitBusy;host.querySelectorAll('[data-job], [data-restore]').forEach(button=>button.disabled=commitBusy);el('editor-dotted').disabled=commitBusy;
 }
 function operation(op){if(commitBusy)return;try{draft.apply(op);hidePublish();renderDraft();status('修改已累计；可以继续改下一处，改完后统一生成预览。');}catch(e){status(e.message,true);}}
 host.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>{const a=b.dataset.edit;if(commitBusy)return;if(a==='up'||a==='down')return operation({op:'pitch.delta',noteIds:selected,semitones:a==='up'?1:-1});if(['undo','redo','reset'].includes(a)){draft[a]();hidePublish();renderDraft();status('本地草稿已更新；公共版本未变。');}if(a==='listen'){hooks.preview?.(selected.map(id=>draft.values().get(id)).filter(Boolean));status('简音试听使用草稿音高与时值，保持原起点；节奏重排、真实谱面与音色需生成服务器预览。');}});
 host.querySelectorAll('[data-duration]').forEach(b=>b.onclick=()=>operation({op:'duration.set',noteIds:selected,type:b.dataset.duration,dots:el('editor-dotted').checked?1:0}));
 el('editor-dotted').onchange=()=>{const notes=draft.values();const types=selected.map(id=>{const n=notes.get(id);return n.notation?.type||Object.keys(durations).find(t=>Math.abs(durations[t]*(n.notation?.dots?1.5:1)-Number(n.duration))<1e-8);});if(types.length&&types.every(t=>t===types[0]&&t in durations))operation({op:'duration.set',noteIds:selected,type:types[0],dots:el('editor-dotted').checked?1:0});else status('选区音值不同或无法推定，请点击音值按钮，附点设置会一并应用。');};
 async function request(body,query=''){if(body){let token=state.csrfToken;if(!token){const configResponse=await fetcher('/api/site-config',{cache:'no-store'});if(configResponse.ok)token=(await configResponse.json()).csrfToken;}if(token)body={...body,csrfToken:token};}const response=await fetcher('/api/editor'+query,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{cache:'no-store'});const data=await response.json();if(!response.ok)throw Error(data.error||`服务返回 ${response.status}`);return data;}
 const idempotency=()=>globalThis.crypto?.randomUUID?.()||`${Date.now()}-${Math.random().toString(36).slice(2)}`;
 function renderJob(job){if(!job)return;activeJob=job;const ident=job.id||job.jobId;draftChangedWhileJob=jobDrafts.has(ident)?jobDrafts.get(ident)!==signature():draft.canUndo&&!ownProposals.has(ident);if(Array.isArray(state.jobs))state.jobs=state.jobs.map(item=>(item.id||item.jobId)===(job.id||job.jobId)?job:item);if(ident===commitJob)commitBusy=!finished.has(job.status);busy=!finished.has(job.status);status(`${labels[job.status]||job.status||'任务已保存'} · 阶段：${labels[job.stage]||job.stage||'未提供'}${job.error?' · '+(typeof job.error==='string'?job.error:JSON.stringify(job.error)):''}`,['failed','conflict'].includes(job.status));
 el('editor-job').innerHTML=`<p class="editor-task-caption">${esc(taskName(job))}，关闭后可在任务列表恢复查看。</p>${job.needsInput?`<p>${esc(typeof job.needsInput==='string'?job.needsInput:JSON.stringify(job.needsInput))}</p>`:''}${job.reason?`<p>校对依据：${esc(job.reason)}</p>`:''}${jobSummary(job)}`;
 el('editor-proposal').innerHTML=job.proposedPatch?`<strong>待确认的修改建议</strong>${proposalSummary(job.proposedPatch,model)}<details><summary>建议技术详情</summary><pre>${esc(JSON.stringify(job.proposedPatch,null,2))}</pre></details>`:'';
 el('editor-confirm').hidden=!job.proposedPatch||!['needs_confirmation','awaiting_confirmation','proposed'].includes(job.status);el('editor-publish').hidden=job.status!=='ready_to_publish'||draftChangedWhileJob;el('editor-confirm').hidden=el('editor-confirm').hidden||draftChangedWhileJob;el('editor-confirm').disabled=staleContext()||!!job.proposedPatch&&!ownProposals.has(ident)&&draft.canUndo;el('editor-publish').disabled=staleContext()||!!(job.basePublishedEditHead!==undefined&&state.publishedHead!==undefined&&job.basePublishedEditHead!==state.publishedHead)||(model.work&&job.work&&model.work!==job.work)||(model.movement&&job.movement&&model.movement!==job.movement);if(el('editor-publish').disabled&&job.status==='ready_to_publish')status('此预览的基线已更新，请基于当前公共版本重新生成预览。',true);el('editor-reload').hidden=job.status!=='published';
 const audio=job.preview?.audio||job.preview?.audioUrl;if(audio&&/^\/(?!\/)/.test(audio)){const player=document.createElement('audio');player.controls=true;player.preload='metadata';player.src=audio;el('editor-job').append(player);}if(Array.isArray(job.preview?.pages)){const preview=document.createElement('details'),title=document.createElement('summary');title.textContent='候选谱面（尚未发布）';preview.append(title);for(const [index,page] of job.preview.pages.entries()){const url=typeof page==='string'?page:page.url||page.svg;if(!url||!/^\/(?!\/)/.test(url))continue;const image=document.createElement('img');image.src=url;image.loading='lazy';image.alt='候选谱面第 '+(index+1)+' 页';image.className='editor-preview-page';preview.append(image);}el('editor-job').append(preview);}if(job.status==='published'){status(ownsPublishedDraft()?'本次修改已发布成功，请刷新载入新版本。':draft.canUndo?'该任务已发布；本地另有未发布草稿，未合并到此任务。':'该任务已发布成功，请刷新载入新版本。');el('editor-reload').textContent='刷新并载入已发布版本';}else if(draftChangedWhileJob)status('本次累积修改已变化；旧预览不能发布，请更新全部修改的预览。');renderDraft();}
 function showWorker(){el('editor-worker').hidden=workerReady(state.worker)&&state.worker.status==='idle';const pending=!!(activeJob&&!finished.has(activeJob.status))||(state.jobs||[]).some(job=>!finished.has(job.status));el('editor-worker').textContent=workerReady(state.worker)?(state.worker.status==='busy'?'后台正在处理任务。':'后台已就绪。'):(pending?'后台暂未响应；任务已保存，恢复后自动处理。':'后台暂未响应；可继续编辑草稿和查看已保存任务。');el('editor-worker').classList.toggle('is-error',!workerReady(state.worker));}
 function schedule(){if(poll)cancelTimer(poll);if(disposed)return;if(commitBusy&&commitJob)poll=timer(()=>refresh(commitJob),1500);else if(activeJob&&!finished.has(activeJob.status))poll=timer(()=>refresh(activeJob.id||activeJob.jobId),1500);else if((state.jobs||[]).some(job=>!finished.has(job.status))||state.enabled===true&&!workerReady(state.worker))poll=timer(()=>refresh(),5000);}
 function renderState(data){state={...state,...data};if(initialContext===null&&state.publishedHead!==undefined){basePublishedHead=state.publishedHead;initialContext={work:state.work,movement:state.movement};}el('editor-version').textContent=`谱版本 ${String(model.revision||'—').slice(0,12)} · 发布头 ${String(state.publishedHead||'—').slice(0,12)}`;el('editor-tasks').innerHTML=(state.jobs||[]).map(j=>`<button data-job="${esc(j.id||j.jobId)}">${esc(taskName(j))}</button>`).join('')||'<p>尚无保存任务。</p>';el('editor-history').innerHTML=(state.history||[]).map(h=>`<div class="editor-history-row"><span>${esc(h.label||h.reason||'历史版本')} · ${esc(taskTime(h.created))}</span><button data-restore="${esc(h.editHead||h.head||h.id)}">恢复为新版本</button></div>`).join('')||'<p>尚无历史版本。</p>';host.querySelectorAll('[data-job]').forEach(b=>b.onclick=()=>{if(!commitBusy)return refresh(b.dataset.job);});host.querySelectorAll('[data-restore]').forEach(b=>b.onclick=()=>{if(commitBusy)return;const confirm=el('editor-restore-confirm');confirm.hidden=false;confirm.dataset.targetHead=b.dataset.restore;confirm.textContent='确认发布恢复到 '+b.dataset.restore.slice(0,12);status('恢复会作为新的公共版本发布，请核对所选历史记录后点击确认。');});if(data.job)renderJob(data.job);if(staleContext()&&activeJob?.status!=='published')status('公共曲谱已有新版本，请刷新后重新校对；本地草稿保留在当前页。',true);showWorker();renderDraft();schedule();}
 async function refresh(jobId){if(commitBusy){if(!commitJob||jobId&&jobId!==commitJob)return;jobId=commitJob;}const token=++epoch;try{const data=await request(null,jobId?'?jobId='+encodeURIComponent(jobId):'');if(!disposed&&token===epoch)renderState(data);}catch(e){if(!disposed&&token===epoch){busy=false;state.worker=null;status(e.message+'；已保存任务可稍后查看。',true);showWorker();renderDraft();schedule();}}}
 async function submit(action,extra={}){if(busy||commitBusy||disposed)return;const submittedSignature=signature();if(action==='publish'||action==='restore'){commitBusy=true;commitJob=null;}busy=true;renderDraft();status('正在提交，请等待服务器接收…');try{const data=await request({action,baseModelRevision:model.revision,basePublishedEditHead:basePublishedHead,selectedNoteIds:[...selected],idempotencyKey:idempotency(),...extra});if(disposed)return;const job=data.job||{id:data.jobId,...data},ident=job.id||job.jobId;if(action==='preview'||action==='publish')jobDrafts.set(ident,submittedSignature);if(action==='publish')publishedJobs.add(ident);if(action==='publish'||action==='restore')commitJob=ident;if(action==='request')ownProposals.add(ident);draftChangedWhileJob=false;renderJob(job);await refresh(activeJob?.id||activeJob?.jobId);}catch(e){busy=false;commitBusy=false;status(e.message,true);renderDraft();}}
 const submitDraft=()=>{if(staleContext()){status('公共版本已变化，请刷新后重新校对；当前草稿保留在本页。',true);return;}return submit('preview',{typedOperations:draft.operations,selectedNoteIds:[...new Set(draft.operations.flatMap(op=>op.noteIds))],beforeValues:draft.beforeValues(),reason:el('editor-reason').value.trim()||'选区人工校对'});};el('editor-preview').onclick=submitDraft;el('editor-send').onclick=()=>{const text=el('editor-request').value.trim();if(!text){status('请填写修改说明。',true);return;}return submit('request',{text});};el('editor-confirm').onclick=()=>{if(busy||commitBusy||!activeJob?.proposedPatch)return;const ident=activeJob.id||activeJob.jobId;if(!ownProposals.has(ident)){if(draft.canUndo){status('请在本页重新提出请求后，将确认的建议并入当前草稿。',true);return;}return submit('confirm',{jobId:ident});}try{const patch=activeJob.proposedPatch;if(state.revision&&state.revision!==model.revision||patch.basePublishedEditHead!==undefined&&state.publishedHead!==undefined&&patch.basePublishedEditHead!==state.publishedHead)throw Error('公共版本已更新，请刷新后重新解析建议');if(patch.baseModelRevision&&patch.baseModelRevision!==model.revision)throw Error('建议版本已过期，请重新解析');const operations=patch.typedOperations||patch.operations;if(!Array.isArray(operations)||!operations.length)throw Error('没有可并入的结构化修改');const occupied=new Set(draft.operations.flatMap(op=>op.noteIds.map(id=>id+'|'+op.op)));if(operations.some(op=>(op.noteIds||[]).some(id=>occupied.has(id+'|'+op.op))))throw Error('建议与本地草稿修改了相同音符的同一参数；请先撤销冲突修改或调整建议，再确认，避免重复叠加。');const check=createDraft(model);for(const op of [...draft.operations,...operations])check.apply(op);for(const op of operations)draft.apply(op);ownProposals.delete(ident);hidePublish();renderDraft();return submitDraft();}catch(e){status(e.message,true);}};el('editor-publish').onclick=()=>{if(draftChangedWhileJob||el('editor-publish').hidden||el('editor-publish').disabled)return;return submit('publish',{jobId:activeJob?.id||activeJob?.jobId});};el('editor-refresh').onclick=()=>refresh();el('editor-restore-confirm').onclick=()=>submit('restore',{targetHead:el('editor-restore-confirm').dataset.targetHead});el('editor-reload').onclick=async()=>{try{const latest=await request();if(!samePublishedContext({...model,...initialContext},latest,activeJob))throw Error('尚未验证同曲目的已发布版本，请稍后刷新任务');location.reload();}catch(e){status(e.message,true);}};
 const controller={setSelection(ids){selected=[...ids].filter(id=>draft.originals.has(id));renderDraft();},refresh,get draft(){return draft;},dispose(){disposed=true;++epoch;if(poll)cancelTimer(poll);}};host.editorController=controller;renderDraft();refresh();return controller;
}
if(typeof module!=='undefined'&&module.exports)Object.assign(module.exports,{createDraft,samePublishedContext,workerReady,jobSummary,initSelectionEditor});if(typeof window!=='undefined')window.initSelectionEditor=initSelectionEditor;
})();
