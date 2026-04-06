"""
Binance Flexible ETH Staking integration.

Wraps the Binance ETH 2.0 staking sapi endpoints so DCA buys can be auto-staked
into BETH (Binance's 1:1 ETH wrapper) for ~2.5-3% APY with no lockup.

Design:
  - Fail-soft: staking failures never break the DCA pipeline. A failed stake
    just logs + sends a Telegram warning; the underlying ETH stays free and
    can be staked manually via the Binance Earn UI.
  - Read-only helpers (`get_beth_balance`, `get_staking_stats`) are safe to
    call from TUI/dashboard refresh loops — they only issue signed GETs.

Binance endpoints used:
  POST /sapi/v1/eth-staking/eth/stake   — wrap ETH → BETH
  GET  /sapi/v1/eth-staking/account     — cumulative profit, holding amount
  GET  /api/v3/account                  — read BETH balance from spot wallet
"""
from __future__ import annotations

from typing import Any, Optional

from config import (
    DCA_TARGET_ASSET,
    DCA_TARGET_PAIR,
    STAKING_ASSET,
    STAKING_ENABLED,
)
from trading.http_client import get, signed_get, signed_post
from trading.notify import send_telegram


# ── BETH balance ──────────────────────────────────────────────────────────────

def get_beth_balance() -> float:
    """Fetch current free BETH (staked ETH wrapper) balance from Binance spot."""
    try:
        acct = signed_get("/api/v3/account", {})
        for b in acct.get("balances", []):
            if b["asset"] == STAKING_ASSET:
                return float(b["free"]) + float(b.get("locked", 0))
    except Exception as e:
        print(f"  \u26a0 BETH balance fetch failed: {e}")
    return 0.0


# ── Stake execution ───────────────────────────────────────────────────────────

def stake_eth(qty: float) -> Optional[dict[str, Any]]:
    """Wrap `qty` ETH into BETH via Binance Flexible ETH Staking.

    Fail-soft: returns None on any failure after logging + Telegram warning.
    The DCA pipeline continues regardless — unstaked ETH stays in the spot
    wallet and can be staked manually.
    """
    if not STAKING_ENABLED:
        return None
    if qty <= 0:
        return None

    try:
        # Binance minimum: 0.0001 ETH per stake request
        if qty < 0.0001:
            msg = f"\u26a0 Stake skipped — qty {qty} < Binance minimum 0.0001 ETH"
            print(f"  {msg}")
            return None

        resp = signed_post("/sapi/v1/eth-staking/eth/stake", {
            "amount": f"{qty:.6f}",
        })
        print(f"  \u2713 Staked {qty:.6f} ETH → BETH (resp: {resp.get('success', resp)})")
        send_telegram(
            f"\U0001f331 *ETH staked*\n"
            f"Amount: `{qty:.6f} ETH` → BETH\n"
            f"Earning ~2.5-3% APY (flexible)"
        )
        return resp
    except Exception as e:
        msg = (
            f"\u26a0 Auto-stake failed for {qty:.6f} ETH: {e}\n"
            f"ETH remains free in spot wallet — stake manually via Binance Earn"
        )
        print(f"  {msg}")
        try:
            send_telegram(msg)
        except Exception:
            pass
        return None


# ── Staking statistics ────────────────────────────────────────────────────────

def get_staking_stats() -> dict[str, Any]:
    """Return staking position stats: beth_qty, eth_value, cumulative rewards.

    Uses `/sapi/v1/eth-staking/account` if available; falls back to spot
    balance + live price if the staking endpoint is unavailable.
    """
    stats: dict[str, Any] = {
        "beth_qty":         0.0,
        "eth_value":        0.0,
        "usdc_value":       0.0,
        "cumulative_reward": 0.0,
        "current_price":    0.0,
    }

    # 1) BETH balance from spot wallet (always available)
    stats["beth_qty"] = get_beth_balance()
    stats["eth_value"] = stats["beth_qty"]  # BETH ≈ ETH 1:1

    # 2) Current ETH price for USDC conversion
    try:
        ticker = get("/api/v3/ticker/price", {"symbol": DCA_TARGET_PAIR})
        stats["current_price"] = float(ticker["price"])
        stats["usdc_value"] = stats["beth_qty"] * stats["current_price"]
    except Exception:
        pass

    # 3) Cumulative reward from staking account endpoint (may fail — fail-soft)
    try:
        acct = signed_get("/sapi/v1/eth-staking/account", {})
        stats["cumulative_reward"] = float(acct.get("cumulativeProfitInBETH", 0))
    except Exception:
        pass  # endpoint unavailable or no staking position

    return stats


# ── Total ETH accumulation (free + staked) ───────────────────────────────────

def get_total_eth() -> dict[str, float]:
    """Return combined ETH position: free ETH + staked BETH.

    Used by the TUI accumulation widget to show progress toward DCA_TARGET_QTY.
    """
    free_eth = 0.0
    try:
        acct = signed_get("/api/v3/account", {})
        for b in acct.get("balances", []):
            if b["asset"] == DCA_TARGET_ASSET:
                free_eth = float(b["free"]) + float(b.get("locked", 0))
                break
    except Exception as e:
        print(f"  \u26a0 {DCA_TARGET_ASSET} balance fetch failed: {e}")

    staked_eth = get_beth_balance()
    return {
        "free_eth":   free_eth,
        "staked_eth": staked_eth,
        "total_eth":  free_eth + staked_eth,
    }
