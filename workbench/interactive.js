/* The same canonical note IDs connect notation, selection, audio, and corrections. */
(() => {
  'use strict';

  const node = (tag, className, text) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  };
  const seconds = value => `${Number(value || 0).toFixed(2)} 秒`;
  const midiName = pitch => {
    const n = Number(pitch);
    return Number.isFinite(n) ? `${['C', 'C♯', 'D', 'E♭', 'E', 'F', 'F♯', 'G', 'A♭', 'A', 'B♭', 'B'][((n % 12) + 12) % 12]}${Math.floor(n / 12) - 1}` : '—';
  };

  window.initInteractive = async function initInteractive(data, hooks = {}) {
    const host = document.getElementById('interactive-host');
    if (!host) throw new Error('缺少互动谱容器');
    let model = data;
    let notes = new Map();
    const elements = new Map();
    let selected = new Set();
    let sounding = new Set();
    let view = 'notation';
    let busy = false;
    let drag = null;
    let touch = null;
    let previewContext;
    let previewVoice;
    let lastFollowed = '';
    let svgLoadToken = 0;

    host.className = 'interactive-host';
    host.replaceChildren();
    const toolbar = node('div', 'interactive-toolbar');
    const tabs = node('div', 'notation-tabs');
    tabs.setAttribute('role', 'group');
    tabs.setAttribute('aria-label', '谱面视图');
    const notationTab = node('button', 'notation-tab is-current', '互动曲谱');
    const originalTab = node('button', 'notation-tab', '原 PDF 对照');
    if(hooks.originalAvailable===false){originalTab.disabled=true;originalTab.title='此导入未提供可定位的原 PDF';}
    tabs.append(notationTab, originalTab);
    const badge = node('span', 'notation-badge');
    toolbar.append(tabs, badge);
    const hint = node('div', 'notation-guide', '单击音符快速试听 · 拖动框选 · Shift / ⌘ 加选 · 触屏可点选');
    const workspace = node('div', 'notation-workspace');
    const stage = node('div', 'notation-stage');
    stage.id = 'interactive-stage';
    stage.tabIndex = 0;
    stage.setAttribute('role', 'region');
    stage.setAttribute('aria-label', '可点击、框选和播放跟随的结构化曲谱');
    const pages = node('div', 'notation-pages');
    const dragBox = node('div', 'notation-drag-box');
    dragBox.hidden = true;
    stage.append(pages, dragBox);
    workspace.append(stage);

    const controls = node('div', 'notation-selection-panel');
    const summary = node('strong', 'notation-selection-summary', '还未选择音符');
    const selectionDescription = node('span', 'notation-selection-description', '绿色表示正在发声，橙色表示所选音符。');
    const actions = node('div', 'notation-actions');
    const playSelected = node('button', 'notation-button primary-action', '合成并播放所选');
    const fromHere = node('button', 'notation-button', '从这里播放全曲');
    const compare = node('button', 'notation-button', '看对应原谱');
    if(hooks.originalAvailable===false)compare.disabled=true;
    const clear = node('button', 'notation-button', '清除选择');
    const full = node('button', 'notation-button', '恢复全曲音频');
    actions.append(playSelected, fromHere, compare, clear, full);
    const rangeBar = node('div', 'notation-range-bar');
    const rangeStart = node('input');
    const rangeEnd = node('input');
    for (const [input, label] of [[rangeStart, '框选起始小节'], [rangeEnd, '框选结束小节']]) {
      input.type = 'number'; input.min = '1'; input.value = '1'; input.setAttribute('aria-label', label);
    }
    const partChoice = node('select');
    partChoice.setAttribute('aria-label', '选择声部');
    const rangeButton = node('button', 'notation-button', '选这些小节');
    rangeBar.append(node('span', '', '跨行 / 跨页选择'), rangeStart, node('span', '', '—'), rangeEnd, partChoice, rangeButton);
    const selectionList = node('div', 'notation-selected-list');
    const status = node('p', 'notation-status', '快速试听使用简音；合成所选使用当前音色、移调和声部音量，保留相对时间与休止。');
    status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    const downloads = node('div', 'notation-downloads');
    controls.append(summary, selectionDescription, actions, rangeBar, selectionList, status, downloads);

    const editor = node('details', 'notation-editor');
    const editHeading = node('summary', '', '人工校对：修改一个音符');
    const editBody = node('div', 'notation-edit-body');
    const editInfo = node('p', 'notation-edit-info', '先在谱面只选择一个音符。');
    const editPitch = node('input'); editPitch.type = 'number'; editPitch.min = '0'; editPitch.max = '127'; editPitch.step = '1';
    const editDuration = node('input'); editDuration.type = 'number'; editDuration.min = '0.03125'; editDuration.max = '16'; editDuration.step = 'any';
    const editReason = node('input'); editReason.type = 'text'; editReason.placeholder = '例如：对照原谱，识别少了升号'; editReason.maxLength = 500;
    const field = (label, input) => { const wrapper = node('label', 'notation-edit-field', label); wrapper.append(input); return wrapper; };
    const saveEdit = node('button', 'notation-button', '保存校对并重建谱面');
    const editHelp = node('p', 'notation-fine-print', '音高以 MIDI 编号输入（中央 C = 60），时值以四分音符为 1。每次修改保留原值与理由；改变时值需要检查小节拍数。');
    editBody.append(editInfo, field('音高 MIDI', editPitch), field('时值 / 四分音符', editDuration), field('校对依据', editReason), saveEdit, editHelp);
    editor.append(editHeading, editBody);

    const audit = node('details', 'notation-audit');
    const auditHeading = node('summary');
    const auditBody = node('div', 'notation-audit-body');
    audit.append(auditHeading, auditBody);
    host.append(toolbar, hint, workspace, controls, editor, audit);

    const setStatus = (text, error = false) => { status.textContent = text; status.classList.toggle('is-error', error); };
    const settings = () => typeof hooks.settings === 'function' ? hooks.settings() : {};
    const selectedNotes = () => [...selected].map(id => notes.get(id)).filter(Boolean).sort((a, b) => Number(a.start) - Number(b.start) || Number(a.pitch) - Number(b.pitch));
    const labelNote = note => `第 ${note.measure} 小节 · ${note.part || '声部'} · ${note.pitchName || midiName(note.pitch)} · ${note.duration} 四分时长单位`;
    function setBusy(value) {
      busy = value;
      for (const button of [playSelected, fromHere, compare, clear, full, rangeButton, saveEdit]) button.disabled = value;
      if (!value) updateSelection();
    }
    function setView(nextView) {
      view = nextView;
      const original = document.getElementById('original-view');
      workspace.hidden = view !== 'notation';
      hint.hidden = view !== 'notation';
      if (original) original.hidden = view !== 'original';
      host.classList.toggle('show-original', view === 'original');
      notationTab.classList.toggle('is-current', view === 'notation');
      originalTab.classList.toggle('is-current', view === 'original');
      notationTab.setAttribute('aria-pressed', String(view === 'notation'));
      originalTab.setAttribute('aria-pressed', String(view === 'original'));
    }
    notationTab.addEventListener('click', () => setView('notation'));
    originalTab.addEventListener('click', () => setView('original'));

    function highlightSelection() {
      for (const [id, element] of elements) {
        element.classList.toggle('is-selected', selected.has(id));
        element.setAttribute('aria-pressed', String(selected.has(id)));
      }
    }
    function updateSelection() {
      highlightSelection();
      const picked = selectedNotes();
      hooks.onSelection?.(picked.map(note => note.id), model);
      const hasSelection = picked.length > 0;
      playSelected.disabled = busy || !hasSelection;
      fromHere.disabled = busy || !hasSelection;
      compare.disabled = busy || !hasSelection || hooks.originalAvailable===false;
      clear.disabled = busy || !hasSelection;
      saveEdit.disabled = busy || picked.length !== 1;
      selectionList.replaceChildren();
      if (!hasSelection) {
        summary.textContent = '还未选择音符';
        selectionDescription.textContent = '绿色表示正在发声，橙色表示所选音符。';
        editInfo.textContent = '先在谱面只选择一个音符。';
        editPitch.disabled = true; editDuration.disabled = true; editReason.disabled = true;
        return;
      }
      const first = picked[0];
      const lastTime = Math.max(...picked.map(note => Number(note.end)));
      const measures = picked.map(note => Number(note.measure));
      const minMeasure = Math.min(...measures), maxMeasure = Math.max(...measures);
      summary.textContent = `已选 ${picked.length} 个音符 · 第 ${minMeasure}${minMeasure === maxMeasure ? '' : `—${maxMeasure}`} 小节`;
      const parts = [...new Set(picked.map(note => note.part))].filter(Boolean).join(' / ');
      selectionDescription.textContent = `${parts} · 时间跨度 ${seconds(lastTime - Number(first.start))} · 只播放选中的音符`;
      rangeStart.value = String(minMeasure); rangeEnd.value = String(maxMeasure);
      for (const note of picked.slice(0, 8)) {
        const chip = node('button', 'notation-note-chip', `${note.pitchName || midiName(note.pitch)} · m${note.measure}`);
        chip.title = labelNote(note);
        chip.addEventListener('click', () => { selected = new Set([note.id]); updateSelection(); focusNote(note.id); preview(note); });
        selectionList.append(chip);
      }
      if (picked.length > 8) selectionList.append(node('span', 'notation-more-notes', `另 ${picked.length - 8} 个`));
      const single = picked.length === 1;
      editPitch.disabled = !single; editDuration.disabled = !single; editReason.disabled = !single;
      if (single) {
        editInfo.textContent = `${labelNote(first)} · ID ${first.id}`;
        editPitch.value = String(first.pitch); editDuration.value = String(first.duration);
      } else editInfo.textContent = '人工修订需要只选择一个音符。';
    }

    async function preview(note) {
      if (note.pitch === null || !Number.isFinite(Number(note.pitch))) return;
      try {
        const AudioContext = window.AudioContext || window.webkitAudioContext;
        if (!AudioContext) { setStatus('设备不支持快速试听，请使用「合成并播放所选」。'); return; }
        previewContext ||= new AudioContext();
        await previewContext.resume();
        if (previewVoice) { try { previewVoice.stop(); } catch (_) { /* Already ended. */ } }
        const oscillator = previewContext.createOscillator();
        const gain = previewContext.createGain();
        const current = settings();
        const pitch = Number(note.pitch) + Number(current.transpose || 0);
        const playbackRate = Math.max(0.1, Number(hooks.audio?.playbackRate || 1));
        const duration = Math.max(0.07, Math.min(12, (Number(note.end) - Number(note.start)) / playbackRate));
        const now = previewContext.currentTime;
        oscillator.type = 'triangle'; oscillator.frequency.value = 440 * 2 ** ((pitch - 69) / 12);
        gain.gain.setValueAtTime(0, now);
        gain.gain.linearRampToValueAtTime(0.12 * Number(hooks.audio?.volume ?? 0.8), now + 0.012);
        gain.gain.setValueAtTime(0.09 * Number(hooks.audio?.volume ?? 0.8), now + Math.max(0.02, duration - 0.025));
        gain.gain.linearRampToValueAtTime(0, now + duration);
        oscillator.connect(gain); gain.connect(previewContext.destination);
        oscillator.start(now); oscillator.stop(now + duration + 0.02); previewVoice = oscillator;
        setStatus(`快速音高试听：${midiName(pitch)}，${seconds(duration)}。真实音色请点击「合成并播放所选」。`);
      } catch (error) { setStatus(`快速试听失败：${error.message}。可使用片段合成。`, true); }
    }
    function focusNote(id) {
      setView('notation');
      const element = elements.get(id);
      if (!element) return;
      const rect = element.getBoundingClientRect(), viewport = stage.getBoundingClientRect();
      stage.scrollTo({ top: stage.scrollTop + rect.top - viewport.top - 90, behavior: 'smooth' });
    }
    function showOriginal(measureNumber) {
      setView('original');
      requestAnimationFrame(() => {
        const box = [...document.querySelectorAll('.measure')].find(element => element.dataset.label === `小节 ${measureNumber}`);
        const viewport = document.getElementById('score-scroll');
        if (!box || !viewport) { setStatus(`原谱第 ${measureNumber} 小节位置暂不可用。`); return; }
        const rect = box.getBoundingClientRect(), bounds = viewport.getBoundingClientRect();
        viewport.scrollTo({ top: viewport.scrollTop + rect.top - bounds.top - 50, behavior: 'smooth' });
        box.classList.add('notation-original-focus');
        window.setTimeout(() => box.classList.remove('notation-original-focus'), 3000);
      });
    }
    compare.addEventListener('click', () => { const first = selectedNotes()[0]; if (first) showOriginal(first.measure); });
    clear.addEventListener('click', () => { selected.clear(); updateSelection(); setStatus('选择已清除。可以继续框选或单击音符。'); });
    full.addEventListener('click', async () => {
      try { await hooks.restoreFull?.(); setStatus('已恢复全曲音频，当前声音参数由右侧控制。'); }
      catch (error) { setStatus(error.message, true); }
    });
    fromHere.addEventListener('click', async () => {
      const first = selectedNotes()[0];
      if (!first) return;
      try { await hooks.restoreFull?.(); await hooks.seek?.(Number(first.start)); await hooks.audio?.play(); setStatus(`从第 ${first.measure} 小节、${first.pitchName || midiName(first.pitch)} 处开始播放全曲。`); }
      catch (error) { setStatus(`播放失败：${error.message}`, true); }
    });
    rangeButton.addEventListener('click', () => {
      const start = Number(rangeStart.value), end = Number(rangeEnd.value);
      if (!Number.isInteger(start) || !Number.isInteger(end) || end < start) { setStatus('请输入有效的小节范围，结束不能早于起始。', true); return; }
      selected = new Set([...notes.values()].filter(note => Number(note.measure) >= start && Number(note.measure) <= end && (!partChoice.value || note.part === partChoice.value)).map(note => note.id));
      updateSelection();
      if (!selected.size) setStatus('这个范围没有可选择的音符，请调整小节或声部。', true);
      else { focusNote(selectedNotes()[0].id); setStatus('已按小节与声部选择。播放时保留这些音符原来的相对时间。'); }
    });

    async function post(path, body) {
      const response = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `服务返回 ${response.status}`);
      return result;
    }
    playSelected.addEventListener('click', async () => {
      if (busy || !selected.size) return;
      const noteIds = [...selected];
      setBusy(true); setStatus(`正在合成所选 ${noteIds.length} 个音符…`);
      try {
        const result = await post('/api/selection', { ...settings(), noteIds });
        if (typeof hooks.onAudio !== 'function') throw new Error('片段播放接口未连接');
        await hooks.onAudio(result);
        downloads.replaceChildren();
        for (const [key, title] of [['wav', '下载所选 WAV'], ['midi', '下载所选 MIDI']]) {
          if (!result[key]) continue;
          const link = node('a', '', `${title} ↗`); link.href = result[key]; link.download = ''; downloads.append(link);
        }
        setStatus(`正在播放${result.audioValidation?.status==='draft'?'草稿选区':'所选片段'} ${noteIds.length} 个音符，保留相对时间、和弦和休止。片段时长 ${seconds(result.duration)}。`);
      } catch (error) { setStatus(`片段合成失败：${error.message}`, true); }
      finally { setBusy(false); }
    });
    saveEdit.addEventListener('click', async () => {
      const picked = selectedNotes();
      if (busy || picked.length !== 1) return;
      const pitch = Number(editPitch.value), duration = Number(editDuration.value), reason = editReason.value.trim();
      if (!Number.isInteger(pitch) || pitch < 0 || pitch > 127 || !Number.isFinite(duration) || duration < 0.03125 || duration > 16) { setStatus('音高必须为 0—127 的整数，时值必须为 0.03125—16 拍。', true); return; }
      if (!reason) { setStatus('请写下校对依据，以便回看这次修改。', true); editReason.focus(); return; }
      setBusy(true); setStatus('正在保存修订、重新检查小节并重建互动谱…');
      try {
        const result = await post('/api/correction', { noteId: picked[0].id, pitch, duration, reason });
        const url = result.model || result.modelUrl || result.interactive || '/assets/interactive.json';
        if (result.data) await refresh(result.data);
        else { const response = await fetch(`${url}${url.includes('?') ? '&' : '?'}revision=${encodeURIComponent(result.revision || Date.now())}`, { cache: 'no-store' }); if (!response.ok) throw new Error('修订已保存，但谱面数据加载失败'); await refresh(await response.json()); }
        editReason.value = '';
        setStatus('校对已保存，互动谱已更新。请重新合成所选或全曲以听到修订后的结果。');
        await hooks.onCorrection?.(result, model);
      } catch (error) { setStatus(`校对失败：${error.message}`, true); }
      finally { setBusy(false); }
    });

    function noteFromTarget(target) {
      const group = target instanceof Element ? target.closest('g.note[data-note-id], [data-note-id]') : null;
      const id = group?.dataset.noteId || group?.id;
      return notes.has(id) ? notes.get(id) : null;
    }
    function activate(note, additive) {
      if (additive) { if (selected.has(note.id)) selected.delete(note.id); else selected.add(note.id); }
      else selected = new Set([note.id]);
      updateSelection(); preview(note);
    }
    stage.addEventListener('pointerdown', event => {
      if (busy || event.button !== 0 || !event.target.closest('svg')) return;
      // Touch keeps native page scrolling; taps still select individual notes.
      if (event.pointerType === 'touch') { touch = { x: event.clientX, y: event.clientY, id: event.pointerId, note: noteFromTarget(event.target), moved: false }; return; }
      drag = { x: event.clientX, y: event.clientY, endX: event.clientX, endY: event.clientY, pointerId: event.pointerId, moving: false, additive: event.shiftKey || event.metaKey || event.ctrlKey, initial: new Set(selected), note: noteFromTarget(event.target) };
      stage.setPointerCapture(event.pointerId);
      event.preventDefault();
    });
    function paintDrag(event) {
      if (!drag || event.pointerId !== drag.pointerId) return;
      drag.endX = event.clientX; drag.endY = event.clientY;
      if (!drag.moving && Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < 6) return;
      drag.moving = true;
      const left = Math.min(drag.x, event.clientX), right = Math.max(drag.x, event.clientX), top = Math.min(drag.y, event.clientY), bottom = Math.max(drag.y, event.clientY);
      const bounds = stage.getBoundingClientRect();
      Object.assign(dragBox.style, { left: `${left - bounds.left + stage.scrollLeft}px`, top: `${top - bounds.top + stage.scrollTop}px`, width: `${right - left}px`, height: `${bottom - top}px` });
      dragBox.hidden = false;
      selected = drag.additive ? new Set(drag.initial) : new Set();
      for (const [id, element] of elements) {
        const head = element.querySelector('.notehead') || element;
        const rect = head.getBoundingClientRect();
        const x = (rect.left + rect.right) / 2, y = (rect.top + rect.bottom) / 2;
        if (rect.width > 0 && x >= left && x <= right && y >= top && y <= bottom) selected.add(id);
      }
      highlightSelection();
    }
    stage.addEventListener('pointermove', event => {
      if (touch && event.pointerId === touch.id && Math.hypot(event.clientX - touch.x, event.clientY - touch.y) > 8) touch.moved = true;
      paintDrag(event);
    });
    stage.addEventListener('pointerup', event => {
      if (event.pointerType === 'touch') { if (touch && touch.id === event.pointerId && !touch.moved && touch.note && !busy) activate(touch.note, true); touch = null; return; }
      if (!drag || event.pointerId !== drag.pointerId) return;
      const finished = drag; drag = null; dragBox.hidden = true;
      if (stage.hasPointerCapture(event.pointerId)) stage.releasePointerCapture(event.pointerId);
      if (finished.moving) { updateSelection(); setStatus(selected.size ? `已框选 ${selected.size} 个音符。点击「合成并播放所选」试听。` : '框内没有音符，请将框覆盖到音符头。'); }
      else if (finished.note) activate(finished.note, finished.additive);
      else if (!finished.additive) { selected.clear(); updateSelection(); }
    });
    stage.addEventListener('pointercancel', () => { if (drag) selected = new Set(drag.initial); drag = null; touch = null; dragBox.hidden = true; updateSelection(); });
    stage.addEventListener('keydown', event => {
      if (event.key === 'Escape') { selected.clear(); updateSelection(); event.preventDefault(); }
      if (event.key === 'Enter' || event.key === ' ') { const note = noteFromTarget(event.target); if (note) { event.preventDefault(); event.stopPropagation(); activate(note, event.shiftKey || event.metaKey || event.ctrlKey); } }
    });

    function buildAudit() {
      const report = model.audit || {};
      const issues = Array.isArray(report.issues) ? report.issues : [];
      const corrections = Array.isArray(report.corrections) ? report.corrections : [];
      const automatic = corrections.filter(item => item.automatic || item.applied || item.status === 'applied');
      auditHeading.textContent = `乐理校验 · ${issues.length} 项待检查 · ${corrections.length} 条规范化 / 校准记录`;
      auditBody.replaceChildren();
      const intro = node('p', 'notation-audit-intro', '乐理检查用于发现拍数、连音和声部异常。候选问题需要对照原谱；检查通过不等于识谱已经准确。');
      auditBody.append(intro);
      if (report.summary) auditBody.append(node('p', '', typeof report.summary === 'string' ? report.summary : JSON.stringify(report.summary)));
      if (automatic.length) auditBody.append(node('p', '', `已有 ${automatic.length} 项结构规范化 / 校准记录，原值和修订依据保留在数据记录中。`));
      if (!issues.length) auditBody.append(node('p', '', '当前规则未发现待检查项；仍需逐音对照验证。'));
      const issueList = node('div', 'notation-issue-list');
      for (const issue of issues.slice(0, 80)) {
        const row = node('div', `notation-issue ${issue.severity === 'error' ? 'is-severe' : ''}`);
        const location = issue.measure !== undefined ? `第 ${issue.measure} 小节${issue.part ? ` · ${issue.part}` : ''}` : '全曲检查';
        const title = node('strong', '', `${location} · ${issue.kind || issue.type || issue.code || '待校对'}`);
        const message = node('p', '', issue.message || issue.description || issue.reason || JSON.stringify(issue));
        row.append(title, message);
        if (issue.measure !== undefined) {
          const locate = node('button', 'notation-button small', '定位互动谱');
          const original = node('button', 'notation-button small', '对照原谱');
          locate.addEventListener('click', () => {
            const ids = issue.noteIds || issue.evidence?.noteIds || (issue.noteId ? [issue.noteId] : [...notes.values()].filter(note => Number(note.measure) === Number(issue.measure) && (!issue.part || note.part === issue.part)).map(note => note.id));
            selected = new Set(ids.filter(id => notes.has(id))); updateSelection();
            if (selected.size) focusNote(selectedNotes()[0].id);
          });
          original.addEventListener('click', () => showOriginal(issue.measure));
          row.append(locate, original);
        }
        issueList.append(row);
      }
      if (issues.length > 80) issueList.append(node('p', '', `其余 ${issues.length - 80} 项请下载完整校验数据查看。`));
      auditBody.append(issueList);
      if (corrections.length) {
        const history = node('details', 'notation-correction-history');
        history.append(node('summary', '', `查看最近 ${Math.min(20, corrections.length)} 次修订依据`));
        for (const change of corrections.slice(-20).reverse()) {
          const row = node('p', '');
          const prefix = change.measure !== undefined ? `第 ${change.measure} 小节：` : '';
          row.textContent = `${prefix}${change.reason || change.message || change.description || JSON.stringify(change)}`;
          history.append(row);
        }
        auditBody.append(history);
      }
      const reportLink = node('a', '', '下载完整结构化与校验数据 ↗'); reportLink.href = '/assets/interactive.json'; reportLink.download = ''; auditBody.append(reportLink);
    }

    async function refresh(nextModel = model) {
      model = nextModel;
      notes = new Map((model.notes || []).filter(note => note.pitch !== null && note.pitch !== undefined).map(note => [note.id, note]));
      selected = new Set([...selected].filter(id => notes.has(id)));
      sounding.clear(); elements.clear(); lastFollowed = '';
      badge.textContent = `${notes.size} 个可互动音符 · 结构化识谱`;
      partChoice.replaceChildren();
      const allParts = node('option', '', '全部声部'); allParts.value = ''; partChoice.append(allParts);
      for (const part of [...new Set([...notes.values()].map(note => note.part))].filter(Boolean)) { const option = node('option', '', part); option.value = part; partChoice.append(option); }
      const measureNumbers = [...notes.values()].map(note => Number(note.measure)).filter(Number.isFinite);
      const maxMeasure = measureNumbers.length ? Math.max(...measureNumbers) : 1;
      rangeStart.max = String(maxMeasure); rangeEnd.max = String(maxMeasure);
      rangeEnd.value = String(Math.min(4, maxMeasure));
      buildAudit();
      const token = ++svgLoadToken;
      pages.replaceChildren(node('p', 'notation-loading', '正在加载互动谱…'));
      const interactivePages = model.pagesInteractive || model.interactivePages || [];
      const loaded = await Promise.all(interactivePages.map(async page => {
        const response = await fetch(`${page.svg}${page.svg.includes('?') ? '&' : '?'}revision=${encodeURIComponent(model.revision || '')}`, { cache: 'no-cache' });
        if (!response.ok) throw new Error(`互动谱第 ${page.page} 页加载失败`);
        const doc = new DOMParser().parseFromString(await response.text(), 'image/svg+xml');
        const svg = doc.documentElement;
        if (svg.localName !== 'svg' || doc.querySelector('parsererror')) throw new Error(`互动谱第 ${page.page} 页格式错误`);
        // Notation assets are local renderer output. SVG stays inert after insertion.
        for (const unsafe of svg.querySelectorAll('script, foreignObject')) unsafe.remove();
        for (const element of svg.querySelectorAll('*')) for (const attribute of [...element.attributes]) if (attribute.name.startsWith('on')) element.removeAttribute(attribute.name);
        const wrapper = node('section', 'notation-page'); wrapper.dataset.page = String(page.page);
        wrapper.append(node('div', 'notation-page-label', `互动谱 ${String(page.page).padStart(2, '0')}`), document.importNode(svg, true));
        return wrapper;
      }));
      if (token !== svgLoadToken) return;
      pages.replaceChildren(...loaded);
      let missing = 0;
      for (const element of pages.querySelectorAll('g.note, [data-note-id]')) {
        const id = element.dataset.noteId || element.id;
        const note = notes.get(id);
        if (!note) { missing += 1; continue; }
        element.dataset.noteId = id;
        const head=element.querySelector('.notehead');
        if(head){const b=head.getBBox();const hit=document.createElementNS('http://www.w3.org/2000/svg','rect');
          hit.setAttribute('x',String(b.x-65));hit.setAttribute('y',String(b.y-65));hit.setAttribute('width',String(b.width+130));hit.setAttribute('height',String(b.height+130));
          hit.setAttribute('fill','transparent');hit.setAttribute('pointer-events','all');hit.setAttribute('class','notation-hit-area');element.prepend(hit);}
        element.setAttribute('tabindex', '0'); element.setAttribute('role', 'button');
        element.setAttribute('aria-label', `${labelNote(note)}，点击快速试听`);
        const title = document.createElementNS('http://www.w3.org/2000/svg', 'title'); title.textContent = labelNote(note); element.prepend(title);
        elements.set(id, element);
      }
      if (!loaded.length) setStatus('互动谱尚未生成，可先使用原谱对照。', true);
      else if (elements.size < notes.size) setStatus(`已连接 ${elements.size} / ${notes.size} 个音符；未映射音符暂不能在谱面点选。`, true);
      else setStatus('互动谱已就绪。单击音符快速试听，拖动框选后合成真实音色片段。');
      host.dataset.mappedNotes = String(elements.size); host.dataset.unmappedSvgNotes = String(missing);
      updateSelection();
    }

    function update(globalTime, playbackOverride = {}) {
      const time = Number(globalTime);
      if (!Number.isFinite(time)) return;
      const playing = typeof playbackOverride.playing === 'boolean' ? playbackOverride.playing : !hooks.audio?.paused;
      const activeNoteIds = playbackOverride.noteIds ?? (typeof hooks.activeNoteIds === 'function' ? hooks.activeNoteIds() : null);
      const whitelist = activeNoteIds == null ? null : activeNoteIds instanceof Set ? activeNoteIds : new Set(activeNoteIds);
      const next = new Set();
      for (const [id, note] of notes) if (playing && (!whitelist || whitelist.has(id)) && time >= Number(note.start) && time < Number(note.end)) next.add(id);
      for (const id of sounding) if (!next.has(id)) elements.get(id)?.classList.remove('is-sounding');
      for (const id of next) if (!sounding.has(id)) elements.get(id)?.classList.add('is-sounding');
      sounding = next;
      if (view !== 'notation' || !playing || !document.getElementById('follow')?.checked || !next.size) return;
      const first = [...next].find(id => elements.has(id));
      if (!first || first === lastFollowed) return;
      lastFollowed = first;
      const element = elements.get(first), rect = element.getBoundingClientRect(), viewport = stage.getBoundingClientRect();
      if (rect.top < viewport.top + 25 || rect.bottom > viewport.bottom - 35) stage.scrollTo({ top: stage.scrollTop + rect.top - viewport.top - 80, behavior: 'smooth' });
    }
    setView('notation');
    await refresh(model);
    document.getElementById('zoom')?.addEventListener('change', event => { pages.style.width = `${event.target.value}%`; });
    return { update, refresh, setView, getSelected: () => [...selected], getModel: () => model };
  };
})();
