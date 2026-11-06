
function addExtraLlm(endpoint={}){
 const row=document.createElement('div');row.className='extra-llm-row card';
 row.innerHTML=`<label>API 地址<input data-url value="${esc(endpoint.base_url||'')}" placeholder="http://host:8000/v1"></label><label>模型名<input data-model value="${esc(endpoint.model||'')}"></label><label>API Key 环境变量<input data-env value="${esc(endpoint.api_key_env||'')}"></label><button class="btn" type="button" data-check>检测连接</button><button class="btn" type="button" data-remove>移除</button><span data-status class="muted"></span>`;
 row.querySelector('[data-remove]').onclick=()=>row.remove();
 row.querySelector('[data-check]').onclick=async()=>{
  const button=row.querySelector('[data-check]'),status=row.querySelector('[data-status]');button.disabled=true;status.textContent='检测中…';
  try{const result=await post('/api/model-settings/check/llm',{base_url:row.querySelector('[data-url]').value.trim(),model:row.querySelector('[data-model]').value.trim(),api_key_env:row.querySelector('[data-env]').value.trim(),clear_api_key:true});status.textContent=`检测通过 · ${result.elapsed_ms} ms`;}
  catch(e){status.textContent='检测失败：'+e.message;}finally{button.disabled=false;}
 };$('#extraLlmRows').append(row);
}
document.getElementById('addExtraLlmBtn')?.addEventListener('click',()=>{
  addExtraLlm();
  document.querySelector('#extraLlmRows .extra-llm-row:last-child input')?.focus();
});
