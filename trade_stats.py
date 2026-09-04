"""
trade_stats.py — one definition of how a rule's record is summarised.

The intraday and swing backtests both produce a record, and strategy_edge
reads both to decide how much weight a firing rule gets in the debate. That
only works if the two mean the same thing by "expectancy" and by "enough
trades", so the computation lives here once rather than being written twice
and drifting apart.

Win rate and expectancy are always returned together. Win rate alone is
trivially gameable — a tiny target and a distant stop manufactures winners
that lose money — so anything reading one should have the other to hand.
"""

from __future__ import annotations


def summarise(trades, min_trades, noun="setups", extra=None):
    """
    A rule's record over a list of trades, each carrying an `r` multiple.

    `min_trades` is the point below which a hit rate is noise rather than
    evidence; under it the record is returned with `enough` False and a note
    saying so, rather than a flattering percentage. `extra` adds fields the
    caller cares about without forking the whole function.
    """
    n = len(trades)
    if not n:
        return {"trades": 0, "enough": False,
                "note": f"no {noun} triggered in the sample"}

    rs = [t["r"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    gross_win, gross_loss = sum(wins), abs(sum(losses))
    enough = n >= min_trades

    out = {
        "trades": n,
        "enough": enough,
        "win_rate_pct": round(len(wins) / n * 100.0, 1),
        "expectancy_r": round(sum(rs) / n, 3),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "avg_win_r": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss_r": round(sum(losses) / len(losses), 2) if losses else None,
        "best_r": round(max(rs), 2),
        "worst_r": round(min(rs), 2),
        "note": None if enough else
                f"only {n} {noun} — below the {min_trades} needed before a "
                f"hit rate means anything",
    }
    if extra:
        out.update(extra(trades))
    return out
