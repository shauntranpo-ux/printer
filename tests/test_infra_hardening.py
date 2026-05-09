"""Tests for infrastructure hardening fixes."""
import os
import sys
import inspect

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot_market
import bot_infra
import bot_stats


def test_bot_market_fstring_no_nested_comprehension():
    """parse_strike warning log must use a variable, not nested dict comprehension."""
    src = inspect.getsource(bot_market.parse_strike)
    assert "{ {k:" not in src, \
        "Nested dict comprehension still in f-string — extract to _diag variable"
    assert "_diag" in src, \
        "parse_strike warning log must use _diag variable"
