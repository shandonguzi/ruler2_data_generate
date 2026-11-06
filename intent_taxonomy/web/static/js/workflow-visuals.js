let visualContext=null,visualRevision=0,visualView='intents',visualPage=1,visualData=null;
function resetWorkflowVisual(){$('#stagePreviewStep').open=false;workflowRequestDialog.close();workflowRequestViewer.reset();visualRevision++;visualContext=null;visualData=null;resetTopicHierarchy();$('#workflowVisual').hidden=true;for(const id of ['visualRecords','visualMetrics','visualBars','visualClusterReview','visualPageInfo'])$('#'+id).innerHTML='';}
function loadWorkflowVisual(run,artifact,host='workspaceVisualMount'){
  $('#'+host).append($('#workflowVisual'));
  visualContext={run,artifact};visualView='intents';visualPage=1;
  $('#visualQuery').value='';$('#visualCluster').innerHTML='<option value="all">全部簇 / 意图</option>';
  $('#workflowVisual').hidden=false;return refreshWorkflowVisual();
}
function visualClusterName(key){return key==='unassigned'?'尚未分簇 / 待确认':String(key)==='-1'?'噪声':`簇 ${key}`;}
function visualAtomKey(atom){return atom.cluster_id==null?'unassigned':String(atom.cluster_id);}
function visualStatusName(status){return ({valid:'有效',known:'已匹配',discovery:'新发现',ambiguous:'待确认',invalid:'非有效请求',processing_error:'处理失败'})[status]||status||'尚无状态';}
function visualBadge(atom){const key=visualAtomKey(atom);return `<span class="visual-badge ${key==='-1'?'noise':key==='unassigned'?'pending':''}">${esc(visualClusterName(key))}</span>`;}
function visualEvidence(ids){return ids.map(id=>`<button type="button" class="visual-evidence" data-visual-evidence="${esc(id)}" title="查看来源原句与意图">${esc(id)}</button>`).join(' ');}
function visualAtom(atom,index){const c=atom.classification,slots=c?.slots||atom.slots||{};return `<div class="visual-atom"><div><b>${index==null?'':String(index+1)+'. '}${esc(atom.text||atom.span)}</b>${visualBadge(atom)}${atom.topic_pending?'<span class="tag">原簇主题待确认</span>':''}<span class="tag">${esc(visualStatusName(atom.status))}</span></div><small>${esc(atom.intent_id)}${atom.text!==atom.span?' · 原文：'+esc(atom.span):''}</small>${atom.reason?`<p>依据：${esc(atom.reason)}</p>`:''}${!c&&(atom.object||atom.action)?`<p class="visual-classification"><b>原子意图语义</b> ${[atom.object,atom.action].filter(Boolean).map(esc).join(' / ')}</p>`:''}${c?`<p class="visual-classification"><b>分类结果（v${esc(c.taxonomy_version)}）</b> ${[c.domain,c.object,c.action].filter(Boolean).map(esc).join(" / ")} · ${esc(visualStatusName(c.status))}${c.task_id?" · "+esc(c.task_id):""}${c.error?" · "+esc(c.error):""}</p>`:''}${Object.keys(slots).length?`<p>参数：${esc(JSON.stringify(slots))}</p>`:''}${atom.delivery_topic_path?.length?`<p class="visual-classification"><b>最终语义主题</b> ${atom.delivery_topic_path.map(n=>esc(n.label)).join(" → ")}</p>`:""}${visualTopicPath(atom)}</div>`;}
function visualRaw(row){
  const raw=Array.from(row.raw_text||''),atoms=row.intents||[];let cursor=0,html='';
  for(let i=0;i<atoms.length;i++){const a=atoms[i];if(!Number.isInteger(a.start)||!Number.isInteger(a.end)||a.start<cursor||a.end>raw.length||a.end<=a.start)continue;
    html+=esc(raw.slice(cursor,a.start).join(''))+`<mark title="原子意图 ${i+1}">${esc(raw.slice(a.start,a.end).join(''))}<sup>${i+1}</sup></mark>`;cursor=a.end;}
  return html+esc(raw.slice(cursor).join(''));
}
async function refreshWorkflowVisual(){
  if(!visualContext)return;const rev=++visualRevision,ctx={...visualContext};
  const key=$('#visualCluster').value,query=$('#visualQuery').value.trim(),topic=visualView==='hierarchy'?$('#visualTopic').value:'all';
  $('#visualStatus').textContent='正在加载可视化…';$('#visualRecords').innerHTML='';$('#visualPrev').disabled=true;$('#visualNext').disabled=true;
  $('#visualIntentsTab').setAttribute('aria-pressed',String(visualView==='intents'));$('#visualClustersTab').setAttribute('aria-pressed',String(visualView==='clusters'));
  $('#visualHierarchyTab').setAttribute('aria-pressed',String(visualView==='hierarchy'));
  $('#visualHierarchyLayout').hidden=visualView!=='hierarchy';$('#visualClusterField').hidden=visualView==='hierarchy';$('#visualTopicField').hidden=visualView!=='hierarchy';
  $('#visualClusterLayout').hidden=visualView!=='clusters';
  try{
    const params=new URLSearchParams({view:visualView,cluster:key,topic,q:query,page:String(visualPage)});
    const data=await api(`/api/workflows/${encodeURIComponent(ctx.run)}/visualization/${encodeURIComponent(ctx.artifact)}?${params}`);
    if(rev!==visualRevision)return;visualData=data;visualPage=data.page;
    $('#visualMetrics').innerHTML=[['原句',data.totals.rows],['原子意图',data.totals.intents],['候选簇',data.totals.clusters],['噪声意图',data.totals.noise],['未分簇 / 待确认',data.totals.unassigned],['处理失败原句',data.totals.errors]].map(([name,count])=>`<div><strong>${count.toLocaleString()}</strong><span>${name}</span></div>`).join('');
    $('#visualCluster').innerHTML='<option value="all">全部簇 / 意图</option>'+data.clusters.map(c=>`<option value="${esc(c.key)}">${esc(c.label)} · ${c.count} 条 / ${c.unique_count} 种文本</option>`).join('');$('#visualCluster').value=key;
    $('#visualStatus').textContent=data.clustered?`全部原子意图中有 ${data.totals.ambiguous} 条待确认。噪声表示未被聚类接纳；未分簇也可能尚未执行聚类。`:'这个阶段尚未执行聚类，可先检查原子意图拆分，再到「阶段处理」执行无监督聚类。';
    if(data.delivery_source==='reviewed_bertopic')$('#visualStatus').textContent+=' 最终导出使用这套逐层审核的 BERTopic 主题路径。';
    renderVisualClusters(data,key);
    renderTopicHierarchy(data,topic);
    $('#visualRecords').innerHTML=data.records.length?data.records.map(record=>visualView!=='clusters'?`<article class="visual-row"><header><b>原句 #${esc(record.row_id)}</b><span class="tag">${esc(visualStatusName(record.status))}</span><span>${record.intents.length} 个原子意图</span></header><p class="visual-raw">${visualRaw(record)}</p>${visualSentenceTopics(record)}${record.reason?`<p class="muted">${esc(record.reason)}</p>`:''}<div class="visual-atoms">${record.intents.map(visualAtom).join('')||'<span class="muted">此原句没有提取出原子意图。</span>'}</div>${record.relations?.length?`<div class="visual-relations">${record.relations.map(r=>`<span>${Number(r.source)+1} → ${Number(r.target)+1} · ${esc(({before:'先于',depends_on:'依赖',parallel:'并行'})[r.kind]||r.kind)}${r.reason?'：'+esc(r.reason):''}</span>`).join('')}</div>`:''}</article>`:`<article class="visual-row">${visualAtom(record)}<p class="visual-source"><b>来源原句 #${esc(record.row_id)}</b> ${esc(record.raw_text)}</p></article>`).join(''):'<div class="empty">当前筛选没有匹配结果。</div>';
    addWorkflowRequestButtons();
    $('#visualPageInfo').textContent=`第 ${data.page} / ${data.pages} 页 · 匹配 ${data.total} ${visualView!=='clusters'?'条原句':'条原子意图'}`;
    $('#visualPrev').disabled=data.page<=1;$('#visualNext').disabled=data.page>=data.pages;
  }catch(error){if(rev===visualRevision){$('#visualStatus').textContent=error.message;$('#visualRecords').innerHTML='<div class="empty">可视化加载失败，请重新选择阶段或重试搜索。</div>';$('#visualMetrics').innerHTML='';$('#visualBars').innerHTML='';$('#visualClusterReview').innerHTML='';$('#visualPageInfo').textContent='';}}
}
function renderVisualClusters(data,key){
  const maximum=data.clusters.reduce((maximum,c)=>Math.max(maximum,c.count),1);
  $('#visualBars').innerHTML=data.clusters.slice(0,20).map(c=>`<button type="button" class="visual-bar ${c.key===key?'selected':''}" data-visual-cluster="${esc(c.key)}" aria-label="查看${esc(c.label)}，${c.count}条意图"><span>${esc(c.label)}</span><span class="visual-bar-track"><i style="width:${c.count/maximum*100}%"></i></span><b>${c.count}</b></button>`).join('')||'<p class="muted">尚无原子意图。</p>';
  const c=data.clusters.find(c=>c.key===key),r=c?.review;
  $('#visualClusterReview').innerHTML=c?`<h4>${esc(c.label)} · ${c.count} 条意图</h4><p class="muted">${c.unique_count} 种不同文本；成员按原始出现次数计数，聚类时相同文本去重。${c.support_level==='single_example'?(c.hierarchy_eligible?' 低支持叶子已准入，可参与分类；尚无多样本稳定性证据。':' 单文本支持量低；是否可用取决于语义与命名审核，样本少本身不构成拒绝理由。'):''}</p><div class="visual-keywords">${c.keywords.map(word=>`<span class="tag">${esc(word)}</span>`).join('')}</div>${r?`<p><b>${esc(c.review_source==='reused_review'?'簇内一致 · 复用已有审核':c.review_source==='singleton_skip'?'单样本 · 已跳过一致性请求':({consistent:'簇内一致',mixed:'混合意图，建议拆分',uncertain:'边界待确认'})[r.verdict]||r.verdict)}</b>${[r.object,r.action].filter(Boolean).length?' · '+[r.object,r.action].filter(Boolean).map(esc).join(' / '):''}</p><p>${esc(r.reason)}</p><p class="muted">${c.review_source==='singleton_skip'?'未请求一致性模型；复用已有解析，不表示类别稳定性已验证。':c.review_scope==='all_unique_texts'?'审核覆盖全部不同文本；重复出现次数单独计入成员数量。':'审核依据为所选代表样本，不代表逐条审核全部成员。'}</p>${r.split_groups?.length?`<p>拆分建议：${r.split_groups.map(g=>esc(g.label)+'（'+visualEvidence(g.evidence_ids||[])+'）').join('；')}</p>`:''}${r.boundary_ids?.length?`<p>边界样本：${visualEvidence(r.boundary_ids)}</p>`:''}`:'<p class="muted">'+(key==='-1'?'这些意图未被聚类接纳，仍保留来源，可在配置文件中调整聚类参数后重跑。':key==='unassigned'?'包括尚未聚类的有效意图与对象不明的待确认意图，请结合各条状态判断。':'这个阶段尚无簇审核结论，可执行「簇一致性审核」。')+'</p>'}${data.pair_reviews.length?`<h4>跨簇复核建议</h4>${data.pair_reviews.map(p=>`<p>簇 ${esc(p.left_cluster_id)} ↔ 簇 ${esc(p.right_cluster_id)}：${esc(({merge:'建议合并',keep:'保持独立',uncertain:'待确认'})[p.decision]||'处理失败')}<br>${esc(p.reason||p.error||'')}</p>`).join('')}`:''}`:'<h4>先选择一个簇</h4><p class="muted">点击左侧分布条或使用上方筛选框，查看簇关键词、审核结论和全部成员。这里展示规模分布，不表示簇之间的语义距离。</p>';
  if(c?.review_error)$('#visualClusterReview').innerHTML+=`<p class="notice">审核失败：${esc(c.review_error)}</p>`;
}
$('#visualBars').addEventListener('click',event=>{const button=event.target.closest('[data-visual-cluster]');if(button){$('#visualCluster').value=button.dataset.visualCluster;visualPage=1;refreshWorkflowVisual();}});
$('#visualClusterReview').addEventListener('click',event=>{const button=event.target.closest('[data-visual-evidence]');if(button){visualView='intents';visualPage=1;$('#visualCluster').value='all';$('#visualQuery').value=button.dataset.visualEvidence;refreshWorkflowVisual();}});
$('#visualCluster').addEventListener('change',()=>{visualPage=1;refreshWorkflowVisual();});
$('#visualSearchBtn').addEventListener('click',()=>{visualPage=1;refreshWorkflowVisual();});
$('#visualQuery').addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();visualPage=1;refreshWorkflowVisual();}});
for(const [id,view] of [['visualIntentsTab','intents'],['visualClustersTab','clusters'],['visualHierarchyTab','hierarchy']])$('#'+id).addEventListener('click',()=>{visualView=view;visualPage=1;if(view==='hierarchy')$('#visualCluster').value='all';refreshWorkflowVisual();});
$('#visualPrev').addEventListener('click',()=>{visualPage--;refreshWorkflowVisual();});$('#visualNext').addEventListener('click',()=>{visualPage++;refreshWorkflowVisual();});
$('#stagePreviewStep').addEventListener('toggle',()=>{
  const preview=$('#stagePreviewStep');
  if(!preview.open){if($('#workflowVisual').parentElement.id==='stagePreviewMount')resetWorkflowVisual();return;}
  const artifact=$('#stageSource').value,run=selectedWorkflowId;
  if(preview.hidden||!run||!artifact||($('#externalStageSettings').open&&$('#stageUseExternal').checked)){preview.open=false;return;}
  $('#stagePreviewContext').textContent=`输入快照：${workflowStageLabels[artifact]||artifact} · ${run}。以下内容对应所选中间结果。`;
  loadWorkflowVisual(run,artifact,'stagePreviewMount');
});

// Both entry points use the same request viewer and persisted request format.
const workflowRequestDialog=document.createElement('dialog');
workflowRequestDialog.className='workflow-request-dialog';
workflowRequestDialog.innerHTML='<div class="request-trace-heading"><h3>流程 LLM 请求审查</h3><button type="button" class="btn mini" data-request-dialog-close>关闭</button></div><p class="muted">用例筛选匹配请求中的原句、原子文本或意图 ID；只含摘要的上层审核请查看整个流程记录。</p><section></section>';
document.body.append(workflowRequestDialog);
let workflowRequestUrl=null;
const workflowRequestViewer=createRequestTraceViewer(workflowRequestDialog.querySelector('section'),()=>workflowRequestUrl);
workflowRequestDialog.querySelector('[data-request-dialog-close]').addEventListener('click',()=>workflowRequestDialog.close());
workflowRequestDialog.addEventListener('close',()=>workflowRequestViewer.reset());
function openWorkflowRequests(terms=[]){
  if(!visualContext)return;
  workflowRequestUrl=`/api/workflows/${encodeURIComponent(visualContext.run)}/requests?terms=${encodeURIComponent(JSON.stringify([...new Set(terms.filter(Boolean))]))}`;
  workflowRequestViewer.reset();
  workflowRequestDialog.showModal();
  workflowRequestViewer.open();
}
const workflowRequestButton=document.createElement('button');
workflowRequestButton.type='button';workflowRequestButton.className='btn mini';workflowRequestButton.textContent='审查流程 LLM 请求';
$('#visualStatus').after(workflowRequestButton);
workflowRequestButton.addEventListener('click',()=>openWorkflowRequests());
$('#visualRecords').addEventListener('click',event=>{
  const button=event.target.closest('[data-review-record]');if(!button||!visualData)return;
  const record=visualData.records[Number(button.dataset.reviewRecord)];if(!record)return;
  const atoms=record.intents||[record];
  openWorkflowRequests([record.raw_text,...atoms.flatMap(a=>[a.intent_id,a.text])]);
});
function addWorkflowRequestButtons(){
  $$('#visualRecords .visual-row').forEach((row,index)=>{
    const button=document.createElement('button');button.type='button';button.className='btn mini';
    button.dataset.reviewRecord=String(index);button.textContent='审查相关 LLM 请求';row.append(button);
  });
}
