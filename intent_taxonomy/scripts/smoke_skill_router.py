import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

"""Run after `pip install -r requirements.txt`.
This smoke test only checks high-precision hard-routing cases and does not call vLLM.
"""
from intent_engine.models import SemanticIntent, SemanticResult
from intent_engine.classification.skill_router import SkillRouter, load_skill_registry


class NoLLM:
    def chat_json(self, *args, **kwargs):
        raise RuntimeError("This smoke test should not call the LLM")


CASES = [
    ("帮我挂明天下午的骨科", "健康医疗", "挂号", "预约", "health-query"),
    ("每天早晚提醒我吃维D", "健康医疗", "提醒", "设置", "health-query"),
    ("查一下附近有没有中医院", "本地生活", "中医院", "查询", "general-query"),
    ("打开客厅灯", "智能家居", "客厅灯", "打开", "iot-control"),
    ("我平时晚上最常把空调调到多少度", "智能家居", "空调", "查询", "home-habits-query"),
    ("联通宽带有哪些套餐", "通信", "联通宽带套餐", "查询", "liantong-query"),
    ("查下手机话费余额", "通信", "话费余额", "查询", "gui-control"),
    ("在淘宝搜一下降噪耳机", "购物", "耳机", "搜索", "gui-control"),
    ("附近有什么评分高的餐馆", "餐饮", "餐馆", "推荐", "general-query"),
    ("推荐一部动作片", "影视", "电影", "推荐", "film-tv"),
    ("这部电影豆瓣评分多少", "影视", "电影", "查询", "general-query"),
]


def main():
    registry = load_skill_registry("data/skills.json")
    if not registry.skills:
        print("SKIPPED: Skill routing is optional; no registry configured")
        return
    router = SkillRouter(NoLLM(), registry, {"enable_hard_rules": True})
    failures = []
    for text, domain, obj, action, expected in CASES:
        intent = SemanticIntent(domain=domain, object=obj, action=action, span=text)
        result = SemanticResult(raw_text=text, intents=[intent])
        route = router.route_intent(intent, result)
        print(f"{text} -> {route.primary_skill_id} ({route.route_mode})")
        if route.primary_skill_id != expected:
            failures.append((text, expected, route.primary_skill_id))
    if failures:
        raise SystemExit(f"FAILED: {failures}")
    print("OK")


if __name__ == "__main__":
    main()
