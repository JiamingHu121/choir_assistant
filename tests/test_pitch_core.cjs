'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');
const core = require('../workbench/pitch_core.js');

function tone(frequency, sampleRate = 48000, seconds = 0.12, harmonics = [0.5]) {
  return Float32Array.from({length: Math.round(sampleRate * seconds)}, (_, index) =>
    harmonics.reduce((sum, amplitude, harmonic) => sum + amplitude * Math.sin(2 * Math.PI * frequency * (harmonic + 1) * index / sampleRate + harmonic * 0.11), 0));
}
const near = (value, expected, tolerance = 2) => assert.ok(Math.abs(core.centsDifference(value, expected)) < tolerance, `${value} vs ${expected}`);

function canonical(notes, groups) {
  const performanceEvents=groups.map((noteIds,index)=>{
    const members=noteIds.map(id=>notes.find(note=>note.id===id)), id=`p-${index}`;
    for(const note of members)note.performanceId=id;
    return {id,noteIds,part:members[0].part,pitch:members[0].pitch,pitchName:members[0].pitchName,
      start:Math.min(...members.map(note=>note.start)),end:Math.max(...members.map(note=>note.end))};
  });
  return {schemaVersion:2,notes,performanceEvents,duration:Math.max(...notes.map(note=>note.end))};
}

test('frequency conversion and signed cents preserve octave errors', () => {
  assert.equal(core.midiToHz(69), 440);
  assert.equal(core.midiToHz(69, 442), 442);
  assert.equal(core.centsDifference(880, 440), 1200);
  assert.equal(core.centsDifference(220, 440), -1200);
  assert.equal(core.describePitch(440).name, 'A4');
});
test('YIN estimates known tones at both browser sample rates and low bass', () => {
  for (const rate of [44100, 48000]) for (const frequency of [55, 65.406, 110, 261.626, 440, 1046.502, 1200]) {
    const result = core.detectPitch(tone(frequency, rate), rate);
    assert.equal(result.voiced, true, `${frequency}Hz at ${rate}: ${result.reason}`);
    near(result.frequency, frequency, 4);
    assert.ok(result.confidence > 0.9);
  }
});
test('strong second harmonic and missing fundamental retain physical periodicity', () => {
  for (const frequency of [110, 220, 440, 734.349, 1004.319]) for (const harmonics of [[0.08, 0.6, 0.04], [0, 0.55, 0.4, 0.1]]) {
    const result = core.detectPitch(tone(frequency, 48000, 0.12, harmonics), 48000);
    near(result.frequency, frequency, 3);
  }
});
test('moderate amplitude modulation, vibrato and deterministic noise stay voiced', () => {
  const rate = 48000;
  let seed = 12, phase = 0;
  const samples = Float32Array.from({length: 4096}, (_, index) => {
    seed = (1664525 * seed + 1013904223) >>> 0;
    const frequency = 440 * Math.pow(2, 15 * Math.sin(2 * Math.PI * 5 * index / rate) / 1200);
    phase += 2 * Math.PI * frequency / rate;
    return (0.3 + 0.15 * Math.sin(2 * Math.PI * 3 * index / rate)) * Math.sin(phase) + (seed / 2 ** 32 - 0.5) * 0.04;
  });
  const result = core.detectPitch(samples, rate);
  assert.equal(result.voiced, true);
  near(result.frequency, 440, 20);
});
test('silence, white noise, DC and above range do not become target pitches', () => {
  assert.equal(core.detectPitch(new Float32Array(4096), 48000).frequency, null);
  assert.equal(core.detectPitch(new Float32Array(4096).fill(0.2), 48000).reason, 'silence');
  let seed = 57821;
  const noise = Float32Array.from({length: 4096}, () => {seed = (1664525 * seed + 1013904223) >>> 0; return (seed / 2 ** 32 - 0.5) * 0.4;});
  assert.equal(core.detectPitch(noise, 48000).frequency, null);
  assert.equal(core.detectPitch(tone(1800), 48000).frequency, null);
});
test('Hann FFT reports the spectrum peak rather than pretending it is the fundamental', () => {
  const {frequencies, db} = core.fftSpectrum(tone(220, 48000, 0.1, [0.1, 0.6]), 48000, {fftSize: 4096});
  let peak = 1;
  for (let i = 2; i < db.length; i++) if (db[i] > db[peak]) peak = i;
  assert.ok(Math.abs(frequencies[peak] - 440) < 48000 / 4096);
  const bins = core.fftSpectrum(tone(468.75, 48000, 4096 / 48000, [0.5]), 48000).db;
  assert.ok(Math.abs(bins[40] - 20 * Math.log10(0.5)) < 0.02);
});
test('targets retain leading rests, explicit ties, transpose and polyphony conflicts', () => {
  const notes = [
    {id:'s1',part:'S',pitch:69,pitchName:'A4',start:10,end:11,performanceId:'p1'},
    {id:'s2',part:'S',pitch:69,pitchName:'A4',start:11,end:12,performanceId:'p1'},
    {id:'a1',part:'A',pitch:64,pitchName:'E4',start:10.5,end:12},
    {id:'s3',part:'S',pitch:71,pitchName:'B4',start:13,end:14},
  ];
  const s = core.buildTargets(notes, {part:'S',transpose:12});
  assert.equal(s.targets.length, 2);
  assert.equal(s.targets[0].frequency, 880);
  assert.equal(s.targets[0].pitchName, 'A5');
  assert.equal(s.targets[0].sourcePitchName, 'A4');
  assert.deepEqual(s.targets[0].noteIds, ['s1','s2']);
  assert.equal(core.targetAtTime(s, 2.4), null);
  assert.equal(core.targetAtTime(s, 3.5).id, 's3');
  const a = core.buildTargets(notes, {part:'A'});
  assert.equal(a.origin, 10);
  assert.equal(a.targets[0].start, 0.5);
  const both = core.buildTargets(notes);
  assert.equal(both.monophonic, false);
  assert.equal(core.targetAtTime(both, 0.75), null);
});
test('canonical event membership determines targets across source parts and continuation clips',()=>{
  const model=canonical([
    {id:'a',kind:'note',part:'S',sourcePart:'P5',pitch:69,pitchName:'A4',start:10,end:11},
    {id:'b',kind:'note',part:'S',sourcePart:'P1',pitch:69,pitchName:'A4',start:11,end:12},
    {id:'repeat',kind:'note',part:'S',pitch:69,pitchName:'A4',start:12,end:13,tie:'stop',slurs:[{type:'stop'}]},
  ],[['a','b'],['repeat']]);
  const all=core.buildTargets(model,{part:'S',a4:442});
  assert.equal(all.eventSource,'performanceEvents');assert.equal(all.targets.length,2);
  assert.deepEqual(all.targets[0].noteIds,['a','b']);assert.equal(all.targets[0].frequency,442);
  assert.deepEqual(all.targets.map(target=>[target.start,target.end]),[[0,2],[2,3]]);
  const continuation=core.buildTargets(model,{noteIds:['b'],part:'S',transpose:12,a4:442});
  assert.equal(continuation.origin,11);assert.equal(continuation.duration,1);
  assert.deepEqual(continuation.targets[0].noteIds,['b']);assert.equal(continuation.targets[0].performanceId,'p-0');
  assert.equal(continuation.targets[0].selectionClipped,true);assert.equal(continuation.targets[0].frequency,884);
  assert.equal(core.targetAtTime(continuation,.5).id,'b');assert.equal(core.targetAtTime(continuation,1.1),null);
});
test('canonical nonadjacent tie selection leaves unselected middle segment silent',()=>{
  const model=canonical(['a','b','c'].map((id,index)=>({id,kind:'note',part:'S',pitch:60,start:index,end:index+1})),[['a','b','c']]);
  const selected=core.buildTargets(model,{noteIds:['a','c']});
  assert.equal(selected.targets.length,2);assert.deepEqual(selected.targets.map(target=>target.noteIds),[['a'],['c']]);
  assert.equal(core.targetAtTime(selected,1.5),null);assert.equal(core.targetAtTime(selected,2.5).id,'c');
});
test('schema two missing or inconsistent performance events never silently recalculate ties',()=>{
  const notes=[{id:'a',kind:'note',part:'S',pitch:60,start:0,end:1}];
  assert.throws(()=>core.buildTargets({schemaVersion:2,notes}),/缺少演奏事件/);
  const model=canonical(notes,[['a']]);model.performanceEvents[0].noteIds=['missing'];
  assert.throws(()=>core.buildTargets(model),/成员失效/);
  const mismatch=canonical(notes,[['a']]);mismatch.notes[0].performanceId='other';
  assert.throws(()=>core.buildTargets(mismatch),/不一致/);
  assert.throws(()=>core.buildTargets({schemaVersion:2,notes,performanceEvents:[]}),/缺少演奏事件/);
});
test('canonical background holds whole region while singer targets clip to selected members',()=>{
  const model=canonical([
    {id:'target-a',kind:'note',part:'S',pitch:69,start:10.5,end:11.5},
    {id:'target-b',kind:'note',part:'S',pitch:69,start:11.5,end:12.5},
    {id:'selected-leading',kind:'note',part:'T',pitch:55,start:10,end:11},
    {id:'bg-a',kind:'note',part:'A',pitch:60,start:9,end:11},
    {id:'bg-b',kind:'note',part:'A',pitch:60,start:11,end:13},
  ],[['target-a','target-b'],['selected-leading'],['bg-a','bg-b']]);
  const region=core.practiceRegion(model,['target-b','selected-leading'],{part:'S',backgroundParts:['A','S'],a4:442});
  assert.equal(region.origin,10);assert.equal(region.duration,2.5);
  assert.deepEqual(region.timeline.targets.map(target=>[target.start,target.end]),[[1.5,2.5]]);
  assert.deepEqual(region.backgroundIds,['bg-a','bg-b']);assert.equal(region.background.length,1);
  assert.deepEqual([region.background[0].start,region.background[0].end],[10,12.5]);
  assert.deepEqual(region.background[0].noteIds,['bg-a','bg-b']);assert.equal(region.timeline.targets[0].frequency,442);
});
test('rhythm target gate scopes the whole selected interval and marks draft explicitly',()=>{
  const model={schemaVersion:2,notes:[{id:'a',start:0,end:1},{id:'c',start:2,end:3}],
    measures:[{index:1,number:'1',start:0,end:1},{index:2,number:'2',start:1,end:2},{index:3,number:'3',start:2,end:3}],
    rhythmValidation:{status:'needs-review',blockingMeasureIndices:[2]}};
  assert.equal(core.referenceValidation(model,['a']).allowed,true);
  const blocked=core.referenceValidation(model,['a','c']);
  assert.equal(blocked.allowed,false);assert.deepEqual(blocked.blockingMeasures,['2']);
  assert.match(blocked.message,/目标小节.*待校对/);
  const draft=core.referenceValidation(model,['a','c'],{rhythmPolicy:'draft'});
  assert.equal(draft.allowed,true);assert.equal(draft.referenceStatus,'draft');
  assert.equal(core.referenceValidation({schemaVersion:2,notes:[]},[]).allowed,false);
  assert.equal(core.referenceValidation({schemaVersion:1,notes:[]},[]).referenceStatus,'legacy');
  model.rhythmValidation.blockingMeasureIndices=[];
  assert.equal(core.referenceValidation(model,['a']).allowed,false);
});
test('offline +50 cents and -50 cents, target mapping and signed offset are measurable', () => {
  const sampleRate = 44100;
  const high = 440 * Math.pow(2, 50 / 1200), low = 440 * Math.pow(2, -50 / 1200);
  const samples = new Float32Array(sampleRate * 2.5);
  samples.set(tone(high, sampleRate, 1), sampleRate / 2);
  samples.set(tone(low, sampleRate, 1), sampleRate * 1.5);
  const targets = core.buildTargets([
    {id:'one',part:'S',pitch:69,start:7,end:8}, {id:'two',part:'S',pitch:69,start:8,end:9},
  ]);
  const result = core.analyzeRecording(samples, sampleRate, {targets,offset:0.5,hopSeconds:0.06,toleranceCents:25});
  assert.ok(Math.abs(result.summary.perTarget.one.medianCents - 50) < 2);
  assert.ok(Math.abs(result.summary.perTarget.two.medianCents + 50) < 2);
  assert.ok(result.summary.coverage > 0.9);
  assert.ok(result.summary.medianAbsoluteCents > 48);
  // The analysis window straddling the +50/-50 transition can briefly average
  // the two; sustained regions must remain outside the tolerance.
  assert.ok(result.summary.inTuneFraction < 0.1);
  assert.ok(result.frames.some(frame => frame.time < 0.5 && frame.targetId === null));
});
test('offline silence counts against coverage while score rests do not', () => {
  const target = core.buildTargets([{id:'one',part:'S',pitch:69,start:0,end:1}, {id:'two',part:'S',pitch:69,start:2,end:3}]);
  const sampleRate = 48000, samples = new Float32Array(sampleRate * 3);
  samples.set(tone(440, sampleRate, 1));
  const {summary} = core.analyzeRecording(samples, sampleRate, {targets:target,hopSeconds:0.1});
  assert.ok(summary.coverage > 0.4 && summary.coverage < 0.6);
  assert.ok(summary.expectedFrames < summary.totalFrames);
  assert.equal(summary.perTarget.two.matchedFrames, 0);
});
test('singing an octave above is reported as +1200 cents', () => {
  const rate = 48000, targets = core.buildTargets([{id:'a4',part:'S',pitch:69,start:0,end:1}]);
  const {summary} = core.analyzeRecording(tone(880, rate, 1), rate, {targets, hopSeconds:0.1});
  assert.ok(Math.abs(summary.medianCents - 1200) < 1);
  assert.equal(summary.inTuneFraction, 0);
});

function referenceFixture({sampleRate=48000,delay=1837,gain=0.6,voiceFrequency=null,voiceAmplitude=0.12,shift=0}={}) {
  const length=4096,pad=Math.round(sampleRate*0.25);
  const signal=index=>0.24*Math.sin(2*Math.PI*220*index/sampleRate)+0.16*Math.sin(2*Math.PI*329.627*index/sampleRate+0.15)+0.10*Math.sin(2*Math.PI*392*index/sampleRate+0.8)+0.035*Math.sin(2*Math.PI*(700*index/sampleRate+0.000015*(index/sampleRate)**2));
  const frame=first=>{
    const reference=Float32Array.from({length:length+pad},(_,i)=>signal(first+i-pad));
    const samples=Float32Array.from({length},(_,i)=>gain*signal(first+i-delay)+(voiceFrequency?voiceAmplitude*Math.sin(2*Math.PI*voiceFrequency*(first+i)/sampleRate+0.3):0));
    return {samples,reference};
  };
  return {sampleRate,frame,frames:Array.from({length:9},(_,i)=>frame(10000+i*4889+shift))};
}
test('speaker calibration estimates frozen acoustic delay and gain without target pitch',()=>{
  for(const sampleRate of [44100,48000]){
    const fixture=referenceFixture({sampleRate,delay:1837,gain:0.6});
    const calibration=core.calibrateReference(fixture.frames,sampleRate);
    assert.equal(calibration.valid,true);
    assert.ok(Math.abs(calibration.delaySamples-1837)<2,JSON.stringify(calibration));
    assert.ok(Math.abs(calibration.gain-0.6)<0.001);
    assert.ok(calibration.noiseRms<0.001);
  }
});
test('background only never scores after known-reference calibration including an acoustic delay',()=>{
  const fixture=referenceFixture(),calibration=core.calibrateReference(fixture.frames,fixture.sampleRate);
  const frame=fixture.frame(50000),result=core.assessInterference(frame.samples,frame.reference,calibration,fixture.sampleRate,{minFrequency:50,maxFrequency:1600,backgroundFrequencies:[220,329.627,392]});
  assert.equal(result.frequency,null);
  assert.equal(result.reason,'reference-only');
  assert.ok(result.signal.residualRms<0.001);
});
test('a singer 50 cents sharp remains measurable under a louder calibrated polyphonic background',()=>{
  const quiet=referenceFixture({gain:1}),calibration=core.calibrateReference(quiet.frames,quiet.sampleRate);
  const singerFrequency=440*Math.pow(2,50/1200);
  const mixed=referenceFixture({gain:1,voiceFrequency:singerFrequency,voiceAmplitude:0.1});
  const frame=mixed.frame(60000),result=core.assessInterference(frame.samples,frame.reference,calibration,mixed.sampleRate,{minFrequency:50,maxFrequency:1600,backgroundFrequencies:[220,329.627,392]});
  assert.equal(result.voiced,true,result.reason);
  near(result.frequency,singerFrequency,2);
  assert.ok(result.signal.rawRms>result.signal.residualRms*2);
  assert.ok(Math.abs(core.centsDifference(result.frequency,440)-50)<2);
});
test('same background note or shared harmonics are rejected even when a singer is present',()=>{
  const quiet=referenceFixture(),calibration=core.calibrateReference(quiet.frames,quiet.sampleRate);
  for(const frequency of [220,440,660]){
    const mixed=referenceFixture({voiceFrequency:frequency}),frame=mixed.frame(60000);
    const result=core.assessInterference(frame.samples,frame.reference,calibration,mixed.sampleRate,{minFrequency:50,maxFrequency:1600,backgroundFrequencies:[220,329.627,392]});
    assert.equal(result.frequency,null,`${frequency}: ${result.reason}`);
    assert.equal(result.signal.suppressed,true);
  }
});
test('moving the speaker changes leakage gain and rejects leftover background instead of a target pass',()=>{
  const initial=referenceFixture({gain:0.6}),calibration=core.calibrateReference(initial.frames,initial.sampleRate);
  const moved=referenceFixture({gain:0.9}),frame=moved.frame(60000);
  const result=core.assessInterference(frame.samples,frame.reference,calibration,moved.sampleRate,{backgroundFrequencies:[220,329.627,392]});
  assert.equal(result.frequency,null);
  assert.equal(result.reason,'reference-dominated');
});
test('quiet calibration does not invent an acoustic model and polyphonic reference selection excludes singer',()=>{
  const frames=Array.from({length:4},()=>({samples:new Float32Array(4096),reference:new Float32Array(16096)}));
  assert.equal(core.calibrateReference(frames,48000).valid,false);
  const notes=[{id:'target1',kind:'note',part:'S',pitch:69,start:10.5,end:11.5},{id:'target2',kind:'note',part:'S',pitch:71,start:11.5,end:12},{id:'other-selected',kind:'note',part:'A',pitch:60,start:10,end:11},{id:'bg-held',kind:'note',part:'A',pitch:60,start:9,end:12},{id:'bg-next',kind:'note',part:'A',pitch:64,start:11,end:13},{id:'bg-outside',kind:'note',part:'A',pitch:65,start:13,end:14}];
  const region=core.practiceRegion(notes,['target1','target2','other-selected'],{part:'S',backgroundParts:['A','S']});
  assert.deepEqual(region.backgroundParts,['A']);
  assert.deepEqual(region.backgroundIds,['other-selected','bg-held','bg-next']);
  assert.equal(region.timeline.origin,10);
  assert.equal(region.timeline.targets[0].start,0.5);
  assert.equal(region.timeline.duration,2);
});
