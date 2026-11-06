let promptSettingsData=null,promptSettingsBusy=false,promptDrafts=new Map();
function selectedPrompt(){return promptSettingsData?.prompts.find(p=>p.key===$('#promptTemplate').value);}
function stashPromptDraft(){const key=$('#promptText').dataset.key;if(!key)return;const current=promptSettingsData?.prompts.find(p=>p.key===key);if(current&&$('#promptText').value===current.value)promptDrafts.delete(key);else promptDrafts.set(key,$('#promptText').value);}
function setPromptBusy(busy){promptSettingsBusy=busy;$$('#promptSettings button, #promptSettings select, #promptSettings textarea').forEach(n=>n.disabled=busy);}
function renderPromptEditor(){
  const p=selectedPrompt();$('#promptEditor').hidden=!p;$('#promptText').dataset.key=p?.key||'';
  if(!p)return;$('#promptText').value=promptDrafts.has(p.key)?promptDrafts.get(p.key):p.value;
  $('#promptDefault').textContent=p.default;$('#promptTemplateInfo').textContent=p.label+' · '+(p.customized?'已自定义':'使用默认模板')+' · '+p.key;
  $('#promptSourcePath').textContent='当前生效文件：'+(p.source_path||p.default_path||'');
  $('#promptDefaultPath').textContent='默认模板文件：'+(p.default_path||'');
}
function renderPromptTemplates(){
  const group=$('#promptGroup').value;$('#promptTemplateField').hidden=!group;
  $('#promptTemplate').innerHTML='<option value="">请选择模板</option>'+(promptSettingsData?.prompts||[]).filter(p=>p.group===group).map(p=>`<option value="${esc(p.key)}">${esc(p.label)}${p.customized?' · 已自定义':''}</option>`).join('');renderPromptEditor();
}
async function loadPromptSettings(){
  if(promptSettingsBusy)return;stashPromptDraft();setPromptBusy(true);$('#promptSettingsStatus').textContent='正在读取 Prompt 配置…';
  try{
    const data=await api('/api/prompt-settings');promptSettingsData=data;
    $('#promptGroup').innerHTML='<option value="">请选择阶段</option>'+[...new Set(data.prompts.map(p=>p.group))].map(g=>`<option value="${esc(g)}">${esc(g)}</option>`).join('');
    renderPromptTemplates();$('#promptSettingsStatus').textContent='已读取配置。未保存的编辑在当前页面会话中保留；请选择阶段和模板。';
  }catch(e){$('#promptSettingsStatus').textContent='读取失败：'+e.message;}
  finally{setPromptBusy(false);}
}
$('#promptGroup').addEventListener('change',()=>{stashPromptDraft();renderPromptTemplates();});
$('#promptTemplate').addEventListener('change',()=>{stashPromptDraft();renderPromptEditor();});
$('#reloadPromptsBtn').addEventListener('click',loadPromptSettings);
$('#resetPromptBtn').addEventListener('click',()=>{const p=selectedPrompt();if(!p)return;$('#promptText').value=p.default;promptDrafts.set(p.key,p.default);$('#promptSettingsStatus').textContent='默认模板已放入编辑器，点击保存后生效。';});
$('#savePromptBtn').addEventListener('click',async()=>{
  const p=selectedPrompt();if(!p||promptSettingsBusy)return;const value=$('#promptText').value;
  if(!value.trim()){$('#promptSettingsStatus').textContent='Prompt 不能为空。';return;}
  stashPromptDraft();setPromptBusy(true);$('#promptSettingsStatus').textContent='正在保存当前模板…';
  try{
    promptSettingsData=await post('/api/prompt-settings',{revision:promptSettingsData.revision,prompts:{[p.key]:value}});
    promptDrafts.delete(p.key);renderPromptEditor();
    const option=[...$('#promptTemplate').options].find(o=>o.value===p.key);const updated=selectedPrompt();if(option)option.textContent=updated.label+(updated.customized?' · 已自定义':'');
    $('#promptSettingsStatus').textContent='已保存到独立 Prompt 文件，后续任务生效。';
  }catch(e){$('#promptSettingsStatus').textContent='保存失败：'+e.message+'。编辑内容已保留。';}
  finally{setPromptBusy(false);}
});
