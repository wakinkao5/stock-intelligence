"""Regression checks for delayed publication and notification gates."""
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import market_cycle as market
import stock_notifications as notifications
from update_intraday import delayed_series, refresh, save_intraday


class PublicationTests(TestCase):
    def test_candles_require_correct_day_and_completed_delay(self):
        payload = {"symbol": "IX0001", "date": "2026-11-26", "data": [
            {"date": "2026-11-26T09:39:00+08:00", "close": 100},
            {"date": "2026-11-26T09:40:00+08:00", "close": 101},
        ]}
        cutoff = datetime.fromisoformat("2026-11-26T09:40:00+08:00")
        self.assertEqual(delayed_series(payload, "IX0001", "2026-11-26", cutoff), [(cutoff, 100)])
        with self.assertRaises(ValueError):
            delayed_series(payload, "IX0043", "2026-11-26", cutoff)

    def test_provider_never_requested_with_unapproved_license(self):
        with patch.dict("os.environ", {"FUGLE_API_KEY": "test"}, clear=True), patch("urllib.request.urlopen") as request:
            with self.assertRaisesRegex(ValueError, "公開展示授權"):
                refresh(None, None, None)
            request.assert_not_called()

    def test_partial_refresh_preserves_verified_chart_and_other_market_quote(self):
        existing = {"date": "2026-11-26", "chart_complete": True,
                    "points": [{"at": "2026-11-26T13:30:00+08:00", "value": 100}],
                    "quotes": [{"symbol": "TAIEX", "quote_at": "2026-11-26T13:31:00+08:00", "value": 100}]}
        partial = {"date": "2026-11-26", "points": [],
                   "quotes": [{"symbol": "TPEX", "quote_at": "2026-11-26T13:31:00+08:00", "value": 99}]}
        with TemporaryDirectory() as directory, patch.object(market, "DATA", Path(directory)):
            market.write("intraday.json", existing)
            save_intraday(partial)
            stored = market.read("intraday.json")
            self.assertEqual(stored["points"], existing["points"])
            self.assertTrue(stored["chart_complete"])
            self.assertEqual({row["symbol"] for row in stored["quotes"]}, {"TAIEX", "TPEX"})

    def test_shared_report_and_alert_are_independent_of_telegram_credentials(self):
        report = {"date": "2026-11-26", "slot": "10:00", "executed_at": "2026-11-26T10:02:00+08:00",
                  "kind": "盤中延遲", "international": [], "forecast": {"status": "未鎖定", "direction": None,
                  "estimated_close": None}, "verification": "等待正式收盤", "quotes": [
                      {"symbol": "TAIEX", "value": 100, "change_pct": 1, "quote_at": "2026-11-26T09:39:00+08:00",
                       "source": "測試"},
                      {"symbol": "TPEX", "value": 99, "change_pct": -1, "quote_at": "2026-11-26T09:39:00+08:00",
                       "source": "測試"},
                  ]}
        with TemporaryDirectory() as directory, patch.object(market, "DATA", Path(directory)):
            status = {"state": "success", "slot": "10:00", "checked_at": report["executed_at"],
                      "report": "report.json", "last_success_at": None}
            market.write("report.json", report)
            with patch.dict("os.environ", {"TELEGRAM_CHAT_ID":"999", "TELEGRAM_BOT_TOKEN":"test"}, clear=True):
                with patch.object(notifications,"verify_pages",return_value=True) as verify, \
                     patch.object(notifications,"reserve",side_effect=[True,False,True]), \
                     patch.object(notifications,"telegram_send") as send:
                    notifications.deliver(status)
                    notifications.deliver(status)
                    verify.assert_called_with("report.json", report)
                    text = send.call_args.args[0]
                    self.assertIn("🔴 加權指數", text)
                    self.assertIn("🟢 上櫃指數", text)
                    self.assertEqual(send.call_count, 1)
                    verify.return_value = False
                    with self.assertRaisesRegex(ValueError,"同步未通過"):
                        notifications.deliver(status)
                    self.assertIn("正式網站未同步", send.call_args.args[0])
                    self.assertNotIn("加權指數 100", send.call_args.args[0])
