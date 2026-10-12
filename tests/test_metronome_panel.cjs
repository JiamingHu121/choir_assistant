const {test}=require('node:test'),assert=require('node:assert/strict');
const vm=require('node:vm'),fs=require('node:fs');
function setup({offset=0}={}) {
 const elements={},events={},intervals=new Set(),shots=[],timeouts=new Set();
 const element=id=>elements[id] ||= {value:id==='metro-subdivision'?'beat':id==='metro-volume'?'.25':'',checked:false,textContent:'',classList:{add(){},remove(){},toggle(){}},setAttribute(){}};
 const audio={paused:true,currentTime:0,playbackRate:1,seeking:false,addEventListener(name,fn){(events[name] ||= []).push(fn);},pause(){this.paused=true;fire('pause');}};
 const fire=name=>{for(const fn of events[name] || [])fn();};
 let context=null,resolveResume=null,delayResume=false;
 class Context {
  constructor(){this.currentTime=0;this.destination={};context=this;}
  resume(){return delayResume?new Promise(resolve=>resolveResume=resolve):Promise.resolve();}
  createGain(){return {gain:{setValueAtTime(){},exponentialRampToValueAtTime(){}},connect(){},disconnect(){}};}
  createOscillator(){const shot={start:null,cancelled:false};shots.push(shot);return {frequency:{value:0},connect(){},disconnect(){},start(time){shot.start=time;},stop(time){if(time===undefined)shot.cancelled=true;}};}
  close(){return Promise.resolve();}
 }
 const document={hidden:false,getElementById:element,addEventListener(name,fn){(events[name] ||= []).push(fn);}};
 const scope={window:{AudioContext:Context},document,module:{exports:{}},setInterval(fn){intervals.add(fn);return fn;},clearInterval(fn){intervals.delete(fn);},setTimeout(fn){timeouts.add(fn);return fn;},clearTimeout(fn){timeouts.delete(fn);}};
 vm.runInNewContext(fs.readFileSync(require.resolve('../workbench/metronome.js'),'utf8'),scope);
 const model={duration:10,tempos:[{onset:0,bpm:108}],measures:[{number:1,onset:0,duration:6,start:0,end:10/3,timeSignature:{text:'12/8'}},{number:2,onset:6,duration:6,start:10/3,end:20/3,timeSignature:{text:'12/8'}}]};
 const controller=scope.module.exports.initMetronome(model,{audio,getPlayback:()=>({offset,selection:false,duration:10}),getRate:()=>audio.playbackRate});
 return {audio,controller,element,shots,intervals,fire,document,context:()=>context,advance(realTime,scoreTime=realTime*audio.playbackRate){context.currentTime+=realTime;audio.currentTime+=scoreTime;for(const fn of [...intervals])fn();},delay(){delayResume=true;},resolve(){resolveResume();}};
}
const settle=()=>new Promise(setImmediate);
test('sync schedules the canonical beat in audio-clock time and cancels on pause',async()=>{
 const s=setup();s.element('metro-enabled').checked=true;s.audio.paused=false;s.fire('play');await settle();
 assert.equal(s.controller.mode,'sync');assert.equal(s.shots.length,1);
 s.advance(.75);assert.equal(s.shots.length,2);assert.ok(Math.abs(s.shots[1].start-5/6)<1e-9);
 s.audio.pause();assert.equal(s.intervals.size,0);assert.ok(s.shots.every(x=>x.cancelled));
});
test('selection offset and rate are each applied once, and seeks drop scheduled clicks',async()=>{
 const s=setup({offset:.75});s.audio.playbackRate=2;s.element('metro-enabled').checked=true;s.audio.paused=false;s.fire('play');await settle();
 assert.ok(Math.abs(s.shots[0].start-(5/6-.75)/2)<1e-9);
 s.audio.seeking=true;s.fire('seeking');assert.equal(s.shots[0].cancelled,true);
 s.audio.currentTime=2;s.audio.seeking=false;s.fire('seeked');await settle();
 s.advance(.2);const shot=s.shots.at(-1);assert.ok(shot.start>=s.context().currentTime);
 s.audio.playbackRate=.5;s.fire('ratechange');await settle();assert.equal(shot.cancelled,true);
});
test('a cancelled pending standalone resume never starts audio',async()=>{
 const s=setup();s.delay();s.element('metro-standalone').onclick();assert.equal(s.controller.mode,'pending');
 s.element('metro-standalone').onclick();assert.equal(s.controller.mode,null);s.resolve();await settle();
 assert.equal(s.shots.length,0);assert.equal(s.intervals.size,0);
});
test('main playback stops standalone, and foreground restores enabled synchronization',async()=>{
 const s=setup();s.element('metro-standalone').onclick();await settle();assert.equal(s.controller.mode,'standalone');
 s.audio.paused=false;s.fire('play');await settle();assert.equal(s.controller.mode,null);
 s.element('metro-enabled').checked=true;s.fire('play');await settle();assert.equal(s.controller.mode,'sync');
 s.document.hidden=true;s.fire('visibilitychange');assert.equal(s.controller.mode,null);
 s.document.hidden=false;s.fire('visibilitychange');await settle();assert.equal(s.controller.mode,'sync');
});
