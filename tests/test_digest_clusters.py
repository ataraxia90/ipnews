import unittest

from monitor import (
    AnalyzedArticle,
    digest_cluster_topic_key,
    render_telegram_digest,
    send_telegram_messages,
    select_digest_clusters,
)


def analyzed_article(title, topic_key, score, source="Test Source", url=None, raw_excerpt=""):
    return AnalyzedArticle(
        source=source,
        region="Test Region",
        title=title,
        url=url or f"https://example.com/{title.lower().replace(' ', '-')}",
        published="2026-05-02",
        summary_ko=f"Summary for {title}",
        importance_score=score,
        category="Test",
        key_points=[],
        raw_excerpt=raw_excerpt,
        topic_key=topic_key,
        topic_label=topic_key,
        issue_region="Test Region",
    )


class DigestClusterSelectionTest(unittest.TestCase):
    def test_top_n_counts_clustered_issues_not_articles(self):
        items = [
            analyzed_article("Issue A report 1", "issue-a", 95),
            analyzed_article("Issue A report 2", "issue-a", 94),
            analyzed_article("Issue A report 3", "issue-a", 93),
            analyzed_article("Issue B", "issue-b", 80),
            analyzed_article("Issue C", "issue-c", 70),
            analyzed_article("Issue D", "issue-d", 60),
            analyzed_article("Issue E", "issue-e", 50),
            analyzed_article("Issue F", "issue-f", 40),
        ]

        selected, skipped = select_digest_clusters(
            items,
            top_n=5,
            min_importance=0,
            sent_topics=[],
            recent_topic_days=0,
            run_date="2026-05-02",
        )

        self.assertEqual(skipped, [])
        self.assertEqual(len(selected), 5)
        self.assertEqual(len(selected[0].items), 3)
        self.assertEqual(
            [digest_cluster_topic_key(cluster) for cluster in selected],
            ["issue-a", "issue-b", "issue-c", "issue-d", "issue-e"],
        )

    def test_equal_scores_prefer_authority_then_direct_reporting(self):
        official = analyzed_article(
            "Official patent policy update",
            "official-policy",
            62,
            source="USPTO",
            url="https://www.uspto.gov/news/official-policy-update",
        )
        direct = analyzed_article(
            "Court decision on patent venue",
            "court-decision",
            62,
            source="IP Watchdog",
            url="https://ipwatchdog.com/2026/05/22/court-decision/",
            raw_excerpt="The court issued a decision in the patent dispute.",
        )
        general = analyzed_article(
            "General patent commentary",
            "general-commentary",
            62,
            source="IP Watchdog",
            url="https://ipwatchdog.com/2026/05/22/general-commentary/",
        )

        selected, skipped = select_digest_clusters(
            [general, direct, official],
            top_n=3,
            min_importance=0,
            sent_topics=[],
            recent_topic_days=0,
            run_date="2026-05-02",
            max_paid_sources=3,
        )

        self.assertEqual(skipped, [])
        self.assertEqual(
            [cluster.representative.title for cluster in selected],
            [
                "Official patent policy update",
                "Court decision on patent venue",
                "General patent commentary",
            ],
        )

    def test_digest_title_includes_run_date(self):
        items = [analyzed_article("Issue A", "issue-a", 95)]
        selected, _ = select_digest_clusters(
            items,
            top_n=5,
            min_importance=0,
            sent_topics=[],
            recent_topic_days=0,
            run_date="2026-05-02",
        )

        digest = render_telegram_digest(selected, run_date="2026-05-02")
        first_line = digest.splitlines()[0]

        self.assertTrue(first_line.startswith("< "))
        self.assertIn("2026", first_line)
        self.assertIn("5", first_line)
        self.assertIn("2", first_line)

    def test_digest_does_not_show_related_cluster_count(self):
        items = [
            analyzed_article("Issue A report 1", "issue-a", 95),
            analyzed_article("Issue A report 2", "issue-a", 94),
        ]
        selected, _ = select_digest_clusters(
            items,
            top_n=5,
            min_importance=0,
            sent_topics=[],
            recent_topic_days=0,
            run_date="2026-05-02",
        )

        digest = render_telegram_digest(selected, run_date="2026-05-02")

        self.assertNotIn("related", digest.lower())
        self.assertNotIn("1 item", digest.lower())

    def test_same_title_clusters_even_when_topic_keys_differ(self):
        items = [
            analyzed_article(
                "USPTO achieves significant progress in reducing patent application Notice of Allowance mailing pendency",
                "uspto-noa-pendency-reduction-2026",
                62,
                source="USPTO Subscription Center",
                url="https://www.uspto.gov/subscription-center/2026/uspto-achieves-significant-progress-reducing-patent-application-notice",
            ),
            analyzed_article(
                "USPTO achieves significant progress in reducing patent application Notice of Allowance mailing pendency",
                "uspto-noa-mailing-pendency-reduction",
                58,
                source="USPTO GovDelivery",
                url="https://content.govdelivery.com/accounts/USPTO/bulletins/417db99",
            ),
        ]

        selected, skipped = select_digest_clusters(
            items,
            top_n=5,
            min_importance=0,
            sent_topics=[],
            recent_topic_days=0,
            run_date="2026-05-29",
        )

        self.assertEqual(skipped, [])
        self.assertEqual(len(selected), 1)
        self.assertEqual(len(selected[0].items), 2)

    def test_topic_key_mismatch_falls_back_to_token_similarity(self):
        items = [
            analyzed_article(
                "USPTO reduces Notice of Allowance mailing pendency for patent applications",
                "uspto-noa-pendency-reduction-2026",
                62,
                source="USPTO Subscription Center",
                url="https://www.uspto.gov/subscription-center/noa-pendency",
            ),
            analyzed_article(
                "USPTO cuts patent application Notice of Allowance mailing pendency",
                "uspto-noa-mailing-pendency-reduction",
                58,
                source="USPTO GovDelivery",
                url="https://content.govdelivery.com/accounts/USPTO/bulletins/noa",
            ),
        ]

        selected, _ = select_digest_clusters(
            items,
            top_n=5,
            min_importance=0,
            sent_topics=[],
            recent_topic_days=0,
            run_date="2026-05-29",
        )

        self.assertEqual(len(selected), 1)
        self.assertEqual(len(selected[0].items), 2)

    def test_send_telegram_messages_returns_digest_chunk_count(self):
        cfg = {"telegram": {"digest_send_enabled": False}}
        text = "IP Digest - 2026-05-02 top 1\n\n" + ("a" * 3600)

        count = send_telegram_messages(
            text,
            cfg,
            chat_id_env="TELEGRAM_DIGEST_CHAT_ID",
            enabled_key="digest_send_enabled",
        )

        self.assertEqual(count, 3)


if __name__ == "__main__":
    unittest.main()
