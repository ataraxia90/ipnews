import unittest

from bs4 import BeautifulSoup

from monitor import (
    Article,
    collection_keyword_matches,
    extract_date_from_text,
    looks_like_non_article,
    article_stale_reason,
    passes_collection_ip_keyword_filter,
    passes_source_allowlist,
    source_allows_detail_collection_ip_keyword_filter,
    source_requires_collection_ip_keyword_filter,
)


class CollectionKeywordFilterTest(unittest.TestCase):
    def test_targets_white_house_ftc_and_itc_sources(self):
        self.assertTrue(source_requires_collection_ip_keyword_filter("미국 백악관(News)"))
        self.assertTrue(source_requires_collection_ip_keyword_filter("미국 연방거래위원회(FTC)"))
        self.assertTrue(source_requires_collection_ip_keyword_filter("미국 국제무역위원회(ITC)"))
        self.assertTrue(source_requires_collection_ip_keyword_filter("중국 상무부(新闻发布, 뉴스레터)"))
        self.assertTrue(source_requires_collection_ip_keyword_filter("중국 시장감독관리총국(总局, 총국)"))
        self.assertTrue(source_requires_collection_ip_keyword_filter("중국 최고인민검찰원(重点推荐, 중점추천)"))
        self.assertTrue(source_requires_collection_ip_keyword_filter("중국 최고인민법원(最高人民法院新闻, 최고인민법원 뉴스)"))
        self.assertTrue(source_requires_collection_ip_keyword_filter("일본 후생노동성"))
        self.assertTrue(source_requires_collection_ip_keyword_filter("일본 총무성"))
        self.assertTrue(source_requires_collection_ip_keyword_filter("유럽연합 집행위원회(뉴스)"))
        self.assertFalse(source_requires_collection_ip_keyword_filter("미국 특허상표청(USPTO)"))

    def test_detail_keyword_fetch_is_limited_to_us_sources(self):
        self.assertTrue(source_allows_detail_collection_ip_keyword_filter("미국 국제무역위원회(ITC)"))
        self.assertFalse(source_allows_detail_collection_ip_keyword_filter("중국 최고인민검찰원(重点推荐, 중점추천)"))

    def test_keeps_ip_related_items_for_target_sources(self):
        self.assertTrue(
            passes_collection_ip_keyword_filter(
                "미국 백악관(News)",
                "Executive order on artificial intelligence and innovation",
                "https://www.whitehouse.gov/news/",
            )
        )
        self.assertTrue(
            passes_collection_ip_keyword_filter(
                "미국 연방거래위원회(FTC)",
                "FTC acts against counterfeit trademark scams",
                "https://www.ftc.gov/news-events/news/press-releases/",
            )
        )

    def test_rejects_non_ip_items_for_target_sources(self):
        self.assertFalse(
            passes_collection_ip_keyword_filter(
                "미국 국제무역위원회(ITC)",
                "Commission announces new hearing schedule",
                "https://www.usitc.gov/news_releases/",
            )
        )

    def test_rejects_terms_and_glossary_links(self):
        self.assertTrue(
            looks_like_non_article(
                "Product specific terms",
                "https://tax.thomsonreuters.com/en/product-specific-terms",
            )
        )
        self.assertTrue(
            looks_like_non_article(
                "Glossary",
                "https://www.bloomberg.com/glossary",
            )
        )
        self.assertTrue(
            looks_like_non_article(
                "On-demand webinars",
                "https://www.thomsonreuters.com/en-us/posts/on-demand-webinars/",
            )
        )
        self.assertTrue(
            looks_like_non_article(
                "Read blog",
                "https://www.thomsonreuters.com/en-us/posts/innovation/",
            )
        )

    def test_us_slash_dates_are_parsed_for_stale_filtering(self):
        self.assertEqual(extract_date_from_text("04/30/2026 10:00 AM"), "2026-04-30")
        article = Article(
            source="USPTO",
            region="US",
            title="Old subscription item",
            url="https://www.uspto.gov/subscription-center/2026/example",
            summary_raw="",
            published="04/30/2026 10:00 AM",
        )
        self.assertEqual(
            article_stale_reason(article, "2026-05-20", 14),
            "published 2026-04-30 (20 days old)",
        )

    def test_reuters_and_bloomberg_search_allow_article_urls_only(self):
        self.assertTrue(
            passes_source_allowlist(
                "Thomson Reuters - Intellectual Property",
                "https://www.thomsonreuters.com/en-us/posts/innovation/example-article/",
            )
        )
        self.assertTrue(
            passes_source_allowlist(
                "Thomson Reuters - Intellectual Property",
                "https://legal.thomsonreuters.com/blog/example-article/",
            )
        )
        self.assertFalse(
            passes_source_allowlist(
                "Thomson Reuters - Intellectual Property",
                "https://store.legal.thomsonreuters.com/law-products/Practice-Materials/example/p/123",
            )
        )
        self.assertFalse(
            passes_source_allowlist(
                "Thomson Reuters - Intellectual Property",
                "https://tax.thomsonreuters.com/en/product-specific-terms",
            )
        )
        self.assertTrue(
            passes_source_allowlist(
                "Bloomberg - Patent",
                "https://www.bloomberg.com/news/articles/2026-05-19/example-article",
            )
        )
        self.assertTrue(
            passes_source_allowlist(
                "Bloomberg - Patent",
                "https://www.bloomberg.com/opinion/articles/2026-05-18/example-opinion",
            )
        )
        self.assertFalse(
            passes_source_allowlist(
                "Bloomberg - Patent",
                "https://www.bloomberg.com/tos",
            )
        )

    def test_short_keywords_require_word_boundaries(self):
        self.assertFalse(collection_keyword_matches("shipping schedule update"))
        self.assertFalse(collection_keyword_matches("chair announces meeting"))
        self.assertTrue(collection_keyword_matches("IP enforcement update"))
        self.assertTrue(collection_keyword_matches("AI patent guidance"))
        self.assertTrue(collection_keyword_matches("知识产权保护工作进展"))
        self.assertTrue(collection_keyword_matches("专利侵权纠纷案"))
        self.assertFalse(collection_keyword_matches("反垄断反不正当竞争委员会会议"))
        self.assertTrue(collection_keyword_matches("特許出願に関するお知らせ"))
        self.assertTrue(collection_keyword_matches("商標・著作権に関する審議会"))
        self.assertTrue(collection_keyword_matches("EU action plan on intellectual property"))
        self.assertTrue(collection_keyword_matches("counterfeit goods enforcement"))

    def test_rejects_recruitment_items_for_target_sources(self):
        self.assertFalse(
            passes_collection_ip_keyword_filter(
                "중국 최고인민법원(最高人民法院新闻, 최고인민법원 뉴스)",
                "最高人民法院知识产权法庭2026年聘用制书记员招聘公告",
                "https://www.court.gov.cn/zixun/xiangqing/498871.html",
            )
        )

    def test_japan_target_sources_keep_ip_keyword_rows(self):
        fixtures = [
            (
                "일본 후생노동성",
                "ul.m-listNews > li",
                "a[href]",
                """
                <ul class="m-listNews">
                  <li><a href="/stf/test.html">特許出願に関する審議会のお知らせ</a></li>
                  <li><a href="/toukei/itiran/roudou/monthly/r08/2603p/2603p.html">毎月勤労統計調査　令和8年3月分結果速報</a></li>
                </ul>
                """,
            ),
            (
                "일본 총무성",
                "h2.recent_list + dl.icon dd",
                'a[href*="/menu_news/s-news/"]',
                """
                <h2 class="recent_list">新着情報</h2>
                <dl class="icon">
                  <dd><a href="/menu_news/s-news/01test.html">商標・著作権に関する審議会の開催</a></dd>
                  <dd><a href="/menu_news/s-news/01tsushin08_02000202.html">電気通信事業分野における市場検証に関する年次計画</a></dd>
                </dl>
                """,
            ),
        ]

        for source_name, row_selector, title_selector, html in fixtures:
            rows = BeautifulSoup(html, "html.parser").select(row_selector)
            kept = []
            rejected = []
            for row in rows:
                title = row.select_one(title_selector).get_text(" ", strip=True)
                if passes_collection_ip_keyword_filter(source_name, title, "https://example.go.jp/test.html"):
                    kept.append(title)
                else:
                    rejected.append(title)

            self.assertEqual(len(kept), 1)
            self.assertEqual(len(rejected), 1)
            self.assertTrue(collection_keyword_matches(kept[0]))


if __name__ == "__main__":
    unittest.main()
