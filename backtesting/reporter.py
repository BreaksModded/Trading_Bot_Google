"""
Backtesting report generator.

Formats backtest results into structured reports with metrics,
exportable as JSON for the dashboard.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger


def generate_report(results: dict, output_path: Optional[Path] = None) -> dict:
    """
    Generate a structured report from backtest results.

    Args:
        results: Raw results from BacktestEngine.run().
        output_path: Optional path to save report as JSON.

    Returns:
        Formatted report dictionary.
    """
    metrics = results.get("metrics", {})
    config = results.get("config", {})
    trades = results.get("trades", [])
    equity = results.get("equity_curve", [])

    report = {
        "generated_at": datetime.utcnow().isoformat(),
        "summary": {
            "total_trades": metrics.get("total_trades", 0),
            "net_pnl_usdt": metrics.get("net_pnl", 0),
            "capital_return_pct": metrics.get("capital_return_pct", 0),
            "max_drawdown_pct": metrics.get("max_drawdown_pct", 0),
            "sharpe_ratio": metrics.get("sharpe_ratio", 0),
            "win_rate_pct": metrics.get("win_rate", 0),
            "profit_factor": metrics.get("profit_factor", 0),
        },
        "detailed_metrics": metrics,
        "config_used": config,
        "trade_count": len(trades),
        "equity_curve_points": len(equity),
        "verdict": _get_verdict(metrics),
    }

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        logger.info(f"Report saved to {output_path}")

    return report


def _get_verdict(metrics: dict) -> dict:
    """Evaluate backtest results against acceptance criteria."""
    net_pnl = metrics.get("net_pnl", 0)
    max_dd = metrics.get("max_drawdown_pct", 100)
    sharpe = metrics.get("sharpe_ratio", 0)

    checks = {
        "pnl_positive": {
            "passed": net_pnl > 0,
            "value": net_pnl,
            "threshold": "> 0",
            "description": "Net PnL is positive",
        },
        "drawdown_acceptable": {
            "passed": max_dd < 15,
            "value": max_dd,
            "threshold": "< 15%",
            "description": "Max drawdown below 15%",
        },
        "sharpe_acceptable": {
            "passed": sharpe > 1.0,
            "value": sharpe,
            "threshold": "> 1.0",
            "description": "Sharpe ratio above 1.0",
        },
    }

    all_passed = all(c["passed"] for c in checks.values())

    return {
        "overall": "PASS" if all_passed else "FAIL",
        "checks": checks,
    }


def format_report_text(report: dict) -> str:
    """Format report as human-readable text."""
    summary = report["summary"]
    verdict = report["verdict"]

    lines = [
        "=" * 50,
        "  BACKTESTING REPORT",
        "=" * 50,
        f"  Generated: {report['generated_at']}",
        "",
        "  RESULTS:",
        f"  Total Trades:    {summary['total_trades']}",
        f"  Net PnL:         {summary['net_pnl_usdt']:+.4f} USDT",
        f"  Return:          {summary['capital_return_pct']:+.2f}%",
        f"  Max Drawdown:    {summary['max_drawdown_pct']:.2f}%",
        f"  Sharpe Ratio:    {summary['sharpe_ratio']:.4f}",
        f"  Win Rate:        {summary['win_rate_pct']:.1f}%",
        f"  Profit Factor:   {summary['profit_factor']:.4f}",
        "",
        "  VERDICT:",
    ]

    for name, check in verdict["checks"].items():
        status = "✅" if check["passed"] else "❌"
        lines.append(f"  {status} {check['description']}: {check['value']} ({check['threshold']})")

    overall = "✅ PASS" if verdict["overall"] == "PASS" else "❌ FAIL"
    lines.extend(["", f"  Overall: {overall}", "=" * 50])

    return "\n".join(lines)
