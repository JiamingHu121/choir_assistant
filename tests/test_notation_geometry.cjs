'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const {bindNotationPageGeometry}=require('../workbench/interactive.js');
function fixture(){
  let hidden=true;
  class Element {
    constructor(name,attributes={}){this.localName=name;this.attributes={...attributes};this.children=[];this.dataset={};}
    append(...children){for(const child of children){child.parent=this;this.children.push(child);}}
    prepend(child){child.parent=this;this.children.unshift(child);}
    remove(){this.parent.children=this.parent.children.filter(child=>child!==this);}
    querySelector(selector){return selector==='.notehead'?this.children.find(child=>child.attributes.class==='notehead'):null;}
    querySelectorAll(selector){
      if(selector===':scope > g.staff:not(.bounding-box)')return this.children.filter(child=>child.attributes.class==='staff');
      if(selector==='g.measure[data-measure-index]')return [measure];
      if(selector==='[data-note-id]')return [note];
      return this.children.filter(child=>selector.split(',').some(part=>child.attributes.class===part.trim().split('.').at(-1)));
    }
    getBBox(){return hidden?{x:0,y:0,width:0,height:0}:this.box;}
  }
  const wrapper=new Element('section'),measure=new Element('g',{class:'measure'}),staff=new Element('g',{class:'staff'});
  measure.dataset={measureIndex:'64',measureNumber:'64'};
  const top=new Element('path'),bottom=new Element('path');top.box={x:100,y:500,width:2200,height:0};bottom.box={x:100,y:1400,width:2200,height:0};staff.append(top,bottom);
  const note=new Element('g',{class:'note is-selected is-sounding'});note.dataset={noteId:'stable-last-note'};note.id='stable-last-note';
  const title=new Element('title'),head=new Element('g',{class:'notehead'});head.box={x:950,y:600,width:190,height:120};note.append(title,head);
  const lyrics=new Element('details',{class:'notation-page-lyrics'});measure.append(staff,note);wrapper.append(measure,lyrics);
  return {wrapper,measure,note,title,lyrics,markers:new Map(),create:(tag,attrs)=>new Element(tag,attrs),show(){hidden=false;}};
}
test('hidden-page zero boxes are deferred, then visible geometry restores real widths without duplicating IDs, lyrics, or hit areas',()=>{
  const s=fixture();
  assert.equal(bindNotationPageGeometry(s.wrapper,s.markers,s.create),false);
  assert.equal(s.markers.size,0);assert.equal(s.note.querySelectorAll(':scope > .notation-hit-area').length,0);
  s.show();assert.equal(bindNotationPageGeometry(s.wrapper,s.markers,s.create),true);
  assert.equal(s.markers.get(64).width,2200);
  const hit=()=>s.note.querySelectorAll(':scope > .notation-hit-area');
  assert.equal(hit().length,1);assert.equal(hit()[0].attributes.width,320);assert.equal(hit()[0].attributes.height,250);
  assert.equal(s.measure.querySelectorAll(':scope > .notation-measure-number')[0].textContent,'64');
  assert.equal(bindNotationPageGeometry(s.wrapper,s.markers,s.create),true);
  for(const cls of ['notation-measure-number','notation-measure-highlight','notation-measure-playhead'])assert.equal(s.measure.querySelectorAll(':scope > .'+cls).length,1);
  assert.equal(hit().length,1);assert.equal(s.note.id,'stable-last-note');assert.equal(s.note.dataset.noteId,'stable-last-note');
  assert.equal(s.note.attributes.class,'note is-selected is-sounding');assert.equal(s.note.children.filter(child=>child===s.title).length,1);
  assert.equal(s.wrapper.children.filter(child=>child===s.lyrics).length,1);
});
