"""Daily stats queries and formatting for Telegram + terminal output."""
import datetime
import logging
import sqlite3

log = logging.getLogger(__name__)

_STRATEGY_LABELS = {
    "strategy1": "S1 · EMA Momentum",
    "strategy2": "S2 · Contract Velocity",
}

_ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE")

_SEP = "-" * 31


def query_stats(db_path: str, today_date: str | None = None) -> dict:
    """Query trade stats from DB. Returns zero-filled dict on DB error."""
    today = today_date or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    empty = {
        "date": today,
        "today_trades": 0,
        "today_wins": 0,
        "today_losses": 0,
        "today_pnl": 0.0,
        "alltime_trades": 0,
        "alltime_wins": 0,
        "alltime_pnl": 0.0,
        "by_strategy_asset": {},
        "last_trade_ts": None,
        "consecutive_losses": 0,
        "mode": "PAPER",
    }
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            return _run_queries(conn, today, empty)
        finally:
            conn.close()
    except Exception as e:
        log.warning("Stats DB query failed (non-fatal): %s", e)
        return empty


def _run_queries(conn: sqlite3.Connection, today: str, base: dict) -> dict:
    # Today breakdown by strategy + asset
    by_sa: dict = {}
    today_wins = 0
    today_losses = 0
    today_pnl = 0.0

    rows = conn.execute(
        """
        SELECT strategy_variant, asset, outcome, COUNT(1) as n, SUM(pnl_dollars) as pnl
        FROM trades
        WHERE DATE(ts) = ? AND outcome IN ('win', 'loss')
        GROUP BY strategy_variant, asset, outcome
        """,
        (today,),
    ).fetchall()

    for row in rows:
        sv = row["strategy_variant"]
        if sv not in _STRATEGY_LABELS:
            log.warning("Unknown strategy_variant in DB: %r — row excluded from stats", sv)
            continue
        key = (sv, row["asset"])
        if key not in by_sa:
            by_sa[key] = {"wins": 0, "losses": 0, "pnl": 0.0}
        if row["outcome"] == "win":
            by_sa[key]["wins"] += row["n"]
            today_wins += row["n"]
        else:
            by_sa[key]["losses"] += row["n"]
            today_losses += row["n"]
        by_sa[key]["pnl"] += row["pnl"] or 0.0
        today_pnl += row["pnl"] or 0.0

    # All-time totals
    alltime = conn.execute(
        """
        SELECT COUNT(1) as n,
               SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
               SUM(pnl_dollars) as pnl
        FROM trades
        WHERE outcome IN ('win', 'loss')
        """,
    ).fetchone()

    # Last trade timestamp
    last_row = conn.execute(
        "SELECT ts FROM trades WHERE outcome IN ('win','loss') ORDER BY ts DESC LIMIT 1"
    ).fetchone()

    return {
        "date": today,
        "today_trades": today_wins + today_losses,
        "today_wins": today_wins,
        "today_losses": today_losses,
        "today_pnl": today_pnl,
        "alltime_trades": alltime["n"] or 0,
        "alltime_wins": alltime["wins"] or 0,
        "alltime_pnl": alltime["pnl"] or 0.0,
        "by_strategy_asset": by_sa,
        "last_trade_ts": last_row["ts"] if last_row else None,
        "consecutive_losses": base["consecutive_losses"],
        "mode": base["mode"],
    }


def _last_trade_str(ts: str | None) -> str:
    if ts is None:
        return "never"
    try:
        # Parse both Z and +00:00 suffixes
        ts_clean = ts.replace("Z", "+00:00")
        trade_dt = datetime.datetime.fromisoformat(ts_clean)
        now = datetime.datetime.now(datetime.timezone.utc)
        diff = now - trade_dt
        total_seconds = diff.total_seconds()
        if total_seconds < 0:
            return "just now"
        mins = int(total_seconds // 60)
        if mins < 60:
            return f"{mins} min ago"
        hours = int(total_seconds // 3600)
        return f"{hours} hr ago"
    except Exception:
        return ts


def _strategy_rows(by_sa: dict, strategy_key: str, html: bool) -> list[str]:
    """Build per-asset rows for one strategy, showing all assets."""
    lines = []
    for asset in _ASSETS:
        counts = by_sa.get((strategy_key, asset))
        has_trades = counts is not None and (counts["wins"] > 0 or counts["losses"] > 0)
        if has_trades:
            pnl = counts["pnl"]
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            wl = f"{counts['wins']}W/{counts['losses']}L"
            if html:
                lines.append(f"  <code>{asset:<5} {wl:<7}  {pnl_str}</code>")
            else:
                lines.append(f"  {asset:<5} {wl:<7}  {pnl_str}")
        else:
            if html:
                lines.append(f"  <code>{asset:<5} —</code>")
            else:
                lines.append(f"  {asset:<5} —")
    return lines


def _strategy_has_trades(by_sa: dict, strategy_key: str) -> bool:
    return any(
        sv == strategy_key and (counts["wins"] > 0 or counts["losses"] > 0)
        for (sv, _asset), counts in by_sa.items()
    )


def format_telegram(stats: dict) -> str:
    """Return HTML-formatted daily summary for Telegram."""
    date = stats["date"]
    trades = stats["today_trades"]
    wins = stats["today_wins"]
    pnl_today = stats["today_pnl"]
    pnl_all = stats["alltime_pnl"]
    alltime = stats["alltime_trades"]
    alltime_wins = stats["alltime_wins"]
    mode = stats["mode"]
    consec = stats["consecutive_losses"]
    by_sa = stats["by_strategy_asset"]

    lines = [
        f"<b>Daily Summary - {date}</b>",
        _SEP,
    ]

    if trades == 0:
        wr_str = "N/A"
        lines.append("Trades: 0  |  WR: N/A")
        lines.append(f"P&amp;L today: $0.00  |  All-time: {_fmt_pnl(pnl_all)}")
        lines.append(_SEP)
        lines.append("No trades today")
    else:
        wr_pct = 100.0 * wins / trades
        lines.append(f"Trades: {trades}  |  WR: {wins}/{trades} ({wr_pct:.1f}%)")
        lines.append(f"P&amp;L today: {_fmt_pnl(pnl_today)}  |  All-time: {_fmt_pnl(pnl_all)}")
        lines.append(_SEP)

        for sv_key, label in _STRATEGY_LABELS.items():
            if _strategy_has_trades(by_sa, sv_key):
                lines.append(f"<b>{label}</b>")
                lines.extend(_strategy_rows(by_sa, sv_key, html=True))

    lines.append(_SEP)
    lines.append(f"Last trade: {_last_trade_str(stats['last_trade_ts'])}")
    lines.append(f"Consecutive losses: {consec}")
    lines.append(f"Mode: {mode}")

    return "\n".join(lines)


def format_terminal(stats: dict) -> str:
    """Return plain-text daily summary for terminal output."""
    date = stats["date"]
    trades = stats["today_trades"]
    wins = stats["today_wins"]
    pnl_today = stats["today_pnl"]
    pnl_all = stats["alltime_pnl"]
    mode = stats["mode"]
    consec = stats["consecutive_losses"]
    by_sa = stats["by_strategy_asset"]

    lines = [
        f"Daily Summary - {date}",
        _SEP,
    ]

    if trades == 0:
        lines.append("Trades: 0  |  WR: N/A")
        lines.append(f"P&L today: $0.00  |  All-time: {_fmt_pnl(pnl_all)}")
        lines.append(_SEP)
        lines.append("No trades today")
    else:
        wr_pct = 100.0 * wins / trades
        lines.append(f"Trades: {trades}  |  WR: {wins}/{trades} ({wr_pct:.1f}%)")
        lines.append(f"P&L today: {_fmt_pnl(pnl_today)}  |  All-time: {_fmt_pnl(pnl_all)}")
        lines.append(_SEP)

        for sv_key, label in _STRATEGY_LABELS.items():
            if _strategy_has_trades(by_sa, sv_key):
                lines.append(label)
                lines.extend(_strategy_rows(by_sa, sv_key, html=False))

    lines.append(_SEP)
    lines.append(f"Last trade: {_last_trade_str(stats['last_trade_ts'])}")
    lines.append(f"Consecutive losses: {consec}")
    lines.append(f"Mode: {mode}")

    return "\n".join(lines)


def _fmt_pnl(pnl: float) -> str:
    if pnl >= 0:
        return f"+${pnl:.2f}"
    return f"-${abs(pnl):.2f}"
