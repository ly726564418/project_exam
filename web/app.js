'use strict';
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = n => new Intl.NumberFormat('zh-CN').format(n || 0);
const bytes = n => n >= 1073741824 ? (n / 1073741824).toFixed(2) + ' GiB' : n >= 1048576 ? (n / 1048576).toFixed(1) + ' MiB' : (n / 1024).toFixed(1) + ' KiB';
const when = ms => new Date(ms).toLocaleString('zh-CN', {hour12:false});
const palettes = ['#c2db92','#92b98d','#acc9b4','#7e9e80','#d1d4b1','#7d958e','#a3af7d','#c4c8bd'];
const state = {run:'', type:null, view:'explore', live:true, types:[], history:[], objects:[], frames:[], points:[], frame:'', cursor:null, cursors:[], next:null, q:'', anomaly:false, attr:'', value:'', minimum:'', maximum:'', stats:null, map:null, loading:false, hovered:null, zoom:1, panX:0, panY:0, lastFocus:null};
let toastTimer;
function toast(message) { $('#toast').textContent = message; $('#toast').classList.remove('hidden'); clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').classList.add('hidden'), 5200); }
async function api(path, options={}) {
  const response=await fetch(path, options);
  if (!response.ok) { let msg; try {msg=(await response.json()).detail;} catch {msg='请求失败 (' + response.status + ')';} throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg)); }
  return response.json();
}
function url(path, extra={}) { const params=new URLSearchParams({run:state.run,...extra}); for (const [k,v] of [...params.entries()]) if (v === '' || v === 'null' || v === 'undefined') params.delete(k); return path+'?'+params; }
function selectedName() { return state.types.find(t => t.type_id === state.type)?.name || '全部对象'; }
function typeName(id) { return state.types.find(t => t.type_id === id)?.name || '待识别分类 #'+id; }
function topType(id) {
  const seen=new Set(); let node=state.types.find(t=>t.type_id===id), last=id;
  while(node && node.parent_id && !seen.has(node.type_id)) { seen.add(node.type_id); const parent=state.types.find(t=>t.type_id===node.parent_id); if(!parent || !parent.parent_id) return node.type_id; last=node.type_id;node=parent; }
  return last;
}
function colorFor(id) { return palettes[Math.abs(topType(id)-2)%palettes.length]; }
function pathFor(id, nodes=state.types) {const result=[],seen=new Set();let node=nodes.find(t=>t.type_id===id);while(node && !seen.has(node.type_id)){seen.add(node.type_id);result.unshift(node.name);node=nodes.find(t=>t.type_id===node.parent_id);}return result.join(' / ') || '待识别分类 #'+id;}

function renderMetrics() {
  const stats=state.stats, run=stats.runs.find(r=>r.id===state.run);
  $('#metric-objects').textContent=fmt(run?.objects || 0);
  $('#metric-types').textContent=fmt(state.types.length);
  $('#metric-rate').textContent=fmt(Math.round(stats.objects_per_second));
  $('#metric-latency').textContent=stats.ack_p95_ms.toFixed(1);
  $('#metric-run').textContent=run ? (state.run.startsWith('demo-')?'独立演示批次':'当前观测批次') : '等待首批观测数据';
  $('#metric-revision').textContent='分类树修订 r'+(run?.revision || 0);
  $('#taxonomy-version').textContent='修订 r'+(run?.revision || 0)+' · 变更可追溯';
  $('#footer-storage').textContent='已用 '+bytes(stats.disk_bytes)+' / '+bytes(stats.disk_budget_bytes);
  $('#updated-at').textContent='SYNC '+new Date().toLocaleTimeString('zh-CN',{hour12:false});
  $('#connection-label').textContent=stats.writer_error?'写入器异常':'观测网络已连接';
  $('#connection-dot').classList.toggle('error',!!stats.writer_error);
  $('#demo-label').classList.toggle('hidden',!state.run.startsWith('demo-'));
}
function renderRuns() {
  const select=$('#run-select');const items=state.stats.runs;
  const signature=items.map(r=>r.id).join('|');
  if(select.dataset.signature!==signature) {select.innerHTML=items.length?items.map(r=>`<option value="${esc(r.id)}">${esc(r.id)}</option>`).join(''):'<option value="">暂无数据</option>';select.dataset.signature=signature;}
  select.value=state.run;
}
function renderTree() {
  const needle=$('#type-search').value.trim().toLowerCase(), nodes=state.types;
  if(!nodes.length){$('#type-tree').innerHTML='<p class="empty-small">分类会随首条消息到达。</p>';return;}
  const result=[],visited=new Set();
  const totalFor=(id,seen=new Set())=>{if(seen.has(id))return 0;seen.add(id);return (nodes.find(n=>n.type_id===id)?.count || 0)+nodes.filter(n=>n.parent_id===id).reduce((s,n)=>s+totalFor(n.type_id,seen),0);};
  const walk=(node,depth)=>{
    if(visited.has(node.type_id))return;visited.add(node.type_id);
    const children=nodes.filter(n=>n.parent_id===node.type_id);
    if(!needle || node.name.toLowerCase().includes(needle))result.push(`<button class="tree-node ${state.type===node.type_id?'active':''}" style="--depth:${Math.min(depth,6)}" data-type="${node.type_id}" title="${esc(pathFor(node.type_id))}"><span class="tree-branch">${children.length?'⌄':'·'}</span><span class="tree-name">${esc(node.name)}</span><span class="tree-count">${fmt(totalFor(node.type_id))}</span></button>`);
    children.forEach(n=>walk(n,depth+1));
  };
  nodes.filter(n=>!n.parent_id || !nodes.some(p=>p.type_id===n.parent_id)).forEach(n=>walk(n,0));
  nodes.forEach(n=>{if(!visited.has(n.type_id))walk(n,0);});
  $('#type-tree').innerHTML=result.join('') || '<p class="empty-small">没有匹配的分类。</p>';
  $('#map-type-label').textContent=selectedName();$('#catalog-type-label').textContent=selectedName();
  const roots=nodes.filter(n=>nodes.some(p=>p.type_id===n.parent_id && p.parent_id===0));
  $('#type-distribution').innerHTML=roots.map(n=>({...n,total:totalFor(n.type_id)})).sort((a,b)=>b.total-a.total).slice(0,4).map(n=>`<div class="distribution-row"><span><i class="type-color" style="background:${colorFor(n.type_id)}"></i>${esc(n.name)}</span><b>${fmt(n.total)}</b></div>`).join('') || '<p class="empty-small">分类分布等待观测。</p>';
}
function renderFrames() {
  const select=$('#frame-select');
  select.innerHTML=state.frames.length?state.frames.map(f=>`<option value="${esc(f.frame)}">${esc(f.frame)} · ${esc(f.unit)}</option>`).join(''):'<option value="">暂无坐标系</option>';
  select.value=state.frame;
}
function renderRecent() {
  $('#recent-objects').innerHTML=state.objects.slice(0,6).map(o=>`<button class="recent-item" data-object="${esc(o.id)}"><span class="object-symbol" style="color:${colorFor(o.type_id)}">${o.anomaly?'◇':'✧'}</span><span class="recent-copy"><span class="recent-name">${esc(o.name)}</span><span class="recent-meta">${esc(typeName(o.type_id))} · ${o.anomaly?'异常标记':'已收录'}</span></span></button>`).join('') || '<p class="empty-small">每一个发现，都会在这里留下记录。</p>';
}
function renderCatalog() {
  $('#object-rows').innerHTML=state.objects.map(o=>`<tr data-object="${esc(o.id)}" tabindex="0" aria-label="查看 ${esc(o.name)}"><td><strong>${esc(o.name)}</strong><span class="id-text">${esc(o.id)}</span></td><td>${esc(o.type_name || typeName(o.type_id))}<span class="id-text">观测修订 r${o.tree_revision}</span></td><td class="mono">${esc(o.frame || '无空间位置')}<span class="id-text">${esc(o.unit || '未提供坐标')}</span></td><td class="mono">${esc(when(o.timestamp_ms))}</td><td><span class="status ${o.anomaly?'warning':''}">${o.anomaly?'异常标记':'已收录'}</span></td></tr>`).join('') || '<tr><td colspan="5"><div class="empty-small">暂无匹配的观测。请调整筛选条件，或接入数据源。</div></td></tr>';
  $('#previous-page').disabled=!state.cursors.length;$('#next-page').disabled=!state.next;
  $('#page-note').textContent='第 '+(state.cursors.length+1)+' 页 · '+state.objects.length+' 条观测';
  $('#catalog-selection').textContent=state.type?'当前分类与全部子分类：'+pathFor(state.type):'点击一条观测，查看完整属性与原始来源。';
}
async function loadObjects() {
  const result=await api(url('/api/objects',{type_id:state.type,q:state.q,anomaly:state.anomaly,cursor:state.cursor,attr:state.attr,value:state.value,minimum:state.minimum,maximum:state.maximum}));
  state.objects=result.items;state.next=result.next_cursor;renderCatalog();renderRecent();
}
async function loadMap() {
  state.map=await api(url('/api/map',{frame:state.frame,type_id:state.type}));state.points=state.map.points;
  $('#map-empty').classList.toggle('hidden',state.points.length>0);
  if(!state.points.length && state.stats?.objects){$('#map-empty h3').textContent='这个视野暂时没有对象';$('#map-empty p').innerHTML='切换参考系或分类，继续探索。<br>未提供坐标的对象可在档案中查看。';$('#empty-demo').classList.add('hidden');}
  else if(!state.points.length){$('#map-empty h3').textContent='观测，即将开始';$('#map-empty p').innerHTML='接入发生器，或载入演示世界，<br>探索第一批宇宙发现。';$('#empty-demo').classList.remove('hidden');}
  $('#map-frame').textContent=state.map.frame || '等待坐标数据';
  $('#map-count').textContent=fmt(state.points.length)+' 个采样点 / 本参考系 '+fmt(state.map.population)+' 个对象';
  $('#map-scale').textContent=state.map.unit?'单位 '+state.map.unit+' · 最近 2,500 条采样':'坐标单位由数据源指定';
  drawMap();
}
async function loadFields() {
  const fields=await api(url('/api/fields',{type_id:state.type})),groups=new Map();
  fields.forEach(f=>{if(!groups.has(f.path))groups.set(f.path,[]);groups.get(f.path).push(f);});
  const drift=[...groups.values()].filter(g=>g.length>1).length;
  $('#schema-summary').innerHTML=`<span class="badge">${fmt(groups.size)} 条属性路径</span><span>${fmt(drift)} 条存在类型差异</span><span>${state.type?'仅所选分类的直接对象':'整个观测批次'} · 最多返回 600 种路径与类型组合</span>`;
  $('#field-rows').innerHTML=[...groups.entries()].map(([path,rows])=>`<tr><td class="mono">${esc(path)}${rows.length>1?' <span class="badge">类型变化</span>':''}</td><td>${rows.map(r=>`<span class="kind-tag">${esc(r.kind)}</span>`).join('')}</td><td class="mono">${fmt(rows.reduce((s,r)=>s+r.count,0))}</td><td class="field-sample">${esc(rows[0].example)}</td></tr>`).join('') || '<tr><td colspan="4"><div class="empty-small">尚无属性观测。接入对象后会自动发现结构。</div></td></tr>';
}
async function loadQuality() {
  const quality=await api(url('/api/quality')),stats=state.stats,c=stats.counters,s=stats.statuses;
  const items=[['可靠接收',c.requests,'全站 HTTP 确认次数'],['整包去重',c.duplicates,'相同原始字节只保存一份'],['待处理',(s.pending||0),'已落盘、等待建立查询视图'],['隔离消息',s.quarantined||0,'全站保留原文的异常消息'],['无效对象',c.invalid_objects,'逐对象校验未通过'],['磁盘占用',bytes(stats.disk_bytes),'预算 '+bytes(stats.disk_budget_bytes)],['对象重放',c.object_duplicates,'内容相同的对象不重复计数'],['内容冲突',c.conflicts,'同 ID 内容不同，保留第一条'],['写入队列',stats.queue,'上限 128；满时返回 429']];
  $('#quality-summary').innerHTML=items.map(([label,n,note])=>`<div class="quality-stat"><strong>${typeof n==='number'?fmt(n):esc(n)}</strong>${label}<small>${esc(note)}</small></div>`).join('');
  if(stats.writer_error)$('#quality-summary').innerHTML+=`<div class="quality-stat">写入器已停止：${esc(stats.writer_error)}</div>`;
  $('#quarantine-list').innerHTML=quality.messages.map(m=>`<div class="issue-row"><div><p><span class="status warning">${m.status==='partial'?'部分异常':'已隔离'}</span> ${esc(m.error)}</p><small>MESSAGE #${m.id} · ${bytes(m.bytes)}</small></div><button class="button" data-receipt="${m.id}">查看原文 ↗</button></div>`).join('') || '<p class="empty-small" style="padding-left:20px">当前批次没有待核查消息。</p>';
  if(quality.issues.length)$('#quarantine-list').innerHTML+=quality.issues.slice(0,15).map(i=>`<div class="issue-row"><div><p>${esc(i.reason)}</p><small>${esc(i.object_id)}</small></div><button class="text-button" data-receipt="${i.inbox_id}">来源 #${i.inbox_id} ↗</button></div>`).join('');
  const ops={create:'新增',rename:'改名',move:'迁移'};
  $('#type-history').innerHTML=state.history.slice(0,20).map(h=>`<div class="history-item"><span class="revision">r${h.revision}</span><span class="history-name">${esc(h.name)}</span><span class="badge">${esc(ops[h.op] || h.op)}</span></div>`).join('') || '<p class="empty-small">等待分类演化记录。</p>';
  $('#receipt-list').innerHTML=quality.recent.map(r=>`<div class="receipt-row"><span class="mono">#${r.id}</span><span>${esc(r.kind || '未知消息')}</span><span>${bytes(r.bytes)}</span><span class="status ${['partial','quarantined'].includes(r.status)?'warning':''}">${esc({pending:'待处理',projected:'已收录',partial:'部分异常',quarantined:'已隔离'}[r.status]||r.status)}</span><button class="text-button" data-receipt="${r.id}">来源 ↗</button></div>`).join('') || '<p class="empty-small">尚无接入记录。</p>';
}
async function refresh() {
  if(state.loading)return;state.loading=true;
  try{
    state.stats=await api('/api/stats');
    if(!state.run || !state.stats.runs.some(r=>r.id===state.run))state.run=state.stats.runs[0]?.id || '';
    const [taxonomy,frames]=await Promise.all([api(url('/api/types')),api(url('/api/frames'))]);
    state.types=taxonomy.types;state.history=taxonomy.history;state.frames=frames;
    if(!frames.some(f=>f.frame===state.frame)) {state.frame=frames[0]?.frame || '';fitMap();}
    renderRuns();renderTree();renderFrames();renderMetrics();
    await loadObjects();
    if(state.view==='explore')await loadMap();
    if(state.view==='schema')await loadFields();
    if(state.view==='quality')await loadQuality();
    drawPulse();
  }catch(error){$('#connection-label').textContent='连接暂时中断';$('#connection-dot').classList.add('error');toast(error.message);}
  finally{state.loading=false;}
}
function changeView(view) {
  state.view=view;$$('.nav-item').forEach(n=>n.classList.toggle('active',n.dataset.view===view));$$('.view').forEach(v=>v.classList.toggle('active',v.id==='view-'+view));
  const titles={explore:['宇宙，正在被看见','让陌生的事物有迹可循，让变化的世界清晰可见。'],catalog:['每一次发现，都有档案','从名称、分类和属性出发，找到值得深入的一次观测。'],schema:['未知的结构，逐渐清晰','在持续变化的属性中，理解世界的共同点与差异。'],quality:['让每一份数据，值得信任','从可靠接入到原始来源，检查系统运行的每个环节。']};
  $('#page-title').innerHTML=esc(titles[view][0])+'<span class="heading-dot">。</span>';$('#page-description').textContent=titles[view][1];
  refresh();if(view==='explore')requestAnimationFrame(drawMap);
}
function resetPage(){state.cursor=null;state.cursors=[];}
async function chooseType(id){state.type=id;resetPage();fitMap();renderTree();await refresh();}
async function seedDemo(){
  const buttons=[$('#demo-button'),$('#empty-demo')];buttons.forEach(b=>b.disabled=true);toast('正在建立独立的演示世界…');
  try{const result=await api('/api/demo',{method:'POST'});state.run=result.run;state.type=null;state.frame='';resetPage();await refresh();toast('演示世界已接入，真实发生器数据使用独立批次。');setTimeout(refresh,1200);}
  catch(error){toast(error.message);}finally{buttons.forEach(b=>b.disabled=false);}
}

function canvasSize(canvas) {const rect=canvas.getBoundingClientRect(),dpr=Math.min(window.devicePixelRatio||1,2);const w=Math.round(rect.width*dpr),h=Math.round(rect.height*dpr);if(canvas.width!==w || canvas.height!==h){canvas.width=w;canvas.height=h;}const ctx=canvas.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);return {ctx,width:rect.width,height:rect.height};}
function fitMap(){state.zoom=1;state.panX=0;state.panY=0;state.hovered=null;}
let projected=[];
function drawMap(){
  const canvas=$('#universe'),{ctx,width:w,height:h}=canvasSize(canvas);if(!w || !h)return;
  ctx.clearRect(0,0,w,h);ctx.fillStyle='#101611';ctx.fillRect(0,0,w,h);
  ctx.strokeStyle='#1c281e';ctx.lineWidth=.7;
  for(let x=0;x<w;x+=44){ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,h);ctx.stroke();}for(let y=0;y<h;y+=44){ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(w,y);ctx.stroke();}
  const points=state.points;
  if(!points.length){projected=[];return;}
  let minX=Infinity,maxX=-Infinity,minY=Infinity,maxY=-Infinity;
  for(const p of points){minX=Math.min(minX,p.x);maxX=Math.max(maxX,p.x);minY=Math.min(minY,p.y);maxY=Math.max(maxY,p.y);}
  const centerX=(maxX+minX)/2,centerY=(maxY+minY)/2;
  const scale=Math.min((w-90)/Math.max(1,maxX-minX),(h-95)/Math.max(1,maxY-minY))*state.zoom;
  const px=x=>(x-centerX)*scale+w/2+state.panX,py=y=>h/2-(y-centerY)*scale+state.panY;
  const ox=px(0),oy=py(0);
  ctx.save();ctx.strokeStyle='#3a4932';ctx.lineWidth=.7;ctx.setLineDash([3,6]);ctx.beginPath();ctx.moveTo(ox,35);ctx.lineTo(ox,h-32);ctx.moveTo(15,oy);ctx.lineTo(w-15,oy);ctx.stroke();ctx.setLineDash([]);
  const radius=Math.max(maxX-minX,maxY-minY)/2;
  for(const fraction of [.25,.5,.75,1]){const r=radius*fraction*scale;if(r>0&&r<10000){ctx.strokeStyle='#263323';ctx.beginPath();ctx.arc(ox,oy,r,0,Math.PI*2);ctx.stroke();}}
  ctx.restore();projected=[];
  for(const p of points){const x=px(p.x),y=py(p.y);if(x<-10||x>w+10||y<-10||y>h+10)continue;const hover=state.hovered?.id===p.id;const r=hover?4:p.anomaly?2.6:1.55;
    ctx.globalAlpha=hover?1:p.anomaly?.95:.72;ctx.fillStyle=colorFor(p.type_id);ctx.beginPath();ctx.arc(x,y,r,0,Math.PI*2);ctx.fill();
    if(hover||p.anomaly){ctx.globalAlpha=hover?.7:.25;ctx.strokeStyle=colorFor(p.type_id);ctx.lineWidth=.8;ctx.beginPath();ctx.arc(x,y,r+4,0,Math.PI*2);ctx.stroke();}
    projected.push({...p,px:x,py:y});
  }
  ctx.globalAlpha=1;
  ctx.fillStyle='#71836e';ctx.font='8px Consolas,monospace';ctx.fillText('+Y',Math.min(w-25,Math.max(15,ox+6)),45);ctx.fillText('+X',w-30,Math.min(h-45,Math.max(50,oy-7)));
  if(ox>30&&ox<w-30&&oy>40&&oy<h-40){ctx.fillStyle='#899c7e';ctx.fillRect(ox-2,oy-2,4,4);ctx.fillText('0',ox+7,oy+13);}
}
function drawPulse(){
  const {ctx,width:w,height:h}=canvasSize($('#pulse'));if(!w||!h)return;ctx.clearRect(0,0,w,h);const series=state.stats?.series||[];
  const rates=series.slice(1).map((r,i)=>({time:r.time,rate:Math.max(0,(r.objects-series[i].objects)/Math.max(.001,r.time-series[i].time))}));
  const max=Math.max(1,...rates.map(r=>r.rate)),now=Date.now()/1000,scale=(h-12)/max;
  ctx.strokeStyle='#2c382a';ctx.lineWidth=.7;ctx.beginPath();ctx.moveTo(0,h-4);ctx.lineTo(w,h-4);ctx.stroke();
  if(!rates.length)return;const coords=rates.map(r=>({x:w-(now-r.time)/90*w,y:h-4-r.rate*scale}));ctx.strokeStyle='#adc987';ctx.lineWidth=1.3;ctx.beginPath();coords.forEach((p,i)=>i?ctx.lineTo(p.x,p.y):ctx.moveTo(p.x,p.y));ctx.stroke();
  ctx.lineTo(coords[coords.length-1].x,h-4);ctx.lineTo(coords[0].x,h-4);ctx.closePath();ctx.fillStyle='#adc98712';ctx.fill();
  ctx.fillStyle='#83957c';ctx.font='8px Consolas,monospace';ctx.fillText(fmt(Math.round(max)),2,9);
}

function showDrawer(){state.lastFocus=document.activeElement;$('#detail-drawer').classList.remove('hidden');$('#drawer-backdrop').classList.remove('hidden');document.body.style.overflow='hidden';$('#detail-drawer').focus();}
function closeDrawer(){$('#detail-drawer').classList.add('hidden');$('#drawer-backdrop').classList.add('hidden');document.body.style.overflow='';state.lastFocus?.focus();}
function attributeHtml(key,value){const kind=typeof value;return `<div class="attribute-row"><span>${esc(key)}</span>${value!==null&&kind==='object'?`<details><summary>${Array.isArray(value)?'array ['+value.length+']':'object'}</summary><pre>${esc(JSON.stringify(value,null,2))}</pre></details>`:`<b>${value===null?'<span class="muted">null</span>':esc(value)}</b>`}</div>`;}
async function openObject(id){
  showDrawer();$('#detail-content').innerHTML='<p class="drawer-empty">正在读取观测档案…</p>';
  try{
    const o=await api(url('/api/objects/'+encodeURIComponent(id))),d=o.data,p=d.position,a=d.attributes;
    $('#detail-content').innerHTML=`<h2 class="detail-title">${esc(d.name)}</h2><p class="detail-id">${esc(d.id)}</p><span class="status ${o.anomaly?'warning':''}">${o.anomaly?'带有异常标记':'已可靠收录'}</span><dl class="detail-grid"><dt>观测时间</dt><dd>${esc(when(d.timestamp_ms))}</dd><dt>当前分类</dt><dd>${esc(pathFor(d.type_id))}</dd><dt>观测时分类</dt><dd>${esc(pathFor(d.type_id,o.observed_types))} <span class="badge">r${o.tree_revision}</span></dd><dt>采集来源</dt><dd>${esc(a.source || '未提供')}</dd><dt>序列号</dt><dd class="mono">${fmt(d.sequence)}</dd><dt>空间位置</dt><dd>${p?`${esc(p.frame_id)}<br><span class="mono">X ${esc(p.x)} · Y ${esc(p.y)} · Z ${esc(p.z)} ${esc(p.unit)}</span>`:'未提供空间位置'}</dd></dl>${a.description?`<p class="detail-description">${esc(a.description)}</p>`:''}<h3 class="detail-heading">观测属性 <span class="muted">${Object.keys(a).length} 项</span></h3>${Object.entries(a).filter(([k])=>k!=='description').map(([k,v])=>attributeHtml(k,v)).join('')}<h3 class="detail-heading">数据来源</h3><div class="detail-digest">MESSAGE #${o.receipt.id}<br>SHA-256 ${esc(o.receipt.digest)}<br>接收于 ${esc(when(o.receipt.received*1000))}</div><div class="detail-actions"><button class="button" id="toggle-raw">查看对象 JSON</button><a class="button" href="/api/receipts/${o.receipt.id}?raw=true">下载原始消息 ↗</a></div><pre id="object-json" class="hidden">${esc(JSON.stringify(d,null,2))}</pre>`;
    $('#toggle-raw').onclick=()=>{$('#object-json').classList.toggle('hidden');$('#toggle-raw').textContent=$('#object-json').classList.contains('hidden')?'查看对象 JSON':'收起对象 JSON';};
  }catch(error){$('#detail-content').innerHTML=`<p class="drawer-empty">${esc(error.message)}</p>`;}
}
async function openReceipt(id){showDrawer();$('#detail-content').innerHTML='<p class="drawer-empty">正在读取原始消息…</p>';try{const r=await api('/api/receipts/'+id);$('#detail-content').innerHTML=`<h2 class="detail-title">原始消息 #${r.id}</h2><p class="detail-id">${esc(r.run)} · ${esc(r.kind || '未知类型')}</p><dl class="detail-grid"><dt>状态</dt><dd>${esc(r.status)}</dd><dt>接收时间</dt><dd>${esc(when(r.received*1000))}</dd><dt>原始大小</dt><dd>${bytes(r.bytes)}</dd><dt>处理说明</dt><dd>${esc(r.error || '已建立查询视图')}</dd></dl><div class="detail-digest">SHA-256 ${esc(r.digest)}</div><h3 class="detail-heading">原始内容</h3><pre>${esc(r.body)}</pre><a class="button" href="/api/receipts/${r.id}?raw=true">下载原始字节 ↗</a>`;}catch(error){$('#detail-content').innerHTML=`<p class="drawer-empty">${esc(error.message)}</p>`;}}

let pointer=null;
const canvas=$('#universe');
canvas.addEventListener('pointerdown',e=>{pointer={x:e.offsetX,y:e.offsetY,startX:e.offsetX,startY:e.offsetY,moved:false};canvas.setPointerCapture(e.pointerId);});
canvas.addEventListener('pointermove',e=>{
  if(pointer){const dx=e.offsetX-pointer.x,dy=e.offsetY-pointer.y;state.panX+=dx;state.panY+=dy;pointer.x=e.offsetX;pointer.y=e.offsetY;pointer.moved ||= Math.hypot(e.offsetX-pointer.startX,e.offsetY-pointer.startY)>4;drawMap();return;}
  let closest=null,distance=10;for(const p of projected){const d=Math.hypot(p.px-e.offsetX,p.py-e.offsetY);if(d<distance){distance=d;closest=p;}}
  const changed=state.hovered?.id!==closest?.id;state.hovered=closest;
  const tip=$('#map-tooltip');tip.classList.toggle('hidden',!closest);
  if(closest){tip.textContent=closest.name+' · '+typeName(closest.type_id);tip.style.left=Math.min(canvas.clientWidth-210,Math.max(10,e.offsetX+12))+'px';tip.style.top=Math.max(43,e.offsetY-37)+'px';canvas.style.cursor='pointer';}else canvas.style.cursor='crosshair';if(changed)drawMap();
});
canvas.addEventListener('pointerup',e=>{if(pointer&&!pointer.moved){let nearest=null,dist=12;for(const p of projected){const d=Math.hypot(p.px-e.offsetX,p.py-e.offsetY);if(d<dist){nearest=p;dist=d;}}if(nearest)openObject(nearest.id);}pointer=null;});
canvas.addEventListener('pointercancel',()=>pointer=null);
canvas.addEventListener('pointerleave',()=>{$('#map-tooltip').classList.add('hidden');state.hovered=null;drawMap();});
canvas.addEventListener('wheel',e=>{e.preventDefault();state.zoom=Math.max(.3,Math.min(18,state.zoom*(e.deltaY>0?.88:1.12)));drawMap();},{passive:false});
canvas.addEventListener('keydown',e=>{if(e.key==='+'||e.key==='='){state.zoom=Math.min(18,state.zoom*1.25);drawMap();}if(e.key==='-'){state.zoom=Math.max(.3,state.zoom/1.25);drawMap();}if(e.key==='0'){fitMap();drawMap();}});
new ResizeObserver(()=>{drawMap();drawPulse();}).observe($('.map-wrap'));
$('#zoom-in').onclick=()=>{state.zoom=Math.min(18,state.zoom*1.35);drawMap();};$('#zoom-out').onclick=()=>{state.zoom=Math.max(.3,state.zoom/1.35);drawMap();};$('#zoom-fit').onclick=()=>{fitMap();drawMap();};
$$('[data-view]').forEach(n=>n.onclick=()=>changeView(n.dataset.view));$('#view-all').onclick=()=>changeView('catalog');
$('#type-tree').onclick=e=>{const n=e.target.closest('[data-type]');if(n)chooseType(Number(n.dataset.type));};$('#clear-type').onclick=()=>chooseType(null);$('#type-search').oninput=renderTree;
$('#run-select').onchange=async e=>{state.run=e.target.value;state.type=null;state.frame='';resetPage();fitMap();await refresh();};
$('#frame-select').onchange=e=>{state.frame=e.target.value;fitMap();loadMap().catch(e=>toast(e.message));};
$('#demo-button').onclick=seedDemo;$('#empty-demo').onclick=seedDemo;
$('#live-toggle').onclick=()=>{state.live=!state.live;$('#live-toggle').classList.toggle('paused',!state.live);$('#live-toggle').setAttribute('aria-pressed',String(state.live));$('#live-toggle span').textContent=state.live?'实时观测':'已暂停刷新';if(state.live)refresh();};
$('#search-form').onsubmit=e=>{e.preventDefault();state.q=$('#object-search').value.trim();resetPage();loadObjects().catch(e=>toast(e.message));};
$('#anomaly-only').onchange=e=>{state.anomaly=e.target.checked;resetPage();loadObjects().catch(e=>toast(e.message));};
$('#filter-toggle').onclick=()=>$('#advanced-filters').classList.toggle('hidden');
$('#apply-filter').onclick=()=>{state.attr=$('#attr-path').value.trim();state.value=$('#attr-value').value;state.minimum=$('#attr-min').value;state.maximum=$('#attr-max').value;resetPage();loadObjects().catch(e=>toast(e.message));};
$('#clear-filter').onclick=()=>{['path','value','min','max'].forEach(k=>$('#attr-'+k).value='');state.attr=state.value=state.minimum=state.maximum='';resetPage();loadObjects().catch(e=>toast(e.message));};
$('#next-page').onclick=()=>{if(!state.next)return;state.cursors.push(state.cursor);state.cursor=state.next;loadObjects().catch(e=>toast(e.message));};$('#previous-page').onclick=()=>{state.cursor=state.cursors.pop()??null;loadObjects().catch(e=>toast(e.message));};
document.addEventListener('click',e=>{const obj=e.target.closest('[data-object]'),receipt=e.target.closest('[data-receipt]');if(obj)openObject(obj.dataset.object);if(receipt)openReceipt(receipt.dataset.receipt);});
$('#object-rows').addEventListener('keydown',e=>{if(e.key==='Enter'){const row=e.target.closest('[data-object]');if(row)openObject(row.dataset.object);}});
$('#close-drawer').onclick=closeDrawer;$('#drawer-backdrop').onclick=closeDrawer;
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!$('#detail-drawer').classList.contains('hidden'))closeDrawer();if(e.key==='Tab'&&!$('#detail-drawer').classList.contains('hidden')){const focusable=$$('#detail-drawer button, #detail-drawer a, #detail-drawer summary');if(!focusable.length)return;const first=focusable[0],last=focusable[focusable.length-1];if(e.shiftKey&&(document.activeElement===first||document.activeElement===$('#detail-drawer'))){e.preventDefault();last.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}}});

async function generatorStatus(){try{const result=await api('/api/generator/status');$('#generator-state').textContent='发生器状态：'+(result.state || result.stats?.state || '就绪')+(result.stats?.acknowledged_objects?' · 已确认 '+fmt(result.stats.acknowledged_objects)+' 对象':'');}catch(error){$('#generator-state').textContent=error.message;}}
$('#open-control').onclick=()=>{$('#gen-test-id').value='observe-'+new Date().toISOString().replace(/\D/g,'').slice(0,14);$('#control-dialog').showModal();generatorStatus();};
$('#generator-form').onsubmit=async e=>{
  e.preventDefault();$('#start-generator').disabled=true;
  const payload={workload:'objects',test_id:$('#gen-test-id').value.trim(),target_url:$('#gen-target').value,seed:42,batch_size:Number($('#gen-batch').value),workers:Number($('#gen-workers').value),objects_per_second:Number($('#gen-rate').value),rate_profile:'bursty',ramp_seconds:5,duration_seconds:Number($('#gen-duration').value),max_batches:20000,retries:2,objects:{scenario:'evolving',tree_every_batches:10}};
  try{await api('/api/generator/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});state.run=payload.test_id;state.type=null;state.frame='';resetPage();fitMap();$('#control-dialog').close();toast('发生器已启动，新观测即将到达。');setTimeout(refresh,1500);}catch(error){toast(error.message);}finally{$('#start-generator').disabled=false;}
};
$('#stop-generator').onclick=async()=>{try{await api('/api/generator/stop',{method:'POST'});toast('已请求停止，发生器正在完成收尾。');generatorStatus();}catch(error){toast(error.message);}};
setInterval(()=>{if(state.live&&!document.hidden)refresh();},3000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&state.live)refresh();});
refresh();
