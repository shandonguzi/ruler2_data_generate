from __future__ import annotations

import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from tqdm import tqdm

from intent_engine.infrastructure.llm_client import LocalLLM
from intent_engine.models import SemanticIntent, SemanticResult, SkillDef, SkillRegistry, SkillRoute


from intent_engine.settings.prompt_settings import prompt_ref


JUDGE_SYSTEM = prompt_ref("skill_router.JUDGE_SYSTEM")


FALLBACK_SYSTEM = prompt_ref("skill_router.FALLBACK_SYSTEM")


APP_WORDS = (
    "高德", "百度地图", "腾讯地图", "淘宝", "天猫", "京东", "拼多多", "美团", "饿了么",
    "携程", "去哪儿", "飞猪", "12306", "滴滴", "爱奇艺", "腾讯视频", "优酷", "芒果TV",
    "哔哩哔哩", "B站", "抖音", "快手", "网易云", "QQ音乐", "支付宝", "微信",
)

FILM_OBJECT_WORDS = ("电影", "电视剧", "电视", "综艺", "影视", "影片", "片子", "剧集", "剧")
FILM_KNOWLEDGE_WORDS = ("评分", "口碑", "剧情", "解析", "导演", "演员", "主演", "续集", "结局", "讲什么", "豆瓣")
FILM_ACTIONS = {"推荐", "搜索", "查找", "播放", "观看", "找"}

HEALTH_EXEC_WORDS = ("挂号", "预约挂号", "吃药提醒", "服药提醒", "用药提醒", "健康档案", "病历档案", "查挂号", "挂号查询")
HEALTH_MEDICATION_REMINDER_WORDS = ("维D", "维生素", "药片", "胶囊", "服药", "吃药", "用药")
HEALTH_QUERY_WORDS = (
    "症状", "疾病", "用药", "药品", "药物", "副作用", "禁忌", "发烧", "咳嗽", "头疼", "头痛",
    "胸痛", "腹痛", "血压", "血糖", "诊断", "治疗", "怎么吃药", "吃什么药", "健康建议", "体检报告",
)
HEALTH_LOCATION_WORDS = ("附近医院", "附近的医院", "附近中医院", "附近的中医院", "附近体检中心", "附近的体检中心")

IOT_DEVICES = ("灯", "空调", "窗帘", "空气净化器", "净化器", "扫地机器人", "电视", "热水器", "加湿器", "风扇", "智能插座")
IOT_CONTROL_WORDS = ("打开", "关闭", "开", "关", "调到", "调成", "调低", "调高", "降低", "升高", "设置为", "切换", "启动", "停止")
HABIT_WORDS = ("历史", "一般", "通常", "平时", "最常", "习惯", "偏好", "经常", "这个月", "每天开多久", "使用时长", "使用频率", "规律")

LIANTONG_WORDS = ("联通", "中国联通")
LIANTONG_PRODUCT_WORDS = ("宽带", "套餐", "号码", "副卡", "云手机", "资费", "业务规则", "办理规则", "业务", "产品")
LIANTONG_EXCLUDE_WORDS = ("话费余额", "查话费", "查询话费", "充值话费", "充话费", "开发票", "开票")

GUI_EXEC_WORDS = (
    "导航", "打车", "叫车", "点外卖", "外卖", "下单", "购买", "买", "订票", "订酒店", "预订酒店",
    "充值", "充话费", "话费余额", "查话费", "查询话费", "开发票", "开票", "设置提醒", "设提醒", "设闹钟", "播放", "打开App", "打开应用",
)
GENERAL_ACTIONS = {"查询", "搜索", "推荐", "比较", "解释", "总结", "翻译", "生成", "写作", "规划", "制定", "问答", "分析"}


def load_skill_registry(path: str) -> SkillRegistry:
    p = Path(path)
    if not p.exists():
        return SkillRegistry()
    return SkillRegistry.model_validate(json.loads(p.read_text(encoding="utf-8")))


def _contains_any(text: str, words: Iterable[str]) -> bool:
    return any(w and w.lower() in text.lower() for w in words)


def _semantic_text(intent: SemanticIntent, result: SemanticResult) -> str:
    # 多意图时必须优先使用自己的 span，避免另一意图的关键词污染当前路由。
    base = intent.span.strip()
    if not base and len(result.intents) == 1:
        base = result.raw_text.strip()
    slots = json.dumps(intent.slots, ensure_ascii=False)
    return " ".join(x for x in [base, intent.domain, intent.object, intent.action, slots] if x)


def _has_app(text: str, slots: dict) -> bool:
    if _contains_any(text, APP_WORDS):
        return True
    for key, value in slots.items():
        k = str(key).lower()
        v = str(value)
        if k in {"app", "platform", "应用", "平台", "软件"} and v.strip():
            return True
    return False


class SkillRouter:
    def __init__(self, llm: LocalLLM, registry: SkillRegistry, cfg: Optional[dict] = None):
        self.llm = llm
        self.registry = registry
        self.cfg = cfg or {}
        self.skills = {s.skill_id: s for s in registry.skills}
        self.top_k = int(self.cfg.get("candidate_top_k", 3))
        self.enable_hard_rules = bool(self.cfg.get("enable_hard_rules", True))
        self.enable_llm_judge = bool(self.cfg.get("enable_llm_judge", True))
        self.fallback_full_registry = bool(self.cfg.get("fallback_full_registry", True))

    def _hard_route(self, intent: SemanticIntent, result: SemanticResult) -> Optional[SkillRoute]:
        if not self.enable_hard_rules:
            return None
        text = _semantic_text(intent, result)
        action = intent.action.strip()
        obj = intent.object.strip()
        app_specified = _has_app(text, intent.slots)

        # 1) 智能家居：实时控制 与 历史习惯先做强区分。
        has_iot_device = _contains_any(text, IOT_DEVICES) or any(x in obj for x in IOT_DEVICES)
        if has_iot_device and _contains_any(text, HABIT_WORDS):
            return SkillRoute(
                primary_skill_id="home-habits-query",
                candidate_skill_ids=["home-habits-query"],
                route_mode="hard_rule",
                confidence=0.99,
                reason="命中智能家居历史状态/使用习惯高精度规则",
                matched_rules=["iot_device + history/habit -> home-habits-query"],
            )
        if has_iot_device and (_contains_any(text, IOT_CONTROL_WORDS) or action in {"打开", "关闭", "调节", "控制", "设置", "启动", "停止"}):
            return SkillRoute(
                primary_skill_id="iot-control",
                candidate_skill_ids=["iot-control"],
                route_mode="hard_rule",
                confidence=0.99,
                reason="命中智能家居实时控制高精度规则",
                matched_rules=["iot_device + control_action -> iot-control"],
            )

        # 2) 健康：健康专用执行/咨询优先；纯附近医疗地点查询除外。
        location_only = _contains_any(text, HEALTH_LOCATION_WORDS) and not _contains_any(text, HEALTH_EXEC_WORDS)
        if location_only:
            return SkillRoute(
                primary_skill_id="general-query",
                candidate_skill_ids=["general-query"],
                route_mode="hard_rule",
                confidence=0.98,
                reason="只是附近医院/体检中心等周边地点查询，不是健康专用咨询或执行",
                matched_rules=["medical_location_only -> general-query"],
            )
        medication_reminder = "提醒" in text and _contains_any(text, HEALTH_MEDICATION_REMINDER_WORDS)
        if _contains_any(text, HEALTH_EXEC_WORDS) or _contains_any(text, HEALTH_QUERY_WORDS) or medication_reminder:
            return SkillRoute(
                primary_skill_id="health-query",
                candidate_skill_ids=["health-query"],
                route_mode="hard_rule",
                confidence=0.99,
                reason="命中健康专用咨询/执行高精度规则",
                matched_rules=["health_specialized -> health-query"],
            )

        # 3) 影视：推荐/搜索/播放进入 film-tv；知识问答进入 general-query。
        film_semantic = _contains_any(text, FILM_OBJECT_WORDS) or any(w in obj for w in FILM_OBJECT_WORDS)
        if film_semantic and _contains_any(text, FILM_KNOWLEDGE_WORDS):
            return SkillRoute(
                primary_skill_id="general-query",
                candidate_skill_ids=["general-query"],
                route_mode="hard_rule",
                confidence=0.98,
                reason="影视评分/剧情/导演/演员/续集等属于知识问答",
                matched_rules=["film_knowledge -> general-query"],
            )
        if film_semantic and (action in FILM_ACTIONS or _contains_any(text, ("推荐", "搜索", "找", "播放", "看"))):
            return SkillRoute(
                primary_skill_id="film-tv",
                candidate_skill_ids=["film-tv"],
                route_mode="hard_rule",
                confidence=0.99,
                reason="影视内容推荐/搜索/播放优先 film-tv",
                matched_rules=["film_content_action -> film-tv"],
            )

        # 4) 联通知识查询；话费余额/充值/开票明确不属于 liantong-query。
        if _contains_any(text, LIANTONG_WORDS):
            if _contains_any(text, LIANTONG_EXCLUDE_WORDS):
                return SkillRoute(
                    primary_skill_id="gui-control",
                    candidate_skill_ids=["gui-control"],
                    route_mode="hard_rule",
                    confidence=0.99,
                    reason="联通场景中的查话费/充值/开票按移动端执行流程处理",
                    matched_rules=["liantong_account_execution -> gui-control"],
                )
            if _contains_any(text, LIANTONG_PRODUCT_WORDS) or action in {"查询", "解释", "比较"}:
                return SkillRoute(
                    primary_skill_id="liantong-query",
                    candidate_skill_ids=["liantong-query"],
                    route_mode="hard_rule",
                    confidence=0.98,
                    reason="命中联通产品/套餐/号码/业务规则查询",
                    matched_rules=["liantong_product_query -> liantong-query"],
                )

        # 5) 普通明确 App/平台任务：专用 Skill 已在上面被截获，剩余进入 GUI。
        if app_specified:
            return SkillRoute(
                primary_skill_id="gui-control",
                candidate_skill_ids=["gui-control"],
                route_mode="hard_rule",
                confidence=0.96,
                reason="明确指定 App/平台的普通搜索/查询/执行任务",
                matched_rules=["explicit_app_or_platform -> gui-control"],
            )

        # 6) 明确执行类动作。
        if _contains_any(text, GUI_EXEC_WORDS) or action in {"导航", "打车", "购买", "下单", "订票", "订酒店", "充值", "开发票", "设置提醒"}:
            return SkillRoute(
                primary_skill_id="gui-control",
                candidate_skill_ids=["gui-control"],
                route_mode="hard_rule",
                confidence=0.95,
                reason="命中移动端执行流程高精度动作",
                matched_rules=["execution_action -> gui-control"],
            )

        # 7) 只有明显的通用查询/生成才硬路由；普通“查询”保留给 Candidate Judge，
        # 避免 taxonomy/抽取偶尔漏掉专用领域时被 general-query 提前吞掉。
        general_cues = (
            "天气", "几点", "现在时间", "附近", "周边", "新闻", "航班", "列车", "火车",
            "推荐", "总结", "翻译", "写一", "写个", "攻略", "规划", "解释一下", "为什么",
        )
        if _contains_any(text, general_cues) and action in GENERAL_ACTIONS:
            return SkillRoute(
                primary_skill_id="general-query",
                candidate_skill_ids=["general-query"],
                route_mode="hard_rule",
                confidence=0.93,
                reason="命中明显的通用查询、推荐或内容生成规则",
                matched_rules=["explicit_general_cue -> general-query"],
            )

        return None

    def _candidate_scores(self, intent: SemanticIntent, result: SemanticResult) -> List[Tuple[str, float]]:
        text = _semantic_text(intent, result).lower()
        scores = Counter()

        for skill in self.registry.skills:
            score = 0.0
            # 结构化关键词仅用于召回，不做最终判定。
            for kw in skill.retrieval_keywords:
                if kw and kw.lower() in text:
                    score += 2.0
            # task_id 若已人工绑定，作为强召回信号。
            if intent.matched_task_id and intent.matched_task_id in skill.supported_task_ids:
                score += 8.0
            if score > 0:
                # 很小的优先级 tie-break，不让 priority 主导召回。
                score += min(max(skill.routing_priority, 0), 100) / 1000.0
                scores[skill.skill_id] = score

        ranked = scores.most_common()
        if ranked:
            ids = [sid for sid, _ in ranked[: self.top_k]]
        else:
            # 没有明显关键词时，只召回最常见的两类，而不是把全部 Skill 塞给 Judge。
            app = _has_app(text, intent.slots)
            ids = ["gui-control", "general-query"] if app else ["general-query", "gui-control"]

        # 确保通用 fallback 至少有一个，但不超过 top_k。
        fallback = "gui-control" if _has_app(text, intent.slots) else "general-query"
        if fallback in self.skills and fallback not in ids:
            ids.append(fallback)
        ids = ids[: self.top_k]

        return [(sid, float(scores.get(sid, 0.0))) for sid in ids if sid in self.skills]

    def recall_candidates(self, intent: SemanticIntent, result: SemanticResult) -> List[SkillDef]:
        return [self.skills[sid] for sid, _ in self._candidate_scores(intent, result) if sid in self.skills]

    def _judge(self, intent: SemanticIntent, result: SemanticResult, candidates: List[SkillDef]) -> SkillRoute:
        payload = {
            "atomic_intent": {
                "raw_text": result.raw_text if len(result.intents) == 1 else "",
                "span": intent.span,
                "domain": intent.domain,
                "object": intent.object,
                "action": intent.action,
                "slots": intent.slots,
                "matched_task_id": intent.matched_task_id,
            },
            "routing_policy": self.registry.routing_policy.model_dump(),
            "candidate_skills": [s.model_dump() for s in candidates],
        }
        data = self.llm.chat_json(JUDGE_SYSTEM, json.dumps(payload, ensure_ascii=False))
        valid = {s.skill_id for s in candidates}
        sid = str(data.get("primary_skill_id") or "")
        if sid not in valid:
            sid = None
        return SkillRoute(
            primary_skill_id=sid,
            candidate_skill_ids=[s.skill_id for s in candidates],
            route_mode="llm_judge",
            confidence=float(data.get("confidence") or 0.0),
            reason=str(data.get("reason") or ""),
            matched_rules=list(data.get("matched_rules") or []),
            rejected_skills=list(data.get("rejected_skills") or []),
        )

    def _fallback(self, intent: SemanticIntent, result: SemanticResult) -> SkillRoute:
        payload = {
            "atomic_intent": {
                "raw_text": result.raw_text if len(result.intents) == 1 else "",
                "span": intent.span,
                "domain": intent.domain,
                "object": intent.object,
                "action": intent.action,
                "slots": intent.slots,
                "matched_task_id": intent.matched_task_id,
            },
            "routing_policy": self.registry.routing_policy.model_dump(),
            "skills": [s.model_dump() for s in sorted(self.registry.skills, key=lambda x: x.routing_priority, reverse=True)],
        }
        data = self.llm.chat_json(FALLBACK_SYSTEM, json.dumps(payload, ensure_ascii=False))
        valid = set(self.skills)
        sid = str(data.get("primary_skill_id") or "")
        if sid not in valid:
            sid = None
        return SkillRoute(
            primary_skill_id=sid,
            candidate_skill_ids=sorted(valid),
            route_mode="fallback_llm",
            confidence=float(data.get("confidence") or 0.0),
            reason=str(data.get("reason") or ""),
            matched_rules=list(data.get("matched_rules") or []),
            rejected_skills=list(data.get("rejected_skills") or []),
        )

    def route_intent(self, intent: SemanticIntent, result: SemanticResult) -> SkillRoute:
        if intent.status == "invalid":
            return SkillRoute(route_mode="unrouted", reason="invalid intent")

        hard = self._hard_route(intent, result)
        if hard is not None and hard.primary_skill_id in self.skills:
            return hard

        candidates = self.recall_candidates(intent, result)
        if not candidates:
            return SkillRoute(route_mode="unrouted", reason="no candidate skill")

        if not self.enable_llm_judge:
            return SkillRoute(
                primary_skill_id=candidates[0].skill_id,
                candidate_skill_ids=[s.skill_id for s in candidates],
                route_mode="unrouted",
                confidence=0.0,
                reason="LLM judge disabled; only candidate recall available",
            )

        try:
            return self._judge(intent, result, candidates)
        except Exception as e:
            if self.fallback_full_registry:
                try:
                    route = self._fallback(intent, result)
                    route.reason = f"candidate judge failed ({e}); fallback: {route.reason}"
                    return route
                except Exception as e2:
                    return SkillRoute(
                        candidate_skill_ids=[s.skill_id for s in candidates],
                        route_mode="unrouted",
                        reason=f"judge failed: {e}; fallback failed: {e2}",
                    )
            return SkillRoute(
                candidate_skill_ids=[s.skill_id for s in candidates],
                route_mode="unrouted",
                reason=f"judge failed: {e}",
            )

    def route_result(self, result: SemanticResult) -> SemanticResult:
        for intent in result.intents:
            intent.skill_route = self.route_intent(intent, result)
        return result


def route_results(
    llm: LocalLLM,
    registry: SkillRegistry,
    results: List[SemanticResult],
    cfg: Optional[dict] = None,
    max_workers: int = 8,
    progress_callback=None,
) -> List[SemanticResult]:
    if progress_callback:
        progress_callback(0, len(results) if registry.skills else 0)
    if not registry.skills:
        return results
    router = SkillRouter(llm, registry, cfg)

    out: List[Optional[SemanticResult]] = [None] * len(results)
    completed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(router.route_result, result.model_copy(deep=True)): idx for idx, result in enumerate(results)}
        for future in tqdm(as_completed(futures), total=len(futures), desc="skill route"):
            idx = futures[future]
            try:
                out[idx] = future.result()
            except Exception:
                out[idx] = results[idx]
            completed += 1
            if progress_callback:
                progress_callback(completed, len(results))
    return [x if x is not None else results[i] for i, x in enumerate(out)]
