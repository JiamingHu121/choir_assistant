'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require.resolve('../choir_assistant/deployment/static/app.js'),'utf8');
const deferred=()=>{let resolve;const promise=new Promise(yes=>resolve=yes);return {promise,resolve};};
const response=value=>({ok:true,json:async()=>value});
const flush=async()=>{for(let i=0;i<16;i++)await Promise.resolve();await new Promise(setImmediate);};
const emptyCatalog={totals:{ready:0,works:0,notes:0},works:[]};
const tutorialKey='choir-workbench-tutorial-v1';
function environment(fetcher,{storage=new Map(),resizeObserver=true}={}){
  class Element {
    constructor(tag='div'){this.tagName=tag.toUpperCase();this.value='';this.hidden=false;this.disabled=false;this.open=false;this.children=[];this.textContent='';this.attributes=new Map();this.listeners=new Map();this.focused=false;this.modalCount=0;}
    replaceChildren(...children){this.children=children;}
    append(...children){this.children.push(...children);}
    addEventListener(type,callback){this.listeners.set(type,callback);}
    setAttribute(name,value){this.attributes.set(name,String(value));}
    getAttribute(name){return this.attributes.get(name)??null;}
    getBoundingClientRect(){return {height:108.4};}
    showModal(){this.open=true;this.modalCount++;}
    close(){this.open=false;}
    focus(){this.focused=true;}
    get firstChild(){return this.children[0];}
    set innerHTML(_){throw Error('Frontend must insert untrusted messages as literal text');}
  }
  const elements=new Map(),calls=[],properties=new Map(),events=new Map();
  const element=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  element('board-category').value='suggestion';
  const document={title:'',getElementById:element,createElement:tag=>new Element(tag),querySelector:selector=>selector==='header'?element('header'):null,documentElement:{style:{setProperty:(name,value)=>properties.set(name,value)}}};
  const context={document,location:{protocol:'https:',hash:''},URLSearchParams,window:{addEventListener:(name,callback)=>events.set(name,callback)},localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value)},fetch:async(url,options)=>{calls.push({url,options});return fetcher(url,options);}};
  if(resizeObserver)context.ResizeObserver=class{constructor(callback){this.callback=callback;}observe(target){this.target=target;this.callback();}};
  vm.runInNewContext(source,context);
  return {document,element,calls,storage,properties,events};
}
function allText(element){return [element.textContent,...element.children.map(allText)].join(' ');}
const event=()=>({preventDefault(){this.defaultPrevented=true;}});

test('a slow site configuration does not delay the catalog request or its visible summary',async()=>{
  const config=deferred(),catalog=deferred();
  const s=environment(async url=>response(await(url==='/api/site-config'?config.promise:catalog.promise)));
  assert.deepEqual(s.calls.map(call=>call.url),['/api/site-config','/api/catalog']);
  catalog.resolve({totals:{ready:5,works:5,notes:9038},works:[]});await flush();
  assert.equal(s.element('total').textContent,'5 / 5 首 · 9,038 音符');
  config.resolve({siteTitle:'心乐合唱团曲库',feedbackEnabled:true});await flush();
  assert.equal(s.document.title,'心乐合唱团曲库');assert.equal(s.element('feedback-open').hidden,false);
  assert.equal(s.properties.get('--site-header-height'),'109px');
});

test('first use opens the tutorial, completion persists, and manual reopening remains available',async()=>{
  const storage=new Map();const fetcher=async url=>response(url==='/api/catalog'?emptyCatalog:{siteTitle:'曲库'});
  const s=environment(fetcher,{storage});await flush();
  assert.equal(s.element('tutorial-dialog').open,true);assert.equal(s.element('tutorial-back').disabled,true);
  for(let step=0;step<4;step++)s.element('tutorial-next').onclick();
  assert.equal(s.element('tutorial-next').textContent,'开始使用');s.element('tutorial-next').onclick();
  assert.equal(s.element('tutorial-dialog').open,false);assert.equal(storage.get(tutorialKey),'done');
  const reopened=environment(fetcher,{storage});await flush();assert.equal(reopened.element('tutorial-dialog').open,false);
  reopened.element('tutorial-open').onclick();assert.equal(reopened.element('tutorial-dialog').open,true);assert.equal(reopened.element('tutorial-progress').getAttribute('aria-label'),'教程第 1 步，共 5 步');
  reopened.element('tutorial-next').onclick();assert.equal(reopened.element('tutorial-content').children[1].focused,true);
});

test('skip and Escape mark the tutorial seen; resize fallback updates header geometry',async()=>{
  const s=environment(async url=>response(url==='/api/catalog'?emptyCatalog:{}),{resizeObserver:false});await flush();
  s.element('tutorial-skip').onclick();assert.equal(s.storage.get(tutorialKey),'done');assert.equal(s.element('tutorial-dialog').open,false);
  s.element('tutorial-open').onclick();const cancel=event();s.element('tutorial-dialog').listeners.get('cancel')(cancel);assert.equal(cancel.defaultPrevented,true);assert.equal(s.element('tutorial-dialog').open,false);
  assert.equal(typeof s.events.get('resize'),'function');s.events.get('resize')();assert.equal(s.properties.get('--site-header-height'),'109px');
});

test('anonymous board renders markup as literal text and posts trimmed category with a fresh CSRF token',async()=>{
  const dangerous='<img src=x onerror=alert(1)><script>attack()</script>';let configCalls=0;const posts=[];
  const s=environment(async(url,options)=>{
    if(url==='/api/catalog')return response(emptyCatalog);
    if(url==='/api/site-config')return response({siteTitle:'曲库',authRequired:true,messageBoardEnabled:true,csrfToken:'csrf-'+(++configCalls)});
    if(url==='/api/message-board'&&options?.method==='POST'){posts.push(JSON.parse(options.body));return response({saved:true});}
    if(url==='/api/message-board')return response({items:[{category:'song',message:dangerous,created:'2026-10-27T12:00:00Z'}]});
    throw Error('Unexpected request '+url);
  },{storage:new Map([[tutorialKey,'done']])});await flush();
  assert.equal(s.element('board-open').hidden,false);s.element('board-open').onclick();await flush();
  assert.equal(s.element('board-dialog').open,true);assert.match(allText(s.element('board-feed')),/匿名团友/);assert.ok(allText(s.element('board-feed')).includes(dangerous));
  s.element('board-category').value='song';s.element('board-message').value='  想唱这首歌 🎵  ';await s.element('board-form').onsubmit(event());
  assert.deepEqual(posts,[{csrfToken:'csrf-2',category:'song',message:'想唱这首歌 🎵'}]);assert.equal(s.calls.some(call=>call.url==='/api/login'),false);
  assert.equal(s.element('board-message').value,'');assert.equal(s.element('board-counter').textContent,'0 / 1200');assert.equal(s.element('board-submit').disabled,false);assert.match(s.element('board-status').textContent,/已匿名发布/);
});

test('empty board messages never post and repeated submission is ignored while pending',async()=>{
  const saved=deferred();let posts=0;
  const s=environment(async(url,options)=>{if(url==='/api/catalog')return response(emptyCatalog);if(url==='/api/site-config')return response({messageBoardEnabled:true,csrfToken:'csrf'});if(options?.method==='POST'){posts++;return response(await saved.promise);}return response({items:[]});},{storage:new Map([[tutorialKey,'done']])});await flush();
  const before=s.calls.length;s.element('board-message').value=' \n ';await s.element('board-form').onsubmit(event());assert.equal(s.calls.length,before);assert.match(s.element('board-status').textContent,/先写一点/);
  s.element('board-message').value='排练建议';const pending=s.element('board-form').onsubmit(event());await flush();assert.equal(s.element('board-submit').disabled,true);assert.equal(posts,1);
  const repeated=s.element('board-form').onsubmit(event());await flush();assert.equal(posts,1);assert.equal(s.element('board-submit').disabled,true);await repeated;
  saved.resolve({saved:true});await pending;assert.equal(s.element('board-submit').disabled,false);
});
