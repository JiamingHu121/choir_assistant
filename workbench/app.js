const $=id=>document.getElementById(id), audio=$('audio');
let score, canonicalModel=null, active=-1, solo=null, busy=false, interactive=null, pitchPanel=null, metronome=null, consolePanel=null, fullAudio=null, playback={offset:0,duration:0,selection:false,noteIds:null}, audioURL='/assets/base.mp3';
let groups=[];
function soundDefaults(){return {transpose:0,program:52,gains:{},...(score?.playbackSettings || canonicalModel?.playbackSettings || {})};}
const tempoUnits={'quarter':1,'dotted-quarter':1.5,'eighth':.5};
function initialQuarterBpm(){return canonicalModel?.tempoMetadata?.initialQuarterBpm || canonicalModel?.tempos?.[0]?.bpm || 120;}
function syncTempoControls(){
 $('speed-value').textContent=`${audio.playbackRate.toFixed(2)}×`;
 $('tempo-bpm').min=initialQuarterBpm()*.25/tempoUnits[$('tempo-unit').value];
 $('tempo-bpm').max=initialQuarterBpm()*2/tempoUnits[$('tempo-unit').value];
 $('tempo-bpm').value=Number((initialQuarterBpm()*audio.playbackRate/tempoUnits[$('tempo-unit').value]).toFixed(2));
}
function setPracticeRate(rate){
 if(!Number.isFinite(rate) || rate<.25 || rate>2){setStatus('练习速度倍率需要在 0.25–2× 之间。');syncTempoControls();return;}
 $('speed').value=rate;audio.playbackRate=rate;syncTempoControls();pitchPanel?.updateSettings();
}
function initializeTempo(model){
 const meta=model.tempoMetadata, unit=meta?.beatUnit;
 $('tempo-unit').value=unit in tempoUnits ? unit : 'quarter';
 const labels={'quarter':'四分音符','dotted-quarter':'附点四分音符','eighth':'八分音符'};
 $('tempo-source').textContent=meta ? `${meta.sourceText || '曲谱数字标记'} · ${meta.inferred?'推定':'谱面标注'} ${meta.initialBeatBpm} ${labels[$('tempo-unit').value]}/分${meta.unresolvedChanges ? '；部分速度变化待校对' : ''}` : '曲谱未含速度依据，使用可调的 120 四分音符/分。';
 audio.playbackRate=1;syncTempoControls();
}
function showPracticePosition(time,meta={}){
 if(time===null){active=-1;update();return;}
 interactive?.update(time,{playing:true,noteIds:meta.noteIds || []});
 const measure=score.measures.find(m=>time>=m.start && time<m.end);
 if(measure){$('position').textContent=`抗干扰跟唱 · 第 ${measure.number} 小节 · 原谱第 ${measure.page} 页`;$('now-measure').textContent=`第 ${measure.number} 小节 · 跟唱`;}
}
const fmt=s=>`${Math.floor(Math.max(0,s)/60)}:${String(Math.floor(Math.max(0,s)%60)).padStart(2,'0')}`;
function gains(){return Object.fromEntries(groups.map(g=>[g,solo&&solo!==g?0:Number($(`gain-${g}`).value)]));}
function setStatus(s){$('render-status').textContent=s;}
function showRhythmStatus(model){
 const badge=$('score-validity');if(!badge)return;
 const check=model.rhythmValidation;
 if(!check){badge.textContent='节奏尚未校验';return;}
 const pending=check.blockingMeasures || [];
 badge.textContent=check.status==='validated'?'节奏结构检查通过':`${pending.length} 个小节待校对`;
 badge.title=pending.length?`待校对小节：${pending.join('、')}。选取已通过节奏检查的片段可正常合成。`:'结构检查通过不等于逐音识谱已经准确。';
}
function updateLabels(){groups.forEach(g=>$(`value-${g}`).textContent=`${Math.round(Number($(`gain-${g}`).value)*100)}%`);}
function loopBounds(){
 const start=Number($('loop-start').value),end=Number($('loop-end').value);
 const a=score.measures.find(m=>m.number===start),b=score.measures.find(m=>m.number===end);
 const ok=Number.isInteger(start)&&Number.isInteger(end)&&a&&b&&end>=start;
 $('loop-error').textContent=ok?'':'请输入有效的小节范围，结束不能早于起始。';
 return ok?[a.start,b.end]:null;
}
function seek(t){
 if(playback.selection && (t<playback.offset || t>playback.offset+playback.duration)){
  switchAudio({audio:score.audio,wav:'/assets/base.wav',start:0,duration:score.duration,selection:false},false,t);return;
 }
 audio.currentTime=Math.max(0,Math.min(playback.duration,t-playback.offset));update({forceFollow:true});
}
async function switchAudio(result,autoplay=false,position=null){
 audio.pause();playback={offset:result.selection?result.start:0,duration:result.duration||score.duration,selection:!!result.selection,noteIds:result.selection?result.noteIds:null};
 if(!playback.selection)fullAudio=result;
 if(playback.selection)$('loop').checked=false;
 $('seek').max=playback.duration;$('duration').textContent=fmt(playback.duration);
 audioURL=result.audio;audio.src=result.audio;
 const loaded=new Promise((resolve,reject)=>{let timer;const cleanup=()=>{clearTimeout(timer);audio.removeEventListener('loadedmetadata',ready);audio.removeEventListener('error',fail);};const ready=()=>{cleanup();resolve();};const fail=()=>{cleanup();reject(new Error('音频加载失败'));};audio.addEventListener('loadedmetadata',ready,{once:true});audio.addEventListener('error',fail,{once:true});timer=setTimeout(fail,15000);});audio.load();
 $('download').href=result.wav||'/assets/base.wav';
 await loaded;audio.playbackRate=Number($('speed').value);
 if(position!==null)audio.currentTime=Math.max(0,Math.min(playback.duration,position-playback.offset));
 active=-1;update();if(autoplay)await audio.play();
}

function update(playbackOptions={}){
 if(!score)return;
 if(playback.selection && !audio.paused && audio.currentTime>=playback.duration+0.4){audio.pause();audio.currentTime=playback.duration;}
 const t=Math.min(audio.currentTime,Math.max(0,playback.duration-(playback.selection?0.0001:0)))+playback.offset, adjusted=t+Number($('offset').value);
 const index=score.measures.findIndex(m=>adjusted>=m.start&&adjusted<m.end);
 const i=index<0?(adjusted<0?0:score.measures.length-1):index;
 const m=score.measures[i];
 if(i!==active){
  document.querySelector('.measure.active')?.classList.remove('active');
  const box=$(`measure-${i}`);box?.classList.add('active');active=i;
  $('position').textContent=`${m.page&&score.pages.length?`原谱第 ${m.page} / ${score.pages.length} 页 · `:''}第 ${m.number} / ${score.measures.length} 小节`;
  $('now-measure').textContent=`第 ${m.number} 小节`;
  if(box&&$('follow').checked&&!$('original-view').hidden){
   const container=$('score-scroll'),rect=box.getBoundingClientRect(),viewport=container.getBoundingClientRect();
   if(rect.top<viewport.top+28||rect.bottom>viewport.bottom-28)container.scrollTo({top:container.scrollTop+rect.top-viewport.top-60,behavior:'smooth'});
  }
 }
 const box=$(`measure-${i}`),line=box?.querySelector('.playhead');
 if(line)line.style.left=`${Math.max(0,Math.min(1,(adjusted-m.start)/(m.end-m.start)))*100}%`;
 $('current-time').textContent=fmt(Math.min(audio.currentTime,playback.duration));$('seek').value=Math.min(audio.currentTime,playback.duration);
 interactive?.update(adjusted,playbackOptions);
 $('now-measure').textContent=`第 ${m.number} 小节${playback.selection?' · 选区':''}`;
 if($('loop').checked&&!audio.paused){const bounds=loopBounds();if(bounds&&(t>=bounds[1]||t<bounds[0]-.1)){audio.currentTime=bounds[0];}}
}
async function toggle(){
 try{
  if(audio.paused){if($('loop').checked){const b=loopBounds();if(!b)return;if(audio.currentTime<b[0]||audio.currentTime>=b[1])seek(b[0]);}await audio.play();}else audio.pause();
 }catch(e){setStatus(`无法播放：${e.message}`);}
}
async function applyMix(){
 if(busy)return;busy=true;$('apply').disabled=true;audio.pause();
 const position=audio.currentTime+playback.offset;setStatus('正在合成音频，完成后可继续从当前位置播放…');
 try{
  const response=await fetch('/api/render',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({transpose:Number($('transpose').value),program:Number($('program').value),gains:gains(),rhythmPolicy:$('draft-audio').checked?'draft':'strict'})});
  const data=await response.json();if(!response.ok)throw new Error(data.error||'生成失败');
  await switchAudio({...data,selection:false},false,position);
  $('download').href=data.wav;setStatus(data.audioValidation?.status==='draft'?'已生成草稿音频；待校对节奏仍按现有识谱结果试听。':'已应用声音参数，所用范围通过节奏检查。');
 }catch(e){setStatus(e.message);}finally{busy=false;$('apply').disabled=false;}
}
function reset(){
 if(busy)return;
 audio.pause();solo=null;
 const defaults=soundDefaults();
 groups.forEach(g=>{$(`gain-${g}`).value=defaults.gains[g]??1;$(`solo-${g}`).classList.remove('on');$(`solo-${g}`).setAttribute('aria-pressed','false');});updateLabels();
 $('speed').value=1;audio.playbackRate=1;$('speed-value').textContent='1.00×';
 initializeTempo(canonicalModel || {});metronome?.stop();$('metro-enabled').checked=false;
 $('volume').value=.8;audio.volume=.8;$('volume-value').textContent='80%';
 $('offset').value=0;$('offset-value').textContent='0.0 秒';$('program').value=defaults.program;$('transpose').value=defaults.transpose;$('loop').checked=false;
 $('draft-audio').checked=false;pitchPanel?.updateSettings();switchAudio({audio:score.audio,wav:'/assets/base.wav',duration:score.duration,selection:false},false);setStatus('已恢复默认声音和参数。');
}
async function init(){
 try{
  consolePanel=typeof initConsole==='function'?initConsole():null;
  const response=await fetch('/assets/score.json');if(!response.ok)throw new Error('未找到乐谱数据');score=await response.json();
  groups=[...new Set(score.groups || [])];
  document.title=`${score.title} · 乐谱工作台`;
  $('title').textContent=score.title;$('subtitle').textContent=score.subtitle;$('duration').textContent=fmt(score.duration);$('seek').max=score.duration;
  for(const p of score.pages){
   const page=document.createElement('div');page.className='page';page.id=`page-${p.page}`;
   const img=document.createElement('img');img.src=p.image;img.width=p.width;img.height=p.height;img.alt=`${score.title} 原谱第 ${p.page} 页`;if(p.page>2)img.loading='lazy';page.append(img);
   const label=document.createElement('span');label.className='page-label';label.textContent=`原谱 ${String(p.page).padStart(2,'0')}`;page.append(label);
   $('pages').append(page);
  }
  score.measures.forEach((m,i)=>{
   if(!$(`page-${m.page}`))return;
   const el=document.createElement('button');el.className='measure';el.id=`measure-${i}`;el.dataset.label=`小节 ${m.number}`;el.setAttribute('aria-label',`跳转第 ${m.page} 页，第 ${m.number} 小节`);el.title=`第 ${m.number} 小节 · ${fmt(m.start)}`;
   Object.assign(el.style,{left:`${m.x*100}%`,top:`${m.y*100}%`,width:`${m.w*100}%`,height:`${m.h*100}%`});
   el.innerHTML='<span class="playhead"></span>';el.addEventListener('click',()=>seek(m.start));$(`page-${m.page}`).append(el);
  });
  for(const g of groups){
   const row=document.createElement('div');row.className='mix-row';
   const gain=soundDefaults().gains[g]??1;
   row.innerHTML=`<span>${g==='Piano'?'钢琴':g}</span><input id="gain-${g}" aria-label="${g} 声部音量" type="range" min="0" max="1.5" step="0.05" value="${gain}"><output id="value-${g}">${Math.round(gain*100)}%</output><button id="solo-${g}" class="solo" aria-pressed="false" title="独听 ${g}">独听</button>`;
   $('mixer').append(row);$(`gain-${g}`).addEventListener('input',()=>{updateLabels();pitchPanel?.updateSettings();setStatus('声音参数已修改，点击「应用并生成音频」生效。');});
   $(`solo-${g}`).onclick=()=>{solo=solo===g?null:g;groups.forEach(x=>{$(`solo-${x}`).classList.toggle('on',solo===x);$(`solo-${x}`).setAttribute('aria-pressed',String(solo===x));});pitchPanel?.updateSettings();setStatus(solo?`已选择独听 ${solo}，点击应用生成。`:'已取消独听，点击应用生成。');};
  }
  for(let n=-12;n<=12;n++){const op=document.createElement('option');op.value=n;op.textContent=n===0?'原调':`${n>0?'+':''}${n} 半音`;$('transpose').append(op);}$('transpose').value=soundDefaults().transpose;$('program').value=soundDefaults().program;
  for(const id of ['loop-start','loop-end']){$(id).max=score.measures.length;$(id).onchange=loopBounds;}
  audio.src=score.audio;audio.volume=.8;audio.preservesPitch=true;playback.duration=score.duration;
  if(score.interactive){
   const response=await fetch('/assets/interactive.json');const canonical=await response.json();canonicalModel=canonical;initializeTempo(canonical);showRhythmStatus(canonical);
   const notice=$('source-review-status');
   if(notice)notice.textContent=`当前曲谱共 ${canonical.measures.length} 小节、${canonical.notes.length} 个音符；${canonical.rhythmValidation.blockingMeasures.length} 个小节存在节奏或声部结构问题。校准规则检查整份曲谱，修订需要原谱证据。结构检查通过与逐音识谱准确性分别记录；当前按谱面顺序播放，反复路线仍需复核。`;
   const currentSettings=()=>({transpose:Number($('transpose').value),program:Number($('program').value),gains:gains(),practiceRate:Number($('speed').value),rhythmPolicy:$('draft-audio').checked?'draft':'strict'});
   // The main media element applies the speed once. Speaker-backed practice
   // requests separately synthesized timing and plays it at buffer rate one.
   interactive=await initInteractive(canonical,{audio,seek,notationOverlayUrl:score.notationOverlay,originalAvailable:score.pages.length>0,settings:()=>{const {practiceRate,...sound}=currentSettings();return sound;},onSelection:ids=>pitchPanel?.setSelection(ids),onAudio:result=>switchAudio({...result,selection:true},true),restoreFull:()=>switchAudio(fullAudio||{audio:score.audio,wav:'/assets/base.wav',duration:score.duration,selection:false}),activeNoteIds:()=>playback.selection?new Set(playback.noteIds):null,onCorrection:()=>location.reload(),follow:()=>$('follow').checked});
   consolePanel?.syncPanels();
   if(typeof initMetronome==='function')metronome=initMetronome(canonical,{audio,getPlayback:()=>playback,getRate:()=>audio.playbackRate,getQuarterBpm:()=>{
    const t=audio.currentTime+playback.offset;const m=canonical.measures.find(m=>t>=m.start&&t<m.end)||canonical.measures[0];return MetronomeCore.tempoAt(canonical,m.onset).bpm;
   },onStatus:setStatus});
   if(typeof initPitchPanel==='function'){
    pitchPanel=await initPitchPanel(canonical,{getSettings:currentSettings,audio,pauseReference:()=>{audio.pause();metronome?.stop();},onStatus:setStatus,onPracticeTime:showPracticePosition});
    pitchPanel.setSelection(interactive.getSelected());
    const panel=$('pitch-host');consolePanel?.syncPanels();
    const jump=document.createElement('button');jump.className='pitch-jump';jump.textContent='音准练习';jump.setAttribute('aria-controls','pitch-host');
    jump.onclick=()=>{consolePanel?.show('pitch');panel.scrollIntoView({block:'nearest',behavior:'smooth'});};document.querySelector('.score-tools').append(jump);
   }
  }
  audio.addEventListener('play',()=>{$('play').textContent='Ⅱ';$('play').setAttribute('aria-label','暂停');});
  audio.addEventListener('pause',()=>{$('play').textContent='▶';$('play').setAttribute('aria-label','播放');update();});
  audio.addEventListener('timeupdate',update);audio.addEventListener('seeked',()=>update({forceFollow:true}));audio.addEventListener('error',()=>setStatus('音频加载失败，请检查本地服务是否仍在运行。'));
  $('play').onclick=toggle;$('restart').onclick=()=>seek($('loop').checked?(loopBounds()?.[0]||0):playback.offset);
  $('seek').oninput=e=>{audio.currentTime=Number(e.target.value);update({forceFollow:true});};
  $('speed').oninput=e=>setPracticeRate(Number(e.target.value));
  $('tempo-bpm').onchange=e=>setPracticeRate(Number(e.target.value)*tempoUnits[$('tempo-unit').value]/initialQuarterBpm());
  $('tempo-bpm').oninput=e=>{const rate=Number(e.target.value)*tempoUnits[$('tempo-unit').value]/initialQuarterBpm();if(rate>=.25 && rate<=2)setPracticeRate(rate);};
  $('tempo-unit').onchange=syncTempoControls;
  $('volume').oninput=e=>{audio.volume=Number(e.target.value);$('volume-value').textContent=`${Math.round(audio.volume*100)}%`;};
  $('offset').oninput=e=>{$('offset-value').textContent=`${Number(e.target.value).toFixed(1)} 秒`;update();};
  $('zoom').onchange=e=>{$('pages').style.width=`${e.target.value}%`;};
  $('follow').onchange=()=>{active=-1;update({forceFollow:true});};$('apply').onclick=applyMix;$('reset').onclick=reset;
  $('loop').onchange=()=>{if($('loop').checked){const b=loopBounds();if(b)seek(b[0]);else $('loop').checked=false;}};
  ['program','transpose'].forEach(id=>$(id).onchange=()=>{setStatus('声音参数已修改，点击「应用并生成音频」生效。');pitchPanel?.updateSettings();});
  $('draft-audio').onchange=()=>{pitchPanel?.updateSettings();setStatus($('draft-audio').checked?'允许生成草稿参考音；待校对节奏会保留在音频和音准报告中。':'生成和音准练习仅使用通过节奏检查的片段。');};
  document.addEventListener('keydown',e=>{if(e.code==='Space'&&!['INPUT','SELECT','BUTTON','TEXTAREA'].includes(document.activeElement.tagName)){e.preventDefault();toggle();}});
  const frame=()=>{if(!audio.paused)update();requestAnimationFrame(frame);};requestAnimationFrame(frame);update();
 }catch(e){$('position').textContent=`加载失败：${e.message}`;setStatus(e.message);}
}
init();
