"""阶段 2：检索候选网页。"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..models import Failure, SearchRequest, SearchResponse, Settings
from ..search import SearchError, SearchProvider, country_from_location
from ..summary import RunSummary
from .log import log_stage

logger = logging.getLogger(__name__)


class SearchStage:
    """用关键词并发调用搜索接口，收集原始结果。"""

    def __init__(
        self,
        settings: Settings,
        provider: SearchProvider,
        summary: RunSummary,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.summary = summary

    def run(
        self, keywords: list[str]
    ) -> list[tuple[int, str, SearchResponse]] | None:
        log_stage("检索")
        run = self.settings.run
        country = self.settings.search.defaults.country or country_from_location(
            run.location
        )
        logger.info(
            "[检索] country=%s，地点=%s",
            country or "none",
            run.location or "none",
        )
        requests = [
            SearchRequest(
                query=query,
                language=run.language,
                country=country,
                location=run.location,
                max_results=self.settings.search.defaults.max_results,
                time_range=run.time_range,
            )
            for query in keywords
        ]

        responses: list[SearchResponse | None] = [None] * len(requests)
        fatal_400 = False
        failed = 0
        done = 0
        workers = max(1, min(len(requests), 8))
        logger.info(
            "[检索] 开始 %d 条查询（每条最多 %d 条结果）…",
            len(requests),
            self.settings.search.defaults.max_results,
        )

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(self.provider.search, request): index
                for index, request in enumerate(requests)
            }
            for future in as_completed(futures):
                index = futures[future]
                done += 1
                try:
                    response = future.result()
                    responses[index] = response
                    logger.info(
                        "[检索] 完成 %d/%d：%s → %d 条",
                        done,
                        len(requests),
                        keywords[index],
                        len(response.results),
                    )
                except SearchError as exc:
                    failed += 1
                    logger.warning(
                        "[检索] 失败 %d/%d：%s（%s）", done, len(requests), keywords[index], exc
                    )
                    self.summary.failures.append(
                        Failure(
                            stage="search",
                            error_type="search_error",
                            message=f"{keywords[index]}：{exc}",
                        )
                    )
                    if exc.status_code == 400:
                        fatal_400 = True
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    logger.warning(
                        "[检索] 失败 %d/%d：%s（%s）", done, len(requests), keywords[index], exc
                    )
                    self.summary.failures.append(
                        Failure(
                            stage="search",
                            error_type="search_error",
                            message=f"{keywords[index]}：{exc}",
                        )
                    )

        succeeded = sum(1 for response in responses if response is not None)
        logger.info("[检索] 结束：成功 %d 条查询，失败 %d 条", succeeded, failed)
        if fatal_400:
            logger.error("搜索参数错误（HTTP 400），终止 run")
            return None
        if failed == len(requests):
            logger.error("所有搜索请求均失败，终止 run")
            return None

        return [
            (index, keywords[index], response)
            for index, response in enumerate(responses)
            if response is not None
        ]
