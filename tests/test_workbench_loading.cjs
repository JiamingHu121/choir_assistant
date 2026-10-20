'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require.resolve('../workbench/app.js'),'utf8');
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const flush=()=>new Promise(setImmediate);
function environment({interactive=true}={}){
  const originals=[],elements=new Map(),requests=[],canonical=deferred();let hooks;
  class Element{
    constructor(tag='div'){this.tag=tag;this.children=[];this.dataset={};this.style={};this.value='0';this.checked=false;this.textContent='';this.classList={add(){},remove(){},toggle(){}};}
    append(...children){this.children.push(...children);}
    addEventListener(){}
    setAttribute(){}
    querySelector(){return null;}
    querySelectorAll(){return this.children.flatMap(child=>[...(child.tag==='img'&&child.dataset.src?[child]:[]),...child.querySelectorAll()]);}
    set src(value){this.source=value;if(this.tag==='img')originals.push(value);}
    get src(){return this.source;}
  }
  const document={getElementById(id){if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},createElement:tag=>new Element(tag),querySelector:()=>null,addEventListener(){}};
  const audio=document.getElementById('audio');Object.assign(audio,{paused:true,currentTime:0,playbackRate:1});
  document.getElementById('speed').value='1';document.getElementById('volume').value='.8';
  const score={title:'fixture',subtitle:'',duration:4,interactive,groups:[],audio:'/base.mp3',pages:[{page:1,image:'/original-1.png',width:1000,height:1400},{page:2,image:'/original-2.png',width:1000,height:1400}],measures:[{number:1,page:1,start:0,end:4,x:0,y:0,w:1,h:1}]};
  const model={notes:[],measures:score.measures,rhythmValidation:{blockingMeasures:[],status:'validated'}};
  const context={document,location:{reload(){}},requestAnimationFrame(){},fetch:async url=>{requests.push(url);return url==='/assets/score.json'?{ok:true,json:async()=>score}:{ok:true,json:async()=>canonical.promise};},initInteractive:async(data,value)=>{hooks=value;value.onView('notation');return {getSelected:()=>[],update(){}};}};
  vm.runInNewContext(source,context);
  return {originals,requests,document,canonical,model,hooks:()=>hooks};
}
test('interactive entry defers original images until PDF is selected and activates each source once',async()=>{
  const s=environment();await flush();
  assert.deepEqual(s.requests,['/assets/score.json','/assets/interactive.json']);assert.deepEqual(s.originals,[]);
  s.canonical.resolve(s.model);await flush();assert.deepEqual(s.originals,[]);
  assert.equal(typeof s.document.getElementById('play').onclick,'function');
  s.hooks().onView('original');assert.deepEqual(s.originals,['/original-1.png','/original-2.png']);
  s.hooks().onView('notation');s.hooks().onView('original');assert.equal(s.originals.length,2);
});
test('a score without interactive notation keeps the original score available immediately',async()=>{
  const s=environment({interactive:false});await flush();
  assert.deepEqual(s.originals,['/original-1.png','/original-2.png']);assert.deepEqual(s.requests,['/assets/score.json']);
});
test('failed interactive data retains original score fallback rather than leaving deferred images blank',async()=>{
  const s=environment();await flush();s.canonical.reject(new Error('invalid model'));await flush();
  assert.deepEqual(s.originals,['/original-1.png','/original-2.png']);assert.match(s.document.getElementById('position').textContent,/加载失败/);
});
