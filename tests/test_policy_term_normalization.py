import unittest

from monitor import (
    apply_importance_score_guardrails,
    clamp_score_axis,
    normalize_korean_policy_terms,
)


class PolicyTermNormalizationTest(unittest.TestCase):
    def test_uspto_director_is_translated_as_commissioner(self):
        text = "USPTO 원장(Director)의 재량권과 미국 특허상표청 국장 결정"

        normalized = normalize_korean_policy_terms(text)

        self.assertIn("USPTO 청장(Director)", normalized)
        self.assertIn("미국 특허상표청 청장 결정", normalized)
        self.assertNotIn("USPTO 원장", normalized)
        self.assertNotIn("특허상표청 국장", normalized)

    def test_korean_ip_office_is_moip_not_kipo(self):
        text = "KIPO와 한국 특허청, 대한민국 특허청이 의견을 냈다."

        normalized = normalize_korean_policy_terms(text)

        self.assertEqual(
            normalized,
            "지식재산처(MOIP)와 지식재산처(MOIP), 지식재산처(MOIP)이 의견을 냈다.",
        )

    def test_score_axis_is_clamped(self):
        self.assertEqual(clamp_score_axis("87"), 87)
        self.assertEqual(clamp_score_axis(101), 100)
        self.assertEqual(clamp_score_axis(-1), 0)
        self.assertEqual(clamp_score_axis("bad", default=42), 42)

    def test_low_ip_directness_caps_importance_score(self):
        score, reason = apply_importance_score_guardrails(
            62,
            ip_directness=15,
            policy_materiality=55,
            score_reason="Official trade measure with Korea relevance.",
        )

        self.assertEqual(score, 40)
        self.assertIn("IP", reason)
        self.assertIn("40", reason)

    def test_low_ip_and_policy_axes_cap_mid_high_score(self):
        score, reason = apply_importance_score_guardrails(
            68,
            ip_directness=45,
            policy_materiality=45,
            score_reason="Official but indirect issue.",
        )

        self.assertEqual(score, 55)
        self.assertIn("55", reason)

    def test_high_ip_directness_keeps_score(self):
        score, reason = apply_importance_score_guardrails(
            74,
            ip_directness=80,
            policy_materiality=70,
            score_reason="Direct IP policy update.",
        )

        self.assertEqual(score, 74)
        self.assertEqual(reason, "Direct IP policy update.")


if __name__ == "__main__":
    unittest.main()
