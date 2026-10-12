/* Musical beat positions use quarter-note units, then the canonical tempo map. */
(function (root, factory) {
  const core = factory();
  if (typeof module === 'object' && module.exports) module.exports = core;
  else { root.MetronomeCore = core; root.initMetronome = core.initMetronome; }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  function tempoMap(model) {
    const map = (model.tempos || []).filter(t => Number.isFinite(t.onset) && t.onset >= 0 && t.bpm > 0).sort((a,b) => a.onset-b.onset);
    if (!map.length || map[0].onset > 0) map.unshift({onset:0,bpm:model.tempoMetadata?.initialQuarterBpm || 120});
    return map;
  }
  function secondsAtBeat(model, beat) {
    const map = tempoMap(model); let seconds = 0;
    for (let i=0;i<map.length;i++) {
      const end = Math.min(beat, map[i+1]?.onset ?? beat);
      if (end > map[i].onset) seconds += (end-map[i].onset)*60/map[i].bpm;
      if (end >= beat) break;
    }
    return seconds;
  }
  function tempoAt(model, beat) {
    const map = tempoMap(model); return [...map].reverse().find(t => t.onset <= beat) || map[0];
  }
  function meterInfo(measure) {
    const match = /^(\d+)\/(\d+)$/.exec(measure.timeSignature?.text || '4/4');
    const top = match ? Number(match[1]) : 4, bottom = match ? Number(match[2]) : 4;
    const compound = (bottom === 8 || bottom === 16) && top >= 6 && top%3 === 0;
    const pulse = compound ? 12/bottom : 4/bottom;
    return {pulse, count:compound ? top/3 : top, expected:top*4/bottom, text:`${top}/${bottom}`};
  }
  function buildBeatGrid(model, subdivision='beat') {
    const grid=[];
    for (const m of model.measures || []) {
      const meter=meterInfo(m), step=subdivision === 'eighth' ? .5 : meter.pulse;
      // A pickup occupies the end of its bar; it does not acquire an artificial
      // downbeat. Overfull OMR bars retain their audited canonical duration.
      const pickup = m.implicit && m.duration < meter.expected ? meter.expected-m.duration : 0;
      let offset = pickup > 0 ? (step-pickup%step)%step : 0;
      for (;offset < m.duration-1e-7;offset+=step) {
        const phase=pickup+offset, pulseIndex=Math.floor((phase+1e-7)/meter.pulse);
        const major=Math.abs(phase/meter.pulse-Math.round(phase/meter.pulse)) < 1e-7;
        const beat=m.onset+offset;
        grid.push({time:secondsAtBeat(model,beat),beat,measure:m.number,index:pulseIndex+1,count:meter.count,
          accent:major && pulseIndex===0,major,meter:meter.text,pulse:meter.pulse,bpm:tempoAt(model,beat).bpm/meter.pulse,
          overflow:phase>=meter.expected});
      }
    }
    return grid;
  }
  function beatsInWindow(grid, start, end) {
    // Half-open windows prevent repeated clicks at a lookahead boundary.
    return grid.filter(b => b.time >= start-1e-7 && b.time < end-1e-7);
  }
  function initMetronome(model, hooks) {
    const $=id=>document.getElementById(id), audio=hooks.audio;
    const enabled=$('metro-enabled'), standalone=$('metro-standalone'), readout=$('metro-readout'), indicator=$('metro-indicator');
    let context=null, timer=null, mode=null, grid=[], next=0, lastTime=null, lastRate=null, generation=0;
    let standaloneNext=0, standaloneBeat=0, standaloneMeter=null;
    const nodes=new Set(), uiTimers=new Set();
    function rate() { return hooks.getRate?.() || audio.playbackRate || 1; }
    function globalTime() {return audio.currentTime+(hooks.getPlayback?.().offset || 0);}
    function clearScheduled() {
      generation++;
      for (const node of nodes) {try {node.stop();} catch (_) {}}
      nodes.clear(); for (const id of uiTimers) clearTimeout(id); uiTimers.clear();
      indicator.classList.remove('is-beat','is-accent');
    }
    function stop() {
      if(timer) clearInterval(timer); timer=null;mode=null;clearScheduled();lastTime=null;
      standalone.setAttribute('aria-pressed','false');standalone.textContent='开始独立打拍';
      readout.textContent='随曲谱速度与拍号';
    }
    async function ready() {
      if (!context) {const Constructor=window.AudioContext || window.webkitAudioContext;if(!Constructor)throw new Error('浏览器不支持节拍器音频');context=new Constructor();}
      await context.resume();
    }
    function click(time, beat) {
      const gain=context.createGain(), osc=context.createOscillator(), volume=Number($('metro-volume').value);
      osc.frequency.value=beat.accent ? 1400 : beat.major ? 950 : 650;
      gain.gain.setValueAtTime(Math.max(.0001,volume*(beat.major ? .32 : .17)),time);
      gain.gain.exponentialRampToValueAtTime(.0001,time+.035);
      osc.connect(gain);gain.connect(context.destination);nodes.add(osc);
      osc.onended=()=>{nodes.delete(osc);osc.disconnect();gain.disconnect();};osc.start(time);osc.stop(time+.04);
      const version=generation;
      const id=setTimeout(()=>{
        uiTimers.delete(id); if(version!==generation)return;
        indicator.classList.add('is-beat');indicator.classList.toggle('is-accent',beat.accent);
        readout.textContent=`${beat.meter} · ${beat.index}/${beat.count} 拍 · ${Math.round(beat.bpm*rate())}/分${beat.overflow ? ' · 小节时值待校对' : ''}`;
        const off=setTimeout(()=>{uiTimers.delete(off);if(version===generation)indicator.classList.remove('is-beat','is-accent');},85);uiTimers.add(off);
      },Math.max(0,(time-context.currentTime)*1000));uiTimers.add(id);
    }
    function resetCursor(t) {
      next=grid.findIndex(b=>b.time>=t-.015);if(next<0)next=grid.length;
    }
    function tick() {
      if(mode==='sync') {
        if(audio.paused || !enabled.checked){stop();return;}
        if(audio.seeking)return;
        const t=globalTime(), r=rate();
        if(lastTime!==null && (t<lastTime-.02 || t-lastTime>.25*r || r!==lastRate)){clearScheduled();resetCursor(t);}
        lastTime=t;lastRate=r;
        const playback=hooks.getPlayback?.(), end=playback?.selection ? playback.offset+playback.duration : model.duration;
        while(next<grid.length && grid[next].time<t+.12*r && grid[next].time<end-1e-7) {
          const beat=grid[next++];if(beat.time>=t-.015)click(Math.max(context.currentTime+.002,context.currentTime+(beat.time-t)/r),beat);
        }
      } else if(mode==='standalone') {
        const m=standaloneMeter, subdiv=$('metro-subdivision').value==='eighth', step=subdiv ?.5:m.pulse;
        const quarterBpm=(hooks.getQuarterBpm?.() || model.tempoMetadata?.initialQuarterBpm || 120)*rate();
        while(standaloneNext<context.currentTime+.12) {
          const phase=standaloneBeat*step, major=Math.abs(phase/m.pulse-Math.round(phase/m.pulse))<1e-7;
          click(standaloneNext,{meter:m.text,index:Math.floor(phase/m.pulse)+1,count:m.count,major,accent:standaloneBeat===0,bpm:quarterBpm/rate()/m.pulse});
          standaloneNext+=60*step/quarterBpm;standaloneBeat=(standaloneBeat+1)%Math.round(m.expected/step);
        }
      }
    }
    async function startSync() {
      if(mode==='standalone'||mode==='pending')stop();
      if(!enabled.checked || audio.paused)return;
      stop();const request=generation;
      try {await ready();if(request!==generation || audio.paused || !enabled.checked)return;
        mode='sync';grid=buildBeatGrid(model,$('metro-subdivision').value);resetCursor(globalTime());lastTime=null;tick();timer=setInterval(tick,25);
      }catch(error){hooks.onStatus?.(error.message);}
    }
    async function startStandalone() {
      stop();audio.pause();const request=generation;
      mode='pending';standalone.setAttribute('aria-pressed','true');standalone.textContent='取消独立打拍';
      try {await ready();if(request!==generation)return;
        const measure=model.measures.find(m=>globalTime()>=m.start && globalTime()<m.end) || model.measures[0];
        standaloneMeter=meterInfo(measure);standaloneNext=context.currentTime+.03;standaloneBeat=0;mode='standalone';
        standalone.setAttribute('aria-pressed','true');standalone.textContent='停止独立打拍';tick();timer=setInterval(tick,25);
      }catch(error){if(request===generation)stop();hooks.onStatus?.(error.message);}
    }
    function refresh() {const previous=mode;clearScheduled();if(previous==='standalone')startStandalone();else if(!audio.paused && enabled.checked)startSync();}
    standalone.onclick=()=>mode==='standalone'||mode==='pending'?stop():startStandalone();
    enabled.onchange=()=>enabled.checked?startSync():stop();
    $('metro-subdivision').onchange=refresh;
    $('metro-volume').oninput=()=>{$('metro-volume-value').textContent=`${Math.round(Number($('metro-volume').value)*100)}%`;};
    audio.addEventListener('play',startSync);
    audio.addEventListener('pause',()=>{if(mode==='sync')stop();});
    audio.addEventListener('seeking',()=>{if(mode==='sync')clearScheduled();});
    audio.addEventListener('seeked',()=>{if(!audio.paused && enabled.checked)startSync();});
    audio.addEventListener('ratechange',refresh);
    document.addEventListener('visibilitychange',()=>{if(document.hidden)stop();else if(enabled.checked && !audio.paused)startSync();});
    return {stop,refresh,dispose(){stop();context?.close();},get mode(){return mode;}};
  }
  return {tempoMap,tempoAt,secondsAtBeat,meterInfo,buildBeatGrid,beatsInWindow,initMetronome};
});
