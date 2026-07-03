# tw_institutional_stocker

台股法人籌碼追蹤（上市 + 上櫃），自動每日更新。外資 / 投信 / 自營商 **分開輸出、分開檢視**，另有券商分點主力進出追蹤。

## 資料語意（重要）

| 類別 | 來源 | 性質 |
|---|---|---|
| 外資持股比率 `foreign_ratio` | TWSE MI_QFIIS / TPEX QFII | **官方數據**，可與券商軟體直接對帳 |
| 外資/投信/自營商 每日買賣超 `*_net` | TWSE T86 / TPEX 3itrade_hedge | **官方數據**（股數），可直接對帳 |
| 投信/自營商「持股比率」 `trust_ratio` / `dealer_ratio` | 模型推估 | 累計買賣超 ÷ 股本的**估計值**（官方不公布投信/自營商每日持股），僅供研究，勿與官方持股比較 |
| 三大法人合計 `three_inst_ratio` | 官方外資% + 上述估計 | 合成估計指標（舊版相容），僅供參考 |

> 2026-07 修正：修復兩個嚴重資料 bug——(1) TWSE T86 自營商買賣超因欄位子字串誤判抓成「外資自營商」（幾乎恆 0）；(2) TPEX 兩層表頭錯位導致投信/自營商淨額整批錯誤。歷史資料已全量重建，並加入「三大法人合計」逐列一致性驗證防止再犯。

## 輸出檔案（docs/data/）

各視窗 w ∈ {5, 20, 60, 120}，方向 ∈ {up, down}：

- `top_foreign_change_{w}_{up,down}.json` — 外資：官方持股比率 N 日變化(pp)排名。record: `{code, name, market, ratio, change, date}`
- `top_trust_change_{w}_{up,down}.json` — 投信：N 日累計買賣超排名。record: `{code, name, market, net_shares, net_lots, pct_cap, change, date}`
- `top_dealer_change_{w}_{up,down}.json` — 自營商：同上格式
- `top_three_inst_change_{w}_{up,down}.json` — 三大法人合計（舊版相容，估計指標）
- `timeseries/{code}.json` — 個股時序，含官方 `foreign_ratio` 與每日 `foreign_net` / `trust_net` / `dealer_net`（股）
- `stock_three_inst_latest.json` — 全市場最新快照
- `broker_*.json` / `target_broker_trades.json` / `main_force_latest.json` — 券商分點主力（富邦 e-Broker，熱門股）

## 結構概覽

- `update_all.py`
  - 抓取：三大法人每日買賣超（上市 T86 CSV + 上櫃 3itrade_hedge **JSON API**）、外資持股統計（MI_QFIIS + QFII）。
  - 資料品質防線：每列驗證「外資+投信+自營商 == 官方三大法人合計」，不一致即丟棄告警；欄位比對採「完全相等優先」。
  - 環境變數：`TW_INST_INIT_DAYS`（首抓回溯天數，預設 60）、`TW_INST_SLEEP_TWSE` / `TW_INST_SLEEP_TPEX`（逐日抓取節流秒數）。
  - 投信/自營商持股估計支援 `data/inst_baseline.csv` 基準點校正（格式見檔內註解；無 baseline 時退化為純 cumsum）。
- `update_broker.py` / `fetch_broker_data.py` — 券商分點主力進出（Playwright 抓富邦 e-Broker）。
- `docs/` — 靜態前端：外資/投信/自營商/合計 排名切換（買超/賣超）、個股持股% + 每日買賣超雙圖、券商分點主力頁。
- `.github/workflows/update.yml` — 每交易日台北 18:00 主跑 + 22:00 補跑，自動 commit + push。

## 本地開發

```bash
pip install -r requirements.txt
python update_all.py
python build_stock_three_inst_latest.py
```

執行完後，`docs/data/` 底下會長出 json 檔，用 `python -m http.server` 打開 `docs/index.html` 即可預覽。

歷史全量重建（修 bug 或換資料源後）：

```bash
rm data/twse_flows.csv data/tpex_flows.csv
TW_INST_INIT_DAYS=300 python update_all.py   # 內建節流，約 20~30 分鐘
```
