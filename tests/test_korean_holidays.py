import tempfile
import unittest
from unittest.mock import Mock, patch

from monitor import (
    fetch_korean_public_holidays_from_api,
    korean_public_holiday_for_date,
    load_korean_public_holidays,
    parse_korean_holiday_payload,
    parse_korean_holiday_xml,
    should_skip_korean_public_holiday_run,
    write_korean_holiday_cache,
)


class KoreanHolidayTest(unittest.TestCase):
    def test_parse_public_data_json_payload(self):
        payload = {
            "response": {
                "body": {
                    "items": {
                        "item": [
                            {"locdate": 20260101, "dateName": "New Year", "isHoliday": "Y"},
                            {"locdate": 20260405, "dateName": "Tree Day", "isHoliday": "N"},
                        ]
                    }
                }
            }
        }

        self.assertEqual(parse_korean_holiday_payload(payload), {"2026-01-01": "New Year"})

    def test_parse_public_data_xml_payload(self):
        xml = """
        <response>
          <body>
            <items>
              <item><locdate>20261005</locdate><dateName>Chuseok</dateName><isHoliday>Y</isHoliday></item>
            </items>
          </body>
        </response>
        """

        self.assertEqual(parse_korean_holiday_xml(xml), {"2026-10-05": "Chuseok"})

    def test_fetch_public_data_monthly_and_merge(self):
        def fake_get(url, params=None, timeout=15):
            month = (params or {}).get("solMonth") or "01"
            response = Mock()
            response.raise_for_status.return_value = None
            response.text = '{"response":{"header":{"resultCode":"00","resultMsg":"NORMAL SERVICE."},"body":{"items":""}}}'
            response.json.return_value = {"response": {"header": {"resultCode": "00"}, "body": {"items": ""}}}
            if month == "01":
                response.text = ""
                response.json.return_value = {
                    "response": {
                        "header": {"resultCode": "00"},
                        "body": {"items": {"item": {"locdate": 20260101, "dateName": "New Year", "isHoliday": "Y"}}},
                    }
                }
            if month == "10":
                response.text = ""
                response.json.return_value = {
                    "response": {
                        "header": {"resultCode": "00"},
                        "body": {"items": {"item": {"locdate": 20261005, "dateName": "Chuseok", "isHoliday": "Y"}}},
                    }
                }
            return response

        with patch("monitor.requests.get", side_effect=fake_get) as get_mock:
            holidays = fetch_korean_public_holidays_from_api(2026, service_key="test-key")

        self.assertEqual(holidays["2026-01-01"], "New Year")
        self.assertEqual(holidays["2026-10-05"], "Chuseok")
        months = [call.kwargs["params"]["solMonth"] for call in get_mock.call_args_list if call.kwargs.get("params")]
        self.assertEqual(months[:12], [f"{month:02d}" for month in range(1, 13)])
        self.assertIn("ServiceKey", get_mock.call_args_list[0].kwargs["params"])

    def test_cache_and_override_are_used_without_api_key(self):
        cfg = {
            "schedule": {
                "korean_public_holiday_overrides": {
                    "2026-08-14": "Temporary holiday",
                    "2027": {"2027-01-04": "Test holiday"},
                }
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            write_korean_holiday_cache(2026, {"2026-01-01": "New Year"}, cache_dir=tmp)

            holidays = load_korean_public_holidays(2026, cfg=cfg, cache_dir=tmp)

        self.assertEqual(holidays["2026-01-01"], "New Year")
        self.assertEqual(holidays["2026-08-14"], "Temporary holiday")
        self.assertNotIn("2027-01-04", holidays)

    def test_holiday_for_date_uses_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_korean_holiday_cache(2026, {"2026-03-01": "March 1st"}, cache_dir=tmp)

            self.assertEqual(
                korean_public_holiday_for_date("2026-03-01", cache_dir=tmp),
                "March 1st",
            )
            self.assertIsNone(korean_public_holiday_for_date("2026-03-02", cache_dir=tmp))

    def test_public_holiday_run_skip_uses_cache_and_config(self):
        cfg = {"schedule": {"skip_korean_public_holidays": True}}
        with tempfile.TemporaryDirectory() as tmp:
            write_korean_holiday_cache(2026, {"2026-01-01": "New Year"}, cache_dir=tmp)

            self.assertEqual(
                should_skip_korean_public_holiday_run("2026-01-01", cfg, cache_dir=tmp),
                "New Year",
            )
            self.assertIsNone(should_skip_korean_public_holiday_run("2026-01-02", cfg, cache_dir=tmp))

    def test_public_holiday_run_skip_can_be_disabled_or_forced(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_korean_holiday_cache(2026, {"2026-01-01": "New Year"}, cache_dir=tmp)

            self.assertIsNone(
                should_skip_korean_public_holiday_run(
                    "2026-01-01",
                    {"schedule": {"skip_korean_public_holidays": False}},
                    cache_dir=tmp,
                )
            )

            with patch.dict("os.environ", {"FORCE_MONITOR_RUN": "true"}):
                self.assertIsNone(
                    should_skip_korean_public_holiday_run(
                        "2026-01-01",
                        {"schedule": {"skip_korean_public_holidays": True}},
                        cache_dir=tmp,
                    )
                )


if __name__ == "__main__":
    unittest.main()
