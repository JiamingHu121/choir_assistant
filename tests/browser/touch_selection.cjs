'use strict';
// Optional real-browser regression: NODE_PATH=<playwright packages> node --test
// tests/browser/touch_selection.cjs. Uses an isolated browser and loopback fixture,
// never a user's browser profile, microphone, account, or production website.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const http=require('node:http');
const {chromium}=require('playwright');

const workbench=path.resolve(__dirname,'../../workbench');
const notes=[['a',60,500,600],['b',62,1400,600],['c',64,2300,1900]];
const model={revision:'touch-fixture',notes:notes.map(([id,pitch],i)=>({id,pitch,part:'S',measure:1,duration:1,start:i,end:i+1})),rests:[],measures:[{index:1,number:1,start:0,end:4}],pagesInteractive:[{page:1,svg:'/fixture.svg'}]};
const svg='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 3000 10000">'+notes.map(([id,,x,y])=>`<g class="note" id="${id}"><g class="notehead"><ellipse cx="${x}" cy="${y}" rx="90" ry="60"/></g></g>`).join('')+'</svg>';
const html=`<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/interactive.css"><style>:root{--ink:#263c38;--line:#d3ded5;--muted:#67756b;--green:#286a50}body{margin:0;font:12px sans-serif}#interactive-host{height:800px}.notation-workspace{flex:0 0 300px}.notation-stage{padding:10px}.notation-page{padding:35px 0 0}.notation-page svg{height:1100px}</style><div id="interactive-host"></div><div id="original-view" hidden></div><script src="/interactive.js"></script><script>window.selectedEvents=[];window.loads=[];window.model=${JSON.stringify(model)};window.ready=initInteractive(window.model,{follow:()=>false,onSelection:ids=>selectedEvents.push(ids),onAudio:result=>loads.push(result)}).then(api=>window.controller=api);</script>`;

test('native mobile touch and desktop selection regressions',async t=>{
  const requests=[];
  let finishRender;
  const server=http.createServer(async(req,res)=>{
    const name=req.url.split('?')[0];
    if(name==='/api/selection'){
      let body='';for await(const chunk of req)body+=chunk;
      requests.push(JSON.parse(body));
      finishRender=()=>{if(res.writableEnded)return;res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify({wav:'/fixture.wav',start:0,duration:1,revision:model.revision}));};
      return;
    }
    const assets={'/':html,'/fixture.svg':svg};
    if(name==='/interactive.js'||name==='/interactive.css'){
      res.writeHead(200,{'Content-Type':name.endsWith('.js')?'text/javascript':'text/css'});res.end(fs.readFileSync(path.join(workbench,name.slice(1))));return;
    }
    if(name in assets){res.writeHead(200,{'Content-Type':name.endsWith('.svg')?'image/svg+xml':'text/html'});res.end(assets[name]);return;}
    res.writeHead(404);res.end();
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const browser=await chromium.launch({headless:true,...(process.env.CHOIR_BROWSER_EXECUTABLE?{executablePath:process.env.CHOIR_BROWSER_EXECUTABLE}:{channel:'chrome'})});
  const context=await browser.newContext({viewport:{width:390,height:844},hasTouch:true,isMobile:true});
  const page=await context.newPage();page.setDefaultTimeout(6000);
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  const session=await context.newCDPSession(page);
  const origin=`http://127.0.0.1:${server.address().port}`;
  const selected=()=>page.evaluate(()=>window.controller.getSelected().sort());
  const pos=async id=>{const b=await page.locator(`#${id} .notehead`).boundingBox();assert.ok(b);return{x:b.x+b.width/2,y:b.y+b.height/2};};
  const tap=async id=>{const p=await pos(id);await page.touchscreen.tap(p.x,p.y);};
  const touch=async(type,points)=>session.send('Input.dispatchTouchEvent',{type,touchPoints:points.map((p,i)=>({id:p.id??i+1,x:p.x,y:p.y,radiusX:4,radiusY:4,force:1}))});
  const slide=async(start,end)=>{await touch('touchStart',[start]);for(let i=1;i<=6;i++){await touch('touchMove',[{x:start.x+(end.x-start.x)*i/6,y:start.y+(end.y-start.y)*i/6}]);await page.waitForTimeout(20);}await touch('touchEnd',[]);};
  const reset=async()=>{await page.goto(origin);await page.evaluate(()=>window.ready);assert.equal(await page.locator('#interactive-host').getAttribute('data-mapped-notes'),'3');};
  const box=async()=>page.getByRole('button',{name:'触屏框选',exact:true}).tap();
  const frame=async()=>{const a=await pos('a'),b=await pos('b');return{start:{x:a.x-22,y:a.y-20},end:{x:b.x+22,y:b.y+20}};};
  try{
    await t.test('tap replaces, repeated tap stays selected, and explicit additive toggles only selected notes',async()=>{
      await reset();await tap('a');assert.deepEqual(await selected(),['a']);await tap('b');assert.deepEqual(await selected(),['b']);await tap('b');assert.deepEqual(await selected(),['b']);
      await page.getByRole('button',{name:'加选音符',exact:true}).tap();await tap('a');assert.deepEqual(await selected(),['a','b']);await tap('b');assert.deepEqual(await selected(),['a']);
      await page.getByRole('button',{name:'加选音符',exact:true}).tap();await tap('b');assert.deepEqual(await selected(),['b']);
    });
    await t.test('native swipe scrolls and never selects the note passed under the finger',async()=>{
      await reset();await tap('a');const s=await page.locator('#interactive-stage').boundingBox();const before=await page.locator('#interactive-stage').evaluate(e=>e.scrollTop);
      await slide({x:s.x+s.width/2,y:s.y+s.height-40},{x:s.x+s.width/2,y:s.y+35});
      assert.ok(await page.locator('#interactive-stage').evaluate(e=>e.scrollTop)>before+20);assert.deepEqual(await selected(),['a']);
    });
    await t.test('box touch captures from whitespace in both directions and returning to point restores panning',async()=>{
      await reset();await box();assert.equal(await page.locator('#interactive-stage').evaluate(e=>getComputedStyle(e).touchAction),'none');
      let f=await frame();await slide(f.start,f.end);assert.deepEqual(await selected(),['a','b']);
      await page.getByRole('button',{name:'清除选择',exact:true}).tap();f=await frame();await slide(f.end,f.start);assert.deepEqual(await selected(),['a','b']);
      await page.getByRole('button',{name:'点选与滚动',exact:true}).tap();assert.notEqual(await page.locator('#interactive-stage').evaluate(e=>getComputedStyle(e).touchAction),'none');
    });
    await t.test('additive box retains an earlier note and subsequent plain box replaces it',async()=>{
      await reset();await tap('c');await box();await page.getByRole('button',{name:'加选音符',exact:true}).tap();let f=await frame();await slide(f.start,f.end);assert.deepEqual(await selected(),['a','b','c']);
      await page.getByRole('button',{name:'加选音符',exact:true}).tap();f=await frame();await slide(f.start,f.end);assert.deepEqual(await selected(),['a','b']);
    });
    await t.test('native pointer cancellation restores initial selection and hides rectangle',async()=>{
      await reset();await tap('c');await box();const f=await frame();await touch('touchStart',[f.start]);await touch('touchMove',[f.end]);assert.deepEqual(await selected(),['a','b']);await touch('touchCancel',[]);
      assert.deepEqual(await selected(),['c']);assert.equal(await page.locator('.notation-drag-box').isVisible(),false);await tap('b');assert.deepEqual(await selected(),['b']);
    });
    await t.test('second finger cancels the whole gesture instead of committing an accidental tap',async()=>{
      await reset();await tap('c');await box();const f=await frame();await touch('touchStart',[{...f.start,id:1}]);await touch('touchMove',[{...f.end,id:1}]);
      await touch('touchStart',[{...f.end,id:1},{x:f.end.x+25,y:f.end.y+25,id:2}]);await touch('touchEnd',[]);
      assert.deepEqual(await selected(),['c']);assert.equal(await page.locator('.notation-drag-box').isVisible(),false);await tap('a');assert.deepEqual(await selected(),['a']);
    });
    await t.test('Escape cancels capture and cannot resurrect a temporary drag selection',async()=>{
      await reset();await tap('c');await box();const f=await frame();await touch('touchStart',[f.start]);await touch('touchMove',[f.end]);await page.keyboard.press('Escape');await touch('touchMove',[f.start]);await touch('touchEnd',[]);
      assert.deepEqual(await selected(),[]);assert.equal(await page.locator('.notation-drag-box').isVisible(),false);
    });
    await t.test('view change and model refresh cancel a gesture',async()=>{
      for(const operation of ['view','refresh']){
        await reset();await tap('c');await box();const f=await frame();await touch('touchStart',[f.start]);await touch('touchMove',[f.end]);
        await page.evaluate(async op=>{if(op==='view'){controller.setView('original');controller.setView('notation');}else await controller.refresh(window.model);},operation);
        await touch('touchEnd',[]);assert.deepEqual(await selected(),['c']);assert.equal(await page.locator('.notation-drag-box').isVisible(),false);
      }
    });
    await t.test('mouse drag and Shift keyboard preserve desktop selection semantics',async()=>{
      await reset();const a=await pos('a'),b=await pos('b');await page.mouse.click(a.x,a.y);assert.deepEqual(await selected(),['a']);await page.keyboard.down('Shift');await page.mouse.click(b.x,b.y);await page.keyboard.up('Shift');assert.deepEqual(await selected(),['a','b']);
      await page.locator('#b').focus();await page.keyboard.press('Shift+Enter');assert.deepEqual(await selected(),['a']);
      const f=await frame();await page.mouse.move(f.start.x,f.start.y);await page.mouse.down();await page.mouse.move(f.end.x,f.end.y,{steps:8});await page.mouse.up();assert.deepEqual(await selected(),['a','b']);
    });
    await t.test('synthesis submits stable IDs and rejects touch and keyboard changes while busy',async()=>{
      await reset();await tap('a');await page.getByRole('button',{name:'合成并播放所选',exact:true}).tap();
      await page.waitForFunction(()=>document.querySelector('.notation-actions button').disabled);
      await tap('b');await page.locator('#b').focus();await page.keyboard.press('Enter');assert.deepEqual(await selected(),['a']);
      assert.deepEqual(requests.at(-1).noteIds,['a']);finishRender();await page.waitForFunction(()=>window.loads.length===1);
      await tap('b');assert.deepEqual(await selected(),['b']);
    });
    assert.deepEqual(errors,[]);
    if(process.env.CHOIR_TOUCH_PROOF_DIR){fs.mkdirSync(process.env.CHOIR_TOUCH_PROOF_DIR,{recursive:true});await reset();await box();const f=await frame();await slide(f.start,f.end);await page.screenshot({path:path.join(process.env.CHOIR_TOUCH_PROOF_DIR,'touch-selection-fixture.png')});}
  }finally{finishRender?.();await context.close();await browser.close();await new Promise(resolve=>server.close(resolve));}
});
