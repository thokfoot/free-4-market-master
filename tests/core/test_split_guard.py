"""Data-integrity guards: split/adjustment discontinuity must never book a
phantom SL exit (paper_trader.split_suspected / _bars_sl_tp guard)."""
from datetime import datetime
import pandas as pd
import pytest
import pytz

from paper_trader import _bars_sl_tp, split_suspected

IST = pytz.timezone("Asia/Kolkata")


def ts(y, mo, d, h, mi):
    return pd.Timestamp(datetime(y, mo, d, h, mi), tz="UTC")


# ---------------------------------------------------------------- pure helper
def test_split_suspected_long_halved():
    assert split_suspected("LONG", 50.0, 100.0) is True


def test_split_suspected_long_normal_sl():
    assert split_suspected("LONG", 98.0, 100.0) is False


def test_split_suspected_short_doubled():
    assert split_suspected("SHORT", 210.0, 100.0) is True


def test_split_suspected_short_normal_sl():
    assert split_suspected("SHORT", 102.0, 100.0) is False


def test_split_suspected_bad_inputs_safe():
    assert split_suspected("LONG", float("nan"), 100.0) is False
    assert split_suspected("LONG", 50.0, 0) is False
    assert split_suspected("LONG", None, 100.0) is False


# ------------------------------------------------------- bars-level integration
def _fade_bars(split_crash):
    """FADE_1h INDIAN: entry 04:00 UTC, SL 97. Pre-entry close 99.5.
    Post-entry bar either a real SL touch (low 96.9) or a split-style
    collapse to 48."""
    lo = 48.0 if split_crash else 96.9
    cl = 50.0 if split_crash else 97.2
    return [
        (ts(2026, 1, 5, 3, 30), 100.0, 99.0, 99.5),   # pre-entry anchor
        (ts(2026, 1, 5, 4, 30), 99.8, lo, cl),        # post-entry breach?
    ]


def test_bars_sl_tp_skips_phantom_sl_on_split_bar():
    out = _bars_sl_tp(_fade_bars(split_crash=True), "FADE_1h",
                      IST.localize(datetime(2026, 1, 5, 9, 30)),
                      "LONG", 97.0, 104.0, 5, mode="INDIAN")
    assert out is None, f"split bar must not book an SL exit, got {out}"


def test_bars_sl_tp_real_sl_still_fires():
    out = _bars_sl_tp(_fade_bars(split_crash=False), "FADE_1h",
                      IST.localize(datetime(2026, 1, 5, 9, 30)),
                      "LONG", 97.0, 104.0, 5, mode="INDIAN")
    assert out is not None and out[1] == "SL Hit (intraday)"
    assert out[0] == pytest.approx(97.0)
