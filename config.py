# =============================================================================
# config.py — アプリケーション設定
# Configuration for ShiningWish Trading Bot Sample
#
# 本番システムでは config.ini + HashiCorp Vault で管理。
# このサンプルでは config.py に直接記載（デモ用途）。
# In production, secrets are stored in HashiCorp Vault.
# For this sample, they are written directly here for simplicity.
# =============================================================================

# -----------------------------------------------------------------------------
# 取引所認証情報 / Exchange Credentials
# Bybit API キーを設定してください。テストネットで動作確認推奨。
# Set your Bybit API key. Testnet is recommended for initial testing.
# テストネット登録: https://testnet.bybit.com
# -----------------------------------------------------------------------------
BYBIT_API_KEY: str = "YOUR_BYBIT_API_KEY"
BYBIT_API_SECRET: str = "YOUR_BYBIT_API_SECRET"

# True = テストネット（デフォルト）, False = 本番
# True = testnet (default), False = live trading
BYBIT_TESTNET: bool = True

# -----------------------------------------------------------------------------
# トレードパラメータ / Trading Parameters
# -----------------------------------------------------------------------------

# レバレッジ倍率（先物のみ）/ Leverage multiplier (futures only)
LEVERAGE: int = 5

# 1トレードあたりのポジションサイズ（USDT建て）
# Position size per trade in USDT
ORDER_QUANTITY_USDT: float = 20.0

# TP1/TP2/TP3 への数量配分比率
# Quantity allocation ratio for TP1/TP2/TP3
# 合計が 1.0 になること / Must sum to 1.0
TP_RATIOS: list[float] = [0.8, 0.1, 0.1]

# SL 時のクローズ数量比率（1.0 = 全ポジション）
# Quantity ratio for stop-loss close (1.0 = full position)
SL_RATIO: float = 1.0

# 指値注文のエントリー価格として ep_high を使用
# Use ep_high as the limit entry price
# True = 指値注文, False = 成行注文
# True = limit order, False = market order
USE_LIMIT_ENTRY: bool = True

# -----------------------------------------------------------------------------
# DCA（買い下がり）設定 / DCA (Dollar Cost Averaging) Settings
# 本番は多段階 + ショック検知。このサンプルは1段階のみ。
# Production has multi-level DCA with shock detection. This sample has 1 level.
# -----------------------------------------------------------------------------
DCA_ENABLED: bool = True

# エントリー価格から何%下落したら DCA を発動するか
# DCA trigger threshold: % drop below entry price
DCA_DROP_PCT: float = 0.02  # 2%

# DCA 注文のポジションサイズ（USDT建て）
# DCA order size in USDT
DCA_QUANTITY_USDT: float = 10.0

# -----------------------------------------------------------------------------
# データベース / Database
# 本番: MySQL + NAMED LOCK / このサンプル: SQLite + threading.Lock
# Production: MySQL + NAMED LOCK / This sample: SQLite + threading.Lock
# -----------------------------------------------------------------------------
DB_PATH: str = "sw_trading.db"

# SQLite WAL モードを有効化（並行読み書き性能向上）
# Enable SQLite WAL mode for better concurrent read/write performance
DB_WAL_MODE: bool = True

# -----------------------------------------------------------------------------
# サーバー設定 / Server Settings
# -----------------------------------------------------------------------------
API_HOST: str = "127.0.0.1"
API_PORT: int = 8000

# ポジション監視ループの間隔（秒）
# Position monitoring loop interval in seconds
# 本番は 10秒 / Production uses 10 seconds
MONITOR_INTERVAL_SEC: int = 30

# DB キープアライブ間隔（秒）
# DB keepalive ping interval in seconds
DB_KEEPALIVE_INTERVAL_SEC: int = 1800  # 30分

# -----------------------------------------------------------------------------
# レート制限（トークンバケット）/ Rate Limiting (Token Bucket)
# 本番: ユーザーごとに 4 req/sec / Production: 4 req/sec per user
# -----------------------------------------------------------------------------
BUCKET_CAPACITY: float = 10.0   # バケット容量 / Bucket capacity (tokens)
BUCKET_FILL_RATE: float = 4.0   # 補充レート / Fill rate (tokens/second)

# -----------------------------------------------------------------------------
# ロギング / Logging
# -----------------------------------------------------------------------------
LOG_LEVEL: str = "INFO"  # DEBUG / INFO / WARNING / ERROR
LOG_FORMAT: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
