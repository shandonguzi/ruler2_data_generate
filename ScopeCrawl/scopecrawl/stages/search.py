"""阶段 2：检索候选网页。"""

from __future__ import annotations

import logging

from ..models import Failure, SearchRequest, SearchResponse, Settings
from ..provider import Provider
from ..search import SearchError, country_from_location
from ..summary import SearchStats, StageAborted
from ..utils import iter_concurrent, log_stage

logger = logging.getLogger(__name__)

FATAL_SEARCH_STATUSES = {400, 401, 402, 403}


class SearchStage:
    """用关键词并发调用搜索接口，收集原始结果。"""

    def __init__(
        self,
        settings: Settings,
        provider: Provider,
    ) -> None:
        self.settings = settings
        self.provider = provider

    def run(
        self, keywords: list[str]
    ) -> tuple[list[tuple[int, str, SearchResponse]], SearchStats]:
        log_stage("检索")
        stats = SearchStats()
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
                max_results=self.settings.search.defaults.max_results,
                time_range=run.time_range,
            )
            for query in keywords
        ]

        responses: list[SearchResponse | None] = [None] * len(requests)
        fatal_status: int | None = None
        failed = 0
        done = 0
        logger.info(
            "[检索] 开始 %d 条查询（每条最多 %d 条结果）…",
            len(requests),
            self.settings.search.defaults.max_results,
        )

        for index, future in iter_concurrent(
            requests, self.provider.search, self.settings.search.concurrency
        ):
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
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.warning(
                    "[检索] 失败 %d/%d：%s（%s）", done, len(requests), keywords[index], exc
                )
                stats.failures.append(
                    Failure(
                        stage="search",
                        error_type="search_error",
                        message=f"{keywords[index]}：{exc}",
                    )
                )
                if isinstance(exc, SearchError) and exc.status_code in FATAL_SEARCH_STATUSES:
                    fatal_status = exc.status_code

        succeeded = sum(1 for response in responses if response is not None)
        logger.info("[检索] 结束：成功 %d 条查询，失败 %d 条", succeeded, failed)
        if fatal_status is not None:
            logger.error("搜索致命错误（HTTP %s），终止 run", fatal_status)
            raise StageAborted("search", stats)
        if failed == len(requests):
            logger.error("所有搜索请求均失败，终止 run")
            raise StageAborted("search", stats)

        return [
            (index, keywords[index], response)
            for index, response in enumerate(responses)
            if response is not None
        ], stats
