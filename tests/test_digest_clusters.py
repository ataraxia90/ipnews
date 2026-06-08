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

    def test_recent_sent_topic_suppresses_same_issue_by_token_similarity(self):
        item = analyzed_article(
            "USTR launches Vietnam IP enforcement investigation",
            "ustr-section301-vietnam-ip-2025",
            74,
            source="mlex",
            url="https://www.mlex.com/mlex/intellectual-property/articles/2484121",
        )
        item.summary_ko = (
            "USTR opened a Section 301 investigation into Vietnam's intellectual property "
            "protection and enforcement practices."
        )
        item.topic_label = "USTR Section 301 Vietnam IP enforcement investigation"
        sent_topics = [
            {
                "topic_key": "2026-ustr-section301-vietnam-ip",
                "topic_label": "USTR 2026 Section 301 Vietnam IP investigation",
                "representative_title": (
                    "USTR Announces Section 301 Investigation of Vietnam's Acts, "
                    "Policies, and Practices Related to Intellectual Property Protection"
                ),
                "last_sent_date": "2026-06-01",
                "representative_score": 78,
                "representative_authority": 4,
            }
        ]

        selected, skipped = select_digest_clusters(
            [item],
            top_n=5,
            min_importance=0,
            sent_topics=sent_topics,
            recent_topic_days=3,
            run_date="2026-06-02",
            max_paid_sources=3,
        )

        self.assertEqual(selected, [])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["reason"], "recent_topic")

    def test_recent_sent_topic_keeps_followup_full_text_release(self):
        item = analyzed_article(
            "6月1日起施行！《商业秘密保护规定》全文发布",
            "china-trade-secret-protection-rules-full-text",
            74,
            source="IPRdaily",
            url="https://www.iprdaily.cn/article/full-text-trade-secret-rules",
        )
        item.summary_ko = "중국 상업비밀 보호 규정의 전문이 공개되었다."
        item.topic_label = "商业秘密保护规定 全文"
        sent_topics = [
            {
                "topic_key": "china-ip-new-rules-june-2026",
                "topic_label": "2026년 6월 시행 중국 지식재산 신규 규정 모음",
                "representative_title": "2026.6.1日起！这些知识产权新规正式实施",
                "representative_tokens": [
                    "2026", "知识产权", "新规", "商业秘密", "保护", "规定", "施行"
                ],
                "last_sent_date": "2026-06-02",
                "representative_score": 72,
                "representative_authority": 3,
                "representative_region": "Test Region",
            }
        ]

        selected, skipped = select_digest_clusters(
            [item],
            top_n=5,
            min_importance=0,
            sent_topics=sent_topics,
            recent_topic_days=3,
            run_date="2026-06-04",
            max_paid_sources=3,
        )

        self.assertEqual(skipped, [])
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].representative.title, item.title)

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
