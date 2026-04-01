# =============================================================================
# signal_parser.py — シグナルテキスト解析
# Trading signal text parser
#
# 本番対応: CommonMessage.py (parse_message, ParsedMessage)
# Production equivalent: CommonMessage.py
#
# 本番では Telegram メッセージ（日本語フォーマット）を解析。
# このサンプルでは HTTP POST で受信する英語フォーマットも対応。
# Production parses Japanese-format Telegram messages.
# This sample also supports English-format HTTP POST bodies.
# =============================================================================

import re
from decimal import Decimal, InvalidOperation
from typing import Optional

from models import ParsedSignal


# シグナルテキストのフォーマット例 / Example signal text format:
#
#   coin: BTCUSDT
#   side: buy
#   ep: 95000 ~ 96000
#   tp1: 98000
#   tp2: 100000
#   tp3: 103000
#   sl: 93000
#
# 本番フォーマット（日本語）/ Production format (Japanese):
#   銘柄：BTC
#   建玉：BUY
#   EP：95000 ～ 96000
#   利確1：98000
#   利確2：100000
#   利確3：103000
#   損切：93000


def parse_signal(text: str) -> ParsedSignal:
    """
    シグナルテキストを解析して ParsedSignal を返す。
    Parse signal text and return a ParsedSignal.

    英語フォーマット・日本語フォーマット両方に対応。
    Supports both English and Japanese formats.

    Args:
        text: シグナルの生テキスト / Raw signal text

    Returns:
        ParsedSignal: 解析結果（フィールド未設定の場合は None）
                      Parsed result (None for unset fields)
    """
    sig = ParsedSignal(raw_text=text)

    # 全クローズシグナルの検出 / Detect close-all signal
    if is_close_signal(text):
        sig.is_close_all = True
        # コイン名だけ抽出を試みる / Try to extract coin name
        sig.coin = _extract_coin(text)
        return sig

    # コイン名 / Coin name
    sig.coin = _extract_coin(text)

    # 売買方向 / Trade direction
    sig.side = _extract_side(text)

    # エントリー価格 / Entry price
    sig.ep_low, sig.ep_high = _extract_entry_price(text)

    # 利確価格 / Take-profit prices
    sig.tp1 = _extract_decimal(text, [
        r'tp1\s*[：:]\s*([\d,.]+)',
        r'利確1\s*[：:]\s*([\d,.]+)',
    ])
    sig.tp2 = _extract_decimal(text, [
        r'tp2\s*[：:]\s*([\d,.]+)',
        r'利確2\s*[：:]\s*([\d,.]+)',
    ])
    sig.tp3 = _extract_decimal(text, [
        r'tp3\s*[：:]\s*([\d,.]+)',
        r'利確3[+＋]*\s*[：:]\s*([\d,.]+)',
    ])

    # 損切価格 / Stop-loss price
    sig.sl = _extract_decimal(text, [
        r'sl\s*[：:]\s*([\d,.]+)',
        r'損切[り]?\s*[：:]\s*([\d,.]+)',
    ])

    return sig


def is_valid_signal(sig: ParsedSignal) -> bool:
    """
    シグナルが有効かチェックする。
    Check whether a parsed signal is valid for trading.

    必須フィールドの存在と価格の論理整合性を検証。
    Validates required fields and price logical consistency.
    """
    if sig.is_close_all:
        return bool(sig.coin)

    # 必須フィールド / Required fields
    required = [sig.coin, sig.side, sig.ep_high, sig.tp1, sig.sl]
    if any(f is None for f in required):
        return False

    # 側方向の確認 / Direction check
    if sig.side not in ("buy", "sell"):
        return False

    # BUY の場合: TP1 > EP > SL / For BUY: TP1 > EP > SL
    if sig.side == "buy":
        if sig.tp1 <= sig.ep_high:
            return False
        if sig.sl >= sig.ep_high:
            return False

    # SELL の場合: TP1 < EP < SL / For SELL: TP1 < EP < SL
    if sig.side == "sell":
        if sig.tp1 >= sig.ep_high:
            return False
        if sig.sl <= sig.ep_high:
            return False

    # TP の順序確認 / TP order check
    if sig.tp2 is not None and sig.side == "buy" and sig.tp2 <= sig.tp1:
        return False
    if sig.tp3 is not None and sig.tp2 is not None and sig.side == "buy" and sig.tp3 <= sig.tp2:
        return False

    return True


def is_close_signal(text: str) -> bool:
    """
    全クローズシグナルかどうか判定する。
    Determine whether the text is a close-all signal.

    本番では "全決済", "close all", "強制決済" などをキーワードとして判定。
    Production checks for keywords like "全決済", "close all", "強制決済".
    """
    keywords = [
        r'全\s*決\s*済',
        r'close\s+all',
        r'強制\s*決\s*済',
        r'全\s*クローズ',
        r'全\s*建\s*玉',
    ]
    text_lower = text.lower()
    for kw in keywords:
        if re.search(kw, text_lower, re.IGNORECASE):
            return True
    return False


# =============================================================================
# Private helpers / プライベートヘルパー
# =============================================================================

def _extract_coin(text: str) -> Optional[str]:
    """
    テキストからコイン名を抽出する。
    Extract coin name from text.

    "BTC" や "BTCUSDT" を "BTCUSDT" に正規化する。
    Normalizes "BTC" or "BTCUSDT" to "BTCUSDT".
    """
    patterns = [
        r'coin\s*[：:]\s*([A-Za-z0-9]+)',
        r'銘柄\s*[：:]\s*([A-Za-z0-9]+)',
        r'symbol\s*[：:]\s*([A-Za-z0-9]+)',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            coin = m.group(1).upper().strip()
            # "BTC" → "BTCUSDT" に正規化 / Normalize "BTC" → "BTCUSDT"
            if not coin.endswith("USDT") and not coin.endswith("USDC"):
                coin = coin + "USDT"
            return coin
    return None


def _extract_side(text: str) -> Optional[str]:
    """
    売買方向を抽出する。
    Extract trade direction (buy/sell).
    """
    patterns = [
        r'side\s*[：:]\s*(buy|sell)',
        r'建\s*玉\s*[：:]\s*(BUY|SELL|buy|sell|ロング|ショート|long|short)',
        r'\b(BUY|SELL)\b',
    ]
    mapping = {
        "buy": "buy", "sell": "sell",
        "long": "buy", "short": "sell",
        "ロング": "buy", "ショート": "sell",
    }
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = m.group(1).lower()
            return mapping.get(val, val)
    return None


def _extract_entry_price(text: str) -> tuple[Optional[Decimal], Optional[Decimal]]:
    """
    エントリー価格の範囲を抽出する。
    Extract entry price range (low, high).

    "ep: 95000 ~ 96000" や "EP：95000" の形式に対応。
    Supports "ep: 95000 ~ 96000" and "EP：95000" formats.
    """
    # 範囲指定 / Range format: "ep: low ~ high"
    range_patterns = [
        r'ep\s*[：:]\s*([\d,.]+)\s*[~～〜]\s*([\d,.]+)',
        r'EP\s*[：:]\s*([\d,.]+)\s*[~～〜]\s*([\d,.]+)',
        r'エントリー\s*[：:]\s*([\d,.]+)\s*[~～〜]\s*([\d,.]+)',
    ]
    for pat in range_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            low = _to_decimal(m.group(1))
            high = _to_decimal(m.group(2))
            if low and high:
                if low > high:
                    low, high = high, low
                return low, high

    # 単一値 / Single value: "ep: 95000"
    single_patterns = [
        r'ep\s*[：:]\s*([\d,.]+)',
        r'EP\s*[：:]\s*([\d,.]+)',
        r'エントリー\s*[：:]\s*([\d,.]+)',
    ]
    for pat in single_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            price = _to_decimal(m.group(1))
            if price:
                return price, price

    return None, None


def _extract_decimal(text: str, patterns: list[str]) -> Optional[Decimal]:
    """
    複数パターンで Decimal 値を抽出するユーティリティ。
    Utility to extract a Decimal value using multiple patterns.
    """
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = _to_decimal(m.group(1))
            if val is not None:
                return val
    return None


def _to_decimal(s: str) -> Optional[Decimal]:
    """
    文字列を Decimal に変換する。失敗時は None を返す。
    Convert string to Decimal. Returns None on failure.

    カンマ区切り (e.g. "95,000") も対応。
    Handles comma-separated numbers (e.g. "95,000").
    """
    try:
        return Decimal(s.replace(",", "").strip())
    except InvalidOperation:
        return None
