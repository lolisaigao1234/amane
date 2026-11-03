"""Exact RJ video lookup; dlsite-async owns DLsite parsing, Amane owns transport.

Pinned to dlsite-async 0.11.2. Its public get() transport hook is adapted to the
shared HttpClient: no second session, proxy policy, retry loop or credentials.
"""

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urljoin

from dlsite_async import DlsiteAPI, WorkType
from dlsite_async.exceptions import DlsiteError

from ...enums import SiteName
from ...net.errors import FailureReason, RequestError, SourceError
from ..base import Crawler, CrawlerProfile
from ..http import HttpClient
from ..models import FetchOptions, MediaMetadata, SearchQuery


@dataclass
class _Response:
    body: Any
    status: int = 200

    async def json(self) -> Any:
        return self.body

    async def text(self) -> str:
        return str(self.body)


class _AmaneAPI(DlsiteAPI):
    def __init__(self, client: HttpClient) -> None:
        # Deliberately no BaseAPI session. Only get_work() and get() are used.
        self.client = client

    @asynccontextmanager
    async def get(
        self, url: str, *, params: dict[str, str] | None = None, raise_for_status: bool = True
    ) -> AsyncIterator[_Response]:
        if params:
            url += "?" + urlencode(params)
        try:
            if params:
                body = await self.client.get_json(url)
            else:
                body = await self.client.get_html(url)
        except RequestError as exc:
            if not raise_for_status and exc.http_status == 404:
                yield _Response("", 404)
                return
            raise
        yield _Response(body)


class DlsiteCrawler(Crawler):
    @classmethod
    def profile(cls) -> CrawlerProfile:
        return CrawlerProfile(name=SiteName.DLSITE, base_url="https://www.dlsite.com")

    async def fetch(self, query: SearchQuery, options: FetchOptions | None = None) -> MediaMetadata | None:
        number = query.number.upper()
        if not re.fullmatch(r"RJ\d{6,8}", number):
            return None
        try:
            work = await _AmaneAPI(self.client).get_work(number)
        except DlsiteError as exc:
            raise SourceError(FailureReason.NOT_FOUND, detail=str(exc)) from exc
        if work.product_id.upper() != number:
            raise SourceError(FailureReason.PARSE_ERROR, detail="DLsite exact-ID mismatch")
        if work.work_type != WorkType.VIDEO:
            raise SourceError(FailureReason.NO_USABLE_METADATA, detail="DLsite work is not a supported video")
        if not work.work_name:
            raise SourceError(FailureReason.NO_USABLE_METADATA, detail="DLsite video has no title")

        def absolute(url: str) -> str:
            return urljoin(self.base_url, url)

        cover = [absolute(work.work_image)] if work.work_image else []
        return MediaMetadata(
            number=number,
            title=work.work_name,
            studio=work.circle,
            release=work.regist_date.date().isoformat() if work.regist_date else None,
            plot=work.description,
            tags=list(dict.fromkeys(["doujin", *(work.genre or [])])),
            source_url=f"{self.base_url}/maniax/work/=/product_id/{number}.html",
            external_id=number,
            poster_urls=cover,
            thumb_urls=cover,
            extrafanart=[absolute(url) for url in (work.sample_images or [])],
        )

    async def _search(self, query: SearchQuery, options: FetchOptions | None = None) -> str | None:
        raise NotImplementedError("DLsite uses exact-ID fetch")

    async def _scrape(self, url: str, options: FetchOptions | None = None) -> MediaMetadata | None:
        raise NotImplementedError("DLsite uses exact-ID fetch")
