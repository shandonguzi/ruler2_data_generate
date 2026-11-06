let topicTreeNodes=new Map(),expandedTopicIds=new Set(),topicTreeInitialized=false;
function resetTopicHierarchy(){topicTreeNodes=new Map();expandedTopicIds=new Set();topicTreeInitialized=false;$('#visualTopic').innerHTML='<option value="all">全部层级主题</option>';for(const id of ['visualTopicTree','visualTopicInfo','visualHierarchySummary'])$('#'+id).innerHTML='';}
function topicNodeButton(key,label){return `<button type="button" class="topic-path-node" data-topic-select="${esc(key)}">${esc(label)}</button>`;}
function visualTopicPath(atom){
  if(!atom.topic_path?.length)return '';
  return `<div class="visual-topic-path"><small>BERTopic 主题路径（从上层到叶子）</small><div>${atom.topic_path.map(key=>{const node=visualData?.topic_nodes?.[key];return topicNodeButton(key,`第 ${(node?.depth??0)+1} 层 · ${node?.label||key}`);}).join('<span aria-hidden="true"> → </span>')}</div></div>`;
}
function visualSentenceTopics(row){
  const delivered=visualData?.delivery_source==='reviewed_bertopic'?`<div class="visual-classification"><b>最终导出主题</b>${(row.delivery_topics_by_level||[]).map(level=>`<p>第 ${level.level} 层：${level.topics.length?level.topics.map(esc).join('；'):'待确认'}</p>`).join('')||'<p>尚无通过验证的语义主题</p>'}</div>`:'';
  if(!row.topic_memberships?.length)return delivered;
  const levels=new Map();for(const item of row.topic_memberships){if(!levels.has(item.level))levels.set(item.level,[]);levels.get(item.level).push(item);}
  const treeName=visualData?.hierarchy?.source==='BERTopic+LLM'?'审核后 BERTopic':'原生 BERTopic';
  return delivered+`<details class="sentence-topics" ${visualView==='hierarchy'?'open':''}><summary>${treeName} 关联 ${row.topic_memberships.length} 个层级主题 · ${row.topic_paths?.length||0} 条意图路径</summary>${[...levels].map(([level,items])=>`<div class="sentence-topic-level"><b>第 ${level} 层</b><div>${items.map(item=>`<div>${topicNodeButton(item.node_id,item.label)}<small>来自 ${item.intent_ids.map(esc).join('、')}</small></div>`).join('')}</div></div>`).join('')}</details>`;
}
function topicTreeNode(key){
  const node=topicTreeNodes.get(key);if(!node)return '';
  const info=`<span><b>第 ${node.depth+1} 层 · ${esc(node.label)}</b><small>${node.intent_count} 条意图 / ${node.row_count} 条原句${node.is_leaf?' · 叶子主题 '+node.topic_id:''}</small></span>`;
  if(node.is_leaf)return `<div class="topic-leaf">${topicNodeButton(key,`第 ${node.depth+1} 层 · ${node.label}`)}<small>${node.intent_count} 条意图 / ${node.row_count} 条原句 · 叶子主题 ${node.topic_id}</small></div>`;
  return `<details class="topic-branch" data-hierarchy-node="${esc(key)}" ${expandedTopicIds.has(key)?'open':''}><summary>${info}<button type="button" class="btn mini" data-topic-select="${esc(key)}" aria-label="查看主题 ${esc(key)} 的原句">查看</button></summary><div class="topic-children"></div></details>`;
}
function fillTopicChildren(detail){
  const container=detail.querySelector(':scope > .topic-children');
  if(!container||container.dataset.loaded)return;
  const node=topicTreeNodes.get(detail.dataset.hierarchyNode);if(!node)return;
  container.innerHTML=node.children.map(topicTreeNode).join('');container.dataset.loaded='true';
}
function renderTopicHierarchy(data,selected){
  const entries=Object.values(data.topic_nodes||{}).sort((a,b)=>a.depth-b.depth||a.node_id.localeCompare(b.node_id));
  $('#visualTopic').innerHTML='<option value="all">全部层级主题</option>'+entries.map(node=>`<option value="${esc(node.node_id)}">第 ${node.depth+1} 层 · ${esc(node.label)}</option>`).join('');$('#visualTopic').value=selected||'all';
  if(visualView!=='hierarchy')return;
  const tree=data.hierarchy||{status:'not_generated',nodes:[],root_ids:[],max_depth:0};
  topicTreeNodes=new Map((tree.nodes||[]).map(node=>[node.node_id,node]));
  if(!topicTreeInitialized){for(const root of tree.root_ids||[])expandedTopicIds.add(root);topicTreeInitialized=true;}
  $('#visualHierarchySummary').textContent=['ready','single_topic','forest'].includes(tree.status)?`${tree.leaf_count} 个叶子主题 · 最深 ${tree.max_depth} 层。层数随分支变化；原句可关联多条路径，噪声不进入主题树，待确认叶子不进入最终导出。`:tree.message||'尚未生成可用的多层主题树。';
  $('#visualRebuildHierarchy').hidden=!['not_generated','failed','disabled'].includes(tree.status);
  $('#visualTopicTree').innerHTML=(tree.root_ids||[]).map(topicTreeNode).join('')||'<div class="empty">没有可展示的主题；先检查叶子簇和噪声。</div>';
  const stack=[...$('#visualTopicTree').querySelectorAll('details[open]')];while(stack.length){const detail=stack.pop();fillTopicChildren(detail);stack.push(...detail.querySelectorAll(':scope > .topic-children > details[open]'));}
  const node=topicTreeNodes.get(selected);
  $('#visualTopicInfo').innerHTML=node?`<h4>第 ${node.depth+1} 层 · ${esc(node.label)}</h4><p>${node.intent_count} 条原子意图 · ${node.unique_text_count} 种文本 · ${node.row_count} 条不同原句</p>${node.is_leaf&&node.support_level==='single_example'?`<p><span class="tag">${node.semantic_status==='approved'?'低支持叶子 · 已准入':'单文本叶子 · 语义或命名待确认'}</span></p>`:''}${node.naming_source==='reused_leaf'?'<p><span class="tag">叶子成员与审核输入未变化 · 复用已审核名称与定义</span></p>':node.naming_source==='reused_singleton'?'<p><span class="tag">单文本叶子 · 复用已审核名称，本次未请求模型</span></p>':''}${node.description?`<p>${esc(node.description)}</p>`:''}${node.review_reason?`<p class="muted">审核依据：${esc(node.review_reason)}</p>`:''}<p>覆盖叶子主题：${node.leaf_topic_ids.map(esc).join('、')}</p>${node.distance!=null?`<p class="muted">合并距离：${node.distance.toFixed(4)}</p>`:''}<p class="muted">下方展示包含该主题的原句，并保留同句其他意图与路径。主题归属由叶子主题向祖先继承，不表示新增了用户请求。</p>`:'<h4>逐层展开，查看主题覆盖的原句</h4><p>点击箭头展开下层，点击“查看”或叶子主题筛选成员。每个原子意图关联一条从根到叶子的路径；一条原句包含多个意图时，可以跨多条分支。</p><p class="muted">BERTopic 提供合并结构，LLM 逐层命名并审核父子主题关系。不合理的父级合并会展开，因此可能保留多个独立根分支；不另建领域树。历史结果仍按其原有结构展示。</p>';
}
function selectVisualTopic(key){visualView='hierarchy';visualPage=1;$('#visualCluster').value='all';$('#visualTopic').value=key;refreshWorkflowVisual();}
$('#visualTopicTree').addEventListener('toggle',event=>{const detail=event.target;if(!detail.matches('details[data-hierarchy-node]'))return;if(detail.open){expandedTopicIds.add(detail.dataset.hierarchyNode);fillTopicChildren(detail);}else expandedTopicIds.delete(detail.dataset.hierarchyNode);},true);
for(const id of ['visualTopicTree','visualRecords'])$('#'+id).addEventListener('click',event=>{const button=event.target.closest('[data-topic-select]');if(button){event.preventDefault();event.stopPropagation();selectVisualTopic(button.dataset.topicSelect);}});
$('#visualTopic').addEventListener('change',()=>{visualPage=1;refreshWorkflowVisual();});
$('#visualRebuildHierarchy').addEventListener('click',()=>openDiagnosticStage('cluster'));
