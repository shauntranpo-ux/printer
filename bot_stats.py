"""Daily stats queries and formatting for Telegram + terminal output."""
import datetime
import logging
import sqlite3

log = logging.getLogger(__name__)

# Kept in sync with bot_strategies.STRATEGY_LABELS (tested); duplicated here so this
# module stays import-light for the standalone scripts.
_STRATEGY_LABELS = {
    "strategy1": "S1 · Momentum",
    "strategy2": "S2 · Favorite-Bias",
    "strategy3": "S3 · Structural Arb",
    "strategy4": "S4 · Mean-Reversion",
    "strategy5": "S5 · Maker Capture",
    "strategy6": "S6 · Window-Fade",
    "strategy7": "S7 · Vol-Spike",
    "strategy8": "S8 · Calm Favorite",
}
_STRATEGY_SHORT = {k: f"S{k[-1]}" for k in _STRATEGY_LABELS}

_ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE")

_SEP = "-" * 31


def query_stats(db_path: str, today_date: str | None = None,
                day_bounds: tuple[str, str] | None = None,
                mode: str | None = None) -> dict:
    """Query trade stats from DB. Returns zero-filled dict on DB error.

    day_bounds: optional (start_utc_iso, end_utc_iso) window for the "today"
    numbers - pass bot_infra.et_day_bounds_utc(...) so the day matches the ET
    trading day. Without it, falls back to the legacy UTC DATE(ts) bucket.
    mode: optional trades.mode filter. The daily summary passes the configured
    mode so its headline P&L matches the dashboard's per-mode tabs - unfiltered,
    stale demo/live rows blend into a total stamped with the current mode.
    """
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
            return _run_queries(conn, today, empty, day_bounds, mode)
        finally:
            conn.close()
    except Exception as e:
        log.warning("Stats DB query failed (non-fatal): %s", e)
        return empty


def _run_queries(conn: sqlite3.Connection, today: str, base: dict,
                 day_bounds: tuple[str, str] | None = None,
                 mode: str | None = None) -> dict:
    _mode_where = " AND mode = ?" if mode else ""
    _mode_params = (mode,) if mode else ()
    # Today breakdown by strategy + asset
    by_sa: dict = {}
    today_wins = 0
    today_losses = 0
    today_pnl = 0.0

    if day_bounds:
        _day_where, _day_params = "ts >= ? AND ts < ?", day_bounds
    else:
        _day_where, _day_params = "DATE(ts) = ?", (today,)
    rows = conn.execute(
        f"""
        SELECT strategy_variant, asset, outcome, COUNT(1) as n, SUM(pnl_dollars) as pnl
        FROM trades
        WHERE {_day_where} AND outcome IN ('win', 'loss'){_mode_where}
        GROUP BY strategy_variant, asset, outcome
        """,
        _day_params + _mode_params,
    ).fetchall()

    for row in rows:
        sv = row["strategy_variant"]
        if sv not in _STRATEGY_LABELS:
            log.warning("Unknown strategy_variant in DB: %r - row excluded from stats", sv)
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
        f"""
        SELECT COUNT(1) as n,
               SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
               SUM(pnl_dollars) as pnl
        FROM trades
        WHERE outcome IN ('win', 'loss'){_mode_where}
        """,
        _mode_params,
    ).fetchone()

    # Last trade timestamp
    last_row = conn.execute(
        "SELECT ts FROM trades WHERE outcome IN ('win','loss')"
        f"{_mode_where} ORDER BY ts DESC LIMIT 1",
        _mode_params,
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


def _last_trade_str(ts: str | None, tz=None) -> str:
    if ts is None:
        return "never"
    try:
        # Parse both Z and +00:00 suffixes
        ts_clean = ts.replace("Z", "+00:00")
        trade_dt = datetime.datetime.fromisoformat(ts_clean)
        if trade_dt.tzinfo is None:
            trade_dt = trade_dt.replace(tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        diff = now - trade_dt
        total_seconds = diff.total_seconds()
        if total_seconds < 0:
            rel = "just now"
        elif total_seconds < 3600:
            rel = f"{int(total_seconds // 60)} min ago"
        else:
            rel = f"{int(total_seconds // 3600)} hr ago"
        if tz is not None:
            _d = trade_dt.astimezone(tz)
            _h = _d.strftime("%I").lstrip("0") or "12"
            return (f"{_d.strftime('%b')} {_d.day}, {_h}:{_d.strftime('%M')} "
                    f"{_d.strftime('%p')} {_d.strftime('%Z')} ({rel})")
        return rel
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
                lines.append(f"  <code>{asset:<5} -</code>")
            else:
                lines.append(f"  {asset:<5} -")
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
        f"<b>Daily Summary - {date}</b>  (ET trading day)",
    ]
    if stats.get("as_of"):
        lines.append(f"Sent {stats['as_of']}")
    lines.append(_SEP)

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

        _totals: dict = {}
        for (_sv, _a), _row in by_sa.items():
            _t = _totals.setdefault(_sv, {"wins": 0, "losses": 0, "pnl": 0.0})
            _t["wins"] += _row["wins"]
            _t["losses"] += _row["losses"]
            _t["pnl"] += _row["pnl"]
        if _totals:
            lines.append(_SEP)
            lines.append("  |  ".join(
                f"{_STRATEGY_SHORT.get(k, k)}: {_fmt_pnl(v['pnl'])} ({v['wins']}W/{v['losses']}L)"
                for k, v in sorted(_totals.items())))
            if len(_totals) >= 2:
                _best = max(_totals.items(), key=lambda kv: kv[1]["pnl"])
                lines.append(f"Day winner: <b>{_STRATEGY_SHORT.get(_best[0], _best[0])}</b>")

    lines.append(_SEP)
    lines.append(f"Last trade: {_last_trade_str(stats['last_trade_ts'], stats.get('display_tz'))}")
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


def format_settle_message(outcome: str, pnl: float, asset: str, brain: str, side: str,
                          entry_c: float, exit_c: float, contracts: int,
                          when_str: str, today_pnl: float | None, mode: str,
                          strat_today: float | None = None) -> str:
    """One-trade settle alert. when_str comes from bot_infra.fmt_ts (display tz).

    today_pnl is the MODE total for the ET day (all strategies - matches the
    dashboard TODAY chip); strat_today is this strategy's own share, rendered
    alongside so the alert's numbers line up with both the dashboard and the
    per-strategy leaderboard instead of showing one ambiguous blended figure.
    """
    head = "WIN" if outcome == "win" else "LOSS"
    today = _fmt_pnl(today_pnl) if today_pnl is not None else "-"
    if strat_today is not None:
        today = f"{today} ({brain.upper()} {_fmt_pnl(strat_today)})"
    return (
        f"<b>{head} {_fmt_pnl(pnl)}</b>  {asset} * {brain.upper()} * {side.upper()} "
        f"{int(round(entry_c))}c -> {int(round(exit_c))}c x{int(contracts)}\n"
        f"Settled {when_str}  |  Today: {today}  |  {mode}"
    )


def format_entry_message(asset: str, brain: str, side: str, entry_c: float,
                         contracts: int, cost: float, ends_str: str, mode: str) -> str:
    """One-trade entry alert (opt-in via notify_on_entry)."""
    return (
        f"<b>ENTRY</b>  {asset} * {brain.upper()} * {side.upper()} @ "
        f"{int(round(entry_c))}c x{int(contracts)} (${cost:.2f})\n"
        f"Window ends {ends_str}  |  {mode}"
    )
