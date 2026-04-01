#  Trading Bot — Portfolio Sample
#  暗号資産自動売買ボット — ポートフォリオサンプル

## 概要
Pythonで仮想通貨の自動売買Botのサンプルです。  
取引所APIと連携し、売買ロジックを実装する構成になっています。

## 技術スタック
- Python
- REST API
- Bot / 自動化
- 予測にsklearn や Transformer　の学習モデルを使用


## 特徴
- 実運用を想定した構成
- ロジックの拡張が可能

> **注意 / Note**: このコードはポートフォリオ展示用サンプルです。
> 本番環境での使用は自己責任でお願いします。
> This code is a portfolio demonstration sample.
> Use in production at your own risk.


## 本番との主な違い
| 機能 | 本番 | サンプル |
|------|------|----------|
| 取引所 | 3社 | 1社（Bybit） |
| ユーザー | マルチユーザー + Vault暗号化 | シングルユーザー、config.py にハードコード |
| DB | MySQL + NAMED LOCK | SQLite + threading.Lock（WAL モード） |
| シグナル受信 | Telegram Bot（Telethon） | HTTP POST エンドポイント |
| DCA | 多段階 + ショック検知 | 1段階、簡易版 |
| タイミング予測 | sklearn + Transformer | なし（即時発注） |

---

## Overview / 概要

**** は Bybit 取引所向けの暗号資産自動売買ボットです。

- HTTP POST でトレードシグナルを受信
- 指値でのエントリー注文を自動発注
- TP（利確）×3 + SL（損切）注文を自動管理
- DCA（買い下がり）ロジックを内蔵
- FastAPI + スレッドベースの非同期アーキテクチャ

**** is a cryptocurrency auto-trading bot for Bybit exchange.

- Receives trade signals via HTTP POST
- Automatically places limit entry orders
- Manages TP (take-profit) ×3 + SL (stop-loss) orders
- Built-in DCA (Dollar Cost Averaging) logic
- FastAPI + thread-based async architecture

---

## Architecture / アーキテクチャ

### Class Inheritance Chain / クラス継承チェーン

```
Common          設定・ロギング / Config & Logging
  └── Database      SQLite接続管理 / SQLite connection management
        └── TradeDatabase  ドメインDB操作 / Domain DB operations
              └── TradeEngine    注文ライフサイクル / Order lifecycle
```

`SWApp` は `TradeEngine` を Composition で保持し、FastAPI + スレッドを管理。

```
SWApp (has-a TradeEngine)
  ├── FastAPI      HTTP エンドポイント / HTTP endpoints
  ├── Thread[1]    trade-monitor  (ポジション監視 / Position monitoring)
  └── Thread[2]    db-keepalive   (DB生死確認 / DB keepalive)
```

### Exchange Abstraction / 取引所抽象化

```
TradeExchange (ABC)
  └── BybitExchange   CCXT を使った Bybit 実装
                      Bybit implementation via CCXT
```

Strategy + Factory パターンで取引所の実装差異を吸収。

### Data Flow / データフロー

```
POST /signal
    │
    ▼
signal_parser.parse_signal(text)   シグナルテキスト解析
    │
    ▼
TradeEngine.process_signal()       冪等性チェック → DB保存 → エントリー発注
    │
    ▼
[Thread: trade-monitor, 30秒毎 / every 30s]
    │
    ├── state=5  → エントリー約定確認 → TP/SL発注
    │              Check entry fill → Place TP/SL
    │
    ├── state=10 → TP/SL状態確認 → クローズ処理
    │              Check TP/SL status → Close trade
    │
    └── state=10 → DCAトリガー確認 → DCA発注
                   Check DCA trigger → Place DCA order
```

### State Machine / 状態機械

```
0  受信済み / received
5  エントリー発注済み / entry placed
10 オープン（TP/SL発注済み）/ open (TP/SL placed)
12 DCA発動済み / DCA triggered
15 TP約定クローズ / closed by TP
16 SL約定クローズ / closed by SL
17 キャンセル / cancelled
22 エラー / error
```

---

## Key Technical Features / 主要技術的特長

| 技術 / Technology | 詳細 / Details |
|---|---|
| **クラス継承** | 4層チェーン（Common → Database → TradeDatabase → TradeEngine） |
| **取引所抽象化** | ABC + Factory パターン（Strategy pattern） |
| **スレッド安全性** | スレッドローカル DB 接続 + `threading.Lock` |
| **Decimal精度** | 全金融計算を `Decimal`（`float` 未使用）|
| **冪等性** | `signal_id` UNIQUE 制約で重複発注を防止 |
| **レート制限** | トークンバケットアルゴリズム |
| **状態機械** | トレードライフサイクルを整数状態で管理 |
| **WAL モード** | SQLite WAL で並行 R/W 性能を向上 |

---

## Quick Start / クイックスタート

### 1. 依存ライブラリのインストール / Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Bybit テストネット API キーを取得 / Get Bybit testnet API key

1. [https://testnet.bybit.com](https://testnet.bybit.com) でアカウント作成
2. API Management > Create New Key
3. 権限: **Read** + **Trade** を有効化

### 3. `config.py` を編集 / Edit config.py

```python
BYBIT_API_KEY = "your_api_key_here"
BYBIT_API_SECRET = "your_api_secret_here"
BYBIT_TESTNET = True   # テストネット / Testnet
```

### 4. 起動 / Start

```bash
python app.py
```

起動後、以下の URL でアクセス可能 / After startup, access:
- API: `http://127.0.0.1:8000`
- Swagger UI: `http://127.0.0.1:8000/docs`

### 5. シグナルを送信 / Send a signal

```bash
curl -X POST http://localhost:8000/signal \
  -H "Content-Type: application/json" \
  -d '{
    "signal_id": 1,
    "text": "coin: BTCUSDT\nside: buy\nep: 95000 ~ 96000\ntp1: 98000\ntp2: 100000\ntp3: 103000\nsl: 93000"
  }'
```

### 6. トレード状態を確認 / Check trade status

```bash
# 全トレード一覧 / List all trades
curl http://localhost:8000/trades

# 特定トレードの詳細 / Get specific trade
curl http://localhost:8000/trades/1

# 未クローズのみ / Open trades only
curl "http://localhost:8000/trades?open_only=true"
```

---

## Signal Format / シグナルフォーマット

```
coin: BTCUSDT       # 銘柄 / Symbol (BTC も可 / BTC also accepted)
side: buy           # 売買方向 / Direction: buy / sell
ep: 95000 ~ 96000   # エントリー価格範囲 / Entry price range
tp1: 98000          # 利確1 / Take-profit 1
tp2: 100000         # 利確2 / Take-profit 2 (省略可 / optional)
tp3: 103000         # 利確3 / Take-profit 3 (省略可 / optional)
sl: 93000           # 損切 / Stop-loss
```

日本語フォーマットにも対応 / Japanese format also supported:

```
銘柄：BTC
建玉：BUY
EP：95000 ～ 96000
利確1：98000
利確2：100000
利確3：103000
損切：93000
```

---

## File Structure / ファイル構成

| ファイル / File | 行数 / LOC | 役割 / Role |
|---|---|---|
| `config.py` | ~80 | 設定・パラメータ / Config & parameters |
| `models.py` | ~80 | データクラス / Data classes |
| `signal_parser.py` | ~120 | シグナル解析 / Signal parsing |
| `database.py` | ~320 | DB管理（3層）/ DB management (3 layers) |
| `exchange.py` | ~380 | 取引所ABC+Bybit / Exchange ABC + Bybit |
| `trade_engine.py` | ~520 | トレードロジック / Trade logic |
| `app.py` | ~420 | FastAPI + スレッド / FastAPI + threads |
| **合計 / Total** | **~1,920** | |

---

## Production System vs. This Sample / 本番システムとの違い

| 機能 / Feature | 本番 / Production | このサンプル / This Sample |
|---|---|---|
| **取引所** | Bitget・Bybit・Phemex（3社）| Bybit のみ |
| **ユーザー** | マルチユーザー + Vault暗号化 | シングルユーザー・config.py |
| **DB** | MySQL + NAMED LOCK | SQLite + threading.Lock |
| **シグナル受信** | Telegram Bot（Telethon）| HTTP POST |
| **DCA** | 多段階 + ショック検知 | 1段階（2%下落で発動）|
| **タイミング予測** | sklearn + Transformer | なし（即時発注）|
| **強制決済回避** | 証拠金監視 + 自動クローズ | なし |
| **トレーリング SL** | 利益確定後に SL 引き上げ | なし |
| **コード規模** | **約17,900行** | **約1,920行** |

本番システムでは HashiCorp Vault による API キー暗号化、MySQL NAMED LOCK による
銘柄単位の排他制御、sklearn + Transformer を使ったエントリータイミング予測など、
プロダクション品質の機能を実装しています。

---

## License / ライセンス

MIT License — ポートフォリオ目的での参照・利用を歓迎します。
Feel free to reference this for portfolio purposes.
