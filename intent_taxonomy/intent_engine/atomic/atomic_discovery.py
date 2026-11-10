"""Schema-free atomic action discovery. No taxonomy or Skill registry is read.

Mixed clusters are refined with bounded local splits and independent re-review.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from intent_engine.infrastructure.excel_io import load_excel
from intent_engine.infrastructure.llm_client import LocalLLM, _ensure_local_no_proxy
from intent_engine.infrastructure.progress import ProgressReporter
from intent_engine.infrastructure.model_requests import USAGE, request_extra_body, tracked_request, usage_since


from intent_engine.settings.prompt_settings import prompt_ref


EXTRACT_PROMPT = prompt_ref("atomic_discovery.EXTRACT_PROMPT")

REVIEW_PROMPT = prompt_ref("atomic_discovery.REVIEW_PROMPT")

PAIR_PROMPT = prompt_ref("atomic_discovery.PAIR_PROMPT")



class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AtomicAction(StrictModel):
    span: str = Field(min_length=1)
    text: str = Field(min_length=1)
    object: str = ""
    action: str = ""
    status: Literal["valid", "ambiguous"] = "valid"
    context_indices: list[int] = Field(default_factory=list)
    reason: str = ""


class ActionRelation(StrictModel):
    source: int
    target: int
    kind: Literal["before", "depends_on", "parallel"]
    reason: str = Field(min_length=1)


class AtomicExtraction(StrictModel):
    status: Literal["valid", "ambiguous", "invalid"]
    reason: str = ""
    intents: list[AtomicAction] = Field(default_factory=list)
    relations: list[ActionRelation] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_structure(self):
        if (self.status == "invalid") != (len(self.intents) == 0):
            raise ValueError("invalid 必须无意图，其他状态必须有意图")
        n = len(self.intents)
        for i, intent in enumerate(self.intents):
            if any(j < 0 or j >= n or j == i for j in intent.context_indices):
                raise ValueError("无效上下文索引")
            if intent.text != intent.span and (not intent.context_indices or not intent.reason.strip()):
                raise ValueError("改写只能用于有原文依据的上下文补全")
        for relation in self.relations:
            if not (0 <= relation.source < n and 0 <= relation.target < n) or relation.source == relation.target:
                raise ValueError("无效动作关系")
        return self


class SplitGroup(StrictModel):
    label: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class ClusterReview(StrictModel):
    verdict: Literal["consistent", "mixed", "uncertain"]
    semantic_clear: bool | None = None
    object: str = ""
    action: str = ""
    reason: str = Field(min_length=1)
    split_groups: list[SplitGroup] = Field(default_factory=list)
    boundary_ids: list[str] = Field(default_factory=list)


class PairReview(StrictModel):
    decision: Literal["merge", "keep", "uncertain"]
    reason: str = Field(min_length=1)
    left_evidence_ids: list[str] = Field(min_length=1)
    right_evidence_ids: list[str] = Field(min_length=1)






def validate_extraction(raw_text: str, result: AtomicExtraction):
    """Offsets refer to the real source, including repeated phrases in order."""
    offsets = []
    cursor = 0
    for intent in result.intents:
        start = raw_text.find(intent.span, cursor)
        if start < 0:
            raise ValueError("原子意图 span 不是按顺序排列的原文片段")
        end = start + len(intent.span)
        offsets.append((start, end))
        cursor = end
    return offsets


def check_evidence(review: ClusterReview, allowed: set[str]):
    evidence = list(review.boundary_ids)
    for group in review.split_groups:
        evidence += group.evidence_ids
    if not set(evidence) <= allowed:
        raise ValueError("审核引用了未提供的样本")
    if review.verdict == "mixed":
        ids = [i for g in review.split_groups for i in g.evidence_ids]
        if len(review.split_groups) < 2 or len(ids) != len(set(ids)):
            raise ValueError("拆分至少需要两组互不重复的真实样本证据")
    if review.verdict == "consistent" and review.semantic_clear is False:
        raise ValueError("语义不明确不能同时判为一致簇")
    if review.verdict == "consistent" and (not review.object.strip() or not review.action.strip()):
        raise ValueError("一致簇必须归纳对象和动作")


def singleton_review(members, semantic_records):
    """Skip within-cluster comparison for one distinct text; reuse actual parsing only."""
    if semantic_records is None or len({m['text'] for m in members}) != 1:
        return None
    by_id = {r['intent_id']:r for r in semantic_records}
    pairs = set()
    complete = True
    for member in members:
        record = by_id.get(member['intent_id'])
        if (not record or record.get('error') or any(record.get(k) != member.get(k)
                for k in ('text','span','raw_text','row_id'))):
            complete = False
            continue
        obj, action = record.get('object','').strip(), record.get('action','').strip()
        if not obj or not action:
            complete = False
        pairs.add((obj,action))
    clear = complete and len(pairs)==1
    obj, action = next(iter(pairs)) if clear else ('','')
    return ClusterReview(verdict='uncertain', semantic_clear=clear, object=obj, action=action,
        reason='仅有一条不同原子文本，跳过簇一致性请求；复用本轮已解析的对象和动作，支持量低，仍需叶子命名审核。'
               if clear else '仅有一条不同原子文本，跳过簇一致性请求；缺少可靠或一致的语义解析，保留待确认。')


def review_eligible(cluster):
    if cluster.get("review_error"):
        return False
    review = cluster.get("review") or {}
    return (review.get("verdict") == "consistent" and review.get("semantic_clear") is not False
            and bool(review.get("object", "").strip()) and bool(review.get("action", "").strip())) or (
        cluster.get("unique_text_count") == 1 and review.get("verdict") == "uncertain"
        and review.get("semantic_clear") is True and bool(review.get("object", "").strip())
        and bool(review.get("action", "").strip()) and not cluster.get("review_error"))


def extraction_issues(text, parsed):
    issues = []
    if parsed.status == "invalid" and (re.search(r"[?？]", text) or re.match(r"(?i)\s*(what|who|which|where|when|why|how)\b", text)):
        issues.append("这是疑问表达，请检查是否误把有效知识问答当作没有可执行动作；信息查询也是意图。")
    for atom in parsed.intents:
        if atom.status == 'valid' and (not atom.object.strip() or not atom.action.strip()):
            issues.append('有效原子意图缺少 object 或 action，请在拆分时一并归纳对象和动作；不能确定时保留 ambiguous，不猜测。')
        if atom.context_indices and re.search(r"(?i)\b(it|its|they|their|them)\b|它|他们|她们", atom.text):
            issues.append("存在 context_indices，但 text 仍有指代；必须根据原文补全对象，不生成原文没有的内容。")
    return issues


def embedding_settings(cfg: dict) -> dict:
    settings = dict(cfg.get("embedding", {}))
    settings["base_url"] = (settings.get("base_url") or "").strip()
    settings["model"] = (settings.get("model") or "").strip()
    if not settings["base_url"] or not settings["model"]:
        raise ValueError("请先在 config/config.yaml 的 embedding 中配置 base_url 和 model")
    return settings


def check_dependencies():
    if any(importlib.util.find_spec(name) is None for name in ("bertopic", "jieba")):
        raise ValueError("缺少 BERTopic 依赖，请运行 uv pip install -r requirements_clustering.txt")


def _write_json(path: Path, value):
    from intent_engine.infrastructure.atomic_json import write_json_atomically
    write_json_atomically(path, value, indent=2)


class SemanticEmbeddings:
    def __init__(self, cfg: dict, cache_dir: Path):
        from openai import OpenAI
        self.cfg = embedding_settings({"embedding": cfg})
        _ensure_local_no_proxy(self.cfg["base_url"])
        api_key = os.environ.get(cfg.get("api_key_env") or "INTENT_EMBEDDING_API_KEY") or cfg.get("api_key") or "EMPTY"
        self.client = OpenAI(base_url=self.cfg["base_url"], api_key=api_key,
                             timeout=cfg.get("timeout", 120), max_retries=0)
        self.cache_dir = cache_dir

    def encode(self, texts: list[str], progress: ProgressReporter):
        identity = {k: self.cfg.get(k) for k in ("base_url", "model", "dimensions", "cache_version", "extra_body")}
        key = hashlib.sha256(json.dumps([identity, texts], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        cache = self.cache_dir / (key + ".json")
        vectors = None
        if cache.exists():
            try:
                vectors = self._validate(json.loads(cache.read_text(encoding="utf-8")), len(texts))
            except (ValueError, TypeError):
                pass
        if vectors is not None:
            progress.update(len(texts), len(texts), cached=len(texts))
            return vectors
        rows = []
        size = max(1, int(self.cfg.get("batch_size", 64)))
        for start in range(0, len(texts), size):
            batch = texts[start:start + size]
            kwargs = {"model": self.cfg["model"], "input": batch}
            if self.cfg.get("dimensions"):
                kwargs["dimensions"] = int(self.cfg["dimensions"])
            extra = request_extra_body(self.cfg, thinking=False)
            if extra:
                kwargs["extra_body"] = extra
            for attempt in range(int(self.cfg.get("max_retries", 2)) + 1):
                try:
                    response = tracked_request("embedding", self.cfg, self.client.embeddings.create, **kwargs)
                    break
                except Exception as error:
                    status = getattr(error, "status_code", None)
                    if attempt == int(self.cfg.get("max_retries", 2)) or (status is not None and 400 <= status < 500 and status != 429):
                        raise
            data = sorted(response.data, key=lambda item: item.index)
            if [item.index for item in data] != list(range(len(batch))):
                raise ValueError("Embedding 服务返回的索引不完整或重复")
            rows.extend(item.embedding for item in data)
            progress.update(len(rows), len(texts))
        vectors = self._validate(rows, len(texts))
        _write_json(cache, vectors.tolist())
        return vectors

    @staticmethod
    def _validate(rows, count):
        import numpy as np
        vectors = np.asarray(rows, dtype=float)
        if vectors.ndim != 2 or vectors.shape[0] != count or vectors.shape[1] < 1:
            raise ValueError("Embedding 数量或维度不正确")
        if not np.isfinite(vectors).all() or (np.linalg.norm(vectors, axis=1) == 0).any():
            raise ValueError("Embedding 包含非有限值或零向量")
        return vectors


def chinese_tokens(text: str):
    import jieba
    tokens = [token.strip() for token in jieba.lcut(text) if re.search(r"\w", token)]
    return tokens or ["空文本"]


def english_tokens(text: str):
    # Keep explicit negation, including contractions, as a real topic feature.
    text = text.lower().replace("’", "'")
    text = re.sub(r"\bcan't\b", "can not", text)
    text = re.sub(r"\bwon't\b", "will not", text)
    text = re.sub(r"n't\b", " not", text)
    return re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text)


def topic_vectorizer(cfg):
    """Language-specific c-TF-IDF only; semantic embeddings retain full text."""
    from sklearn.feature_extraction.text import CountVectorizer, ENGLISH_STOP_WORDS
    language = cfg.get("topic_language", "chinese")
    if language not in {"english", "chinese"}:
        raise ValueError("主题语言必须为 english 或 chinese")
    ranges = cfg.get("topic_ngram_ranges", {})
    if not isinstance(ranges, dict):
        raise ValueError("topic_ngram_ranges 必须是映射")
    ngrams = ranges.get(language, [1, 2] if language == "english" else [1, 1])
    if not isinstance(ngrams, (list, tuple)) or list(ngrams) not in ([1, 1], [1, 2], [1, 3], [2, 2]):
        raise ValueError("主题 n-gram 仅支持 [1,1]、[1,2]、[1,3]、[2,2]")
    words = set(ENGLISH_STOP_WORDS) if cfg.get("topic_english_stop_words", True) else set()
    # sklearn also removes some intent-bearing verbs and particles by default.
    words -= set("no nor not never cannot without call get find give go keep make move put send show take back off on up down out over under before after".split())
    if language == "chinese" and cfg.get("topic_chinese_stop_words", True):
        words.update("的 了 呢 吗 啊 呀 吧 在 从 向 于 把 被 和 与 以及 请 帮 帮我 我 一下".split())
    custom = cfg.get("topic_stop_words_by_language", {}).get(language, cfg.get("topic_stop_words", []))
    if not isinstance(custom, list) or any(not isinstance(word, str) for word in custom):
        raise ValueError("主题停用词必须是字符串列表")
    tokenizer = english_tokens if language == "english" else chinese_tokens
    words.update(token for word in custom for token in tokenizer(word.lower()))
    return CountVectorizer(tokenizer=tokenizer, token_pattern=None, lowercase=True,
                           ngram_range=tuple(ngrams), stop_words=sorted(words) or None)


def cluster_atomic_texts(texts, embeddings, cfg):
    """One document per distinct atomic text; repeated rows retain provenance."""
    from bertopic import BERTopic
    from hdbscan import HDBSCAN
    from umap import UMAP
    from intent_engine.atomic.clustering_compat import configure_clustering_compat
    from intent_engine.atomic.topic_hierarchy import ClusteringResult, build_topic_tree
    configure_clustering_compat()
    n = len(texts)
    minimum = max(2, int(cfg.get("min_cluster_size", 5)))
    if n < max(3, minimum):
        labels = [-1] * n
        return ClusteringResult(labels, {}, build_topic_tree(labels, {}, message="不同原子文本过少，未生成候选主题"))
    vectorizer = topic_vectorizer(cfg)
    if not any(vectorizer.build_analyzer()(text) for text in texts):
        labels = [-1] * n
        return ClusteringResult(labels, {}, build_topic_tree(labels, {}, message="停用词过滤后没有可用主题词，保留原句为噪声"))
    topic_model = BERTopic(
        language="multilingual", embedding_model=None,
        umap_model=UMAP(n_neighbors=min(int(cfg.get("n_neighbors", 15)), n - 1),
                        n_components=min(5, n - 2), metric="cosine", init="random",
                        random_state=int(cfg.get("random_state", 42))),
        hdbscan_model=HDBSCAN(min_cluster_size=minimum, min_samples=int(cfg.get("min_samples", 2)),
                             metric="euclidean", cluster_selection_method="eom"),
        vectorizer_model=vectorizer,
        calculate_probabilities=False, verbose=False,
    )
    labels, _ = topic_model.fit_transform(texts, embeddings=embeddings)
    keywords = {int(label): [word for word, _ in topic_model.get_topic(label) or [] if word.strip()]
                for label in set(labels) if label != -1}
    labels = [int(label) for label in labels]
    linkage = cfg.get("hierarchy_linkage", "average")
    if linkage not in {"average", "complete", "single"}:
        raise ValueError("层级聚类 linkage 仅支持 average、complete 或 single（余弦距离）")
    if len(keywords) < 2:
        tree = build_topic_tree(labels, keywords, linkage=linkage)
    elif cfg.get("flat_topics",False) or not cfg.get("hierarchical_topics", True):
        tree = build_topic_tree(labels, keywords, status="disabled", message="本次未启用层级主题合并", linkage=linkage)
    else:
        try:
            from scipy.cluster.hierarchy import linkage as topic_linkage
            hierarchy = topic_model.hierarchical_topics(texts, use_ctfidf=True,
                linkage_function=lambda distances: topic_linkage(distances, method=linkage, optimal_ordering=True))
            tree = build_topic_tree(labels, keywords, hierarchy.to_dict(orient="records"), linkage=linkage)
        except Exception as exc:
            tree = build_topic_tree(labels, keywords, status="failed", message=f"BERTopic 层级主题生成失败：{exc}", linkage=linkage)
    return ClusteringResult(labels, keywords, tree)


def review_sample(members, vectors, limit):
    """Show centroid representatives plus distant boundary examples, deterministically."""
    import numpy as np
    unique = {}
    for member in members:
        unique.setdefault(member["text"], member)
    candidates = list(unique.values())
    matrix = np.asarray([vectors[m["text_index"]] for m in candidates])
    matrix = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)
    center = matrix.mean(axis=0)
    order = np.argsort(-(matrix @ center), kind="stable").tolist()
    selected = []
    while order and len(selected) < limit:
        selected.append(candidates[order.pop(0)])
        if order and len(selected) < limit:
            selected.append(candidates[order.pop()])
    return [{k: m[k] for k in ("intent_id", "text", "span", "raw_text")} for m in selected]


def consistency_review_payload(cluster, data=None):
    from intent_engine.atomic.topic_context import topic_context
    """Project audit evidence for the model; keep full samples in the artifact."""
    metadata = ("cluster_id", "parent_cluster_id", "refinement_round", "sample_count",
                "unique_text_count", "keywords", "review_scope")
    return {
        **{key: cluster[key] for key in metadata if key in cluster},
        "topic_context": topic_context(data, cluster["cluster_id"], cluster.get("parent_cluster_id")),
        "members": [{"intent_id": sample["intent_id"], "text": sample["text"]}
                    for sample in cluster["review_samples"]],
    }


def neighbor_pairs(clusters, vectors, top_k=3):
    import numpy as np
    ids = sorted(clusters)
    if len(ids) < 2:
        return []
    centers = []
    for label in ids:
        indices = sorted({m["text_index"] for m in clusters[label]})
        matrix = vectors[indices]
        center = (matrix / np.linalg.norm(matrix, axis=1, keepdims=True)).mean(axis=0)
        centers.append(center / max(np.linalg.norm(center), 1e-12))
    centers = np.asarray(centers)
    pairs = set()
    for i, scores in enumerate(centers @ centers.T):
        others = [j for j in np.argsort(-scores, kind="stable") if j != i][:top_k]
        pairs.update(tuple(sorted((ids[i], ids[j]))) for j in others)
    return sorted(pairs)


WORKFLOW_STAGES = {
    "extract": "原子意图分析", "cluster": "无监督聚类", "review": "簇一致性审核",
    "pairs": "跨簇边界复核", "domains": "逐层命名与合并审核", "iterate": "候选体系迭代", "export": "统计与导出",
}
STAGE_REQUIRED = {"extract": None, "cluster": "extract", "review": "cluster", "pairs": "cluster",
                  "domains": "review", "iterate": "domains", "export": "extract"}


def read_stage_source(path: str | Path, stage: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate_stage_source(data, stage)


def validate_stage_source(data: dict, stage: str) -> dict:
    """Validate actual stage inputs; completion labels alone never establish usability."""
    if stage not in WORKFLOW_STAGES:
        raise ValueError("不支持的流程阶段")
    if not isinstance(data, dict) or data.get("mode") != "atomic_actions" or not data.get("run_id"):
        raise ValueError("请选择本流程生成的原子意图中间结果 JSON")
    if not isinstance(data.get("completed_stages"), list) or any(key not in WORKFLOW_STAGES for key in data["completed_stages"]):
        raise ValueError("中间结果的阶段记录无效")
    required = STAGE_REQUIRED[stage]
    if not isinstance(data.get("rows"), list) or not data["rows"]:
        raise ValueError("中间结果缺少原句记录")
    rows = {r["row_id"]: r for r in data["rows"]}
    if len(rows) != len(data["rows"]):
        raise ValueError("中间结果原句编号重复")
    if any(not isinstance(row.get("raw_text"), str) for row in rows.values()):
        raise ValueError("中间结果原句格式错误")
    if stage != "extract":
        intents = data.get("intents")
        if not isinstance(intents, list):
            raise ValueError("中间结果缺少原子意图")
        ids = set()
        for atom in intents:
            if atom["intent_id"] in ids or atom["row_id"] not in rows:
                raise ValueError("原子意图编号重复或无法关联原句")
            ids.add(atom["intent_id"])
            action = AtomicAction.model_validate({k: atom[k] for k in AtomicAction.model_fields if k in atom})
            raw = rows[atom["row_id"]]["raw_text"]
            if raw[atom["start"]:atom["end"]] != action.span or atom["raw_text"] != raw:
                raise ValueError("原子意图与原句证据不一致")
        if required in {"cluster", "review", "domains"}:
            atoms_by_id = {atom["intent_id"]: atom for atom in intents}
            if not isinstance(data.get("clusters"), list):
                raise ValueError("中间结果缺少聚类记录")
            labels = set()
            for cluster in data["clusters"]:
                label = cluster["cluster_id"]
                if label in labels or label < 0:
                    raise ValueError("聚类编号无效或重复")
                labels.add(label)
                samples = cluster["review_samples"]
                if not samples:
                    raise ValueError("聚类缺少真实样本证据")
                for sample in samples:
                    atom = atoms_by_id.get(sample["intent_id"])
                    if not atom or atom["cluster_id"] != label or any(sample[key] != atom[key] for key in ("text", "span", "raw_text")):
                        raise ValueError("聚类样本与原子意图不一致")
                if stage not in {"review", "pairs"} and cluster.get("review"):
                    check_evidence(ClusterReview.model_validate({k: v for k, v in cluster["review"].items() if k != "evidence_ids"}), {sample["intent_id"] for sample in samples})
            pairs = data.get("neighbor_pairs", [])
            invalid_pairs = not isinstance(pairs, list) or any(
                not isinstance(pair, (list, tuple)) or len(pair) != 2
                or not all(isinstance(label,int) and not isinstance(label,bool) for label in pair)
                or pair[0] == pair[1] or pair[0] not in labels or pair[1] not in labels
                for pair in pairs)
            if invalid_pairs:
                if stage != "iterate":
                    raise ValueError("相邻簇引用无效")
                # Neighbor evidence is derived state, not an input to iteration.
                # Clear the entire old graph and its reviews rather than retain
                # partially valid pairs from a different membership snapshot.
                data["neighbor_pairs"] = []
                data["pair_reviews"] = []
                data.setdefault("input_repairs", []).append({
                    "stage":"iterate", "kind":"stale_neighbor_graph",
                    "reason":"簇结构已变化，清理过期相邻关系及其审核记录"})
    valid = [atom for atom in data.get("intents", []) if atom.get("status") == "valid"]
    if stage == "cluster" and not valid:
        raise ValueError("没有可用于聚类的有效原子意图")
    if stage in {"review", "pairs", "domains", "iterate"}:
        clusters = data.get("clusters", [])
        if not clusters:
            raise ValueError("尚未完成可用聚类：缺少非噪声簇及成员证据，无法生成候选层级")
        counts = {cluster["cluster_id"]: 0 for cluster in clusters}
        allowed_labels = set(counts) | {-1}
        for atom in valid:
            label = atom.get("cluster_id")
            if label not in allowed_labels:
                raise ValueError("有效原子意图缺少合法的聚类归属")
            if label in counts:
                counts[label] += 1
        if any(count == 0 for count in counts.values()):
            raise ValueError("存在没有有效成员的聚类")
        if stage == "pairs" and len(clusters) < 2:
            raise ValueError("跨簇边界复核至少需要两个非噪声簇")
        if stage == "pairs" and not data.get("neighbor_pairs"):
            raise ValueError("缺少可复核的相邻簇对，请先生成聚类邻居关系")
        if stage in {"domains", "iterate"} and not any(review_eligible(c) for c in clusters):
            raise ValueError("没有审核合格的簇，无法生成或继续候选层级")
    if stage == "iterate":
        from intent_engine.models import Taxonomy
        from intent_engine.atomic.topic_hierarchy import index_topic_tree
        taxonomy = Taxonomy.model_validate(data.get("candidate_taxonomy") or {})
        if not taxonomy.tasks:
            raise ValueError("候选层级没有可用的叶子任务")
        tree = data.get("topic_hierarchy")
        if not tree or tree.get("source") != "BERTopic+LLM":
            raise ValueError("缺少逐层审核后的主题树，请先执行主题树审核")
        nodes, _ = index_topic_tree(tree)
        mapping = data.get("task_topic_nodes") or {}
        cluster_tasks = data.get("cluster_task_ids") or {}
        for task in taxonomy.tasks:
            node = nodes.get(mapping.get(task.task_id))
            if not node or not node.get("is_leaf") or node.get("semantic_status") != "approved":
                raise ValueError("叶子任务没有关联到已批准的主题节点")
            if cluster_tasks.get(str(node.get("topic_id"))) != task.task_id:
                raise ValueError("叶子任务与聚类的映射不一致")
    if stage not in {"extract", "cluster"} and data.get("topic_hierarchy"):
        from intent_engine.atomic.topic_hierarchy import attach_topic_memberships
        attach_topic_memberships(data, validate_existing=True)
    return data


def run_atomic_discovery(cfg: dict, input_path: str | None = None, *, text_column=None, sheet_name=None,
                         limit=None, workers=4, review=True, stage="all", source_path=None, iteration_rounds=None, iteration_scope=None):
    import copy
    if stage != "all" and stage not in WORKFLOW_STAGES:
        raise ValueError("不支持的流程阶段")
    if (limit is not None and limit < 1) or workers < 1:
        raise ValueError("limit 和 workers 必须大于零")
    if stage not in {"all", "extract"} and not source_path:
        raise ValueError("阶段工具必须选择已有流程的中间结果")
    source = read_stage_source(source_path, "extract" if stage == "all" else stage) if source_path else None
    if not source and not input_path:
        raise ValueError("请选择原句输入文件")
    if source and input_path and stage != "iterate":
        raise ValueError("追加文件仅支持继续迭代")
    settings = embedding_settings(cfg) if stage in {"all", "cluster"} else None
    if settings:
        check_dependencies()
    options = dict(cfg.get("atomic_discovery", {}))
    from intent_engine.atomic.topic_mode import apply_topic_mode
    apply_topic_mode(options,cfg)
    from intent_engine.infrastructure.iteration_scope import normalize_iteration_scope
    options['iteration_scope'] = normalize_iteration_scope(iteration_scope or options.get('iteration_scope','unresolved_only'))
    from intent_engine.infrastructure.iteration_stop import resolve_rounds
    iteration_rounds,automatic=resolve_rounds(options.get("iteration_rounds",0) if iteration_rounds is None else iteration_rounds,options)
    options.update(iteration_auto=automatic,iteration_rounds=-1 if automatic else iteration_rounds)
    if stage == "iterate" and not iteration_rounds:
        raise ValueError("总流程迭代轮数需为 0–10，独立迭代至少 1 轮")
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    from intent_engine.workflows.workflow_layout import create_workflow_directory
    paths = create_workflow_directory(Path(cfg["paths"]["reports_dir"]), run_id)
    import os
    os.environ["INTENT_TRACE_RUN_ID"] = run_id
    result_path, manifest_path = paths["report"], paths["manifest"]
    result = copy.deepcopy(source) if source else {}
    usage_start = USAGE.snapshot()
    start_rank = list(WORKFLOW_STAGES).index(stage) if stage != "all" else 0
    completed = [key for key in result.get("completed_stages", []) if list(WORKFLOW_STAGES).index(key) < start_rank]
    result.update(run_id=run_id, job_id=os.environ.get("INTENT_JOB_ID"), status="running", mode="atomic_actions", created_at=datetime.now().isoformat(),
                  parameters=options, requested_stage=stage, review_enabled=bool(review),
                  completed_stages=completed, artifacts={}, errors=[], adjustments_applied=bool(source and source.get("adjustments_applied") and stage not in {"all", "extract", "cluster"}),
                  source={"run_id": source["run_id"], "stage": source.get("stage"), "path": str(Path(source_path).resolve())} if source else None)
    result.pop("summary", None)
    result.pop("delivery_topics", None)
    result.pop("stage", None)
    if stage != "export":
        if os.environ.get("INTENT_RESUME_STATE") == "1" and stage == "iterate":
            result["resume_iteration"] = copy.deepcopy(result.get("iteration", {}))
        result.pop("iteration", None)
        result["taxonomy_updates_applied"] = False
    result["stage_outcomes"] = {key: value for key, value in result.get("stage_outcomes", {}).items()
                                if stage == "export" or key in completed}
    result["embedding"] = ({k: settings.get(k) for k in ("base_url", "model", "dimensions", "cache_version")}
                           if settings else result.get("embedding", {}))
    if source:
        items = [(r["row_id"], r["raw_text"]) for r in source["rows"]]
    else:
        column = text_column or cfg["pipeline"]["text_column"]
        df = load_excel(input_path, column, sheet_name)
        if limit:
            df = df.head(limit)
        items = [(i + 1, str(text)) for i, text in enumerate(df[column].tolist())]
        result["input"] = {"path": str(Path(input_path).resolve()), "column": column, "sheet": sheet_name}
    if stage in {"all", "extract"}:
        result.update(rows=[], intents=[])
    if stage in {"all", "extract", "cluster"}:
        result["clusters"] = []
        result["neighbor_pairs"] = []
        result.pop("topic_hierarchy", None)
        result.pop("bertopic_hierarchy", None)
        result.pop("hierarchy_review", None)
        result.pop("task_topic_nodes", None)
        result.pop("cluster_refinement", None)
        for atom in result.get("intents", []):
            atom.pop("topic_path", None)
            atom.pop("topic_pending", None)
        for row in result.get("rows", []):
            row.pop("topic_paths", None)
            row.pop("topic_memberships", None)
    if stage in {"all", "extract", "cluster", "review", "pairs"}:
        result["pair_reviews"] = []
    if stage not in {"export", "iterate"}:
        result["candidate_taxonomy"] = {"version": 1, "domains": [], "tasks": []}
        result.pop("cluster_task_ids", None)
        result.pop("task_aliases", None)
        result.pop("leaf_merging", None)
    result.setdefault("clusters", [])
    result.setdefault("pair_reviews", [])
    result.setdefault("candidate_taxonomy", {"version": 1, "domains": [], "tasks": []})
    # Preserve upstream operational errors when deriving a new version.
    if source:
        result["errors"] = [e for e in source.get("errors", []) if e.get("stage") in completed]
    execute = set(WORKFLOW_STAGES) if stage == "all" else {stage, "export"}
    if stage == "all" and not review:
        execute -= {"review", "pairs", "domains", "iterate"}
        result["stage_outcomes"].update({key: {"status": "skipped", "message": "本次显式关闭审核（--no-review）"}
                                         for key in ("review", "pairs", "domains", "iterate")})
    progress_steps = []
    for key, label in WORKFLOW_STAGES.items():
        if key in execute:
            if key == "iterate" and iteration_rounds:
                from intent_engine.workflows.hierarchy_iteration import hierarchy_iteration_steps
                progress_steps.extend(hierarchy_iteration_steps(iteration_rounds,options.get('flat_topics',False)))
                continue
            if key == "cluster":
                progress_steps.append({"id": "embed", "label": "计算原子意图向量"})
            progress_steps.append({"id": key, "label": "簇命名与语义审核" if key=="domains" and options.get("flat_topics",False) else label})
            if key == "domains":
                progress_steps.append({"id":"leaf_merge_initial","label":"初版叶子实例泛化与归并"})
    reporter = ProgressReporter()
    if source and input_path and stage == "iterate":
        progress_steps.insert(0, {"id":"append_extract", "label":"追加用例：原子意图分析"})
    reporter.plan(progress_steps)
    llm = LocalLLM(cfg["llm"]) if execute & {"extract", "review", "pairs", "domains", "iterate"} else None

    def persist():
        result["token_usage"] = usage_since(usage_start)
        result["artifacts"]["report"] = str(result_path)
        _write_json(result_path, result)
        manifest = {k: result.get(k) for k in ("run_id", "job_id", "created_at", "status", "stage", "input", "source", "summary", "completed_stages", "stage_outcomes", "artifacts", "token_usage")}
        manifest["requested_stage"] = stage
        manifest["iteration"] = {key: result.get("iteration", {}).get(key) for key in ("status", "max_rounds", "stop_reason", "stop_message", "converged")}
        manifest["iteration"]["rounds_completed"] = len(result.get("iteration", {}).get("rounds", []))
        _write_json(manifest_path, manifest)

    def finish(key, status="completed", message="", **counts):
        reporter.complete(status)
        result["stage_outcomes"][key] = {"status": status, "message": message, **counts}
        if status in {"completed", "completed_with_errors"} and key not in result["completed_stages"]:
            result["completed_stages"].append(key)
        result["stage"] = key
        artifact = paths[key]
        result["artifacts"][key] = str(artifact)
        result["token_usage"] = usage_since(usage_start)
        _write_json(artifact, result)
        persist()

    def extract(item):
        row_number, text = item
        row = {"row_id": row_number, "raw_text": text, "status": "invalid", "reason": "空文本", "relations": []}
        if not text.strip():
            return row, []
        try:
            parsed = llm.chat_json(EXTRACT_PROMPT, json.dumps({"user_text": text}, ensure_ascii=False),
                                   schema=AtomicExtraction, max_tokens=options.get("extract_max_tokens", 2400))
            validate_extraction(text, parsed)
            issues = extraction_issues(text, parsed)
            if issues:
                parsed = llm.chat_json(EXTRACT_PROMPT, json.dumps({"user_text": text, "previous": parsed.model_dump(),
                    "validation_feedback": issues}, ensure_ascii=False), schema=AtomicExtraction,
                    max_tokens=options.get("extract_max_tokens", 2400))
                validate_extraction(text, parsed)
                row["extraction_rechecked"] = True
            if any(a.status=='valid' and (not a.object.strip() or not a.action.strip()) for a in parsed.intents):
                raise ValueError('原子意图提取缺少对象或动作，复核后仍未补全')
            # A single unchanged fragment has no sibling intent: retain its full grounded context.
            if len(parsed.intents) == 1:
                atom = parsed.intents[0]
                if not atom.context_indices and atom.text == atom.span:
                    atom.span = atom.text = text
            for atom in parsed.intents:
                if atom.context_indices and re.search(r"(?i)\b(it|its|they|their|them)\b|它|他们|她们", atom.text):
                    atom.status = "ambiguous"
                    atom.reason += "；复核后仍未补全指代，保留待确认"
            offsets = validate_extraction(text, parsed)
            row.update(status=parsed.status, reason=parsed.reason, relations=[r.model_dump() for r in parsed.relations])
            atoms = [{**atom.model_dump(), "intent_id": f"r{row_number}:i{i}", "row_id": row_number,
                      "raw_text": text, "start": start, "end": end, "cluster_id": None}
                     for i, (atom, (start, end)) in enumerate(zip(parsed.intents, offsets))]
            return row, atoms
        except Exception as exc:
            row.update(status="processing_error", reason=str(exc))
            return row, []

    try:
        persist()
        if source and input_path and stage == "iterate":
            column = text_column or cfg["pipeline"]["text_column"]
            frame = load_excel(input_path, column, sheet_name)
            if limit:
                frame = frame.head(limit)
            from intent_engine.workflows.append_input import plan_append
            additions, append_counts = plan_append(result["rows"], frame[column].tolist())
            source_rows = {row_id:source_row for row_id, _, source_row in additions}
            items_to_extract = [(row_id,text) for row_id,text,_ in additions]
            result["append_input"] = {"path": str(Path(input_path).resolve()), "column": column,
                                      "sheet": sheet_name, **append_counts, "deduplication":"exact_after_strip"}
            print(f"[追加去重] 输入 {append_counts['input_rows']} 条，新增 {append_counts['row_count']} 条，"
                  f"已有重复 {append_counts['duplicate_existing']} 条，文件内重复 {append_counts['duplicate_in_file']} 条，空白 {append_counts['blank_rows']} 条",flush=True)
            reporter.start("append_extract")
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for done, (row, atoms) in enumerate(pool.map(extract, items_to_extract), 1):
                    row["source_file"] = result["append_input"]["path"]
                    row["source_row"] = source_rows[row["row_id"]]
                    result["rows"].append(row)
                    result["intents"].extend(atoms)
                    reporter.update(done, len(additions))
                    persist()
            reporter.complete()
        if "extract" in execute:
            reporter.start("extract")
            with ThreadPoolExecutor(max_workers=workers) as pool:
                pending = [pool.submit(extract, item) for item in items]
                for done, future in enumerate(as_completed(pending), 1):
                    row, atoms = future.result()
                    result["rows"].append(row)
                    result["intents"].extend(atoms)
                    failed = sum(r["status"] == "processing_error" for r in result["rows"])
                    reporter.update(done, len(items), failed=failed)
                    if done == 1 or done % 25 == 0 or done == len(items):
                        persist()
            result["rows"].sort(key=lambda r: r["row_id"])
            result["intents"].sort(key=lambda a: (a["row_id"], a["start"]))
            result["errors"].extend({"stage": "extract", "row_id": r["row_id"], "error": r["reason"]}
                                    for r in result["rows"] if r["status"] == "processing_error")
            finish("extract")
        valid = [a for a in result["intents"] if a["status"] == "valid"]
        texts = list(dict.fromkeys(a["text"] for a in valid))
        vectors = None
        if "cluster" in execute:
            reporter.start("embed")
            if not valid:
                raise ValueError("没有可聚类的明确原子意图；请查看保存的无效、待确认或处理失败记录")
            indices = {text: i for i, text in enumerate(texts)}
            for atom in valid:
                atom["text_index"] = indices[atom["text"]]
            vectors = SemanticEmbeddings(settings, Path(cfg["paths"]["checkpoint_dir"]) / "atomic_embeddings").encode(texts, reporter)
            reporter.start("cluster")
            clustering = cluster_atomic_texts(texts, vectors, options)
            labels, keywords = clustering
            from intent_engine.atomic.topic_hierarchy import build_topic_tree, attach_topic_memberships
            result["topic_hierarchy"] = getattr(clustering, "hierarchy", None) or build_topic_tree(labels, keywords,
                status="not_generated", message="该聚类适配器未返回主题合并树")
            clusters = {}
            for atom in valid:
                atom["cluster_id"] = labels[atom["text_index"]]
                if atom["cluster_id"] != -1:
                    clusters.setdefault(atom["cluster_id"], []).append(atom)
            review_limit = max(2, int(options.get("review_examples", 8)))
            result["neighbor_pairs"] = neighbor_pairs(clusters, vectors, top_k=max(1, int(options.get("neighbor_top_k", 3))))
            for label, members in sorted(clusters.items()):
                unique_count = len({m["text"] for m in members})
                samples = review_sample(members, vectors, review_limit)
                result["clusters"].append({"cluster_id": label, "sample_count": len(members), "unique_text_count": unique_count,
                    "keywords": keywords.get(label, []), "reviewed_ids": [s["intent_id"] for s in samples],
                    "review_scope": "all_unique_texts" if unique_count <= review_limit else "sampled", "review": None,
                    "review_samples": samples})
            reporter.update(len(texts), len(texts))
            attach_topic_memberships(result)
            if result["topic_hierarchy"]["status"] == "failed":
                result["errors"].append({"stage": "hierarchy", "error": result["topic_hierarchy"]["message"]})
            finish("cluster", "completed_with_errors" if result["topic_hierarchy"]["status"] == "failed" else "completed",
                   result["topic_hierarchy"].get("message", ""), hierarchy_depth=result["topic_hierarchy"]["max_depth"])
        if "review" in execute:
            from intent_engine.atomic.review_reuse import remember_review
            review_members = {}
            for atom in valid:
                review_members.setdefault(atom.get("cluster_id"), []).append(atom)
            reporter.start("review")
            reporter.update(0, len(result["clusters"]))
            # Workers only return decisions; the coordinator owns snapshot mutations.
            def audit_cluster(payload):
                samples = payload["review_samples"]
                decision = llm.chat_json(REVIEW_PROMPT, json.dumps(consistency_review_payload(payload, result), ensure_ascii=False),
                                         schema=ClusterReview, max_tokens=2400)
                check_evidence(decision, {s["intent_id"] for s in samples})
                return decision.model_dump()

            for cluster in result["clusters"]:
                cluster["review"] = None
                cluster.pop("review_error", None)
                cluster.pop("review_source", None)
                cluster.pop("reused_from_cluster_id", None)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                pending = {pool.submit(audit_cluster, copy.deepcopy(cluster)): cluster for cluster in result["clusters"]}
                for i, future in enumerate(as_completed(pending), 1):
                    cluster = pending[future]
                    try:
                        cluster["review"] = future.result()
                        remember_review(cluster, review_members[cluster["cluster_id"]], llm, options)
                    except Exception as exc:
                        cluster["review_error"] = str(exc)
                        result["errors"].append({"stage": "review", "cluster_id": cluster["cluster_id"], "error": str(exc)})
                    reporter.update(i, len(result["clusters"]), failed=sum(bool(c.get("review_error")) for c in result["clusters"]))
                    persist()
            from intent_engine.atomic.cluster_refinement import refine_clusters
            if any((c.get("review") or {}).get("verdict") == "mixed" for c in result["clusters"]):
                if vectors is None and int(options.get("refinement_rounds", 2)) > 0:
                    try:
                        settings = embedding_settings(cfg)
                        indices = {text: i for i, text in enumerate(texts)}
                        for atom in valid:
                            atom["text_index"] = indices[atom["text"]]
                        vectors = SemanticEmbeddings(settings, Path(cfg["paths"]["checkpoint_dir"]) / "atomic_embeddings").encode(texts, reporter)
                    except Exception as exc:
                        result["errors"].append({"stage": "review", "error": f"局部细分向量不可用，将尝试完整 LLM 分组：{exc}"})
                refine_clusters(result, vectors, options, llm, reporter, persist)
            else:
                for cluster in result["clusters"]:
                    pending = not review_eligible(cluster)
                    cluster["topic_pending"] = pending
                    cluster["support_level"] = "single_example" if cluster["unique_text_count"] == 1 else "multiple_examples"
                    for atom in valid:
                        if atom.get("cluster_id") == cluster["cluster_id"]:
                            atom["topic_pending"] = pending
            for cluster in result["clusters"]:
                cluster["support_level"] = "single_example" if cluster["unique_text_count"] == 1 else "multiple_examples"
            reviewed = sum(bool(c.get("review")) for c in result["clusters"])
            failed = len(result["clusters"]) - reviewed
            outcome = "blocked" if not result["clusters"] else "failed" if not reviewed else "completed_with_errors" if failed else "completed"
            if reviewed and result.get("cluster_refinement") and result.get("topic_hierarchy", {}).get("status") == "failed":
                outcome = "completed_with_errors"
            finish("review", outcome, f"审核成功 {reviewed} 个簇，失败 {failed} 个簇" if result["clusters"] else "没有可审核的候选簇，请检查噪声与聚类结果",
                   total=len(result["clusters"]), succeeded=reviewed, failed=failed)
        if "pairs" in execute:
            reporter.start("pairs")
            clusters = {c["cluster_id"]: c for c in result["clusters"]}
            pairs = result.get("neighbor_pairs", [])
            def audit_pair(left_samples, right_samples, left_id, right_id):
                from intent_engine.atomic.topic_context import pair_review_payload
                decision = llm.chat_json(PAIR_PROMPT, json.dumps(pair_review_payload(result,left_samples,right_samples,left_id,right_id), ensure_ascii=False),
                                         schema=PairReview, max_tokens=1000)
                if not set(decision.left_evidence_ids) <= {s["intent_id"] for s in left_samples} or not set(decision.right_evidence_ids) <= {s["intent_id"] for s in right_samples}:
                    raise ValueError("跨簇审核引用了未提供的样本")
                return decision.model_dump()

            # Keep result ordering stable while persisting each completed request.
            pair_results = {}
            with ThreadPoolExecutor(max_workers=workers) as pool:
                pending = {pool.submit(audit_pair, copy.deepcopy(clusters[left]["review_samples"]),
                                      copy.deepcopy(clusters[right]["review_samples"]), left, right): (index, left, right)
                           for index, (left, right) in enumerate(pairs)}
                for i, future in enumerate(as_completed(pending), 1):
                    index, left, right = pending[future]
                    entry = {"left_cluster_id": left, "right_cluster_id": right}
                    try:
                        entry.update(future.result())
                    except Exception as exc:
                        entry["error"] = str(exc)
                        result["errors"].append({"stage": "pairs", **entry})
                    pair_results[index] = entry
                    result["pair_reviews"] = [pair_results[key] for key in sorted(pair_results)]
                    reporter.update(i, len(pairs), failed=sum(bool(p.get("error")) for p in pair_results.values()))
                    persist()
            pair_failed = sum(bool(p.get("error")) for p in result["pair_reviews"])
            finish("pairs", "skipped" if not pairs else "failed" if pair_failed == len(pairs) else "completed_with_errors" if pair_failed else "completed",
                   "没有可复核的相邻簇对" if not pairs else f"复核 {len(pairs)} 对，失败 {pair_failed} 对")
        if "domains" in execute:
            from intent_engine.atomic.hierarchy_review import review_hierarchy
            reporter.start("domains")
            state = review_hierarchy(result, llm, workers, reporter, persist,cfg=cfg)
            from intent_engine.atomic.leaf_merge import merge_leaves
            reporter.start("leaf_merge_initial")
            merge_state,vectors = merge_leaves(cfg,result,llm,workers,reporter,persist,vectors=vectors,phase="initial")
            state = result.get("hierarchy_review", state)
            tasks = result["candidate_taxonomy"]["tasks"]
            outcome = "completed_with_errors" if merge_state["status"] in {"failed","completed_with_errors"} else state["status"] if tasks else "blocked"
            finish("domains", outcome, f"审核 {len(state['reviews'])} 个层级节点，保留 {state['accepted_parents']} 个父主题、{len(tasks)} 个叶子任务",
                   tasks=len(tasks), reviewed_nodes=len(state["reviews"]))
        if "iterate" in execute:
            from intent_engine.workflows.hierarchy_iteration import run_hierarchy_iterations, hierarchy_iteration_steps
            if not iteration_rounds or not result["candidate_taxonomy"]["tasks"]:
                reason = "disabled" if not iteration_rounds else "no_seed"
                message = "未启用层级迭代" if not iteration_rounds else "没有审核通过的叶子主题，无法开始迭代"
                result["iteration"] = {"status":"skipped" if not iteration_rounds else "blocked", "rounds":[],
                                       "stop_reason":reason,"stop_message":message}
                if iteration_rounds:
                    reporter.skip(s["id"] for s in hierarchy_iteration_steps(iteration_rounds))
                else:
                    reporter.start("iterate")
                finish("iterate", result["iteration"]["status"], message)
            else:
                # Historical stage sources can enter the unified flow after reviewing their native tree.
                if result.get("topic_hierarchy", {}).get("source") != "BERTopic+LLM":
                    from intent_engine.atomic.hierarchy_review import review_hierarchy
                    review_hierarchy(result, llm, workers, reporter, persist,cfg=cfg)
                state = run_hierarchy_iterations(cfg, result, iteration_rounds, workers, reporter, persist, vectors)
                result["taxonomy_updates_applied"] = any(record["changes"] for record in state["rounds"])
                if state["status"] in {"failed", "completed_with_errors"}:
                    result["errors"].append({"stage":"iterate", "error":state["stop_message"]})
                finish("iterate", state["status"], state["stop_message"], rounds=len(state["rounds"]))
        valid = [atom for atom in result["intents"] if atom["status"] == "valid"]
        reporter.start("export")
        from intent_engine.exporting.delivery_topics import prepare_delivery_topics
        prepare_delivery_topics(result)
        import pandas as pd
        def sheet_rows(rows):
            return [{k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for k, v in row.items()} for row in rows]
        xlsx_path = paths["workbook"]
        with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
            from intent_engine.exporting.sentence_results import build_sentence_results, sentence_result_tables
            for name, rows in sentence_result_tables(build_sentence_results(result)).items():
                pd.DataFrame(rows).to_excel(writer, sheet_name=name, index=False)
            for name, rows in (("原句", result["rows"]), ("原子意图", result["intents"]),
                               ("交付主题映射", [{"intent_id":key, **value} for key,value in result["delivery_topics"].get("assignments", {}).items()]),
                               ("覆盖统计", [result["delivery_topics"]["coverage"]]),
                               ("簇审核", result["clusters"]), ("聚类细分", result.get("cluster_refinement", {}).get("history", [])), ("跨簇建议", result["pair_reviews"]),
                               ("候选任务", result["candidate_taxonomy"]["tasks"]),
                               ("层级节点审核", result.get("hierarchy_review", {}).get("reviews", [])),
                               ("层级审核轮次", result.get("hierarchy_review", {}).get("levels", [])), ("错误", result["errors"]),
                               ("迭代记录", [{key: value for key, value in record.items() if key not in {"taxonomy_before", "taxonomy_after", "assignments", "evidence", "changes", "hierarchy_before", "hierarchy_after", "memberships", "hierarchy_review"}}
                                         for record in result.get("iteration", {}).get("rounds", [])]),
                               ("迭代分类", result.get("iteration", {}).get("assignments", [])),
                               ("迭代变更", [{"round": record["round"], **change} for record in result.get("iteration", {}).get("rounds", []) for change in record["changes"]]),
                               ("Token用量", usage_since(usage_start)["models"])):
                pd.DataFrame(sheet_rows(rows)).to_excel(writer, sheet_name=name, index=False)
            from intent_engine.atomic.topic_hierarchy import hierarchy_export_tables
            for name, rows in hierarchy_export_tables(result).items():
                pd.DataFrame(sheet_rows(rows)).to_excel(writer, sheet_name=name, index=False)
            for sheet in writer.book.worksheets:
                for row in sheet:
                    for cell in row:
                        if cell.data_type == "f":
                            cell.data_type = "s"
        result["status"] = "completed_with_errors" if result["errors"] or any(r["status"] == "processing_error" for r in result["rows"]) or any(
            item["status"] in {"failed", "blocked", "completed_with_errors"} for item in result["stage_outcomes"].values()) else "completed"
        result["summary"] = {"rows": len(result["rows"]), "atomic_intents": len(result["intents"]),
                             "unique_clustered_texts": len(texts), "clusters": len(result["clusters"]),
                             "noise_intents": sum(a["cluster_id"] == -1 for a in valid),
                             "ambiguous_intents": sum(a["status"] == "ambiguous" for a in result["intents"]),
                             "processing_errors": sum(r["status"] == "processing_error" for r in result["rows"]),
                             "coverage": result["delivery_topics"]["coverage"]}
        result["review_policy"] = "bounded_local_refinement_with_re_review; taxonomy_iteration_updates_workflow_draft; neighboring pairs only; sampled reviews are not full-cluster guarantees"
        result["artifacts"]["workbook"] = str(xlsx_path)
        from intent_engine.exporting.sentence_results import write_sentence_results
        result["artifacts"].update(write_sentence_results(paths["manifest"].parent / "result", result, directory=paths["sentence_results"].parent))
        finish("export")
        print(json.dumps({"status": result["status"], "summary": result["summary"], "report": str(result_path),
                         "workbook": str(xlsx_path), "sentence_results": result["artifacts"]["sentence_results"],
                         "sentence_workbook": result["artifacts"]["sentence_workbook"]}, ensure_ascii=False))
        return result
    except Exception as exc:
        result["status"] = "failed"
        failed_stage = ("cluster" if reporter.state.current_id == "embed" else "iterate" if reporter.state.current_id and
                        (reporter.state.current_id.startswith("round_") or reporter.state.current_id.startswith("iteration_")) else reporter.state.current_id)
        if failed_stage in WORKFLOW_STAGES:
            result["stage_outcomes"][failed_stage] = {"status": "failed", "message": str(exc)}
        result["errors"].append({"stage": reporter.state.current_id, "error": str(exc)})
        persist()
        raise
