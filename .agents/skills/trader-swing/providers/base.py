"""Base market data provider interface for trader-swing."""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional
import os
import sys

# Ensure local imports work regardless of execution context
PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

from market_data_models import OHLCVCandle


class MarketDataProvider(ABC):
    """Abstract base class for all market data providers (Yahoo, Polygon, IBKR, etc.)."""

    name: str = "base"
    is_delayed: bool = True
    native_adjustment_type: str = "RAW_UNADJUSTED"

    @abstractmethod
    def fetch_historical_bars(
        self,
        ticker: str,
        interval: str,
        start: datetime,
        end: datetime,
        current_time: Optional[datetime] = None,
    ) -> List[OHLCVCandle]:
        """Fetch historical completed and optionally forming OHLCV candles."""
        pass

    @abstractmethod
    def fetch_latest_bar(
        self,
        ticker: str,
        interval: str,
        current_time: Optional[datetime] = None,
    ) -> Optional[OHLCVCandle]:
        """Fetch the single most recent candle for the specified ticker and interval."""
        pass

    @abstractmethod
    def get_provider_metadata(self) -> Dict[str, Any]:
        """Return provider properties, delay disclosures, and endpoint health metadata."""
        pass
