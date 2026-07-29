"""
Shared pytest fixtures for the FREE 3-Market paper trade test suite.

Provides:
    - Time freezing (monkeypatched datetime.now)
    - File path redirection (tmp_path for all log/portfolio files)
    - Environment variable setup (Telegram tokens)
    - Reusable monkeypatch setups for yfinance, requests, etc.

Usage:
    def test_something(frozen_time, isolated_fs, mock_telegram):
        ...
"""

import os
import json
import math
import pytest
import pandas as pd
from datetime import datetime, timezone
from unittest.mock import MagicMock


# ======================================================================
# pytest hooks
# ======================================================================

def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')")
    config.addinivalue_line("markers", "replay: marks tests as replay/end-to-end tests")


# ======================================================================
# Time freezing
# ======================================================================

@pytest.fixture
def frozen_time(monkeypatch):
    """
    Freeze datetime.now(IST) and datetime.utcnow() to a known timestamp.
    
    Frozen time: 2026-01-15 10:30:00 IST (05:00:00 UTC)
    """
    import pytz
    IST = pytz.timezone("Asia/Kolkata")
    
    class FrozenDateTime:
        """A datetime class that always returns the frozen time."""
        
        _FROZEN_NAIVE = datetime(2026, 1, 15, 10, 30, 0)
        
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return IST.localize(cls._FROZEN_NAIVE)
            return cls._FROZEN_NAIVE
        
        @classmethod
        def utcnow(cls):
            return cls._FROZEN_NAIVE.replace(tzinfo=timezone.utc)
        
        @classmethod
        def strptime(cls, date_string, format):
            return datetime.strptime(date_string, format)
    
    # Patch paper_trader's module-level datetime reference
    monkeypatch.setattr("paper_trader.datetime", FrozenDateTime)
    
    return FrozenDateTime._FROZEN_NAIVE


# ======================================================================
# Filesystem isolation (tmp_path based)
# ======================================================================

@pytest.fixture
def isolated_fs(monkeypatch, tmp_path):
    """
    Redirect all log/portfolio file paths to a temp directory.
    
    This ensures:
    - No test writes to real logs/ directory
    - Each test gets a fresh, empty filesystem
    - No cross-test contamination
    """
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    
    import paper_trader
    
    monkeypatch.setattr(paper_trader, "LOG_DIR", str(log_dir))
    monkeypatch.setattr(paper_trader, "PAPER_FILE", str(log_dir / "paper_trades.csv"))
    monkeypatch.setattr(paper_trader, "PORTFOLIO_FILE", str(log_dir / "portfolio.json"))
    monkeypatch.setattr(paper_trader, "STRATEGY_STATS_FILE", str(log_dir / "strategy_stats.json"))
    monkeypatch.setattr(paper_trader, "AUDIT_FILE", str(log_dir / "trade_audit.json"))
    monkeypatch.setattr(paper_trader, "LIVE_STATE_FILE_PT", str(log_dir / "live_pnl_state.json"))
    
    # Also patch logger.py paths
    import logger
    monkeypatch.setattr(logger, "LOG_DIR", str(log_dir))
    
    # Initialize system in the isolated environment
    paper_trader.initialize_system()
    
    return tmp_path  # Return tmp_path so tests can access it directly


# ======================================================================
# Environment variables
# ======================================================================

@pytest.fixture
def mock_env(monkeypatch):
    """
    Set up test environment variables.
    
    Telegram tokens are set to dummy values — no real Telegram calls happen.
    """
    monkeypatch.setenv("TELEGRAM_TOKEN", "test_token_12345")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")
    
    # Re-import config to pick up new env vars
    import config
    monkeypatch.setattr(config, "TELEGRAM_TOKEN", "test_token_12345")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "-1001234567890")


# ======================================================================
# Telegram mocking
# ======================================================================

@pytest.fixture
def mock_telegram(monkeypatch):
    """
    Mock requests.post to capture Telegram messages instead of sending them.
    
    Captured messages are stored in a list for later assertion:
        captured_messages = []
        mock_telegram(monkeypatch)
        # ... run code that sends TG messages ...
        # assert len(captured_messages) == 1
        # assert "SL HIT" in captured_messages[0]["data"]["text"]
    """
    captured = []
    
    def mock_post(url, data=None, **kwargs):
        captured.append({"url": url, "data": data})
        response = MagicMock()
        response.status_code = 200
        response.text = "ok"
        return response
    
    monkeypatch.setattr("paper_trader.requests.post", mock_post)
    
    # Also patch in live_pnl_updater if it's imported
    try:
        import live_pnl_updater
        monkeypatch.setattr(live_pnl_updater, "requests", MagicMock())
        monkeypatch.setattr(live_pnl_updater.requests, "post", mock_post)
    except (ImportError, AttributeError):
        pass
    
    return captured  # Tests can inspect captured messages


# ======================================================================
# yfinance mocking
# ======================================================================

@pytest.fixture
def mock_yfinance(monkeypatch):
    """
    Mock yfinance.download to return frozen test data.
    
    The mock returns a simple DataFrame with predefined OHLC values.
    For customized data, override with monkeypatch in individual tests.
    """
    import yfinance as yf
    
    def mock_download(ticker, period=None, interval=None, progress=False, auto_adjust=True, **kwargs):
        """Return a simple OHLC DataFrame with known values."""
        import pandas as pd
        from datetime import datetime
        
        dates = pd.date_range(start="2026-01-01", periods=30, freq="D")
        data = {
            "Open": [450.0] * 30,
            "High": [455.0] * 30,
            "Low": [445.0] * 30,
            "Close": [448.0] * 30,
            "Volume": [1000000] * 30,
        }
        df = pd.DataFrame(data, index=dates)
        df.index.name = "Date"
        
        # Add MultiIndex columns if that's what yfinance returns
        if isinstance(ticker, list) and len(ticker) > 1:
            # Multiple tickers
            arrays = [[t for t in ticker for _ in ["Open", "High", "Low", "Close", "Volume"]],
                      ["Open", "High", "Low", "Close", "Volume"] * len(ticker)]
            df.columns = pd.MultiIndex.from_arrays(arrays)
        
        return df
    
    monkeypatch.setattr(yf, "download", mock_download)
    return mock_download


# ======================================================================
# Combined fixtures (convenience)
# ======================================================================

@pytest.fixture
def test_env(frozen_time, isolated_fs, mock_env, mock_telegram, mock_yfinance):
    """
    Complete test environment setup:
    - Time frozen to 2026-01-15 10:30:00 IST
    - Filesystem isolated to tmp_path
    - Telegram mocked
    - yfinance mocked
    - System initialized
    
    This is the recommended fixture for most tests.
    """
    return {
        "tmp_path": isolated_fs,
    }
