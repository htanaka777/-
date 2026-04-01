# =============================================================================
# database.py — データベース接続管理 + ドメイン操作
# Database connection management and domain-specific operations
#
# 本番対応:
#   Common.py    (設定・ロギング / Config & Logging)
#   DataBase.py  (MySQL接続管理 / MySQL connection management)
#   TradeDB.py   (トレードDB操作 / Trade DB operations)
#
# 本番との主な違い / Main differences from production:
#   - MySQL → SQLite（インフラ不要 / No infrastructure needed）
#   - NAMED LOCK → threading.Lock（簡易化 / Simplified）
#   - exec_write_with_retry の retry はシンプル化
# =============================================================================

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from decimal import Decimal
from typing import Iterator, List, Optional

import config
from models import ParsedSignal, TradeRecord, STATE_RECEIVED


# =============================================================================
# Layer 1: Common — 設定・ロギング
# Config and logging setup
# 本番対応: Common.py (~896行 → ~40行に簡略化)
# =============================================================================
class Common:
    """
    設定・ロギングの基底クラス。
    Base class for configuration and logging.

    本番の Common.py は config.ini の読み込み、メール送信、
    Decimal丸め、タイムゾーン処理なども含む。
    Production Common.py also handles config.ini parsing,
    email alerts, Decimal rounding, and timezone handling.
    """

    def setup_logging(self, name: str = "sw") -> logging.Logger:
        """
        ロガーを取得・設定する。
        Get and configure a logger.

        本番ではユーザーIDとプラットフォームごとにログファイルを分けて管理。
        Production uses separate log files per user ID and platform.
        """
        logger = logging.getLogger(name)
        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(config.LOG_FORMAT)
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            logger.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
        return logger

    @staticmethod
    def dec(value: Optional[str]) -> Optional[Decimal]:
        """
        TEXT カラムから Decimal に変換するヘルパー。
        Helper to convert TEXT column value to Decimal.

        SQLite は DECIMAL 型を持たないため TEXT で保存し、
        読み出し時に Decimal に変換する。
        SQLite has no DECIMAL type, so values are stored as TEXT
        and converted to Decimal on read.
        """
        if value is None:
            return None
        try:
            return Decimal(value)
        except Exception:
            return None


# =============================================================================
# Layer 2: Database — SQLite 接続管理
# SQLite connection management
# 本番対応: DataBase.py (~801行 → ~160行に簡略化)
# =============================================================================
class Database(Common):
    """
    スレッドローカル SQLite 接続管理クラス。
    Thread-local SQLite connection management class.

    本番の DataBase.py は以下を実装:
    - MySQLdb を使った接続プーリング
    - スレッドローカル接続 (threading.local)
    - 接続断時の自動再接続 (2006/2013エラー対応)
    - safe_cursor() コンテキストマネージャ
    - exec_write_with_retry() による書き込みリトライ

    Production DataBase.py implements:
    - Connection pooling via MySQLdb
    - Thread-local connections (threading.local)
    - Auto-reconnect on connection loss (MySQL error 2006/2013)
    - safe_cursor() context manager
    - exec_write_with_retry() for write retries
    """

    _MAX_RETRIES = 3        # 書き込みリトライ回数 / Write retry count
    _RETRY_DELAY = 0.5      # リトライ待機秒 / Retry delay seconds

    def __init__(self, db_path: str) -> None:
        super().__init__()
        self._db_path = db_path
        self._tls = threading.local()   # スレッドローカルストレージ / Thread-local storage

    def _get_conn(self) -> sqlite3.Connection:
        """
        スレッドローカル接続を返す。なければ新規作成する。
        Return thread-local connection, creating one if needed.

        本番では MySQLdb の接続を ping して生死確認し、
        切断されていれば再接続する (DataBase.get_fresh_conn)。
        Production pings the MySQLdb connection and reconnects
        if dropped (DataBase.get_fresh_conn).
        """
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,    # スレッドローカルで管理するため
            )
            conn.row_factory = sqlite3.Row  # カラム名アクセスを可能に / Named column access
            if config.DB_WAL_MODE:
                # WAL モードで並行読み書き性能を向上 / Improve concurrent R/W with WAL mode
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._tls.conn = conn
        return conn

    @contextmanager
    def cursor(self, commit: bool = False) -> Iterator[sqlite3.Cursor]:
        """
        安全なカーソルコンテキストマネージャ。
        Safe cursor context manager with auto commit/rollback.

        本番の DataBase.safe_cursor() に対応。
        Production equivalent: DataBase.safe_cursor().

        Usage:
            with self.cursor(commit=True) as cur:
                cur.execute("INSERT INTO ...", params)
        """
        conn = self._get_conn()
        cur = conn.cursor()
        try:
            yield cur
            if commit:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()

    def execute_write(self, sql: str, params: tuple = (),
                      purpose: str = "") -> int:
        """
        書き込みクエリをリトライ付きで実行する。lastrowid を返す。
        Execute a write query with retry. Returns lastrowid.

        本番の DataBase.exec_write_with_retry() に対応。
        Production equivalent: DataBase.exec_write_with_retry().

        Args:
            sql:     実行する SQL / SQL to execute
            params:  バインドパラメータ / Bind parameters
            purpose: ログ用説明（デバッグ用）/ Description for logging (debug)
        """
        last_exc: Optional[Exception] = None
        for attempt in range(1, self._MAX_RETRIES + 1):
            try:
                with self.cursor(commit=True) as cur:
                    cur.execute(sql, params)
                    return cur.lastrowid or 0
            except sqlite3.OperationalError as e:
                # "database is locked" などの一時エラーはリトライ
                # Retry on transient errors like "database is locked"
                last_exc = e
                log = self.setup_logging()
                log.warning(
                    f"[execute_write] attempt={attempt}/{self._MAX_RETRIES} "
                    f"purpose={purpose!r} error={e}"
                )
                time.sleep(self._RETRY_DELAY * attempt)
        raise RuntimeError(
            f"execute_write failed after {self._MAX_RETRIES} retries: {last_exc}"
        ) from last_exc

    def initialize_schema(self) -> None:
        """
        テーブルが存在しない場合に作成する（初回起動時）。
        Create tables if they do not exist (first run).
        """
        with self.cursor(commit=True) as cur:
            # ------------------------------------------------------------------
            # signals テーブル — 受信シグナルの記録
            # Signal records
            # 本番対応: wait_message + telegram_chat テーブルの役割を統合
            # Production equivalent: combines wait_message + telegram_chat roles
            # ------------------------------------------------------------------
            cur.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id   INTEGER NOT NULL UNIQUE,
                    symbol      TEXT    NOT NULL,
                    side        TEXT    NOT NULL,
                    ep_low      TEXT,
                    ep_high     TEXT,
                    tp1_price   TEXT,
                    tp2_price   TEXT,
                    tp3_price   TEXT,
                    sl_price    TEXT,
                    raw_text    TEXT,
                    created_at  TEXT NOT NULL
                        DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
                )
            """)

            # ------------------------------------------------------------------
            # trades テーブル — トレードのライフサイクル全状態
            # Full trade lifecycle state
            # 本番対応: trade_symbols テーブル
            # Production equivalent: trade_symbols table
            #
            # Decimal値はすべて TEXT で保存（SQLite REAL は IEEE-754 浮動小数点）
            # All Decimal values stored as TEXT (SQLite REAL is IEEE-754 float)
            # ------------------------------------------------------------------
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id       INTEGER NOT NULL
                                        REFERENCES signals(signal_id),
                    symbol          TEXT    NOT NULL,
                    side            TEXT    NOT NULL DEFAULT 'buy',
                    trade_type      INTEGER NOT NULL DEFAULT 2,
                    state           INTEGER NOT NULL DEFAULT 0,

                    -- エントリー / Entry
                    entry_price     TEXT,
                    entry_qty       TEXT,
                    entry_order_id  TEXT,

                    -- 利確 / Take-profit
                    tp1_price       TEXT,
                    tp2_price       TEXT,
                    tp3_price       TEXT,
                    tp1_size        TEXT,
                    tp2_size        TEXT,
                    tp3_size        TEXT,
                    tp1_order_id    TEXT,
                    tp2_order_id    TEXT,
                    tp3_order_id    TEXT,

                    -- 損切 / Stop-loss
                    sl_price        TEXT,
                    sl_size         TEXT,
                    sl_order_id     TEXT,

                    -- DCA / 買い下がり
                    dca_triggered   INTEGER NOT NULL DEFAULT 0,
                    dca_order_id    TEXT,

                    -- 結果 / Outcome
                    close_price     TEXT,
                    realized_pnl    TEXT,

                    created_at  TEXT NOT NULL
                        DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
                    updated_at  TEXT NOT NULL
                        DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
                )
            """)

            # ------------------------------------------------------------------
            # settings テーブル — ランタイムフラグ
            # Runtime flags (key-value store)
            # 本番対応: general テーブル (KEYKBN='RUN', 'ERROR' など)
            # Production equivalent: general table (KEYKBN='RUN', 'ERROR', etc.)
            # ------------------------------------------------------------------
            cur.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key         TEXT PRIMARY KEY,
                    value       TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                        DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
                )
            """)

            # デフォルト設定を挿入 / Insert default settings
            cur.execute("""
                INSERT OR IGNORE INTO settings (key, value) VALUES ('run', '1')
            """)


# =============================================================================
# Layer 3: TradeDatabase — トレードDB操作
# Trade-specific database operations
# 本番対応: TradeDB.py (~1,099行 → ~160行に簡略化)
# =============================================================================
class TradeDatabase(Database):
    """
    トレードドメイン固有のDB操作クラス。
    Trade domain-specific database operations.

    本番の TradeDB.py は以下を実装:
    - load_users() — ユーザー一覧 + Vault での API キー復号
    - fetch_symbol_configs() — 全オープンポジション取得
    - record_order_ids() — TP/SL オーダーID の保存
    - record_state() — 状態遷移の記録
    - exec_write_with_retry() — 書き込みリトライ

    Production TradeDB.py implements:
    - load_users() — User list + API key decryption from HashiCorp Vault
    - fetch_symbol_configs() — Fetch all open positions
    - record_order_ids() — Save TP/SL order IDs
    - record_state() — State transition recording
    """

    def insert_signal(self, sig: ParsedSignal, signal_id: int) -> None:
        """
        受信シグナルを DB に挿入する（冪等）。
        Insert received signal to DB (idempotent via UNIQUE constraint).
        """
        self.execute_write(
            """
            INSERT OR IGNORE INTO signals
                (signal_id, symbol, side, ep_low, ep_high,
                 tp1_price, tp2_price, tp3_price, sl_price, raw_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal_id,
                sig.coin,
                sig.side,
                str(sig.ep_low) if sig.ep_low else None,
                str(sig.ep_high) if sig.ep_high else None,
                str(sig.tp1) if sig.tp1 else None,
                str(sig.tp2) if sig.tp2 else None,
                str(sig.tp3) if sig.tp3 else None,
                str(sig.sl) if sig.sl else None,
                sig.raw_text,
            ),
            purpose="insert_signal",
        )

    def insert_trade(self, rec: TradeRecord) -> int:
        """
        新規トレードレコードを挿入する。trade_id を返す。
        Insert a new trade record. Returns trade_id.
        """
        return self.execute_write(
            """
            INSERT INTO trades (signal_id, symbol, side, trade_type, state,
                                tp1_price, tp2_price, tp3_price, sl_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.signal_id, rec.symbol, rec.side, rec.trade_type, rec.state,
                str(rec.tp1_price) if rec.tp1_price else None,
                str(rec.tp2_price) if rec.tp2_price else None,
                str(rec.tp3_price) if rec.tp3_price else None,
                str(rec.sl_price) if rec.sl_price else None,
            ),
            purpose="insert_trade",
        )

    def fetch_open_trades(self) -> List[TradeRecord]:
        """
        クローズしていないトレードを全件取得する。
        Fetch all trades that are not yet closed (state < 15).

        本番の TradeDB.fetch_symbol_configs() に対応。
        Production equivalent: TradeDB.fetch_symbol_configs().
        """
        with self.cursor() as cur:
            cur.execute("""
                SELECT * FROM trades WHERE state < 15
                ORDER BY created_at ASC
            """)
            rows = cur.fetchall()
        return [self._row_to_trade(row) for row in rows]

    def fetch_trade(self, trade_id: int) -> Optional[TradeRecord]:
        """指定IDのトレードを取得する / Fetch trade by ID."""
        with self.cursor() as cur:
            cur.execute("SELECT * FROM trades WHERE id = ?", (trade_id,))
            row = cur.fetchone()
        return self._row_to_trade(row) if row else None

    def fetch_all_trades(self) -> List[TradeRecord]:
        """全トレードを取得する（管理API用）/ Fetch all trades (for admin API)."""
        with self.cursor() as cur:
            cur.execute("SELECT * FROM trades ORDER BY created_at DESC LIMIT 100")
            rows = cur.fetchall()
        return [self._row_to_trade(row) for row in rows]

    def update_entry(self, trade_id: int, order_id: str,
                     price: Decimal, qty: Decimal) -> None:
        """
        エントリー注文発注後に注文IDと価格を保存する。
        Save entry order ID and price after order placement.

        本番の TradeDB での telegram_chat.BUY_ORDER_ID 更新に対応。
        """
        self.execute_write(
            """
            UPDATE trades SET
                entry_order_id = ?,
                entry_price = ?,
                entry_qty = ?,
                state = ?,
                updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')
            WHERE id = ?
            """,
            (order_id, str(price), str(qty), 5, trade_id),
            purpose="update_entry",
        )

    def update_state(self, trade_id: int, state: int) -> None:
        """
        トレードの状態を更新する。
        Update trade state.

        本番の TradeDB.record_state() に対応。
        Production equivalent: TradeDB.record_state().
        """
        self.execute_write(
            """
            UPDATE trades SET
                state = ?,
                updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')
            WHERE id = ?
            """,
            (state, trade_id),
            purpose="update_state",
        )

    def record_order_ids(self, trade_id: int,
                         tp1_order_id: Optional[str],
                         tp2_order_id: Optional[str],
                         tp3_order_id: Optional[str],
                         sl_order_id: Optional[str],
                         tp1_size: Optional[Decimal] = None,
                         tp2_size: Optional[Decimal] = None,
                         tp3_size: Optional[Decimal] = None,
                         sl_size: Optional[Decimal] = None) -> None:
        """
        TP/SL 注文発注後に注文IDとサイズを保存する。
        Save TP/SL order IDs and sizes after placement.

        本番の TradeDB.record_order_ids() + record_order_sizes() に対応。
        Production equivalent: TradeDB.record_order_ids() + record_order_sizes().
        """
        self.execute_write(
            """
            UPDATE trades SET
                tp1_order_id = ?, tp2_order_id = ?, tp3_order_id = ?,
                sl_order_id = ?,
                tp1_size = ?, tp2_size = ?, tp3_size = ?,
                sl_size = ?,
                state = ?,
                updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')
            WHERE id = ?
            """,
            (
                tp1_order_id, tp2_order_id, tp3_order_id, sl_order_id,
                str(tp1_size) if tp1_size else None,
                str(tp2_size) if tp2_size else None,
                str(tp3_size) if tp3_size else None,
                str(sl_size) if sl_size else None,
                10,  # STATE_OPEN
                trade_id,
            ),
            purpose="record_order_ids",
        )

    def record_dca(self, trade_id: int, dca_order_id: str) -> None:
        """DCA 注文IDを保存する / Save DCA order ID."""
        self.execute_write(
            """
            UPDATE trades SET
                dca_triggered = 1,
                dca_order_id = ?,
                state = 12,
                updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')
            WHERE id = ?
            """,
            (dca_order_id, trade_id),
            purpose="record_dca",
        )

    def close_trade(self, trade_id: int, state: int,
                    close_price: Decimal, realized_pnl: Decimal) -> None:
        """
        トレードをクローズ済みとして記録する。
        Mark a trade as closed and record PnL.

        本番の TradeDB.delete_symbol_record() + telegram_chat 更新に対応。
        Production: TradeDB.delete_symbol_record() + telegram_chat update.
        """
        self.execute_write(
            """
            UPDATE trades SET
                state = ?,
                close_price = ?,
                realized_pnl = ?,
                updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')
            WHERE id = ?
            """,
            (state, str(close_price), str(realized_pnl), trade_id),
            purpose="close_trade",
        )

    def is_running(self) -> bool:
        """
        システム稼働フラグを取得する。
        Get system running flag.

        本番の general テーブル KEYKBN='RUN' に対応。
        Production equivalent: general table KEYKBN='RUN'.
        """
        with self.cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key = 'run'")
            row = cur.fetchone()
        return row is not None and row[0] == "1"

    # --------------------------------------------------------------------------
    # Private helper / プライベートヘルパー
    # --------------------------------------------------------------------------
    @staticmethod
    def _row_to_trade(row: sqlite3.Row) -> TradeRecord:
        """DB行を TradeRecord に変換する / Convert DB row to TradeRecord."""
        d = dict(row)
        return TradeRecord(
            id=d["id"],
            signal_id=d["signal_id"],
            symbol=d["symbol"],
            side=d["side"],
            trade_type=d["trade_type"],
            state=d["state"],
            entry_price=Common.dec(d.get("entry_price")),
            entry_qty=Common.dec(d.get("entry_qty")),
            entry_order_id=d.get("entry_order_id"),
            tp1_price=Common.dec(d.get("tp1_price")),
            tp2_price=Common.dec(d.get("tp2_price")),
            tp3_price=Common.dec(d.get("tp3_price")),
            tp1_size=Common.dec(d.get("tp1_size")),
            tp2_size=Common.dec(d.get("tp2_size")),
            tp3_size=Common.dec(d.get("tp3_size")),
            tp1_order_id=d.get("tp1_order_id"),
            tp2_order_id=d.get("tp2_order_id"),
            tp3_order_id=d.get("tp3_order_id"),
            sl_price=Common.dec(d.get("sl_price")),
            sl_size=Common.dec(d.get("sl_size")),
            sl_order_id=d.get("sl_order_id"),
            dca_triggered=bool(d.get("dca_triggered", 0)),
            dca_order_id=d.get("dca_order_id"),
            close_price=Common.dec(d.get("close_price")),
            realized_pnl=Common.dec(d.get("realized_pnl")),
            created_at=d.get("created_at"),
            updated_at=d.get("updated_at"),
        )
