"""Final deliverable contains original sentences and their topic levels only."""
from collections import defaultdict
import copy

from intent_engine.atomic.topic_hierarchy import build_topic_tree, index_topic_tree


def build_sentence_results(data):
    atoms = data.get("intents", [])
    from intent_engine.exporting.delivery_topics import hierarchy_projection
    projection = hierarchy_projection(data)
    if projection is not None:
        by_row = defaultdict(lambda: defaultdict(dict))
        for atom in atoms:
            for level, node in enumerate(projection['assignments'].get(atom['intent_id'], {}).get('path', []), 1):
                by_row[atom['row_id']][level][node['node_id']] = node['label']
        depth = max((level for levels in by_row.values() for level, topics in levels.items() if topics), default=0)
        return {'records':[{'raw_text':row['raw_text'], 'topics_by_level':[
            {'level':level, 'topics':list(by_row[row['row_id']][level].values())} for level in range(1,depth+1)]}
            for row in data.get('rows', [])]}

    tree = copy.deepcopy(data.get("topic_hierarchy"))
    if not tree:
        # Historical flat results may expose their existing leaf topics, but
        # never invent missing parent themes or infer business dimensions.
        labels = [atom["cluster_id"] for atom in atoms if atom.get("status") == "valid" and atom.get("cluster_id") is not None]
        keywords = {cluster["cluster_id"]: cluster.get("keywords", []) for cluster in data.get("clusters", [])}
        tree = build_topic_tree(labels, keywords, status="not_generated")
    nodes, paths = index_topic_tree(tree)
    by_row = defaultdict(lambda: defaultdict(dict))
    for atom in atoms:
        if atom.get("status") != "valid" or atom.get("topic_pending"):
            continue
        for key in paths.get(atom.get("cluster_id"), []):
            node = nodes[key]
            by_row[atom["row_id"]][node["depth"] + 1][key] = node["label"]
    depth = max((level for levels in by_row.values() for level, topics in levels.items() if topics), default=0)
    return {"records": [{"raw_text": row["raw_text"], "topics_by_level": [
        {"level": level, "topics": list(by_row[row["row_id"]][level].values())}
        for level in range(1, depth + 1)]} for row in data.get("rows", [])]}


def sentence_result_tables(result):
    depth = max((item["level"] for record in result["records"] for item in record["topics_by_level"]), default=0)
    rows = []
    for record in result["records"]:
        levels = {item["level"]: [topic.get("label", "") if isinstance(topic, dict) else topic for topic in item["topics"]]
                  for item in record["topics_by_level"]}
        rows.append({"原始句子": record["raw_text"], **{f"第{level}层主题": "\n".join(levels.get(level, []))
                                                    for level in range(1, depth + 1)}})
    return {"原句结果": rows}


def write_sentence_results(prefix, data, *, directory=None):
    import pandas as pd
    from openpyxl.styles import Alignment
    from intent_engine.atomic.atomic_discovery import _write_json
    result = build_sentence_results(data)
    json_path = prefix.with_name(prefix.name + "_sentence_results.json")
    xlsx_path = prefix.with_name(prefix.name + "_sentence_results.xlsx")
    if directory is not None:
        json_path = directory / "sentence_results.json"
        xlsx_path = directory / "sentence_results.xlsx"
        directory.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        for name, rows in sentence_result_tables(result).items():
            pd.DataFrame(rows).to_excel(writer, sheet_name=name, index=False)
            sheet = writer.book[name]
            sheet.freeze_panes = "B2"
            sheet.auto_filter.ref = sheet.dimensions
            for column in sheet.columns:
                sheet.column_dimensions[column[0].column_letter].width = 55 if column[0].value == "原始句子" else 38
                for cell in column:
                    cell.alignment = Alignment(vertical="top", wrap_text=True)
                    if isinstance(cell.value, str):
                        cell.data_type = "s"
    _write_json(json_path, result)
    return {"sentence_results": str(json_path), "sentence_workbook": str(xlsx_path)}
