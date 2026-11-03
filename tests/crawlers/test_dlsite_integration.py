"""Public Amane parsing, provider and organization contracts (Python 3.14 image)."""

import copy
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import AsyncMock

from amane.config import HotSettings
from amane.crawlers.factory import CrawlerFactory
from amane.crawlers.http import HttpClient
from amane.crawlers.models import SearchQuery
from amane.db.models import Library, Metadata
from amane.media.nfo import write_nfo
from amane.net.errors import FailureKind, RequestError, RequestFailure, SourceError
from amane.organize import PlaceOutcome, execute_organize, resolve_paths
from amane.parsing.file_info import parse_file_info

RELEASE = "(同人アニメ)[260925][royal-s] さいみんえっち 競泳水着お姉さん [RJ01722419]"


class ParsingTests(unittest.TestCase):
    def test_exact_rj_before_generic_matching_and_parent_fallback(self) -> None:
        for args in (
            {"text": "rj01722419"},
            {"text": RELEASE},
            {"path": RELEASE + "/さいみんえっち競泳水着お姉さん(本編).mp4"},
        ):
            with self.subTest(args=args):
                info = parse_file_info(**args)
                self.assertEqual((info.number, str(info.content_type), info.prefix), ("RJ01722419", "doujin", "RJ"))
        self.assertEqual(parse_file_info(text="SSIS-001").number, "SSIS-001")
        self.assertEqual(parse_file_info(text="FC2-PPV-1851398-CD2").cd, 2)
        for text in ("XRJ01722419", "RJ017224190", "01722419"):
            self.assertNotEqual(str(parse_file_info(text=text).content_type), "doujin")


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_provider_returns_exact_video_metadata(self) -> None:
        fixture = Path(__file__).parent / "fixtures"
        web = AsyncMock()
        web.get_json.return_value = json.loads((fixture / "RJ01722419.json").read_text())
        web.get_text.return_value = (fixture / "RJ01722419.html").read_text()
        factory = CrawlerFactory(HttpClient(web))
        provider = await factory.get("dlsite")
        self.assertIsNotNone(provider)
        result = await provider.fetch(SearchQuery("RJ01722419"))
        self.assertEqual(result.number, "RJ01722419")
        self.assertEqual(result.title, "さいみんえっち 競泳水着お姉さん")
        self.assertEqual(result.studio, "royal/s")
        self.assertEqual(result.release, "2026-09-25")
        self.assertIn("doujin", result.tags)
        self.assertIn("スクール水着", result.tags)
        self.assertTrue(result.plot)
        self.assertEqual(result.source_url, "https://www.dlsite.com/maniax/work/=/product_id/RJ01722419.html")
        self.assertEqual(len(result.extrafanart), 5)
        self.assertTrue(result.poster_urls[0].endswith("RJ01722419_img_main.jpg"))
        self.assertEqual(HotSettings().scraping.content_routes["doujin"], ["dlsite"])


class ProviderFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_is_visible_and_same_provider_can_retry(self) -> None:
        fixture = Path(__file__).parent / "fixtures"
        data = json.loads((fixture / "RJ01722419.json").read_text())
        web = AsyncMock()
        web.get_text.return_value = (fixture / "RJ01722419.html").read_text()
        provider = await CrawlerFactory(HttpClient(web)).get("dlsite")
        for failure in (
            {},
            {"RJ00000001": data["RJ01722419"]},
            RequestError("https://www.dlsite.com", RequestFailure(kind=FailureKind.TIMEOUT, message="timeout")),
        ):
            web.get_json.side_effect = failure if isinstance(failure, Exception) else None
            web.get_json.return_value = failure
            with self.assertRaises(SourceError):
                await provider.fetch(SearchQuery("RJ01722419"))
        web.get_json.side_effect = None
        web.get_json.return_value = data
        self.assertEqual((await provider.fetch(SearchQuery("RJ01722419"))).number, "RJ01722419")

    async def test_non_video_is_rejected_and_missing_optional_metadata_stays_absent(self) -> None:
        fixture = Path(__file__).parent / "fixtures"
        data = json.loads((fixture / "RJ01722419.json").read_text())
        web = AsyncMock()
        web.get_json.return_value = data
        web.get_text.return_value = "<html></html>"
        provider = await CrawlerFactory(HttpClient(web)).get("dlsite")
        web.get_json.side_effect = lambda *a, **k: copy.deepcopy(data)
        for work_type in ("SOU", "ACN", "MNG"):
            data["RJ01722419"]["work_type"] = work_type
            with self.subTest(work_type=work_type), self.assertRaises(SourceError):
                await provider.fetch(SearchQuery("RJ01722419"))
        data["RJ01722419"]["work_type"] = "MOV"
        for key in ("work_image", "regist_date", "options"):
            data["RJ01722419"].pop(key, None)
        result = await provider.fetch(SearchQuery("RJ01722419"))
        self.assertEqual((result.studio, result.release, result.plot), (None, None, None))
        self.assertEqual((result.poster_urls, result.extrafanart), ([], []))
        self.assertEqual(result.tags, ["doujin"])


class OrganizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_subtitled_version_filename_is_stable_on_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            library = Library(
                name="JAV",
                path=str(root / "_inbox"),
                video_template=str(root / "{number}/{number}[-CD{cd?}][{version?}][-{sub?}].{ext}"),
            )
            metadata = Metadata(number="RJ01722419", content_type="doujin", title="Feature")
            for filename in ("RJ01722419 - main-C.mp4", "RJ01722419-C.mp4"):
                source = root / "RJ01722419" / filename
                for _ in range(3):
                    paths = resolve_paths(
                        library,
                        metadata,
                        ext="mp4",
                        source_path=source,
                        file_info=parse_file_info(source),
                        safe_dirs=[root],
                    )
                    self.assertEqual(paths.video, source)
                    source = paths.video

    async def test_versions_survive_retries_and_collision_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            library = Library(
                name="JAV",
                path=str(root / "_inbox"),
                video_template=str(
                    root
                    / "{content_type|doujin=doujin,censored=,uncensored=,chinese=,western=,fc2=,amateur=,hentai=}/{studio}/{number}/{number}[-CD{cd?}][{version?}].{ext}"
                ),
            )
            metadata = Metadata(
                number="RJ01722419",
                content_type="doujin",
                title="さいみんえっち 競泳水着お姉さん",
                studio="royal/s",
                release="2026-09-25",
                tags=["doujin"],
                external_ids={"dlsite": "RJ01722419"},
            )
            destinations = []
            for name, content in (("本編", b"main feature"), ("Hシーン版", b"alternate version")):
                source = root / "_inbox" / RELEASE / ("さいみんえっち競泳水着お姉さん(" + name + ").mp4")
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(content)
                info = parse_file_info(source)
                paths = resolve_paths(
                    library, metadata, ext="mp4", source_path=source, file_info=info, safe_dirs=[root]
                )
                self.assertIn("/doujin/royal s/RJ01722419/", str(paths.video))
                self.assertIn(name, paths.video.name)
                result = await execute_organize(source, paths.video.parent, paths.video.stem)
                self.assertEqual(result.outcome, PlaceOutcome.PLACED)
                self.assertEqual(paths.video.read_bytes(), content)
                self.assertTrue(await write_nfo(metadata, paths.nfo))
                self.assertEqual(ET.parse(paths.nfo).findtext("studio"), "royal/s")
                self.assertEqual(ET.parse(paths.nfo).findtext("dlsiteid"), "RJ01722419")
                again = resolve_paths(
                    library,
                    metadata,
                    ext="mp4",
                    source_path=paths.video,
                    file_info=parse_file_info(paths.video),
                    safe_dirs=[root],
                )
                self.assertEqual(again.video, paths.video)
                self.assertEqual(
                    (await execute_organize(paths.video, paths.video.parent, paths.video.stem)).outcome,
                    PlaceOutcome.PLACED,
                )
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(b"distinct file")
                collision = await execute_organize(source, paths.video.parent, paths.video.stem)
                self.assertEqual(collision.outcome, PlaceOutcome.CONFLICT)
                self.assertEqual(paths.video.read_bytes(), content)
                self.assertEqual(source.read_bytes(), b"distinct file")
                destinations.append(paths.video)
            self.assertNotEqual(*destinations)


if __name__ == "__main__":
    unittest.main()
