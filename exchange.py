# =============================================================================
# exchange.py — 取引所ABCと Bybit 実装
# Exchange abstract base class and Bybit implementation
#
# 本番対応: TradeExchange.py
# Production equivalent: TradeExchange.py (~3,523行 → ~380行に簡略化)
#
# 設計パターン: Strategy + Factory
# Design patterns: Strategy + Factory
#
# 本番では Bitget（独自SDK）、Bybit（CCXT）、Phemex（CCXT）の3取引所を実装。
# このサンプルでは Bybit（CCXT）のみ実装。
# Production implements 3 exchanges: Bitget (native SDK), Bybit (CCXT), Phemex (CCXT).
# This sample implements Bybit (CCXT) only.
# =============================================================================

import logging
import time
from abc import ABC, abstractmethod
from decimal import ROUND_DOWN, Decimal, getcontext
from typing import Dict, Optional, Tuple

import ccxt

import config

# 金融計算用高精度 Decimal コンテキスト / High-precision Decimal context for finance
getcontext().prec = 28


# =============================================================================
# TradeExchange — 取引所 ABC（抽象基底クラス）
# Abstract base class for exchange adapters
# 本番対応: TradeExchange(ABC) in TradeExchange.py
# =============================================================================
class TradeExchange(ABC):
    """
    取引所 API アダプタの抽象基底クラス。
    Abstract base class for exchange API adapters.

    全取引所の具体実装はこのインタフェースを実装する。
    All concrete exchange implementations must implement this interface.

    設計原則 / Design principles:
    - このクラスは純粋な API 呼び出しのみ（DB操作・リトライなし）
    - DB 操作、リトライ、レート制限は TradeEngine 層が担当
    - Pure API calls only (no DB operations, no retries)
    - DB ops, retries, and rate limiting are handled by TradeEngine layer
    """

    @abstractmethod
    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        price: Decimal,
        qty: Decimal,
        reduce_only: bool = False,
    ) -> Optional[str]:
        """
        注文を発注する。order_id を返す。失敗時は None。
        Place an order. Returns order_id, or None on failure.

        Args:
            symbol:      銘柄 e.g. "BTCUSDT" / Symbol e.g. "BTCUSDT"
            side:        "buy" or "sell"
            order_type:  "limit" or "market"
            price:       指値価格（成行の場合は無視）/ Limit price (ignored for market)
            qty:         注文数量（コントラクト数）/ Order quantity (contracts)
            reduce_only: ポジション縮小専用フラグ / Reduce-only flag
        """

    @abstractmethod
    def cancel_order(self, symbol: str, order_id: str) -> bool:
        """
        注文をキャンセルする。成功時 True。
        Cancel an order. Returns True on success.
        """

    @abstractmethod
    def get_order_status(
        self, symbol: str, order_id: str
    ) -> Optional[str]:
        """
        注文ステータスを返す。
        Return order status string.

        Returns:
            "open"             — 未約定 / Not filled
            "closed"           — 全約定 / Fully filled
            "cancelled"        — キャンセル済み / Cancelled
            "partially_filled" — 一部約定 / Partially filled
            None               — 注文が見つからない / Order not found
        """

    @abstractmethod
    def get_position(self, symbol: str) -> Optional[Dict]:
        """
        現在の先物ポジションを返す。なければ None。
        Return current futures position or None if no position.

        Returns:
            {
                "symbol":       str,
                "side":         "buy" or "sell",
                "size":         Decimal,
                "entry_price":  Decimal,
                "liq_price":    Decimal,  # 強制決済価格 / Liquidation price
                "unrealized_pnl": Decimal,
            }
        """

    @abstractmethod
    def get_ticker_price(self, symbol: str) -> Decimal:
        """
        最新の中間価格を返す。
        Return the latest mid-price (ticker).
        """

    @abstractmethod
    def get_balance(self, coin: str = "USDT") -> Decimal:
        """
        指定コインの利用可能残高を返す。
        Return available balance for the specified coin.
        """

    @abstractmethod
    def set_leverage(self, symbol: str, leverage: int) -> bool:
        """
        先物レバレッジを設定する。成功時 True。
        Set futures leverage. Returns True on success.
        """

    @abstractmethod
    def get_symbol_info(self, symbol: str) -> Dict:
        """
        銘柄の精度情報を返す。
        Return precision metadata for a symbol.

        Returns:
            {
                "qty_prec":   int,      # 数量小数点以下桁数 / Qty decimal places
                "qty_step":   Decimal,  # 数量の最小単位 / Minimum qty increment
                "min_qty":    Decimal,  # 最小注文数量 / Minimum order quantity
                "price_prec": int,      # 価格小数点以下桁数 / Price decimal places
                "min_notional": Decimal,# 最小注文金額(USDT) / Min order value (USDT)
            }
        """

    # --------------------------------------------------------------------------
    # 共通ユーティリティ / Shared utilities
    # 本番の TradeUtil.adjust_price / round_to_step に対応
    # Production equivalent: TradeUtil.adjust_price / round_to_step
    # --------------------------------------------------------------------------
    def adjust_qty(self, qty: Decimal, qty_step: Decimal, qty_prec: int) -> Decimal:
        """
        取引所の最小数量単位に切り捨てる。
        Floor quantity to the exchange's minimum lot size.

        float の丸め誤差を避けるため Decimal で処理。
        Processed in Decimal to avoid float rounding errors.
        """
        if qty_step == 0:
            return qty
        steps = (qty / qty_step).to_integral_value(rounding=ROUND_DOWN)
        result = steps * qty_step
        quant = Decimal(10) ** -qty_prec
        return result.quantize(quant, rounding=ROUND_DOWN)

    def adjust_price(self, price: Decimal, price_prec: int) -> Decimal:
        """
        取引所の価格精度に丸める。
        Round price to exchange price precision.
        """
        quant = Decimal(10) ** -price_prec
        return price.quantize(quant, rounding=ROUND_DOWN)


# =============================================================================
# BybitExchange — Bybit 具体実装
# Concrete Bybit exchange adapter
# 本番対応: BybitExchange(CCXTBaseExchange) in TradeExchange.py
#
# USDT証拠金無期限先物（linear perpetual）を使用。
# Uses USDT-margined linear perpetual futures.
# =============================================================================
class BybitExchange(TradeExchange):
    """
    CCXT ライブラリを使った Bybit 取引所アダプタ。
    Bybit exchange adapter using the CCXT library.

    本番では CCXTBaseExchange を継承し、サーキットブレーカー機能を持つ。
    Production inherits CCXTBaseExchange with circuit breaker functionality.
    (Circuit breaker: 3回連続タイムアウトで2分間 API 呼び出しを停止)
    (Circuit breaker: 3 consecutive timeouts → 2-minute pause on all API calls)
    """

    # シンボル情報キャッシュの有効期限（秒）/ Symbol info cache TTL (seconds)
    _SYMBOL_CACHE_TTL = 3600  # 1時間 / 1 hour

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool = True,
        log: Optional[logging.Logger] = None,
    ) -> None:
        self._log = log or logging.getLogger(__name__)
        self._exchange = ccxt.bybit({
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "options": {
                "defaultType": "linear",  # USDT建て先物 / USDT-margined futures
            },
        })
        if testnet:
            self._exchange.set_sandbox_mode(True)
            self._log.info("[exchange] Bybit testnet mode enabled")

        # シンボル情報キャッシュ / Symbol info cache
        self._symbol_cache: Dict[str, Dict] = {}
        self._cache_loaded_at: float = 0.0

    def _ccxt_symbol(self, symbol: str) -> str:
        """
        内部シンボル形式を CCXT 形式に変換する。
        Convert internal symbol format to CCXT format.

        "BTCUSDT" → "BTC/USDT:USDT" (linear futures)
        "ETHUSDT" → "ETH/USDT:USDT"
        """
        base = symbol.replace("USDT", "")
        return f"{base}/USDT:USDT"

    def _load_markets_if_needed(self) -> None:
        """
        TTL 期限切れの場合にマーケット情報を再取得する。
        Reload market info if cache has expired.
        """
        now = time.monotonic()
        if now - self._cache_loaded_at > self._SYMBOL_CACHE_TTL:
            self._log.debug("[exchange] loading markets from Bybit")
            markets = self._exchange.load_markets(reload=True)
            self._symbol_cache = {
                sym.replace("/USDT:USDT", "USDT"): info
                for sym, info in markets.items()
                if sym.endswith("/USDT:USDT")
            }
            self._cache_loaded_at = now

    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        price: Decimal,
        qty: Decimal,
        reduce_only: bool = False,
    ) -> Optional[str]:
        """
        Bybit に注文を発注する。order_id を返す。
        Place an order on Bybit. Returns order_id.

        CCXT への変換:
        - Decimal → float（CCXT は float を期待）
        - "BTCUSDT" → "BTC/USDT:USDT"

        CCXT conversion:
        - Decimal → float (CCXT expects float)
        - "BTCUSDT" → "BTC/USDT:USDT"
        """
        try:
            ccxt_sym = self._ccxt_symbol(symbol)
            params: Dict = {}
            if reduce_only:
                params["reduceOnly"] = True

            response = self._exchange.create_order(
                symbol=ccxt_sym,
                type=order_type,              # "limit" or "market"
                side=side,                    # "buy" or "sell"
                amount=float(qty),            # Decimal → float (CCXT境界のみ)
                price=float(price) if order_type == "limit" else None,
                params=params,
            )
            order_id = response.get("id")
            self._log.info(
                f"[exchange] place_order {symbol} {side} {order_type} "
                f"qty={qty} price={price} → order_id={order_id}"
            )
            return order_id
        except ccxt.BaseError as e:
            self._log.error(f"[exchange] place_order error {symbol}: {e}")
            return None

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        """
        注文をキャンセルする。
        Cancel an order.
        """
        try:
            self._exchange.cancel_order(
                id=order_id,
                symbol=self._ccxt_symbol(symbol),
            )
            self._log.info(f"[exchange] cancel_order {symbol} order_id={order_id}")
            return True
        except ccxt.OrderNotFound:
            # 既にキャンセル済みまたは約定済み / Already cancelled or filled
            self._log.warning(
                f"[exchange] cancel_order not found {symbol} order_id={order_id}"
            )
            return True
        except ccxt.BaseError as e:
            self._log.error(f"[exchange] cancel_order error {symbol}: {e}")
            return False

    def get_order_status(
        self, symbol: str, order_id: str
    ) -> Optional[str]:
        """
        注文ステータスを取得する。
        Fetch order status.

        CCXT ステータス → 内部ステータスのマッピング:
        CCXT status → internal status mapping:
          "open"      → "open"
          "closed"    → "closed"
          "canceled"  → "cancelled"
          "partially_filled" → "partially_filled"
        """
        try:
            order = self._exchange.fetch_order(
                id=order_id,
                symbol=self._ccxt_symbol(symbol),
            )
            raw_status = order.get("status", "")
            status_map = {
                "open": "open",
                "closed": "closed",
                "canceled": "cancelled",
                "partially_filled": "partially_filled",
            }
            return status_map.get(raw_status, raw_status)
        except ccxt.OrderNotFound:
            return None
        except ccxt.BaseError as e:
            self._log.error(f"[exchange] get_order_status error {symbol}: {e}")
            return None

    def get_position(self, symbol: str) -> Optional[Dict]:
        """
        現在のポジションを取得する。
        Fetch current position.
        """
        try:
            positions = self._exchange.fetch_positions(
                symbols=[self._ccxt_symbol(symbol)]
            )
            for pos in positions:
                size = Decimal(str(pos.get("contracts", 0) or 0))
                if size > 0:
                    return {
                        "symbol": symbol,
                        "side": pos.get("side", "buy"),
                        "size": size,
                        "entry_price": Decimal(str(pos.get("entryPrice") or 0)),
                        "liq_price": Decimal(str(pos.get("liquidationPrice") or 0)),
                        "unrealized_pnl": Decimal(str(pos.get("unrealizedPnl") or 0)),
                    }
            return None
        except ccxt.BaseError as e:
            self._log.error(f"[exchange] get_position error {symbol}: {e}")
            return None

    def get_ticker_price(self, symbol: str) -> Decimal:
        """
        最新価格（中間価格）を取得する。
        Fetch latest mid-price.
        """
        ticker = self._exchange.fetch_ticker(self._ccxt_symbol(symbol))
        # bid/ask の中間値を使用 / Use mid-price of bid/ask
        bid = Decimal(str(ticker.get("bid") or ticker.get("last") or 0))
        ask = Decimal(str(ticker.get("ask") or ticker.get("last") or 0))
        if bid > 0 and ask > 0:
            return (bid + ask) / 2
        return Decimal(str(ticker.get("last") or 0))

    def get_balance(self, coin: str = "USDT") -> Decimal:
        """
        利用可能残高を取得する。
        Fetch available balance.
        """
        try:
            balance = self._exchange.fetch_balance()
            free = balance.get("free", {}).get(coin, 0) or 0
            return Decimal(str(free))
        except ccxt.BaseError as e:
            self._log.error(f"[exchange] get_balance error: {e}")
            return Decimal("0")

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        """
        先物レバレッジを設定する。
        Set futures leverage.
        """
        try:
            self._exchange.set_leverage(
                leverage=leverage,
                symbol=self._ccxt_symbol(symbol),
            )
            self._log.info(f"[exchange] set_leverage {symbol} × {leverage}")
            return True
        except ccxt.BaseError as e:
            self._log.error(f"[exchange] set_leverage error {symbol}: {e}")
            return False

    def get_symbol_info(self, symbol: str) -> Dict:
        """
        銘柄の精度情報を取得する（キャッシュ利用）。
        Return precision metadata for a symbol (uses cache).

        本番の TradeUtil.get_prec() / future_info_maps に対応。
        Production equivalent: TradeUtil.get_prec() / future_info_maps.
        """
        self._load_markets_if_needed()

        market = self._symbol_cache.get(symbol)
        if market is None:
            raise ValueError(f"Symbol not found on Bybit: {symbol}")

        precision = market.get("precision", {})
        limits = market.get("limits", {})

        qty_prec = int(precision.get("amount", 3))
        price_prec = int(precision.get("price", 2))
        qty_step = Decimal(str(precision.get("amount", 10 ** -qty_prec)))
        min_qty = Decimal(str(limits.get("amount", {}).get("min", qty_step)))
        min_cost = Decimal(str(limits.get("cost", {}).get("min", 5)))

        return {
            "qty_prec": qty_prec,
            "qty_step": qty_step,
            "min_qty": min_qty,
            "price_prec": price_prec,
            "min_notional": min_cost,
        }

    def get_filled_avg_price(
        self, symbol: str, order_id: str
    ) -> Tuple[Optional[Decimal], Optional[Decimal]]:
        """
        約定した注文の平均価格と約定数量を取得する。
        Fetch average fill price and filled quantity for an order.

        Returns:
            (avg_price, filled_qty) または (None, None) / or (None, None)
        """
        try:
            order = self._exchange.fetch_order(
                id=order_id, symbol=self._ccxt_symbol(symbol)
            )
            avg = order.get("average")
            filled = order.get("filled")
            if avg and filled:
                return Decimal(str(avg)), Decimal(str(filled))
            return None, None
        except ccxt.BaseError as e:
            self._log.error(f"[exchange] get_filled_avg_price error: {e}")
            return None, None


# =============================================================================
# Factory — 取引所インスタンスを生成する
# Factory to create exchange instances
# 本番の get_exchange(platform) に対応
# Production equivalent: get_exchange(platform) in TradeExchange.py
# =============================================================================
def create_exchange(
    api_key: str,
    api_secret: str,
    testnet: bool = True,
    log: Optional[logging.Logger] = None,
) -> TradeExchange:
    """
    取引所アダプタを生成して返す。
    Create and return an exchange adapter.

    本番では platform 番号（1=Bitget, 2=Bybit, 3=Phemex）で分岐。
    Production branches by platform number (1=Bitget, 2=Bybit, 3=Phemex).
    このサンプルでは Bybit 固定。
    This sample always returns Bybit.
    """
    return BybitExchange(
        api_key=api_key,
        api_secret=api_secret,
        testnet=testnet,
        log=log,
    )
