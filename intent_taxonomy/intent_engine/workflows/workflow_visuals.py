"""Full-snapshot aggregates with paginated, traceable visualization records."""
from collections import Counter, defaultdict


def visualization_page(data, *, view="intents", cluster="all", topic="all", query="", page=1, page_size=20):
    if view not in {"intents", "clusters", "hierarchy"}:
        raise ValueError("可视化类型无效")
    from intent_engine.atomic.topic_hierarchy import attach_topic_memberships
    if data.get("topic_hierarchy"):
        # Views derive counts and paths from actual membership, not stored sample counts.
        import copy
        data = copy.deepcopy(data)
        attach_topic_memberships(data, validate_existing=True)
    hierarchy = data.get("topic_hierarchy", {"status": "not_generated", "nodes": [], "root_ids": [], "max_depth": 0,
        "message": "这个历史结果尚未生成 BERTopic 层级主题，请重跑无监督聚类"})
    node_ids = {node["node_id"] for node in hierarchy.get("nodes", [])}
    if topic != "all" and topic not in node_ids:
        raise ValueError("所选层级主题不存在")
    latest = {item["intent_id"]: item for item in data.get("iteration", {}).get("assignments", [])}
    atoms = [{**atom, "classification": latest.get(atom["intent_id"])} for atom in data.get("intents", [])]
    rows = data.get("rows", [])
    from intent_engine.exporting.delivery_topics import hierarchy_projection
    projection = hierarchy_projection(data)
    if projection is not None:
        for atom in atoms:
            atom['delivery_topic_path'] = projection['assignments'].get(atom['intent_id'], {}).get('path', [])
    if projection is not None and projection['source']=='reviewed_bertopic':
        # The tree view and final file must show identical validated paths, including
        # noise later matched to an existing leaf. Keep physical clusters unchanged.
        tree_nodes = {node['node_id']:node for node in hierarchy.get('nodes', [])}
        members, texts, origins = Counter(), defaultdict(set), defaultdict(set)
        row_topics, row_paths = defaultdict(lambda:defaultdict(list)), defaultdict(list)
        for atom in atoms:
            atom['cluster_topic_path'] = atom.get('topic_path', [])
            path = atom.get('delivery_topic_path', [])
            atom['topic_path'] = [node['node_id'] for node in path]
            if path:
                row_paths[atom['row_id']].append({'intent_id':atom['intent_id'],'node_ids':atom['topic_path']})
            for node in path:
                key = node['node_id']
                members[key] += 1
                texts[key].add(atom['text'])
                origins[key].add(atom['row_id'])
                row_topics[atom['row_id']][key].append(atom['intent_id'])
        for key,node in tree_nodes.items():
            node.update(intent_count=members[key],unique_text_count=len(texts[key]),row_count=len(origins[key]))
        rows = [{**row,'topic_paths':row_paths[row['row_id']], 'topic_memberships':[
            {'node_id':key,'level':tree_nodes[key]['depth']+1,'label':tree_nodes[key]['label'],'intent_ids':ids}
            for key,ids in sorted(row_topics[row['row_id']].items(),key=lambda item:(tree_nodes[item[0]]['depth'],item[0]))]}
            for row in rows]
    from intent_engine.exporting.sentence_results import build_sentence_results
    delivered = build_sentence_results(data)['records']
    rows = [{**row, 'delivery_topics_by_level':record['topics_by_level']} for row,record in zip(rows, delivered)]
    grouped = defaultdict(list)
    counts = Counter()
    unique = defaultdict(set)
    for atom in atoms:
        key = str(atom["cluster_id"]) if atom.get("cluster_id") is not None else "unassigned"
        counts[key] += 1
        unique[key].add(atom.get("text", ""))
        grouped[atom["row_id"]].append(atom)
    metadata = {str(c["cluster_id"]): c for c in data.get("clusters", [])}
    clusters = []
    for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        info = metadata.get(key, {})
        clusters.append({"key": key, "count": count, "unique_count": len(unique[key]),
                         "keywords": info.get("keywords", []), "review": info.get("review"),
                         "support_level": info.get("support_level"), "topic_pending":info.get("topic_pending"),
                         "hierarchy_eligible": info.get("hierarchy_eligible"),
                         "review_scope": info.get("review_scope"), "review_source": info.get("review_source"), "review_error": info.get("review_error"),
                         "label": "噪声" if key == "-1" else "尚未分簇 / 待确认" if key == "unassigned" else f"簇 {key}"})
    if cluster != "all" and cluster not in counts:
        raise ValueError("所选簇不存在")
    query = query.strip().casefold()

    def matches(atom):
        key = str(atom["cluster_id"]) if atom.get("cluster_id") is not None else "unassigned"
        return (cluster == "all" or cluster == key) and (topic == "all" or topic in atom.get("topic_path", [])) and (not query or
            query in atom.get("text", "").casefold() or query in atom.get("raw_text", "").casefold()
            or query == str(atom.get("intent_id", "")).casefold())

    if view == "clusters":
        records = [a for a in atoms if matches(a)]
    else:
        records = [{**row, "intents": grouped[row["row_id"]]} for row in rows
                   if (cluster == "all" and topic == "all" and (not query or query in row.get("raw_text", "").casefold()))
                   or any(matches(a) for a in grouped[row["row_id"]])]
    size = max(1, min(int(page_size), 50))
    pages = max(1, (len(records) + size - 1) // size)
    page = max(1, min(int(page), pages))
    return {"totals": {"rows": len(rows), "intents": len(atoms),
                       "clusters": sum(key not in {"-1", "unassigned"} for key in counts),
                       "noise": counts["-1"], "unassigned": counts["unassigned"],
                       "ambiguous": sum(a.get("status") == "ambiguous" for a in atoms),
                       "errors": sum(row.get("status") == "processing_error" for row in rows)},
            "delivery_source": projection["source"] if projection is not None else "bertopic",
            "clusters": clusters, "records": records[(page - 1) * size:page * size],
            "page": page, "pages": pages, "total": len(records), "page_size": size,
            "clustered": "cluster" in data.get("completed_stages", []),
            "hierarchy": hierarchy if view == "hierarchy" else {key: value for key, value in hierarchy.items() if key != "nodes"},
            "topic_nodes": {node["node_id"]: {field: node[field] for field in ("node_id", "label", "depth", "is_leaf")} for node in hierarchy.get("nodes", [])},
            "pair_reviews": [p for p in data.get("pair_reviews", []) if cluster != "all" and
                             cluster in {str(p.get("left_cluster_id")), str(p.get("right_cluster_id"))}]}
