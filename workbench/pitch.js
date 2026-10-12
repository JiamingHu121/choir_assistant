/* Singing feedback is local to this browser. Score pitches are never changed here. */
(() => {
  'use strict';
  const PARTS = {S:'女高音 S', A:'女低音 A', T:'男高音 T', B:'男低音 B', Piano:'钢琴'};
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const median = values => { const s = values.slice().sort((a,b) => a-b); return s.length ? (s[Math.floor((s.length-1)/2)] + s[Math.floor(s.length/2)]) / 2 : null; };
  const signed = (value, places=1) => Number.isFinite(value) ? `${value >= 0 ? '+' : ''}${value.toFixed(places)}` : '—';
  const hz = value => Number.isFinite(value) ? `${value.toFixed(1)} Hz` : '— Hz';
  const frequencyName = midi => {
    const names = ['C','C♯','D','E♭','E','F','F♯','G','A♭','A','B♭','B'];
    return `${names[((Math.round(midi)%12)+12)%12]}${Math.floor(Math.round(midi)/12)-1}`;
  };

  window.initPitchPanel = (initialModel, hooks={}) => {
    const host = document.getElementById('pitch-host');
    if (!host || !window.PitchCore) return null;
    const core = window.PitchCore;
    host.innerHTML = `<span class="eyebrow">音准 / 听与唱</span><h2>把声音对准这个音</h2>
      <p class="pitch-intro">先点选或框选音符。对照合成频谱，用麦克风或录音测量歌声的基频与音分偏差。</p>
      <p class="pitch-empty" id="pitch-empty">尚未选音。可先框选一个声部的几个音。</p>
      <div id="pitch-content" hidden>
      <div class="pitch-fields"><label>练习方式<select id="pitch-mode" aria-label="练习方式"><option value="fixed">保持一个音</option><option value="follow">跟随选定旋律</option><option value="interference">抗干扰跟唱（外放背景）</option></select></label>
      <label>目标声部<select id="pitch-part" aria-label="目标声部"></select></label><label class="wide">目标音<select id="pitch-target" aria-label="目标音"></select></label></div>
      <section id="pitch-background-panel" class="pitch-background-panel" hidden><div class="pitch-subheading">外放背景声部</div><div id="pitch-background-parts" class="pitch-background-parts"></div><label class="pitch-background-volume">背景音量<input id="pitch-background-volume" aria-label="背景音量" type="range" min="0.05" max="1" step="0.05" value="0.35"></label><p class="pitch-chart-caption">只播放所选时间段内的背景声部，目标声部从背景中排除。开始后先保持安静约 2 秒做外放校准，再倒数 2 秒开始唱。请固定设备与外放音量。</p><p class="pitch-interference-limit">实验模式：浏览器回声消除加已知伴奏校准；同音、共用泛音或回声不清时暂停评分。强混响或多人同时唱尚不能可靠分离。</p><p id="pitch-echo-state" class="pitch-chart-caption">麦克风尚未开启；将申请浏览器回声消除。</p><p class="pitch-chart-caption">此模式的绿色频谱为校准后的麦克风残余信号；不清楚的混合声音不计为音准结果。普通录音分析请切换到前两种方式。</p></section><div class="pitch-readout"><div class="pitch-target-label" id="pitch-target-label">目标 —</div>
      <div class="pitch-readout-line"><strong class="pitch-frequency" id="pitch-frequency">— Hz</strong><span class="pitch-cents" id="pitch-cents">— 音分</span></div>
      <div class="pitch-measurement-label" id="pitch-difference">等待歌声</div><div class="pitch-meter"><span class="pitch-needle" id="pitch-needle" hidden></span></div>
      <div class="pitch-meter-labels"><span>偏低 −100</span><span>准确 0</span><span>偏高 +100</span></div><div class="pitch-detection-state" id="pitch-confidence">基频估计会区分静音与不稳定声音。</div></div>
      <div class="pitch-actions"><button id="pitch-reference">合成参考频谱</button><button class="primary" id="pitch-mic">开始麦克风</button><button id="pitch-stop" disabled>停止</button></div>
      <audio id="pitch-reference-audio" class="pitch-reference-player" controls preload="metadata" hidden></audio>
      <p class="pitch-chart-caption">黄色为所选音的实际合成频谱，绿色为输入歌声；虚线为理论基频。横轴按频率对数排列，频谱峰值可能是泛音。</p>
      <div class="pitch-legend"><span>合成参考</span><span>歌声输入</span></div><canvas id="pitch-spectrum" class="pitch-chart" aria-label="合成参考与歌声频谱"></canvas>
      <p id="pitch-status" class="pitch-status" role="status" aria-live="polite">点击麦克风按钮后才申请权限；开始前自动暂停伴奏，请佩戴耳机。</p>
      <details class="pitch-targets"><summary id="pitch-targets-summary">所选音的理论频率</summary><div class="pitch-target-table-wrap"><table class="pitch-target-table"><thead><tr><th>音</th><th>时间 / 秒</th><th>频率</th></tr></thead><tbody id="pitch-target-table"></tbody></table></div></details>
      <details class="pitch-advanced"><summary>基准、容差与对齐</summary><div class="pitch-fields"><label>A4 基准 / Hz<input id="pitch-a4" aria-label="A4 基准 / Hz" type="number" min="400" max="480" step="0.1" value="440"></label><label>容差 / 音分<input id="pitch-tolerance" aria-label="容差 / 音分" type="number" min="1" max="100" step="1" value="20"></label><label class="wide">歌声起点偏移 / 秒<input id="pitch-alignment" aria-label="歌声起点偏移 / 秒" type="number" min="-30" max="300" step="0.05" value="0"></label></div><p class="pitch-chart-caption">正偏移表示歌声比目标旋律晚开始。麦克风先倒数 2 秒；偏移从倒数结束计起。旋律按工作台播放速度跟随。单音模式忽略节奏，持续比较所选目标。</p></details>
      <hr class="pitch-divider"><div class="pitch-subheading">录音分析与练习记录</div><label class="pitch-file-label">导入人声录音（仅浏览器处理）<input id="pitch-file" aria-label="导入人声录音" type="file" accept="audio/*,.wav,.mp3,.m4a,.ogg,.flac"></label>
      <div class="pitch-actions"><button id="pitch-analyze" disabled>重新分析录音</button><button id="pitch-report" disabled>下载音准记录</button></div>
      <p id="pitch-file-info" class="pitch-chart-caption">支持浏览器可解码的音频，最长 5 分钟、50 MB。单人清唱效果更稳定。</p>
      <canvas id="pitch-trajectory" class="pitch-chart pitch-trajectory" aria-label="歌声音分偏差随时间变化"></canvas><div id="pitch-summary" class="pitch-summary" hidden></div>
      <p class="pitch-chart-caption">音准反馈采用基频估计；八度误差保留原值。曲线超出 ±100 音分时在边缘显示标记。此面板用于指导练习，不会修改乐谱或自动修人声。</p>
      </div>`;
    const el = id => host.querySelector(`#pitch-${id}`);
    let model = initialModel, selected = [], timeline = null, ctx = null, stream = null, source = null, analyser = null, raf = null;
    let worker = null, workerId = 0, selectionVersion = 0, renderVersion = 0, micVersion = 0, fileVersion = 0, live = false, liveStart = 0;
    let referenceBuffer = null, referenceOffset = 0, vocalBuffer = null, inputName = null;
    let referenceSpectrum = null, vocalSpectrum = null, lastTarget = null, frames = [], analysis = null, sourceKind = null, pendingMic = false;
    let lastFrameAt = 0, lastStatusAt = 0, lastTargetKey = '', liveOptions = null;
    let backgroundParts=[], backgroundRun=null, backgroundNode=null, backgroundGain=null, interferenceInfo=null;
    let referenceState={allowed:true,referenceStatus:'legacy',message:''},referenceAudioValidation=null;
    const interferenceMode=()=>el('mode').value==='interference';
    const context = () => {
      if (!ctx) { const Audio = window.AudioContext || window.webkitAudioContext; if (!Audio) throw Error('当前浏览器不支持音频分析。'); ctx = new Audio(); }
      return ctx;
    };
    const value = (id, fallback, min, max) => { const n = Number(el(id).value); return Number.isFinite(n) ? Math.min(max, Math.max(min, n)) : fallback; };
    const settings = () => ({...(hooks.getSettings?.() || {}), a4:value('a4',440,400,480), toleranceCents:value('tolerance',20,1,100), offset:value('alignment',0,-30,300), mode:el('mode').value, part:el('part').value || null, targetId:el('target').value, backgroundParts:backgroundParts.slice(), backgroundVolume:value('background-volume',0.35,0.05,1), playbackRate:Number(hooks.getSettings?.().practiceRate || 1)});
    const analysisOptions = () => { const s=settings(); return {...s,targets:timeline,hopSeconds:0.06,windowSeconds:0.096,minFrequency:50,maxFrequency:1600,minRms:0.008,minConfidence:0.8}; };
    const status = (message, error=false) => { if(referenceState.referenceStatus==='draft' && referenceState.allowed)message=`草稿目标 · ${message}`;if(el('status').textContent!==message){el('status').textContent=message;hooks.onStatus?.(message);}el('status').classList.toggle('is-error',error); };
    const targetNow = referenceTime => core.targetAtTime(timeline,referenceTime,{mode:el('mode').value,targetId:el('target').value});
    const targetLabel = target => target ? `${frequencyName(target.pitch)} · ${PARTS[target.part] || target.part} · ${hz(target.frequency)}` : '此刻无单一目标音';
    const permitted = () => referenceState.allowed && Boolean(timeline?.targets.length) && (el('mode').value==='fixed' || timeline.monophonic) && (!interferenceMode() || backgroundParts.length>0);
    const noteIdsForReference = () => {
      if (!timeline) return [];
      if (el('mode').value==='fixed') return timeline.targets.find(t=>t.id===el('target').value)?.noteIds || [];
      return [...new Set(timeline.targets.flatMap(t=>t.noteIds))];
    };
    const cacheKey = () => JSON.stringify({revision:model.revision,ids:noteIdsForReference(),settings:settings()});
    const invalidateResult = () => { worker?.terminate();worker=null;++workerId;el('analyze').disabled=!vocalBuffer || !permitted() || interferenceMode();analysis=null; frames=[]; sourceKind=null;vocalSpectrum=null; el('summary').hidden=true; el('report').disabled=true; el('cents').textContent='— 音分';el('cents').className='pitch-cents';el('frequency').textContent='— Hz';el('difference').textContent='等待歌声';el('confidence').textContent='基频估计会区分静音与不稳定声音。';el('needle').hidden=true;drawTrajectory();drawSpectrum(); };
    function stopBackground() {
      try {backgroundNode?.stop();} catch (_) {}
      try {backgroundNode?.disconnect();backgroundGain?.disconnect();} catch (_) {}
      backgroundNode=null;backgroundGain=null;
    }
    function stopLive(message='麦克风已停止。') {
      ++micVersion;
      const wasLive=live || Boolean(stream) || pendingMic;pendingMic=false;
      live=false;stopBackground();backgroundRun=null; if(raf) cancelAnimationFrame(raf); raf=null;
      stream?.getTracks().forEach(track=>track.stop()); stream=null;
      try { source?.disconnect(); analyser?.disconnect(); } catch (_) {}
      source=null;analyser=null;el('stop').disabled=true;el('mic').disabled=!permitted();el('file').disabled=interferenceMode() || !referenceState.allowed;
      if(wasLive){hooks.onPracticeTime?.(null);if(frames.length){analysis={frames,summary:summarizeLive(frames),duration:Math.max(0,frames.at(-1).time)};sourceKind='microphone';renderSummary();el('report').disabled=false;}status(message);}
    }
    function summarizeLive(items) {
      const voiced=items.filter(f=>Number.isFinite(f.frequency)), matched=items.filter(f=>Number.isFinite(f.cents)), expected=items.filter(f=>Number.isFinite(f.targetFrequency));
      return {voicedFrames:voiced.length,matchedFrames:matched.length,expectedFrames:expected.length,medianCents:median(matched.map(f=>f.cents)),medianAbsoluteCents:median(matched.map(f=>Math.abs(f.cents))),meanAbsoluteCents:matched.length?matched.reduce((s,f)=>s+Math.abs(f.cents),0)/matched.length:null,inTuneFraction:matched.length?matched.filter(f=>Math.abs(f.cents)<=settings().toleranceCents).length/matched.length:null,coverage:expected.length?matched.length/expected.length:0};
    }
    function updateTargets(quiet=false) {
      stopLive('目标参数已改变，麦克风已停止。');
      worker?.terminate();worker=null;++workerId;++renderVersion;
      el('a4').value=value('a4',440,400,480);
      const notes=(model.notes || []).filter(n=>selected.includes(n.id) && n.kind==='note');
      const previousPart=el('part').value, previousTarget=el('target').value;
      const parts=[...new Set(notes.map(n=>n.part))].filter(p=>!interferenceMode() || p!=='Piano');
      el('part').innerHTML=parts.map(p=>`<option value="${escape(p)}">${escape(PARTS[p] || p)}</option>`).join('');
      if(parts.includes(previousPart)) el('part').value=previousPart;
      else if(parts.includes('S'))el('part').value='S';
      const allParts=[...new Set((model.notes || []).filter(n=>n.kind==='note').map(n=>n.part))].filter(p=>p!==el('part').value);
      backgroundParts=backgroundParts.filter(p=>allParts.includes(p));
      if(!backgroundParts.length)backgroundParts=allParts.filter(p=>p==='Piano');
      if(!backgroundParts.length && allParts.length)backgroundParts=[allParts[0]];
      el('background-parts').innerHTML=allParts.map(p=>`<label><input id="pitch-bg-${escape(p)}" type="checkbox" value="${escape(p)}" ${backgroundParts.includes(p)?'checked':''}>${escape(PARTS[p] || p)}</label>`).join('');
      for(const p of allParts){const checkbox=el(`bg-${p}`);checkbox?.addEventListener('change',()=>{if(checkbox.checked){if(!backgroundParts.includes(p))backgroundParts.push(p);}else backgroundParts=backgroundParts.filter(part=>part!==p);++selectionVersion;stopLive('背景声部已改变，请重新开始练习。');invalidateResult();el('mic').disabled=!permitted();});}
      el('background-panel').hidden=!interferenceMode();
      el('mic').textContent=interferenceMode()?'外放校准并开始跟唱':'开始麦克风';
      const s=settings();timeline=interferenceMode()?core.practiceRegion(model,selected,{transpose:Number(s.transpose || 0),a4:s.a4,part:s.part,backgroundParts}).timeline:core.buildTargets(model,{noteIds:selected,transpose:Number(s.transpose || 0),a4:s.a4,part:s.part});
      referenceState=core.referenceValidation(model,selected,{rhythmPolicy:s.rhythmPolicy});referenceAudioValidation=null;
      el('target').innerHTML=timeline.targets.map(t=>`<option value="${escape(t.id)}">${escape(frequencyName(t.pitch))} · ${t.start.toFixed(2)} s · ${hz(t.frequency)}</option>`).join('');
      if(timeline.targets.some(t=>t.id===previousTarget))el('target').value=previousTarget;
      el('target').disabled=s.mode!=='fixed';el('file').disabled=interferenceMode() || !referenceState.allowed;
      el('empty').hidden=Boolean(notes.length);el('content').hidden=!notes.length;
      el('mic').disabled=!permitted();el('reference').disabled=!timeline.targets.length || !referenceState.allowed;el('analyze').disabled=!vocalBuffer || !permitted() || interferenceMode();
      el('targets-summary').textContent=`所选声部 ${timeline.targets.length} 个目标音 · 理论频率${referenceState.referenceStatus==='draft'?' · 草稿参考':''}`;
      el('target-table').innerHTML=timeline.targets.map(t=>`<tr><td>${escape(frequencyName(t.pitch))}</td><td>${t.start.toFixed(2)}–${t.end.toFixed(2)}</td><td>${hz(t.frequency)}</td></tr>`).join('');
      el('target-label').textContent=`目标 ${targetLabel(targetNow(0) || timeline.targets[0])}`;
      invalidateResult();interferenceInfo=null;el('echo-state').textContent='麦克风尚未开启；将申请浏览器回声消除。';lastTargetKey='';lastTarget=targetNow(0) || timeline.targets[0];
      referenceSpectrum=null;vocalSpectrum=null;referenceBuffer=null;referenceOffset=0;
      el('reference-audio').pause();el('reference-audio').removeAttribute('src');el('reference-audio').hidden=true;
      drawSpectrum();
      if(!quiet && notes.length)status(!referenceState.allowed?referenceState.message:!permitted()?(!timeline.targets.length?'请先选择要唱声部的音符。':interferenceMode()&&!backgroundParts.length?'请选择至少一个背景声部。':'所选目标声部同时出现不同音高；请重新选单一旋律。'):interferenceMode()?'背景将在所选时间段内外放；先校准再倒数，结束后自动停止。':'目标已更新。可合成参考频谱，再开始麦克风或导入录音。',!permitted());
    }
    function updateMeasurement(frame, target) {
      lastTarget=target;
      el('target-label').textContent=`目标 ${targetLabel(target)}`;
      el('frequency').textContent=hz(frame.frequency);
      const matched=Number.isFinite(frame.cents);
      el('cents').textContent=matched?`${signed(frame.cents,0)} 音分`:'— 音分';
      el('cents').className=`pitch-cents ${matched ? (Math.abs(frame.cents)<=settings().toleranceCents?'in-tune':'off-tune'):''}`;
      el('needle').hidden=!matched;
      if(matched)el('needle').style.left=`${50+Math.max(-100,Math.min(100,frame.cents))/2}%`;
      el('difference').textContent=matched?`${frame.cents>0?'偏高':frame.cents<0?'偏低':'准确'} ${signed(frame.hzDifference)} Hz${Math.abs(frame.cents)>100?' · 超出表盘范围':''}`:target?'等待稳定、清晰的歌声':'休止或目标重叠，暂不评分';
      const reasons={silence:'静音 / 输入音量低',aperiodic:'未检测到稳定基频','out-of-range':'基频超出检测范围','insufficient-data':'等待音频帧','ambiguous-target':'多个目标音重叠','reference-only':'仅检测到背景音 / 无清晰人声','reference-dominated':'背景回声仍占主导，暂不评分','background-unison':'人声与背景同音或共享泛音，暂不评分','uncalibrated-reference':'外放校准不足，暂不评分'};
      el('confidence').textContent=`${frame.frequency?'周期稳定度 '+Math.round(frame.confidence*100)+'%':(reasons[frame.reason] || '等待稳定基频')} · 输入强度 ${(20*Math.log10(Math.max(frame.rms || 0,1e-6))).toFixed(0)} dBFS`;
      if(referenceBuffer && target?.id!==lastTargetKey){lastTargetKey=target?.id;referenceAt(Math.max(0,(target?.start || 0)-referenceOffset+0.06));}
    }
    function referenceAt(seconds) {
      if(!referenceBuffer)return;
      const sr=referenceBuffer.sampleRate, start=Math.max(0,Math.round(seconds*sr)), samples=new Float32Array(4096);
      const channels=referenceBuffer.numberOfChannels;
      for(let c=0;c<channels;c++){const data=referenceBuffer.getChannelData(c);for(let i=0;i<samples.length;i++)samples[i]+=(data[start+i] || 0)/channels;}
      referenceSpectrum=core.fftSpectrum(samples,sr,{fftSize:4096});drawSpectrum();
    }
    const chartContext = id => {
      const canvas=el(id), rect=canvas.getBoundingClientRect(), scale=Math.min(window.devicePixelRatio || 1,2);
      const w=Math.max(220,rect.width || 270),h=rect.height || 160;
      if(canvas.width!==Math.round(w*scale) || canvas.height!==Math.round(h*scale)){canvas.width=Math.round(w*scale);canvas.height=Math.round(h*scale);}
      const c=canvas.getContext('2d');c.setTransform(scale,0,0,scale,0,0);c.clearRect(0,0,w,h);return {c,w,h};
    };
    function drawSpectrum() {
      const {c,w,h}=chartContext('spectrum'), left=30,right=w-9,top=12,bottom=h-24;
      const x=f=>left+Math.log(Math.max(40,f)/40)/Math.log(100)*(right-left), y=db=>bottom-(Math.max(-100,Math.min(0,db))+100)/100*(bottom-top);
      c.font='9px system-ui';c.lineWidth=1;c.strokeStyle='#e5eae0';c.fillStyle='#809080';
      for(const db of [-80,-40,0]){c.beginPath();c.moveTo(left,y(db));c.lineTo(right,y(db));c.stroke();c.fillText(`${db}`,3,y(db)+3);}
      for(const f of [50,100,200,500,1000,2000,4000]){const px=x(f);c.beginPath();c.moveTo(px,top);c.lineTo(px,bottom);c.stroke();c.fillText(f>=1000?`${f/1000}k`:String(f),px-8,h-9);}
      const line=(spec,color)=>{if(!spec)return;c.beginPath();let began=false;for(let i=0;i<spec.frequencies.length;i++){const f=spec.frequencies[i];if(f<40 || f>4000)continue;const px=x(f),py=y(spec.db[i]);if(!began){c.moveTo(px,py);began=true;}else c.lineTo(px,py);}c.strokeStyle=color;c.lineWidth=1.2;c.stroke();};
      line(referenceSpectrum,'#c49245');line(vocalSpectrum,'#218460');
      const target=lastTarget || targetNow(0);
      if(target){const px=x(target.frequency);if(px<=right){c.setLineDash([3,3]);c.strokeStyle='#71826b';c.beginPath();c.moveTo(px,top);c.lineTo(px,bottom);c.stroke();c.setLineDash([]);c.fillStyle='#596d59';c.fillText(hz(target.frequency),Math.min(right-52,Math.max(left,px+3)),top+8);}}
      if(!referenceSpectrum && !vocalSpectrum){c.fillStyle='#8a9687';c.textAlign='center';c.fillText('合成参考或输入歌声后显示频谱',(left+right)/2,(top+bottom)/2);c.textAlign='left';}
    }
    function drawTrajectory() {
      const {c,w,h}=chartContext('trajectory'),left=31,right=w-9,top=12,bottom=h-23;
      const duration=Math.max(1,analysis?.duration || frames.at(-1)?.time || timeline?.duration || 1);
      const x=t=>left+Math.max(0,t)/duration*(right-left),y=cents=>bottom-(Math.max(-100,Math.min(100,cents))+100)/200*(bottom-top),tol=settings().toleranceCents;
      c.fillStyle='#eaf3e5';c.fillRect(left,y(tol),right-left,y(-tol)-y(tol));c.font='9px system-ui';c.fillStyle='#809080';c.strokeStyle='#e1e8dc';
      for(const cents of [-100,0,100]){c.beginPath();c.moveTo(left,y(cents));c.lineTo(right,y(cents));c.stroke();c.fillText(String(cents),3,y(cents)+3);}
      for(let i=0;i<=4;i++){const t=duration*i/4;c.fillText(`${t.toFixed(duration>30?0:1)}s`,x(t)-7,h-8);}
      const items=analysis?.frames || frames;c.strokeStyle='#22825a';c.lineWidth=1.3;c.beginPath();let began=false;
      for(const f of items){if(!Number.isFinite(f.cents)){began=false;continue;}const px=x(f.time),py=y(f.cents);if(!began){c.moveTo(px,py);began=true;}else c.lineTo(px,py);}
      c.stroke();c.fillStyle='#b87831';for(const f of items){if(Number.isFinite(f.cents)&&Math.abs(f.cents)>100){const px=x(f.time),py=y(f.cents);c.fillRect(px-1.2,py-1.5,2.4,3);}}
      if(!items.length){c.fillStyle='#8a9687';c.textAlign='center';c.fillText('偏差随时间显示在这里',(left+right)/2,(top+bottom)/2);c.textAlign='left';}
    }
    function renderSummary() {
      const s=analysis?.summary;if(!s)return;
      const ratio=v=>Number.isFinite(v)?`${(v*100).toFixed(0)}%`:'—';
      el('summary').hidden=false;
      el('summary').textContent=s.matchedFrames?`可比较覆盖率 ${ratio(s.coverage)} · ${s.matchedFrames} 帧；有符号偏差中位数 ${signed(s.medianCents)} 音分，绝对偏差中位数 ${Number.isFinite(s.medianAbsoluteCents)?s.medianAbsoluteCents.toFixed(1):'—'} 音分。容差 ±${settings().toleranceCents} 音分内 ${ratio(s.inTuneFraction)}（仅按可比较帧统计）。`:'没有可比较的稳定人声帧。检查输入音量、目标声部和歌声起点偏移。';
      drawTrajectory();
    }
    async function synthesizeReference() {
      const requestVersion=++renderVersion, key=cacheKey(), s=settings(), ids=noteIdsForReference();
      if(!referenceState.allowed){status(referenceState.message,true);return;}if(!ids.length)return;stopLive();el('reference').disabled=true;status('正在合成所选音并读取实际频谱…');
      try {
        if(s.gains && Number(s.gains[s.part])===0)throw Error('目标声部目前静音。请先调整工作台声部音量，再生成参考。');
        const response=await fetch('/api/selection',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({noteIds:ids,transpose:Number(s.transpose || 0),program:Number(s.program ?? 52),gains:s.gains,a4:s.a4,rhythmPolicy:s.rhythmPolicy || 'strict'})});
        const result=await response.json();if(!response.ok)throw Error(result.error || '参考音频合成失败');
        const audioResponse=await fetch(result.wav);if(!audioResponse.ok)throw Error('参考 WAV 读取失败');
        const decoded=await context().decodeAudioData(await audioResponse.arrayBuffer());
        if(requestVersion!==renderVersion || key!==cacheKey())return;
        referenceBuffer=decoded;referenceOffset=Number(result.start || 0)-timeline.origin;referenceAudioValidation=result.audioValidation || null;
        const audio=el('reference-audio');audio.src=result.wav;audio.playbackRate=s.playbackRate;audio.preservesPitch=true;audio.hidden=false;
        const target=targetNow(0) || timeline.targets[0];lastTarget=target;referenceAt(Math.max(0,(target?.start || 0)-referenceOffset+0.06));
        status('实际合成频谱已就绪。可播放参考音；开始测音准前暂停播放并佩戴耳机。');
      }catch(error){if(requestVersion===renderVersion)status(error.message,true);}finally{if(requestVersion===renderVersion)el('reference').disabled=!timeline?.targets.length || !referenceState.allowed;}
    }
    async function loadBackground(token, options) {
      const region=core.practiceRegion(model,selected,{...options,backgroundParts});
      if(!region.backgroundIds.length)throw Error('这些背景声部在所选时间段内没有音符，请换一个时间段或声部。');
      if(region.backgroundParts.every(p=>Number(options.gains?.[p] ?? 1)<=0))throw Error('所选背景声部目前在混音器中静音。请调高背景声部音量后重试。');
      const gains={...(options.gains || {})};for(const p of Object.keys(PARTS))if(!region.backgroundParts.includes(p))gains[p]=0;
      const response=await fetch('/api/selection',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({noteIds:region.backgroundIds,transpose:Number(options.transpose || 0),program:Number(options.program ?? 52),gains,a4:options.a4,practiceRate:options.playbackRate,rhythmPolicy:options.rhythmPolicy || 'strict'})});
      const result=await response.json();if(!response.ok)throw Error(result.error || '背景音合成失败');
      if(token!==micVersion)return null;
      if(result.revision && result.revision!==model.revision)throw Error('乐谱已更新，请刷新后重新选择练习音符。');
      referenceAudioValidation=result.audioValidation || null;
      if(options.playbackRate!==1 && Number(result.practiceRate)!==options.playbackRate)throw Error('当前服务尚不支持保留音高的背景变速，请将工作台播放速度设为 1× 后重试。');
      const fetched=await fetch(result.wav);if(!fetched.ok)throw Error('背景 WAV 读取失败');
      const buffer=await context().decodeAudioData(await fetched.arrayBuffer());
      if(token!==micVersion)return null;
      const data=new Float32Array(buffer.length);for(let channel=0;channel<buffer.numberOfChannels;channel++){const pcm=buffer.getChannelData(channel);for(let i=0;i<data.length;i++)data[i]+=pcm[i]/buffer.numberOfChannels;}
      const offset=Math.max(0,(region.origin-Number(result.start || 0))/options.playbackRate);
      const leadIn=Math.max(0,(Number(result.start || 0)-region.origin)/options.playbackRate);
      const firstNote=Math.min(...region.background.map(n=>Math.max(region.origin,n.start)));
      return {buffer,data,region,rate:options.playbackRate,offset,leadIn,calibrationOffset:Math.max(0,(firstNote-Number(result.start || 0))/options.playbackRate+0.04),volume:options.backgroundVolume,sampleRate:buffer.sampleRate,calibrationFrames:[],calibration:null};
    }
    function playBackground(run, when, calibration=false) {
      stopBackground();const audioContext=context(),node=audioContext.createBufferSource(),gain=audioContext.createGain();
      node.buffer=run.buffer;gain.gain.value=run.volume;node.connect(gain);gain.connect(audioContext.destination);
      const offset=Math.min(run.buffer.duration-0.01,calibration?run.calibrationOffset:run.offset);
      const duration=calibration?Math.min(1.75,run.buffer.duration-offset):Math.min(run.region.duration/run.rate-run.leadIn,run.buffer.duration-offset);
      const actualWhen=when+(calibration?0:run.leadIn);
      node.start(actualWhen,Math.max(0,offset),Math.max(0.01,duration));
      backgroundNode=node;backgroundGain=gain;
      run.playback={when:actualWhen,offset,duration,calibration};run.phase=calibration?'calibration':'singing';
    }
    function backgroundReference(run, now, length) {
      const sr=context().sampleRate,delay=Math.ceil(sr*0.25),reference=new Float32Array(length+delay),play=run.playback;
      if(!play)return reference;
      for(let i=0;i<reference.length;i++){
        const time=now-(length+delay-i)/sr,elapsed=time-play.when;
        if(elapsed<0 || elapsed>=play.duration)continue;
        const position=(play.offset+elapsed)*run.sampleRate,index=Math.floor(position),fraction=position-index;
        reference[i]=((run.data[index] || 0)*(1-fraction)+(run.data[index+1] || 0)*fraction)*run.volume;
      }
      return reference;
    }
    function backgroundFrequencies(run, referenceTime) {
      const absolute=timeline.origin+referenceTime;
      return [...new Set(run.region.background.filter(n=>n.start<=absolute && n.end>absolute).map(n=>core.midiToHz(n.pitch+Number(liveOptions.transpose || 0),liveOptions.a4)))];
    }
    async function startMic() {
      if(!permitted())return;
      if(!navigator.mediaDevices?.getUserMedia){status('此浏览器无法访问麦克风。请用 localhost 安全页面，或导入录音分析。',true);return;}
      stopLive();invalidateResult();++fileVersion;sourceKind='microphone';const token=++micVersion;
      hooks.pauseReference?.();hooks.audio?.pause();el('reference-audio').pause();el('mic').disabled=true;
      pendingMic=true;el('stop').disabled=false;status(interferenceMode()?'正在合成外放背景音…':'正在申请麦克风权限…');
      try {
        const audioContext=context();await audioContext.resume();
        if(token!==micVersion)return;
        const useInterference=interferenceMode(),options=analysisOptions();
        const prepared=useInterference?await loadBackground(token,options):null;
        if(token!==micVersion)return;
        status('正在申请麦克风权限…');
        const incoming=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:useInterference,noiseSuppression:useInterference,autoGainControl:false,channelCount:1},video:false});
        if(token!==micVersion){incoming.getTracks().forEach(t=>t.stop());return;}
        pendingMic=false;stream=incoming;
        stream.getTracks().forEach(track=>track.addEventListener('ended',()=>{
          if(token===micVersion && stream===incoming)stopLive('麦克风连接已结束。');
        },{once:true}));
        source=audioContext.createMediaStreamSource(stream);analyser=audioContext.createAnalyser();analyser.fftSize=4096;analyser.smoothingTimeConstant=0.25;source.connect(analyser);
        live=true;liveStart=audioContext.currentTime+2;liveOptions=options;
        if(useInterference){
          backgroundRun=prepared;const audioTrack=incoming.getTracks()[0];
          const capabilities=audioTrack?.getCapabilities?.() || {};let allEchoRequested=false;
          if(Array.isArray(capabilities.echoCancellation) && capabilities.echoCancellation.includes('all') && audioTrack.applyConstraints){
            try {allEchoRequested=true;await audioTrack.applyConstraints({echoCancellation:'all'});} catch (_) {}
            if(token!==micVersion)return;
          }
          const applied=audioTrack?.getSettings?.() || {};
          interferenceInfo={mode:'experimental-reference-aware',backgroundParts:prepared.region.backgroundParts.slice(),targetPart:options.part,backgroundNoteIds:prepared.region.backgroundIds.slice(),volume:prepared.volume,aec:{requested:true,allRequested:allEchoRequested,applied:applied.echoCancellation ?? null,noiseSuppression:applied.noiseSuppression ?? null,autoGainControl:applied.autoGainControl ?? null},calibration:null,limits:['shared-pitch-or-harmonics-not-scored','linear-frozen-echo-model','physical-speaker-microphone-not-validated']};
          el('echo-state').textContent=`浏览器回声消除：${(applied.echoCancellation===true || applied.echoCancellation==='all')?'已启用':applied.echoCancellation===false?'未启用':'设备未报告'} · 先安静做外放校准。`;
          playBackground(prepared,audioContext.currentTime+0.1,true);
        }el('stop').disabled=false;el('file').disabled=true;frames=[];lastFrameAt=0;lastStatusAt=0;
        const samples=new Float32Array(analyser.fftSize);
        const tick=()=>{
          if(!live)return;
          raf=requestAnimationFrame(tick);const now=audioContext.currentTime;if(now-lastFrameAt<0.095)return;lastFrameAt=now;
          analyser.getFloatTimeDomainData(samples);
          if(backgroundRun?.phase==='calibration'){
            const run=backgroundRun,calibrationElapsed=now-run.playback.when;
            if(calibrationElapsed>0.3 && calibrationElapsed<run.playback.duration+0.2){run.calibrationFrames.push({samples:samples.slice(),reference:backgroundReference(run,now,samples.length)});}
            if(calibrationElapsed<run.playback.duration+0.22){status('外放校准中，请保持安静；稍后倒数再开始唱。');vocalSpectrum=core.fftSpectrum(samples,audioContext.sampleRate,{fftSize:4096});drawSpectrum();return;}
            run.calibration=core.calibrateReference(run.calibrationFrames,audioContext.sampleRate);run.calibrationFrames=[];
            interferenceInfo.calibration={...run.calibration};
            if(!run.calibration.valid && ![true,'all'].includes(interferenceInfo.aec.applied)){stopLive('外放校准不足，且设备未确认回声消除。请降低外放音量或改用耳机和“跟随选定旋律”。');return;}
            el('echo-state').textContent=`浏览器回声消除：${[true,'all'].includes(interferenceInfo.aec.applied)?'已启用':'未确认'} · ${run.calibration.valid?`外放校准完成，延迟约 ${Math.round(run.calibration.delaySeconds*1000)} ms`:'回声已很弱，采用保守检测'}。同音或泛音重叠时暂停评分。`;
            stopBackground();run.phase='count-in';liveStart=now+2;
            // The second start reuses precisely the same decoded PCM at region origin.
            playBackground(run,liveStart,false);run.phase='count-in';
          }
          if(backgroundRun?.phase==='count-in' && now>=liveStart)backgroundRun.phase='singing';
          vocalSpectrum=core.fftSpectrum(samples,audioContext.sampleRate,{fftSize:4096});
          // The analyser window ends at now; compare its center with the score.
          const elapsed=now-liveStart-samples.length/(2*audioContext.sampleRate);
          if(elapsed<0){status(`准备 ${Math.ceil(-elapsed)} 秒… 倒数结束后开始唱。`);drawSpectrum();return;}
          const referenceTime=(elapsed-liveOptions.offset)*liveOptions.playbackRate;
          if(liveOptions.mode!=='fixed' && referenceTime>timeline.duration+0.12){stopLive('选定旋律已结束，麦克风已停止。可再次开始练习。');return;}
          let detection;
          if(backgroundRun){
            const backgroundClock=elapsed*liveOptions.playbackRate;
            const audibleBackgroundClock=(elapsed-(backgroundRun.calibration?.valid?backgroundRun.calibration.delaySeconds:0))*liveOptions.playbackRate;
            hooks.onPracticeTime?.(timeline.origin+backgroundClock,{noteIds:backgroundRun.region.backgroundIds});
            const assessed=core.assessInterference(samples,backgroundReference(backgroundRun,now,samples.length),backgroundRun.calibration,audioContext.sampleRate,{...liveOptions,backgroundFrequencies:backgroundFrequencies(backgroundRun,audibleBackgroundClock)});
            vocalSpectrum=core.fftSpectrum(assessed.samples,audioContext.sampleRate,{fftSize:4096});
            const {samples:residual,...metadata}=assessed;detection=metadata;
          }else detection=core.detectPitch(samples,audioContext.sampleRate,liveOptions);
          const target=core.targetAtTime(timeline,referenceTime,{mode:liveOptions.mode,targetId:liveOptions.targetId});
          const measured=Number.isFinite(detection.frequency)&&target;
          const frame={time:elapsed,referenceTime,...detection,targetId:target?.id || null,targetFrequency:target?.frequency || null,cents:measured?core.centsDifference(detection.frequency,target.frequency):null,hzDifference:measured?detection.frequency-target.frequency:null};
          frames.push(frame);if(frames.length>6000){stopLive('练习已达 10 分钟，麦克风已停止。');return;}
          updateMeasurement(frame,target);drawSpectrum();drawTrajectory();el('report').disabled=false;
          const latency=`分析窗约 ${Math.round(samples.length/audioContext.sampleRate*1000)} ms，另有设备延迟。`;
          if(now-lastStatusAt>=1){lastStatusAt=now;status(liveOptions.mode==='interference'?`抗干扰跟唱 · ${Math.max(0,referenceTime).toFixed(1)} / ${timeline.duration.toFixed(1)} 秒 · 背景 ${backgroundParts.map(p=>PARTS[p] || p).join('、')}。${latency}`:liveOptions.mode==='follow'?`正在跟随旋律 · ${Math.max(0,referenceTime).toFixed(1)} / ${timeline.duration.toFixed(1)} 秒。${latency}`:`正在测量单音。完成后点击停止。${latency}`);}
        };raf=requestAnimationFrame(tick);
      }catch(error){if(token===micVersion){stopLive();el('mic').disabled=!permitted();status(error.name==='NotAllowedError'?'麦克风权限未获准。可在浏览器允许麦克风，或导入已有录音。':`无法启动麦克风：${error.message}`,true);}}
    }
    async function importRecording(event) {
      const file=event.target.files?.[0];if(!file)return;if(!referenceState.allowed){status(referenceState.message,true);return;}const selectionToken=selectionVersion, token=++fileVersion;stopLive();invalidateResult();worker?.terminate();worker=null;++workerId;
      if(file.size>50*1024*1024){status('录音大于 50 MB，请先截取练习片段。',true);event.target.value='';return;}
      status('正在本地解码录音…');
      try {
        const buffer=await context().decodeAudioData(await file.arrayBuffer());
        if(token!==fileVersion)return;
        if(buffer.duration>300)throw Error('录音超过 5 分钟，请先截取练习片段。');
        vocalBuffer=buffer;inputName=file.name;el('file-info').textContent=`${file.name} · ${buffer.duration.toFixed(2)} 秒 · ${buffer.sampleRate} Hz · ${buffer.numberOfChannels} 声道（分析时混为单声道）。`;
        el('analyze').disabled=!permitted() || interferenceMode();if(selectionToken!==selectionVersion){status('录音已读取；所选音已改变，点击重新分析。');return;}if(!permitted()){status('录音已读取。所选声部包含重叠音高，请改用保持一个音或选单一旋律。',true);return;}await analyzeRecording();
      }catch(error){if(token!==fileVersion)return;vocalBuffer=null;el('analyze').disabled=true;status(`录音读取失败：${error.message}`,true);}
    }
    async function analyzeRecording() {
      if(!vocalBuffer || !permitted())return;
      if(interferenceMode()){status('抗干扰模式用于麦克风与本工作台已知外放背景的实时比较。普通录音请改用“保持一个音”或“跟随选定旋律”分析。',true);return;}
      stopLive();invalidateResult();sourceKind='recording';worker?.terminate();const id=++workerId;
      const local=vocalBuffer, options=analysisOptions(), samples=new Float32Array(local.length);
      for(let c=0;c<local.numberOfChannels;c++){const data=local.getChannelData(c);for(let i=0;i<samples.length;i++)samples[i]+=data[i]/local.numberOfChannels;}
      el('analyze').disabled=true;status(`正在浏览器中分析 ${local.duration.toFixed(1)} 秒录音…`);
      try {
        worker=new Worker('/pitch-worker.js');
        worker.onmessage=event=>{
          if(event.data.id!==workerId)return;worker.terminate();worker=null;el('analyze').disabled=!permitted() || interferenceMode();
          if(event.data.error){status(`录音分析失败：${event.data.error}`,true);return;}
          analysis=event.data.result;frames=analysis.frames;sourceKind='recording';renderSummary();el('report').disabled=false;
          const representative=frames.find(f=>Number.isFinite(f.cents)) || frames.find(f=>Number.isFinite(f.frequency)) || frames[0];
          if(representative){const target=core.targetAtTime(timeline,representative.referenceTime,{mode:options.mode,targetId:options.targetId});updateMeasurement(representative,target);
            const start=Math.max(0,Math.round((representative.time-0.04)*local.sampleRate)), chunk=new Float32Array(4096);for(let i=0;i<chunk.length;i++)chunk[i]=samples[start+i] || 0;vocalSpectrum=core.fftSpectrum(chunk,local.sampleRate,{fftSize:4096});drawSpectrum();}
          status(`录音分析完成。${analysis.summary.matchedFrames || 0} 帧可与目标比较；调整对齐后可重新分析。`);
        };
        worker.onerror=event=>{if(id!==workerId)return;worker?.terminate();worker=null;el('analyze').disabled=!permitted() || interferenceMode();status(`分析工作线程不可用：${event.message || '请刷新页面后重试'}`,true);};
        // Preserve the local samples for the representative spectrum; only the copy transfers.
        const transferred=samples.slice();worker.postMessage({id,samples:transferred.buffer,sampleRate:local.sampleRate,options},[transferred.buffer]);
      }catch(error){el('analyze').disabled=!permitted() || interferenceMode();status(error.message,true);}
    }
    function downloadReport() {
      const current=analysis || {frames,summary:summarizeLive(frames),duration:frames.at(-1)?.time || 0};if(!current.frames.length)return;
      const report={schema:'sheet-to-audio.pitch-session/v1',createdAt:new Date().toISOString(),revision:model.revision,source:sourceKind || 'microphone',inputName:sourceKind==='recording'?inputName:null,selectedNoteIds:selected.slice(),parameters:settings(),referenceStatus:referenceState.referenceStatus,referenceValidation:referenceState,audioValidation:referenceAudioValidation,targets:timeline,interference:interferenceInfo,algorithm:{name:'YIN',minFrequency:50,maxFrequency:1600,minConfidence:0.8,minRms:0.008,sampleRate:sourceKind==='microphone'?ctx?.sampleRate:current.sampleRate,frameSeconds:sourceKind==='microphone'?4096/(ctx?.sampleRate || 48000):current.options?.windowSeconds || 0.096},...current};
      const url=URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=`pitch-${model.revision || 'session'}-${Date.now()}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);
    }
    el('reference').addEventListener('click',synthesizeReference);el('mic').addEventListener('click',startMic);el('stop').addEventListener('click',()=>stopLive());el('file').addEventListener('change',importRecording);el('analyze').addEventListener('click',analyzeRecording);el('report').addEventListener('click',downloadReport);
    for(const id of ['part','mode','a4'])el(id).addEventListener('change',()=>{++selectionVersion;updateTargets();});
    el('target').addEventListener('change',()=>{++selectionVersion;stopLive();invalidateResult();++renderVersion;lastTarget=targetNow(0);el('target-label').textContent=`目标 ${targetLabel(lastTarget)}`;lastTargetKey='';referenceBuffer=null;referenceSpectrum=null;referenceAudioValidation=null;el('reference-audio').pause();el('reference-audio').removeAttribute('src');el('reference-audio').hidden=true;el('reference').disabled=!timeline?.targets.length || !referenceState.allowed;drawSpectrum();status(referenceState.allowed?'目标音已改变；重新合成以播放当前单音参考。':referenceState.message,!referenceState.allowed);});
    el('background-volume').addEventListener('change',()=>{++selectionVersion;stopLive('背景音量已改变，请重新做外放校准。');invalidateResult();});
    for(const id of ['tolerance','alignment'])el(id).addEventListener('change',()=>{++selectionVersion;if(id==='tolerance')el(id).value=value(id,20,1,100);else el(id).value=value(id,0,-30,300);stopLive();invalidateResult();status('比较参数已改变，请重新分析录音或开始麦克风。');});
    el('reference-audio').addEventListener('play',()=>{if(live || pendingMic)stopLive('播放参考音时已停止麦克风，避免伴奏影响测量。');});
    el('reference-audio').addEventListener('timeupdate',()=>{if(!referenceBuffer)return;const t=el('reference-audio').currentTime;lastTarget=targetNow(t+referenceOffset);el('target-label').textContent=`目标 ${targetLabel(lastTarget)}`;referenceAt(t);});
    const resize=()=>{drawSpectrum();drawTrajectory();};const observer=new ResizeObserver(resize);observer.observe(host);
    const visibility=()=>{if(document.hidden)stopLive('页面切到后台，麦克风已停止。');};document.addEventListener('visibilitychange',visibility);
    const unload=()=>stopLive();window.addEventListener('pagehide',unload);
    const mainPlayback=()=>{if(live || pendingMic)stopLive('工作台开始播放，麦克风已停止，避免伴奏影响测量。');};hooks.audio?.addEventListener('play',mainPlayback);
    updateTargets(true);
    return {
      setSelection(ids,nextModel){
        const nextIds=[...new Set(ids || [])];
        const sameIds=nextIds.length===selected.length&&nextIds.every(id=>selected.includes(id));
        const sameRevision=!nextModel || (nextModel.revision || nextModel.version)===(model.revision || model.version);
        if(sameIds&&sameRevision)return;
        if(nextModel)model=nextModel;
        selected=nextIds;++selectionVersion;updateTargets();
      },
      updateSettings(){++selectionVersion;updateTargets();},
      stop(){stopLive();},
      interferenceState(){return interferenceInfo;},
      dispose(){
        stopLive();++fileVersion;++workerId;++renderVersion;++selectionVersion;
        worker?.terminate();worker=null;observer.disconnect();
        document.removeEventListener('visibilitychange',visibility);
        window.removeEventListener('pagehide',unload);
        hooks.audio?.removeEventListener('play',mainPlayback);ctx?.close();
      }
    };
  };
})();
