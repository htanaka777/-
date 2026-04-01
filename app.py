# =============================================================================
# app.py — アプリケーションエントリーポイント
# Application entry point: FastAPI + daemon threads
#
# 本番対応: SW.py (~1,096行 → ~420行に簡略化)
# Production equivalent: SW.py
#
# スレッドモデル / Thread model:
#   1. trade-monitor  — TradeEngine.run_monitor_cycle() を定期実行
#   2. db-keepalive   — DB 接続の死活確認
#   3. uvicorn        — FastAPI HTTP サーバー（メインスレッド）
#
# 本番のスレッド構成 / Production thread model:
#   1. mysql-keepalive — MySQL 定期 PING
#   2. system-monitor  — メモリ使用量監視 + GC
#   3. trade-main     — 取引メインループ
#   4. uvicorn        — FastAPI サーバー
#
# 本番との主な違い / Main differences from production:
#   - 継承（SWApp is-a TradeUtil）→ コンポジション（SWApp has-a TradeEngine）
#   - Telegram シグナル受信なし → HTTP POST で代替
#   - マルチユーザー処理なし
# =============================================================================

import logging
import signal as sig_module
import sys
import threading
from dataclasses import asdict
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import config
from exchange import create_exchange
from models import ParsedSignal
from signal_parser import is_valid_signal, parse_signal
from trade_engine import TradeEngine


# =============================================================================
# Pydantic リクエスト/レスポンスモデル
# Pydantic request/response models
# 本番の Msg dataclass に対応 (SW.py)
# =============================================================================
class SignalRequest(BaseModel):
    """
    トレードシグナルの HTTP POST ボディ。
    HTTP POST body for trading signals.

    本番では Telegram.py が Telethon 経由でメッセージを受信し、
    SW.py の /message エンドポイントに転送する。
    In production, Telegram.py receives messages via Telethon
    and forwards them to SW.py's /message endpoint.

    このサンプルでは /signal エンドポイントに直接 POST する。
    This sample accepts signals directly via POST to /signal.
    """
    signal_id: int = Field(
        description="冪等性キー（重複防止）/ Idempotency key (prevents duplicate trades)"
    )
    text: str = Field(
        description="シグナルのテキスト / Signal text",
        example=(
            "coin: BTCUSDT\n"
            "side: buy\n"
            "ep: 95000 ~ 96000\n"
            "tp1: 98000\n"
            "tp2: 100000\n"
            "tp3: 103000\n"
            "sl: 93000"
        ),
    )


class SignalResponse(BaseModel):
    ok: bool
    trade_id: Optional[int]
    symbol: Optional[str]
    message: str


class TradeResponse(BaseModel):
    """トレードレコードのレスポンス / Trade record response."""
    id: Optional[int]
    signal_id: int
    symbol: str
    side: str
    state: int
    state_label: str
    entry_price: Optional[str]
    entry_qty: Optional[str]
    tp1_price: Optional[str]
    tp2_price: Optional[str]
    tp3_price: Optional[str]
    sl_price: Optional[str]
    realized_pnl: Optional[str]
    created_at: Optional[str]
    updated_at: Optional[str]


# 状態ラベルマップ / State label map
STATE_LABELS = {
    0:  "受信済み / received",
    5:  "エントリー発注済み / entry placed",
    10: "オープン（TP/SL発注済み）/ open (TP/SL placed)",
    12: "DCA発動済み / DCA triggered",
    15: "TP約定クローズ / closed by TP",
    16: "SL約定クローズ / closed by SL",
    17: "キャンセル / cancelled",
    22: "エラー / error",
}


# =============================================================================
# SWApp — メインアプリケーションクラス
# Main application class
# 本番対応: SWApp(TradeUtil) in SW.py
#
# 本番は TradeUtil を継承して is-a 関係で実装。
# このサンプルは TradeEngine を has-a で保持（コンポジション）。
# コードの明確さのためにコンポジションを採用。
# Production uses inheritance (SWApp is-a TradeUtil).
# This sample uses composition (SWApp has-a TradeEngine) for clarity.
# =============================================================================
class SWApp:
    """
    統合アプリケーション: FastAPI + トレードエンジン + デーモンスレッド。
    Integrated application: FastAPI + trade engine + daemon threads.

    本番の SWApp(TradeUtil) に対応。
    Production equivalent: SWApp(TradeUtil) in SW.py.
    """

    def __init__(self) -> None:
        self.log = logging.getLogger("sw")
        self._setup_logging()
        self._stop = threading.Event()

        # 取引所アダプタを作成 / Create exchange adapter
        # 本番ではユーザーごとに個別のアダプタを作成
        # Production creates separate adapters per user
        exchange = create_exchange(
            api_key=config.BYBIT_API_KEY,
            api_secret=config.BYBIT_API_SECRET,
            testnet=config.BYBIT_TESTNET,
            log=self.log,
        )

        # トレードエンジンを初期化 / Initialize trade engine
        self.engine = TradeEngine(exchange=exchange, db_path=config.DB_PATH)
        self.engine.initialize_schema()

        # FastAPI アプリを作成 / Create FastAPI app
        self.app = FastAPI(
            title="ShiningWish Trading Bot Sample",
            description=(
                "暗号資産自動売買ボット サンプル実装\n\n"
                "Cryptocurrency auto-trading bot - portfolio sample\n\n"
                "本番システム (~17,900行) の簡易版です。\n"
                "Simplified version of the production system (~17,900 lines)."
            ),
            version="1.0.0",
        )
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )
        self._mount_routes()

    def _setup_logging(self) -> None:
        """ルートロガーを設定する / Configure root logger."""
        logging.basicConfig(
            level=getattr(logging, config.LOG_LEVEL, logging.INFO),
            format=config.LOG_FORMAT,
        )

    # ==========================================================================
    # FastAPI エンドポイント / FastAPI Endpoints
    # 本番対応: SWApp._mount_routes() in SW.py
    # ==========================================================================
    def _mount_routes(self) -> None:
        """
        FastAPI エンドポイントを登録する。
        Mount FastAPI endpoints.

        本番のエンドポイント:
        - GET  /health  — ヘルスチェック
        - POST /message — Telegram からのシグナル受信
        このサンプルのエンドポイント:
        - GET  /health         — ヘルスチェック
        - POST /signal         — HTTP POST でのシグナル受信
        - GET  /trades         — トレード一覧
        - GET  /trades/{id}    — トレード詳細
        - POST /stop           — システム停止フラグ
        - POST /start          — システム起動フラグ
        """
        app = self.app

        @app.get(
            "/health",
            tags=["System"],
            summary="ヘルスチェック / Health check",
        )
        def health():
            """
            システムの稼働状態を返す。
            Return system health status.
            """
            return {
                "status": "ok",
                "testnet": config.BYBIT_TESTNET,
                "running": self.engine.is_running(),
                "monitor_interval_sec": config.MONITOR_INTERVAL_SEC,
            }

        @app.post(
            "/signal",
            response_model=SignalResponse,
            tags=["Trading"],
            summary="トレードシグナル受信 / Receive trade signal",
        )
        def receive_signal(req: SignalRequest) -> SignalResponse:
            """
            トレードシグナルを受信し、注文ライフサイクルを開始する。
            Receive a trading signal and begin the order lifecycle.

            本番では Telegram.py が /message に POST する。
            In production, Telegram.py POSTs to /message.

            **冪等性**: 同一 signal_id は一度だけ処理される。
            **Idempotency**: The same signal_id is processed only once.

            シグナル形式 / Signal format:
            ```
            coin: BTCUSDT
            side: buy
            ep: 95000 ~ 96000
            tp1: 98000
            tp2: 100000
            tp3: 103000
            sl: 93000
            ```
            """
            # テキスト解析 / Parse signal text
            parsed: ParsedSignal = parse_signal(req.text)

            # バリデーション / Validation
            if not is_valid_signal(parsed):
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid signal format. Parsed: coin={parsed.coin}, "
                           f"side={parsed.side}, ep_high={parsed.ep_high}, "
                           f"tp1={parsed.tp1}, sl={parsed.sl}",
                )

            # 全クローズシグナルの処理 / Handle close-all signal
            if parsed.is_close_all:
                self.log.info(
                    f"[app] close-all signal received for {parsed.coin}"
                )
                return SignalResponse(
                    ok=True,
                    trade_id=None,
                    symbol=parsed.coin,
                    message=f"Close-all signal for {parsed.coin} acknowledged",
                )

            # シグナル処理をエンジンに委譲 / Delegate to engine
            try:
                trade_id = self.engine.process_signal(parsed, req.signal_id)
            except Exception as e:
                self.log.exception(f"[app] process_signal error: {e}")
                raise HTTPException(
                    status_code=500,
                    detail=f"Internal error: {e}",
                )

            return SignalResponse(
                ok=True,
                trade_id=trade_id,
                symbol=parsed.coin,
                message=(
                    f"Signal accepted. trade_id={trade_id}"
                    if trade_id else "Signal skipped (already processed or system stopped)"
                ),
            )

        @app.get(
            "/trades",
            response_model=list[TradeResponse],
            tags=["Trading"],
            summary="トレード一覧 / List trades",
        )
        def list_trades(open_only: bool = False) -> list[TradeResponse]:
            """
            トレードレコードの一覧を返す。
            Return a list of trade records.

            Args:
                open_only: True の場合、未クローズのトレードのみ返す。
                           If True, return only open (unclosed) trades.
            """
            if open_only:
                trades = self.engine.fetch_open_trades()
            else:
                trades = self.engine.fetch_all_trades()
            return [_to_trade_response(t) for t in trades]

        @app.get(
            "/trades/{trade_id}",
            response_model=TradeResponse,
            tags=["Trading"],
            summary="トレード詳細 / Trade detail",
        )
        def get_trade(trade_id: int) -> TradeResponse:
            """
            指定 ID のトレードを返す。
            Return a single trade by ID.
            """
            trade = self.engine.fetch_trade(trade_id)
            if trade is None:
                raise HTTPException(
                    status_code=404, detail=f"Trade {trade_id} not found"
                )
            return _to_trade_response(trade)

        @app.post(
            "/stop",
            tags=["System"],
            summary="取引停止 / Stop trading",
        )
        def stop_trading():
            """
            新規取引を停止するフラグを設定する。
            Set flag to stop new trades.
            本番の general テーブル KEYKBN='RUN' を '0' に更新する操作に対応。
            Production equivalent: UPDATE general SET DATA='0' WHERE KEYKBN='RUN'.
            """
            self.engine.execute_write(
                "UPDATE settings SET value='0', "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%S','now','localtime') "
                "WHERE key='run'",
                purpose="stop_trading",
            )
            return {"ok": True, "message": "Trading stopped"}

        @app.post(
            "/start",
            tags=["System"],
            summary="取引再開 / Resume trading",
        )
        def start_trading():
            """新規取引を再開するフラグを設定する / Set flag to resume new trades."""
            self.engine.execute_write(
                "UPDATE settings SET value='1', "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%S','now','localtime') "
                "WHERE key='run'",
                purpose="start_trading",
            )
            return {"ok": True, "message": "Trading started"}

    # ==========================================================================
    # デーモンスレッド / Daemon Threads
    # 本番対応: SWApp のスレッド起動処理
    # ==========================================================================

    def _trade_monitor_loop(self) -> None:
        """
        定期的にポジションを監視するバックグラウンドスレッド。
        Background thread: periodic position monitoring.

        本番の SWApp._trade_mainloop() に対応。
        Production equivalent: SWApp._trade_mainloop().

        MONITOR_INTERVAL_SEC ごとに TradeEngine.run_monitor_cycle() を呼び出す。
        Calls TradeEngine.run_monitor_cycle() every MONITOR_INTERVAL_SEC.
        """
        self.log.info("[monitor] thread started")
        while not self._stop.is_set():
            try:
                self.engine.run_monitor_cycle()
            except Exception as e:
                self.log.exception(f"[monitor] cycle error: {e}")
            # 停止シグナルを待機しながらスリープ / Sleep while checking stop signal
            self._stop.wait(timeout=config.MONITOR_INTERVAL_SEC)
        self.log.info("[monitor] thread stopped")

    def _db_keepalive_loop(self) -> None:
        """
        DB 接続の死活確認スレッド。
        Background thread: DB connection keepalive.

        本番の SWApp._mysql_keepalive() に対応。
        Production equivalent: SWApp._mysql_keepalive().

        SQLite の場合は定期的に PRAGMA クエリを実行して接続を維持。
        For SQLite, executes periodic PRAGMA queries to keep connection alive.
        """
        self.log.info("[keepalive] thread started")
        while not self._stop.is_set():
            try:
                with self.engine.cursor() as cur:
                    cur.execute("PRAGMA integrity_check")
                self.log.debug("[keepalive] db ping ok")
            except Exception as e:
                self.log.warning(f"[keepalive] db ping error: {e}")
            self._stop.wait(timeout=config.DB_KEEPALIVE_INTERVAL_SEC)
        self.log.info("[keepalive] thread stopped")

    def _handle_shutdown(self, signum: int, frame) -> None:
        """
        SIGINT/SIGTERM を受信した際のグレースフルシャットダウン。
        Graceful shutdown handler for SIGINT/SIGTERM.
        """
        self.log.info(f"[shutdown] signal {signum} received, stopping...")
        self._stop.set()
        sys.exit(0)

    def start(self) -> None:
        """
        全デーモンスレッドと HTTP サーバーを起動する。
        Start all daemon threads and the HTTP server.

        本番の SWApp.main() に対応。
        Production equivalent: SWApp.main().
        """
        # シグナルハンドラを設定 / Set signal handlers
        sig_module.signal(sig_module.SIGINT, self._handle_shutdown)
        sig_module.signal(sig_module.SIGTERM, self._handle_shutdown)

        # デーモンスレッドを起動 / Start daemon threads
        threads = [
            ("trade-monitor", self._trade_monitor_loop),
            ("db-keepalive", self._db_keepalive_loop),
        ]
        for name, target in threads:
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self.log.info(f"[startup] thread '{name}' started")

        # HTTP サーバーを起動（メインスレッドでブロック）
        # Start HTTP server (blocks main thread)
        self.log.info(
            f"[startup] HTTP server starting on "
            f"http://{config.API_HOST}:{config.API_PORT}"
        )
        self.log.info(
            f"[startup] API docs available at "
            f"http://{config.API_HOST}:{config.API_PORT}/docs"
        )
        uvicorn.run(
            self.app,
            host=config.API_HOST,
            port=config.API_PORT,
            log_level=config.LOG_LEVEL.lower(),
        )


# =============================================================================
# ヘルパー関数 / Helper Functions
# =============================================================================

def _to_trade_response(trade) -> TradeResponse:
    """TradeRecord を TradeResponse に変換する / Convert TradeRecord to TradeResponse."""
    return TradeResponse(
        id=trade.id,
        signal_id=trade.signal_id,
        symbol=trade.symbol,
        side=trade.side,
        state=trade.state,
        state_label=STATE_LABELS.get(trade.state, f"unknown ({trade.state})"),
        entry_price=str(trade.entry_price) if trade.entry_price else None,
        entry_qty=str(trade.entry_qty) if trade.entry_qty else None,
        tp1_price=str(trade.tp1_price) if trade.tp1_price else None,
        tp2_price=str(trade.tp2_price) if trade.tp2_price else None,
        tp3_price=str(trade.tp3_price) if trade.tp3_price else None,
        sl_price=str(trade.sl_price) if trade.sl_price else None,
        realized_pnl=str(trade.realized_pnl) if trade.realized_pnl else None,
        created_at=trade.created_at,
        updated_at=trade.updated_at,
    )


# =============================================================================
# エントリーポイント / Entry Point
# =============================================================================
if __name__ == "__main__":
    print(
        "\n"
        "============================================================\n"
        "  ShiningWish Trading Bot Sample\n"
        "  暗号資産自動売買ボット サンプル\n"
        "------------------------------------------------------------\n"
        f"  Testnet: {config.BYBIT_TESTNET}\n"
        f"  DB: {config.DB_PATH}\n"
        f"  Monitor interval: {config.MONITOR_INTERVAL_SEC}s\n"
        "------------------------------------------------------------\n"
        "  Bybit testnet APIキーを config.py に設定してください。\n"
        "  Set your Bybit testnet API key in config.py.\n"
        "  https://testnet.bybit.com\n"
        "============================================================\n"
    )

    SWApp().start()
