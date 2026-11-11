/* ---------- live poll controller (session-based) ----------
   One session per deck run, started from the first poll slide that launches, and one
   controller per poll slide. The deck signs in anonymously once and owns the session;
   phones follow the session on the hosted page through a QR code. Works from a local file.
   Poll definitions come from one of two places, checked in this order:
   1. <script type="application/json" id="llpoll-polls"> in the deck (the html-presentations
      build): {"<id>": {"type","title"?,"sub"?,"activeTitle"?,"questions":[{"prompt","options"? |
      "scale":{"min","max","labels":[low,high]}}]}}, with the poll's element marked data-poll="<id>".
   2. <script type="application/json" class="llp-config"> inside a .slide[data-poll] (decks up to
      Live Polls Demo v1.0.1): {"title","sub","questions":[...],"activeTitle"?,"activeOption"?}. */
(function(){
const SUPABASE_URL='__SUPABASE_URL__';
const SUPABASE_PUBLISHABLE_KEY='__SUPABASE_KEY__';
const VOTE_BASE='__PAGES_BASE__';
const OFFLINE='Can’t reach the poll service. Check the internet connection, then try again.';
const SKEY='llsession:'+document.title;
const SEG_DARK=[0,3]; // segment colors that take white text
let client=null;
function db(){
 if(!client){
  if(!window.supabase||!window.supabase.createClient)throw new Error('poll library missing');
  client=window.supabase.createClient(SUPABASE_URL,SUPABASE_PUBLISHABLE_KEY,{
   auth:{persistSession:true,autoRefreshToken:true,detectSessionInUrl:false,storageKey:'llpoll-auth'}});
 }
 return client;
}
async function ensureUser(){
 const c=db();
 const s=await c.auth.getSession();
 if(s.data&&s.data.session&&s.data.session.user)return s.data.session.user;
 const r=await c.auth.signInAnonymously();
 if(r.error)throw r.error;
 return r.data.user;
}
async function rpc(fn,params){
 const r=await db().rpc(fn,params);
 if(r.error)throw new Error(r.error.message||String(r.error));
 return r.data;
}
function store(k,v){try{if(v===undefined)return JSON.parse(localStorage.getItem(k)||'null');
 if(v===null)localStorage.removeItem(k);else localStorage.setItem(k,JSON.stringify(v));}catch(e){return null;}}
const el=(tag,attrs,...kids)=>{const n=document.createElement(tag);
 for(const k in attrs||{}){if(k==='class')n.className=attrs[k];else if(k==='text')n.textContent=attrs[k];
  else if(k.startsWith('on'))n.addEventListener(k.slice(2),attrs[k]);else if(attrs[k]===false||attrs[k]==null)continue;
  else n.setAttribute(k,attrs[k]===true?'':attrs[k]);}
 for(const kid of kids)if(kid!=null)n.append(kid);return n;};
const lcfirst=t=>/^[A-Z][a-z]/.test(t)?t[0].toLowerCase()+t.slice(1):t;
const nbsp=s=>{const t=String(s||'').trim(),i=t.lastIndexOf(' ');return i>0&&t.split(' ').length>=4?t.slice(0,i)+' '+t.slice(i+1):t;};
const pct=(c,n)=>n?Math.round(c/n*100):0;
const plural=(n,w)=>n+' '+(n===1?w:w+'s');
function fill(tpl,vars){return String(tpl||'').replace(/\{(\w+)\}/g,(m,k)=>vars[k]!=null?vars[k]:m);}
// Label in place of a legend: name and value when they fit, value alone when only that fits.
function segLabel(name,p,w){const full=name+' '+Math.round(p)+'%',px=p/100*w;return px>=full.length*9.6+28?full:px>=52?Math.round(p)+'%':'';}
function fmtLeft(ms){if(ms<=0)return'0:00';const s=Math.floor(ms/1000),h=Math.floor(s/3600),m=Math.floor(s/60)%60,sec=s%60;
 return h?h+':'+String(m).padStart(2,'0')+':'+String(sec).padStart(2,'0'):m+':'+String(sec).padStart(2,'0');}

/* ---------- session controller ---------- */
const polls=[];
const session={id:null,code:null,expiresAt:0,offset:0,ended:false,starting:null,channel:null,tick:null,refresh:null};
function voteUrl(){return VOTE_BASE+'?s='+session.code;}
function timeLeft(){return session.expiresAt-(Date.now()+session.offset);}
function saveSession(){store(SKEY,session.id?{id:session.id,code:session.code,expiresAt:session.expiresAt}:null);}
async function startSession(){
 if(session.starting)return session.starting;
 session.starting=(async()=>{
  await ensureUser();
  const rows=await rpc('start_session',{p_deck_key:document.title.slice(0,200),
   p_polls:polls.map(p=>({slide_key:p.key,questions:p.config.questions}))});
  const s=rows[0];
  stopRealtime();
  session.id=s.session_id;session.code=s.code;session.expiresAt=new Date(s.expires_at).getTime();session.offset=0;session.ended=false;
  saveSession();
  polls.forEach(p=>p.ctrl.reset());
  await refreshState();
  startRealtime();
 })();
 try{await session.starting;}finally{session.starting=null;}
}
async function refreshState(){
 if(!session.id)return;
 const st=await rpc('session_state',{p_session:session.id});
 if(!st||st.state==='ended'||!st.is_owner){markEnded();return;}
 if(st.server_time)session.offset=new Date(st.server_time).getTime()-Date.now();
 session.expiresAt=new Date(st.expires_at).getTime();session.ended=false;
 const byKey={};(st.polls||[]).forEach(p=>{byKey[p.slide_key]=p;});
 polls.forEach(p=>{const row=byKey[p.key];if(row)p.ctrl.apply(row);});
}
function markEnded(){
 session.ended=true;stopRealtime();
 polls.forEach(p=>p.ctrl.paint());
}
function startRealtime(){
 stopRealtime();
 if(!session.id)return;
 const onRows=()=>{clearTimeout(session.refresh);session.refresh=setTimeout(()=>{polls.forEach(p=>{if(p.ctrl.pollId&&p.slide.classList.contains('on'))p.ctrl.loadResults().catch(()=>{});});},250);};
 session.channel=db().channel('llsession-'+session.id)
  .on('postgres_changes',{event:'*',schema:'public',table:'session_answers'},onRows)
  .on('postgres_changes',{event:'*',schema:'public',table:'session_text_entries'},onRows)
  .on('postgres_changes',{event:'*',schema:'public',table:'session_polls'},()=>{refreshState().catch(()=>{});})
  .subscribe();
 clearInterval(session.tick);
 session.tick=setInterval(()=>{
  polls.forEach(p=>p.ctrl.paintStatus());
  if(!session.ended&&timeLeft()<=0){refreshState().catch(()=>{});}
 },1000);
 // Fallback refresh in case the socket can't connect: every 5 s while the visible slide's poll is open.
 clearInterval(session.poll5);
 session.poll5=setInterval(()=>{polls.forEach(p=>{if(p.slide.classList.contains('on')&&p.ctrl.status==='open')p.ctrl.loadResults().catch(()=>{});});},5000);
}
function stopRealtime(){
 if(session.channel){try{db().removeChannel(session.channel);}catch(e){}session.channel=null;}
 clearInterval(session.tick);clearInterval(session.poll5);
}
async function resumeSession(){
 const saved=store(SKEY);
 if(!saved||!saved.id)return;
 session.id=saved.id;session.code=saved.code;session.expiresAt=saved.expiresAt||0;
 polls.forEach(p=>p.ctrl.say('Reconnecting…'));
 try{
  await ensureUser();
  await refreshState();
  if(!session.ended)startRealtime();
  polls.forEach(p=>p.ctrl.paintStatus());
 }catch(e){console.error('[poll]',e);polls.forEach(p=>p.ctrl.say(OFFLINE));}
}

/* ---------- poll definitions ---------- */
let deckPolls=null;
function readDeckPolls(){
 if(deckPolls)return deckPolls;
 const node=document.getElementById('llpoll-polls');
 try{deckPolls=node?JSON.parse(node.textContent):{};}catch(e){console.error('[poll] bad #llpoll-polls',e);deckPolls={};}
 return deckPolls;
}
// polls.json shape (type on the poll, scale {min,max,labels:[lo,hi]}) -> the per-question shape start_session validates.
function fromDeckPoll(p){
 const questions=(p.questions||[]).map(q=>{
  const o={type:p.type,prompt:q.prompt};
  if(p.type==='choice'||p.type==='ranking')o.options=q.options;
  if(p.type==='ranking'&&q.rankLabel)o.rankLabel=q.rankLabel;
  if(p.type==='rating'){const sc=q.scale||{},lb=sc.labels||[];o.min=sc.min;o.max=sc.max;o.labels={min:lb[0]||'',max:lb[1]||''};}
  if(p.type==='text'&&q.maxEntries)o.maxEntries=q.maxEntries;
  return o;
 });
 return {title:p.title,sub:p.sub,activeTitle:p.activeTitle,activeOption:p.activeOption,activeMode:p.activeMode,questions};
}
function configFor(host){
 const cfgEl=host.querySelector('script.llp-config');
 if(cfgEl){try{return JSON.parse(cfgEl.textContent);}catch(e){console.error('[poll] bad config on slide',e);return null;}}
 const p=readDeckPolls()[host.dataset.poll];
 if(!p)console.error('[poll] no definition for',host.dataset.poll);
 return p?fromDeckPoll(p):null;
}

/* ---------- slide controller ---------- */
function setup(host){
 const config=configFor(host);
 if(!config)return;
 const slide=host.closest('.slide')||host;
 const key=host.dataset.poll,Q=config.questions,n=Q.length;
 const root=host.querySelector('.llpoll')||host.appendChild(el('div',{class:'llpoll'}));
 root.classList.add('n'+Math.min(5,Math.max(1,n)));
 const ctrl={pollId:null,status:'draft',revealed:[],results:null,current:-1,busy:false,localReveal:false};
 const poll={key,slide,config,ctrl};
 polls.push(poll);

 // ----- static markup -----
 const title=el('h2',{class:'llp-title',text:config.title||'Vote on your phone'});
 const sub=el('p',{class:'llp-sub',text:config.sub||'Vote on your phone. Results stay hidden until we reveal them.'});
 const cards=el('div',{class:'llp-cards'});
 const qr=el('div',{class:'llp-qr'},el('p',{class:'llp-qrnote',text:'Start the session to show the QR code'}));
 const code=el('p',{class:'llp-code'});
 const count=el('p',{class:'llp-count',text:'Poll not launched'});
 const mainBtn=el('button',{type:'button',class:'llp-btn primary',text:'Launch poll'});
 const revealBtn=el('button',{type:'button',class:'llp-btn',text:'Reveal results',disabled:true});
 const msg=el('span',{class:'llp-msg'});
 const linkBtn=el('button',{type:'button',class:'llp-link',text:'Copy link',hidden:true});
 const newBtn=el('button',{type:'button',class:'llp-link',text:'Start a new session',hidden:true});
 root.replaceChildren(
  el('div',{class:'llp-head'},title,sub),cards,
  el('aside',{class:'llp-side'},
   el('div',{class:'llp-qrcard'},qr,el('p',{class:'llp-scan',text:'Scan to vote'}),code),
   count,el('div',{class:'llp-btns'},mainBtn,revealBtn),
   el('p',{class:'llp-status'},msg,linkBtn,newBtn)));
 const cardEls=[],barEls=[],bodyEls=[];
 Q.forEach((q,i)=>{
  const label=(n>1?(i+1)+'. ':'')+q.prompt;
  const card=el('article',{class:'llp-card q'},el('p',{class:'llp-q',text:nbsp(label)}));
  let body;
  if(q.type==='choice'||q.type==='rating'){
   const bar=el('div',{class:'llp-bar'});
   const opts=q.type==='choice'?q.options:Array.from({length:q.max-q.min+1},(_,k)=>String(q.min+k));
   opts.forEach((o,k)=>bar.append(el('div',{class:'seg c'+(k%8)},el('span'))));
   bar.append(el('em',{class:'llp-empty',text:'Results hidden'}));
   body=el('div',{class:'llp-track'},bar,el('i',{class:'llp-mid'},i===0?el('b',{text:'50%'}):null));
   barEls[i]=bar;
   card.append(body);
  }else if(q.type==='ranking'){
   body=el('div',{class:'llp-rankbox'});card.append(body);
  }else{
   body=el('div',{class:'llp-textbox'});card.append(body);
  }
  bodyEls[i]=body;cardEls[i]=card;cards.append(card);
 });

 // ----- interaction: keep poll clicks and key presses from moving the deck -----
 root.addEventListener('click',e=>{
  e.stopPropagation();
  if(e.target.closest('button'))return;
  const card=e.target.closest('.llp-card');
  if(card&&ctrl.status==='closed')toggle(cardEls.indexOf(card));
 });
 root.addEventListener('keydown',e=>{if(e.target.tagName==='BUTTON'&&(e.key===' '||e.key==='Enter'))e.stopPropagation();});
 addEventListener('keydown',e=>{
  if((e.key==='r'||e.key==='R')&&!e.metaKey&&!e.ctrlKey&&!e.altKey&&slide.classList.contains('on')&&ctrl.status==='closed'
     &&!document.body.classList.contains('grid')){revealNext();e.preventDefault();}
 });
 const say=t=>{msg.textContent=t||'';msg.classList.toggle('warn',false);};
 ctrl.say=say;

 function drawQR(){
  if(!session.code)return clearQR();
  const c=qrcode(0,'M');c.addData(voteUrl());c.make();
  qr.innerHTML=c.createSvgTag({cellSize:4,margin:2,scalable:true});
  qr.classList.add('on');
  code.replaceChildren('Or open ',el('span',{text:VOTE_BASE.replace(/^https?:\/\//,'')}),' and enter ',el('b',{text:session.code}));
 }
 function clearQR(){qr.classList.remove('on');qr.replaceChildren(el('p',{class:'llp-qrnote',text:'Start the session to show the QR code'}));code.textContent='';}

 // ----- results per type -----
 const shown=()=>new Set(ctrl.revealed);
 function paintChoiceLike(i,q,rq,vis){
  const bar=barEls[i],opts=q.type==='choice'?q.options:Array.from({length:q.max-q.min+1},(_,k)=>String(q.min+k));
  const counts=(rq&&rq.counts)||opts.map(()=>0),total=counts.reduce((a,b)=>a+b,0);
  bar.classList.toggle('shown',vis&&total>0);
  bar.querySelector('.llp-empty').textContent=shown().has(i)?'No responses yet':(ctrl.status==='closed'?'Click to reveal':'Results hidden');
  const w=root.classList.contains('revealed')?1088:672;
  bar.querySelectorAll('.seg').forEach((seg,k)=>{
   const p=vis&&total?counts[k]/total*100:0;
   seg.style.width=p+'%';
   seg.firstElementChild.textContent=vis&&total?segLabel(opts[k],p,w):'';
  });
 }
 function paintRanking(i,q,rq,vis){
  const box=bodyEls[i];
  if(!vis){
   box.replaceChildren(el('ol',{class:'llp-list'},...q.options.map((o,k)=>el('li',{},el('i',{text:String.fromCharCode(65+k)}),el('span',{text:o})))));
   return;
  }
  const avg=(rq&&rq.average_rank)||[],N=q.options.length,total=(rq&&rq.answered)||0;
  const order=q.options.map((o,k)=>({o,k,a:avg[k]==null?Infinity:Number(avg[k])})).sort((x,y)=>x.a-y.a);
  box.replaceChildren(el('div',{class:'llp-ranks'},...order.map((it,pos)=>{
   const share=isFinite(it.a)&&N>1?Math.max(8,Math.round((N-it.a)/(N-1)*100)):0;
   return el('div',{class:'llp-rank'},el('i',{text:String(pos+1)}),
    el('div',{class:'llp-rbar'},el('b',{style:'width:'+(total?share:0)+'%'}),el('span',{class:share<40?'dark':'',text:it.o,style:share<40?'left:'+'calc('+share+'% + 12px)':''})),
    el('em',{text:isFinite(it.a)&&total?'avg '+it.a.toFixed(1):'—'}));
  })));
 }
 function paintText(i,q,rq,vis){
  const box=bodyEls[i];
  if(!vis){box.replaceChildren(el('p',{class:'llp-note',text:ctrl.status==='open'?'Entries are collecting. Close the poll, then reveal them.':ctrl.status==='closed'?'Click to reveal the entries.':'Entries appear here after you reveal them.'}));return;}
  const ents=(rq&&rq.entries)||[];
  if(!ents.length){box.replaceChildren(el('p',{class:'llp-note',text:'No entries yet.'}));return;}
  box.replaceChildren(el('div',{class:'llp-wall'},...ents.map(e=>el('div',{class:'llp-chip'+(e.masked?' masked':'')+(e.hidden?' hidden-entry':''),'data-id':e.id},
   el('span',{text:e.body}),
   el('button',{type:'button','aria-label':'Hide this entry',title:'Hide this entry',text:'×',onclick:ev=>{ev.stopPropagation();hideEntry(e.id);}})))));
 }
 function activeTitle(){
  const r=ctrl.results,on=shown();
  if(!on.size){title.textContent=config.title||'Vote on your phone';sub.textContent=config.sub||'';return;}
  const total=r?r.respondents:0;
  if(!total){title.textContent='No responses yet';sub.textContent='Share the QR code or link, then reveal again.';return;}
  // The question the title talks about: the current one, or with activeMode "max" the revealed
  // choice question where the active option scores highest (v1.2.0's "most-held belief").
  let i=ctrl.current>=0&&on.has(ctrl.current)?ctrl.current:[...on].sort((a,b)=>b-a)[0];
  if(config.activeMode==='max'){let best=-1;[...on].sort().forEach(j=>{const qq=Q[j],rr=r.questions[j]||{};if(qq.type!=='choice')return;
   const c=rr.counts||[],tot=c.reduce((a,b)=>a+b,0);let k=config.activeOption;if(!(k>=0&&k<qq.options.length))k=c.indexOf(Math.max(...c));
   const p=pct(c[k]||0,tot);if(p>best){best=p;i=j;}});}
  const q=Q[i],rq=r.questions[i]||{};
  const bare=q.prompt.replace(/[.?!]$/,'');
  const vars={prompt:bare,prompt_lc:lcfirst(bare),lead:bare.split(':')[0].trim(),n:total,max:q.max,min:q.min};
  let t,s;
  if(q.type==='choice'){
   const counts=rq.counts||[],tot=counts.reduce((a,b)=>a+b,0);
   let k=config.activeOption;if(!(k>=0&&k<q.options.length))k=counts.indexOf(Math.max(...counts));
   Object.assign(vars,{pct:pct(counts[k]||0,tot),option:q.options[k]});
   t=fill(config.activeTitle||'{pct}% chose “{option}”',vars);
   s='Share of the room choosing each answer, out of '+plural(tot,'response');
  }else if(q.type==='rating'){
   Object.assign(vars,{mean:rq.mean!=null?Number(rq.mean).toFixed(1):'—'});
   t=fill(config.activeTitle||'{lead}: the room averages {mean} of {max}',vars);
   const lg=q.labels?' ('+q.min+' = '+(q.labels.min||'')+', '+q.max+' = '+(q.labels.max||'')+')':'';
   s='Share of the room choosing each rating'+lg+', out of '+plural(rq.answered||0,'response');
  }else if(q.type==='ranking'){
   const avg=rq.average_rank||[];let best=0;avg.forEach((a,k)=>{if(a!=null&&(avg[best]==null||Number(a)<Number(avg[best])))best=k;});
   Object.assign(vars,{option:q.options[best],avg:avg[best]!=null?Number(avg[best]).toFixed(1):'—'});
   t=fill(config.activeTitle||'“{option}” came first',vars);
   s='Average position across '+plural(rq.answered||0,'ranking')+' (1 = '+(q.rankLabel||'top')+')';
  }else{
   const ents=(rq.entries||[]).filter(e=>!e.hidden&&!e.masked);
   Object.assign(vars,{count:ents.length,people:rq.answered||0});
   t=fill(config.activeTitle||'{count} ideas from the room',vars);
   s=plural(rq.answered||0,'person').replace('persons','people')+' added entries. Click × on an entry to hide it from every screen.';
  }
  title.textContent=t;sub.textContent=s;
 }
 function paint(){
  const on=shown(),rev=on.size>0&&ctrl.status!=='open';
  root.classList.toggle('live',!!ctrl.pollId);
  root.classList.toggle('closed',ctrl.status==='closed');
  root.classList.toggle('revealed',rev);
  const r=ctrl.results;
  count.textContent=ctrl.pollId?(ctrl.status==='draft'?'Not launched yet':plural(r?r.respondents:0,'response')):'Poll not launched';
  Q.forEach((q,i)=>{
   const vis=rev&&on.has(i),rq=r&&r.questions?r.questions[i]:null;
   cardEls[i].classList.toggle('pending',rev&&!on.has(i));
   cardEls[i].classList.toggle('current',rev&&i===ctrl.current);
   if(q.type==='choice'||q.type==='rating')paintChoiceLike(i,q,rq,vis);
   else if(q.type==='ranking')paintRanking(i,q,rq,vis);
   else paintText(i,q,rq,vis);
  });
  activeTitle();
  // buttons
  const ended=session.ended;
  mainBtn.textContent=!session.id||session.ended?'Start session':ctrl.status==='open'?'Close poll':ctrl.status==='closed'?'Reopen':'Launch poll';
  mainBtn.classList.toggle('primary',ctrl.status!=='closed');
  mainBtn.disabled=ctrl.busy;
  if(ended)mainBtn.textContent='Start a new session';
  revealBtn.disabled=ctrl.busy||ctrl.status!=='closed';
  revealBtn.classList.toggle('primary',ctrl.status==='closed'&&!rev);
  revealBtn.textContent=!on.size?'Reveal results':(on.size<n?'Reveal next':'Hide results');
  linkBtn.hidden=!session.code||ended;
  newBtn.hidden=!session.id;
  if(session.ended){if(qr.classList.contains('on')){clearQR();qr.replaceChildren(el('p',{class:'llp-qrnote',text:'Session ended. Start a new session for a new QR\u00a0code'}));}}
  else if(session.code&&!qr.classList.contains('on'))drawQR();
  else if(!session.code)clearQR();
  paintStatus();
 }
 function paintStatus(){
  if(!session.id){return;}
  if(session.ended){msg.textContent='Session ended. Phones show "This session has ended." Start a new session for a new QR code.';msg.classList.add('warn');return;}
  const left=timeLeft();
  const st=ctrl.status==='open'?'Open':ctrl.status==='closed'?'Closed':'Ready to launch';
  msg.textContent=st+' · '+fmtLeft(left)+' left in session';
  msg.classList.toggle('warn',left<15*60*1000);
 }
 ctrl.paint=paint;ctrl.paintStatus=paintStatus;

 // ----- state from the database -----
 ctrl.apply=function(row){
  const changed=ctrl.pollId!==row.id||ctrl.status!==row.status||JSON.stringify(ctrl.revealed)!==JSON.stringify(row.revealed||[]);
  ctrl.pollId=row.id;ctrl.status=row.status;
  if(!ctrl.localReveal)ctrl.revealed=row.revealed||[];
  if(ctrl.current>=0&&!ctrl.revealed.includes(ctrl.current))ctrl.current=ctrl.revealed.length?ctrl.revealed[ctrl.revealed.length-1]:-1;
  if(changed||!ctrl.results){if(ctrl.status!=='draft')ctrl.loadResults().catch(()=>{});else{ctrl.results=null;paint();}}
  else paint();
 };
 ctrl.reset=function(){ctrl.pollId=null;ctrl.status='draft';ctrl.revealed=[];ctrl.results=null;ctrl.current=-1;ctrl.localReveal=false;clearQR();paint();};
 ctrl.loadResults=async function(){
  if(!ctrl.pollId)return;
  const r=await rpc('poll_results',{p_poll:ctrl.pollId});
  ctrl.results=r;paint();
 };

 // ----- actions -----
 async function act(fn,label){
  if(ctrl.busy)return;ctrl.busy=true;paint();
  try{await fn();}
  catch(e){console.error('[poll]',e);const m=String(e.message||e);
   if(/ended/i.test(m)){session.ended=true;markEnded();}
   else if(/library missing|Failed to fetch|NetworkError|fetch/i.test(m))say(OFFLINE);
   else say(m);}
  finally{ctrl.busy=false;paint();}
 }
 async function launch(){
  await act(async()=>{
   if(!session.id||session.ended){
    // First click on any poll slide starts the session and shows the QR code, so people can
    // scan while the facilitator explains. The next click launches the poll.
    say('Starting a session…');await startSession();say('');return;
   }
   say('Opening the poll…');
   if(!ctrl.pollId)throw new Error('This slide is not registered in the session. Start a new session.');
   await rpc('open_poll',{p_poll:ctrl.pollId});
   ctrl.localReveal=false;
   await refreshState();
   say('');
  });
 }
 async function close(){await act(async()=>{await rpc('close_poll',{p_poll:ctrl.pollId});await refreshState();});}
 async function setRevealed(list){
  ctrl.revealed=list;
  if(session.ended){ctrl.localReveal=true;paint();return;}
  await act(async()=>{await rpc('reveal_questions',{p_poll:ctrl.pollId,p_revealed:list});await ctrl.loadResults();});
 }
 function revealNext(){
  if(ctrl.status!=='closed')return;
  const on=shown();
  if(on.size>=n){ctrl.current=-1;setRevealed([]);return;}
  const next=Q.findIndex((_,i)=>!on.has(i));
  ctrl.current=next;setRevealed([...on,next].sort((a,b)=>a-b));
 }
 function toggle(i){
  if(i<0||ctrl.status!=='closed')return;
  const on=shown();
  if(on.has(i)&&ctrl.current===i){on.delete(i);ctrl.current=-1;}
  else{on.add(i);ctrl.current=i;}
  setRevealed([...on].sort((a,b)=>a-b));
 }
 async function hideEntry(id){await act(async()=>{await rpc('hide_text_entry',{p_entry:id,p_hidden:true});await ctrl.loadResults();});}
 async function copyLink(){
  const url=voteUrl();
  try{await navigator.clipboard.writeText(url);say('Link copied. Paste it into the chat.');setTimeout(paintStatus,2500);}
  catch(e){window.prompt('Copy this poll link:',url);}
 }
 mainBtn.addEventListener('click',e=>{e.currentTarget.blur();if(ctrl.busy)return;
  if(session.ended){act(async()=>{say('Starting a new session…');await startSession();say('');});return;}
  if(ctrl.status==='open')close();else launch();});
 revealBtn.addEventListener('click',e=>{e.currentTarget.blur();revealNext();});
 linkBtn.addEventListener('click',e=>{e.currentTarget.blur();copyLink();});
 newBtn.addEventListener('click',e=>{e.currentTarget.blur();
  if(!session.ended&&!confirm('Start a new session? Phones will need to scan the new QR code, and this session’s polls stay as they are.'))return;
  act(async()=>{say('Starting a new session…');await startSession();say('');});});
 paint();
}
document.querySelectorAll('#deck [data-poll]').forEach(setup);
resumeSession();
})();
