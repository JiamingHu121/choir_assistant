'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const {loadNotationAssets}=require('../workbench/interactive.js');
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
