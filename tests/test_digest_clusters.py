import unittest

from monitor import (
    AnalyzedArticle,
    digest_cluster_topic_key,
    render_telegram_digest,
    send_telegram_messages,
    select_digest_clusters,
)


def analyzed_article(title, topic_key, score):
    return AnalyzedArticle(
        source="Test Source",
        region="Test Region",
        title=title,
        url=f"https://example.com/{title.lower().replace(' ', '-')}",
        published="2026-05-02",
        summary_ko=f"Summary for {title}",
        importance_score=score,
        category="Test",
        key_points=[],
        raw_excerpt="",
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

        self.assertTrue(digest.startswith("IP 동향 Digest - 2026-05-02 상위 1건"))

    def test_send_telegram_messages_returns_digest_chunk_count(self):
        cfg = {"telegram": {"digest_send_enabled": False}}
        text = "IP 동향 Digest - 2026-05-02 상위 1건\n\n" + ("a" * 3600)

        count = send_telegram_messages(
            text,
            cfg,
            chat_id_env="TELEGRAM_DIGEST_CHAT_ID",
            enabled_key="digest_send_enabled",
        )

        self.assertEqual(count, 3)


if __name__ == "__main__":
    unittest.main()
