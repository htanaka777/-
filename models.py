# =============================================================================
# models.py — データモデル定義
# Data model definitions
#
# 本番では Data.py + CommonMessage.py + 各所のdataclass に分散。
# Production equivalents: Data.py, CommonMessage.py, and inline dataclasses
# across TradeUtil.py and TradeDB.py.
# =============================================================================

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional


# =============================================================================
# ParsedSignal — シグナル解析結果
# Parsed trading signal from HTTP POST body
# 本番対応: CommonMessage.ParsedMessage
# =============================================================================
@dataclass
class ParsedSignal:
    """
    HTTP POST で受信した取引シグナルの解析結果。
    Parsed result of a trading signal received via HTTP POST.

    本番では Telegram メッセージを CommonMessage.parse_message() で解析。
    In production, Telegram messages are parsed by CommonMessage.parse_message().
    """
    coin: Optional[str] = None          # 銘柄名 e.g. "BTCUSDT" / Symbol name
    side: Optional[str] = None          # 売買方向 "buy" or "sell" / Trade direction
    ep_low: Optional[Decimal] = None    # エントリー価格（下限）/ Entry price (lower bound)
    ep_high: Optional[Decimal] = None   # エントリー価格（上限）/ Entry price (upper bound)
    tp1: Optional[Decimal] = None       # 利確1 / Take-profit 1
    tp2: Optional[Decimal] = None       # 利確2 / Take-profit 2
    tp3: Optional[Decimal] = None       # 利確3 / Take-profit 3
    sl: Optional[Decimal] = None        # 損切 / Stop-loss
    is_close_all: bool = False          # 全クローズシグナル / Close-all signal flag
    raw_text: str = ""                  # 元テキスト / Original raw text


# =============================================================================
# TradeRecord — トレードレコード（状態機械）
# In-flight trade state (state machine)
# 本番対応: trade_symbols テーブル + telegram_chat テーブル
# =============================================================================

# 状態機械定数 / State machine constants
# 本番の TRADE_STATE 値に対応
STATE_RECEIVED     = 0   # シグナル受信済み / Signal received
STATE_ENTRY_PLACED = 5   # エントリー注文発注済み / Entry order placed
STATE_OPEN         = 10  # エントリー約定、TP/SL発注済み / Entry filled, TP/SL placed
STATE_DCA          = 12  # DCA注文発注済み / DCA order placed
STATE_CLOSED_TP    = 15  # TP約定によりクローズ / Closed by take-profit
STATE_CLOSED_SL    = 16  # SL約定によりクローズ / Closed by stop-loss
STATE_CANCELLED    = 17  # キャンセル済み / Cancelled
STATE_ERROR        = 22  # エラー状態 / Error state


@dataclass
class TradeRecord:
    """
    1トレードの全ライフサイクル状態を保持するレコード。
    Holds the full lifecycle state of a single trade.

    本番では telegram_chat と trade_symbols の2テーブルに分散して管理。
    In production, this is split between telegram_chat and trade_symbols tables.
    """
    # DB識別子 / DB identifier
    id: Optional[int] = None

    # シグナル情報 / Signal info
    signal_id: int = 0                  # 冪等性キー / Idempotency key
    symbol: str = ""                    # e.g. "BTCUSDT"
    side: str = "buy"                   # "buy" or "sell"
    trade_type: int = 2                 # 1=現物(spot), 2=先物(futures)

    # 状態 / State
    state: int = STATE_RECEIVED

    # エントリー情報 / Entry info
    entry_price: Optional[Decimal] = None
    entry_qty: Optional[Decimal] = None
    entry_order_id: Optional[str] = None

    # 利確情報 / Take-profit info
    tp1_price: Optional[Decimal] = None
    tp2_price: Optional[Decimal] = None
    tp3_price: Optional[Decimal] = None
    tp1_size: Optional[Decimal] = None
    tp2_size: Optional[Decimal] = None
    tp3_size: Optional[Decimal] = None
    tp1_order_id: Optional[str] = None
    tp2_order_id: Optional[str] = None
    tp3_order_id: Optional[str] = None

    # 損切情報 / Stop-loss info
    sl_price: Optional[Decimal] = None
    sl_size: Optional[Decimal] = None
    sl_order_id: Optional[str] = None

    # DCA（買い下がり）情報 / DCA info
    dca_triggered: bool = False
    dca_order_id: Optional[str] = None

    # クローズ情報 / Close info
    close_price: Optional[Decimal] = None
    realized_pnl: Optional[Decimal] = None

    # タイムスタンプ / Timestamps
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# =============================================================================
# TokenBucket — API レート制限
# Token bucket for API rate limiting
# 本番対応: TokenBucket.py
# =============================================================================
@dataclass
class TokenBucket:
    """
    トークンバケットアルゴリズムによる API レート制限。
    Token bucket algorithm for API rate limiting.

    取引所の 429 (Too Many Requests) エラーを防ぐために使用。
    Used to prevent 429 (Too Many Requests) errors from exchanges.

    本番では ユーザー×プラットフォーム ごとにインスタンスを保持。
    In production, one instance is held per (user, platform) pair.
    """
    capacity: float       # バケット最大容量 / Maximum bucket capacity (tokens)
    fill_rate: float      # 補充レート / Fill rate (tokens per second)

    # 内部状態（__post_init__ で初期化）/ Internal state (initialized in __post_init__)
    _tokens: float = field(init=False)
    _last_refill_ts: float = field(init=False)
    _lock: threading.Lock = field(init=False)

    def __post_init__(self) -> None:
        self._tokens = self.capacity
        self._last_refill_ts = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        """経過時間に応じてトークンを補充する / Refill tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self._last_refill_ts
        self._tokens = min(self.capacity, self._tokens + elapsed * self.fill_rate)
        self._last_refill_ts = now

    def consume(self, amount: float = 1.0) -> bool:
        """
        トークンを消費する。残量不足の場合は False を返す。
        Consume tokens. Returns False if insufficient tokens.

        Usage:
            if not bucket.consume():
                time.sleep(0.25)  # レート制限待機 / Wait for rate limit
        """
        with self._lock:
            self._refill()
            if self._tokens >= amount:
                self._tokens -= amount
                return True
            return False

    def consume_blocking(self, amount: float = 1.0,
                         check_interval: float = 0.05) -> None:
        """
        トークンが利用可能になるまでブロッキング待機する。
        Block until tokens are available.
        """
        while not self.consume(amount):
            time.sleep(check_interval)
