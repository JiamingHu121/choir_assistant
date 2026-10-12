const {test}=require('node:test');
const assert=require('node:assert/strict');
const core=require('../workbench/metronome.js');
const bar=(number,onset,duration,text,implicit=false)=>({number,onset,duration,timeSignature:{text},implicit});
test('12/8 counts four dotted-quarter pulses at 72, not twelve eighth beats',()=>{
 const model={tempos:[{onset:0,bpm:108}],measures:[bar(1,0,6,'12/8'),bar(2,6,6,'12/8')]};
 const grid=core.buildBeatGrid(model);
 assert.equal(grid.length,8);assert.deepEqual(grid.slice(0,4).map(x=>x.index),[1,2,3,4]);
 assert.equal(grid[0].bpm,72);assert.equal(grid[4].accent,true);
 assert.ok(Math.abs(grid[1].time-60/72)<1e-9);
 assert.equal(core.buildBeatGrid(model,'eighth').length,24);
});
test('a tempo change inside a bar is integrated before subsequent beat onsets',()=>{
 const model={tempos:[{onset:0,bpm:120},{onset:1.5,bpm:60}],measures:[bar(1,0,4,'4/4'),bar(2,4,3,'3/4')]};
 const grid=core.buildBeatGrid(model);
 assert.deepEqual(grid.map(x=>x.time),[0,.5,1.25,2.25,3.25,4.25,5.25]);
 assert.equal(grid[4].count,3);assert.equal(grid[4].accent,true);
});
test('pickup retains end-of-bar beat position and no artificial downbeat',()=>{
 const model={tempos:[{onset:0,bpm:60}],measures:[bar(0,0,1,'4/4',true),bar(1,1,4,'4/4')]};
 const grid=core.buildBeatGrid(model);
 assert.equal(grid[0].index,4);assert.equal(grid[0].accent,false);
 assert.equal(grid[1].time,1);assert.equal(grid[1].accent,true);
});
test('selection starts midway through a bar with global phase and half-open windows',()=>{
 const model={tempos:[{onset:0,bpm:120}],measures:[bar(1,0,4,'4/4'),bar(2,4,4,'4/4')]};
 const grid=core.buildBeatGrid(model), selected=core.beatsInWindow(grid,.75,2.5);
 assert.deepEqual(selected.map(b=>[b.time,b.index,b.accent]),[[1,3,false],[1.5,4,false],[2,1,true]]);
 assert.deepEqual(core.beatsInWindow(grid,2.5,3).map(b=>b.time),[2.5]);
});
test('audited overfull bars are flagged and never silently shift the next measure',()=>{
 const model={tempos:[{onset:0,bpm:60}],measures:[bar(1,0,5,'4/4'),bar(2,5,4,'4/4')]};
 const grid=core.buildBeatGrid(model);
 assert.equal(grid[4].overflow,true);assert.equal(grid[4].accent,false);
 assert.equal(grid[5].time,5);assert.equal(grid[5].accent,true);
});
