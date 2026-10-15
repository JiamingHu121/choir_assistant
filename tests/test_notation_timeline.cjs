'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const {measureAtTime,mappedMeasure,measureFollowAction,validateNotationOverlay,loadNotationOverlay}=require('../workbench/interactive.js');
const measures=[{number:47,start:220.8,end:225.6},{number:48,start:225.6,end:232.8},{number:49,start:232.8,end:237.6},{number:64,start:304.8,end:309.6}];
test('late score position follows canonical seconds across the real 6/4 meter and final tied tail',()=>{
  assert.equal(measureAtTime(measures,225.6).number,48);
  assert.equal(measureAtTime(measures,232.7).number,48);
  assert.equal(measureAtTime(measures,232.8).number,49);
  assert.equal(measureAtTime(measures,309.599).number,64);
  assert.equal(measureAtTime(measures,309.6).number,64);
  assert.equal(measureAtTime(measures,NaN),null);
});
test('renderer measure numbering uses canonical rests and notes, never random SVG IDs or bounding-box IDs',()=>{
  const symbols=new Map([['n-part-m52-o1',{measure:52}],['rest-part-m53-o1',{measure:53}]]);
  assert.equal(mappedMeasure(['random-svg-id','bbox-n-part-m52-o1','n-part-m52-o1'],symbols),52);
  assert.equal(mappedMeasure(['rest-part-m53-o1'],symbols),53);
  assert.equal(mappedMeasure(['n-part-m52-o1','rest-part-m53-o1'],symbols),null);
  assert.equal(mappedMeasure(['random-svg-id'],symbols),null);
});
test('pickup and repeated printed numbers keep distinct canonical lyric and highlight targets',()=>{
  const model={revision:'pickup',measures:[
    {index:1,number:0,start:0,end:.5},
    {index:2,number:1,start:.5,end:2.5},
    {index:3,number:1,start:2.5,end:4.5},
  ]};
  const symbols=new Map([['pickup',{measure:0,measureIndex:1}],['first-one',{measure:1,measureIndex:2}],['second-one',{measure:1,measureIndex:3}]]);
  assert.equal(mappedMeasure(['pickup'],symbols),1);
  assert.equal(mappedMeasure(['first-one'],symbols),2);
  assert.equal(mappedMeasure(['second-one'],symbols),3);
  const overlay={schemaVersion:1,revision:'pickup',measureLyrics:[{measure:1,part:'S',lines:['pickup']},{measure:2,part:'S',lines:['first']},{measure:3,part:'S',lines:['second']}]};
  validateNotationOverlay(overlay,model);
  const at=measureAtTime(model.measures,3);
  assert.equal(at.number,1);assert.equal(at.index,3);
  assert.equal(overlay.measureLyrics.find(lyric=>lyric.measure===at.index).lines[0],'second');
  assert.throws(()=>validateNotationOverlay({...overlay,measureLyrics:[{measure:0,part:'S',lines:['printed zero is not an index']}]},model),/格式/);
});
const follow={view:'notation',playing:true,follow:true,current:{number:53},outside:true,key:'53',previousKey:'52',jumped:false};
test('cross-page rests follow, repeated fast notes do not restart scrolling, and paused seeks relocate',()=>{
  assert.equal(measureFollowAction(follow),'smooth');
  assert.equal(measureFollowAction({...follow,previousKey:'53'}),null);
  assert.equal(measureFollowAction({...follow,previousKey:'53',playing:false,forceFollow:true}),'auto');
  assert.equal(measureFollowAction({...follow,previousKey:'53',jumped:true}),'auto');
  assert.equal(measureFollowAction({...follow,view:'original'}),null);
  assert.equal(measureFollowAction({...follow,follow:false}),null);
});
test('lyrics reject a different revision or an absent measure and render only literal text',()=>{
  const model={revision:'fixture',measures};
  const overlay={schemaVersion:1,revision:'fixture',measureLyrics:[{measure:48,part:'S',lines:['<script>literal text</script>']}]};
  assert.equal(validateNotationOverlay(overlay,model),overlay);
  assert.throws(()=>validateNotationOverlay({...overlay,revision:'other'},model),/版本/);
  assert.throws(()=>validateNotationOverlay({...overlay,measureLyrics:[{measure:99,part:'S',lines:['text']}]},model),/格式/);
});
test('a lyric outage has a bounded visible error and does not stall notation initialization',async()=>{
  const result=await loadNotationOverlay('fixture',{revision:'fixture',measures},async(url,{signal})=>new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(Object.assign(new Error('abort'),{name:'AbortError'})),{once:true})),10);
  assert.match(result.error,/唱词加载超时/);
});
