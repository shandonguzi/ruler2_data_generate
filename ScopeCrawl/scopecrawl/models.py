"""所有 pydantic 数据模型与轻量中间结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

Language = Literal["zh", "en", "ar", "ar-EG"]
TimeRange = Literal["day", "week", "month", "year"]
FailureStage = Literal[
    "keyword", "search", "extract", "quality", "review", "authority", "write"
]


class Model(BaseModel):
    """项目内模型基类：拒绝未知字段，避免配置拼写错误被静默忽略。"""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# 配置树
# ---------------------------------------------------------------------------


class RunConfig(Model):
    s3: str
    s4: str
    location: str | None = None
    language: Language = "zh"
    time_range: TimeRange | None = None


# 与文档中的命名保持一致。
RunInput = RunConfig


class KeywordsSettings(Model):
    max_queries: int = Field(15, ge=1, le=100)
    prompt_version: str = "v1"
    scenarios_path: str = "config/scenarios.json"


class SearchDefaults(Model):
    max_results: int = Field(10, ge=1, le=20)
    country: str | None = None


class DedupeSettings(Model):
    enabled: bool = True
    merge_www: bool = False
    strip_tracking_params: bool = True
    max_urls: int | None = Field(None, ge=1)


class TavilySettings(Model):
    search_depth: Literal["basic", "advanced"] = "basic"
    topic: Literal["general", "news"] = "general"
    include_published_date: bool = True
    filter_by_published_date: bool = False
    include_answer: bool = False
    include_images: bool = False
    auto_parameters: bool = False
    exact_match: bool = False
    safe_search: bool = True
    timeout_seconds: float = Field(30, gt=0)
    max_retries: int = Field(3, ge=0, le=10)
    rate_limit_qps: float = Field(1, gt=0)


class PrefilterSettings(Model):
    enabled: bool = True


class SearchSettings(Model):
    provider: str = "tavily"
    defaults: SearchDefaults = Field(default_factory=SearchDefaults)
    dedupe: DedupeSettings = Field(default_factory=DedupeSettings)
    prefilter: PrefilterSettings = Field(default_factory=PrefilterSettings)
    tavily: TavilySettings = Field(default_factory=TavilySettings)


class ExtractSettings(Model):
    provider: Literal["tavily"] = "tavily"
    format: Literal["markdown", "text"] = "markdown"
    depth: Literal["basic", "advanced"] = "basic"
    batch_size: int = Field(20, ge=1, le=20)
    concurrency: int = Field(2, ge=1, le=32)
    rate_limit_qps: float = Field(2, gt=0)
    timeout_seconds: float = Field(60, gt=0, le=60)
    max_retries: int = Field(3, ge=0, le=10)
    min_chars: int = Field(800, ge=1)
    min_paragraphs: int = Field(3, ge=0)
    max_link_text_ratio: float = Field(0.4, ge=0, le=1)
    max_nav_line_ratio: float = Field(0.5, ge=0, le=1)
    max_garbled_ratio: float = Field(0.35, ge=0, le=1)
    skip_domains: list[str] = Field(
        default_factory=lambda: [
            "youtube.com",
            "youtu.be",
            "bilibili.com",
            "douyin.com",
        ]
    )


class LLMSettings(Model):
    timeout_seconds: float = Field(60, gt=0)
    max_retries: int = Field(2, ge=0, le=10)
    concurrency: int = Field(4, ge=1, le=64)


class ScoreThresholds(Model):
    high: float = 0.7
    medium: float = 0.4
    low: float = 0.2


class ContentSettings(Model):
    richness_low: float = Field(0.4, ge=0.0, le=1.0)
    quality_low: float = Field(0.4, ge=0.0, le=1.0)
    drop_when_both_low: bool = False


class DiversitySettings(Model):
    enabled: bool = True
    max_per_query: int | None = Field(5, ge=1)
    max_per_domain: int | None = Field(4, ge=1)


class ReviewSettings(Model):
    enabled: bool = True
    thresholds: ScoreThresholds = Field(default_factory=ScoreThresholds)
    drop_low: bool = True
    drop_none: bool = True
    drop_structural: bool = True
    drop_unevaluated: bool = True
    content: ContentSettings = Field(default_factory=ContentSettings)


class AuthorityDomainSettings(Model):
    tld_scores: dict[str, float] = Field(
        default_factory=lambda: {"gov": 0.9, "edu": 0.9, "int": 0.9}
    )
    unknown_domain_score: float = 0.4


class PageWeights(Model):
    author: float = 0.25
    published_at: float = 0.20
    content_length: float = 0.20
    citations: float = 0.20
    structured_data: float = 0.15


class AuthorityPageSettings(Model):
    weights: PageWeights = Field(default_factory=PageWeights)
    max_ad_penalty: float = 0.20


class AuthorityScoringSettings(Model):
    weights: dict[str, float] = Field(default_factory=lambda: {"domain": 0.6, "page": 0.4})
    high_threshold: float = 0.7
    low_threshold: float = 0.4
    drop_low: bool = False


class AuthoritySettings(Model):
    enabled: bool = True
    config_path: str = "config/authority.yaml"
    domain: AuthorityDomainSettings = Field(default_factory=AuthorityDomainSettings)
    page: AuthorityPageSettings = Field(default_factory=AuthorityPageSettings)
    scoring: AuthorityScoringSettings = Field(default_factory=AuthorityScoringSettings)


class Settings(Model):
    run: RunConfig
    keywords: KeywordsSettings = Field(default_factory=KeywordsSettings)
    search: SearchSettings = Field(default_factory=SearchSettings)
    extract: ExtractSettings = Field(default_factory=ExtractSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    review: ReviewSettings = Field(default_factory=ReviewSettings)
    authority: AuthoritySettings = Field(default_factory=AuthoritySettings)
    diversity: DiversitySettings = Field(default_factory=DiversitySettings)

    # 运行时从 .env 解析，不来自 YAML，也不读进程环境变量。
    llm_model: str | None = None
    tavily_api_key: SecretStr | None = Field(default=None, exclude=True)
    llm_api_key: SecretStr | None = Field(default=None, exclude=True)
    llm_base_url: str | None = None


class AuthorityEntry(Model):
    domain: str
    reason: str = ""


class AuthorityLists(Model):
    whitelist: list[AuthorityEntry] = Field(default_factory=list)
    blacklist: list[AuthorityEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------


class SearchRequest(Model):
    query: str
    language: Language
    country: str | None = None
    location: str | None = None
    max_results: int = Field(10, ge=1, le=20)
    time_range: TimeRange | None = None
    include_domains: list[str] | None = None
    exclude_domains: list[str] | None = None
    provider_options: dict[str, Any] = Field(default_factory=dict)


class SearchResult(Model):
    url: str
    title: str = ""
    snippet: str = ""
    rank: int
    score: float | None = None
    domain: str = ""
    published_at: str | None = None
    raw: dict[str, Any] | None = None


class SearchResponse(Model):
    query: str
    provider: str
    results: list[SearchResult] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int = 0
    request_id: str | None = None


# ---------------------------------------------------------------------------
# 正文提取与文档
# ---------------------------------------------------------------------------


@dataclass
class ExtractedContent:
    """Tavily Extract 单 URL 结果；error_type 为空表示成功。"""

    url: str
    final_url: str = ""
    raw_content: str = ""
    error_type: str = ""
    error: str = ""
    fatal: bool = False


class ExtractedDoc(Model):
    doc_id: str
    url: str
    final_url: str
    title: str = ""
    site: str = ""
    author: str | None = None
    published_at: str | None = None
    fetched_at: str
    lang: str | None = None
    markdown: str
    matched_queries: list[str] = Field(default_factory=list)
    has_jsonld: bool = False
    outbound_links: int = 0
    outbound_text_ratio: float = 0.0
    extractor: str = "tavily-extract"


class RelevanceResult(Model):
    relevance_score: float | None = None
    relevance_level: Literal["high", "medium", "low", "none"] | None = None
    content_type: Literal["article", "listing", "widget", "other"] | None = None
    matched_scope: Literal["s3", "s4", "both", "none"] = "none"
    location_match: bool | None = None
    language_match: bool | None = None
    temporal_match: bool | None = None
    flags: list[str] = Field(default_factory=list)
    reason: str = ""
    model: str | None = None
    reviewed_at: str | None = None


class DomainAssessment(Model):
    domain: str
    decision: Literal["allow", "review", "reject"]
    domain_score: float
    signals: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    assessed_at: str = ""


class PageAssessment(Model):
    doc_id: str
    url: str
    page_score: float
    signals: dict[str, float] = Field(default_factory=dict)
    quality_flags: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    assessed_at: str = ""


class AuthorityResult(Model):
    authority_score: float | None = None
    authority_level: Literal["high", "medium", "low"] | None = None
    quality_flags: list[str] = Field(default_factory=list)
    authority_reasons: list[str] = Field(default_factory=list)
    domain: DomainAssessment | None = None
    page: PageAssessment | None = None


class ContentScore(Model):
    score: float | None = None
    level: Literal["high", "medium", "low"] | None = None
    reason: str = ""


class DocumentEvaluation(Model):
    relevance: RelevanceResult = Field(default_factory=RelevanceResult)
    content_richness: ContentScore = Field(default_factory=ContentScore)
    content_quality: ContentScore = Field(default_factory=ContentScore)
    authority: AuthorityResult = Field(default_factory=AuthorityResult)
    decision: Literal["keep", "drop"] = "keep"
    drop_reasons: list[str] = Field(default_factory=list)


class Document(Model):
    extracted: ExtractedDoc
    evaluation: DocumentEvaluation = Field(default_factory=DocumentEvaluation)


class Failure(Model):
    stage: FailureStage
    url: str | None = None
    error_type: str
    message: str


# ---------------------------------------------------------------------------
# 中间结构
# ---------------------------------------------------------------------------


@dataclass
class UrlTask:
    url: str
    normalized_url: str
    matched_queries: list[str] = field(default_factory=list)
    best_rank: int = 0
    first_query_index: int = 0
    provider: str = "tavily"
    title: str = ""
    snippet: str = ""
    published_at: str | None = None
