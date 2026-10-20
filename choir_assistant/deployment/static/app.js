(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let catalog, selected = null, movement = null, opening = 0, querying = 0;
  let siteConfig={siteTitle:"合唱曲库",authRequired:false,feedbackEnabled:false}, passwordResolve=null;
  const label = {S:'女高',A:'女低',T:'男高',B:'男低',Piano:'钢琴'};
  const el = (tag, text, cls) => {const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;};
  const duration = value => {const seconds=Math.round(value);return `${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')}`;};
  const message = (text) => {$('notice').textContent=text;$('notice').hidden=!text;};
  async function api(path, data) {const response=await fetch(path,data?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}:{});const value=await response.json();if(!response.ok){const error=Error(value.error||'读取失败');error.authRequired=!!value.authenticationRequired;error.status=response.status;throw error;}return value;}
  async function refreshConfig() {
    siteConfig=await api('/api/site-config');
    document.title=selected?selected.title+' · '+siteConfig.siteTitle:siteConfig.siteTitle;
    $('home').textContent='♫ '+siteConfig.siteTitle;$('library-title').textContent=siteConfig.siteTitle;
    $('github-link').hidden=!siteConfig.githubUrl;if(siteConfig.githubUrl)$('github-link').href=siteConfig.githubUrl;
    $('feedback-open').hidden=!siteConfig.feedbackEnabled;
  }
  function askPassword(work) {
    if(passwordResolve)passwordResolve(false);
    $('password-work').textContent='《'+work.title+'》';$('work-password').value='';$('password-status').textContent='';
    if(!$('password-dialog').open)$('password-dialog').showModal();$('work-password').focus();
    return new Promise(resolve=>{passwordResolve=resolve;});
  }
  function closePassword(allowed=false) {
    $('work-password').value='';$('password-dialog').close();const resolve=passwordResolve;passwordResolve=null;if(resolve)resolve(allowed);
  }
  async function openAuthorized(work, mid) {
    try{return await api('/api/open',{work:work.id,movement:mid});}
    catch(error){if(!error.authRequired)throw error;}
    if(!await askPassword(work))return null;
    return api('/api/open',{work:work.id,movement:mid});
  }
  function downloads(work, target) {
    const types=[['pdf','原 PDF'],['musicxml','MusicXML'],['midi','MIDI'],['wav','WAV'],['model','逐音 JSON'],['review','校对报告']];
    target.replaceChildren(...types.map(([type,text])=>{const a=el('a',text+' ↗');a.href='/api/download?'+new URLSearchParams({work:work.id,movement:movement?.id||work.movements[0]?.id||'',type});return a;}));
  }
  function renderCards() {
    const search=$('search').value.trim().toLowerCase();$('cards').replaceChildren();
    for(const [index,work] of catalog.works.entries()) {
      if(search&&!`${work.title} ${work.parts.join(' ')} ${work.parts.map(p=>label[p]||p).join(' ')}`.toLowerCase().includes(search))continue;
      const card=el('article',null,'card');card.append(el('span',String(index+1).padStart(2,'0'),'number'),el('h2',work.title));
      const badges=el('div',null,'badges');for(const part of work.parts)badges.append(el('span',label[part]||part,'badge'));card.append(badges);
      card.append(el('div',work.status==='ready'?`${work.measures} 小节 · ${work.notes.toLocaleString()} 音符 · ${duration(work.duration)}\n${work.movements.length>1?work.movements.length+' 个段落':work.sourceReviewed?'全曲已对照原谱校对':'全曲结构化草稿'}`:'正在识谱与校准…','metrics'));
      card.append(el('div',work.status==='ready'?(work.sourceReviewed&&!work.blockingMeasures?'整谱校对完成 · 节奏检查通过':`原谱复核中 · ${work.blockingMeasures} 个小节有结构待审项`):'原谱、识谱和原图校对分阶段保存','warning'));
      const footer=el('div',null,'card-footer');const open=el('button',work.status==='ready'?'打开排练工作台':'处理中','open');open.disabled=work.status!=='ready';open.onclick=()=>openWork(work.id);footer.append(open);card.append(footer);$('cards').append(card);
    }
  }
  async function load(catalogRequest) {
    catalog=await (catalogRequest||api('/api/catalog'));$('total').textContent=`${catalog.totals.ready} / ${catalog.totals.works} 首 · ${catalog.totals.notes.toLocaleString()} 音符`;
    const value=$('work-select').value;$('work-select').replaceChildren(el('option','选择曲目'));$('work-select').firstChild.value='';
    for(const work of catalog.works){const option=el('option',work.title+(work.status==='ready'?'':' · 处理中'));option.value=work.id;option.disabled=work.status!=='ready';$('work-select').append(option);}
    $('work-select').value=value;renderCards();
    if(selected){const priorRevision=movement?.revision;selected=catalog.works.find(w=>w.id===selected.id);movement=selected?.movements.find(m=>m.id===movement?.id)||selected?.movements[0];if(!movement)goHome();else if(priorRevision!==movement.revision){querying++;$('data-panel').hidden=true;if(!$('workbench').hidden)await openWork(selected.id,movement.id);}}
  }
  async function openWork(id, mid) {
    const work=catalog.works.find(w=>w.id===id);if(!work||work.status!=='ready')return;
    querying++;$('data-panel').hidden=true;
    $('workbench').src='about:blank';$('workbench').hidden=true;
    const token=++opening;selected=work;movement=work.movements.find(m=>m.id===mid)||work.movements[0];
    $('work-select').value=id;$('movement-select').replaceChildren(...work.movements.map(m=>{const option=el('option',m.title);option.value=m.id;return option;}));$('movement-select').value=movement.id;$('movement-select').hidden=work.movements.length<2;
    message(`正在打开《${work.title}》并核验谱面与音频…`);
    try{const result=await openAuthorized(work,movement.id);if(token!==opening)return;if(!result){goHome();return;}$('workbench').src=result.url;$('workbench').hidden=false;$('home-view').hidden=true;message('');document.title=work.title+' · '+siteConfig.siteTitle;location.hash=id;}
    catch(error){if(token===opening)message(error.message);}
  }
  async function showData() {
    if(!selected){selected=catalog.works.find(w=>w.status==='ready');movement=selected?.movements[0];}if(!selected)return;
    if(!await openAuthorized(selected,movement.id))return;
    $('data-summary').textContent=`${selected.title} · ${selected.measures} 小节 · ${selected.notes} 音符 + ${selected.rests} 休止 · 版本 ${movement.revision}。结构检查与原谱准确率分别记录。`;
    downloads(selected,$('downloads'));$('part-filter').replaceChildren(el('option','全部声部'));$('part-filter').firstChild.value='';for(const part of selected.parts){const option=el('option',label[part]||part);option.value=part;$('part-filter').append(option);}
    $('data-panel').hidden=false;await query();
  }
  async function query() {
    if(!selected)return;const token=++querying;const params={work:selected.id,movement:movement.id};if($('part-filter').value)params.part=$('part-filter').value;if($('measure-filter').value)params.measure=$('measure-filter').value;
    try{const result=await api('/api/symbols?'+new URLSearchParams(params));if(token!==querying)return;$('query-status').textContent=`共 ${result.total} 个符号；显示 ${result.symbols.length} 个`;$('symbols').replaceChildren(...result.symbols.map(note=>{const row=el('tr');const fraction=note.rational?.duration;const q=fraction?`${fraction.numerator}/${fraction.denominator}`:note.duration;const rel=[...(note.tieTypes||[]).map(t=>'tie '+t),...(note.slurs||[]).map(s=>'slur '+s.type),...(note.notation?.tuplets||[]).map(t=>'tuplet '+t.type)].join(' · ');for(const text of [note.id,note.part,note.measure,note.kind==='rest'?'休止':`${note.pitchName||'无定音高'} (${note.pitch??'—'})`,q,`${Number(note.start||0).toFixed(2)}–${Number(note.end||0).toFixed(2)}`,rel||'—'])row.append(el('td',String(text)));return row;}));}catch(error){if(token===querying)message(error.message);}
  }
  function goHome(){opening++;querying++;selected=null;movement=null;$('work-select').value='';$('home-view').hidden=false;$('workbench').src='about:blank';$('workbench').hidden=true;$('data-panel').hidden=true;$('movement-select').hidden=true;message('');document.title=siteConfig.siteTitle;location.hash='';}
  $('password-form').onsubmit=async event=>{
    event.preventDefault();if(!selected)return;$('password-submit').disabled=true;$('password-status').textContent='正在验证…';
    try{await refreshConfig();await api('/api/login',{work:selected.id,password:$('work-password').value,csrfToken:siteConfig.csrfToken});closePassword(true);}
    catch(error){$('password-status').textContent=error.message;$('work-password').value='';$('work-password').focus();}
    finally{$('password-submit').disabled=false;}
  };
  $('password-close').onclick=()=>closePassword();$('password-dialog').addEventListener('cancel',event=>{event.preventDefault();closePassword();});
  $('feedback-open').onclick=()=>{
    $('feedback-work').replaceChildren(el('option','首页 / 整体问题'));$('feedback-work').firstChild.value='';
    for(const work of catalog.works){const option=el('option',work.title);option.value=work.id;$('feedback-work').append(option);}
    $('feedback-work').value=selected?.id||'';$('feedback-measure').value='';$('feedback-position').value='';$('feedback-status').textContent='';$('feedback-status').className='form-status';
    try{const doc=$('workbench').contentDocument;$('feedback-measure').value=doc?.getElementById('now-measure')?.textContent||'';$('feedback-position').value=doc?.getElementById('current-time')?.textContent||'';}catch(_){}
    $('feedback-dialog').showModal();$('feedback-text').focus();
  };
  $('feedback-close').onclick=()=>$('feedback-dialog').close();
  $('feedback-form').onsubmit=async event=>{
    event.preventDefault();$('feedback-submit').disabled=true;$('feedback-status').textContent='正在保存反馈…';$('feedback-status').className='form-status';
    try{await refreshConfig();const result=await api('/api/feedback',{csrfToken:siteConfig.csrfToken,text:$('feedback-text').value,work:$('feedback-work').value,movement:selected?.id===$('feedback-work').value?movement?.id||'':'',measure:$('feedback-measure').value,position:$('feedback-position').value});$('feedback-status').textContent='已保存 · 回执 '+result.receipt;$('feedback-status').className='form-status success';$('feedback-text').value='';}
    catch(error){$('feedback-status').textContent=error.message;}
    finally{$('feedback-submit').disabled=false;}
  };
  $('search').oninput=renderCards;$('reload').onclick=()=>load().catch(e=>message(e.message));$('work-select').onchange=event=>event.target.value?openWork(event.target.value):goHome();$('movement-select').onchange=event=>openWork(selected.id,event.target.value);
  $('home').onclick=goHome;
  $('data-toggle').onclick=()=>showData().catch(e=>message(e.message));$('data-close').onclick=()=>{querying++;$('data-panel').hidden=true;};$('query').onclick=query;
  if(location.protocol==='file:'){message('此导出需要 HTTP 服务；请使用独立 site_server.py 启动后打开返回的网址。');$('total').textContent='需要本地服务';return;}
  Promise.all([refreshConfig(),load(api('/api/catalog'))]).then(()=>{const id=location.hash.slice(1);if(id)openWork(id);}).catch(e=>message(e.message));
})();
