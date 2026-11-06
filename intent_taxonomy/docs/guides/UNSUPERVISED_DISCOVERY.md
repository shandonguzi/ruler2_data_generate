const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)]; let currentJob=null, logOffset=0, pollTimer=null, jobSelection=0, taxonomyTasks=[],taxonomyDomains=[]; let tmState={version:0,path:'',originalDomains:[],draftDomains:[],original:[],draft:[],selectedDomain:'',editingId:null,editingDomainName:null};
let stageToolMode='stage';
const titles={clusteringSettings:'聚类设置',promptSettings:'Prompt 管理',workspace:'分类与迭代',stageTools:'阶段处理',continueIteration:'继续迭代',modelSettings:'模型配置',taxonomy:'层级浏览',taxonomyManager:'层级整理',reports:'报告文件',jobs:'运行任务'};
async function api(url,opt={}){const r=await fetch(url,opt);const raw=await r.text();let data={};if(raw){try{data=JSON.parse(raw)}catch{data={error:raw}}}if(!r.ok){const detail=data.detail||data.error||r.statusText;throw new Error(Array.isArray(detail)?detail.map(item=>`${(item.loc||[]).slice(1).join('.')}: ${item.msg}`).join('；'):detail)}return data}
function post(url,obj){return api(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(obj)})}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]))}
function fmtBytes(n){if(n<1024)return n+' B';if(n<1048576)return(n/1024).toFixed(1)+' KB';return(n/1048576).toFixed(1)+' MB'}
function renderTable(el,rows,cols){if(!rows?.length){el.innerHTML='<div class="empty">暂无数据</div>';return}cols=cols||Object.keys(rows[0]);el.innerHTML='<div class="table-wrap"><table><thead><tr>'+cols.map(c=>'<th>'+esc(c)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+cols.map(c=>'<td title="'+esc(r[c])+'">'+esc(r[c])+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>'}
const subtitles={clusteringSettings:'为中文和英文语料分别配置主题特征。',promptSettings:'按阶段编辑模型指令，保存后应用于后续任务。',workspace:'从原句分析原子意图，再无监督发现分类层级。',stageTools:'选择已有流程的中间结果，单独处理并生成派生版本。',continueIteration:'从已有分类结果继续完整迭代，保留已确认归属。',modelSettings:'设置对话与向量服务，保存后同步到本地配置。',taxonomy:'选择流程中间结果，查看候选领域与任务。',taxonomyManager:'选择层级中间结果，整理后保存为派生版本。',reports:'查看和下载分类、收敛与审核报告。',jobs:'跟踪运行进度，查看实时日志与执行结果。'};
function openPanel(name){
  const navigation=name;
  if(name==='stageTools'||name==='continueIteration'){
    workflowPreviewRevision++;
    const mode=name==='continueIteration'?'iterate':'stage';
    if(stageToolMode!==mode){
      stageToolMode=mode;$('#stageAction').value='';$('#stageSource').value='';
      $('#stageToolStatus').textContent='';resetWorkflowVisual();
    }
    name='stageTools';
    const continuing=stageToolMode==='iterate';
    $('#stageToolHeading').textContent=continuing?'从已有结果继续迭代':'重跑单个阶段';
    $('#stageToolCaption').textContent=continuing?'每轮依次执行叶子匹配、未覆盖意图聚类、成员审核与细分、主题树重建和叶子实例归并；没有变化时提前结束。':'选择已有流程，只执行一个阶段并保存新版本；运行多轮完整迭代请使用「继续迭代」。';
    $('#stageHowto').innerHTML=continuing?'<div><b>1. 选择已有流程</b><span>沿用已有原子意图与分类体系。</span></div><div><b>2. 确认迭代起点</b><span>优先使用上次迭代结果，可在本页展开查看。</span></div><div><b>3. 设置轮数并运行</b><span>本次轮次重新计数，结果保存为派生版本。</span></div>':'<div><b>1. 选一次运行</b><span>选择你已处理过的数据流程。</span></div><div><b>2. 选要重做的步骤</b><span>只执行所选单阶段能力。</span></div><div><b>3. 看输入，再执行</b><span>选择上游结果，执行后创建新版本。</span></div>';
    $('label[for="stageSource"]').textContent=continuing?'② 继续迭代的起点':'③ 选择对应中间结果';
  }
  if(name==='workspace'&&$('#workflowVisual').parentElement.id==='stagePreviewMount'){
    resetWorkflowVisual();$('#workspaceVisualMount').append($('#workflowVisual'));
  }
  const b=$$('.nav button').find(x=>x.dataset.panel===navigation);
  if(!b)return;
  if(b.closest('details'))b.closest('details').open=true;
  $$('.nav button').forEach(x=>{x.classList.toggle('active',x===b);x.setAttribute('aria-current',x===b?'page':'false')});
  $$('.panel').forEach(x=>x.classList.toggle('active',x.id===name));
  $('#pageTitle').textContent=titles[navigation];document.title=titles[navigation]+' · Intent Studio';
  $('#breadcrumbPage').textContent=titles[navigation];$('#pageSubtitle').textContent=subtitles[navigation]||'';
  if(name==='clusteringSettings')return loadClusteringSettings();
  if(name==='promptSettings')return loadPromptSettings();
  if(name==='modelSettings')return loadModelSettings();
  if(name==='workspace'||navigation==='continueIteration')return Promise.allSettled([loadDiscoverySettings(),loadWorkflows()]);
  if(name==='stageTools')return loadWorkflows();
  if(name==='reports')return loadReports();
  if(name==='taxonomy')return loadTaxonomy();
  if(name==='taxonomyManager')return loadTaxonomyManager();
  if(name==='jobs')return loadJobs();
}
$$('.nav button').forEach(b=>b.addEventListener('click',()=>{if(b.dataset.stage){$('#stageAction').value=b.dataset.stage;$('#stageSource').value='';}openPanel(b.dataset.panel)}));
$$('[data-open-panel]').forEach(b=>b.addEventListener('click',()=>openPanel(b.dataset.openPanel)));
// Associate form labels without disturbing checkbox labels or existing events.
$$('.field').forEach(field=>{const control=field.querySelector('input[id], select[id], textarea[id]');const label=field.querySelector('label');if(control&&label)label.htmlFor=control.id});
async function loadStatus(){const s=await api('/api/status');$('#sideVersion').textContent=`${s.workflows?.count??0} 个独立流程`;}
async function checkLlm(){const btn=$('#checkLlmBtn');btn.disabled=true;$('#llmState').textContent='检测中…';try{const r=await api('/api/llm-check');$('#llmDot').className='dot ok';$('#llmState').textContent='vLLM Online'+(r.models?.length?' · '+r.models[0]:'')}catch(e){$('#llmDot').className='dot bad';$('#llmState').textContent='vLLM Offline'}finally{btn.disabled=false}}
$('#checkLlmBtn').addEventListener('click',checkLlm);

let discoveryReady=false;
const inputImports={
  append:{revision:0,metadata:null,path:'',controller:null,busy:false,upload:'#appendUploadInput',uploadButton:'#appendUploadBtn',run:'#runStageBtn',status:'#appendStatus'},
  disc:{revision:0,metadata:null,path:'',controller:null,busy:false,upload:'#discUploadInput',uploadButton:'#discUploadBtn',run:'#runDiscoveryBtn',status:'#discoveryStatus'},
};
function inputPath(key){let path=$('#'+key+'Input').value.trim();if(path.length>=2&&((path.startsWith('"')&&path.endsWith('"'))||(path.startsWith("'")&&path.endsWith("'"))))path=path.slice(1,-1).trim();return path}
function updateImportControls(key){
  const state=inputImports[key],data=state.metadata;
  if($(state.uploadButton))$(state.uploadButton).disabled=state.busy;
  if($('#'+key+'InspectBtn'))$('#'+key+'InspectBtn').disabled=state.busy;
  $('#'+key+'TextCol').disabled=state.busy||!data;
  $('#'+key+'Sheet').disabled=state.busy||!data?.sheets?.length;
  if(key==='disc')$(state.run).disabled=!discoveryReady||!inputReady(key);
  else updateStageControls();
  const sheetReady=Boolean(data)&&(!data.sheets?.length||$('#'+key+'Sheet').value);$('#'+key+'ColumnField').hidden=!sheetReady;$('#'+key+'InputSample').hidden=!sheetReady;$('#'+key+'TextCol').disabled=state.busy||!sheetReady;if(key==='disc')$('#discRunSettings').hidden=!inputReady(key);
}
function invalidateImport(key,message='文件已变更，请重新读取列名或导入文件。'){
  const state=inputImports[key];state.controller?.abort();state.controller=null;state.revision++;state.metadata=null;state.path='';state.busy=false;
  $('#'+key+'FilePreview').hidden=true;$('#'+key+'TextCol').innerHTML='<option value="">请先导入文件</option>';$('#'+key+'Sheet').innerHTML='<option value="">请先导入文件</option>';
  $(state.status).textContent=message;updateImportControls(key);
}
function renderInputSample(key){
  const data=inputImports[key].metadata;if(!data)return;const selected=$('#'+key+'TextCol').value;
  const table=$('#'+key+'InputSample');
  if(!data.rows.length){table.innerHTML='<div class="empty">文件只有列名，没有数据行。</div>';return}
  table.innerHTML='<div class="table-wrap"><table><thead><tr>'+data.columns.map(c=>`<th class="${c===selected?'selected-input-column':''}">${esc(c)}${c===selected?' · 文本列':''}</th>`).join('')+'</tr></thead><tbody>'+data.rows.map(row=>'<tr>'+data.columns.map(c=>`<td class="${c===selected?'selected-input-column':''}" title="${esc(row[c])}">${esc(row[c])}</td>`).join('')+'</tr>').join('')+'</tbody></table></div>';
}
function applyImportMetadata(key,data,preferredColumn='用户提问',sheetConfirmed=false){
  const state=inputImports[key];state.metadata=data;state.path=data.path;$('#'+key+'Input').value=data.path;
  $('#'+key+'FilePreview').hidden=false;$('#'+key+'FileName').textContent=data.name||data.path.split(/[\\/]/).pop();
  $('#'+key+'FileInfo').textContent=`${data.format.toUpperCase()} · ${data.columns.length} 列 · 预览 ${data.rows.length} 行`;
  $('#'+key+'SheetField').hidden=!data.sheets?.length;$('#'+key+'FilePreview').classList.toggle('csv-preview',!data.sheets?.length);
  $('#'+key+'Sheet').innerHTML=(data.sheets||[]).map(name=>`<option value="${esc(name)}">${esc(name)}</option>`).join('');$('#'+key+'Sheet').value=data.sheet||'';if(data.sheets?.length){$('#'+key+'Sheet').insertAdjacentHTML('afterbegin','<option value="">请选择工作表</option>');if(!sheetConfirmed)$('#'+key+'Sheet').value='';}
  $('#'+key+'TextCol').innerHTML='<option value="">请选择需要分类的列</option>'+data.columns.map(c=>`<option value="${esc(c)}">${esc(c)}</option>`).join('');
  $('#'+key+'TextCol').value='';
  $(state.status).textContent=!data.has_rows?'文件中没有数据行，请更换文件。':$('#'+key+'TextCol').value?'文件已就绪，可确认预览后开始运行。':'文件已读取，请选择需要分类的文本列。';
  renderInputSample(key);updateImportControls(key);$(state.status).textContent=!data.has_rows?'文件中没有数据行，请更换文件。':data.sheets?.length&&!sheetConfirmed?'文件已读取，请选择工作表。':'文件已读取，请选择用户原句所在列。';
}
async function inspectImport(key,sheet=null){
  const state=inputImports[key],path=inputPath(key),preferred=$('#'+key+'TextCol').value||'用户提问',displayName=state.path===path?state.metadata?.name:null;
  if(!path){$(state.status).textContent='请先填写文件路径，或从电脑导入 xlsx / csv。';return}
  invalidateImport(key,'正在读取工作表、列名和样例…');const revision=state.revision;state.controller=new AbortController();state.busy=true;updateImportControls(key);
  try{const data=await api('/api/input-preview?path='+encodeURIComponent(path)+(sheet!==null?'&sheet='+encodeURIComponent(sheet):''),{signal:state.controller.signal});if(revision!==state.revision)return;applyImportMetadata(key,{...data,name:displayName||data.name},preferred,true)}
  catch(e){if(e.name!=='AbortError'&&revision===state.revision){$(state.status).textContent='读取失败：'+e.message;if(key==='disc')$(state.upload).value='';}}
  finally{if(revision===state.revision){state.busy=false;state.controller=null;updateImportControls(key)}}
}
async function uploadImport(key){
  const state=inputImports[key],file=$(state.upload).files[0];if(!file){$(state.status).textContent='请先选择 xlsx 或 csv 文件。';return}
  if(!/\.(xlsx|csv|xls)$/i.test(file.name)){invalidateImport(key,'仅支持 .xlsx、.csv 或 .xls 文件。');return}
  invalidateImport(key,'正在导入文件并读取列名…');const revision=state.revision;state.controller=new AbortController();state.busy=true;updateImportControls(key);
  const fd=new FormData();fd.append('file',file);
  try{const result=await api('/api/upload',{method:'POST',body:fd,signal:state.controller.signal});if(revision!==state.revision)return;applyImportMetadata(key,{...result.preview,path:result.path,name:file.name})}
  catch(e){if(e.name!=='AbortError'&&revision===state.revision){$(state.status).textContent='导入失败：'+e.message;if(key==='disc')$(state.upload).value='';}}
  finally{if(revision===state.revision){state.busy=false;state.controller=null;updateImportControls(key)}}
}
function inputReady(key){const state=inputImports[key],column=$('#'+key+'TextCol').value;return (!state.metadata?.sheets?.length||Boolean($('#'+key+'Sheet').value))&&!state.busy&&state.metadata?.has_rows&&state.metadata.columns.includes(column)&&state.path===inputPath(key)}
for(const key of Object.keys(inputImports)){
  const state=inputImports[key];
  $(state.uploadButton)?.addEventListener('click',()=>uploadImport(key));$('#'+key+'InspectBtn')?.addEventListener('click',()=>inspectImport(key));
  $('#'+key+'Input').addEventListener('input',()=>{$(state.upload).value='';invalidateImport(key)});
  $(state.upload).addEventListener('change',()=>{if($(state.upload).files.length){$('#'+key+'Input').value='';uploadImport(key)}});
  $('#'+key+'Sheet').addEventListener('change',()=>{if(!$('#'+key+'Sheet').value){state.controller?.abort();state.revision++;state.busy=false;$('#'+key+'TextCol').value='';updateImportControls(key);return;}inspectImport(key,$('#'+key+'Sheet').value)});
  $('#'+key+'TextCol').addEventListener('change',()=>{renderInputSample(key);updateImportControls(key);$(state.status).textContent=$('#'+key+'TextCol').value?'已选择文本列：'+$('#'+key+'TextCol').value:'请选择需要分类的文本列。'});
}
async function startJob(url,payload,statusEl){try{const j=await post(url,payload);currentJob=j.job_id;if(url==='/api/jobs/classification-workflow'||url==='/api/jobs/workflow-stage'||url==='/api/jobs/scene-mapping')workflowSelectNewest=true;jobSelection++;logOffset=0;$('#terminal').textContent='';renderJobProgress(j);if(statusEl)$(statusEl).textContent=`任务 ${j.job_id} 已启动`;$$('.nav button').find(b=>b.dataset.panel==='jobs').click();pollJob()}catch(e){alert(e.message)}}

$('#cancelJobBtn').addEventListener('click',async()=>{if(!currentJob)return alert('当前没有任务');if(!confirm('确认停止当前运行任务？'))return;try{await post(`/api/jobs/${currentJob}/cancel`,{});pollJob()}catch(e){alert(e.message)}});
function forgetDeletedJobs(ids){
  if(!ids?.includes(currentJob))return;
  clearTimeout(pollTimer);jobSelection++;currentJob=null;logOffset=0;
  displayedUsageJob=null;selectedUsageStage=null;usageJobSnapshot=null;
  $('#jobProgress').hidden=true;$('#jobStageUsage').hidden=true;jobTraceViewer.reset();requestPanel.hidden=true;
  $('#jobStageList').innerHTML='';$('#terminal').textContent='任务记录已删除。';
  $('#deleteJobBtn').disabled=true;$('#cancelJobBtn').disabled=true;
}
$('#deleteJobBtn').addEventListener('click',async()=>{
  const id=currentJob;if(!id||$('#deleteJobBtn').disabled)return;
  if(!confirm('删除此任务的历史、日志和请求记录？已生成的流程文件保留。'))return;
  $('#deleteJobBtn').disabled=true;
  try{const result=await api(`/api/jobs/${encodeURIComponent(id)}`,{method:'DELETE'});forgetDeletedJobs(result.deleted_jobs);await loadJobs();if(result.cleanup_warnings?.length)alert(result.cleanup_warnings.join('\n'));}
  catch(error){alert(error.message);if(currentJob===id)$('#deleteJobBtn').disabled=false;}
});

const jobStatusNames={queued:'排队中',running:'运行中',succeeded:'已完成',failed:'运行失败',cancelled:'已取消'};
const phaseStatusNames={pending:'等待',running:'执行中',completed:'已完成',completed_with_errors:'部分失败',blocked:'条件不足',skipped:'已跳过',failed:'失败',cancelled:'已取消'};
function setProgressBar(selector,percent,indeterminate=false){
  const bar=$(selector);bar.classList.toggle('indeterminate',indeterminate);
  bar.firstElementChild.style.width=percent===null?'0%':Math.min(100,Math.max(0,percent))+'%';
  if(percent===null)bar.removeAttribute('aria-valuenow');else bar.setAttribute('aria-valuenow',Math.round(percent));
}
function formatElapsed(seconds){const n=Math.max(0,Math.floor(seconds||0));return n>=3600?`${Math.floor(n/3600)} 小时 ${Math.floor(n%3600/60)} 分`:n>=60?`${Math.floor(n/60)} 分 ${n%60} 秒`:`${n} 秒`}
let displayedUsageJob=null,selectedUsageStage=null,usageJobSnapshot=null;
function renderJobProgress(job){
  $('#jobProgress').hidden=false;requestPanel.hidden=false;
  if(displayedUsageJob!==job.job_id){displayedUsageJob=job.job_id;selectedUsageStage=null;}
  usageJobSnapshot=job;
  const p=job.progress||{},stage=p.current,active=['running','queued'].includes(job.status),percent=p.percent??null;
  $('#jobProgress').dataset.status=job.status;$('#jobProgressStatus').textContent=jobStatusNames[job.status]||job.status;
  $('#jobTokenUsage').textContent=formatTokenUsage(job.token_usage);
  $('#jobElapsed').textContent='已用时 '+formatElapsed(job.elapsed_seconds);
  $('#jobStageTitle').textContent=stage?.label||(job.status==='succeeded'?'任务执行完成':job.status==='queued'?'正在排队，等待启动':job.status==='failed'?'任务启动或执行失败':'正在准备任务');
  const round=stage?.round?`第 ${stage.round} / ${p.rounds} 轮 · `:'';
  $('#jobRoundInfo').textContent=p.total_stages?`${round}已处理 ${p.processed_stages??p.completed_stages} / ${p.total_stages} 个阶段${p.skipped_stages?' · 跳过 '+p.skipped_stages+' 个阶段':''}`:'等待执行计划上报';
  $('#jobOverallPercent').textContent=percent===null?'—':Math.floor(percent)+'%';
  $('#jobProgressEstimate').textContent=job.status==='succeeded'?'执行完成':'按阶段估算';
  setProgressBar('#jobOverallBar',percent,active&&percent===null);
  const total=stage?.total,hasCount=total!==null&&total!==undefined;
  const phasePercent=hasCount?(total>0?stage.completed/total*100:100):stage?.status==='completed'?100:null;
  $('#jobStageCount').textContent=hasCount?`${stage.completed.toLocaleString()} / ${total.toLocaleString()} ${stage.unit||'条'}${total===0?' · 无待处理项':''}`:stage?.status==='completed'?'已完成':active?'正在执行，暂无条数统计':'暂无条数统计';
  setProgressBar('#jobStageBar',phasePercent,active&&phasePercent===null);
  const detail=[];
  if(stage?.selection_scope==='first_round_all')detail.push('首轮全量匹配');
  else if(stage?.selection_scope==='unresolved_only')detail.push('重试噪声与待确认意图，含失效归属的必要重验');
  else if(stage?.selection_scope==='all')detail.push('全量分类');
  if(stage?.reused)detail.push(stage.unit==='个簇'?`复用已有簇审核 ${stage.reused} 个，计入已处理数量`:`直接复用已有归属 ${stage.reused} 条，不计入本阶段待分类数`);
  if(stage?.cached)detail.push(`复用缓存 ${stage.cached} 条`);
  if(stage?.failed)detail.push(`其中失败 ${stage.failed} 条，重跑时会重试`);
  if(job.error)detail.push(job.error);
  $('#jobStageDetail').textContent=detail.join(' · ')||(hasCount?'计数表示已处理条数；阶段结束后还会继续后续流程。':'该阶段按执行状态更新，整体百分比不代表剩余时间。');
  renderJobStageNavigation(job);
  renderStageUsage(job);
  $('#cancelJobBtn').disabled=!active;
  $('#deleteJobBtn').disabled=active||job.finished_at==null;
  $('#resumeJobBtn').disabled=!['failed','cancelled'].includes(job.status)||job.finished_at==null;
  $$('[data-job]').forEach(row=>{row.classList.toggle('selected',row.dataset.job===job.job_id);if(row.dataset.job===job.job_id){const label=row.querySelector('.job-list-status');if(label){label.className='job-list-status '+job.status;label.textContent=(jobStatusNames[job.status]||job.status)+(percent!==null?' · '+Math.floor(percent)+'%':'')}}});
}
function renderStageUsage(job){
  syncRequestTraceStage(job);
  const id=selectedUsageStage||job.progress?.current?.id,step=job.progress?.steps?.find(s=>s.id===id),usage=job.stage_usage?.[id];
  const node=$('#jobStageUsage');node.hidden=!id;if(!id)return;
  const label=step?(step.round?'第 '+step.round+' 轮 · ':'')+step.label:'未归属阶段';
  if(!usage){node.innerHTML=`<h3>${esc(label)}</h3><p class="muted">${step?.status==='pending'?'此阶段尚未开始。':step?.status==='skipped'?'此阶段已跳过，未记录阶段请求。':'该任务未记录此阶段用量，无法从任务总量推算。'}</p>`;return;}
  const n=v=>(v||0).toLocaleString(),token=v=>usage.reported_requests?n(v):usage.requests?'未上报':'0';
  node.innerHTML=`<h3>${esc(label)}</h3><div class="diagnostic-counts"><span>已返回请求 <b>${n(usage.requests)}</b></span><span>失败 <b>${n(usage.failed_requests)}</b></span><span>输入 Token <b>${token(usage.prompt_tokens)}</b></span><span>输出 Token <b>${token(usage.completion_tokens)}</b></span><span>合计 Token <b>${token(usage.total_tokens)}</b></span><span>用量未上报 <b>${n(usage.unreported_requests)}</b></span></div><p class="muted">按实际接口调用统计，含重试；正在等待响应的请求尚未计入。${usage.unreported_requests?'Token 为已上报部分，未知用量不按零估算。':'复用本地缓存不产生接口请求。'} 缓存 Token 和思考 Token 属于输入/输出的子集，不重复累加。</p>${usage.models?.length?`<table><thead><tr><th>服务 / 模型</th><th>请求 / 失败</th><th>输入 / 输出 / 合计 Token</th><th>缓存 / 思考 Token</th></tr></thead><tbody>${usage.models.map(m=>`<tr><td>${m.kind==='embedding'?'Embedding':'LLM'} · ${esc(m.model)}</td><td>${n(m.requests)} / ${n(m.failed_requests)}</td><td>${m.reported_requests?n(m.prompt_tokens)+' / '+n(m.completion_tokens)+' / '+n(m.total_tokens):'未上报'}</td><td>${m.cached_reported_requests?n(m.cached_tokens):'未上报'} / ${m.reasoning_reported_requests?n(m.reasoning_tokens):'未上报'}</td></tr>`).join('')}</tbody></table>`:''}`;
}
$('#jobStageList').addEventListener('click',event=>{const button=event.target.closest('[data-usage-stage]');if(!button||!usageJobSnapshot)return;selectedUsageStage=button.dataset.usageStage;freezeJobStageFollowing(selectedUsageStage);$$('#jobStageList [data-usage-stage]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));renderStageUsage(usageJobSnapshot);});
async function pollJob(){
  const jobId=currentJob,selection=jobSelection;if(!jobId)return;clearTimeout(pollTimer);
  try{
    const j=await api(`/api/jobs/${encodeURIComponent(jobId)}?offset=${logOffset}`);
    if(currentJob!==jobId||selection!==jobSelection)return; // A slower response from the previous selection must not replace this task.
    renderJobProgress(j);
    if(j.logs?.length){const t=$('#terminal');t.textContent+=(t.textContent?'\n':'')+j.logs.join('\n');t.scrollTop=t.scrollHeight}
    logOffset=j.next_offset??logOffset;
    if(j.status==='running'||j.status==='queued')pollTimer=setTimeout(pollJob,900);
    else{
      $('#terminal').textContent+=`\n\n[${j.status.toUpperCase()}] returncode=${j.returncode}`;
      await Promise.allSettled([loadStatus(),loadReports(),loadJobs()]);
    }
  }catch(e){if(currentJob===jobId&&selection===jobSelection){$('#jobStageDetail').textContent='进度连接暂时中断，正在重试…';pollTimer=setTimeout(pollJob,1500)}}
}
function selectJob(job){clearTimeout(pollTimer);jobSelection++;currentJob=job.job_id;logOffset=0;$('#terminal').textContent='';renderJobProgress(job);pollJob()}
function beijingTimestamp(seconds){
  if(typeof seconds!=='number'||!Number.isFinite(seconds))return null;
  const date=new Date(seconds*1000);
  if(!Number.isFinite(date.getTime()))return null;
  const parts=Object.fromEntries(new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).formatToParts(date).map(p=>[p.type,p.value]));
  const text=`${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
  return `<time datetime="${date.toISOString()}" title="北京时间（UTC+8）">${text}</time>`;
}
function jobSubmissionTime(seconds){
  const timestamp=beijingTimestamp(seconds);
  return `<small class="job-list-submitted">${timestamp?'提交：'+timestamp:'提交时间未记录'}</small>`;
}
async function loadJobs(){
  const list=await api('/api/jobs');
  $('#jobList').innerHTML=list.length?list.map(j=>`<button class="btn job-list-item ${currentJob===j.job_id?'selected':''}" data-job="${esc(j.job_id)}"><span class="job-list-main"><b>${esc(j.job_type)}</b><small>${esc(j.job_id)}</small>${jobSubmissionTime(j.created_at)}</span><span class="job-list-status ${esc(j.status)}">${esc(jobStatusNames[j.status]||j.status)}${j.progress?.percent!=null?' · '+Math.floor(j.progress.percent)+'%':''}</span></button>`).join(''):'<div class="empty">暂无任务记录</div>';
  $$('[data-job]').forEach(b=>b.addEventListener('click',()=>{const job=list.find(j=>j.job_id===b.dataset.job);if(job)selectJob(job)}));
  if(!currentJob&&list.length)selectJob(list.find(j=>['running','queued'].includes(j.status))||list[0]);
}
async function loadReports(){await loadWorkflows();renderScopedReports();}
async function loadTaxonomy(){
  const run=selectedWorkflowId,artifact=$('#taxonomyArtifact').value;$('#taxonomyBody').hidden=true;
  if(!run||!artifact)return;
  try{const t=await api(`/api/workflows/${encodeURIComponent(run)}/taxonomy/${encodeURIComponent(artifact)}`);if(run!==selectedWorkflowId||artifact!==$('#taxonomyArtifact').value)return;taxonomyTasks=t.tasks;taxonomyDomains=t.domains||[];$('#taxDomain').innerHTML='<option value="">全部 Domain</option>'+taxonomyDomains.map(d=>`<option>${esc(d.name)}</option>`).join('');$('#taxonomyBody').hidden=false;renderTax();}
  catch(e){$('#taxonomyBody').hidden=false;$('#taxTable').textContent=e.message;}
}
function renderTax(){const d=$('#taxDomain').value,q=$('#taxSearch').value.trim().toLowerCase(),parents=Object.fromEntries(taxonomyDomains.map(x=>[x.name,x.parent_domain||'']));const rows=taxonomyTasks.filter(x=>(!d||x.domain===d)&&(!q||[x.object,x.action,x.task_id,x.description,...(x.subtasks||[]),...(x.domain_aliases||[]),...(x.object_aliases||[]),...(x.action_aliases||[])].join(' ').toLowerCase().includes(q))).map(x=>({task_id:x.task_id,parent_domain:parents[x.domain]||'',domain:x.domain,object:x.object,action:x.action,subtasks:(x.subtasks||[]).join(' | '),object_aliases:(x.object_aliases||[]).join(' | '),action_aliases:(x.action_aliases||[]).join(' | '),domain_aliases:(x.domain_aliases||[]).join(' | '),description:x.description,created_in_version:x.created_in_version}));renderTable($('#taxTable'),rows,['parent_domain','domain','object','action','subtasks','object_aliases','action_aliases','domain_aliases','task_id','description','created_in_version'])}
$('#taxDomain').addEventListener('change',renderTax);$('#taxSearch').addEventListener('input',renderTax);$('#refreshTaxBtn').addEventListener('click',loadTaxonomy);
function tmClone(v){return JSON.parse(JSON.stringify(v))}
function tmList(v){return (v||'').split(/\r?\n|\|/).map(x=>x.trim()).filter(Boolean).filter((x,i,a)=>a.indexOf(x)===i)}
function tmComparable(t){return {task_id:t.task_id,domain:t.domain,object:t.object,action:t.action,description:t.description||'',positive_examples:t.positive_examples||[],boundary_notes:t.boundary_notes||[],domain_aliases:t.domain_aliases||[],object_aliases:t.object_aliases||[],action_aliases:t.action_aliases||[],subtasks:t.subtasks||[]}}
function tmDomainComparable(d){return {name:d.name,parent_domain:d.parent_domain||'',description:d.description||''}}
function tmChanges(){
  const original=new Map(tmState.original.map(x=>[x.task_id,x])),draft=new Map(tmState.draft.filter(x=>!String(x.task_id).startsWith('__new__')).map(x=>[x.task_id,x]));
  const added=tmState.draft.filter(x=>String(x.task_id).startsWith('__new__')),deleted=tmState.original.filter(x=>!draft.has(x.task_id));
  const updated=tmState.draft.filter(x=>{if(String(x.task_id).startsWith('__new__'))return false;const o=original.get(x.task_id);return o&&JSON.stringify(tmComparable(o))!==JSON.stringify(tmComparable(x))});
  const od=new Map(tmState.originalDomains.map(x=>[x.name,x])),dd=new Map(tmState.draftDomains.map(x=>[x.name,x]));
  const domainsAdded=tmState.draftDomains.filter(x=>!od.has(x.name));
  const domainsDeleted=tmState.originalDomains.filter(x=>!dd.has(x.name));
  const domainsUpdated=tmState.draftDomains.filter(x=>od.has(x.name)&&JSON.stringify(tmDomainComparable(od.get(x.name)))!==JSON.stringify(tmDomainComparable(x)));
  return {added,deleted,updated,domainsAdded,domainsDeleted,domainsUpdated,total:added.length+deleted.length+updated.length+domainsAdded.length+domainsDeleted.length+domainsUpdated.length}
}
function tmUpdateDraftBar(){const c=tmChanges(),bar=$('#tmDraftBar');if(c.total){bar.classList.add('dirty');$('#tmDraftSummary').innerHTML=`待保存 Task：<b>新增 ${c.added.length}</b> · <b>编辑 ${c.updated.length}</b> · <b>删除 ${c.deleted.length}</b>；Domain：<b>新增 ${c.domainsAdded.length}</b> · <b>编辑 ${c.domainsUpdated.length}</b> · <b>删除 ${c.domainsDeleted.length}</b> · 保存后从 v${tmState.version} 进入下一版本`;}else{bar.classList.remove('dirty');$('#tmDraftSummary').textContent='没有待保存修改'}$('#tmSaveBtn').disabled=!c.total;$('#tmDiscardBtn').disabled=!c.total}
async function loadTaxonomyManager(){
  const run=selectedWorkflowId,artifact=$('#managerArtifact').value;tmSourceContext=null;$('#taxonomyManagerBody').hidden=true;
  if(!run||!artifact)return;
  try{const t=await api(`/api/workflows/${encodeURIComponent(run)}/taxonomy/${encodeURIComponent(artifact)}`);if(run!==selectedWorkflowId||artifact!==$('#managerArtifact').value)return;tmSourceContext={run_id:run,artifact,revision:t.revision};$('#tmAddBtn').disabled=false;$('#tmAddDomainBtn').disabled=false;tmState={version:t.version,path:t.path,originalDomains:tmClone(t.domains||[]),draftDomains:tmClone(t.domains||[]),original:tmClone(t.tasks||[]),draft:tmClone(t.tasks||[]),selectedDomain:t.domains[0]?.name||'',editingId:null,editingDomainName:null};$('#tmVersionLine').textContent=`流程 ${run} · ${workflowStageLabels[artifact]} · v${t.version}`;$('#taxonomyManagerBody').hidden=false;renderTaxonomyManager();}
  catch(e){$('#taxonomyManagerBody').hidden=false;$('#tmVersionLine').textContent=e.message;$('#tmDomainList').innerHTML='';$('#tmTaskGrid').innerHTML='';$('#tmAddBtn').disabled=true;$('#tmAddDomainBtn').disabled=true;}
}
function renderTaxonomyManager(){
  const counts={};tmState.draft.forEach(t=>counts[t.domain]=(counts[t.domain]||0)+1);
  const groups=new Map();[...tmState.draftDomains].sort((a,b)=>(a.parent_domain||'').localeCompare(b.parent_domain||'zh-CN')||a.name.localeCompare(b.name,'zh-CN')).forEach(d=>{const p=d.parent_domain||'未分组';if(!groups.has(p))groups.set(p,[]);groups.get(p).push(d)});
  $('#tmDomainList').innerHTML=groups.size?[...groups.entries()].map(([parent,domains])=>`<div class="parent-domain-group"><div class="parent-domain-label">${esc(parent)}</div>${domains.map(d=>`<button type="button" class="domain-node ${d.name===tmState.selectedDomain?'active':''}" data-tm-domain="${esc(d.name)}"><div class="domain-node-row"><b>${esc(d.name)}</b><span class="count-badge">${counts[d.name]||0}</span></div><small>${esc(d.description||'')}</small></button>`).join('')}</div>`).join(''):'<div class="empty">暂无 Domain</div>';
  $$('[data-tm-domain]').forEach(b=>b.addEventListener('click',()=>{tmState.selectedDomain=b.dataset.tmDomain;renderTaxonomyManager()}));
  const d=tmState.draftDomains.find(x=>x.name===tmState.selectedDomain);$('#tmDomainTitle').textContent=tmState.selectedDomain||'未选择 Domain';const domainTasks=tmState.draft.filter(x=>x.domain===tmState.selectedDomain);$('#tmDomainMeta').textContent=d?`${d.parent_domain?d.parent_domain+' → ':''}${d.name}${d.description?' · '+d.description:''} · ${domainTasks.length} Tasks · ${new Set(domainTasks.map(x=>x.object)).size} Objects · ${new Set(domainTasks.map(x=>x.action)).size} Actions`:'';$('#tmEditDomainBtn').disabled=!d;renderTaxonomyManagerTasks();tmUpdateDraftBar()
}
function renderTaxonomyManagerTasks(){const q=$('#tmSearch').value.trim().toLowerCase(),original=new Map(tmState.original.map(x=>[x.task_id,x]));let rows=tmState.draft.filter(x=>x.domain===tmState.selectedDomain);if(q)rows=rows.filter(x=>[x.task_id,x.object,x.action,x.description,...(x.subtasks||[]),...(x.object_aliases||[]),...(x.action_aliases||[]),...(x.domain_aliases||[])].join(' ').toLowerCase().includes(q));if(!rows.length){$('#tmTaskGrid').innerHTML='<div class="manager-empty" style="grid-column:1/-1">当前 Domain 没有匹配的 Task。可以点击“新增 Task”。</div>';return}$('#tmTaskGrid').innerHTML=rows.map(x=>{const isNew=String(x.task_id).startsWith('__new__'),o=original.get(x.task_id),changed=!isNew&&o&&JSON.stringify(tmComparable(o))!==JSON.stringify(tmComparable(x));const aliases=[...(x.object_aliases||[]).map(v=>'O: '+v),...(x.action_aliases||[]).map(v=>'A: '+v),...(x.domain_aliases||[]).map(v=>'D: '+v)].slice(0,8),subs=(x.subtasks||[]).slice(0,10),skills=x.skill_refs||[];return `<div class="task-manage-card ${isNew?'new':changed?'changed':''}"><div class="task-card-head"><div class="task-doa"><span class="domain-mini">${isNew?'NEW · ':changed?'EDITED · ':''}${esc(x.task_id)}</span>${esc(x.object)} <span class="muted">/</span> ${esc(x.action)}</div><div class="task-actions"><button type="button" class="btn mini" data-tm-edit="${esc(x.task_id)}">编辑</button><button type="button" class="btn mini danger" data-tm-delete="${esc(x.task_id)}" ${skills.length?'title="先移除 skills.json 引用"':''}>删除</button></div></div>${x.description?`<div class="task-description">${esc(x.description)}</div>`:''}${subs.length?`<div class="chip-row">${subs.map(v=>`<span class="subtask-chip">子任务: ${esc(v)}</span>`).join('')}</div>`:''}<div class="chip-row">${aliases.map(v=>`<span class="chip">${esc(v)}</span>`).join('')}${skills.map(v=>`<span class="skill-ref">Skill: ${esc(v)}</span>`).join('')}</div><div class="task-footer"><span>created v${esc(x.created_in_version??'-')}</span><span>${subs.length} subtasks · ${(x.positive_examples||[]).length} examples · ${(x.boundary_notes||[]).length} boundaries</span></div></div>`}).join('');$$('[data-tm-edit]').forEach(b=>b.addEventListener('click',()=>tmOpenEditor(b.dataset.tmEdit)));$$('[data-tm-delete]').forEach(b=>b.addEventListener('click',()=>tmDeleteTask(b.dataset.tmDelete)))}
function tmOpenEditor(taskId=null){tmState.editingId=taskId;const task=taskId?tmState.draft.find(x=>x.task_id===taskId):null;$('#tmDialogTitle').textContent=task?'编辑 Task':'新增 Task';$('#tmDialogId').textContent=task?task.task_id:'保存后自动生成稳定 task_id';$('#tmEditDomain').innerHTML=tmState.draftDomains.map(d=>`<option value="${esc(d.name)}">${esc(d.parent_domain?d.parent_domain+' → ':'')}${esc(d.name)}</option>`).join('');$('#tmEditDomain').value=task?.domain||tmState.selectedDomain||tmState.draftDomains[0]?.name||'';$('#tmEditObject').value=task?.object||'';$('#tmEditAction').value=task?.action||'';$('#tmEditDescription').value=task?.description||'';$('#tmEditObjectAliases').value=(task?.object_aliases||[]).join('\n');$('#tmEditActionAliases').value=(task?.action_aliases||[]).join('\n');$('#tmEditDomainAliases').value=(task?.domain_aliases||[]).join('\n');$('#tmEditSubtasks').value=(task?.subtasks||[]).join('\n');$('#tmEditExamples').value=(task?.positive_examples||[]).join('\n');$('#tmEditBoundary').value=(task?.boundary_notes||[]).join('\n');$('#tmEditDialog').showModal()}
function tmDeleteTask(taskId){const task=tmState.draft.find(x=>x.task_id===taskId);if(!task)return;if((task.skill_refs||[]).length)return alert(`这个 Task 被 Skill 引用：${task.skill_refs.join(', ')}\n请先处理 skills.json 的 supported_task_ids，再删除。`);if(!confirm(`从草稿删除：\n${task.domain} / ${task.object} / ${task.action}\n\n保存后会创建新的流程版本。`))return;tmState.draft=tmState.draft.filter(x=>x.task_id!==taskId);renderTaxonomyManager()}
$('#tmEditForm').addEventListener('submit',e=>{e.preventDefault();const domain=$('#tmEditDomain').value.trim(),object=$('#tmEditObject').value.trim(),action=$('#tmEditAction').value.trim();if(!domain||!object||!action)return alert('Domain / Object / Action 不能为空');const duplicate=tmState.draft.find(x=>x.task_id!==tmState.editingId&&x.domain===domain&&x.object===object&&x.action===action);if(duplicate)return alert(`已经存在相同 canonical Task：${duplicate.task_id}`);let task=tmState.editingId?tmState.draft.find(x=>x.task_id===tmState.editingId):null;if(!task){task={task_id:'__new__'+Date.now().toString(36)+Math.random().toString(36).slice(2,6),created_in_version:'待保存',skill_refs:[]};tmState.draft.push(task)}Object.assign(task,{domain,object,action,description:$('#tmEditDescription').value.trim(),object_aliases:tmList($('#tmEditObjectAliases').value),action_aliases:tmList($('#tmEditActionAliases').value),domain_aliases:tmList($('#tmEditDomainAliases').value),subtasks:tmList($('#tmEditSubtasks').value),positive_examples:tmList($('#tmEditExamples').value),boundary_notes:tmList($('#tmEditBoundary').value)});tmState.selectedDomain=domain;$('#tmEditDialog').close();renderTaxonomyManager()});
function tmOpenDomainEditor(name=null){tmState.editingDomainName=name;const d=name?tmState.draftDomains.find(x=>x.name===name):null;$('#tmDomainDialogTitle').textContent=d?'编辑 Domain':'新增 Domain';$('#tmDomainParent').value=d?.parent_domain||'';$('#tmDomainName').value=d?.name||'';$('#tmDomainDescription').value=d?.description||'';$('#tmDomainDeleteBtn').style.display=d?'inline-block':'none';$('#tmDomainDialog').showModal()}
$('#tmDomainForm').addEventListener('submit',e=>{e.preventDefault();const old=tmState.editingDomainName,name=$('#tmDomainName').value.trim(),parent_domain=$('#tmDomainParent').value.trim(),description=$('#tmDomainDescription').value.trim();if(!name)return alert('Domain 不能为空');if(tmState.draftDomains.some(x=>x.name===name&&x.name!==old))return alert('已经存在同名 Domain');if(old){const d=tmState.draftDomains.find(x=>x.name===old);if(!d)return;d.name=name;d.parent_domain=parent_domain;d.description=description;if(old!==name)tmState.draft.forEach(t=>{if(t.domain===old)t.domain=name});}else tmState.draftDomains.push({name,parent_domain,description,task_count:0,object_count:0,action_count:0});tmState.selectedDomain=name;$('#tmDomainDialog').close();renderTaxonomyManager()});
$('#tmDomainDeleteBtn').addEventListener('click',()=>{const name=tmState.editingDomainName;if(!name)return;const n=tmState.draft.filter(t=>t.domain===name).length;if(n)return alert(`Domain 下还有 ${n} 个 Task，请先移动或删除这些 Task。`);if(!confirm(`删除空 Domain：${name}？`))return;tmState.draftDomains=tmState.draftDomains.filter(x=>x.name!==name);tmState.selectedDomain=tmState.draftDomains[0]?.name||'';$('#tmDomainDialog').close();renderTaxonomyManager()});
$('#tmAddBtn').addEventListener('click',()=>tmOpenEditor(null));$('#tmAddDomainBtn').addEventListener('click',()=>tmOpenDomainEditor(null));$('#tmEditDomainBtn').addEventListener('click',()=>tmOpenDomainEditor(tmState.selectedDomain));$('#tmReloadBtn').addEventListener('click',()=>{if(tmChanges().total&&!confirm('当前草稿有未保存修改，确认丢弃并重新加载所选流程的层级结果？'))return;loadTaxonomyManager()});$('#tmDiscardBtn').addEventListener('click',()=>{if(!confirm('放弃当前页面所有未保存修改？'))return;tmState.draft=tmClone(tmState.original);tmState.draftDomains=tmClone(tmState.originalDomains);renderTaxonomyManager()});$('#tmSaveBtn').addEventListener('click',async()=>{
  if(!tmSourceContext||!tmChanges().total)return;const btn=$('#tmSaveBtn');btn.disabled=true;
  try{const tasks=tmState.draft.map(task=>{const row=tmClone(task);delete row.skill_refs;if(String(row.task_id).startsWith('__new__'))delete row.created_in_version;return row;});const domains=tmState.draftDomains.map(domain=>({name:domain.name,parent_domain:domain.parent_domain||'',description:domain.description||''}));const response=await post('/api/workflows/taxonomy-edit',{...tmSourceContext,tasks,domains});const savedArtifact=response.changes.changed?'domains':tmSourceContext.artifact;selectedWorkflowId=response.run_id;resetWorkflowChoices();await loadWorkflows();$('#managerArtifact').value=savedArtifact;await loadTaxonomyManager();alert(response.changes.changed?'已保存为新的流程版本，来源结果已保留。':'没有实际变化。');}
  catch(e){alert('保存失败：'+e.message);btn.disabled=false;}
});$('#tmDialogClose').addEventListener('click',()=>$('#tmEditDialog').close());$('#tmDialogCancel').addEventListener('click',()=>$('#tmEditDialog').close());$('#tmDomainDialogClose').addEventListener('click',()=>$('#tmDomainDialog').close());$('#tmDomainCancel').addEventListener('click',()=>$('#tmDomainDialog').close());$('#tmSearch').addEventListener('input',renderTaxonomyManagerTasks);$('#tmClearSearchBtn').addEventListener('click',()=>{$('#tmSearch').value='';renderTaxonomyManagerTasks()});

async function preview(path,sheet,el,selector){if(!path){el.innerHTML='<div class="empty">没有可预览文件</div>';return}const data=await api(`/api/table-preview?path=${encodeURIComponent(path)}${sheet?'&sheet='+encodeURIComponent(sheet):''}&limit=300`);if(selector){selector.innerHTML=data.sheets.map(x=>`<option ${x===data.sheet?'selected':''}>${esc(x)}</option>`).join('');selector.onchange=()=>preview(path,selector.value,el,selector)}renderTable(el,data.rows,data.columns)}
window.addEventListener('DOMContentLoaded',()=>{openPanel('workspace');Promise.allSettled([loadStatus(),loadTaxonomy(),loadJobs(),checkLlm()])});

function renderDomainDistribution(){
  const counts=new Map();taxonomyTasks.forEach(t=>counts.set(t.domain,(counts.get(t.domain)||0)+1));
  const rows=[...counts.entries()].sort((a,b)=>b[1]-a[1]).slice(0,5),max=rows[0]?.[1]||1;
  const el=$('#domainDistribution');
  if(!rows.length){el.innerHTML='<div class="empty"><strong>分类体系尚无任务</strong><span>在「体系管理」中添加领域与任务，或从分类数据中发现新意图。</span></div>';return}
  el.innerHTML=rows.map(([name,count])=>`<button class="domain-stat" data-domain="${esc(name)}"><span class="domain-stat-head"><span>${esc(name)}</span><b>${count}<small> Tasks</small></b></span><span class="domain-stat-track"><span style="width:${count/max*100}%"></span></span></button>`).join('');
  el.querySelectorAll('[data-domain]').forEach(b=>b.addEventListener('click',async()=>{await openPanel('taxonomy');$('#taxDomain').value=b.dataset.domain;renderTax()}));
}

async function loadDiscoverySettings(){
  try{const data=await api('/api/discovery-settings');discoveryReady=data.ready;
    if(data.iteration_scope)for(const prefix of ['disc','stage'])$('#'+prefix+'IterationScope option[value=""]').textContent='使用配置文件（'+(data.iteration_scope==='unresolved_only'?'仅未确认归属的意图':'全部有效原子意图')+'）';
    $('#discoverySettings').textContent=data.ready?`Embedding：${data.model} · ${data.base_url}`:data.issues.join('；');
  }catch(e){discoveryReady=false;$('#discoverySettings').textContent=e.message}
  updateImportControls('disc');
}
$('#discoverySettingsBtn').addEventListener('click',loadDiscoverySettings);
$('#runDiscoveryBtn').addEventListener('click',()=>{
  if(!inputReady('disc')||!discoveryReady)return;
  const workers=Number($('#discWorkers').value),rawLimit=$('#discLimit').value,limit=rawLimit?Number(rawLimit):null;
  if(!Number.isInteger(workers)||workers<1||workers>32||(limit!==null&&(!Number.isInteger(limit)||limit<1)))return alert('并发数需要为 1–32 的整数，处理条数需要为正整数。');
  const iteration_rounds=$('#discIterate').checked?Number($('#discIterationRounds').value):0;
  if($('#discIterate').checked&&(!Number.isInteger(iteration_rounds)||iteration_rounds<1||iteration_rounds>10))return alert('最大迭代轮数需要为 1–10 的整数。');
  startJob('/api/jobs/classification-workflow',{input:inputPath('disc'),text_column:$('#discTextCol').value,
    sheet_name:inputImports.disc.metadata.sheet,workers,limit,review:true,iteration_rounds,iteration_scope:$('#discIterationScope').value||null},'#discoveryStatus');
});
$('#discIterate').addEventListener('change',()=>{$('#discIterationSettings').hidden=!$('#discIterate').checked;});

let modelSettingsRevision=null,modelSettingsBusy=false,modelSettingsGeneration=0;
function renderModelSettings(data){
  modelSettingsGeneration++;
  modelSettingsRevision=data.revision;
  $("#extraLlmRows").innerHTML="";for(const endpoint of data.extra_llms||[])addExtraLlm(endpoint);
  for(const name of ['llm','embedding']){
    const settings=data[name];
    $('#model'+name+'Url').value=settings.base_url;
    $('#model'+name+'Name').value=settings.model;
    $('#model'+name+'Env').value=settings.api_key_env;renderExtraRows(name,settings.extra_body||{});
    $('#checkModel'+name+'Status').textContent='';
    $('#model'+name+'Key').value='';
    $('#model'+name+'ClearKey').checked=false;
    $('#model'+name+'KeyState').textContent=settings.api_key_configured?'文件中已有 Key；为保护凭据，不回显原值。':'文件中未设置 Key。';
  }
  $('#modelThinkingMode').value=data.llm.thinking_mode||'default';$('#modelThinkingParameter').value=data.llm.thinking_parameter||'chat_template_kwargs.enable_thinking';$('#modelThinkingPathField').hidden=$('#modelThinkingMode').value==='default';
}
function setModelSettingsBusy(busy){
  modelSettingsBusy=busy;
  $$('#modelSettings input, #modelSettings select, #modelSettings textarea, #modelSettings [data-model-param-add], #modelSettings [data-model-param-remove]').forEach(node=>node.disabled=busy);
  $('#saveModelSettingsBtn').disabled=busy||!modelSettingsRevision;
  $('#reloadModelSettingsBtn').disabled=busy;
  for(const name of ['llm','embedding'])$('#checkModel'+name+'Btn').disabled=busy||!modelSettingsRevision;
}
async function loadModelSettings(){
  if(modelSettingsBusy)return;
  setModelSettingsBusy(true);$('#modelSettingsStatus').textContent='正在读取配置…';
  try{renderModelSettings(await api('/api/model-settings'));$('#modelSettingsStatus').textContent='已读取 config/config.yaml。';}
  catch(e){modelSettingsRevision=null;$('#modelSettingsStatus').textContent='读取失败：'+e.message;}
  finally{setModelSettingsBusy(false);}
}
$('#reloadModelSettingsBtn').addEventListener('click',loadModelSettings);
$('#saveModelSettingsBtn').addEventListener('click',async()=>{
  if(modelSettingsBusy||!modelSettingsRevision)return;
  const payload={revision:modelSettingsRevision,extra_llms:[...document.querySelectorAll(".extra-llm-row")].map(row=>({base_url:row.querySelector("[data-url]").value.trim(),model:row.querySelector("[data-model]").value.trim(),api_key_env:row.querySelector("[data-env]").value.trim()}))};
  try{for(const name of ['llm','embedding'])payload[name]=collectModelEndpoint(name);}
  catch(e){$('#modelSettingsStatus').textContent=e.message;return;}
  if(!payload.llm.base_url||!payload.llm.model){$('#modelSettingsStatus').textContent='请填写对话模型的 API 地址和模型名。';return;}
  if(Boolean(payload.embedding.base_url)!==Boolean(payload.embedding.model)){$('#modelSettingsStatus').textContent='Embedding 地址和模型名需要同时填写，或同时留空。';return;}
  setModelSettingsBusy(true);$('#modelSettingsStatus').textContent='正在保存…';
  try{
    renderModelSettings(await post('/api/model-settings',payload));
    $('#modelSettingsStatus').textContent='已保存到 config/config.yaml，后续任务使用新配置。';
    Promise.allSettled([loadDiscoverySettings(),loadStatus(),checkLlm()]);
  }catch(e){$('#modelSettingsStatus').textContent='保存失败：'+e.message;}
  finally{setModelSettingsBusy(false);}
});

for(const name of ['llm','embedding']){
  const button=$('#checkModel'+name+'Btn'),status=$('#checkModel'+name+'Status');
  let checkRevision=0,checking=false;
  $('#'+name+'ModelCard').addEventListener('input',()=>{checkRevision++;status.textContent='配置已修改，请重新检测。';});
  button.addEventListener('click',async()=>{
    if(modelSettingsBusy||checking)return;
    let settings;try{settings=collectModelEndpoint(name);}catch(e){status.textContent=e.message;return;}
    if(!settings.base_url||!settings.model){status.textContent='请先填写 API 地址和模型名。';return;}
    const revision=++checkRevision,generation=modelSettingsGeneration;checking=true;button.disabled=true;button.textContent='检测中…';
    status.textContent='正在使用当前填写的设置检测，无需先保存…';
    try{
      const result=await post('/api/model-settings/check/'+name,settings);
      if(revision===checkRevision&&generation===modelSettingsGeneration&&!modelSettingsBusy)status.textContent=`检测通过 · ${result.elapsed_ms} ms`+(name==='embedding'?` · ${result.dimensions} 维向量`:' · 对话接口正常')+' · '+(result.usage?.reported?`输入 ${result.usage.prompt_tokens} / 输出 ${result.usage.completion_tokens} / 合计 ${result.usage.total_tokens} Token`:'Token 用量未上报')+'（检测不会保存配置）';
    }catch(e){if(revision===checkRevision&&generation===modelSettingsGeneration&&!modelSettingsBusy)status.textContent='检测失败：'+e.message;}
    finally{checking=false;button.disabled=modelSettingsBusy||!modelSettingsRevision;button.textContent='检测连接';}
  });
}

let workflowRuns=[],workflowStageLabels={},workflowRequired={},selectedWorkflowId='',workflowLoadRevision=0,workflowPreviewRevision=0,workflowSelectNewest=false;
let workflowPreviewTables={},tmSourceContext=null;
const workflowRunSelectors=['workspaceRun','stageRun','reportsRun','taxonomyRun','managerRun'];
const artifactLabels={report:'完整过程报告',workbook:'过程审核工作簿',sentence_results:'原句与主题（JSON）',sentence_workbook:'原句与主题（Excel）'};
function workflowLabel(run){return `${run.created_at?.replace('T',' ').slice(0,19)||run.run_id} · ${run.source?'派生版本':'完整流程'} · ${({running:'运行中',completed:'已完成',completed_with_errors:'完成，含错误',failed:'失败'})[run.status]||run.status}`}
function stageCapability(run,stage){return run?.capabilities?.[stage]||{available:false,sources:[],reason:'尚未取得文件校验结果，请刷新流程'};}
function briefReason(reason){return reason.length>70?reason.slice(0,70)+'…':reason;}
function renderStageRunOptions(){
  const select=$('#stageRun'),continuing=stageToolMode==='iterate';
  select.innerHTML='<option value="">① 请选择流程</option>'+workflowRuns.map(run=>{
    const capability=stageCapability(run,'iterate'),count=Object.entries(run.capabilities||{}).filter(([key,value])=>key!=='iterate'&&value.available).length;
    const disabled=continuing&&!capability.available;
    const suffix=continuing?(disabled?'不可继续迭代：'+briefReason(capability.reason):'可继续迭代'):`可用 ${count} 项单阶段能力`;
    return `<option value="${esc(run.run_id)}" ${disabled?'disabled':''} title="${esc(disabled?capability.reason:suffix)}">${esc(workflowLabel(run)+' · '+suffix)}</option>`;
  }).join('');
  select.value=selectedWorkflowId;
  // A globally selected workflow may become unusable for this target; never retain a disabled input.
  if(continuing&&selectedWorkflowId&&!stageCapability(selectedWorkflow(),'iterate').available)select.value='';
}
function selectedWorkflow(){return workflowRuns.find(run=>run.run_id===selectedWorkflowId)}
function chooseOptions(node,items,placeholder){const previous=node.value;node.innerHTML=`<option value="">${placeholder}</option>`+items.map(([value,label])=>`<option value="${esc(value)}">${esc(label)}</option>`).join('');node.value=items.some(([value])=>value===previous)?previous:'';}
function resetWorkflowChoices(){
  resetWorkflowDiagnostics();
  resetWorkflowVisual();
  workflowPreviewRevision++;workflowPreviewTables={};tmSourceContext=null;$('#stageToolStatus').textContent='';
  for(const id of ['workspaceArtifact','workspaceResultTable','stageAction','stageSource','taxonomyArtifact','managerArtifact','reportArtifact'])$('#'+id).value='';
  for(const id of ['workspaceTableStep','workspaceResult','taxonomyBody','taxonomyManagerBody','reportList'])$('#'+id).hidden=true;
}
function artifactsFor(run,required=null){
  if(!run||(required&&!(run.completed_stages||[]).includes(required)))return [];
  const order=Object.keys(workflowStageLabels);
  return Object.keys(run.artifacts||{}).filter(key=>workflowStageLabels[key]&&(!required||key==='export'||order.indexOf(key)>=order.indexOf(required)));
}
async function loadWorkflows(){
  const revision=++workflowLoadRevision,data=await api('/api/workflows');if(revision!==workflowLoadRevision)return;
  workflowRuns=data.runs;workflowStageLabels=data.stages;workflowRequired=data.required;
  if(workflowSelectNewest&&workflowRuns[0]&&workflowRuns[0].run_id!==selectedWorkflowId){selectedWorkflowId=workflowRuns[0].run_id;workflowSelectNewest=false;resetWorkflowChoices();}
  if(selectedWorkflowId&&!workflowRuns.some(run=>run.run_id===selectedWorkflowId)){selectedWorkflowId='';resetWorkflowChoices();}
  renderWorkflowChoices();
}
function renderWorkflowChoices(){
  const run=selectedWorkflow();
  const sentenceXlsx=run?.artifacts?.sentence_workbook,sentenceJson=run?.artifacts?.sentence_results;
  $('#sentenceDownloads').hidden=!sentenceXlsx&&!sentenceJson;
  for(const [id,path] of [['sentenceDownloadXlsx',sentenceXlsx],['sentenceDownloadJson',sentenceJson]]){const link=$('#'+id);link.hidden=!path;if(path)link.href='/api/download?path='+encodeURIComponent(path);else link.removeAttribute('href');}
  $('#deleteWorkflowBtn').disabled=!run;
  loadWorkflowDiagnostics(selectedWorkflowId);
  for(const id of workflowRunSelectors){chooseOptions($('#'+id),workflowRuns.map(item=>[item.run_id,workflowLabel(item)]),'① 请选择流程');$('#'+id).value=selectedWorkflowId;}
  $('#workspaceArtifactStep').hidden=!run;
  chooseOptions($('#workspaceArtifact'),artifactsFor(run).map(key=>[key,workflowStageLabels[key]]),'② 请选择阶段中间结果');
  $('#workspaceRunInfo').textContent=run?`${run.run_id} · 已完成 ${(run.completed_stages||[]).map(key=>workflowStageLabels[key]).join(' → ')} · ${formatTokenUsage(run.token_usage)}`:'先选择流程，再选择阶段中间结果。';
  for(const prefix of ['taxonomy','manager']){
    const eligible=artifactsFor(run,'domains'),canonical=eligible.includes('iterate')&&(run.completed_stages||[]).includes('iterate')?['iterate']:eligible.includes('domains')?['domains']:eligible.slice(-1);
    $('#'+prefix+'ArtifactStep').hidden=!run;
    chooseOptions($('#'+prefix+'Artifact'),canonical.map(key=>[key,workflowStageLabels[key]]),canonical.length?'② 请选择层级中间结果':'该流程尚未生成候选层级');
  }
  $('#reportArtifactStep').hidden=!run;
  chooseOptions($('#reportArtifact'),Object.keys(run?.artifacts||{}).map(key=>[key,workflowStageLabels[key]||artifactLabels[key]||key]),'② 请选择报告或中间结果');
  renderStageRunOptions();renderStageSources();renderScopedReports();
  if($('#workspace').classList.contains('active')&&$('#workspaceArtifact').value)loadWorkflowPreview();
}
function renderStageSources(){
  const run=selectedWorkflow(),external=$('#externalStageSettings').open&&$('#stageUseExternal').checked;
  const continuing=stageToolMode==='iterate';
  const actions=Object.keys(workflowStageLabels).filter(key=>continuing?key==='iterate':key!=='iterate');
  $('#stageActionStep').hidden=continuing||(!run&&!external);
  const actionSelect=$('#stageAction'),previous=actionSelect.value;
  actionSelect.innerHTML='<option value="">② 请选择可用能力</option>'+actions.map(key=>{
    const capability=stageCapability(run,key),enabled=external||capability.available;
    return `<option value="${esc(key)}" ${enabled?'':'disabled'} title="${esc(enabled?'输入文件已通过校验':capability.reason)}">${esc(workflowStageLabels[key]+(enabled?'':' · 不可用：'+briefReason(capability.reason)))}</option>`;
  }).join('');
  actionSelect.value=continuing&&(external||stageCapability(run,'iterate').available)?'iterate':actions.includes(previous)&&(external||stageCapability(run,previous).available)?previous:'';
  const unavailable=continuing?workflowRuns.filter(item=>!stageCapability(item,'iterate').available).map(item=>[workflowLabel(item),stageCapability(item,'iterate').reason]):run?actions.filter(key=>!stageCapability(run,key).available).map(key=>[workflowStageLabels[key],stageCapability(run,key).reason]):[];
  $('#stageAvailability').hidden=external||!unavailable.length;
  $('#stageAvailabilityReasons').innerHTML=unavailable.map(([label,reason])=>`<p><b>${esc(label)}</b><br>${esc(reason)}</p>`).join('');
  const stage=$('#stageAction').value,required=workflowRequired[stage];
  let eligible=stage&&!external?stageCapability(run,stage).sources||[]:[];
  // Prefer the latest usable iteration snapshot. Other stages expose only verified sources.
  if(continuing){
    const preferred=['iterate','export','report','domains'].find(key=>eligible.includes(key));
    eligible=preferred?[preferred]:eligible.slice(-1);
  }else{
    eligible=[...eligible].sort((a,b)=>a===required?-1:b===required?1:0);
  }
  $('#stageSourceStep').hidden=!run||!stage||external;
  chooseOptions($('#stageSource'),eligible.map(key=>[key,workflowStageLabels[key]||artifactLabels[key]||key]),'③ 请选择对应中间结果');
  $('#stageScopeInfo').hidden=!stage&&!(continuing&&run);
  const guidance={extract:'重新用 LLM 拆分原句，输出原子意图及动作关系；后续聚类需要重新执行。',cluster:'用已有原子意图请求 Embedding，再执行 BERTopic；输出候选簇和噪声，后续审核需要重新执行。',review:'用 LLM 检查簇内是否属于同一动作与对象，输出一致性结论和拆分建议。',pairs:'用 LLM 对比相邻簇，输出合并或保留建议，不直接修改簇成员。',domains:'沿 BERTopic 树自下而上逐层命名与审核，同层并行；不合理的父级合并展开为独立分支。',iterate:'召回已有叶子主题，匹配原子意图；未覆盖意图重新聚类，审核后重建同一棵 BERTopic 树并验证。',export:'整理已有结果并导出统计报告，不请求模型。'};
  $('#stageScopeInfo').textContent=stage?`${guidance[stage]||''} ${external?'使用高级设置指定的其他中间结果。':'执行后保存为新版本，原结果保留。'}`:'';
  if(continuing&&eligible.length===1)$('#stageSource').value=eligible[0];
  if(continuing&&run&&!stage)$('#stageScopeInfo').textContent='无法继续迭代：'+stageCapability(run,'iterate').reason;
  updateStageControls();
}
function updateStageControls(){
  const external=$('#externalStageSettings').open&&$('#stageUseExternal').checked;
  $('#externalStageFields').hidden=!external;
  $('#stageRun').disabled=external;$('#stageSource').disabled=external;
  const ready=Boolean($('#stageAction').value&&(external?$('#stageExternalPath').value.trim():selectedWorkflowId&&$('#stageSource').value));
  const preview=$('#stagePreviewStep'),source=JSON.stringify([selectedWorkflowId,$('#stageAction').value,$('#stageSource').value,external]);
  if(preview.dataset.source!==source||!ready){
    preview.open=false;
    if($('#workflowVisual').parentElement.id==='stagePreviewMount')resetWorkflowVisual();
    preview.dataset.source=source;
  }
  preview.hidden=!ready||external;
  $('#runStageBtn').textContent=stageToolMode==='iterate'?'开始继续迭代，保存新版本':$('#stageAction').value?`开始${workflowStageLabels[$('#stageAction').value]}，保存新版本`:'处理所选中间结果';
  $('#stageExecuteStep').hidden=!ready;$('#runStageBtn').disabled=!ready||($('#stageAction').value==='iterate'&&appendSelected()&&!inputReady('append'));
  $('#stageIterationSettings').hidden=!ready||$('#stageAction').value!=='iterate';
  $('#mapScenesBtn').disabled=external||!selectedWorkflowId||!$('#stageSource').value;
}
function renderScopedReports(){
  const run=selectedWorkflow(),key=$('#reportArtifact').value,path=run?.artifacts?.[key];
  $('#reportList').hidden=!path;
  $('#reportList').innerHTML=path?`<div class="file"><div><b>${esc(workflowStageLabels[key]||artifactLabels[key]||key)}</b><br><span class="muted">${esc(path.split(/[\\/]/).pop())}</span></div><span></span><span>${esc(run.status)}</span><a href="/api/download?path=${encodeURIComponent(path)}">下载</a></div>`:'';
}
async function loadWorkflowPreview(){
  resetWorkflowVisual();
  const revision=++workflowPreviewRevision,runId=selectedWorkflowId,artifact=$('#workspaceArtifact').value;
  workflowPreviewTables={};$('#workspaceResultTable').innerHTML='<option value="">③ 请选择结果表</option>';
  $('#workspaceTableStep').hidden=true;$('#workspaceResult').hidden=true;
  if(!runId||!artifact)return;
  try{
    const data=await api(`/api/workflows/${encodeURIComponent(runId)}/artifact/${encodeURIComponent(artifact)}`);
    if(revision!==workflowPreviewRevision)return;
    workflowPreviewTables=data.tables;
    const names=Object.keys(data.tables).filter(name=>data.tables[name].length);
    chooseOptions($('#workspaceResultTable'),names.map(name=>[name,name]),names.length?'③ 请选择结果表':'该结果没有可预览的数据');
    $('#workspaceTableStep').hidden=false;
    loadWorkflowVisual(runId,artifact);
  }catch(e){if(revision===workflowPreviewRevision){$('#workspaceResult').hidden=false;$('#workspaceResult').textContent=e.message;}}
}
function renderWorkflowPreview(){
  $('#workspaceResult').hidden=!$('#workspaceResultTable').value;
  const rows=(workflowPreviewTables[$('#workspaceResultTable').value]||[]).map(row=>Object.fromEntries(Object.entries(row).map(([key,value])=>[key,value&&typeof value==='object'?JSON.stringify(value):value])));
  $('#workspaceResult').classList.remove('empty');renderTable($('#workspaceResult'),rows);
}
for(const id of workflowRunSelectors)$('#'+id).addEventListener('change',event=>{selectedWorkflowId=event.target.value;resetWorkflowChoices();renderWorkflowChoices();});
$('#refreshWorkflowsBtn').addEventListener('click',()=>loadWorkflows().catch(e=>$('#workspaceRunInfo').textContent=e.message));
$('#workspaceArtifact').addEventListener('change',loadWorkflowPreview);
$('#workspaceResultTable').addEventListener('change',renderWorkflowPreview);
$('#stageAction').addEventListener('change',()=>{$('#stageSource').value='';renderStageSources();});
$('#stageSource').addEventListener('change',updateStageControls);
$('#stageUseExternal').addEventListener('change',()=>{$('#stageAction').value='';$('#stageSource').value='';renderStageSources();});
$('#stageExternalPath').addEventListener('input',updateStageControls);
$('#stageOtherArtifact').addEventListener('change',()=>{$('#stageExternalPath').value=$('#stageOtherArtifact').value;updateStageControls();});
$('#externalStageSettings').addEventListener('toggle',()=>{
  if(!$('#externalStageSettings').open){$('#stageUseExternal').checked=false;$('#stageAction').value='';$('#stageSource').value='';renderStageSources();}
  else{chooseOptions($('#stageOtherArtifact'),workflowRuns.flatMap(run=>Object.entries(run.artifacts||{}).filter(([key])=>workflowStageLabels[key]).map(([key,path])=>[path,run.run_id+' · '+workflowStageLabels[key]])),'请选择其他流程的中间结果');updateStageControls();}
});
$('#stageUploadBtn').addEventListener('click',async()=>{
  const file=$('#stageUploadInput').files[0];if(!file){$('#stageToolStatus').textContent='请先选择中间结果 JSON。';return;}
  const form=new FormData();form.append('file',file);$('#stageUploadBtn').disabled=true;
  try{const data=await api('/api/workflows/upload-artifact',{method:'POST',body:form});$('#stageExternalPath').value=data.path;$('#stageToolStatus').textContent=stageToolMode==='iterate'?'已导入中间结果，请设置本次迭代参数。':'已导入中间结果，请选择处理能力。';updateStageControls();}
  catch(e){$('#stageToolStatus').textContent=e.message;}finally{$('#stageUploadBtn').disabled=false;}
});
$('#runStageBtn').addEventListener('click',()=>{
  const external=$('#externalStageSettings').open&&$('#stageUseExternal').checked;
  if(!external&&(!stageCapability(selectedWorkflow(),$('#stageAction').value).available||!stageCapability(selectedWorkflow(),$('#stageAction').value).sources.includes($('#stageSource').value))){$('#stageToolStatus').textContent='所选输入未通过校验，请刷新流程并查看不可用原因。';return;}
  const workers=Number($('#stageWorkers').value);if(!Number.isInteger(workers)||workers<1||workers>32){$('#stageToolStatus').textContent='并发数需要为 1–32 的整数。';return;}
  const iteration_rounds=Number($('#stageIterationRounds').value);
  if($('#stageAction').value==='iterate'&&(!Number.isInteger(iteration_rounds)||iteration_rounds<1||iteration_rounds>10)){$('#stageToolStatus').textContent='最大迭代轮数需要为 1–10 的整数。';return;}
  if($('#stageAction').value==='iterate'&&appendSelected()&&!inputReady('append'))return alert('请等待文件读取完成，并选择工作表和文本列；也可以清除追加文件。');
  const use_external=$('#externalStageSettings').open&&$('#stageUseExternal').checked;
  startJob('/api/jobs/workflow-stage',{stage:$('#stageAction').value,run_id:selectedWorkflowId||null,artifact:$('#stageSource').value||null,workers,iteration_scope:$('#stageIterationScope').value||null,iteration_rounds:$('#stageAction').value==='iterate'?iteration_rounds:3,use_external,external_source:use_external?$('#stageExternalPath').value.trim():null,...($('#stageAction').value==='iterate'&&inputReady('append')?{append_input:inputPath('append'),text_column:$('#appendTextCol').value,sheet_name:inputImports.append.metadata.sheet||null}:{})},'#stageToolStatus');
});
$('#taxonomyArtifact').addEventListener('change',loadTaxonomy);
$('#managerArtifact').addEventListener('change',loadTaxonomyManager);
$('#reportArtifact').addEventListener('change',renderScopedReports);
$('#loadOtherReportsBtn').addEventListener('click',async()=>{try{const data=await api('/api/reports');$('#otherReportList').innerHTML=data.all.map(file=>`<div class="file"><b>${esc(file.name)}</b><span>${fmtBytes(file.size)}</span><span>${esc(file.modified_text)}</span><a href="/api/download?path=${encodeURIComponent(file.path)}">下载</a></div>`).join('')||'<div class="empty">没有其他文件。</div>';}catch(e){$('#otherReportList').textContent=e.message;}});

function renderExtraRows(name,body){
  const node=$('#model'+name+'ExtraRows');node.innerHTML='';
  for(const [key,value] of Object.entries(body))addExtraRow(name,key,JSON.stringify(value));
}
function addExtraRow(name,key='',value=''){
  const container=$('#model'+name+'ExtraRows'),row=document.createElement('div');row.className='request-param-row';
  row.innerHTML=`<input class="request-param-key" aria-label="参数 Key" placeholder="例如 chat_template_kwargs" value="${esc(key)}"><textarea class="request-param-value" aria-label="参数 JSON Value" rows="2" placeholder='例如 {"enable_thinking":false}'>${esc(value)}</textarea><button class="btn" type="button" data-model-param-remove>删除</button>`;
  row.querySelector('button').addEventListener('click',()=>{row.remove();container.dispatchEvent(new Event('input',{bubbles:true}));});container.append(row);
}
function collectModelEndpoint(name){
  const extra_body=Object.create(null);
  for(const row of $$('#model'+name+'ExtraRows .request-param-row')){
    const key=row.querySelector('.request-param-key').value.trim(),raw=row.querySelector('.request-param-value').value.trim();
    if(!key&&!raw)continue;if(!key)throw new Error('请填写额外参数的 Key');
    if(Object.hasOwn(extra_body,key))throw new Error('额外参数 Key 重复：'+key);
    try{extra_body[key]=JSON.parse(raw);}catch{throw new Error('参数 '+key+' 的 Value 不是有效 JSON；字符串需要加双引号。');}
  }
  const settings={base_url:$('#model'+name+'Url').value.trim(),model:$('#model'+name+'Name').value.trim(),api_key_env:$('#model'+name+'Env').value.trim(),api_key:$('#model'+name+'Key').value||null,clear_api_key:$('#model'+name+'ClearKey').checked,extra_body};
  if(name==='llm'){settings.thinking_mode=$('#modelThinkingMode').value;settings.thinking_parameter=$('#modelThinkingParameter').value.trim();if(settings.thinking_mode!=='default'&&!settings.thinking_parameter)throw new Error('请填写思考参数路径。');}
  return settings;
}
$$('[data-model-param-add]').forEach(button=>button.addEventListener('click',()=>{addExtraRow(button.dataset.modelParamAdd);button.dispatchEvent(new Event('input',{bubbles:true}));}));
$('#modelThinkingMode').addEventListener('change',()=>{$('#modelThinkingPathField').hidden=$('#modelThinkingMode').value==='default';});
function formatTokenUsage(usage){
  if(!usage)return '该历史版本未记录 Token 用量';
  if(!usage.requests)return '尚无已返回请求的 Token 统计；复用本地缓存不产生请求';
  const number=value=>(value||0).toLocaleString();
  let text=`已报告 Token：输入 ${number(usage.prompt_tokens)} / 输出 ${number(usage.completion_tokens)} / 合计 ${number(usage.total_tokens)} · ${usage.requests} 次请求`;
  if(usage.reasoning_reported_requests)text+=` · 思考 ${number(usage.reasoning_tokens)}（计入输出）`;
  if(usage.cached_reported_requests)text+=` · 缓存 ${number(usage.cached_tokens)}（计入输入）`;
  if(usage.unreported_requests)text+=` · ${usage.unreported_requests} 次用量未知`;
  if(usage.failed_requests)text+=` · ${usage.failed_requests} 次请求失败`;
  return text;
}

function renderTraceRecord(r){
  const status=({success:'成功',failed:'失败',running:'请求中'})[r.status]||r.status||'未知';
  const state=r.status==='success'?'success':r.status==='failed'?'failed':'pending';
  const requestTime=beijingTimestamp(r.started_at);
  const fields={input:'实际输入',output:'模型原始输出',parsed:'解析及校验后的数据',error:'失败原因',fallback:'接口重试说明'};
  return `<details class="request-trace-record"><summary class="request-record-heading"><span class="request-record-title"><strong>${r.kind==='embedding'?'Embedding':'LLM'} 请求 <span class="request-record-id">${esc(String(r.request_id||'').slice(0,8))}</span></strong><span class="request-record-meta request-record-time">${requestTime?'请求时间：'+requestTime+'（北京时间）':'请求时间未记录'}</span><span class="request-record-meta">${esc(r.stage_id||'未归属阶段')}${r.base_url?' · '+esc(r.base_url):''} · 尝试 ${esc(r.attempt||1)} · ${esc(r.input?.model||'')} · ${Number(r.duration_seconds||0).toFixed(2)} 秒</span></span><span class="request-status ${state}">${esc(status)}</span></summary><div class="request-record-body">${Object.keys(fields).filter(k=>r[k]!==undefined).map((k,index)=>`<section class="request-content-section" data-part="${k}"><h4><span class="request-section-number">${String(index+1).padStart(2,'0')}</span>${fields[k]}</h4><pre>${esc(typeof r[k]==='string'?r[k]:JSON.stringify(r[k],null,2))}</pre></section>`).join('')}</div></details>`;
}
function renderTraceTree(data){
  function node(n,depth=0){return `<details class="request-cluster-node" data-depth="${depth}"><summary class="request-cluster-heading"><span class="request-level-badge">第 ${depth+1} 层</span><span class="request-cluster-title"><strong>簇 ${esc(n.cluster_id)}</strong><span class="request-cluster-meta">${esc(n.stage_id)}${n.parent_cluster_id!=null?' · 父簇 '+esc(n.parent_cluster_id):' · 根簇'}</span></span><span class="request-count-badge">${n.request_count} 次请求</span></summary><div class="request-cluster-body"><button type="button" class="btn mini" data-trace-cluster="${esc(n.cluster_id)}" data-trace-stage="${esc(n.stage_id)}" data-trace-offset="0">查看本簇请求</button><div class="request-cluster-records"></div>${n.children.map(child=>node(child,depth+1)).join('')}</div></details>`;}
  return `<p class="muted">按父子簇递归展示，每页 10 个根簇；本簇请求单独分页，每页 10 条。${data.unlinked} 条请求缺少明确簇关联，可在列表查看。</p>`+data.nodes.map(n=>node(n)).join('');
}
// Shared, on-demand viewer for job stages and workflow examples.
function createRequestTraceViewer(requestPanel,getUrl){
requestPanel.classList.add('request-trace-panel');
let tracePage=0, traceGeneration=0, traceView='list';
function collapseRequestTraces(){
  traceGeneration++;
  tracePage=0;
  requestPanel.innerHTML='<button class="btn mini" type="button" data-trace-refresh>查看 模型请求记录</button>';
}
async function loadRequestTraces(){
  if(!getUrl())return;
  const generation=++traceGeneration;
  try{
  const response=await fetch(getUrl()+`&view=${traceView}&offset=${tracePage*10}`);
  if(generation!==traceGeneration)return;
  if(!response.ok)throw new Error('请求记录加载失败');
  const data=await response.json();
  if(generation!==traceGeneration)return;
  requestPanel.innerHTML=`<div class="request-trace-heading"><h3>模型请求记录</h3><button class="btn mini" type="button" data-trace-close>收起</button></div><p class="muted">共 ${data.total} ${traceView==='tree'?'个根簇':'条尝试记录'} · 最新在前 · 每页 10 ${traceView==='tree'?'簇':'条'} · 第 ${tracePage+1} / ${Math.max(1,Math.ceil(data.total/10))} 页</p><button class="btn mini" type="button" data-trace-mode>${traceView==='tree'?'切换请求列表':'按父子簇查看'}</button> <button class="btn mini" type="button" data-trace-refresh>刷新</button> <button class="btn mini" type="button" data-trace-prev ${tracePage?'':'disabled'}>上一页</button> <button class="btn mini" type="button" data-trace-next ${(tracePage+1)*10>=data.total?'disabled':''}>下一页</button>${data.view==='tree'?renderTraceTree(data):data.records.map(renderTraceRecord).join('')||'<p>暂无记录。历史任务无法还原未保存的输入输出。</p>'}`;
  }catch(error){if(generation===traceGeneration)requestPanel.innerHTML='<p>请求记录加载失败</p><button class="btn mini" type="button" data-trace-refresh>重试</button> <button class="btn mini" type="button" data-trace-close>收起</button>';}
}
requestPanel.innerHTML='<button class="btn mini" type="button" data-trace-refresh>查看 模型请求记录</button>';
requestPanel.addEventListener('click',async e=>{
const clusterButton=e.target.closest('[data-trace-cluster]');
if(clusterButton){
  const target=clusterButton.closest('.request-cluster-body').querySelector('.request-cluster-records');
  const url=new URL(getUrl(),location.origin),offset=Number(clusterButton.dataset.traceOffset||0);
  url.searchParams.set('view','list');url.searchParams.set('cluster',clusterButton.dataset.traceCluster);url.searchParams.set('stage',clusterButton.dataset.traceStage);url.searchParams.set('offset',offset);
  clusterButton.disabled=true;
  try{
    const response=await fetch(url);if(!response.ok)throw new Error('加载失败');const data=await response.json();
    const attrs=`data-trace-cluster="${esc(clusterButton.dataset.traceCluster)}" data-trace-stage="${esc(clusterButton.dataset.traceStage)}"`;
    target.innerHTML=`<p class="muted">共 ${data.total} 条 · 最新在前 · 第 ${Math.floor(offset/10)+1} 页</p><button type="button" class="btn mini" ${attrs} data-trace-offset="${Math.max(0,offset-10)}" ${offset?'':'disabled'}>上一页</button> <button type="button" class="btn mini" ${attrs} data-trace-offset="${offset+10}" ${offset+10>=data.total?'disabled':''}>下一页</button>`+data.records.map(renderTraceRecord).join('');
  }catch(error){target.textContent='请求加载失败，请重试';}finally{clusterButton.disabled=false;}
  return;
}
if(e.target.closest('[data-trace-mode]')){traceView=traceView==='tree'?'list':'tree';tracePage=0;loadRequestTraces();return;}
if(e.target.closest('[data-trace-close]')){collapseRequestTraces();return;}if(e.target.closest('[data-trace-next]'))tracePage++;else if(e.target.closest('[data-trace-prev]'))tracePage=Math.max(0,tracePage-1);else if(e.target.closest('[data-trace-refresh]'))tracePage=0;else return;loadRequestTraces();});

return {reset:collapseRequestTraces, open:loadRequestTraces};
}

const requestPanel=document.createElement('section');
$('#jobStageUsage').after(requestPanel);
const jobTraceViewer=createRequestTraceViewer(requestPanel,()=>usageJobSnapshot?`/api/jobs/${encodeURIComponent(usageJobSnapshot.job_id)}/requests?stage=${encodeURIComponent(selectedUsageStage||usageJobSnapshot.progress?.current?.id||'')}`:null);
let traceContext='';
function syncRequestTraceStage(job){
  const context=job.job_id+':'+(selectedUsageStage||job.progress?.current?.id||'');
  if(context!==traceContext){traceContext=context;jobTraceViewer.reset();}
}

function appendSelected(){return Boolean($('#appendUploadInput').files.length||inputPath('append')||inputImports.append.busy);}
$('#appendClear').addEventListener('click',()=>{
 $('#appendUploadInput').value='';$('#appendInput').value='';invalidateImport('append','已清除追加文件，可仅继续迭代已有用例。');
});

$('#resumeJobBtn').addEventListener('click',()=>{
 if(!currentJob)return;
 if(!confirm('创建新任务重新执行原计划，复用已保存且输入完全一致的成功请求。缺少记录或输入变化的部分会重新请求；原任务保留。继续？'))return;
 startJob(`/api/jobs/${encodeURIComponent(currentJob)}/resume`,{});
});

$('#mapScenesBtn').addEventListener('click',()=>{
 if(!selectedWorkflowId||!$('#stageSource').value)return alert('请先选择已有流程及 JSON 中间结果');
 const workers=Number($('#stageWorkers').value);
 if(!Number.isInteger(workers)||workers<1||workers>32)return alert('并发数需为1–32');
 startJob('/api/jobs/scene-mapping',{run_id:selectedWorkflowId,artifact:$('#stageSource').value,workers},'#stageToolStatus');
});

已有旧版 S4 关联结果也可补生成任务名称：在 S4 场景关联中选择该版本的完整过程报告。场景目录、原子意图ID及来源文本完全一致时，保留原 scene_id，仅调用 NAME_PROMPT 生成 task_name，不重新选择场景。没有旧归属或目录发生变化的意图仍执行完整关联。保存为新版本，原文件保留。


S4关联同时生成 results/query_atomic_intents.csv，仅含 query（原始句子）和原子意图（简短任务名称 task_name）两列；按原始输入行汇总，每条query占一行，多意图任务名称去重后换行拼接，名称生成失败则保留空值。所有结果统一在“流程能力 → 结果导出”下载；独立两列表导出不需要执行 S4 关联。


### 独立导出 query 与任务名称（不请求模型）

在“流程能力 → 结果导出”选择已有流程及结果快照，下载“query 与原子意图（CSV）”，不启动LLM任务，不读取场景目录，不重新拆分或分类。优先使用原子意图自身已保存的 task_name 或来源一致的独立名称记录；否则读取最终已确认任务归属对应的已审核叶子名称。没有迭代归属记录的首版结果，可以使用审核合格簇对应的叶子名称。显式待确认、未覆盖或失效版本归属不按簇成员关系强行补名。没有保存名称时列值留空，页面提示缺失条数；绝不回退为原句或自动补生成。

CSV仅含 query（原句）和原子意图（已有任务名称）两列。按原始row_id保留输入行顺序，每条query一行，多个任务名称在同一单元格去重、换行拼接；无解析结果或无名称的原始行也保留。原文件中不同来源行即使query文本相同，也不会合成一行。CLI命令 `python main.py export-atomic-tasks --source <阶段JSON>` 同样只做本地导出，并保存派生文件；不需要配置LLM、API Key或场景目录。S4关联模块自身的名称生成不受此修改影响。


### 统一结果导出入口

所有业务结果下载集中在 **流程能力 → 结果导出**。分类与迭代页面保留跳转按钮，不再散放下载操作。

| 导出内容 | 数据来源与格式 |
| --- | --- |
| query 与原子意图 | 按所选 JSON 快照本地生成 CSV；每个原始输入行对应一个或多个已有任务名称 |
| 原句与主题 | 当前流程已保存的最终 Excel / JSON，包含层级主题路径 |
| S4 场景与任务汇总 | 已执行场景关联的流程结果，CSV 中每个 S4 一行，任务列为任务名称 |
| S4 关联明细 | 当前流程已保存的每条原子意图与场景的关联明细 |
| 过程文件与中间结果 | 折叠区域下载审核工作簿、阶段快照与完整过程报告 |

先选择流程，再选择导出依据；未生成或已丢失的文件显示不可下载。切换快照只改变动态生成的 query 表，最终 Excel/JSON 和 S4 文件始终是所选流程实际保存的文件。导出不会调用模型、重跑分类或自动补齐缺失名称，也不会创建新的运行任务。历史 S4 版本即使将汇总文件保存在 workbook 字段下，也会按明确的 S4 文件名展示。

实现集中在 `intent_engine/exporting/`：`catalog.py` 负责下载目录及可用性，`query_tasks.py` 负责已有名称解析和按原始行汇总，`sentence_results.py` 负责原句与层级结果。工作流与 HTTP 接口复用这些实现。
