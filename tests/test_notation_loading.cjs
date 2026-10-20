'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const {loadNotationAssets,loadNotationProgressively}=require('../workbench/interactive.js');
const deferred=()=>{let resolve;const promise=new Promise(yes=>resolve=yes);return {promise,resolve};};
const flush=()=>new Promise(setImmediate);
test('one missing SVG does not discard the other notation pages',async()=>{
  const requests=[];
  const result=await loadNotationAssets([{page:1,svg:'/assets/one.svg'},{page:2,svg:'/assets/two.svg?x=1'}],'rev A',async url=>{
    requests.push(url);
    return url.includes('one.svg')?{ok:false,status:404}:{ok:true,text:async()=>'<svg/>'};
  });
  assert.match(result[0].error,/第 1 页.*404/);
  assert.equal(result[1].text,'<svg/>');
  assert.deepEqual(requests,['/assets/one.svg?revision=rev%20A','/assets/two.svg?x=1&revision=rev%20A']);
});
test('failed network or response body remains an explicit per-page error',async()=>{
  const result=await loadNotationAssets([{page:1,svg:'one'},{page:2,svg:'two'}],'r',async url=>{
    if(url.startsWith('one'))throw new Error('network failed');
    return {ok:true,text:async()=>{throw new Error('body interrupted');}};
  });
  assert.deepEqual(result.map(item=>item.error),['network failed','body interrupted']);
});
test('an unresponsive SVG has a bounded wait and cannot leave every page loading forever',async()=>{
  const result=await loadNotationAssets([{page:1,svg:'good'},{page:2,svg:'hang'}],'r',async(url,{signal})=>{
    if(url.startsWith('good'))return {ok:true,text:async()=>'<svg/>'};
    return new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(Object.assign(new Error('abort'),{name:'AbortError'})),{once:true}));
  },10);
  assert.equal(result[0].text,'<svg/>');
  assert.match(result[1].error,/第 2 页加载超时/);
});
test('the first page becomes usable while a later page remains slow, with at most two background transfers',async()=>{
  const pages=Array.from({length:6},(_,i)=>({page:i+1,svg:String(i+1)}));
  const gates=new Map(pages.map(page=>[page.page,deferred()]));
  const rendered=[],requested=[];let inflight=0,maximum=0;
  const loading=loadNotationProgressively(pages,'r',asset=>rendered.push(asset.page.page),{fetchAsset:async url=>{
    const number=Number(url.split('?')[0]);requested.push(number);maximum=Math.max(maximum,++inflight);await gates.get(number).promise;inflight--;
    return {ok:true,text:async()=>'<svg/>'};
  }});
  assert.deepEqual(requested,[1]);gates.get(1).resolve();await loading.firstPage;await flush();
  assert.deepEqual(rendered,[1]);assert.deepEqual(requested,[1,2,3]);
  gates.get(3).resolve();await flush();assert.deepEqual(rendered,[1,3]);assert.deepEqual(requested,[1,2,3,4]);
  for(const number of [2,4,5,6])gates.get(number).resolve();await loading.done;
  assert.equal(maximum,2);assert.deepEqual([...rendered].sort((a,b)=>a-b),[1,2,3,4,5,6]);
});
test('a failed first page remains explicit and does not block usable later pages',async()=>{
  const rendered=[];
  const loading=loadNotationProgressively([{page:1,svg:'one'},{page:2,svg:'two'}],'r',asset=>rendered.push(asset),{fetchAsset:async url=>url.startsWith('one')?{ok:false,status:404}:{ok:true,text:async()=>'<svg/>'}});
  await loading.firstPage;assert.match(rendered[0].error,/404/);await loading.done;assert.equal(rendered[1].text,'<svg/>');
});
test('refreshing a model prevents stale in-flight pages from replacing the newer notation',async()=>{
  const gate=deferred(),rendered=[],requested=[];let current=true;
  const loading=loadNotationProgressively([{page:1,svg:'one'},{page:2,svg:'two'}],'r',asset=>rendered.push(asset),{isCurrent:()=>current,fetchAsset:async url=>{
    requested.push(url);await gate.promise;return {ok:true,text:async()=>'<svg/>'};
  }});
  current=false;gate.resolve();await loading.done;
  assert.equal(rendered.length,0);assert.equal(requested.length,1);
});
