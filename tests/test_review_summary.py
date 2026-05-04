import unittest

from monitor import Article, article_stale_reason, build_raw_review_summary_lines


class ReviewSummaryTest(unittest.TestCase):
    def test_review_summary_separates_fetch_seen_and_new_counts(self):
        articles = [
            Article(
                source="Source B",
                region="US",
                title="New article",
                url="https://example.com/new",
                summary_raw="",
                published="2026-05-02",
            )
        ]
        source_records = [
            {
                "name": "Source A",
                "status": "ok",
                "count": 5,
                "new_count": 0,
                "seen_skipped_count": 5,
                "non_article_skipped_count": 0,
            },
            {
                "name": "Source B",
                "status": "ok",
                "count": 2,
                "new_count": 1,
                "seen_skipped_count": 1,
                "non_article_skipped_count": 0,
            },
        ]

        summary = "\n".join(
            build_raw_review_summary_lines(
                articles,
                source_check_records=source_records,
                generated_at="2026-05-02 09:00",
            )
        )

        self.assertIn("신규 기사: 1개", summary)
        self.assertIn("전체 fetch 후보: 7개 / seen 제외 6개 / 비기사 제외 0개", summary)
        self.assertIn("특이사항 없음", summary)
        self.assertNotIn("max_items", summary)

    def test_article_stale_reason_uses_published_date(self):
        old_article = Article(
            source="Source",
            region="US",
            title="Old article",
            url="https://example.com/old",
            summary_raw="",
            published="2024-03-01",
        )
        recent_article = Article(
            source="Source",
            region="US",
            title="Recent article",
            url="https://example.com/recent",
            summary_raw="",
            published="2026-05-03",
        )

        self.assertIn(
            "published 2024-03-01",
            article_stale_reason(old_article, "2026-05-04", 14) or "",
        )
        self.assertIsNone(article_stale_reason(recent_article, "2026-05-04", 14))


if __name__ == "__main__":
    unittest.main()
