'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require.resolve('../choir_assistant/deployment/static/app.js'),'utf8');
const deferred=()=>{let resolve;const promise=new Promise(yes=>resolve=yes);return {promise,resolve};};
test('a slow site configuration does not delay the catalog request or its visible summary',async()=>{
  class Element {
    constructor(){this.value='';this.hidden=false;this.children=[];this.textContent='';}
    replaceChildren(...children){this.children=children;}
    append(...children){this.children.push(...children);}
    addEventListener(){}
    get firstChild(){return this.children[0];}
  }
  const elements=new Map(),calls=[],config=deferred(),catalog=deferred();
  const document={title:'',getElementById:id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},createElement:()=>new Element()};
  vm.runInNewContext(source,{document,location:{protocol:'https:',hash:''},URLSearchParams,fetch:async url=>{
    calls.push(url);const value=await (url==='/api/site-config'?config.promise:catalog.promise);return {ok:true,json:async()=>value};
  }});
  assert.deepEqual(calls,['/api/site-config','/api/catalog']);
  catalog.resolve({totals:{ready:5,works:5,notes:9038},works:[]});await new Promise(setImmediate);
  assert.equal(document.getElementById('total').textContent,'5 / 5 首 · 9,038 音符');
  config.resolve({siteTitle:'心乐合唱团曲库',feedbackEnabled:true});await new Promise(setImmediate);
  assert.equal(document.title,'心乐合唱团曲库');assert.equal(document.getElementById('feedback-open').hidden,false);
});
