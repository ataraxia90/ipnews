import unittest

from monitor import clamp_score_axis, normalize_korean_policy_terms


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


if __name__ == "__main__":
    unittest.main()
