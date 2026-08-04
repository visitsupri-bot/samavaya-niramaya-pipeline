"""
tests/test_builder.py — unit tests for builder.py user-data preservation logic.
"""

from datetime import date
from unittest.mock import patch

from builder import build, USER_DATA_KEYS


def test_build_preserves_user_data_keys():
    """Pipeline build() must preserve user-data keys fetched from live JSON."""
    live_sections = {
        'participants': [{'id': 'par_real', 'name': 'Real User', 'plan': 'monthly'}],
        'attendance':   {'cls_001': {'2026-08-04': ['par_real']}},
        'invoices':     {'cls_001': [{'id': 'inv_001', 'amount': 4800}]},
        'venues':       [{'id': 'v1', 'name': 'Test Studio'}],
        'week_overrides': {},
        'venue_pipeline': [],
        'wisdom_favourites': [],
    }

    with patch('builder.fetch_live_user_data', return_value=live_sections):
        payload = build(date(2026, 8, 4), source_json=None)

    result_sections = payload.get('sections', {})
    assert result_sections.get('participants') == live_sections['participants'], 'participants should be preserved from live data'
    assert result_sections.get('attendance') == live_sections['attendance'], 'attendance should be preserved from live data'
    assert result_sections.get('venues') == live_sections['venues'], 'venues should be preserved from live data'


def test_build_uses_template_when_no_live_data():
    """Pipeline build() falls back to template values when live fetch fails."""
    with patch('builder.fetch_live_user_data', return_value={}):
        payload = build(date(2026, 8, 4), source_json=None)
    assert 'participants' in payload.get('sections', {}), 'participants from template should be present when no live data'
