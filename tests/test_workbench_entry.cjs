'use strict';
const {test}=require('node:test'), assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync(require.resolve('../workbench/entry.js'),'utf8');
function environment(protocol) {
  const elements={},scripts=[];
  const document={getElementById(id){return elements[id] ||= {hidden:true,textContent:''};},createElement(){return {};},body:{append(script){scripts.push(script);}}};
  const copied=[];
  const scope={document,location:{protocol,href:protocol==='file:'?"file:///tmp/a%20b%27c/workbench/index.html":'http://127.0.0.1:8765/'},navigator:{clipboard:{async writeText(value){copied.push(value);}}},URL};
  vm.runInNewContext(source,scope);
  return {document,elements,scripts,copied};
}
test('opening a file shows the launcher and never fetches operational scripts',async()=>{
  const s=environment('file:');assert.equal(s.elements['local-entry'].hidden,false);assert.equal(s.scripts.length,0);
  assert.equal(s.elements['entry-command'].textContent,"'/tmp/a b'\\''c/workbench/start.command'");
  await s.elements['entry-copy'].onclick();assert.equal(s.copied.length,1);assert.equal(s.elements['entry-copy-status'].textContent,'已复制');
});
test('HTTP entry loads dependencies in their actual completion order',()=>{
  const s=environment('http:');assert.deepEqual(s.scripts.map(script=>script.src),['pitch_core.js']);
  for(let i=0;i<7;i++)s.scripts[i].onload();
  assert.deepEqual(s.scripts.map(script=>script.src),['pitch_core.js','pitch.js','interactive.js','console.js','metronome.js','calibration_ui.js','app.js']);
});
test('missing resource is a visible error, not an infinite loading state',()=>{
  const s=environment('http:');s.scripts[0].onerror();assert.equal(s.elements['entry-error'].hidden,false);assert.match(s.elements['entry-error'].textContent,/pitch_core.js/);assert.equal(s.scripts.length,1);
});
test('launch page styles remain local-file resolvable',()=>{
  const html=fs.readFileSync(require.resolve('../workbench/index.html'),'utf8');
  for(const link of html.matchAll(/<link[^>]*href="([^"]+)"/g))assert.ok(!link[1].startsWith('/'));
  assert.match(html,/href="http:\/\/127.0.0.1:8765\/"/);assert.match(html,/<script src="entry.js"><\/script>/);
});
