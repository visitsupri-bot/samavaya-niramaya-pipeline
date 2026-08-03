"""
tests/test_fetcher.py — Unit tests for fetcher.py
==================================================
All tests are fully mocked (no network calls).
19 test cases covering:
  - Trend entry structure (YouTube & Google Trends)
  - Tag / query fallback logic
  - Per-niche failure isolation
  - Empty DataFrame handling
  - Exception handling & graceful degradation
  - Partial success (one source fails, other succeeds)
  - Both-fail → fetch_opportunity returns None
"""

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

# Ensure the project root is on the path so we can import fetcher directly.
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_yt_item(title="Test Video", video_id="abc123", channel="TestCh",
                  published_at="2025-01-01T00:00:00Z", query="yoga India"):
    return {
        "id": {"videoId": video_id},
        "snippet": {
            "title": title,
            "channelTitle": channel,
            "publishedAt": published_at,
        },
        "_query": query,
    }


def _mock_youtube_build(items_per_query: list[dict] | None = None, raise_on_query: str | None = None):
    """Return a mock googleapiclient build() that yields items_per_query."""
    from googleapiclient.errors import HttpError  # real import for exception class

    mock_response = {
        "items": items_per_query or [
            {
                "id": {"videoId": "v1"},
                "snippet": {
                    "title": "Yoga Beginners India",
                    "channelTitle": "Yoga Channel",
                    "publishedAt": "2025-03-01T00:00:00Z",
                },
            }
        ]
    }

    def _execute_side_effect():
        if raise_on_query:
            raise HttpError(resp=MagicMock(status=403), content=b"quota")
        return mock_response

    mock_search_list = MagicMock()
    mock_search_list.execute.side_effect = _execute_side_effect

    mock_search = MagicMock()
    mock_search.list.return_value = mock_search_list

    mock_yt = MagicMock()
    mock_yt.search.return_value = mock_search

    mock_build = MagicMock(return_value=mock_yt)
    return mock_build


def _make_trends_df(keywords: list[str], values: list[int] | None = None, rising: bool = True):
    """Build a minimal pandas-like mock DataFrame."""
    import pandas as pd
    import numpy as np

    n = 7
    data = {}
    for i, kw in enumerate(keywords):
        if values:
            data[kw] = [values[i]] * n
        else:
            # rising: last > first
            data[kw] = list(range(1, n + 1)) if rising else list(range(n, 0, -1))
    return pd.DataFrame(data)


# ─────────────────────────────────────────────────────────────────────────────
# Tests: fetch_youtube_trends
# ─────────────────────────────────────────────────────────────────────────────

class TestFetchYoutubeTrends(unittest.TestCase):

    def setUp(self):
        # Reset cached config between tests
        import fetcher
        fetcher._fetch_config = None

    # 1. Returns [] when api_key is empty string
    def test_empty_api_key_returns_empty_list(self):
        import fetcher
        result = fetcher.fetch_youtube_trends("", ["yoga India"])
        self.assertEqual(result, [])

    # 2. Returns [] when api_key is None
    def test_none_api_key_returns_empty_list(self):
        import fetcher
        result = fetcher.fetch_youtube_trends(None, ["yoga India"])  # type: ignore[arg-type]
        self.assertEqual(result, [])

    # 3. Trend entry has all required keys
    def test_entry_has_required_keys(self):
        import fetcher
        mock_build = _mock_youtube_build()
        from googleapiclient import errors as _gae
        fake_discovery = types.ModuleType("googleapiclient.discovery")
        fake_discovery.build = mock_build
        fake_errors = types.ModuleType("googleapiclient.errors")
        fake_errors.HttpError = _gae.HttpError

        with patch.dict("sys.modules", {
            "googleapiclient.discovery": fake_discovery,
            "googleapiclient.errors": fake_errors,
        }):
            result = fetcher.fetch_youtube_trends("FAKE_KEY", ["yoga India"])

        self.assertTrue(len(result) > 0)
        entry = result[0]
        for key in ("title", "video_id", "channel", "published_at", "query"):
            self.assertIn(key, entry, f"Missing key: {key}")

    # 4. query field equals the originating search query
    def test_entry_query_matches_input(self):
        import fetcher
        from googleapiclient import errors as _gae
        fake_discovery = types.ModuleType("googleapiclient.discovery")
        fake_discovery.build = _mock_youtube_build()
        fake_errors = types.ModuleType("googleapiclient.errors")
        fake_errors.HttpError = _gae.HttpError
        with patch.dict("sys.modules", {
            "googleapiclient.discovery": fake_discovery,
            "googleapiclient.errors": fake_errors,
        }):
            result = fetcher.fetch_youtube_trends("KEY", ["hatha yoga"])
        self.assertTrue(all(e["query"] == "hatha yoga" for e in result))

    # 5. Per-query HttpError does NOT abort remaining queries
    def test_per_query_http_error_continues(self):
        import fetcher
        from googleapiclient import errors as _gae

        call_count = {"n": 0}

        def _execute():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise _gae.HttpError(resp=MagicMock(status=403), content=b"quota")
            return {
                "items": [{
                    "id": {"videoId": "v2"},
                    "snippet": {
                        "title": "Second Query Result",
                        "channelTitle": "Ch",
                        "publishedAt": "2025-01-01T00:00:00Z",
                    },
                }]
            }

        mock_list = MagicMock()
        mock_list.execute.side_effect = _execute
        mock_search = MagicMock()
        mock_search.list.return_value = mock_list
        mock_yt = MagicMock()
        mock_yt.search.return_value = mock_search

        fake_discovery = types.ModuleType("googleapiclient.discovery")
        fake_discovery.build = MagicMock(return_value=mock_yt)
        fake_errors = types.ModuleType("googleapiclient.errors")
        fake_errors.HttpError = _gae.HttpError

        with patch.dict("sys.modules", {
            "googleapiclient.discovery": fake_discovery,
            "googleapiclient.errors": fake_errors,
        }):
            result = fetcher.fetch_youtube_trends("KEY", ["q1", "q2"])

        # q1 failed, q2 succeeded → should have result from q2
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "Second Query Result")

    # 6. Returns [] on unexpected top-level exception
    def test_unexpected_exception_returns_empty_list(self):
        import fetcher
        from googleapiclient import errors as _gae

        fake_discovery = types.ModuleType("googleapiclient.discovery")
        fake_discovery.build = MagicMock(side_effect=RuntimeError("boom"))
        fake_errors = types.ModuleType("googleapiclient.errors")
        fake_errors.HttpError = _gae.HttpError

        with patch.dict("sys.modules", {
            "googleapiclient.discovery": fake_discovery,
            "googleapiclient.errors": fake_errors,
        }):
            result = fetcher.fetch_youtube_trends("KEY", ["q1"])

        self.assertEqual(result, [])

    # 7. Returns [] when google-api-python-client is not installed
    def test_missing_package_returns_empty_list(self):
        import fetcher
        with patch.dict("sys.modules", {
            "googleapiclient": None,
            "googleapiclient.discovery": None,
            "googleapiclient.errors": None,
        }):
            result = fetcher.fetch_youtube_trends("KEY", ["q1"])
        self.assertEqual(result, [])


# ─────────────────────────────────────────────────────────────────────────────
# Tests: fetch_google_trends
# ─────────────────────────────────────────────────────────────────────────────

class TestFetchGoogleTrends(unittest.TestCase):

    def setUp(self):
        import fetcher
        fetcher._fetch_config = None

    # 8. Returns [] for empty keywords list
    def test_empty_keywords_returns_empty_list(self):
        import fetcher
        result = fetcher.fetch_google_trends([])
        self.assertEqual(result, [])

    # 9. Entry has required keys
    def test_entry_has_required_keys(self):
        import fetcher
        import pandas as pd

        df = _make_trends_df(["yoga classes near me"], rising=True)

        mock_pt = MagicMock()
        mock_pt.interest_over_time.return_value = df

        fake_pytrends = types.ModuleType("pytrends.request")
        fake_pytrends.TrendReq = MagicMock(return_value=mock_pt)

        with patch.dict("sys.modules", {"pytrends": MagicMock(), "pytrends.request": fake_pytrends}):
            result = fetcher.fetch_google_trends(["yoga classes near me"])

        self.assertEqual(len(result), 1)
        for key in ("keyword", "avg_interest", "peak_interest", "is_rising"):
            self.assertIn(key, result[0])

    # 10. is_rising is True when last > first
    def test_is_rising_true_when_ascending(self):
        import fetcher
        import pandas as pd

        df = _make_trends_df(["sound healing"], rising=True)
        mock_pt = MagicMock()
        mock_pt.interest_over_time.return_value = df

        fake_pytrends_module = types.ModuleType("pytrends.request")
        fake_pytrends_module.TrendReq = MagicMock(return_value=mock_pt)

        with patch.dict("sys.modules", {"pytrends": MagicMock(), "pytrends.request": fake_pytrends_module}):
            result = fetcher.fetch_google_trends(["sound healing"])

        self.assertTrue(result[0]["is_rising"])

    # 11. is_rising is False when last < first
    def test_is_rising_false_when_descending(self):
        import fetcher
        import pandas as pd

        df = _make_trends_df(["sound healing"], rising=False)
        mock_pt = MagicMock()
        mock_pt.interest_over_time.return_value = df

        fake_pytrends_module = types.ModuleType("pytrends.request")
        fake_pytrends_module.TrendReq = MagicMock(return_value=mock_pt)

        with patch.dict("sys.modules", {"pytrends": MagicMock(), "pytrends.request": fake_pytrends_module}):
            result = fetcher.fetch_google_trends(["sound healing"])

        self.assertFalse(result[0]["is_rising"])

    # 12. Returns [] on empty DataFrame
    def test_empty_dataframe_returns_empty_list(self):
        import fetcher
        import pandas as pd

        mock_pt = MagicMock()
        mock_pt.interest_over_time.return_value = pd.DataFrame()

        fake_pytrends_module = types.ModuleType("pytrends.request")
        fake_pytrends_module.TrendReq = MagicMock(return_value=mock_pt)

        with patch.dict("sys.modules", {"pytrends": MagicMock(), "pytrends.request": fake_pytrends_module}):
            result = fetcher.fetch_google_trends(["yoga"])

        self.assertEqual(result, [])

    # 13. Retries on transient exception and eventually returns []
    def test_retries_exhausted_returns_empty_list(self):
        import fetcher

        mock_pt = MagicMock()
        mock_pt.interest_over_time.side_effect = ConnectionError("timeout")

        fake_pytrends_module = types.ModuleType("pytrends.request")
        fake_pytrends_module.TrendReq = MagicMock(return_value=mock_pt)

        with patch("fetcher.time.sleep", return_value=None):  # don't actually sleep
            with patch.dict("sys.modules", {"pytrends": MagicMock(), "pytrends.request": fake_pytrends_module}):
                result = fetcher.fetch_google_trends(["yoga"])

        self.assertEqual(result, [])
        # Should have retried _TRENDS_MAX_RETRIES times
        self.assertEqual(mock_pt.interest_over_time.call_count, fetcher._TRENDS_MAX_RETRIES)

    # 14. Returns [] when pytrends not installed
    def test_missing_pytrends_returns_empty_list(self):
        import fetcher
        with patch.dict("sys.modules", {"pytrends": None, "pytrends.request": None}):
            result = fetcher.fetch_google_trends(["yoga"])
        self.assertEqual(result, [])

    # 15. Skips keyword not present in DataFrame columns
    def test_missing_keyword_in_df_is_skipped(self):
        import fetcher
        import pandas as pd

        # Only "yoga" in DataFrame, not "meditation"
        df = _make_trends_df(["yoga"], rising=True)
        mock_pt = MagicMock()
        mock_pt.interest_over_time.return_value = df

        fake_pytrends_module = types.ModuleType("pytrends.request")
        fake_pytrends_module.TrendReq = MagicMock(return_value=mock_pt)

        with patch.dict("sys.modules", {"pytrends": MagicMock(), "pytrends.request": fake_pytrends_module}):
            result = fetcher.fetch_google_trends(["yoga", "meditation"])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["keyword"], "yoga")


# ─────────────────────────────────────────────────────────────────────────────
# Tests: fetch_opportunity
# ─────────────────────────────────────────────────────────────────────────────

class TestFetchOpportunity(unittest.TestCase):

    def setUp(self):
        import fetcher
        fetcher._fetch_config = None

    def _yt_entry(self, title="Yoga Video", query="yoga India"):
        return {
            "title": title,
            "video_id": "v1",
            "channel": "Ch",
            "published_at": "2025-01-01T00:00:00Z",
            "query": query,
        }

    def _trend_entry(self, keyword="yoga classes near me", rising=True):
        return {
            "keyword": keyword,
            "avg_interest": 60,
            "peak_interest": 80,
            "is_rising": rising,
        }

    # 16. Returns None for unknown niche
    def test_unknown_niche_returns_none(self):
        import fetcher
        result = fetcher.fetch_opportunity("KEY", "nonexistent_niche")
        self.assertIsNone(result)

    # 17. Returns None when both sources fail
    def test_both_sources_empty_returns_none(self):
        import fetcher
        with patch.object(fetcher, "fetch_youtube_trends", return_value=[]):
            with patch.object(fetcher, "fetch_google_trends", return_value=[]):
                result = fetcher.fetch_opportunity("KEY", "yoga_india")
        self.assertIsNone(result)

    # 18. Returns dict with required keys on partial success (YouTube only)
    def test_partial_success_youtube_only(self):
        import fetcher
        with patch.object(fetcher, "fetch_youtube_trends", return_value=[self._yt_entry()]):
            with patch.object(fetcher, "fetch_google_trends", return_value=[]):
                result = fetcher.fetch_opportunity("KEY", "yoga_india")

        self.assertIsNotNone(result)
        assert result is not None
        for key in ("market_headline", "trend_data", "trends", "venues"):
            self.assertIn(key, result)
        # trends[] has 2 entries: YouTube + Google Trends (fallback with note)
        self.assertEqual(len(result["trends"]), 2)
        platforms = [t["platform"] for t in result["trends"]]
        self.assertIn("YouTube India", platforms)
        self.assertIn("Google Trends India", platforms)
        self.assertEqual(result["trend_data"]["youtube_count"], 1)
        self.assertEqual(result["trend_data"]["trends_count"], 0)
        # venues are populated from config pool
        self.assertGreater(len(result["venues"]), 0)

    # 19. Returns dict with required keys on partial success (trends only)
    def test_partial_success_trends_only(self):
        import fetcher
        trend = self._trend_entry()
        with patch.object(fetcher, "fetch_youtube_trends", return_value=[]):
            with patch.object(fetcher, "fetch_google_trends", return_value=[trend]):
                result = fetcher.fetch_opportunity("", "sound_healing")

        self.assertIsNotNone(result)
        assert result is not None
        for key in ("market_headline", "trend_data", "trends", "venues"):
            self.assertIn(key, result)
        self.assertEqual(result["trend_data"]["youtube_count"], 0)
        self.assertEqual(result["trend_data"]["trends_count"], 1)
        self.assertGreater(len(result["market_headline"]), 0)
        self.assertEqual(len(result["trends"]), 2)
        self.assertGreater(len(result["venues"]), 0)

    # Bonus — 20th test: trend_direction is "upward" for rising trend
    def test_trend_direction_upward_for_rising(self):
        import fetcher
        trend = self._trend_entry(rising=True)
        with patch.object(fetcher, "fetch_youtube_trends", return_value=[]):
            with patch.object(fetcher, "fetch_google_trends", return_value=[trend]):
                result = fetcher.fetch_opportunity("", "yoga_india")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("upward", result["market_headline"])

    # 21st test: trend_direction is "stable" for falling trend
    def test_trend_direction_stable_for_falling(self):
        import fetcher
        trend = self._trend_entry(rising=False)
        with patch.object(fetcher, "fetch_youtube_trends", return_value=[]):
            with patch.object(fetcher, "fetch_google_trends", return_value=[trend]):
                result = fetcher.fetch_opportunity("", "yoga_india")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("stable", result["market_headline"])


if __name__ == "__main__":
    unittest.main()
