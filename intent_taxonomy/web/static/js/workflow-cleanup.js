let workflowDeletePlan=null,workflowDeleteBusy=false,workflowDeleteRevision=0;
function closeWorkflowDelete(){if(!workflowDeleteBusy){workflowDeleteRevision++;$('#deleteWorkflowDialog').close();workflowDeletePlan=null;}}
$('#deleteWorkflowBtn').addEventListener('click',async()=>{
  const run=selectedWorkflowId;if(!run)return;workflowDeletePlan=null;const revision=++workflowDeleteRevision;
  $('#deleteWorkflowIdentity').textContent=`流程版本：${run}`;
  $('#deleteWorkflowSummary').textContent='正在核对文件与其他流程的引用…';$('#deleteWorkflowFiles').innerHTML='';$('#deleteWorkflowRetained').hidden=true;$('#deleteWorkflowError').textContent='';$('#deleteWorkflowConfirm').disabled=true;
  $('#deleteWorkflowDialog').showModal();
  try{
    const plan=await api(`/api/workflows/${encodeURIComponent(run)}/delete-preview`);
    if(revision!==workflowDeleteRevision||!$('#deleteWorkflowDialog').open||run!==selectedWorkflowId)return;
    workflowDeletePlan=plan;$('#deleteWorkflowSummary').textContent=`将删除 ${plan.files.length} 个文件，释放 ${fmtBytes(plan.bytes)}；同时删除 ${(plan.history_jobs||[]).length} 条关联任务历史。`;
    $('#deleteWorkflowFiles').innerHTML=plan.files.map(file=>`<li><span>${esc(file.name)}</span><small>${fmtBytes(file.size)}</small></li>`).join('');
    $('#deleteWorkflowRetained').hidden=!plan.retained.length;
    $('#deleteWorkflowRetained').innerHTML=`<b>以下文件被其他流程引用，本次保留</b><ul>${plan.retained.map(file=>`<li>${esc(file.name)}<br><small>引用版本：${file.referenced_by.map(esc).join('、')}</small></li>`).join('')}</ul>`;
    $('#deleteWorkflowConfirm').disabled=!plan.files.length;
  }catch(error){if(revision===workflowDeleteRevision&&$('#deleteWorkflowDialog').open){$('#deleteWorkflowSummary').textContent='暂时无法删除。';$('#deleteWorkflowError').textContent=error.message;}}
});
for(const id of ['deleteWorkflowClose','deleteWorkflowCancel'])$('#'+id).addEventListener('click',closeWorkflowDelete);
$('#deleteWorkflowDialog').addEventListener('cancel',event=>{if(workflowDeleteBusy)event.preventDefault();else{workflowDeleteRevision++;workflowDeletePlan=null;}});
$('#deleteWorkflowConfirm').addEventListener('click',async()=>{
  if(!workflowDeletePlan||workflowDeleteBusy)return;workflowDeleteBusy=true;
  for(const id of ['deleteWorkflowConfirm','deleteWorkflowClose','deleteWorkflowCancel'])$('#'+id).disabled=true;
  try{
    const result=await api(`/api/workflows/${encodeURIComponent(workflowDeletePlan.run_id)}`,{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision:workflowDeletePlan.revision})});
    forgetDeletedJobs(result.deleted_jobs);
    $('#deleteWorkflowDialog').close();workflowDeletePlan=null;
    if(selectedWorkflowId===result.run_id){selectedWorkflowId='';resetWorkflowChoices();}
    workflowRuns=workflowRuns.filter(run=>run.run_id!==result.run_id);renderWorkflowChoices();
    await Promise.all([loadWorkflows(),loadJobs()]);$('#workspaceRunInfo').textContent=`已删除 ${result.deleted} 个流程文件，释放 ${fmtBytes(result.bytes)}${result.retained.length?'；保留 '+result.retained.length+' 个仍被引用的文件':''}；移除 ${(result.deleted_jobs||[]).length} 条任务历史。`+(result.cleanup_warnings?.join('；')||'');
  }catch(error){$('#deleteWorkflowError').textContent=error.message+'；请关闭窗口后重新预览。';workflowDeletePlan=null;}
  finally{workflowDeleteBusy=false;$('#deleteWorkflowClose').disabled=false;$('#deleteWorkflowCancel').disabled=false;}
});
