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
      element('calibration-scan').disabled = busy;
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
