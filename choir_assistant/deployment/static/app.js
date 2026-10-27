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
    $('board-open').hidden=siteConfig.messageBoardEnabled!==true;
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
  const tutorialKey='choir-workbench-tutorial-v1';
  const tutorialSteps=[
    {title:'你的合唱排练工作台',lead:'选一首歌，从听熟自己的声部开始。曲谱、音频和练习工具都放在同一个页面。',tip:'第一次进入',steps:['在首页选择曲目，或使用页眉的曲目列表切换。','输入团内共享的曲目密码，进入排练工作台。','点左上角的曲库标题可回到首页。']},
    {title:'让曲谱跟着音乐走',lead:'播放时，谱面会标出正在唱的音。你也可以先选中需要练习的部分。',tip:'点选、框选和播放',steps:['点选音符；触屏可先选择“框选”，再拖动框住片段。','需要加选时打开“加选”，完成后关闭即可恢复单选。','在“选区”和“播放”面板中，练习选中片段、调整声部音量和速度；“原 PDF 对照”可查看原谱。']},
    {title:'零散修改，一次发布',lead:'校准会累计本次所有改动。换一个音再改，前面的修改仍在待发布清单里。',tip:'校对的三个步骤',steps:['在“所选音符校准”卡里调整半音、音值或附点，可撤销、重做和试听。','改完后，在“本次修改”卡生成全部修改的预览，核对谱面与音频。','点击“发布全部修改”更新公共曲谱。持曲目密码者都可发布；版本历史支持恢复。']},
    {title:'用你习惯的方式练习',lead:'控制台按功能分开，找到当下需要的工具即可。',tip:'更多练习与校准工具',steps:['“节拍”提供节拍器；“音准”可使用麦克风比较歌声与目标音。','自然语言修改请求有独立卡片：先确认解析出的差异，再与本次修改一起预览。','后台处理进度会显示在校对卡里；音准读数和自动识谱都需要结合实际听感、原谱核对。']},
    {title:'把想法留给合唱团',lead:'想唱什么歌，或对排练有什么新想法？到留言板匿名分享吧。',tip:'留言和问题分别放在哪里',steps:['“留言板”公开展示匿名建议、想法和想唱的歌，大家都能看到。','谱面、音频或操作出了问题，使用“问题反馈”提交给维护者。','请勿在公开留言中填写密码、联系方式或其他个人信息。']}
  ];
  let tutorialIndex=0,tutorialResolve=null;
  function tutorialSeen(){try{return localStorage.getItem(tutorialKey)==='done';}catch(_){return false;}}
  function renderTutorial(){
    const step=tutorialSteps[tutorialIndex];
    $('tutorial-progress').replaceChildren(...tutorialSteps.map((_,index)=>{const n=el('span',null,index<=tutorialIndex?'is-active':'');n.setAttribute('aria-hidden','true');return n;}));
    $('tutorial-progress').setAttribute('aria-label',`教程第 ${tutorialIndex+1} 步，共 ${tutorialSteps.length} 步`);
    const count=el('span',`${String(tutorialIndex+1).padStart(2,'0')} / ${String(tutorialSteps.length).padStart(2,'0')}`,'tutorial-step-count');
    const title=el('h2',step.title);title.id='tutorial-title';title.tabIndex=-1;
    const tip=el('div',null,'tutorial-tip'),list=el('ol');list.append(...step.steps.map(text=>el('li',text)));tip.append(el('strong',step.tip),list);
    $('tutorial-content').replaceChildren(count,title,el('p',step.lead,'tutorial-lead'),tip);
    $('tutorial-back').disabled=tutorialIndex===0;$('tutorial-next').textContent=tutorialIndex===tutorialSteps.length-1?'开始使用':'下一步';
    if($('tutorial-dialog').open)title.focus({preventScroll:true});
  }
  function openTutorial(){tutorialIndex=0;renderTutorial();if(!$('tutorial-dialog').open)$('tutorial-dialog').showModal();return new Promise(resolve=>{tutorialResolve=resolve;});}
  function closeTutorial(){try{localStorage.setItem(tutorialKey,'done');}catch(_){}$('tutorial-dialog').close();const resolve=tutorialResolve;tutorialResolve=null;if(resolve)resolve();}
  $('tutorial-open').onclick=()=>openTutorial();$('tutorial-close').onclick=closeTutorial;$('tutorial-skip').onclick=closeTutorial;
  $('tutorial-back').onclick=()=>{if(tutorialIndex>0){tutorialIndex--;renderTutorial();}};
  $('tutorial-next').onclick=()=>{if(tutorialIndex<tutorialSteps.length-1){tutorialIndex++;renderTutorial();}else closeTutorial();};
  $('tutorial-dialog').addEventListener('cancel',event=>{event.preventDefault();closeTutorial();});
  const boardCategories={suggestion:'建议与想法',song:'想唱的歌',other:'其他分享'};
  let boardEpoch=0;
  function renderBoard(items){
    const rows=Array.isArray(items)?items:[];
    if(!rows.length){const empty=el('div',null,'board-empty');empty.append(el('strong','还没有留言，来留第一条吧'),el('p','一首想唱的歌、一个关于排练的小点子，都可以。'));$('board-feed').replaceChildren(empty);return;}
    $('board-feed').replaceChildren(...rows.map(item=>{const card=el('article',null,'board-message'),heading=el('div',null,'board-message-header');const time=el('time');const date=new Date(item.created);time.textContent=Number.isFinite(date.getTime())?date.toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}):'';if(time.textContent)time.dateTime=item.created;heading.append(el('span',boardCategories[item.category]||'其他分享','board-category'),el('span','匿名团友'),time);card.append(heading,el('p',item.message));return card;}));
  }
  async function loadBoard(){const token=++boardEpoch;$('board-refresh').disabled=true;try{const data=await api('/api/message-board');if(token===boardEpoch)renderBoard(data.items);}catch(error){if(token===boardEpoch)$('board-feed').replaceChildren(el('p','留言暂时无法读取：'+error.message));}finally{if(token===boardEpoch)$('board-refresh').disabled=false;}}
  $('board-open').onclick=()=>{$('board-status').textContent='';if(!$('board-dialog').open)$('board-dialog').showModal();loadBoard();};
  $('board-close').onclick=()=>$('board-dialog').close();$('board-refresh').onclick=loadBoard;
  $('board-message').oninput=()=>{$('board-counter').textContent=`${Array.from($('board-message').value).length} / 1200`;};
  $('board-form').onsubmit=async event=>{
    event.preventDefault();if($('board-submit').disabled)return;const text=$('board-message').value.trim();if(!text){$('board-status').textContent='先写一点想分享的内容吧。';return;}
    $('board-submit').disabled=true;$('board-status').className='form-status';$('board-status').textContent='正在保存留言…';
    try{await refreshConfig();await api('/api/message-board',{csrfToken:siteConfig.csrfToken,category:$('board-category').value,message:text});$('board-message').value='';$('board-message').oninput();$('board-status').className='form-status success';$('board-status').textContent='已匿名发布，谢谢你的分享。';await loadBoard();}
    catch(error){$('board-status').textContent=error.message;}
    finally{$('board-submit').disabled=false;}
  };
  function updateHeaderHeight(){const header=document.querySelector('header');document.documentElement.style.setProperty('--site-header-height',`${Math.ceil(header.getBoundingClientRect().height)}px`);}
  if(typeof ResizeObserver==='function')new ResizeObserver(updateHeaderHeight).observe(document.querySelector('header'));else window.addEventListener('resize',updateHeaderHeight);
  updateHeaderHeight();
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
  Promise.all([refreshConfig(),load(api('/api/catalog'))]).then(async()=>{const id=location.hash.slice(1);if(!tutorialSeen())await openTutorial();if(id)await openWork(id);}).catch(e=>message(e.message));
})();
