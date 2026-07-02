"""Tests for live win rate calibration from settled trade DB."""
import sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _setup_test_db():
    """Create a temp DB file and init schema."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    import bot_state
    original_db = bot_state._DB_FILE
    bot_state._DB_FILE = tmp.name
    import bot_infra
    bot_infra.init_db()
    return tmp.name, original_db


def test_update_wr_bucket_increments_counts():
    """After 15 wins and 5 losses, empirical WR = 0.75."""
    from bot_infra import _update_wr_bucket, _get_empirical_wr
    import bot_state
    db_path, orig = _setup_test_db()
    try:
        for _ in range(15):
            _update_wr_bucket("ETH", 0.006, 4.0, "win", "live")
        for _ in range(5):
            _update_wr_bucket("ETH", 0.006, 4.0, "loss", "live")
        result = _get_empirical_wr("ETH", 0.006, 4.0, "live", min_samples=20)
        assert result is not None, "Should have empirical WR after 20 samples"
        assert abs(result - 0.75) < 0.01, f"Expected 15/20=0.75, got {result:.3f}"
    finally:
        bot_state._DB_FILE = orig
        os.unlink(db_path)


def test_get_empirical_wr_returns_none_below_min_samples():
    """Returns None when bucket has < min_samples trades."""
    from bot_infra import _update_wr_bucket, _get_empirical_wr
    import bot_state
    db_path, orig = _setup_test_db()
    try:
        for _ in range(10):
            _update_wr_bucket("BTC", 0.004, 7.0, "win", "live")
        result = _get_empirical_wr("BTC", 0.004, 7.0, "live", min_samples=20)
        assert result is None, f"Expected None with only 10 samples, got {result}"
    finally:
        bot_state._DB_FILE = orig
        os.unlink(db_path)


def test_wr_buckets_isolated_by_asset():
    """ETH and BTC WR buckets are independent."""
    from bot_infra import _update_wr_bucket, _get_empirical_wr
    import bot_state
    db_path, orig = _setup_test_db()
    try:
        for _ in range(20):
            _update_wr_bucket("ETH", 0.006, 4.0, "win", "live")
        for _ in range(20):
            _update_wr_bucket("BTC", 0.006, 4.0, "loss", "live")
        eth_wr = _get_empirical_wr("ETH", 0.006, 4.0, "live", min_samples=20)
        btc_wr = _get_empirical_wr("BTC", 0.006, 4.0, "live", min_samples=20)
        assert eth_wr is not None and eth_wr > 0.9, f"ETH should be high WR: {eth_wr}"
        # BTC's bucket is all losses -> Wilson LB is far below breakeven, so _get_empirical_wr
        # correctly returns None (it only surfaces a WR statistically PROVEN above breakeven).
        # That ETH stays high while BTC is gated out also proves the buckets are isolated.
        assert btc_wr is None, f"BTC (all losses) must be gated to None, got: {btc_wr}"
    finally:
        bot_state._DB_FILE = orig
        os.unlink(db_path)
