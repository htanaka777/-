# =============================================================================
# trade_engine.py — トレードエンジン（コアロジック）
# Trade engine: order lifecycle and position monitoring
#
# 本番対応: TradeUtil.py (~5,495行 → ~520行に簡略化)
# Production equivalent: TradeUtil.py
#
# 継承チェーン / Inheritance chain:
#   TradeEngine → TradeDatabase → Database → Common
#
# 本番との主な簡略化点 / Main simplifications from production:
#   - シングルユーザー（マルチユーザー対応なし）
#   - DCA は1段階のみ（多段階 + ショック検知なし）
#   - タイミング分類器なし（即時発注）
#   - 証拠金不足チェックなし（強制決済ロジックなし）
# =============================================================================

import logging
import threading
import time
from decimal import ROUND_DOWN, ROUND_UP, Decimal, getcontext
from typing import List, Optional, Tuple

import config
from database import TradeDatabase
from exchange import TradeExchange
from models import (
    ParsedSignal,
    TokenBucket,
    TradeRecord,
    STATE_CANCELLED,
    STATE_CLOSED_SL,
    STATE_CLOSED_TP,
    STATE_DCA,
    STATE_ENTRY_PLACED,
    STATE_ERROR,
    STATE_OPEN,
    STATE_RECEIVED,
)

# 金融計算用高精度 Decimal / High-precision Decimal for finance
getcontext().prec = 28


class TradeEngine(TradeDatabase):
    """
    注文ライフサイクルとポジション監視を担うコアエンジン。
    Core engine handling order lifecycle and position monitoring.

    本番の TradeUtil.py は5,495行。主な機能:
    - 多段階DCA（ショック検知付き）
    - 多ユーザー対応（ユーザーごとのトークンバケット）
    - 証拠金不足検知と強制決済
    - トレーリングSL（利益確定後のSL価格引き上げ）
    - MySQL NAMED LOCK によるシンボル単位の排他制御
    - メール通知
    Production TradeUtil.py is 5,495 lines and includes:
    - Multi-level DCA with shock detection
    - Multi-user support (per-user token buckets)
    - Margin risk detection and forced liquidation avoidance
    - Trailing stop-loss (raise SL as profit increases)
    - MySQL NAMED LOCK for symbol-level mutual exclusion
    - Email notifications

    このサンプルは上記を1段階DCA・シングルユーザーに簡略化。
    This sample simplifies to 1-level DCA and single user.
    """

    def __init__(self, exchange: TradeExchange, db_path: str) -> None:
        super().__init__(db_path)
        self._exchange = exchange
        self.log = self.setup_logging("engine")

        # 監視ループの再入防止ロック / Prevent re-entrant monitor cycles
        self._monitor_lock = threading.Lock()

        # API レート制限 / API rate limiting
        # 本番ではユーザーごとにバケットを保持 / Production: per-user buckets
        self._bucket = TokenBucket(
            capacity=config.BUCKET_CAPACITY,
            fill_rate=config.BUCKET_FILL_RATE,
        )

    # ==========================================================================
    # シグナル処理 / Signal Processing
    # 本番対応: SWApp._process_message()
    # ==========================================================================

    def process_signal(
        self, sig: ParsedSignal, signal_id: int
    ) -> Optional[int]:
        """
        受信シグナルをエンドツーエンドで処理する。
        Process a received trading signal end-to-end.

        本番の SWApp._process_message() に対応。
        本番ではここでタイミング分類器の予測結果も確認する。
        Production equivalent: SWApp._process_message().
        Production also checks timing classifier predictions here.

        Steps:
        1. 冪等性チェック（同一 signal_id の重複を無視）
           Idempotency check (ignore duplicate signal_id)
        2. シグナルを DB に保存 / Save signal to DB
        3. トレードレコードを作成 / Create trade record
        4. エントリー注文を発注 / Place entry order

        Returns:
            trade_id (int) — 作成されたトレードのID / Created trade ID
            None — シグナルをスキップした場合 / When signal is skipped
        """
        self.log.info(
            f"[engine] process_signal signal_id={signal_id} "
            f"symbol={sig.coin} side={sig.side}"
        )

        # 冪等性チェック: 同一 signal_id が既に存在すれば無視
        # Idempotency check: ignore if signal_id already processed
        existing = self._find_trade_by_signal_id(signal_id)
        if existing:
            self.log.info(
                f"[engine] signal_id={signal_id} already processed "
                f"(trade_id={existing.id}), skipping"
            )
            return existing.id

        # システム停止フラグ確認 / Check system stop flag
        if not self.is_running():
            self.log.warning("[engine] system is stopped (settings.run='0'), skipping")
            return None

        # シグナルを DB に保存 / Save signal to DB
        self.insert_signal(sig, signal_id)

        # レバレッジ設定 / Set leverage
        self._exchange.set_leverage(sig.coin, config.LEVERAGE)

        # トレードレコードを作成 / Create trade record
        rec = TradeRecord(
            signal_id=signal_id,
            symbol=sig.coin,
            side=sig.side,
            trade_type=2,           # 先物固定 / Futures (fixed)
            state=STATE_RECEIVED,
            tp1_price=sig.tp1,
            tp2_price=sig.tp2,
            tp3_price=sig.tp3,
            sl_price=sig.sl,
        )
        trade_id = self.insert_trade(rec)
        rec.id = trade_id

        self.log.info(f"[engine] trade_id={trade_id} created")

        # エントリー注文を発注 / Place entry order
        # エントリー価格: ep_high を使用（指値の場合）
        # Entry price: use ep_high (for limit orders)
        entry_price = sig.ep_high or sig.ep_low
        if entry_price is None:
            self.log.error(f"[engine] trade_id={trade_id} no entry price")
            self.update_state(trade_id, STATE_ERROR)
            return trade_id

        # 取引所精度情報を取得 / Fetch symbol precision info
        try:
            info = self._exchange.get_symbol_info(sig.coin)
        except Exception as e:
            self.log.error(f"[engine] get_symbol_info failed: {e}")
            self.update_state(trade_id, STATE_ERROR)
            return trade_id

        # 注文数量を計算 / Calculate order quantity
        qty = self._calc_order_qty(
            price=entry_price,
            usdt_size=Decimal(str(config.ORDER_QUANTITY_USDT)),
            info=info,
        )
        if qty <= 0:
            self.log.error(
                f"[engine] trade_id={trade_id} qty={qty} too small, skipping"
            )
            self.update_state(trade_id, STATE_ERROR)
            return trade_id

        # 価格を取引所精度に丸める / Adjust price to exchange precision
        adjusted_price = self._exchange.adjust_price(
            entry_price, info["price_prec"]
        )

        # エントリー注文を発注 / Place entry order
        self._bucket.consume_blocking()  # レート制限 / Rate limiting
        order_type = "limit" if config.USE_LIMIT_ENTRY else "market"
        order_id = self._exchange.place_order(
            symbol=sig.coin,
            side=sig.side,
            order_type=order_type,
            price=adjusted_price,
            qty=qty,
            reduce_only=False,
        )

        if order_id is None:
            self.log.error(
                f"[engine] trade_id={trade_id} entry order placement failed"
            )
            self.update_state(trade_id, STATE_ERROR)
            return trade_id

        # DB にエントリー注文情報を保存 / Save entry order info to DB
        self.update_entry(trade_id, order_id, adjusted_price, qty)

        self.log.info(
            f"[engine] trade_id={trade_id} entry order placed "
            f"order_id={order_id} price={adjusted_price} qty={qty}"
        )
        return trade_id

    # ==========================================================================
    # 監視ループ / Monitoring Loop
    # 本番対応: TradeUtil の定期監視 + SWApp._trade_mainloop()
    # ==========================================================================

    def run_monitor_cycle(self) -> None:
        """
        ポジション監視ループの1サイクルを実行する。
        Execute one cycle of the position monitoring loop.

        本番の SWApp._trade_mainloop() に対応。
        Production equivalent: SWApp._trade_mainloop().

        threading.Lock で再入を防止。
        threading.Lock prevents re-entrant execution.
        """
        # 前のサイクルがまだ実行中なら本サイクルをスキップ
        # Skip if previous cycle is still running
        if not self._monitor_lock.acquire(blocking=False):
            self.log.debug("[monitor] previous cycle still running, skipping")
            return

        try:
            open_trades = self.fetch_open_trades()
            if not open_trades:
                self.log.debug("[monitor] no open trades")
                return

            self.log.debug(f"[monitor] checking {len(open_trades)} open trade(s)")
            for trade in open_trades:
                try:
                    self._process_open_trade(trade)
                except Exception as e:
                    self.log.exception(
                        f"[monitor] trade_id={trade.id} unexpected error: {e}"
                    )
        finally:
            self._monitor_lock.release()

    def _process_open_trade(self, trade: TradeRecord) -> None:
        """
        単一トレードの状態に応じた処理を行う（状態機械ディスパッチ）。
        State-machine dispatch for a single trade.

        状態機械 / State machine:
            STATE_RECEIVED     (0)  → エントリー発注を試みる（未発注の場合）
            STATE_ENTRY_PLACED (5)  → エントリー約定を確認する
            STATE_OPEN         (10) → TP/SL 監視 + DCA チェック
            STATE_DCA          (12) → TP/SL 監視（DCA注文発注済み）
        """
        state = trade.state
        self.log.debug(
            f"[monitor] trade_id={trade.id} symbol={trade.symbol} state={state}"
        )

        if state == STATE_RECEIVED:
            # 何らかの理由で発注されていない場合は何もしない
            # Do nothing if not yet placed (handled by process_signal)
            pass

        elif state == STATE_ENTRY_PLACED:
            self._check_entry_fill(trade)

        elif state == STATE_OPEN:
            self._check_tp_sl(trade)
            if config.DCA_ENABLED and not trade.dca_triggered:
                self._check_dca_trigger(trade)

        elif state == STATE_DCA:
            # DCA発注後は TP/SL のみ監視 / Monitor only TP/SL after DCA
            self._check_tp_sl(trade)

    def _check_entry_fill(self, trade: TradeRecord) -> None:
        """
        エントリー注文の約定を確認し、約定済みなら TP/SL を発注する。
        Check entry order fill; if filled, place TP/SL orders.

        本番の TradeUtil でのエントリー約定確認ロジックに対応。
        Production equivalent: entry fill check in TradeUtil.
        """
        if not trade.entry_order_id:
            return

        self._bucket.consume_blocking()
        status = self._exchange.get_order_status(trade.symbol, trade.entry_order_id)

        if status == "closed":
            # 約定価格と数量を取得 / Fetch fill price and quantity
            avg_price, filled_qty = self._exchange.get_filled_avg_price(
                trade.symbol, trade.entry_order_id
            )
            if avg_price and filled_qty:
                self.log.info(
                    f"[engine] trade_id={trade.id} entry filled "
                    f"avg_price={avg_price} qty={filled_qty}"
                )
                # DB を更新して TP/SL を発注 / Update DB and place TP/SL
                trade.entry_price = avg_price
                trade.entry_qty = filled_qty
                self._place_tp_sl_orders(trade)

        elif status == "cancelled":
            self.log.warning(
                f"[engine] trade_id={trade.id} entry order cancelled"
            )
            self.update_state(trade.id, STATE_CANCELLED)

        elif status is None:
            self.log.warning(
                f"[engine] trade_id={trade.id} entry order not found"
            )

    def _place_tp_sl_orders(self, trade: TradeRecord) -> None:
        """
        エントリー約定後に TP×3 と SL を発注する。
        Place TP×3 and SL orders after entry fill.

        本番の TradeUtil.NewOCO() に対応。
        本番では TP が指値条件付き注文（plan order）になることもある。
        Production equivalent: TradeUtil.NewOCO().
        Production may use conditional orders (plan orders) for TP.
        """
        if not trade.entry_qty:
            return

        info = self._exchange.get_symbol_info(trade.symbol)
        close_side = "sell" if trade.side == "buy" else "buy"

        # TP1/TP2/TP3 の数量を算出 / Calculate TP1/TP2/TP3 quantities
        tp1_size, tp2_size, tp3_size = self._calc_tp_sizes(
            trade.entry_qty, info
        )
        sl_size = self._exchange.adjust_qty(
            trade.entry_qty, info["qty_step"], info["qty_prec"]
        )

        tp1_order_id = tp2_order_id = tp3_order_id = sl_order_id = None

        # TP1 発注 / Place TP1
        if trade.tp1_price and tp1_size > 0:
            self._bucket.consume_blocking()
            tp1_order_id = self._exchange.place_order(
                symbol=trade.symbol,
                side=close_side,
                order_type="limit",
                price=self._exchange.adjust_price(
                    trade.tp1_price, info["price_prec"]
                ),
                qty=tp1_size,
                reduce_only=True,
            )

        # TP2 発注 / Place TP2
        if trade.tp2_price and tp2_size > 0:
            self._bucket.consume_blocking()
            tp2_order_id = self._exchange.place_order(
                symbol=trade.symbol,
                side=close_side,
                order_type="limit",
                price=self._exchange.adjust_price(
                    trade.tp2_price, info["price_prec"]
                ),
                qty=tp2_size,
                reduce_only=True,
            )

        # TP3 発注 / Place TP3
        if trade.tp3_price and tp3_size > 0:
            self._bucket.consume_blocking()
            tp3_order_id = self._exchange.place_order(
                symbol=trade.symbol,
                side=close_side,
                order_type="limit",
                price=self._exchange.adjust_price(
                    trade.tp3_price, info["price_prec"]
                ),
                qty=tp3_size,
                reduce_only=True,
            )

        # SL 発注（逆指値の成行注文）/ Place SL (stop-market order)
        # 本番では conditional order（plan order）として発注
        # Production places as a conditional order (plan order)
        if trade.sl_price and sl_size > 0:
            self._bucket.consume_blocking()
            # CCXT では stop-market を params で指定
            # In CCXT, stop-market is specified via params
            try:
                ccxt_sym = f"{trade.symbol.replace('USDT', '')}/USDT:USDT"
                response = self._exchange._exchange.create_order(
                    symbol=ccxt_sym,
                    type="market",
                    side=close_side,
                    amount=float(sl_size),
                    params={
                        "stopLoss": {
                            "triggerPrice": float(
                                self._exchange.adjust_price(
                                    trade.sl_price, info["price_prec"]
                                )
                            )
                        },
                        "reduceOnly": True,
                    },
                )
                sl_order_id = response.get("id")
            except Exception as e:
                self.log.error(f"[engine] SL order error trade_id={trade.id}: {e}")

        # DB に注文IDとサイズを保存 / Save order IDs and sizes to DB
        self.record_order_ids(
            trade_id=trade.id,
            tp1_order_id=tp1_order_id,
            tp2_order_id=tp2_order_id,
            tp3_order_id=tp3_order_id,
            sl_order_id=sl_order_id,
            tp1_size=tp1_size,
            tp2_size=tp2_size,
            tp3_size=tp3_size,
            sl_size=sl_size,
        )

        self.log.info(
            f"[engine] trade_id={trade.id} TP/SL placed "
            f"tp1={tp1_order_id} tp2={tp2_order_id} "
            f"tp3={tp3_order_id} sl={sl_order_id}"
        )

    def _check_tp_sl(self, trade: TradeRecord) -> None:
        """
        TP/SL の約定状態を確認し、状態を遷移させる。
        Check TP/SL fill status and transition state accordingly.

        本番の TradeUtil でのオーダー監視ロジックに対応。
        TP が約定したら SL をキャンセル（またはその逆）。
        Production equivalent: order monitoring in TradeUtil.
        Cancel SL if TP fills (or vice versa).
        """
        # TP1 確認 / Check TP1
        if trade.tp1_order_id:
            self._bucket.consume_blocking()
            status = self._exchange.get_order_status(
                trade.symbol, trade.tp1_order_id
            )
            if status == "closed":
                self.log.info(
                    f"[engine] trade_id={trade.id} TP1 filled → closing trade"
                )
                self._handle_tp_fill(trade)
                return

        # SL 確認 / Check SL
        if trade.sl_order_id:
            self._bucket.consume_blocking()
            status = self._exchange.get_order_status(
                trade.symbol, trade.sl_order_id
            )
            if status == "closed":
                self.log.info(
                    f"[engine] trade_id={trade.id} SL triggered → closing trade"
                )
                self._handle_sl_fill(trade)
                return

    def _handle_tp_fill(self, trade: TradeRecord) -> None:
        """
        TP 約定後の処理: SL をキャンセルしてトレードをクローズ。
        After TP fill: cancel SL and close the trade.
        """
        # SL をキャンセル / Cancel SL
        if trade.sl_order_id:
            self._bucket.consume_blocking()
            self._exchange.cancel_order(trade.symbol, trade.sl_order_id)

        # 現在価格を取得して PnL を計算 / Fetch current price and calc PnL
        current_price = self._exchange.get_ticker_price(trade.symbol)
        pnl = self._calc_pnl(trade, current_price)

        self.close_trade(
            trade_id=trade.id,
            state=STATE_CLOSED_TP,
            close_price=current_price,
            realized_pnl=pnl,
        )
        self.log.info(
            f"[engine] trade_id={trade.id} closed by TP "
            f"pnl={pnl} close_price={current_price}"
        )

    def _handle_sl_fill(self, trade: TradeRecord) -> None:
        """
        SL 約定後の処理: TP をキャンセルしてトレードをクローズ。
        After SL fill: cancel TPs and close the trade.
        """
        # 残存 TP 注文をキャンセル / Cancel remaining TP orders
        for order_id in [trade.tp1_order_id, trade.tp2_order_id, trade.tp3_order_id]:
            if order_id:
                self._bucket.consume_blocking()
                self._exchange.cancel_order(trade.symbol, order_id)

        current_price = self._exchange.get_ticker_price(trade.symbol)
        pnl = self._calc_pnl(trade, current_price)

        self.close_trade(
            trade_id=trade.id,
            state=STATE_CLOSED_SL,
            close_price=current_price,
            realized_pnl=pnl,
        )
        self.log.info(
            f"[engine] trade_id={trade.id} closed by SL "
            f"pnl={pnl} close_price={current_price}"
        )

    def _check_dca_trigger(self, trade: TradeRecord) -> None:
        """
        DCA（買い下がり）発動条件を確認する。
        Check DCA (Dollar Cost Averaging) trigger condition.

        エントリー価格から DCA_DROP_PCT 以上下落した場合に DCA 注文を発注。
        Place DCA order if price drops DCA_DROP_PCT below entry.

        本番は多段階DCA（BD_LEVEL で管理）+ ショック検知（急落時はDCA停止）。
        Production: multi-level DCA (managed by BD_LEVEL) + shock detection.
        """
        if not trade.entry_price:
            return

        try:
            self._bucket.consume_blocking()
            current_price = self._exchange.get_ticker_price(trade.symbol)
        except Exception as e:
            self.log.warning(f"[engine] get_ticker_price error: {e}")
            return

        drop_ratio = (trade.entry_price - current_price) / trade.entry_price
        trigger = Decimal(str(config.DCA_DROP_PCT))

        if drop_ratio < trigger:
            return  # まだ DCA 発動条件を満たさない / DCA not triggered yet

        self.log.info(
            f"[engine] trade_id={trade.id} DCA triggered "
            f"entry={trade.entry_price} current={current_price} "
            f"drop={drop_ratio:.2%}"
        )

        # DCA 注文を発注 / Place DCA order
        try:
            info = self._exchange.get_symbol_info(trade.symbol)
        except Exception as e:
            self.log.error(f"[engine] get_symbol_info for DCA failed: {e}")
            return

        dca_qty = self._calc_order_qty(
            price=current_price,
            usdt_size=Decimal(str(config.DCA_QUANTITY_USDT)),
            info=info,
        )
        if dca_qty <= 0:
            return

        adjusted_price = self._exchange.adjust_price(current_price, info["price_prec"])
        self._bucket.consume_blocking()
        dca_order_id = self._exchange.place_order(
            symbol=trade.symbol,
            side=trade.side,
            order_type="limit",
            price=adjusted_price,
            qty=dca_qty,
        )

        if dca_order_id:
            self.record_dca(trade.id, dca_order_id)
            self.log.info(
                f"[engine] trade_id={trade.id} DCA order placed "
                f"order_id={dca_order_id} price={adjusted_price} qty={dca_qty}"
            )

    # ==========================================================================
    # 計算ユーティリティ / Calculation Utilities
    # 本番対応: TradeUtil の各種計算メソッド
    # ==========================================================================

    def _calc_order_qty(
        self,
        price: Decimal,
        usdt_size: Decimal,
        info: dict,
    ) -> Decimal:
        """
        USDT建てポジションサイズからコントラクト数量を計算する。
        Calculate contract quantity from USDT position size.

        本番の TradeUtil での数量計算ロジックに対応。
        Production equivalent: quantity calculation in TradeUtil.

        Decimal で一貫して計算（float 精度誤差を回避）。
        All calculations in Decimal (avoids float precision errors).
        """
        if price <= 0:
            return Decimal("0")
        raw_qty = usdt_size / price
        return self._exchange.adjust_qty(
            raw_qty, info["qty_step"], info["qty_prec"]
        )

    def _calc_tp_sizes(
        self,
        total_qty: Decimal,
        info: dict,
    ) -> Tuple[Decimal, Decimal, Decimal]:
        """
        全数量を TP1/TP2/TP3 の配分比率で分割する。
        Split total quantity into TP1/TP2/TP3 allocations.

        本番の TP_RATIOS 定数と数量分割ロジックに対応。
        Production equivalent: TP_RATIOS constant and quantity splitting.

        端数は TP3 に集約（合計数量の保存）。
        Remainder goes to TP3 (preserves total quantity).
        """
        ratios = config.TP_RATIOS
        qty_step = info["qty_step"]
        qty_prec = info["qty_prec"]

        tp1 = self._exchange.adjust_qty(
            total_qty * Decimal(str(ratios[0])), qty_step, qty_prec
        )
        tp2 = self._exchange.adjust_qty(
            total_qty * Decimal(str(ratios[1])), qty_step, qty_prec
        )
        # 残りをすべて TP3 に割り当て / Assign remainder to TP3
        tp3 = self._exchange.adjust_qty(
            total_qty - tp1 - tp2, qty_step, qty_prec
        )

        return tp1, tp2, tp3

    def _calc_pnl(
        self, trade: TradeRecord, close_price: Decimal
    ) -> Decimal:
        """
        実現損益を計算する（概算）。
        Calculate realized PnL (approximate).

        本番では取引所の約定データから正確な PnL を取得。
        Production fetches exact PnL from exchange fill data.
        """
        if not trade.entry_price or not trade.entry_qty:
            return Decimal("0")

        if trade.side == "buy":
            return (close_price - trade.entry_price) * trade.entry_qty
        else:
            return (trade.entry_price - close_price) * trade.entry_qty

    # ==========================================================================
    # DB ユーティリティ / DB Utilities
    # ==========================================================================

    def _find_trade_by_signal_id(
        self, signal_id: int
    ) -> Optional[TradeRecord]:
        """
        signal_id に対応するトレードを DB から検索する。
        Find trade by signal_id for idempotency check.
        """
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM trades WHERE signal_id = ? LIMIT 1",
                (signal_id,),
            )
            row = cur.fetchone()
        if row:
            return self._row_to_trade(row)
        return None
