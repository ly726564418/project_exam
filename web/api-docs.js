'use strict';
const safe=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function schemaType(schema){return schema.type||schema.anyOf?.map(schemaType).join(' | ')||schema.$ref?.split('/').pop()||'any';}
fetch('/openapi.json').then(r=>{if(!r.ok)throw new Error('接口定义暂不可用');return r.json();}).then(spec=>{
  const html=[];
  for(const [path,methods] of Object.entries(spec.paths))for(const [method,operation] of Object.entries(methods)){
    if(!['get','post','put','patch','delete'].includes(method))continue;
    const parameters=operation.parameters||[];
    html.push(`<section class="api-endpoint"><div class="api-route"><span class="kind-tag">${safe(method.toUpperCase())}</span><h2>${safe(path)}</h2></div><p class="muted">${safe(operation.summary||'')}</p>${parameters.length?`<div class="table-scroll"><table><thead><tr><th>参数</th><th>位置</th><th>类型</th><th>默认 / 约束</th></tr></thead><tbody>${parameters.map(p=>`<tr><td class="mono">${safe(p.name)} ${p.required?'<span class="badge">必需</span>':''}</td><td>${safe(p.in)}</td><td class="mono">${safe(schemaType(p.schema||{}))}</td><td class="mono">${safe(JSON.stringify(p.schema||{}))}</td></tr>`).join('')}</tbody></table></div>`:'<p class="empty-small">无查询参数。POST 请求体与业务语义见 README 和原始 OpenAPI。</p>'}<details><summary>查看响应定义</summary><pre>${safe(JSON.stringify(operation.responses,null,2))}</pre></details></section>`);
  }
  document.querySelector('#api-endpoints').innerHTML=html.join('');
}).catch(error=>{document.querySelector('#api-endpoints').textContent=error.message;});
