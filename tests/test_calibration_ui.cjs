'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync(require.resolve('../workbench/calibration_ui.js'),'utf8');
const helper=require('../workbench/calibration_ui.js');
const candidate={id:'beam-1',ruleId:'beam-duration',measureIndices:[5],edits:[{noteId:'n1',duration:.5}],current:{duration:1},proposed:{duration:.5},status:'pending',reason:'one beam',evidenceUrls:['/assets/source.png']};
const response=data=>({ok:true,status:200,json:async()=>data});
const flush=async()=>{for(let i=0;i<8;i++)await Promise.resolve();};
function environment(fetcher) {
  const elements={},timers=[];
  function element(id){return elements[id] ||= {hidden:false,disabled:false,value:id==='calibration-filter'?'all':'',textContent:'',classList:{add(){},toggle(){}},listeners:{},addEventListener(name,fn){this.listeners[name]=fn;},set innerHTML(html){this.html=html;for(const match of html.matchAll(/id="([^"]+)"/g))element(match[1]);},get innerHTML(){return this.html;}};}
  const document={getElementById:element};
  const scope={document,module:{exports:{}},URL,fetch:fetcher,setTimeout(fn){timers.push(fn);return fn;},clearTimeout(){},location:{reload(){}}};
  vm.runInNewContext(source,scope);
  const controller=scope.module.exports.initCalibrationUI();
  return {controller,elements,element,timers};
}
test('accepted corrections require source checking, reason and evidence',()=>{
  assert.throws(()=>helper.decisionPayload(candidate,{decision:'accept',reason:'beam'}),/核对原谱/);
  assert.throws(()=>helper.decisionPayload(candidate,{decision:'accept',sourceChecked:true,reason:'beam'}),/依据和原谱/);
  const decision=helper.decisionPayload(candidate,{decision:'accept',sourceChecked:true,reason:'single beam visible',evidence:'/assets/source.png'});
  assert.equal(decision.candidateId,'beam-1');assert.deepEqual(decision.evidence,['/assets/source.png']);
  assert.throws(()=>helper.decisionPayload({...candidate,edits:[]},{decision:'accept',sourceChecked:true,reason:'beam'}),/没有可提交/);
  assert.throws(()=>helper.decisionPayload(candidate,{decision:'accept',sourceChecked:true,reason:'beam',evidence:'https://example.com/source.png'}),/本地原谱/);
  assert.throws(()=>helper.decisionPayload({...candidate,canApply:false},{decision:'accept',sourceChecked:true,reason:'beam',evidence:'/assets/source.png'}),/没有可提交/);
});
test('evidence links reject executable or traversal values and text is escaped',()=>{
  assert.equal(helper.evidenceURL('javascript:alert(1)'),null);assert.equal(helper.evidenceURL('/generated/../secret'),null);
  assert.equal(helper.evidenceURL('/generated/report/evidence.png'),'/generated/report/evidence.png');
  assert.equal(helper.escape('<img onerror="x">'),'&lt;img onerror=&quot;x&quot;&gt;');
});
test('whole-score scan binds current revision and disables duplicate scan while running',async()=>{
  const requests=[];
  const s=environment(async(url,options)=>{requests.push({url,options});return response(options?.method==='POST'?{status:'running',revision:'rev-A',runId:'run-A'}:{status:'idle',revision:'rev-A'});});await flush();
  await s.element('calibration-scan').onclick();assert.deepEqual(JSON.parse(requests[1].options.body),{action:'scan',revision:'rev-A'});
  assert.equal(s.element('calibration-scan').disabled,true);assert.equal(s.timers.length,1);
  await s.element('calibration-scan').onclick();assert.equal(requests.length,2);
});
test('a stale refresh cannot replace a newer ready queue',async()=>{
  let resolveInitial;let count=0;
  const s=environment(()=>++count===1?new Promise(resolve=>resolveInitial=resolve):Promise.resolve(response({status:'ready',revision:'new',runId:'run-new',candidates:[candidate]})));
  await s.controller.refresh();resolveInitial(response({status:'idle',revision:'old'}));await flush();
  assert.equal(s.controller.state.revision,'new');assert.match(s.element('calibration-queue').innerHTML,/beam-duration/);
  assert.match(s.element('calibration-queue').innerHTML,/textarea data-field="evidence"[^>]*>\/assets\/source.png/);
});
test('source-reviewed decisions bind both run and revision on submission',async()=>{
  const requests=[];
  const s=environment(async(url,options)=>{requests.push({url,options});return response(options?.method==='POST'?{status:'running',revision:'rev-A',runId:'run-A'}:{status:'ready',revision:'rev-A',runId:'run-A',candidates:[candidate]});});await flush();
  function input(field,value){s.element('calibration-queue').listeners.input({target:{dataset:{field},value,checked:value,closest(){return {dataset:{candidateId:'beam-1'}};}}});}
  input('decision','accept');input('reason','PDF has one beam');input('evidence','/assets/source.png');
  await s.element('calibration-submit').onclick();assert.equal(requests.length,1);assert.match(s.element('calibration-status').textContent,/核对原谱/);
  input('sourceChecked',true);await s.element('calibration-submit').onclick();
  const body=JSON.parse(requests[1].options.body);assert.equal(body.revision,'rev-A');assert.equal(body.runId,'run-A');assert.equal(body.decisions[0].candidateId,'beam-1');assert.equal(body.decisions[0].decision,'accept');
});
