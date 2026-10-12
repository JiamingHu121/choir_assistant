'use strict';
// Execute the real pitch panel with bounded browser/audio doubles. The microphone
// receives synthetic PCM only; no device access, chooser, or user voice capture.
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const core = require('../workbench/pitch_core.js');

class EventSource {
  constructor() { this.listeners = new Map(); }
  addEventListener(name, fn) { const list = this.listeners.get(name) || []; list.push(fn); this.listeners.set(name, list); }
  removeEventListener(name, fn) { this.listeners.set(name, (this.listeners.get(name) || []).filter(item => item !== fn)); }
  async emit(name, event = {}) { return Promise.all((this.listeners.get(name) || []).map(fn => fn({target:this, ...event}))); }
}
class Element extends EventSource {
  constructor(tag = 'div', attributes = '') {
    super(); this.tag = tag; this.value = attributes.match(/value="([^"]*)"/)?.[1] || '';
    this.hidden = /\bhidden\b/.test(attributes); this.disabled = /\bdisabled\b/.test(attributes);
    this.textContent = ''; this.style = {}; this.files = []; this.currentTime = 0; this.classes = new Set();
    this.classList = {toggle: (name, on) => on ? this.classes.add(name) : this.classes.delete(name)};
  }
  set innerHTML(value) {
    this.html = value;this.onInnerHTML?.(value);
    if (this.tag === 'select') {
      this.options = [...value.matchAll(/<option[^>]*value="([^"]*)"/g)].map(match => match[1]);
      this.value = this.options[0] || '';
    }
  }
  get innerHTML() { return this.html || ''; }
  pause() { this.paused = true; }
  removeAttribute(name) { delete this[name]; }
  getBoundingClientRect() { return {width:270,height:160}; }
  getContext() { return new Proxy({}, {get: (object, key) => object[key] || (()=>{}), set: (object,key,value) => (object[key] = value, true)}); }
}
const deferred = () => { let resolve, reject; const promise = new Promise((yes,no)=>{resolve=yes;reject=no;}); return {promise,resolve,reject}; };
const flush = async () => { for(let i=0;i<5;i++) await Promise.resolve(); };
const notes = [
  {id:'a4',part:'S',kind:'note',pitch:69,pitchName:'A4',start:10,end:11},
  {id:'b4',part:'S',kind:'note',pitch:71,pitchName:'B4',start:11,end:12},
];

function environment({resumePending=false, micPending=false, decodePending=false, practiceRate=1,modelNotes=notes,modelInput=null,rhythmPolicy='strict',selectedIds=notes.map(n=>n.id),echoCancellation=true,assetPending=false,assetStart=9} = {}) {
  const state = {elements:new Map(),audioContexts:[],tracks:[],sources:[],analysers:[],workers:[],requests:[],downloads:[],rafs:new Map(),nextRaf:0,practiceRate,rhythmPolicy,frequency:440,playbacks:[],network:[],backgroundGain:0.6,voiceAfter:Infinity,voiceAmplitude:0.12,echoCancellation,practiceTimes:[]};
  const host = new Element();
  Object.defineProperty(host,'innerHTML',{set(html){
    for(const match of html.matchAll(/<([\w-]+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
      const element = new Element(match[1],match[2]); state.elements.set(match[3],element);
      element.onInnerHTML=value=>{for(const nested of value.matchAll(/<([\w-]+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)){const child=new Element(nested[1],nested[2]);child.checked=/\bchecked\b/.test(nested[2]);state.elements.set(nested[3],child);}};
    }
    // Select defaults need their nested options, matching browser creation.
    for(const match of html.matchAll(/<select\b[^>]*id="([^"]+)"[^>]*>([\s\S]*?)<\/select>/g)) state.elements.get(match[1]).innerHTML = match[2];
  }});
  host.querySelector = selector => state.elements.get(selector.slice(1));
  const document = new EventSource(); document.hidden=false;
  document.getElementById = id => id === 'pitch-host' ? host : state.elements.get(id);
  document.createElement = () => ({click(){state.downloads.push({url:this.href,name:this.download});}});
  const resume = deferred(), microphone = deferred(), decoding = deferred(),asset=deferred();
  const backgroundSignal=seconds=>0.3*Math.sin(2*Math.PI*220*seconds)+0.15*Math.sin(2*Math.PI*329.627*seconds+0.1)+0.1*Math.sin(2*Math.PI*392*seconds+0.6);
  const backgroundBuffer=()=>({sampleRate:48000,numberOfChannels:1,duration:5,length:240000,getChannelData:()=>Float32Array.from({length:240000},(_,i)=>backgroundSignal(i/48000))});
  class AudioContext {
    constructor(){this.currentTime=0;this.sampleRate=48000;this.closed=false;state.audioContexts.push(this);}
    resume(){return resumePending?resume.promise:Promise.resolve();}
    close(){this.closed=true;return Promise.resolve();}
    createMediaStreamSource(){const source={connect(node){this.connected=node;},disconnect(){this.disconnected=true;}};state.sources.push(source);return source;}
    createAnalyser(){const context=this;const analyser={fftSize:2048,disconnect(){this.disconnected=true;},getFloatTimeDomainData(samples){for(let i=0;i<samples.length;i++){
      if(!state.network.length){samples[i]=0.3*Math.sin(2*Math.PI*state.frequency*i/48000);continue;}
      const now=context.currentTime-(samples.length-i)/48000,delay=1837/48000;
      const playback=state.playbacks.find(p=>now-delay>=p.when && now-delay<p.when+p.duration && (!p.stoppedAt || now-delay<p.stoppedAt));
      const bg=playback?backgroundSignal(playback.offset+now-delay-playback.when)*state.backgroundGain*playback.gain.gain.value:0;
      samples[i]=bg+(now>=state.voiceAfter?state.voiceAmplitude*Math.sin(2*Math.PI*state.frequency*now+0.3):0);
    }}};state.analysers.push(analyser);return analyser;}
    createBufferSource(){const context=this,node={connect(gain){this.gain=gain;},start(when,offset,duration){Object.assign(this,{when,offset,duration});state.playbacks.push(this);},stop(){this.stoppedAt=context.currentTime;this.stopped=true;},disconnect(){this.disconnected=true;}};return node;}
    createGain(){return {gain:{value:1},connect(){},disconnect(){this.disconnected=true;}};}
    decodeAudioData(){return decodePending ? decoding.promise : Promise.resolve(state.decodedBuffer);}
  }
  function makeStream(){
    const track=new EventSource(); track.stopped=0;track.stop=()=>{track.stopped++;};track.getSettings=()=>({echoCancellation:state.echoCancellation,noiseSuppression:true,autoGainControl:false});state.tracks.push(track);
    return {getTracks:()=>[track]};
  }
  class Worker {
    constructor(url){this.url=url;this.terminated=false;state.workers.push(this);}
    terminate(){this.terminated=true;}
    postMessage(payload){this.payload=payload;}
    respond(result){this.onmessage?.({data:{id:this.payload.id,result}});}
  }
  const mainAudio = new Element('audio');
  const window = new EventSource(); Object.assign(window,{PitchCore:core,AudioContext,devicePixelRatio:1});
  const sandbox={window,document,navigator:{mediaDevices:{getUserMedia(options){state.requests.push(options);return micPending?microphone.promise:Promise.resolve(makeStream());}}},
    requestAnimationFrame(fn){const id=++state.nextRaf;state.rafs.set(id,fn);return id;},cancelAnimationFrame(id){state.rafs.delete(id);},
    ResizeObserver:class {observe(){}disconnect(){}},Worker,Float32Array,Blob,URL:{createObjectURL(blob){state.reportBlob=blob;return 'blob:test';},revokeObjectURL(){}},setTimeout(){},
    fetch(url,options){
      state.network.push({url,options});
      if(url==='/api/selection'){if(assetPending)return asset.promise;return Promise.resolve({ok:true,json:async()=>({wav:'/fixture-background.wav',start:assetStart,duration:5,revision:'fixture-revision',practiceRate:state.practiceRate,audioValidation:{policy:state.rhythmPolicy,status:state.rhythmPolicy==='draft'?'draft':'rhythm-validated'}})});}
      if(url==='/fixture-background.wav'){state.decodedBuffer=backgroundBuffer();return Promise.resolve({ok:true,arrayBuffer:async()=>new ArrayBuffer(4)});}
      throw Error('Unexpected network request '+url);
    },console};
  vm.runInNewContext(fs.readFileSync(require.resolve('../workbench/pitch.js'),'utf8'),sandbox,{filename:'pitch.js'});
  const panel=window.initPitchPanel(modelInput || {revision:'fixture-revision',notes:modelNotes},{audio:mainAudio,getSettings:()=>({transpose:0,program:52,gains:{S:1},practiceRate:state.practiceRate,rhythmPolicy:state.rhythmPolicy}),onPracticeTime:(time,details)=>state.practiceTimes.push({time,details})});
  panel.setSelection(selectedIds);
  state.element=name=>state.elements.get(`pitch-${name}`);
  state.click=name=>state.element(name).emit('click');
  state.change=async(name,value)=>{state.element(name).value=String(value);await state.element(name).emit('change');};
  state.tick=async time=>{
    state.audioContexts[0].currentTime=time;
    const queued=[...state.rafs.values()];state.rafs.clear();
    queued.forEach(fn=>fn());await flush();
  };
  state.report=async()=>{await state.click('report');return JSON.parse(await state.reportBlob.text());};
  state.buffer=(frequency=440,duration=1)=>({sampleRate:48000,numberOfChannels:1,duration,length:Math.round(48000*duration),getChannelData:()=>Float32Array.from({length:Math.round(48000*duration)},(_,i)=>0.3*Math.sin(2*Math.PI*frequency*i/48000))});
  state.import=async()=>{
    state.decodedBuffer=state.buffer();state.element('file').files=[{name:'synthetic-voice.wav',size:96000,arrayBuffer:()=>Promise.resolve(new ArrayBuffer(4))}];
    await state.element('file').emit('change');
  };
  return {state,panel,document,window,mainAudio,resume,microphone,decoding,makeStream,asset};
}

test('live synthetic microphone count-in and analyzer window center align with target timeline',async()=>{
  const {state}=environment({practiceRate:2});
  await state.change('mode','follow');await state.change('alignment',0.1);
  await state.click('mic');
  assert.equal(state.requests.length,1);
  assert.equal(state.requests[0].video,false);
  assert.equal(state.requests[0].audio.echoCancellation,false);
  await state.tick(1.9);
  assert.equal(state.element('report').disabled,true);
  assert.match(state.element('status').textContent,/准备/);
  await state.tick(2.4);
  assert.equal(state.element('report').disabled,false);
  assert.match(state.element('frequency').textContent,/440\.0 Hz/);
  assert.match(state.element('cents').textContent,/\+0 音分/);
  await state.click('stop');
  const report=await state.report();
  assert.equal(report.source,'microphone');
  assert.equal(report.revision,'fixture-revision');
  assert.deepEqual(report.selectedNoteIds,['a4','b4']);
  const frame=report.frames[0], center=2.4-2-4096/(2*48000);
  assert.ok(Math.abs(frame.time-center)<1e-9);
  assert.ok(Math.abs(frame.referenceTime-(center-0.1)*2)<1e-9);
  assert.equal(frame.targetId,'a4');
  assert.equal(report.algorithm.sampleRate,48000);
  assert.equal(state.tracks[0].stopped,1);
  assert.equal(state.sources[0].disconnected,true);
  assert.equal(state.analysers[0].disconnected,true);
  assert.equal(state.rafs.size,0);
});

test('changing target, visibility loss and main playback all release live microphone tracks',async()=>{
  for(const cause of ['target','hidden','play']){
    const {state,document,mainAudio}=environment();await state.click('mic');await state.tick(2.4);
    if(cause==='target')await state.change('target','b4');
    if(cause==='hidden'){document.hidden=true;await document.emit('visibilitychange');}
    if(cause==='play')await mainAudio.emit('play');
    assert.equal(state.tracks[0].stopped,1,cause);
    assert.equal(state.rafs.size,0,cause);
    assert.equal(state.element('stop').disabled,true,cause);
    assert.equal(state.element('file').disabled,false,cause);
  }
});

test('pending permission cancellation closes subsequently granted stream without starting RAF',async()=>{
  const {state,microphone,makeStream}=environment({micPending:true});
  const starting=state.click('mic');await flush();
  assert.equal(state.requests.length,1);await state.click('stop');
  microphone.resolve(makeStream());await starting;
  assert.equal(state.tracks[0].stopped,1);
  assert.equal(state.rafs.size,0);
  assert.equal(state.element('stop').disabled,true);
});

test('cancel during suspended context resume prevents a later permission request',async()=>{
  const {state,resume}=environment({resumePending:true});
  const starting=state.click('mic');await flush();await state.click('stop');
  resume.resolve();await starting;
  assert.equal(state.requests.length,0,'Stopped session must not ask for microphone after context resumes');
});

test('follow practice stops and exports last frame when selected melody is complete',async()=>{
  const {state}=environment();await state.change('mode','follow');await state.click('mic');
  await state.tick(2.3);await state.tick(4.3);
  assert.equal(state.tracks[0].stopped,1);assert.equal(state.rafs.size,0);
  assert.match(state.element('status').textContent,/旋律已结束/);
  const report=await state.report();assert.equal(report.frames.length,1);assert.equal(report.targets.duration,2);
});

test('stale offline worker completion cannot replace results after selection changes',async()=>{
  const {state,panel}=environment();await state.import();
  const previous=state.workers[0];assert.equal(previous.url,'/pitch-worker.js');
  panel.setSelection(['b4']);assert.equal(previous.terminated,true);
  previous.respond({frames:[{time:0.4,cents:999,frequency:440}],summary:{matchedFrames:1},duration:1});
  assert.equal(state.element('summary').hidden,true);
  assert.equal(state.element('report').disabled,true);
  assert.equal(state.element('frequency').textContent,'— Hz');
});

test('offline report carries source filename, target frequency and session settings',async()=>{
  const {state}=environment();await state.change('a4',442);await state.import();
  const worker=state.workers[0],payload=worker.payload;
  const result=core.analyzeRecording(new Float32Array(payload.samples),payload.sampleRate,payload.options);
  worker.respond(result);
  const report=await state.report();
  assert.equal(report.source,'recording');assert.equal(report.inputName,'synthetic-voice.wav');
  assert.equal(report.parameters.a4,442);assert.equal(report.targets.targets[0].frequency,442);
  assert.equal(report.algorithm.sampleRate,48000);assert.equal(report.algorithm.name,'YIN');
  assert.ok(report.frames[0].cents < -7);
  assert.equal(report.parameters.mode,'fixed');
});

test('queued end event from previous device session cannot stop a restarted session',async()=>{
  const {state}=environment();await state.click('mic');
  const previousTrack=state.tracks[0];await state.click('stop');await state.click('mic');
  const currentTrack=state.tracks[1];
  // A previously queued device-ended callback may dispatch after a new session.
  await previousTrack.emit('ended');
  assert.equal(currentTrack.stopped,0);
  assert.equal(state.element('stop').disabled,false);
  await state.click('stop');assert.equal(currentTrack.stopped,1);
});

test('disposing panel invalidates already queued worker completion callbacks',async()=>{
  const {state,panel}=environment();await state.import();
  const previous=state.workers[0];panel.dispose();
  previous.respond({frames:[{time:0.4,cents:999,frequency:440}],summary:{matchedFrames:1},duration:1});
  assert.equal(previous.terminated,true);
  assert.equal(state.element('summary').hidden,true);
  assert.equal(state.element('report').disabled,true);
});

test('current device ended event stops the active session and clears audio graph',async()=>{
  const {state}=environment();await state.click('mic');await state.tick(2.3);
  await state.tracks[0].emit('ended');
  assert.equal(state.tracks[0].stopped,1);assert.equal(state.rafs.size,0);
  assert.equal(state.sources[0].disconnected,true);assert.equal(state.analysers[0].disconnected,true);
  assert.match(state.element('status').textContent,/连接已结束/);
});

test('microphone start invalidates an older recording decode completion',async()=>{
  const {state,decoding}=environment({decodePending:true});
  const importing=state.import();await flush();await state.click('mic');
  decoding.resolve(state.buffer());await importing;
  assert.equal(state.workers.length,0);
  assert.equal(state.element('analyze').disabled,true);
  assert.equal(state.element('file').disabled,true);
  assert.equal(state.tracks[0].stopped,0);
  await state.click('stop');
});

test('follow mode rejects overlapping target pitches while fixed mode allows one chosen target',async()=>{
  const {state,panel}=environment();
  panel.setSelection(['a4','chord'],{revision:'polyphonic',notes:[notes[0],{id:'chord',kind:'note',part:'S',pitch:72,start:10,end:11}]});
  await state.change('mode','follow');
  assert.equal(state.element('mic').disabled,true);
  await state.click('mic');assert.equal(state.requests.length,0);
  await state.change('mode','fixed');assert.equal(state.element('mic').disabled,false);
  await state.change('target','chord');await state.click('mic');await state.tick(2.4);await state.click('stop');
  const report=await state.report();assert.equal(report.frames[0].targetId,'chord');
  assert.ok(report.frames[0].cents < -290 && report.frames[0].cents > -310);
});

const interferenceNotes=[...notes,{id:'bg-held',kind:'note',part:'A',pitch:57,start:9,end:13},{id:'bg-other',kind:'note',part:'A',pitch:64,start:10.5,end:11.2},{id:'bg-after',kind:'note',part:'A',pitch:65,start:13,end:14}];
function canonicalPanelModel(modelNotes=notes, blockingMeasureIndices=[2]) {
  const canonicalNotes=modelNotes.map(note=>({...note,performanceId:`p-${note.id}`}));
  return {schemaVersion:2,revision:'fixture-revision',notes:canonicalNotes,
    performanceEvents:canonicalNotes.map(note=>({id:note.performanceId,noteIds:[note.id],part:note.part,pitch:note.pitch,pitchName:note.pitchName,start:note.start,end:note.end})),
    measures:[{index:1,number:1,start:10,end:11},{index:2,number:2,start:11,end:12}],
    rhythmValidation:{status:blockingMeasureIndices.length?'needs-review':'validated',blockingMeasureIndices}};
}
test('schema two strict pitch gate disables synthesis and assessment only in unresolved target region',async()=>{
  const {state,panel}=environment({modelInput:canonicalPanelModel()});
  for(const id of ['mic','reference','file','analyze'])assert.equal(state.element(id).disabled,true,id);
  assert.match(state.element('status').textContent,/目标小节（2）待校对/);
  await state.click('mic');await state.click('reference');await state.import();
  assert.equal(state.requests.length,0);assert.equal(state.network.length,0);assert.equal(state.workers.length,0);
  panel.setSelection(['a4']);
  assert.equal(state.element('mic').disabled,false,'Unresolved bar two does not block bar one');
  assert.equal(state.element('reference').disabled,false);assert.equal(state.element('file').disabled,false);
  await state.click('mic');assert.equal(state.requests.length,1);
  await state.click('stop');panel.dispose();
});
test('schema two targets without rhythm validation remain blocked even with draft switch',async()=>{
  const model=canonicalPanelModel();delete model.rhythmValidation;
  const {state}=environment({modelInput:model,rhythmPolicy:'draft'});
  assert.equal(state.element('mic').disabled,true);assert.equal(state.element('file').disabled,true);
  assert.match(state.element('status').textContent,/缺少节奏校验/);
  await state.click('mic');await state.click('reference');assert.equal(state.network.length,0);assert.equal(state.requests.length,0);
});
test('draft pitch references carry policy to synthesis and mark recorded analysis reports',async()=>{
  const {state,panel}=environment({modelInput:canonicalPanelModel()});
  state.rhythmPolicy='draft';panel.updateSettings();
  assert.equal(state.element('mic').disabled,false);assert.match(state.element('targets-summary').textContent,/草稿参考/);
  await state.click('reference');
  assert.equal(JSON.parse(state.network.find(request=>request.url==='/api/selection').options.body).rhythmPolicy,'draft');
  await state.import();
  state.workers[0].respond({frames:[{time:.4,referenceTime:.4,cents:0,frequency:440,targetFrequency:440,targetId:'a4'}],summary:{matchedFrames:1},duration:1});
  const report=await state.report();
  assert.equal(report.referenceStatus,'draft');assert.equal(report.referenceValidation.allowed,true);
  assert.equal(report.audioValidation.status,'draft');assert.equal(report.parameters.rhythmPolicy,'draft');
});
test('draft anti-interference background forwards policy and uses canonical events for region',async()=>{
  const {state}=environment({modelInput:canonicalPanelModel(interferenceNotes),rhythmPolicy:'draft'});
  await state.change('mode','interference');await state.click('mic');
  const payload=JSON.parse(state.network.find(request=>request.url==='/api/selection').options.body);
  assert.equal(payload.rhythmPolicy,'draft');assert.deepEqual(payload.noteIds,['bg-held','bg-other']);
  assert.equal(state.requests.length,1);await state.click('stop');
});
async function finishCalibration(state){for(const time of [0.5,0.65,0.8,0.95,1.1,1.25,1.4,1.55,1.7,1.85,2.1])await state.tick(time);}
test('anti-interference session selects full background region, excludes singer and rejects background-only frames',async()=>{
  const {state,panel}=environment({modelNotes:interferenceNotes});
  await state.change('mode','interference');
  assert.equal(state.element('background-panel').hidden,false);
  assert.equal(state.element('bg-A').checked,true);
  assert.doesNotMatch(state.element('background-parts').innerHTML,/id="pitch-bg-S"/);
  await state.click('mic');
  assert.equal(state.requests[0].audio.echoCancellation,true);
  const request=JSON.parse(state.network[0].options.body);
  assert.deepEqual(request.noteIds,['bg-held','bg-other']);
  assert.equal(request.gains.S,0);
  assert.equal(request.practiceRate,1);
  await finishCalibration(state);
  assert.equal(state.playbacks.length,2);
  assert.equal(panel.interferenceState().calibration.valid,true);
  assert.equal(state.element('report').disabled,true,'Calibration and count-in are not scored');
  await state.tick(4.4);
  assert.equal(state.element('cents').textContent,'— 音分');
  assert.match(state.element('confidence').textContent,/仅检测到背景音/);
  await state.click('stop');const report=await state.report();
  assert.equal(report.interference.aec.requested,true);
  assert.equal(report.interference.aec.applied,true);
  assert.deepEqual(report.interference.backgroundParts,['A']);
  assert.equal(report.frames[0].frequency,null);
  assert.equal(report.frames[0].signal.reason,'reference-only');
  assert.equal(report.summary.matchedFrames,0);
  assert.equal(state.playbacks.every(p=>p.disconnected),true);
  assert.equal(state.tracks[0].stopped,1);
});
test('anti-interference residual keeps detuned singer and aligns target with accompaniment start',async()=>{
  const {state}=environment({modelNotes:interferenceNotes});await state.change('mode','interference');await state.click('mic');
  await finishCalibration(state);state.voiceAfter=4.1;state.frequency=440*Math.pow(2,50/1200);
  await state.tick(4.4);await state.click('stop');const report=await state.report(),frame=report.frames[0];
  assert.ok(Math.abs(frame.cents-50)<2,JSON.stringify(frame));
  assert.equal(frame.targetId,'a4');
  assert.ok(Math.abs(frame.referenceTime-(4.4-4.1-4096/(2*48000)))<1e-9);
  assert.equal(frame.signal.calibrated,true);
  assert.equal(report.summary.matchedFrames,1);
});
test('background playback preserves reference pitch at practice rate and target clock follows score rate',async()=>{
  const {state}=environment({modelNotes:interferenceNotes,practiceRate:2});await state.change('mode','interference');await state.click('mic');await finishCalibration(state);
  state.voiceAfter=4.1;state.frequency=440*Math.pow(2,50/1200);await state.tick(4.4);await state.click('stop');const report=await state.report();
  assert.equal(JSON.parse(state.network[0].options.body).practiceRate,2);
  assert.equal(state.playbacks[1].duration,1);
  assert.equal(state.playbacks[1].offset,0.5);
  assert.equal(state.playbacks[1].playbackRate,undefined,'PCM is rendered at score tempo; AudioBufferSource never changes pitch');
  assert.ok(Math.abs(report.frames[0].referenceTime-(4.4-4.1-4096/(2*48000))*2)<1e-9);
});
test('cancelling background render invalidates late response before requesting microphone',async()=>{
  const {state,asset}=environment({modelNotes:interferenceNotes,assetPending:true});await state.change('mode','interference');
  const starting=state.click('mic');await flush();await state.click('stop');
  asset.resolve({ok:true,json:async()=>({wav:'/fixture-background.wav',start:9,duration:5,practiceRate:1})});await starting;
  assert.equal(state.requests.length,0);
  assert.equal(state.playbacks.length,0);
  assert.equal(state.rafs.size,0);
});
test('changing background configuration releases mic and scheduled accompaniment together',async()=>{
  const {state}=environment({modelNotes:interferenceNotes});await state.change('mode','interference');await state.click('mic');
  await state.change('background-volume',0.2);
  assert.equal(state.tracks[0].stopped,1);
  assert.equal(state.playbacks[0].disconnected,true);
  assert.equal(state.rafs.size,0);
  assert.equal(state.element('stop').disabled,true);
});

test('singer alignment changes only target clock; background collision guard and practice position keep playback clock',async()=>{
  const modelNotes=[...notes,{id:'early-bg',kind:'note',part:'A',pitch:57,start:9,end:10.8},{id:'late-bg',kind:'note',part:'A',pitch:67,start:10.8,end:13}];
  const {state}=environment({modelNotes});await state.change('mode','interference');await state.change('alignment',1);await state.click('mic');await finishCalibration(state);
  state.voiceAfter=4.1;state.frequency=440;await state.tick(5.4);
  assert.match(state.element('cents').textContent,/\+0 音分/,'Current background is G4, while shifted target time still has A3 background; using singer clock would wrongly reject the A4 singer');
  const backgroundTime=10+5.4-4.1-4096/(2*48000);
  assert.ok(Math.abs(state.practiceTimes.at(-1).time-backgroundTime)<1e-9);
  assert.deepEqual([...state.practiceTimes.at(-1).details.noteIds],['early-bg','late-bg']);
  await state.click('stop');const report=await state.report();
  assert.equal(report.frames[0].targetId,'a4');
  assert.ok(Math.abs(report.frames[0].referenceTime-(backgroundTime-10-1))<1e-9);
  assert.equal(state.practiceTimes.at(-1).time,null);
  const count=state.practiceTimes.length;await state.click('stop');assert.equal(state.practiceTimes.length,count,'A stopped session clears the practice cursor only once');
});
test('a background starting after target region preserves leading rest at normal and changed practice rates',async()=>{
  for(const practiceRate of [1,2]){
    const modelNotes=[...notes,{id:'late-bg',kind:'note',part:'A',pitch:57,start:10.5,end:13}];
    const {state}=environment({modelNotes,practiceRate,assetStart:10.5});await state.change('mode','interference');await state.click('mic');await finishCalibration(state);
    const playback=state.playbacks[1];
    assert.ok(Math.abs(playback.when-(4.1+0.5/practiceRate))<1e-9);
    assert.equal(playback.offset,0);
    assert.equal(playback.duration,1.5/practiceRate);
    assert.ok(state.playbacks[0].when<0.11,'Calibration may start immediately, independently of singing leading rest');
    state.voiceAfter=4.1;state.frequency=440*Math.pow(2,50/1200);await state.tick(4.25);
    assert.match(state.element('cents').textContent,/\+50 音分/,'Target starts before background and is scored against silence without an early accompaniment');
    await state.click('stop');assert.equal(state.playbacks.every(p=>p.disconnected),true);
  }
});
