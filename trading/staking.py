"""
Binance ETH Staking integration.

Wraps the Binance ETH 2.0 staking sapi endpoints so DCA buys can be auto-staked.
As of Binance's 2023 migration, the staking product distributes WBETH
(an accrual token — 1 WBETH > 1 ETH, see exchange_rate) rather than the
legacy BETH wrapper.

Design:
  - Fail-soft: staking failures never break the DCA pipeline. A failed stake
    just logs + sends a Telegram warning; the underlying ETH stays free and
    can be staked manually via the Binance Earn UI.
  - Read-only helpers (`get_beth_balance`, `get_staked_eth`, `get_staking_stats`)
    are safe to call from TUI/dashboard refresh loops — they only issue signed
    GETs and are fully fail-soft on API error.
  - Resolves staked balance across ALL possible locations: `/sapi/v2/eth-staking/
    account.holdingInETH`, spot BETH (legacy), spot WBETH, spot LDWBETH (Simple
    Earn locked WBETH), and spot LDBETH (Simple Earn legacy BETH).

Binance endpoints used:
  POST /sapi/v2/eth-staking/eth/stake              — wrap ETH → WBETH
  GET  /sapi/v2/eth-staking/account                — holdingInETH + profit
  GET  /sapi/v1/eth-staking/eth/history/rateHistory — live WBETH:ETH rate
  GET  /api/v3/account                             — spot + LD-prefix balances
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from config import (
    DCA_TARGET_ASSET,
    DCA_TARGET_PAIR,
    STAKING_ENABLED,
)
from trading.db import (
    db_connect,
    db_init,
    get_staked_eth_cache,
    get_wbeth_rate_cache,
    set_staked_eth_cache,
    set_wbeth_rate_cache,
)
from trading.http_client import get, signed_get, signed_post
from trading.notify import send_telegram

# Cache WBETH:ETH rate for 1h — it only moves via daily rebase.
_WBETH_RATE_CACHE_H = 1.0

# Cache full get_staked_eth() result for 120s. The TUI's 30s scan worker
# calls get_staked_eth(force_refresh=True) to warm the cache; the 5s refresh
# loop and the webapp read cached values without hitting Binance.
_STAKED_ETH_CACHE_S = 120.0

# Fallback used when both the staking endpoint AND any cached value are
# unavailable. Documented as KNOWN undervaluation (~10% today) — only kicks
# in under double-failure. Prefer stale cache over fallback whenever possible.
_WBETH_RATE_FALLBACK = 1.0


# ── Exchange rate (WBETH → ETH) ───────────────────────────────────────────────

def get_wbeth_exchange_rate() -> float:
    """Return the live WBETH:ETH exchange rate. Cached 1h in state.db.

    The rate is the number of ETH redeemable per 1 WBETH. It only moves UP
    (via daily rebase) — that's how Binance delivers ~2.5% APY staking yield.

    Fallback cascade:
      1. Cached value < 1h old    → use cache, no API call
      2. Fresh fetch from Binance → update cache, return fresh value
      3. Stale cache (any age)    → return stale, log warning
      4. 1.0 fallback             → log error, accept ~10% undervaluation
    """
    try:
        conn = db_connect()
        db_init(conn)
    except Exception as e:
        print(f"  \u26a0 WBETH rate cache unavailable: {e}")
        conn = None

    try:
        cached = None
        if conn is not None:
            try:
                cached = get_wbeth_rate_cache(conn)
            except Exception:
                cached = None

        if cached:
            try:
                age_h = (
                    datetime.now() - datetime.fromisoformat(cached["ts"])
                ).total_seconds() / 3600
                if age_h < _WBETH_RATE_CACHE_H:
                    return float(cached["exchange_rate"])
            except (ValueError, TypeError, KeyError):
                cached = None  # corrupt cache row, refetch

        # Fetch fresh
        try:
            resp = signed_get(
                "/sapi/v1/eth-staking/eth/history/rateHistory",
                {"size": 1},
            )
            rows = resp.get("rows", []) if isinstance(resp, dict) else []
            if rows and isinstance(rows[0], dict) and "exchangeRate" in rows[0]:
                rate = float(rows[0]["exchangeRate"])
                apr_raw = rows[0].get("annualPercentageRate")
                apr = float(apr_raw) if apr_raw is not None else None
                if conn is not None:
                    try:
                        set_wbeth_rate_cache(conn, rate, apr)
                    except Exception:
                        pass
                return rate
        except Exception as e:
            print(f"  \u26a0 WBETH rate fetch failed: {e}")

        if cached:
            print("  \u21a9 Using stale WBETH rate (refresh failed)")
            try:
                return float(cached["exchange_rate"])
            except (ValueError, TypeError, KeyError):
                pass

        print("  \u26a0 No WBETH rate available — falling back to 1:1")
        return _WBETH_RATE_FALLBACK
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ── Staked ETH balance — resolves all possible locations ────────────────────

def get_staked_eth(force_refresh: bool = False) -> dict[str, float]:
    """Return staked ETH position resolved across all Binance balance locations.

    Single source of truth for "how much ETH do I have staked right now".
    Used by get_beth_balance(), get_total_eth(), and get_staking_stats().

    Caching: the full result is cached for 120s in `staked_eth_cache` so that
    refresh loops (TUI 5s, webapp) can read it without hitting Binance API
    on every tick. The TUI's 30s scan worker calls with force_refresh=True
    to re-populate the cache on each real scan boundary.

    Args:
      force_refresh: Bypass the cache and always fetch live. Used by the
        30s scan worker to warm the cache. Defaults to False (cache-first).

    Resolution cascade (checks ALL, sums what it finds):
      1. /sapi/v2/eth-staking/account.holdingInETH  — authoritative, includes yield
      2. Spot BETH    — legacy wrapper, 1:1 with ETH
      3. Spot WBETH   — free WBETH × exchange_rate
      4. Spot LDWBETH — Simple Earn locked WBETH × exchange_rate
      5. Spot LDBETH  — Simple Earn locked legacy BETH, 1:1 with ETH

    Returns a dict with per-location breakdown and a total, so callers can
    distinguish "real staking" from pre-existing Simple Earn products.
    All error paths return zero-filled values (fail-soft).
    """
    # ── Cache read (unless force_refresh) ─────────────────────────────────
    if not force_refresh:
        try:
            cache_conn = db_connect()
            db_init(cache_conn)
            cached = get_staked_eth_cache(cache_conn)
            cache_conn.close()
            if cached:
                try:
                    age_s = (
                        datetime.now() - datetime.fromisoformat(cached["ts"])
                    ).total_seconds()
                    if age_s < _STAKED_ETH_CACHE_S:
                        return {k: v for k, v in cached.items() if k != "ts"}
                except (ValueError, TypeError, KeyError):
                    pass  # corrupt cache row, fall through to fresh fetch
        except Exception as e:
            print(f"  \u26a0 staked_eth cache read failed: {e}")

    # ── Fresh fetch ────────────────────────────────────────────────────────
    result = _fetch_staked_eth_live()

    # ── Cache write (fail-soft) ────────────────────────────────────────────
    try:
        cache_conn = db_connect()
        db_init(cache_conn)
        set_staked_eth_cache(cache_conn, result)
        cache_conn.close()
    except Exception as e:
        print(f"  \u26a0 staked_eth cache write failed: {e}")

    return result


def _fetch_staked_eth_live() -> dict[str, float]:
    """Always-fresh version of get_staked_eth — hits Binance API directly.

    Separated from the public `get_staked_eth()` so tests can mock this
    function to bypass the cache layer entirely, and so the public function
    can focus on cache-management logic.
    """
    result: dict[str, float] = {
        "holdingInETH":  0.0,
        "spot_beth":     0.0,
        "spot_wbeth":    0.0,
        "spot_ldwbeth":  0.0,
        "spot_ldbeth":   0.0,
        "exchange_rate": 1.0,
        "total_eth":     0.0,
    }

    # 1) Authoritative staking account (includes accrued yield in ETH units)
    try:
        acct = signed_get("/sapi/v2/eth-staking/account", {})
        if isinstance(acct, dict):
            result["holdingInETH"] = float(acct.get("holdingInETH", 0) or 0)
    except Exception as e:
        print(f"  \u26a0 eth-staking/account fetch failed: {e}")

    # 2-5) Spot wallet scan for all possible BETH/WBETH asset names
    try:
        spot = signed_get("/api/v3/account", {})
        balances = spot.get("balances", []) if isinstance(spot, dict) else []
        for b in balances:
            asset = b.get("asset", "")
            try:
                total = float(b.get("free", 0) or 0) + float(b.get("locked", 0) or 0)
            except (ValueError, TypeError):
                continue
            if total <= 0:
                continue
            if asset == "BETH":
                result["spot_beth"] = total
            elif asset == "WBETH":
                result["spot_wbeth"] = total
            elif asset == "LDWBETH":
                result["spot_ldwbeth"] = total
            elif asset == "LDBETH":
                result["spot_ldbeth"] = total
    except Exception as e:
        print(f"  \u26a0 spot balance fetch failed: {e}")

    # Apply exchange rate to WBETH-denominated amounts
    rate = get_wbeth_exchange_rate()
    result["exchange_rate"] = rate

    # If the staking endpoint gave us holdingInETH, prefer it as the canonical
    # reading — it already folds in LDWBETH (same staking product). Only add
    # balances that are DEFINITELY separate from the staking account product.
    if result["holdingInETH"] > 0:
        result["total_eth"] = (
            result["holdingInETH"]
            + result["spot_beth"]           # legacy, not in staking account
            + result["spot_wbeth"] * rate   # free WBETH, separate from LD wallet
            + result["spot_ldbeth"]         # legacy Simple Earn, separate product
        )
    else:
        # Staking account unavailable — fall back to summing spot reads
        result["total_eth"] = (
            result["spot_beth"]
            + result["spot_wbeth"] * rate
            + result["spot_ldwbeth"] * rate
            + result["spot_ldbeth"]
        )

    return result


# ── Back-compat: get_beth_balance() now returns total staked ETH equivalent ──

def get_beth_balance() -> float:
    """Legacy name — returns total staked ETH equivalent across all locations.

    Historically this returned only spot `BETH.free + BETH.locked`. After the
    2023 Binance migration to WBETH, that reading was always 0 for new users.
    This function now delegates to `get_staked_eth()` which resolves the full
    picture (staking account, BETH, WBETH, LDWBETH, LDBETH).
    """
    try:
        return get_staked_eth()["total_eth"]
    except Exception as e:
        print(f"  \u26a0 staked ETH lookup failed: {e}")
        return 0.0


# ── Free ETH in Simple Earn (pre-existing, separate from DCA staking) ───────

def get_free_ldeth() -> float:
    """Return ETH held in Simple Earn as LDETH (flexible earn, not staking).

    This is distinct from WBETH staking — it represents pre-existing ETH the
    user parked in a flexible-earn product before (or outside of) the DCA
    pipeline. Folded into portfolio total by `get_portfolio()` but NOT counted
    as DCA-staking progress.
    """
    try:
        spot = signed_get("/api/v3/account", {})
        balances = spot.get("balances", []) if isinstance(spot, dict) else []
        for b in balances:
            if b.get("asset") == "LDETH":
                try:
                    return float(b.get("free", 0) or 0) + float(b.get("locked", 0) or 0)
                except (ValueError, TypeError):
                    return 0.0
    except Exception as e:
        print(f"  \u26a0 LDETH balance fetch failed: {e}")
    return 0.0


# ── Stake execution (unchanged — the bug was downstream in balance reading) ─

def stake_eth(qty: float) -> Optional[dict[str, Any]]:
    """Wrap `qty` ETH into WBETH via Binance ETH Staking.

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

        # Binance enforces 4-decimal precision on the `amount` parameter.
        # Truncate (don't round) so we never attempt to stake more than we hold.
        import math as _math
        qty_4dp = _math.floor(qty * 10000) / 10000
        if qty_4dp < 0.0001:
            msg = f"\u26a0 Stake skipped — qty {qty} truncates to {qty_4dp} < 0.0001"
            print(f"  {msg}")
            return None
        resp = signed_post("/sapi/v2/eth-staking/eth/stake", {
            "amount": f"{qty_4dp:.4f}",
        })
        print(f"  \u2713 Staked {qty:.6f} ETH → WBETH (resp: {resp.get('success', resp)})")
        send_telegram(
            f"\U0001f331 *ETH staked*\n"
            f"Amount: `{qty:.6f} ETH` → WBETH\n"
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
    """Return staking position stats: qty (ETH-equivalent), value, rewards.

    Uses `get_staked_eth()` for the authoritative balance and the staking
    account endpoint for cumulative yield. Fail-soft: zero-filled on any error.
    """
    stats: dict[str, Any] = {
        "beth_qty":          0.0,  # kept for back-compat; now "staked ETH equiv"
        "eth_value":         0.0,
        "usdc_value":        0.0,
        "cumulative_reward": 0.0,
        "current_price":     0.0,
        "exchange_rate":     1.0,
    }

    # 1) Total staked ETH equivalent across all locations
    try:
        staked = get_staked_eth()
        stats["beth_qty"] = staked["total_eth"]
        stats["eth_value"] = staked["total_eth"]
        stats["exchange_rate"] = staked["exchange_rate"]
    except Exception as e:
        print(f"  \u26a0 staked ETH lookup failed: {e}")

    # 2) Current ETH price for USDC conversion
    try:
        ticker = get("/api/v3/ticker/price", {"symbol": DCA_TARGET_PAIR})
        stats["current_price"] = float(ticker["price"])
        stats["usdc_value"] = stats["beth_qty"] * stats["current_price"]
    except Exception:
        pass

    # 3) Cumulative reward from staking account endpoint
    try:
        acct = signed_get("/sapi/v2/eth-staking/account", {})
        # New endpoint uses `thirtyDaysProfitInETH` — the old
        # `cumulativeProfitInBETH` field no longer exists.
        if isinstance(acct, dict):
            profit = acct.get("thirtyDaysProfitInETH")
            if profit is None:
                profit = acct.get("cumulativeProfitInBETH", 0)
            stats["cumulative_reward"] = float(profit or 0)
    except Exception:
        pass

    return stats


# ── Total ETH accumulation (free + staked + Simple Earn) ─────────────────────

def get_total_eth() -> dict[str, float]:
    """Return combined ETH position: free ETH + staked (WBETH/BETH) + LDETH.

    Used by the TUI accumulation widget to show progress toward DCA_TARGET_QTY
    and by `get_portfolio()` to count staked value in portfolio total.
    """
    free_eth = 0.0
    try:
        acct = signed_get("/api/v3/account", {})
        balances = acct.get("balances", []) if isinstance(acct, dict) else []
        for b in balances:
            if b.get("asset") == DCA_TARGET_ASSET:
                free_eth = float(b.get("free", 0) or 0) + float(b.get("locked", 0) or 0)
                break
    except Exception as e:
        print(f"  \u26a0 {DCA_TARGET_ASSET} balance fetch failed: {e}")

    try:
        staked_eth = get_staked_eth()["total_eth"]
    except Exception:
        staked_eth = 0.0

    try:
        ldeth = get_free_ldeth()
    except Exception:
        ldeth = 0.0

    return {
        "free_eth":   free_eth,
        "staked_eth": staked_eth,
        "ldeth":      ldeth,
        "total_eth":  free_eth + staked_eth + ldeth,
    }
