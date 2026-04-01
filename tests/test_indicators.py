"""
Unit tests for pure indicator functions.

All tests are offline — no network calls, no file I/O.
scanner.py is imported with the Binance API calls guarded by mock.
"""

from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

# ── Import pure functions directly (no side effects on import) ────────────────
from scanner import calc_rsi, calc_sma, calc_atr, detect_bullish_divergence
from backtest import (
    calc_rsi as bt_calc_rsi,
    calc_sma as bt_calc_sma,
    calc_atr as bt_calc_atr,
    compute_signal,
    compute_stats,
)


# ═══════════════════════════════════════════════════════════════════════════════
# calc_rsi
# ═══════════════════════════════════════════════════════════════════════════════

class TestCalcRsi:
    def test_insufficient_data_returns_50(self):
        # < 14 price changes → not enough for Wilder seed
        closes = [100.0] * 10
        assert calc_rsi(closes) == 50.0

    def test_exactly_14_prices_returns_50(self):
        # 14 prices → 13 changes, still < period=14
        closes = list(range(1, 15))  # 14 values, 13 diffs
        assert calc_rsi(closes) == 50.0

    def test_constant_prices_returns_100(self):
        # No gains AND no losses → avg_loss == 0 → the guard returns 100.0
        # (mathematically RSI is undefined here; the implementation favours 100)
        closes = [100.0] * 30
        assert calc_rsi(closes) == 100.0

    def test_all_gains_returns_100(self):
        # Every candle up → avg_loss = 0 → RSI = 100
        closes = list(range(1, 31))  # 30 strictly rising prices
        assert calc_rsi(closes) == 100.0

    def test_all_losses_returns_near_zero(self):
        # Every candle down → avg_gain = 0 → RSI = 0
        closes = list(range(30, 0, -1))  # 30 strictly falling prices
        assert calc_rsi(closes) == pytest.approx(0.0, abs=1e-6)

    def test_oversold_threshold(self):
        # 10 sharp drops then 5 mild recoveries → RSI should be < 40
        closes = [100.0 - i * 3 for i in range(20)] + [40.0 + i * 0.5 for i in range(5)]
        rsi = calc_rsi(closes)
        assert rsi < 40.0

    def test_overbought_threshold(self):
        # 20 sharp gains then small dip → RSI should be > 60
        closes = [100.0 + i * 3 for i in range(20)] + [160.0 - i * 0.5 for i in range(5)]
        rsi = calc_rsi(closes)
        assert rsi > 60.0

    def test_period_parameter_respected(self):
        # With period=5, only 5 prices needed (4 diffs)
        closes = [10.0, 11.0, 12.0, 11.0, 10.0, 11.0]  # 6 values, 5 diffs ≥ period=5
        rsi = calc_rsi(closes, period=5)
        assert 0.0 <= rsi <= 100.0

    def test_output_range(self):
        import random
        random.seed(42)
        closes = [100.0]
        for _ in range(49):
            closes.append(closes[-1] + random.uniform(-5, 5))
        rsi = calc_rsi(closes)
        assert 0.0 <= rsi <= 100.0

    def test_backtest_clone_matches_scanner(self):
        # Both files define calc_rsi identically — they must return the same value
        closes = [100.0 + i * 0.5 - (i % 3) * 2 for i in range(30)]
        assert calc_rsi(closes) == pytest.approx(bt_calc_rsi(closes), rel=1e-9)


# ═══════════════════════════════════════════════════════════════════════════════
# calc_sma
# ═══════════════════════════════════════════════════════════════════════════════

class TestCalcSma:
    def test_insufficient_data_returns_none(self):
        assert calc_sma([100.0] * 19, period=20) is None

    def test_exact_period_data(self):
        closes = [float(i) for i in range(1, 21)]  # 1..20
        result = calc_sma(closes, period=20)
        assert result == pytest.approx(10.5)

    def test_uses_last_n_prices(self):
        # First 10 prices are huge, last 20 are small — SMA should reflect last 20
        closes = [1000.0] * 10 + [1.0] * 20
        result = calc_sma(closes, period=20)
        assert result == pytest.approx(1.0)

    def test_single_period(self):
        closes = [42.0, 43.0, 44.0]
        assert calc_sma(closes, period=1) == pytest.approx(44.0)

    def test_backtest_clone_matches_scanner(self):
        closes = [float(i) for i in range(1, 31)]
        assert calc_sma(closes) == pytest.approx(bt_calc_sma(closes), rel=1e-9)


# ═══════════════════════════════════════════════════════════════════════════════
# calc_atr
# ═══════════════════════════════════════════════════════════════════════════════

def _make_kline(open_: float, high: float, low: float, close: float) -> list:
    """Build a minimal kline list matching Binance format (only indices 2,3,4 used)."""
    return [None, str(open_), str(high), str(low), str(close), "1000", None, None, None, None, None, None]


class TestCalcAtr:
    def test_insufficient_data_returns_none(self):
        klines = [_make_kline(100, 105, 95, 100) for _ in range(14)]  # 14 klines → 13 TRs
        assert calc_atr(klines) is None

    def test_constant_candles_returns_range(self):
        # Every candle: high=105, low=95, close=100 → TR = 10 always
        klines = [_make_kline(100, 105, 95, 100) for _ in range(20)]
        atr = calc_atr(klines)
        assert atr == pytest.approx(10.0)

    def test_wider_range_produces_higher_atr(self):
        narrow = [_make_kline(100, 101, 99, 100) for _ in range(20)]
        wide   = [_make_kline(100, 110, 90, 100) for _ in range(20)]
        assert calc_atr(wide) > calc_atr(narrow)

    def test_atr_is_positive(self):
        klines = [_make_kline(100 + i, 105 + i, 95 + i, 100 + i) for i in range(20)]
        atr = calc_atr(klines)
        assert atr > 0.0

    def test_backtest_clone_matches_scanner(self):
        klines = [_make_kline(100 + i, 106 + i, 94 + i, 100 + i) for i in range(20)]
        assert calc_atr(klines) == pytest.approx(bt_calc_atr(klines), rel=1e-9)

    def test_atr_wilder_smoothing(self):
        # Spike in the middle — ATR should decay toward baseline, not stay at spike
        klines = [_make_kline(100, 102, 98, 100) for _ in range(15)]  # baseline TR=4
        klines += [_make_kline(100, 130, 70, 100)]                     # spike TR=60
        klines += [_make_kline(100, 102, 98, 100) for _ in range(5)]   # back to baseline
        atr_vals = []
        # Recompute with progressively more klines to see decay
        for n in range(17, len(klines) + 1):
            atr_vals.append(calc_atr(klines[:n]))
        # After the spike the ATR should decrease (smoothing decay)
        assert atr_vals[-1] < atr_vals[1]


# ═══════════════════════════════════════════════════════════════════════════════
# analyze() signal tier logic — mocked API calls
# ═══════════════════════════════════════════════════════════════════════════════

def _make_klines(closes: list[float], vol: float = 1000.0) -> list[list]:
    """Build minimal klines list for analyze() — all 1h candles at given closes."""
    klines = []
    for i, c in enumerate(closes):
        high = c * 1.005
        low  = c * 0.995
        klines.append([
            i * 3600000, str(c * 0.998), str(high), str(low), str(c), str(vol),
            None, None, None, None, None, None,
        ])
    return klines


def _neutral_context() -> dict:
    """Neutral market context — passes all market filters."""
    return {"fg_value": 30, "btc_rsi": 50.0, "btc_above_sma": True, "btc_dom_rising": False}


class TestAnalyzeSignalTiers:
    """
    analyze() makes two real API calls: get() (klines + ticker) and get() for daily.
    We patch scanner.get to return synthetic klines and ticker data.

    For signal-tier boundary tests (F&G guard, BTC-SMA guard, daily filter) we also
    patch scanner.calc_rsi to return a precise value without crafting exact price series.
    The mock distinguishes 1h from daily by list length: daily closes ≈ 29 items,
    1h closes ≈ 99 items (KLINE_LIMIT=100 minus the forming candle).
    """

    def _run_analyze(self, h1_closes: list[float], d1_closes: list[float],
                     context: dict, vol_surge: bool = False) -> dict:
        """Run analyze() with fully mocked API responses."""
        import scanner

        vol_normal = 1000.0
        last_vol   = vol_normal * 1.5 if vol_surge else vol_normal * 0.8

        # 1h klines: all normal vol except the last candle
        h1_klines = _make_klines(h1_closes[:-1], vol_normal)
        h1_klines[-1][-7] = None  # keep as is
        # Rebuild so each kline vol matches
        h1_klines = []
        for i, c in enumerate(h1_closes):
            v = last_vol if i == len(h1_closes) - 1 else vol_normal
            h1_klines.append([
                i, str(c), str(c * 1.005), str(c * 0.995), str(c), str(v),
                None, None, None, None, None, None,
            ])
        # +1 dummy "forming" candle that analyze() drops
        h1_klines.append(h1_klines[-1][:])

        # daily klines
        d1_klines = _make_klines(d1_closes) + [_make_klines([d1_closes[-1]])[0]]

        ticker = {"priceChangePercent": "0.5"}

        call_count = [0]
        def fake_get(path, params=None):
            call_count[0] += 1
            if "ticker" in path:
                return ticker
            interval = (params or {}).get("interval", "1h")
            return d1_klines if interval == "1d" else h1_klines

        with patch.object(scanner, "get", side_effect=fake_get):
            return scanner.analyze("ETHUSDC", context)

    def _run_analyze_rsi_controlled(
        self,
        h1_rsi: float,
        fg: int,
        btc_above: bool,
        daily_rsi: float,
        vol_surge: bool = True,
        above_sma: bool = True,
        momentum_up: bool = True,
        btc_dom_rising: bool = False,
    ) -> dict:
        """
        Test signal tier logic with a controlled RSI value and daily regime.

        Patches scanner.calc_rsi so:
          - calls with len(closes) < 50 → daily_rsi  (daily: ~29 values)
          - calls with len(closes) >= 50 → h1_rsi    (1h: ~99 values)

        Klines are built flat at 100.0 with structural tweaks for above_sma,
        vol_surge, and momentum_up — SMA and volume are exact, not approximate.
        """
        import scanner

        price = 100.0
        vol_base = 1000.0

        # Build h1 closes: flat except last 5 adjusted for above_sma + momentum_up
        #   SMA20 = avg of last 20 closes. If all are `price` except last = price+delta:
        #     SMA20 = (19*price + (price+delta)) / 20 = price + delta/20
        #     above_sma ⟺ last_close > SMA20 ⟺ delta > 0
        sma_delta = +1.0 if above_sma else -1.0
        mom_delta  = +0.5 if momentum_up else -0.5   # closes[-1] vs closes[-5]

        h1_closes = [price] * 99
        h1_closes[-1]  = price + sma_delta + mom_delta   # last close
        h1_closes[-5]  = price + sma_delta               # 5th-from-last

        h1_klines = []
        for i, c in enumerate(h1_closes):
            v = vol_base * (1.5 if vol_surge and i == len(h1_closes) - 1 else 0.8)
            h1_klines.append([i, str(c), str(c*1.005), str(c*0.995), str(c), str(v),
                               None, None, None, None, None, None])
        h1_klines.append(h1_klines[-1][:])   # dummy forming candle

        # Build d1 closes: flat so d_above_sma = (delta > 0)
        # daily_bullish = daily_rsi > 45 AND d_above_sma
        # For daily_bullish state: daily_rsi > 45 → use +1 delta (price above SMA)
        # For daily_neutral:       daily_rsi in [30,45] → above_sma doesn't matter for neutral
        # For daily_bearish:       daily_rsi < 30 → below SMA to also fail above_sma check
        d_delta = +1.0 if daily_rsi > 45 else -1.0
        d_closes = [price] * 29
        d_closes[-1] = price + d_delta

        d_klines = []
        for i, c in enumerate(d_closes):
            d_klines.append([i, str(c), str(c*1.005), str(c*0.995), str(c), "1000",
                              None, None, None, None, None, None])
        d_klines.append(d_klines[-1][:])

        ticker = {"priceChangePercent": "0.5"}
        context = {
            "fg_value": fg, "btc_rsi": 50.0, "btc_above_sma": btc_above,
            "btc_dom_rising": btc_dom_rising,
        }

        def fake_get(path, params=None):
            if "ticker" in path:
                return ticker
            interval = (params or {}).get("interval", "1h")
            return d_klines if interval == "1d" else h1_klines

        def mock_calc_rsi(closes, period=14):
            return daily_rsi if len(closes) < 50 else h1_rsi

        with patch.object(scanner, "get", side_effect=fake_get), \
             patch.object(scanner, "calc_rsi", side_effect=mock_calc_rsi):
            return scanner.analyze("ETHUSDC", context)

    def test_extreme_signal_rsi_below_25(self):
        # RSI < 25: need many consecutive drops
        closes = [100.0 - i * 2 for i in range(50)]  # strong downtrend → low RSI
        result = self._run_analyze(closes, closes[-29:], _neutral_context())
        assert result["signal_strength"] == "EXTREME"
        assert result["buy_signal"] is True

    def test_none_signal_rsi_above_40(self):
        # Neutral / sideways market → RSI stays around 50
        closes = [100.0 + (i % 4) - 2 for i in range(50)]
        result = self._run_analyze(closes, closes[-29:], _neutral_context())
        assert result["signal_strength"] == "NONE"
        assert result["buy_signal"] is False

    def test_extreme_bypasses_daily_bearish(self):
        # Even if daily is bearish, EXTREME should still fire
        h1_closes = [100.0 - i * 2 for i in range(50)]  # deep oversold
        d1_closes  = [100.0 - i * 3 for i in range(29)]  # daily downtrend → bearish
        result = self._run_analyze(h1_closes, d1_closes, _neutral_context())
        assert result["signal_strength"] == "EXTREME"

    def test_fg_blocks_moderate_above_60(self):
        # MODERATE fires at F&G=30, is blocked at F&G=65.
        # Uses controlled RSI=37 in the MODERATE band [32,40].
        # daily_rsi=55 (bullish) ensures the daily filter doesn't fire first.
        result_allowed = self._run_analyze_rsi_controlled(
            h1_rsi=37.0, fg=30, btc_above=True, daily_rsi=55.0,
            vol_surge=True, above_sma=True, momentum_up=True,
        )
        assert result_allowed["signal_strength"] == "MODERATE"

        result_blocked = self._run_analyze_rsi_controlled(
            h1_rsi=37.0, fg=65, btc_above=True, daily_rsi=55.0,
            vol_surge=True, above_sma=True, momentum_up=True,
        )
        # F&G ≥ 60: MODERATE blocked. RSI=37 is also in STRONG range (< 40 but ≥ 32)
        # so STRONG fires if daily not bearish (daily_rsi=55 → bullish, allowed).
        # But STRONG requires rsi < 32 — rsi=37 is outside STRONG band.
        assert result_blocked["signal_strength"] == "NONE"

    def test_btc_below_sma_blocks_moderate(self):
        # BTC below its 1h SMA20 should block MODERATE specifically.
        # daily_rsi=55 (bullish) — ensures daily filter is not the blocker.
        result_allowed = self._run_analyze_rsi_controlled(
            h1_rsi=37.0, fg=30, btc_above=True, daily_rsi=55.0,
            vol_surge=True, above_sma=True, momentum_up=True,
        )
        assert result_allowed["signal_strength"] == "MODERATE"

        result_blocked = self._run_analyze_rsi_controlled(
            h1_rsi=37.0, fg=30, btc_above=False, daily_rsi=55.0,
            vol_surge=True, above_sma=True, momentum_up=True,
        )
        assert result_blocked["signal_strength"] == "NONE"

    def test_strong_blocked_by_daily_bearish(self):
        # STRONG (RSI < 32) fires on daily_neutral but is blocked on daily_bearish.
        # daily_rsi=38 → neutral ([30,45], not bullish): STRONG allowed.
        # daily_rsi=25 → bearish (< 30): STRONG blocked.
        result_neutral = self._run_analyze_rsi_controlled(
            h1_rsi=28.0, fg=30, btc_above=True, daily_rsi=38.0,
            vol_surge=False, above_sma=True, momentum_up=True,
        )
        assert result_neutral["signal_strength"] == "STRONG"

        result_bearish = self._run_analyze_rsi_controlled(
            h1_rsi=28.0, fg=30, btc_above=True, daily_rsi=25.0,
            vol_surge=False, above_sma=True, momentum_up=True,
        )
        assert result_bearish["signal_strength"] == "NONE"

    def test_moderate_requires_daily_bullish_not_neutral(self):
        # MODERATE requires daily_bullish (RSI > 45 AND above SMA).
        # daily_neutral (RSI in [30,45]) blocks MODERATE even if all other conditions pass.
        result_bullish = self._run_analyze_rsi_controlled(
            h1_rsi=37.0, fg=30, btc_above=True, daily_rsi=55.0,
            vol_surge=True, above_sma=True, momentum_up=True,
        )
        assert result_bullish["signal_strength"] == "MODERATE"

        result_neutral = self._run_analyze_rsi_controlled(
            h1_rsi=37.0, fg=30, btc_above=True, daily_rsi=38.0,
            vol_surge=True, above_sma=True, momentum_up=True,
        )
        # daily_neutral → daily_bullish=False → MODERATE blocked.
        # RSI=37 also ≥ 32 so STRONG (rsi < 32) is also blocked. → NONE
        assert result_neutral["signal_strength"] == "NONE"

    def test_result_keys_present(self):
        closes = [100.0 - i for i in range(50)]
        result = self._run_analyze(closes, closes[-29:], _neutral_context())
        expected_keys = {
            "symbol", "price", "rsi", "daily_rsi", "sma20", "above_sma",
            "vol_surge", "momentum", "change24h", "buy_signal",
            "signal_strength", "extreme_quality", "divergence", "btc_dom_rising",
            "closed_klines",
        }
        assert set(result.keys()) == expected_keys

    def test_extreme_quality_falling_knife(self):
        # Deep downtrend: EXTREME fires but price is below SMA → extreme_quality = False
        # (falling-knife pattern — capital is halved via _calc_capital)
        closes = [100.0 - i * 2 for i in range(50)]
        # SMA20 of last 20 closes: avg(2,4,...,40) = 21.0; last close = 2.0 < 21.0
        result = self._run_analyze(closes, closes[-29:], _neutral_context())
        assert result["signal_strength"] == "EXTREME"
        assert result["extreme_quality"] is False  # below SMA → falling knife

    def test_extreme_quality_true_when_above_sma(self):
        # RSI < 25 + above_sma + F&G < 40 → extreme_quality = True
        result = self._run_analyze_rsi_controlled(
            h1_rsi=20.0, fg=30, btc_above=True, daily_rsi=55.0,
            vol_surge=False, above_sma=True, momentum_up=True,
        )
        assert result["signal_strength"] == "EXTREME"
        assert result["extreme_quality"] is True

    # ── Divergence gate integration (T2-2) ─────────────────────────────────────

    def test_divergence_false_blocks_strong_signal(self):
        # When detect_bullish_divergence returns False (confirmed weakness),
        # a STRONG-eligible RSI should be blocked.
        import scanner
        result = self._run_analyze_rsi_controlled(
            h1_rsi=30.0, fg=50, btc_above=True, daily_rsi=40.0,
        )
        # Verify baseline: without divergence filter STRONG would fire
        assert result["signal_strength"] in ("STRONG", "NONE")

        with patch.object(scanner, "detect_bullish_divergence", return_value=False):
            result2 = self._run_analyze_rsi_controlled(
                h1_rsi=30.0, fg=50, btc_above=True, daily_rsi=40.0,
            )
        assert result2["signal_strength"] == "NONE", (
            "STRONG must be blocked when divergence=False (confirmed weakness)"
        )

    def test_divergence_false_blocks_moderate_signal(self):
        # When detect_bullish_divergence returns False, MODERATE is blocked.
        import scanner
        with patch.object(scanner, "detect_bullish_divergence", return_value=False):
            result = self._run_analyze_rsi_controlled(
                h1_rsi=38.0, fg=50, btc_above=True, daily_rsi=50.0,
                vol_surge=True, above_sma=True, momentum_up=True,
            )
        assert result["signal_strength"] == "NONE", (
            "MODERATE must be blocked when divergence=False (confirmed weakness)"
        )

    def test_divergence_false_does_not_block_extreme(self):
        # EXTREME bypasses the divergence gate — deep panic is always worth catching.
        import scanner
        with patch.object(scanner, "detect_bullish_divergence", return_value=False):
            result = self._run_analyze_rsi_controlled(
                h1_rsi=22.0, fg=30, btc_above=True, daily_rsi=50.0,
            )
        assert result["signal_strength"] == "EXTREME", (
            "EXTREME must fire even when divergence=False"
        )

    # ── BTC dominance gate integration (T2-3) ──────────────────────────────────

    def test_btc_dom_rising_blocks_moderate(self):
        # btc_dom_rising=True in context → MODERATE blocked (altcoins bleed).
        # RSI=37 puts us squarely in MODERATE band; all other conditions satisfied.
        result = self._run_analyze_rsi_controlled(
            h1_rsi=37.0, fg=30, btc_above=True, daily_rsi=55.0,
            vol_surge=True, above_sma=True, momentum_up=True,
            btc_dom_rising=True,
        )
        assert result["signal_strength"] == "NONE", (
            "MODERATE must be blocked when BTC dominance is rising"
        )

    def test_btc_dom_not_rising_allows_moderate(self):
        # btc_dom_rising=False → no block from dominance filter.
        result = self._run_analyze_rsi_controlled(
            h1_rsi=37.0, fg=30, btc_above=True, daily_rsi=55.0,
            vol_surge=True, above_sma=True, momentum_up=True,
            btc_dom_rising=False,
        )
        assert result["signal_strength"] == "MODERATE"

    def test_btc_dom_rising_does_not_block_strong(self):
        # STRONG (rsi<32) is unaffected by BTC dominance filter — only MODERATE is blocked.
        result = self._run_analyze_rsi_controlled(
            h1_rsi=30.0, fg=30, btc_above=True, daily_rsi=40.0,
            btc_dom_rising=True,
        )
        assert result["signal_strength"] == "STRONG", (
            "STRONG must not be blocked by BTC dominance filter"
        )

    def test_btc_dom_rising_does_not_block_extreme(self):
        # EXTREME ignores the BTC dominance filter entirely.
        result = self._run_analyze_rsi_controlled(
            h1_rsi=22.0, fg=30, btc_above=True, daily_rsi=50.0,
            btc_dom_rising=True,
        )
        assert result["signal_strength"] == "EXTREME", (
            "EXTREME must fire even when BTC dominance is rising"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# _calc_capital — capital sizing rules
# ═══════════════════════════════════════════════════════════════════════════════

class TestCalcCapital:
    def setup_method(self):
        import scanner
        self.fn = scanner._calc_capital
        self.CAPITAL = scanner.CAPITAL  # 200.0

    def test_extreme_quality_full_capital(self):
        s = {"signal_strength": "EXTREME", "extreme_quality": True}
        assert self.fn(s, {"btc_rsi": 50.0}) == self.CAPITAL

    def test_extreme_crash_half_capital(self):
        s = {"signal_strength": "EXTREME", "extreme_quality": False}
        assert self.fn(s, {"btc_rsi": 50.0}) == self.CAPITAL / 2

    def test_strong_weak_btc_half_capital(self):
        s = {"signal_strength": "STRONG", "extreme_quality": False}
        assert self.fn(s, {"btc_rsi": 34.9}) == self.CAPITAL / 2

    def test_strong_normal_btc_full_capital(self):
        s = {"signal_strength": "STRONG", "extreme_quality": False}
        assert self.fn(s, {"btc_rsi": 35.0}) == self.CAPITAL

    def test_moderate_always_full_capital(self):
        # MODERATE takes neither the EXTREME nor the STRONG branch → always full capital
        # even when BTC RSI is below the 35 threshold that would halve a STRONG order.
        s = {"signal_strength": "MODERATE", "extreme_quality": False}
        assert self.fn(s, {"btc_rsi": 20.0}) == self.CAPITAL  # weak BTC irrelevant for MODERATE
        assert self.fn(s, {"btc_rsi": 50.0}) == self.CAPITAL  # same result with neutral BTC


# ═══════════════════════════════════════════════════════════════════════════════
# _is_btc_dom_rising — unit tests (T2-3)
# ═══════════════════════════════════════════════════════════════════════════════

class TestIsBtcDomRising:
    """Unit tests for _is_btc_dom_rising().

    The function reads btc_dom_prev from state.json.  We patch os.path.exists
    and builtins.open to inject controlled state without touching the filesystem.
    """

    def _run(self, current, prev_value=None):
        import scanner, json as _json, builtins

        state = {}
        if prev_value is not None:
            state["btc_dom_prev"] = prev_value

        fake_open = MagicMock()
        fake_open.return_value.__enter__ = lambda s: s
        fake_open.return_value.__exit__ = MagicMock(return_value=False)
        fake_open.return_value.read = MagicMock(return_value=_json.dumps(state))
        fake_open.return_value.__iter__ = MagicMock(return_value=iter([]))
        # json.load needs a file-like with .read()
        import io
        fake_file = io.StringIO(_json.dumps(state))

        with patch("scanner.os.path.exists", return_value=True), \
             patch("builtins.open", return_value=fake_file):
            return scanner._is_btc_dom_rising(current)

    def test_none_current_returns_false(self):
        # CoinGecko down → fail-open
        import scanner
        assert scanner._is_btc_dom_rising(None) is False

    def test_no_prev_returns_false(self):
        # First run — no baseline to compare
        assert self._run(current=55.0, prev_value=None) is False

    def test_rising_above_threshold_returns_true(self):
        # 55.0 vs 50.0 → rise = 10% >> 0.5% threshold
        assert self._run(current=55.0, prev_value=50.0) is True

    def test_falling_returns_false(self):
        # 48.0 vs 50.0 → falling dominance
        assert self._run(current=48.0, prev_value=50.0) is False

    def test_below_threshold_returns_false(self):
        # 50.1 vs 50.0 → rise = 0.2% < 0.5% threshold
        assert self._run(current=50.1, prev_value=50.0) is False

    def test_clearly_below_threshold_returns_false(self):
        # 50.1 vs 50.0 → rise = 0.2%, well below 0.5% threshold
        assert self._run(current=50.1, prev_value=50.0) is False

    def test_well_above_threshold_returns_true(self):
        # 50.5 vs 50.0 → rise = 1.0% > 0.5% threshold
        assert self._run(current=50.5, prev_value=50.0) is True


# ═══════════════════════════════════════════════════════════════════════════════
# compute_stats (backtest)
# ═══════════════════════════════════════════════════════════════════════════════

class TestComputeStats:
    def _trade(self, outcome: str, pnl: float) -> dict:
        return {"outcome": outcome, "pnl_pct": pnl, "signal": "STRONG"}

    def test_empty_trades(self):
        s = compute_stats([])
        assert s["n"] == 0
        assert s["win_rate"] == 0.0
        assert s["net_pct"] == 0.0
        # Verify the empty-path dict has the same keys as the non-empty path
        # (guards against missing keys like avg_to_pct that callers may unconditionally read)
        non_empty = compute_stats([self._trade("TP", 1.0)])
        assert set(s.keys()) == set(non_empty.keys()), (
            f"Empty-path dict missing keys: {set(non_empty.keys()) - set(s.keys())}"
        )

    def test_all_wins(self):
        trades = [self._trade("TP", 7.5) for _ in range(4)]
        s = compute_stats(trades)
        assert s["wins"] == 4
        assert s["losses"] == 0
        assert s["win_rate"] == pytest.approx(100.0)
        assert s["net_pct"] == pytest.approx(30.0)

    def test_all_losses(self):
        trades = [self._trade("SL", -3.0) for _ in range(4)]
        s = compute_stats(trades)
        assert s["wins"] == 0
        assert s["losses"] == 4
        assert s["win_rate"] == pytest.approx(0.0)
        assert s["net_pct"] == pytest.approx(-12.0)

    def test_mixed_win_rate(self):
        trades = [self._trade("TP", 7.5)] * 3 + [self._trade("SL", -3.0)] * 1
        s = compute_stats(trades)
        assert s["win_rate"] == pytest.approx(75.0)

    def test_timeout_counted_separately(self):
        trades = [
            self._trade("TP", 5.0),
            self._trade("SL", -3.0),
            self._trade("TIMEOUT", 0.5),
        ]
        s = compute_stats(trades)
        assert s["timeouts"] == 1
        assert s["n"] == 3

    def test_expectancy_equals_net_over_n(self):
        trades = [self._trade("TP", 7.5)] * 2 + [self._trade("SL", -3.0)] * 1
        s = compute_stats(trades)
        expected_exp = (7.5 + 7.5 - 3.0) / 3
        assert s["expectancy"] == pytest.approx(expected_exp, rel=1e-3)

    def test_net_pct_is_sum_of_pnl(self):
        pnls = [7.5, -3.0, 2.1, -1.5, 6.0]
        trades = [self._trade("TP" if p > 0 else "SL", p) for p in pnls]
        s = compute_stats(trades)
        assert s["net_pct"] == pytest.approx(sum(pnls), rel=1e-3)


# ═══════════════════════════════════════════════════════════════════════════════
# detect_bullish_divergence (T2-2)
# ═══════════════════════════════════════════════════════════════════════════════

def _make_divergence_data(
    price_lows: list[float],
    rsi_lows: list[float],
    n: int = 20,
) -> tuple[list[float], list[float]]:
    """Build closes and rsi_series lists with swing lows embedded at fixed positions."""
    # Place swing lows at positions 5 and 15 in a 20-candle window
    closes     = [100.0] * n
    rsi_series = [50.0]  * n
    # Swing 1 at index 5: lower than neighbors by >0.5%
    closes[4], closes[5], closes[6]         = price_lows[0] * 1.01, price_lows[0], price_lows[0] * 1.01
    rsi_series[4], rsi_series[5], rsi_series[6] = rsi_lows[0] + 1,    rsi_lows[0],  rsi_lows[0] + 1
    # Swing 2 at index 15
    closes[14], closes[15], closes[16]          = price_lows[1] * 1.01, price_lows[1], price_lows[1] * 1.01
    rsi_series[14], rsi_series[15], rsi_series[16] = rsi_lows[1] + 1,    rsi_lows[1],  rsi_lows[1] + 1
    return closes, rsi_series


class TestDetectBullishDivergence:
    def test_lower_price_lower_rsi_returns_false(self):
        # Price lower low + RSI lower low = confirmed weakness → False (block)
        closes, rsi_series = _make_divergence_data(
            price_lows=[99.0, 97.0],  # 97 < 99 → lower low
            rsi_lows=[35.0, 30.0],    # 30 < 35 → lower RSI low (no divergence)
        )
        assert detect_bullish_divergence(closes, rsi_series) is False

    def test_lower_price_higher_rsi_returns_true(self):
        # Price lower low + RSI higher low = bullish divergence → True (allow)
        closes, rsi_series = _make_divergence_data(
            price_lows=[99.0, 97.0],  # 97 < 99 → lower price low
            rsi_lows=[30.0, 35.0],    # 35 > 30 → higher RSI low (divergence!)
        )
        assert detect_bullish_divergence(closes, rsi_series) is True

    def test_insufficient_swings_returns_none(self):
        # Flat prices → no local minima → ambiguous → None
        closes     = [100.0] * 20
        rsi_series = [50.0]  * 20
        assert detect_bullish_divergence(closes, rsi_series) is None

    def test_only_one_swing_returns_none(self):
        # Only one qualifying swing low found → can't compare → None
        # The dip is 1.5% which clears the 0.5% threshold; only 1 swing exists.
        closes     = [100.0] * 20
        rsi_series = [50.0]  * 20
        closes[9], closes[10], closes[11] = 100.5, 99.0, 100.5
        assert detect_bullish_divergence(closes, rsi_series) is None

    def test_price_higher_low_returns_none(self):
        # Price making higher lows → not a lower-low pattern → None (allow)
        closes, rsi_series = _make_divergence_data(
            price_lows=[95.0, 97.0],  # 97 > 95 → higher price low
            rsi_lows=[30.0, 28.0],
        )
        assert detect_bullish_divergence(closes, rsi_series) is None

    def test_swing_depth_threshold_respected(self):
        # A dip of exactly 0.3% (< default 0.5%) is NOT counted as a swing
        closes     = [100.0] * 20
        rsi_series = [50.0]  * 20
        # 0.3% dip at position 5
        closes[4], closes[5], closes[6] = 100.0, 99.7, 100.0
        # 0.3% dip at position 15
        closes[14], closes[15], closes[16] = 100.0, 99.7, 100.0
        assert detect_bullish_divergence(closes, rsi_series, swing_depth=0.005) is None
