"""Provider-independent, hypothetical cross-bookmaker arbitrage detection."""

from betting_scanner.strategies.arbitrage.calculation import calculate_arbitrage
from betting_scanner.strategies.arbitrage.engine import detect_arbitrage
from betting_scanner.strategies.arbitrage.results import ArbitrageResult, EngineConfig

__all__ = ["ArbitrageResult", "EngineConfig", "calculate_arbitrage", "detect_arbitrage"]
