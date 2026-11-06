// Only one round's phase buttons are rendered; polling preserves manual browsing.
let jobStageNavigation={jobId:null,follow:true,round:null,activeGroup:null,activeRound:null,activeStep:null,open:new Set()};
let renderedStageNavigation=null;
const iterationClosingStages=new Set(['iteration_validation','iteration_reports']);
const iterationPreparingStages=new Set(['iteration_prepare']);
function stageGroup(step){return step&&(step.round>0||iterationClosingStages.has(step.id)||iterationPreparingStages.has(step.id))?'iterations':'main';}
function phaseButton(step,job){
  const selected=selectedUsageStage||job.progress?.current?.id;
  return `<button type="button" data-usage-stage="${esc(step.id)}" class="progress-stage ${esc(step.status||'')}" ${step.id===job.progress?.current?.id?'aria-current="step"':''} aria-pressed="${step.id===selected}" title="点击查看阶段请求和 Token 用量"><span class="phase-marker">${step.status==='completed'?'✓':step.status==='skipped'?'−':['failed','blocked','completed_with_errors'].includes(step.status)?'!':'·'}</span>${esc(step.label)}${['failed','blocked','completed_with_errors'].includes(step.status)?' · '+esc(phaseStatusNames[step.status]):''}</button>`;
}
function roundStatus(steps){
  if(steps.every(s=>s.status==='skipped'))return '未执行';
  if(steps.some(s=>s.status==='running'))return '执行中';
  if(steps.some(s=>['failed','cancelled','blocked','completed_with_errors'].includes(s.status)))return '需关注';
  if(steps.every(s=>['completed','skipped'].includes(s.status)))return '已完成';
  return steps.some(s=>s.status==='completed')?'已处理部分':'等待';
}
function groupSummary(steps){
  const processed=steps.filter(s=>['completed','completed_with_errors','failed','blocked','cancelled'].includes(s.status)).length;
  const planned=steps.filter(s=>s.status!=='skipped').length;
  return `${processed} / ${planned} 个阶段已处理`;
}
function followStageButton(){
  return `<button type="button" class="btn" data-job-follow aria-pressed="${jobStageNavigation.follow}">${jobStageNavigation.follow?'自动跟随当前轮':'返回当前阶段'}</button>`;
}
function roundPickerInUse(){
  const picker=$('#jobRoundSelect');
  return Boolean(picker&&(document.activeElement===picker||picker.matches(':hover')));
}
function renderJobStageNavigation(job,{force=false}={}){
  // Replacing an open native select closes its popup. Keep it alive even if a
  // different round starts; the overview and token counters continue polling.
  if(!force&&jobStageNavigation.jobId===job.job_id&&roundPickerInUse())return;
  const steps=job.progress?.steps||[],current=job.progress?.current;
  const rounds=[...new Set(steps.filter(s=>s.round>0).map(s=>s.round))].sort((a,b)=>a-b);
  const group=stageGroup(current);
  if(jobStageNavigation.jobId!==job.job_id){
    renderedStageNavigation=null;
    jobStageNavigation={jobId:job.job_id,follow:true,round:null,activeGroup:null,activeRound:null,activeStep:null,open:new Set()};
  }
  const state=jobStageNavigation;
  const started=rounds.filter(r=>steps.some(s=>s.round===r&&!['pending','skipped'].includes(s.status)));
  const currentRound=current?.round>0?current.round:started.at(-1)||rounds[0]||null;
  if(state.follow){
    state.round=currentRound;
    selectedUsageStage=null;
    if(state.activeGroup!==group||state.activeRound!==currentRound){
      state.open=new Set([group]);
    }
    if(state.activeStep!==current?.id&&iterationClosingStages.has(current?.id))state.open.add('closing');
    if(state.activeStep!==current?.id&&iterationPreparingStages.has(current?.id))state.open.add('preparing');
  }
  state.activeStep=current?.id;
  state.activeGroup=group;state.activeRound=currentRound;
  if(!rounds.includes(state.round))state.round=currentRound;
  const main=steps.filter(s=>!s.round&&!iterationClosingStages.has(s.id)&&!iterationPreparingStages.has(s.id));
  const closing=steps.filter(s=>iterationClosingStages.has(s.id));
  const preparing=steps.filter(s=>iterationPreparingStages.has(s.id));
  const roundSteps=steps.filter(s=>s.round===state.round);
  const groupHtml=(key,title,summary,body)=>`<details class="job-stage-group" data-job-group="${key}" ${state.open.has(key)?'open':''}><summary><span><b>${title}</b>${key===group&&current?'<span class="tag">当前</span>':''}</span><small>${summary}</small></summary><div class="job-stage-group-body">${body}</div></details>`;
  const mainButtons=main.map(s=>phaseButton(s,job)).join('')+(job.stage_usage?.__unassigned__?phaseButton({id:'__unassigned__',label:'未归属阶段的请求'},job):'');
  let html=steps.length?`<div class="job-stage-navigation-heading"><span>分层阶段 · 点击阶段查看请求和 Token</span>${followStageButton()}</div>`:'';
  if(main.length||job.stage_usage?.__unassigned__)html+=groupHtml('main','主流程',groupSummary(main),`<div class="progress-stage-list">${mainButtons}</div>`);
  if(rounds.length||closing.length||preparing.length){
    const roundSelector=rounds.length?`<div class="job-round-selector"><label for="jobRoundSelect">迭代轮次</label><select id="jobRoundSelect" aria-label="选择迭代轮次">${rounds.map(r=>`<option value="${r}" ${r===state.round?'selected':''}>第 ${r} 轮 · ${roundStatus(steps.filter(s=>s.round===r))}${current?.round===r?'（当前）':''}</option>`).join('')}</select><span class="muted">共计划 ${rounds.length} 轮${state.follow?'，自动定位当前轮':'，正在手动查看'}</span></div><div class="progress-stage-list">${roundSteps.map(s=>phaseButton(s,job)).join('')}</div>`:'';
    const closingHtml=closing.length?`<details class="job-iteration-closing" data-job-group="closing" ${state.open.has('closing')?'open':''}><summary>迭代收尾 · ${groupSummary(closing)}</summary><div class="progress-stage-list">${closing.map(s=>phaseButton(s,job)).join('')}</div></details>`:'';
    const preparingHtml=preparing.length?`<details class="job-iteration-closing" data-job-group="preparing" ${state.open.has('preparing')?'open':''}><summary>迭代准备 · ${groupSummary(preparing)}</summary><div class="progress-stage-list">${preparing.map(s=>phaseButton(s,job)).join('')}</div></details>`:'';
    html+=groupHtml('iterations','候选体系迭代',`${current?.round>0?'当前第 '+current.round+' 轮 · ':''}${groupSummary(steps.filter(s=>s.round>0||iterationClosingStages.has(s.id)||iterationPreparingStages.has(s.id)))}`,preparingHtml+roundSelector+closingHtml);
  }
  if(renderedStageNavigation!==html){
    const restorePickerFocus=document.activeElement===$('#jobRoundSelect');
    $('#jobStageList').innerHTML=html;
    renderedStageNavigation=html;
    if(restorePickerFocus)$('#jobRoundSelect')?.focus({preventScroll:true});
  }
}
function freezeJobStageFollowing(id){
  jobStageNavigation.follow=false;
  const step=usageJobSnapshot?.progress?.steps?.find(s=>s.id===id);
  if(step?.round>0)jobStageNavigation.round=step.round;
  const follow=$('#jobStageList [data-job-follow]');
  if(follow){follow.textContent='返回当前阶段';follow.setAttribute('aria-pressed','false');}
}
$('#jobStageList').addEventListener('change',event=>{
  if(event.target.id!=='jobRoundSelect'||!usageJobSnapshot)return;
  jobStageNavigation.follow=false;jobStageNavigation.round=Number(event.target.value);
  const steps=usageJobSnapshot.progress?.steps?.filter(s=>s.round===jobStageNavigation.round)||[];
  selectedUsageStage=(steps.find(s=>s.status==='running')||steps[0])?.id||null;
  jobStageNavigation.open.add('iterations');
  renderJobStageNavigation(usageJobSnapshot,{force:true});renderStageUsage(usageJobSnapshot);
});
$('#jobStageList').addEventListener('click',event=>{
  if(!event.target.closest('[data-job-follow]')||!usageJobSnapshot)return;
  jobStageNavigation.follow=true;jobStageNavigation.activeGroup=null;jobStageNavigation.activeStep=null;selectedUsageStage=null;
  renderJobStageNavigation(usageJobSnapshot,{force:true});renderStageUsage(usageJobSnapshot);
});
$('#jobStageList').addEventListener('toggle',event=>{
  const node=event.target;
  if(!node.matches?.('[data-job-group]')||!$('#jobStageList').contains(node))return;
  if(node.open)jobStageNavigation.open.add(node.dataset.jobGroup);
  else jobStageNavigation.open.delete(node.dataset.jobGroup);
},true);

// Resume after leaving the picker; a small delay lets the original click finish
// before navigation DOM can be replaced. A subsequent poll also resumes it.
function resumeJobStageNavigation(){
  setTimeout(()=>{
    if(!usageJobSnapshot||roundPickerInUse())return;
    renderJobStageNavigation(usageJobSnapshot);renderStageUsage(usageJobSnapshot);
  },150);
}
$('#jobStageList').addEventListener('focusout',event=>{
  if(event.target.id==='jobRoundSelect')resumeJobStageNavigation();
});
$('#jobStageList').addEventListener('pointerout',event=>{
  if(event.target.id==='jobRoundSelect')resumeJobStageNavigation();
});
