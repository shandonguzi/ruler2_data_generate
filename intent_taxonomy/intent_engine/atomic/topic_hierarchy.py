"""BERTopic merge tree and provenance-preserving memberships at every depth."""
from collections import defaultdict, deque
from dataclasses import dataclass
import math


@dataclass
class ClusteringResult:
    labels: list
    keywords: dict
    hierarchy: dict

    def __iter__(self):
        # Preserve the existing two-value adapter contract.
        yield self.labels
        yield self.keywords


def build_topic_tree(labels, keywords, merges=(), *, status=None, message="", linkage="average"):
    topics = sorted({int(label) for label in labels if label >= 0})
    nodes = {f"topic:{topic}": {"node_id": f"topic:{topic}", "topic_id": topic,
        "label": " · ".join(keywords.get(topic, [])[:5]) or f"主题 {topic}",
        "keywords": keywords.get(topic, []), "children": [], "is_leaf": True,
        "distance": None} for topic in topics}
    merges = list(merges)
    for row in merges:
        parent = int(row["Parent_ID"])
        if parent in topics or f"merge:{parent}" in nodes:
            raise ValueError("层级主题的父节点编号重复或与叶子冲突")
        children = [int(row["Child_Left_ID"]), int(row["Child_Right_ID"])]
        distance = float(row["Distance"])
        if not math.isfinite(distance) or distance < 0:
            raise ValueError("层级主题合并距离无效")
        name = str(row.get("Parent_Name", ""))
        nodes[f"merge:{parent}"] = {"node_id": f"merge:{parent}", "topic_id": None,
            "label": name.replace("_", " · ") or f"合并主题 {parent}", "keywords": name.split("_") if name else [],
            "children": [f"topic:{child}" if child in topics else f"merge:{child}" for child in children],
            "is_leaf": False, "distance": distance}
    tree = {"version": 1, "source": "BERTopic.hierarchical_topics", "representation": "c_tf_idf",
            "linkage": linkage, "status": status or ("ready" if merges else "single_topic" if len(topics) == 1 else "no_topics"),
            "message": message, "nodes": list(nodes.values())}
    index_topic_tree(tree, set(topics))
    return tree


def index_topic_tree(tree, expected_topics=None):
    """Validate a forest iteratively (deep, unbalanced merge trees are valid)."""
    entries = tree.get("nodes", [])
    nodes = {node["node_id"]: node for node in entries}
    if len(nodes) != len(entries):
        raise ValueError("层级主题节点编号重复")
    parents = {}
    leaves = {}
    for key, node in nodes.items():
        if not isinstance(key, str) or not key:
            raise ValueError("层级主题节点编号无效")
        children = node.get("children", [])
        if not isinstance(children, list) or not isinstance(node.get("is_leaf"), bool):
            raise ValueError("层级主题节点结构无效")
        if node.get("is_leaf"):
            topic = node.get("topic_id")
            if children or not isinstance(topic, int) or isinstance(topic, bool) or topic < 0 or topic in leaves:
                raise ValueError("层级主题叶子无效")
            leaves[topic] = key
        elif (len(children) < 2 if tree.get("source") == "BERTopic+LLM" else len(children) != 2) or len(set(children)) != len(children):
            raise ValueError("层级合并节点必须恰好包含两个不同子节点")
        elif isinstance(node.get("distance"), bool) or not isinstance(node.get("distance"), (int, float)) or not math.isfinite(node["distance"]) or node["distance"] < 0:
            raise ValueError("层级主题合并距离无效")
        for child in children:
            if child not in nodes or child in parents:
                raise ValueError("层级主题子节点缺失或被多个父节点引用")
            parents[child] = key
    if expected_topics is not None and set(leaves) != set(expected_topics):
        raise ValueError("层级主题叶子与实际聚类编号不一致")
    roots = sorted(set(nodes) - set(parents))
    if tree.get("status") == "ready" and len(roots) != 1:
        raise ValueError("完整主题树必须有一个根节点")
    queue = deque((root, 0) for root in roots)
    ordered = []
    while queue:
        key, depth = queue.popleft()
        node = nodes[key]
        node.update(parent_id=parents.get(key), depth=depth)
        ordered.append(key)
        queue.extend((child, depth + 1) for child in node.get("children", []))
    if len(ordered) != len(nodes):
        raise ValueError("层级主题包含循环或不可达节点")
    for key in reversed(ordered):
        node = nodes[key]
        node["leaf_topic_ids"] = [node["topic_id"]] if node["is_leaf"] else sorted(
            topic for child in node["children"] for topic in nodes[child]["leaf_topic_ids"])
    tree.update(root_ids=roots, max_depth=max((node["depth"] + 1 for node in entries), default=0),
                leaf_count=len(leaves))
    paths = {}
    for topic, key in leaves.items():
        path = []
        while key is not None:
            path.append(key)
            key = parents.get(key)
        paths[topic] = list(reversed(path))
    return nodes, paths


def attach_topic_memberships(data, *, validate_existing=False):
    tree = data.get("topic_hierarchy")
    if not tree:
        return
    expected = {a["cluster_id"] for a in data.get("intents", []) if a.get("cluster_id") is not None and a["cluster_id"] >= 0}
    if any(a.get("status") != "valid" and a.get("cluster_id") is not None and a["cluster_id"] >= 0 for a in data.get("intents", [])):
        raise ValueError("待确认或无效意图不能分配叶子主题")
    nodes, paths = index_topic_tree(tree, expected)
    members, unique, source_rows = defaultdict(int), defaultdict(set), defaultdict(set)
    by_row = defaultdict(list)
    for atom in data.get("intents", []):
        path = paths.get(atom.get("cluster_id"), []) if atom.get("status") == "valid" else []
        if validate_existing and "topic_path" in atom and atom["topic_path"] != path:
            raise ValueError("原子意图的层级主题路径与聚类不一致")
        atom["topic_path"] = path
        by_row[atom["row_id"]].append(atom)
        for key in path:
            members[key] += 1
            unique[key].add(atom["text"])
            source_rows[key].add(atom["row_id"])
    for key, node in nodes.items():
        node.update(intent_count=members[key], unique_text_count=len(unique[key]), row_count=len(source_rows[key]))
    for row in data.get("rows", []):
        row_paths, origins = [], defaultdict(list)
        for atom in by_row[row["row_id"]]:
            if atom["topic_path"]:
                row_paths.append({"intent_id": atom["intent_id"], "node_ids": atom["topic_path"]})
            for key in atom["topic_path"]:
                origins[key].append(atom["intent_id"])
        memberships = [{"node_id": key, "level": nodes[key]["depth"] + 1, "label": nodes[key]["label"],
                        "intent_ids": ids} for key, ids in sorted(origins.items(), key=lambda item: (nodes[item[0]]["depth"], item[0]))]
        if validate_existing and ("topic_paths" in row and row["topic_paths"] != row_paths or
                                 "topic_memberships" in row and row["topic_memberships"] != memberships):
            raise ValueError("原句的多层主题关联与原子意图路径不一致")
        row.update(topic_paths=row_paths, topic_memberships=memberships)


def hierarchy_export_tables(data):
    tree = data.get("topic_hierarchy", {})
    nodes = {node["node_id"]: node for node in tree.get("nodes", [])}
    paths = []
    for atom in data.get("intents", []):
        for key in atom.get("topic_path", []):
            node = nodes[key]
            paths.append({"row_id": atom["row_id"], "intent_id": atom["intent_id"], "text": atom["text"],
                          "level": node["depth"] + 1, "node_id": key, "topic_label": node["label"],
                          "leaf_topic_id": atom["cluster_id"], "is_leaf": node["is_leaf"]})
    rows = [{"row_id": row["row_id"], "raw_text": row["raw_text"], **membership}
            for row in data.get("rows", []) for membership in row.get("topic_memberships", [])]
    return {"主题层级": list(nodes.values()), "意图主题路径": paths, "原句多层主题": rows}
