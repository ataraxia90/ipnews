import unittest

from monitor import canonical_article_url


class UrlCanonicalizationTest(unittest.TestCase):
    def test_euipo_links_use_www_host(self):
        self.assertEqual(
            canonical_article_url("https://euipo.europa.eu/en/news/example"),
            "https://www.euipo.europa.eu/en/news/example",
        )

    def test_other_europa_hosts_are_unchanged(self):
        self.assertEqual(
            canonical_article_url("https://commission.europa.eu/news/example"),
            "https://commission.europa.eu/news/example",
        )


if __name__ == "__main__":
    unittest.main()
