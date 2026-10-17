/* The same canonical note IDs connect notation, selection, audio, and corrections. */
async function loadNotationAssets(pages, revision, fetchAsset = fetch, timeoutMs = 15000) {
  return Promise.all(pages.map(async page => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetchAsset(`${page.svg}${page.svg.includes('?') ? '&' : '?'}revision=${encodeURIComponent(revision || '')}`, { cache: 'no-cache', signal: controller.signal });
      if (!response.ok) throw new Error(`互动谱第 ${page.page} 页加载失败（${response.status}）`);
      return { page, text: await response.text() };
    } catch (error) {
      return { page, error: error.name === 'AbortError' ? `互动谱第 ${page.page} 页加载超时，可重试` : error.message || String(error) };
    } finally {
      clearTimeout(timer);
    }
  }));
}
function measureAtTime(measures, time) {
  if (!Number.isFinite(Number(time))) return null;
  const position = Number(time);
  const current = measures.find(measure => position >= Number(measure.start) && position < Number(measure.end));
  if (current) return current;
  const last = measures[measures.length - 1];
  return last && position >= Number(last.end) ? last : null;
}
function mappedMeasure(ids, symbols) {
  const numbers = new Set(ids.map(id => {
    const symbol = symbols.get(id);
    return symbol ? symbol.measureIndex ?? symbol.measure : undefined;
  }).filter(value => value !== undefined));
  // Never infer a number from the renderer's random measure ID or DOM order.
  return numbers.size === 1 ? [...numbers][0] : null;
}
function validateNotationOverlay(overlay, model) {
  if (!overlay || overlay.schemaVersion !== 1 || overlay.revision !== model.revision || !Array.isArray(overlay.measureLyrics)) throw new Error('唱词版本与当前曲谱不一致');
  const measures = new Set((model.measures || []).map(measure => Number(measure.index ?? measure.number)));
  for (const lyric of overlay.measureLyrics) {
    if (!measures.has(Number(lyric.measure)) || typeof lyric.part !== 'string' || !Array.isArray(lyric.lines) || lyric.lines.some(line => typeof line !== 'string' || line.length > 1000)) throw new Error('唱词小节或文本格式无效');
  }
  return overlay;
}
function measureFollowAction({ view, playing, forceFollow, follow, current, outside, key, previousKey, jumped }) {
  if (view !== 'notation' || (!playing && !forceFollow) || !follow || !current) return null;
  if (!outside || (previousKey === key && !forceFollow && !jumped)) return null;
  return forceFollow || jumped ? 'auto' : 'smooth';
}
async function loadNotationOverlay(url, model, fetchAsset = fetch, timeoutMs = 15000) {
  if (!url) return null;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetchAsset(url, { cache: 'no-cache', signal: controller.signal });
    if (!response.ok) throw new Error(`唱词加载失败（${response.status}）`);
    return validateNotationOverlay(await response.json(), model);
  } catch (error) {
    return { error: error.name === 'AbortError' ? '唱词加载超时，可重新加载' : error.message || String(error) };
  } finally { clearTimeout(timer); }
}
if (typeof module !== 'undefined' && module.exports) module.exports = { loadNotationAssets, loadNotationOverlay, measureAtTime, mappedMeasure, validateNotationOverlay, measureFollowAction };
(() => {
  'use strict';
  if (typeof window === 'undefined') return;

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
    host._disposeInteractivePointers?.();
    let model = data;
    let notes = new Map();
    const elements = new Map();
    const measureElements = new Map();
    const measureMarkers = new Map();
    let selected = new Set();
    let sounding = new Set();
    let view = 'notation';
    let busy = false;
    let drag = null;
    let touch = null;
    let selectionMode = 'point';
    let additiveSelection = false;
    let multiTouch = false;
    const touchPointers = new Set();
    let previewContext;
    let previewVoice;
    let lastFollowed = '';
    let lastTime = 0;
    let activeMeasure = null;
    let notationOverlay = null;
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
    const interactionTools = node('div', 'notation-interaction-tools');
    const selectionModes = node('div', 'notation-selection-modes');
    selectionModes.setAttribute('role', 'group');
    selectionModes.setAttribute('aria-label', '触屏选取方式');
    const pointMode = node('button', 'notation-mode-button', '点选 / 滚动');
    pointMode.setAttribute('aria-label', '点选与滚动');
    const boxMode = node('button', 'notation-mode-button', '框选');
    boxMode.setAttribute('aria-label', '触屏框选');
    const additiveMode = node('button', 'notation-mode-button notation-additive-mode', '加选：关');
    additiveMode.setAttribute('aria-label', '加选音符');
    for (const button of [pointMode, boxMode, additiveMode]) button.type = 'button';
    pointMode.title = '触屏点选一个音符，滑动滚动谱面';
    boxMode.title = '触屏单指拖动框选；滚动前切回点选 / 滚动';
    additiveMode.title = '开启后保留已有选区；点选已选音符可取消它';
    selectionModes.append(pointMode, boxMode);
    interactionTools.append(selectionModes, additiveMode);
    const hint = node('div', 'notation-guide');
    const lyricsNow = node('div', 'notation-lyrics-current');
    lyricsNow.setAttribute('aria-label', '当前小节唱词');
    lyricsNow.hidden = true;
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
    host.append(toolbar, interactionTools, hint, lyricsNow, workspace, controls, editor, audit);

    const setStatus = (text, error = false) => { status.textContent = text; status.classList.toggle('is-error', error); };
    const settings = () => typeof hooks.settings === 'function' ? hooks.settings() : {};
    const selectedNotes = () => [...selected].map(id => notes.get(id)).filter(Boolean).sort((a, b) => Number(a.start) - Number(b.start) || Number(a.pitch) - Number(b.pitch));
    const labelNote = note => `第 ${note.measure} 小节 · ${note.part || '声部'} · ${note.pitchName || midiName(note.pitch)} · ${note.duration} 四分时长单位`;
    function setBusy(value) {
      if (value) cancelGesture();
      busy = value;
      for (const button of [playSelected, fromHere, compare, clear, full, rangeButton, saveEdit, pointMode, boxMode, additiveMode]) button.disabled = value;
      if (!value) updateSelection();
    }
    function setView(nextView) {
      if (nextView !== view) cancelGesture();
      view = nextView;
      const original = document.getElementById('original-view');
      workspace.hidden = view !== 'notation';
      hint.hidden = view !== 'notation';
      interactionTools.hidden = view !== 'notation';
      if (original) original.hidden = view !== 'original';
      host.classList.toggle('show-original', view === 'original');
      notationTab.classList.toggle('is-current', view === 'notation');
      originalTab.classList.toggle('is-current', view === 'original');
      notationTab.setAttribute('aria-pressed', String(view === 'notation'));
      originalTab.setAttribute('aria-pressed', String(view === 'original'));
      lastFollowed = '';
      if (view === 'notation' && measureElements.size) requestAnimationFrame(() => update(lastTime, { forceFollow: true }));
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
    const isAdditive = event => additiveSelection || event.shiftKey || event.metaKey || event.ctrlKey;
    function releaseCapture(pointerId) {
      if (pointerId === undefined) return;
      try { if (stage.hasPointerCapture(pointerId)) stage.releasePointerCapture(pointerId); }
      catch (_) { /* A cancelled pointer may already have been released by the browser. */ }
    }
    function cancelGesture() {
      const previousDrag = drag, previousTouch = touch;
      drag = null; touch = null; dragBox.hidden = true;
      releaseCapture(previousDrag?.pointerId ?? previousTouch?.pointerId);
      if (previousDrag) { selected = new Set(previousDrag.initial); updateSelection(); }
    }
    function updateInteractionMode() {
      stage.dataset.selectionMode = selectionMode;
      stage.dataset.additive = String(additiveSelection);
      stage.classList.toggle('is-box-selection', selectionMode === 'box');
      pointMode.setAttribute('aria-pressed', String(selectionMode === 'point'));
      boxMode.setAttribute('aria-pressed', String(selectionMode === 'box'));
      additiveMode.setAttribute('aria-pressed', String(additiveSelection));
      additiveMode.textContent = `加选：${additiveSelection ? '开' : '关'}`;
      hint.textContent = selectionMode === 'box'
        ? '触屏单指拖动框选；滚动请切回「点选 / 滚动」。电脑可直接拖动，Shift / ⌘ 加选。'
        : '触屏点选替换选区，滑动滚动；先切到「框选」再拖动。电脑可直接拖动，Shift / ⌘ 加选。';
      if (additiveSelection) hint.textContent += ' 当前加选已开启，已有选区会保留。';
    }
    for (const [button, mode] of [[pointMode, 'point'], [boxMode, 'box']]) button.addEventListener('click', () => {
      if (busy) return;
      cancelGesture(); selectionMode = mode; updateInteractionMode();
    });
    additiveMode.addEventListener('click', () => {
      if (busy) return;
      cancelGesture(); additiveSelection = !additiveSelection; updateInteractionMode();
    });
    updateInteractionMode();

    // Track secondary fingers outside the stage too. A two-finger gesture must
    // cancel its temporary box selection, never commit a partial selection.
    const pointerListeners = new AbortController();
    document.addEventListener('pointerdown', event => {
      if (event.pointerType !== 'touch') return;
      touchPointers.add(event.pointerId);
      if (touchPointers.size > 1 || event.isPrimary === false) {
        multiTouch = true;
        cancelGesture();
      }
    }, { capture: true, signal: pointerListeners.signal });
    const endTouch = event => {
      if (event.pointerType !== 'touch') return;
      touchPointers.delete(event.pointerId);
      if (!touchPointers.size) multiTouch = false;
    };
    document.addEventListener('pointerup', endTouch, { capture: true, signal: pointerListeners.signal });
    document.addEventListener('pointercancel', endTouch, { capture: true, signal: pointerListeners.signal });
    window.addEventListener('blur', () => { cancelGesture(); touchPointers.clear(); multiTouch = false; }, { signal: pointerListeners.signal });
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) { cancelGesture(); touchPointers.clear(); multiTouch = false; }
    }, { signal: pointerListeners.signal });
    host._disposeInteractivePointers = () => { cancelGesture(); pointerListeners.abort(); };

    stage.addEventListener('pointerdown', event => {
      if (busy || view !== 'notation' || event.button !== 0 || !(event.target instanceof Element)) return;
      if (event.target.closest('button, a, input, select, textarea, summary')) return;
      if (event.pointerType === 'touch' && (multiTouch || event.isPrimary === false)) { cancelGesture(); return; }
      if (drag || touch) { cancelGesture(); return; }
      const gesture = { x: event.clientX, y: event.clientY, endX: event.clientX, endY: event.clientY, pointerId: event.pointerId, pointerType: event.pointerType, moving: false, additive: isAdditive(event), initial: new Set(selected), note: noteFromTarget(event.target) };
      // Capture keeps tap-up observable even if the finger leaves the stage.
      // In point mode touch-action still lets the browser take over scrolling.
      stage.setPointerCapture(event.pointerId);
      if (event.pointerType === 'touch' && selectionMode === 'point') { touch = gesture; return; }
      drag = gesture;
      event.preventDefault();
    });
    function paintDrag(event) {
      if (!drag || event.pointerId !== drag.pointerId) return;
      drag.endX = event.clientX; drag.endY = event.clientY;
      if (!drag.moving && Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < (drag.pointerType === 'touch' ? 8 : 6)) return;
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
      if (touch && event.pointerId === touch.pointerId && Math.hypot(event.clientX - touch.x, event.clientY - touch.y) > 8) touch.moving = true;
      if (drag && event.pointerId === drag.pointerId) event.preventDefault();
      paintDrag(event);
    });
    stage.addEventListener('pointerup', event => {
      if (touch && event.pointerId === touch.pointerId) {
        const finished = touch; touch = null; releaseCapture(event.pointerId);
        const moved = finished.moving || Math.hypot(event.clientX - finished.x, event.clientY - finished.y) > 8;
        if (!moved && !busy && !multiTouch) {
          if (finished.note) activate(finished.note, finished.additive);
          else if (!finished.additive) { selected.clear(); updateSelection(); }
        }
        return;
      }
      if (!drag || event.pointerId !== drag.pointerId) return;
      if (busy || multiTouch) { cancelGesture(); return; }
      paintDrag(event);
      const finished = drag; drag = null; dragBox.hidden = true;
      releaseCapture(event.pointerId);
      if (finished.moving) { updateSelection(); setStatus(selected.size ? `已框选 ${selected.size} 个音符。点击「合成并播放所选」试听。` : '框内没有音符，请将框覆盖到音符头。'); }
      else if (finished.note) activate(finished.note, finished.additive);
      else if (!finished.additive) { selected.clear(); updateSelection(); }
    });
    stage.addEventListener('pointercancel', event => {
      if (event.pointerId === drag?.pointerId || event.pointerId === touch?.pointerId) cancelGesture();
    });
    stage.addEventListener('lostpointercapture', event => {
      if (event.pointerId === drag?.pointerId || event.pointerId === touch?.pointerId) cancelGesture();
    });
    stage.addEventListener('contextmenu', event => { if (drag || touch) event.preventDefault(); });
    stage.addEventListener('keydown', event => {
      if (event.key === 'Escape') { cancelGesture(); selected.clear(); updateSelection(); event.preventDefault(); return; }
      if (busy || drag || touch) return;
      if (event.key === 'Enter' || event.key === ' ') { const note = noteFromTarget(event.target); if (note) { event.preventDefault(); event.stopPropagation(); activate(note, isAdditive(event)); } }
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

    function annotateMeasures() {
      const symbols = new Map([...(model.notes || []), ...(model.rests || [])].map(symbol => [symbol.id, symbol]));
      const svgNode = (name, attributes) => {
        const element = document.createElementNS('http://www.w3.org/2000/svg', name);
        for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
        return element;
      };
      for (const measure of pages.querySelectorAll('g.measure:not(.bounding-box)')) {
        const number = mappedMeasure([...measure.querySelectorAll('[id]')].map(element => element.id), symbols);
        if (number === null) continue;
        const canonical = model.measures.find(item => Number(item.index ?? item.number) === Number(number));
        if (!canonical) continue;
        measure.dataset.measureIndex = String(number);
        measure.dataset.measureNumber = String(canonical.number);
        measureElements.set(Number(number), measure);
        // Use the actual staff boundary, excluding clefs, ledger lines and lyrics.
        const staffs = [...measure.querySelectorAll(':scope > g.staff:not(.bounding-box)')];
        const first = staffs[0], last = staffs[staffs.length - 1];
        const firstLines = first ? [...first.children].filter(element => element.localName === 'path') : [];
        const lastLines = last ? [...last.children].filter(element => element.localName === 'path') : [];
        if (!firstLines.length || !lastLines.length) continue;
        const start = firstLines[0].getBBox(), bottom = lastLines[lastLines.length - 1].getBBox();
        const x = start.x, y = start.y, width = start.width, height = Math.max(1, bottom.y - y);
        for (const existing of measure.querySelectorAll(':scope > .mNum')) existing.remove();
        const label = svgNode('text', { x: x + 35, y: y - 170, class: 'notation-measure-number', 'font-size': 275 });
        label.textContent = String(canonical.number);
        measure.append(label);
        const area = svgNode('rect', { x, y: y - 60, width, height: height + 120, class: 'notation-measure-highlight' });
        const line = svgNode('line', { x1: x, x2: x, y1: y - 60, y2: y + height + 60, class: 'notation-measure-playhead' });
        measure.prepend(area); measure.append(line);
        measureMarkers.set(Number(number), { element: measure, anchor: firstLines[0], line, x, width });
      }
      host.dataset.mappedMeasures = String(measureElements.size);
      if (!notationOverlay) return;
      for (const wrapper of pages.children) {
        const numbers = new Set([...wrapper.querySelectorAll('[data-measure-index]')].map(element => Number(element.dataset.measureIndex)));
        const lyrics = notationOverlay.measureLyrics.filter(lyric => numbers.has(Number(lyric.measure)));
        if (!lyrics.length) continue;
        const details = node('details', 'notation-page-lyrics');
        details.open = false;
        details.append(node('summary', '', '本页唱词 · 按小节与声部'));
        for (const lyric of lyrics) {
          const row = node('button', 'notation-lyric-row');
          row.dataset.lyricMeasure = String(lyric.measure);
          const current = model.measures.find(measure => Number(measure.index ?? measure.number) === Number(lyric.measure));
          row.append(node('strong', '', `${current?.number ?? lyric.measure} · ${lyric.part}`), node('span', '', lyric.lines.join(' / ')));
          row.addEventListener('click', () => {
            if (current) hooks.seek?.(Number(current.start));
          });
          details.append(row);
        }
        wrapper.append(details);
      }
    }

    async function refresh(nextModel = model) {
      cancelGesture();
      model = nextModel;
      notes = new Map((model.notes || []).filter(note => note.pitch !== null && note.pitch !== undefined).map(note => [note.id, note]));
      selected = new Set([...selected].filter(id => notes.has(id)));
      sounding.clear(); elements.clear(); measureElements.clear(); measureMarkers.clear(); lastFollowed = ''; activeMeasure = null;
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
      const overlayRequest = loadNotationOverlay(hooks.notationOverlayUrl, model);
      const [assets, overlay] = await Promise.all([loadNotationAssets(interactivePages, model.revision), overlayRequest]);
      notationOverlay = overlay && !overlay.error ? overlay : null;
      if (token !== svgLoadToken) return;
      let failedPages = 0;
      const loaded = assets.map(asset => {
        const page = asset.page;
        const wrapper = node('section', 'notation-page'); wrapper.dataset.page = String(page.page);
        wrapper.append(node('div', 'notation-page-label', `互动谱 ${String(page.page).padStart(2, '0')}`));
        const doc = asset.error ? null : new DOMParser().parseFromString(asset.text, 'image/svg+xml');
        const svg = doc?.documentElement;
        const error = asset.error || (svg?.localName !== 'svg' || doc.querySelector('parsererror') ? `互动谱第 ${page.page} 页格式错误` : '');
        if (error) {
          failedPages += 1;
          wrapper.append(node('p', 'notation-loading', error));
          const retry = node('button', 'notation-button', '重新加载互动谱');
          retry.addEventListener('click', () => refresh(model).catch(error => setStatus(error.message, true)));
          wrapper.append(retry);
          return wrapper;
        }
        // Notation assets are local renderer output. SVG stays inert after insertion.
        for (const unsafe of svg.querySelectorAll('script, foreignObject')) unsafe.remove();
        for (const element of svg.querySelectorAll('*')) for (const attribute of [...element.attributes]) if (attribute.name.startsWith('on')) element.removeAttribute(attribute.name);
        wrapper.append(document.importNode(svg, true));
        return wrapper;
      });
      if (token !== svgLoadToken) return;
      pages.replaceChildren(...loaded);
      if (notationOverlay?.replaceUnverifiedLyrics) for (const element of pages.querySelectorAll('g.verse')) element.remove();
      annotateMeasures();
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
      if (!loaded.length) {
        pages.append(node('p', 'notation-loading', '互动谱尚未生成，可切换原 PDF 对照。'));
        setStatus('互动谱尚未生成，可先使用原谱对照。', true);
      }
      else if (failedPages) setStatus(`${failedPages} 页加载失败，可在对应页面重新加载；已载入页面仍可点选。`, true);
      else if (overlay?.error) setStatus(`${overlay.error}；可使用原 PDF 对照唱词。`, true);
      else if (measureElements.size < (model.measures || []).length) setStatus(`已连接 ${measureElements.size} / ${model.measures.length} 个小节；缺失小节暂不能跟随定位，可重新加载。`, true);
      else if (elements.size < notes.size) setStatus(`已连接 ${elements.size} / ${notes.size} 个音符；未映射音符暂不能在谱面点选。`, true);
      else setStatus('互动谱已就绪。单击音符快速试听，拖动框选后合成真实音色片段。');
      host.dataset.mappedNotes = String(elements.size); host.dataset.unmappedSvgNotes = String(missing);
      updateSelection();
      update(lastTime, { forceFollow: true });
    }

    function update(globalTime, playbackOverride = {}) {
      const time = Number(globalTime);
      if (!Number.isFinite(time)) return;
      const jumped = Math.abs(time - lastTime) > 1;
      lastTime = time;
      const playing = typeof playbackOverride.playing === 'boolean' ? playbackOverride.playing : !hooks.audio?.paused;
      const activeNoteIds = playbackOverride.noteIds ?? (typeof hooks.activeNoteIds === 'function' ? hooks.activeNoteIds() : null);
      const whitelist = activeNoteIds == null ? null : activeNoteIds instanceof Set ? activeNoteIds : new Set(activeNoteIds);
      const next = new Set();
      const positioned = [];
      for (const [id, note] of notes) if ((!whitelist || whitelist.has(id)) && time >= Number(note.start) && time < Number(note.end)) {
        positioned.push(note);
        if (playing) next.add(id);
      }
      for (const id of sounding) if (!next.has(id)) elements.get(id)?.classList.remove('is-sounding');
      for (const id of next) if (!sounding.has(id)) elements.get(id)?.classList.add('is-sounding');
      sounding = next;
      const current = measureAtTime(model.measures || [], time);
      const number = current ? Number(current.index ?? current.number) : null;
      if (activeMeasure !== number) {
        measureElements.get(activeMeasure)?.classList.remove('is-current-measure');
        measureElements.get(number)?.classList.add('is-current-measure');
        activeMeasure = number;
        const lyrics = notationOverlay?.measureLyrics.filter(lyric => Number(lyric.measure) === number) || [];
        lyricsNow.replaceChildren(); lyricsNow.hidden = !lyrics.length;
        for (const lyric of lyrics) lyricsNow.append(node('span', '', `${lyric.part} · ${lyric.lines.join(' / ')}`));
        if (lyrics.length) lyricsNow.prepend(node('strong', '', `第 ${current.number} 小节`));
        for (const row of pages.querySelectorAll('[data-lyric-measure]')) row.classList.toggle('is-current', Number(row.dataset.lyricMeasure) === number);
      }
      const marker = measureMarkers.get(number);
      if (current && marker) {
        const fraction = Math.max(0, Math.min(1, (time - Number(current.start)) / (Number(current.end) - Number(current.start))));
        const x = marker.x + marker.width * fraction;
        marker.line.setAttribute('x1', String(x)); marker.line.setAttribute('x2', String(x));
      }
      const follow = typeof hooks.follow === 'function' ? hooks.follow() : document.getElementById('follow')?.checked;
      if (!current || drag || touch) return;
      // Follow the canonical measure clock, including rests and tied tails. A
      // paused seek and returning from PDF must relocate even on the same note.
      // Prefer the staff that is actually sounding. Empty vocal staves can be
      // much taller than the viewport during a piano interlude.
      const targetNote = positioned.find(note => Number(note.measureIndex ?? note.measure) === number && elements.has(note.id)) || positioned.find(note => elements.has(note.id));
      const noteElement = targetNote ? elements.get(targetNote.id) : null;
      const element = noteElement?.querySelector('.notehead') || noteElement || marker?.anchor || measureElements.get(number);
      if (!element) return;
      const key = `${number}:${targetNote?.sourcePart || ''}:${targetNote?.staff || ''}`, rect = element.getBoundingClientRect(), viewport = stage.getBoundingClientRect();
      const outside = rect.top < viewport.top + 25 || rect.top > viewport.bottom - 55;
      const behavior = measureFollowAction({ view, playing, forceFollow: playbackOverride.forceFollow, follow, current, outside, key, previousKey: lastFollowed, jumped });
      if (behavior) stage.scrollTo({ top: stage.scrollTop + rect.top - viewport.top - 65, behavior });
      if (view === 'notation' && follow && (playing || playbackOverride.forceFollow)) lastFollowed = key;
    }
    setView('notation');
    await refresh(model);
    document.getElementById('zoom')?.addEventListener('change', event => { pages.style.width = `${event.target.value}%`; });
    return { update, refresh, setView, getSelected: () => [...selected], getModel: () => model };
  };
})();
