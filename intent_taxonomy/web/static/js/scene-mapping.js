let sceneMappingRuns=[],sceneMappingRevision=0;
function renderSceneMappingSource(){
 const run=sceneMappingRuns.find(r=>r.run_id===$('#sceneRun').value);
 const sources=run?.capabilities?.cluster?.sources||[];
 const previous=$('#sceneSource').value;
 const ordered=['report','export','iterate','domains','pairs','review','cluster','extract'].filter(k=>sources.includes(k));
 $('#sceneSource').innerHTML='<option value="">请选择原子意图结果</option>'+ordered.map(k=>`<option value="${esc(k)}">${esc(workflowStageLabels[k]||artifactLabels[k]||k)}</option>`).join('');
 if(ordered.includes(previous))$('#sceneSource').value=previous;
 $('#sceneSourceField').hidden=!ordered.length;
 $('#sceneStatus').textContent=run?(ordered.length?'请选择中间结果后开始关联。':run.capabilities?.cluster?.reason||'该流程没有可用的原子意图结果。'):'请选择已有流程。';
 updateSceneMappingReady();
}
function updateSceneMappingReady(){
 const ready=Boolean($('#sceneRun').value&&$('#sceneSource').value);
 $('#sceneRunSettings').hidden=!ready;$('#sceneStartBtn').disabled=!ready;
}
async function loadSceneMappingSources(){
 const revision=++sceneMappingRevision,previous=$('#sceneRun').value;
 $('#sceneStartBtn').disabled=true;$('#sceneStatus').textContent='正在读取已有原子意图结果…';
 try{
  const data=await api('/api/workflows');if(revision!==sceneMappingRevision)return;
  sceneMappingRuns=data.runs;workflowStageLabels=data.stages||workflowStageLabels;
  $('#sceneRun').innerHTML='<option value="">请选择已有流程</option>'+sceneMappingRuns.map(r=>`<option value="${esc(r.run_id)}">${esc(r.run_id)} · ${esc(r.status)}</option>`).join('');
  if(sceneMappingRuns.some(r=>r.run_id===previous))$('#sceneRun').value=previous;
  else if(sceneMappingRuns.some(r=>r.run_id===selectedWorkflowId))$('#sceneRun').value=selectedWorkflowId;
  renderSceneMappingSource();
 }catch(e){if(revision===sceneMappingRevision)$('#sceneStatus').textContent='读取失败：'+e.message;}
}
$('#sceneRefreshBtn').addEventListener('click',loadSceneMappingSources);
$('#sceneRun').addEventListener('change',()=>{$('#sceneSource').value='';renderSceneMappingSource();});
$('#sceneSource').addEventListener('change',updateSceneMappingReady);
$('#sceneStartBtn').addEventListener('click',()=>{
 const run=sceneMappingRuns.find(r=>r.run_id===$('#sceneRun').value),artifact=$('#sceneSource').value;
 if(!run?.capabilities?.cluster?.sources?.includes(artifact))return alert('请选择有效的原子意图结果');
 const workers=Number($('#sceneWorkers').value);
 if(!Number.isInteger(workers)||workers<1||workers>32)return alert('并发数需要为1–32');
 startJob('/api/jobs/scene-mapping',{run_id:run.run_id,artifact,workers},'#sceneStatus');
});
