let diagnosticsRevision=0;
const diagnosticsNodes=['workspaceDiagnostics','stageDiagnostics','taxonomyDiagnostics','managerDiagnostics'];
function resetWorkflowDiagnostics(){diagnosticsRevision++;for(const id of diagnosticsNodes){$('#'+id).hidden=true;$('#'+id).innerHTML='';}}
async function loadWorkflowDiagnostics(run){
  const revision=++diagnosticsRevision;
  for(const id of diagnosticsNodes){$('#'+id).hidden=!run;delete $('#'+id).dataset.code;$('#'+id).innerHTML=run?'<p class="muted">正在检查审核与层级结果…</p>':'';}
  if(!run)return;
  try{
    const data=await api(`/api/workflows/${encodeURIComponent(run)}/diagnostics`);
    if(revision!==diagnosticsRevision||run!==selectedWorkflowId)return;
    const r=data.review,labels={consistent:'一致簇',mixed:'混合簇',uncertain:'簇审核待确认（含低支持）',failed:'审核失败',pending:'尚未审核',low_support_eligible:'语义可用的低支持簇',low_support_approved:'已准入的低支持叶子'};
    const stages=data.stages.map(s=>`<span class="diagnostic-stage ${esc(s.status)}" title="${esc(s.message)}">${esc(s.label)} · ${esc(phaseStatusNames[s.status]||s.status)}</span>`).join('');
    const actions=data.next_stage?`<button class="btn" type="button" data-diagnostic-stage="${esc(data.next_stage)}">前往${esc(workflowStageLabels[data.next_stage])}</button>`:'';
    const viewSource=data.stages.some(s=>s.key==='cluster'&&['completed','completed_with_errors'].includes(s.status));
    const view=`<button class="btn" type="button" data-diagnostic-view="${viewSource?'cluster':'extract'}">查看${viewSource?'簇与审核建议':'原子意图'}</button>`;
    const html=`<div class="diagnostic-heading"><b>${data.tasks?'候选层级已生成':'候选层级尚未生成'}</b><span>${data.domains} 个领域 · ${data.tasks} 个候选任务</span></div><p>${esc(data.message)}</p><div class="diagnostic-counts">${Object.entries(labels).map(([key,label])=>`<span>${label} <b>${r[key]??0}</b></span>`).join('')}<span>噪声意图 <b>${data.noise}</b></span></div><details class="diagnostic-details"><summary>查看各阶段状态${data.errors.length?'及错误详情':''}</summary><div class="diagnostic-stages">${stages}</div>${data.errors.length?`<ul>${data.errors.map(e=>`<li>${esc(workflowStageLabels[e.stage]||e.stage)}${e.cluster_id!=null?' · 簇 '+esc(e.cluster_id):''}：${esc(e.error)}</li>`).join('')}</ul><p class="muted">最多显示 30 条错误，完整内容保存在流程报告中。</p>`:''}</details><div class="actions">${actions}${view}</div>`;
    const iteration=data.iteration,rounds=iteration?.rounds||[];
    const iterationHtml=iteration?.status?`<section class="iteration-overview"><b>候选体系迭代 · ${esc(({running:'执行中',completed:'已结束',completed_with_errors:'结束，需处理',failed:'失败',blocked:'条件不足',skipped:'未启用'})[iteration.status]||iteration.status)}</b><p>${esc(iteration.stop_message||'正在执行分类、审核与候选体系更新…')}</p><p class="muted">已执行 ${rounds.length} / ${iteration.max_rounds} 轮 · ${iteration.converged?'本轮无实际变更':'尚未确认收敛'}${iteration.validated_taxonomy_version!=null?' · 最终分类已验证 v'+esc(iteration.validated_taxonomy_version):''}</p>${rounds.length?`<div class="iteration-history"><table><thead><tr><th>轮次</th><th>任务数</th><th>实际变更</th><th>重新分类 / 复用</th><th>已匹配</th><th>新发现</th><th>错误</th></tr></thead><tbody>${rounds.map(r=>`<tr><td>第 ${r.round} 轮</td><td>${r.before} → ${r.after}</td><td>${r.changes}</td><td>${r.classified??'—'} / ${r.reused??'—'}</td><td>${r.known}</td><td>${r.discovery}</td><td>${r.errors}</td></tr>`).join('')}</tbody></table></div><p class="muted">匹配数量为各轮更新前的体系分类；最终分类在「阶段结果 → 迭代分类」查看。上方簇统计包含已应用的局部细分。</p>`:''}${iteration.error?`<p>${esc(iteration.error)}</p>`:''}</section>`:'';
    const refinement=data.refinement,history=refinement?.history||[];
    const refinementHtml=refinement?.status?`<section class="iteration-overview"><b>混合簇局部细分</b><p>已应用 ${history.filter(h=>h.applied).length} 次拆分 · 待确认 ${refinement.pending_clusters} 个簇 · 每条分支最多 ${refinement.max_rounds} 轮</p><details><summary>查看拆分记录</summary>${history.map(h=>`<p>簇 ${esc(h.parent_cluster_id)} · 第 ${esc(h.round)} 轮 · ${h.method==='local_bertopic'?'局部 BERTopic':h.method==='llm_full_partition'?'LLM 完整分组':'未可靠分组'} · ${h.applied?'已拆为 '+esc((h.child_cluster_ids||[]).join('、')):'保留待确认'}${h.errors?.length?' · '+esc(h.errors.join('；')):''}</p>`).join('')}</details><p class="muted">通过审核的子簇继续层级归纳；待确认意图保留原句并参加后续分类；仅最终验证通过的匹配可补充语义主题。</p></section>`:'';
    const delivery=data.delivery,coverage=delivery?.coverage;
    const deliveryHtml=coverage?`<section class="iteration-overview"><b>最终主题覆盖 ${coverage.covered_rows} / ${coverage.rows} 条原句</b><p>${delivery.source==='semantic'?'使用审核后的语义主题；已验证分类可补充原噪声和待确认簇。':'使用原生 BERTopic 主题路径。'} 无效原句 ${coverage.invalid_rows} 条 · 待确认意图 ${coverage.pending_intents} 条 · 噪声意图 ${coverage.noise_intents} 条</p>${delivery.warnings?.map(w=>`<p class="notice">${esc(w)}</p>`).join('')||''}<p class="muted">收敛只表示本轮体系无实际变更，不表示全部原句已覆盖或分类正确。</p></section>`:'';
    const mergeStatus={applied:'已实际合并',verified:'复核通过',keep:'保持独立',uncertain:'关系待确认',rejected:'独立复核未通过',deferred_overlap:'延后重新审核',rolled_back:'已回滚',error:'审核失败',failed:'归并失败',completed:'已完成',completed_with_errors:'完成，含错误',disabled:'未启用',no_candidates:'没有可处理的叶子'};
    const regroup=data.parent_regroup?.passes||[];
    const regroupHtml=regroup.length?`<section class="iteration-overview"><b>父级重组</b><details><summary>查看父级调整与审核理由</summary>${regroup.map(p=>`<p>状态：${esc(p.status)} · 应用 ${p.applied||0} 项${p.error?' · '+esc(p.error):''}</p>${(p.proposals||[]).map(r=>`<p>${esc(r.root)} → ${esc(r.target_id||'保持独立')} · ${r.applied?'已应用':r.deferred?'延后处理':'保留原结构'}<br>${esc(r.verification?.reason||r.reason||'')}</p>`).join('')}`).join('')}</details></section>`:'';
    const merge=data.leaf_merge,mergeHtml=merge?.passes?.length?`<section class="iteration-overview"><b>叶子实例泛化与归并</b><p>已实际合并 ${merge.applied||0} 对叶子 · 旧任务 ID 映射 ${Object.keys(merge.aliases||{}).length} 项</p><details><summary>查看合并、保留与失败记录</summary>${merge.passes.map(pass=>`<p><b>${esc(pass.phase)}</b> · ${esc(pass.skipped?'体系与配置未变化，已跳过':mergeStatus[pass.status]||pass.status)} · 合并 ${pass.applied||0} 对 · 缓存 ${pass.cached||0} 对 · 待后续处理 ${pass.deferred_candidates||0} 对${pass.error?' · '+esc(pass.error):''}</p>${(pass.pairs||[]).map(pair=>`<p>${(pair.task_ids||[]).map(esc).join(' ↔ ')} · ${esc(mergeStatus[pair.status]||pair.status)}${pair.canonical_task_id?' → '+esc(pair.canonical_task_id):''}<br>${esc(pair.validation?.reason||pair.proposal?.reason||pair.error||'')}${pair.sample_counts?'（抽样 '+pair.sample_counts.map(esc).join(' + ')+' 条，不代表全簇逐条验证）':''}</p>`).join('')}`).join('')}</details></section>`:'';
    const appended=data.append_input;
    const appendHtml=appended?.input_rows!=null?`<p class="notice">追加用例：输入 ${appended.input_rows} 条 · 新增 ${appended.row_count} 条 · 已有重复 ${appended.duplicate_existing} 条 · 文件内重复 ${appended.duplicate_in_file} 条 · 空白 ${appended.blank_rows} 条</p>`:'';
    const stageSummary=`<div class="diagnostic-heading"><b>所选流程摘要</b><span>${data.tasks} 个候选任务 · ${data.noise} 条噪声意图</span></div><p class="section-caption">${esc(data.message)} 选择下方中间结果后，可在本页展开详情。</p>`;
    for(const id of diagnosticsNodes){$('#'+id).innerHTML=id==='stageDiagnostics'?stageSummary+appendHtml:(id==='workspaceDiagnostics'?'<h2 class="section-title">流程总览</h2><p class="section-caption">所选流程的整体状态与最终汇总，不随下方阶段结果切换。</p>':'')+html+appendHtml+deliveryHtml+mergeHtml+refinementHtml+iterationHtml;$('#'+id).dataset.code=data.code;}
  }catch(error){if(revision===diagnosticsRevision)for(const id of diagnosticsNodes)$('#'+id).textContent='无法读取结果诊断：'+error.message;}
}
async function openDiagnosticStage(stage){
  const run=selectedWorkflowId;await openPanel(stage==='iterate'?'continueIteration':'stageTools');if(run!==selectedWorkflowId)return;
  if(![...$('#stageAction').options].some(option=>option.value===stage)){$('#stageToolStatus').textContent='该阶段的上游结果尚未准备好，请先处理前序阶段。';return;}
  $('#stageAction').value=stage;$('#stageSource').value='';renderStageSources();
  // Explicit recovery action prepares the only compatible source; it never starts a job.
  if($('#stageSource').options.length===2){$('#stageSource').selectedIndex=1;updateStageControls();}
  $('#stageScopeInfo').scrollIntoView({block:'center'});
}
async function openDiagnosticView(stage){
  const run=selectedWorkflowId;await openPanel('workspace');if(run!==selectedWorkflowId)return;
  const artifacts=selectedWorkflow()?.artifacts||{};
  const artifact=stage==='extract'&&artifacts.extract?'extract':['export','iterate','domains','pairs','review','cluster','extract'].find(key=>artifacts[key]);
  if(!artifact)return;$('#workspaceArtifact').value=artifact;await loadWorkflowPreview();
  if(stage==='cluster'){$('#visualClustersTab').click();}
  $('#workflowVisual').scrollIntoView({block:'start'});
}
for(const id of diagnosticsNodes)$('#'+id).addEventListener('click',event=>{const stage=event.target.closest('[data-diagnostic-stage]'),view=event.target.closest('[data-diagnostic-view]');if(stage)openDiagnosticStage(stage.dataset.diagnosticStage);if(view)openDiagnosticView(view.dataset.diagnosticView);});
