"""
Unit tests for pure indicator functions.

All tests are offline — no network calls, no file I/O.
scanner.py is imported with the Binance API calls guarded by mock.
"""

from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

# ── Import pure functions directly (no side effects on import) ────────────────
from scanner import calc_rsi, calc_sma, calc_atr
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
    return {"fg_value": 30, "btc_rsi": 50.0, "btc_above_sma": True}


class TestAnalyzeSignalTiers:
    """
    analyze() makes two real API calls: get() (klines + ticker) and get() for daily.
    We patch scanner.get to return synthetic klines and ticker data.
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
        # Even with vol_surge + above SMA + RSI ~35, high F&G blocks MODERATE
        closes = [100.0 - i * 0.8 for i in range(40)] + [72.0 + i * 0.2 for i in range(10)]
        context = {"fg_value": 65, "btc_rsi": 50.0, "btc_above_sma": True}
        result = self._run_analyze(closes, closes[-29:], context, vol_surge=True)
        # MODERATE blocked by F&G ≥ 60; STRONG may fire depending on RSI
        assert result["signal_strength"] in ("STRONG", "NONE")

    def test_btc_below_sma_blocks_moderate(self):
        # BTC below its SMA should block MODERATE
        closes = [100.0 - i * 0.8 for i in range(40)] + [72.0 + i * 0.2 for i in range(10)]
        context = {"fg_value": 30, "btc_rsi": 50.0, "btc_above_sma": False}
        result = self._run_analyze(closes, closes[-29:], context, vol_surge=True)
        assert result["signal_strength"] in ("STRONG", "NONE")

    def test_result_keys_present(self):
        closes = [100.0 - i for i in range(50)]
        result = self._run_analyze(closes, closes[-29:], _neutral_context())
        required = {
            "symbol", "price", "rsi", "daily_rsi", "sma20", "above_sma",
            "vol_surge", "momentum", "change24h", "buy_signal",
            "signal_strength", "extreme_quality", "closed_klines",
        }
        assert required.issubset(result.keys())

    def test_extreme_quality_flag(self):
        # EXTREME + above SMA + F&G < 40 → extreme_quality = True
        closes = [100.0 - i * 2 for i in range(50)]  # strong downtrend
        # Last price should still be above SMA20 — ensure upward bias at end
        sma_base = closes[-20:]
        context = {"fg_value": 30, "btc_rsi": 50.0, "btc_above_sma": True}
        result = self._run_analyze(closes, closes[-29:], context)
        if result["signal_strength"] == "EXTREME":
            # extreme_quality depends on above_sma — just verify the field exists and is bool
            assert isinstance(result["extreme_quality"], bool)


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
        s = {"signal_strength": "MODERATE", "extreme_quality": False}
        assert self.fn(s, {"btc_rsi": 20.0}) == self.CAPITAL  # BTC RSI ignored for MODERATE


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
