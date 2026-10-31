# -*- coding: utf-8 -*-
"""Update broker branch trading data and export statistics.

每日更新券商分點交易數據，從富邦 e-Broker 網站抓取主力進出資料。
此腳本可獨立執行或由 GitHub Actions 調用。

功能：
1. 抓取熱門股票的券商分點買賣超數據
2. 統計各券商分點的績效（勝率、累計獲利）
3. 匯出 JSON 供前端顯示

使用：
  python update_broker.py         # 抓取熱門 20 支股票
  python update_broker.py --top50 # 抓取前 50 支熱門股
  python update_broker.py --all   # 抓取所有上市櫃股票 (很慢)
"""

import argparse
import os
import json
import time
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd

# 數據目錄
DATA_DIR = "data"
BROKER_DATA_DIR = os.path.join(DATA_DIR, "broker")
DOCS_DIR = os.path.join("docs", "data")

# 熱門股票清單（預設抓取）
HOT_STOCKS = [
    # 權值股
    "2330",  # 台積電
    "2317",  # 鴻海
    "2454",  # 聯發科
    "2308",  # 台達電
    "2382",  # 廣達
    "2891",  # 中信金
    "2881",  # 富邦金
    "2882",  # 國泰金
    "2303",  # 聯電
    "3711",  # 日月光投控
    # 熱門股
    "2603",  # 長榮
    "2609",  # 陽明
    "2615",  # 萬海
    "2618",  # 長榮航
    "3037",  # 欣興
    "2345",  # 智邦
    "3006",  # 晶豪科
    "2379",  # 瑞昱
    "3034",  # 聯詠
    "2412",  # 中華電
]

# 目標追蹤券商（部分匹配）
TARGET_BROKERS = [
    "凱基",
    "美林",
    "摩根",
    "高盛",
    "瑞銀",
    "野村",
    "大和",
    "麥格理",
    "元大",
    "富邦",
]


def get_latest_trade_date() -> str:
    """最近一個「官方已公布資料」的交易日（來自 twse_flows.csv）。

    分點資料的日期戳記必須用成交日而非爬取日——凌晨/早盤爬到的是前一交易日
    的盤後資料，用 date.today() 會整批往後錯一天，與券商軟體對不上。
    """
    path = os.path.join(DATA_DIR, "twse_flows.csv")
    try:
        df = pd.read_csv(path, usecols=["date"])
        return str(df["date"].max())
    except Exception:  # noqa: BLE001 — 保底退回今天
        return date.today().isoformat()


def get_active_codes(cap: int = 150) -> list[str]:
    """熱門股 + 近 20 日法人買賣超榜上的活躍股（各榜前 30），去重、上限 cap 檔。

    讓「主力(分點)」的覆蓋範圍跟著法人動能走，而不是只有固定 20 檔。
    """
    codes = list(HOT_STOCKS)
    ranking_files = (
        "top_three_inst_net_20_up.json", "top_three_inst_net_20_down.json",
        "top_foreign_change_20_up.json", "top_foreign_change_20_down.json",
        "top_trust_change_20_up.json", "top_trust_change_20_down.json",
        "top_dealer_change_20_up.json", "top_dealer_change_20_down.json",
    )
    for fname in ranking_files:
        path = os.path.join(DOCS_DIR, fname)
        try:
            with open(path, encoding="utf-8") as f:
                recs = json.load(f)[:30]
        except Exception:  # noqa: BLE001 — 檔案缺漏就跳過
            continue
        for r in recs:
            c = str(r.get("code", ""))
            if c.isdigit() and len(c) == 4:  # 富邦 zco 頁面以 4 碼個股為主
                codes.append(c)

    seen: set[str] = set()
    out = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            out.append(c)
        if len(out) >= cap:
            break
    return out


def get_all_stock_codes(limit: Optional[int] = None) -> list[str]:
    """
    從現有的 flows CSV 中取得所有股票代碼
    
    Args:
        limit: 限制數量，None 表示全部
    
    Returns:
        股票代碼清單
    """
    codes = set()
    
    for csv_file in ["twse_flows.csv", "tpex_flows.csv"]:
        csv_path = os.path.join(DATA_DIR, csv_file)
        if os.path.exists(csv_path):
            try:
                df = pd.read_csv(csv_path)
                if "code" in df.columns:
                    codes.update(df["code"].astype(str).unique())
            except Exception as e:
                print(f"Warning: Could not read {csv_file}: {e}")
    
    # 排序並過濾只保留純數字代碼（排除權證等）
    valid_codes = [c for c in codes if c.isdigit() and len(c) == 4]
    valid_codes.sort()
    
    if limit:
        return valid_codes[:limit]
    return valid_codes


def ensure_dirs():
    """確保必要目錄存在"""
    os.makedirs(BROKER_DATA_DIR, exist_ok=True)
    os.makedirs(DOCS_DIR, exist_ok=True)



def fetch_all_broker_data(stock_codes: list[str], delay: float = 1.5) -> pd.DataFrame:
    """
    抓取多支股票的券商分點數據
    
    Args:
        stock_codes: 股票代碼清單
        delay: 每次請求間隔（秒）
    
    Returns:
        合併的 DataFrame
    """
    from fetch_broker_data import fetch_broker_trading, close_browser
    
    all_data = []
    total = len(stock_codes)
    
    for i, code in enumerate(stock_codes, 1):
        print(f"[{i}/{total}] Fetching broker data for {code}...")
        try:
            df = fetch_broker_trading(code)
            if not df.empty:
                all_data.append(df)
                print(f"  -> Got {len(df)} records")
            else:
                print(f"  -> No data")
        except Exception as e:
            print(f"  -> Error: {e}")
        
        if i < total:
            time.sleep(delay)
    
    close_browser()
    
    if all_data:
        return pd.concat(all_data, ignore_index=True)
    return pd.DataFrame()


def filter_target_brokers(df: pd.DataFrame) -> pd.DataFrame:
    """過濾出目標券商的交易"""
    if df.empty:
        return df
    
    mask = df["broker_name"].apply(
        lambda x: any(target in str(x) for target in TARGET_BROKERS)
    )
    return df[mask].copy()


def aggregate_broker_stats(df: pd.DataFrame) -> pd.DataFrame:
    """
    彙總券商分點統計
    
    Returns:
        DataFrame with columns:
            - broker_name: 券商名稱
            - total_net_vol: 總買賣超張數
            - buy_count: 買超次數
            - sell_count: 賣超次數
            - stocks_traded: 交易股票數
            - avg_net_vol: 平均買賣超
    """
    if df.empty:
        return pd.DataFrame()
    
    stats = df.groupby("broker_name").agg({
        "net_vol": ["sum", "mean"],
        "side": lambda x: (x == "buy").sum(),
        "stock_code": "nunique"
    })
    
    stats.columns = ["total_net_vol", "avg_net_vol", "buy_count", "stocks_traded"]
    stats = stats.reset_index()
    stats["sell_count"] = df.groupby("broker_name").apply(
        lambda x: (x["side"] == "sell").sum(), include_groups=False
    ).values
    
    # 排序：按絕對買賣超量
    stats["abs_net_vol"] = stats["total_net_vol"].abs()
    stats = stats.sort_values("abs_net_vol", ascending=False)
    stats = stats.drop(columns=["abs_net_vol"])
    
    return stats


def aggregate_stock_broker_matrix(df: pd.DataFrame) -> pd.DataFrame:
    """
    建立股票-券商矩陣，顯示每支股票各券商的買賣超
    
    Returns:
        Pivot table: rows=stock_code, columns=broker_name, values=net_vol
    """
    if df.empty:
        return pd.DataFrame()
    
    # 只取目標券商
    target_df = filter_target_brokers(df)
    if target_df.empty:
        return pd.DataFrame()
    
    pivot = target_df.pivot_table(
        index="stock_code",
        columns="broker_name",
        values="net_vol",
        aggfunc="sum",
        fill_value=0
    )
    
    return pivot


def export_broker_ranking(df: pd.DataFrame, output_path: str):
    """匯出券商排名 JSON"""
    if df.empty:
        return
    
    result = {
        "updated": datetime.now().isoformat(),
        "data": df.to_dict(orient="records")
    }
    
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    print(f"Saved broker ranking to {output_path}")


def export_broker_trades(df: pd.DataFrame, output_path: str):
    """匯出今日分點交易明細 JSON"""
    if df.empty:
        return
    
    # 轉換為可序列化格式
    records = df.to_dict(orient="records")
    
    result = {
        "updated": datetime.now().isoformat(),
        "count": len(records),
        "data": records
    }
    
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    print(f"Saved {len(records)} trades to {output_path}")


def export_target_broker_trades(df: pd.DataFrame, output_path: str):
    """匯出目標券商交易 JSON"""
    target_df = filter_target_brokers(df)
    if target_df.empty:
        print("No target broker trades found")
        return
    
    # 按券商和股票分組
    grouped = target_df.groupby("broker_name").apply(
        lambda g: g[["stock_code", "net_vol", "side", "pct", "rank"]].to_dict(orient="records"),
        include_groups=False
    ).to_dict()
    
    result = {
        "updated": datetime.now().isoformat(),
        "brokers": grouped
    }
    
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    print(f"Saved target broker trades to {output_path}")


def export_main_force_latest(df: pd.DataFrame, output_path: str):
    """匯出個股「主力買賣超」彙總 JSON。

    主力 = 該股當日前 15 大買超分點 + 前 15 大賣超分點的買賣超合計（張）。
    正值表示主力群整體站買方。供前端與下游 fetch_main_force_latest() 使用。
    """
    if df.empty:
        return

    rows = []
    for code, g in df.groupby("stock_code"):
        buy_lots = int(g.loc[g["net_vol"] > 0, "net_vol"].sum())
        sell_lots = int(g.loc[g["net_vol"] < 0, "net_vol"].sum())
        rows.append(
            {
                "code": str(code),
                "main_net_lots": buy_lots + sell_lots,
                "buy_lots": buy_lots,
                "sell_lots": sell_lots,
                "n_brokers": int(g["broker_name"].nunique()),
            }
        )

    rows.sort(key=lambda r: r["main_net_lots"], reverse=True)
    result = {
        "updated": datetime.now().isoformat(),
        "date": get_latest_trade_date(),
        "data": rows,
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"Saved main force summary ({len(rows)} stocks) to {output_path}")


def _load_stock_names() -> dict:
    """code -> (name, market)，取自全市場快照（同 repo 內產出）。"""
    path = os.path.join(DOCS_DIR, "stock_three_inst_latest.json")
    try:
        with open(path, encoding="utf-8") as f:
            snap = json.load(f)
        return {r["code"]: (r.get("name", ""), r.get("market", "")) for r in snap}
    except Exception:  # noqa: BLE001
        return {}


def _official_trade_dates() -> list[str]:
    """官方已公布資料的交易日（twse_flows.csv），新到舊。讀不到回傳空 list。"""
    path = os.path.join(DATA_DIR, "twse_flows.csv")
    try:
        d = pd.read_csv(path, usecols=["date"])["date"].astype(str)
        return sorted(d.unique(), reverse=True)
    except Exception:  # noqa: BLE001
        return []


def _window_dates(broker_dates: list[str], official: list[str], w: int) -> list[str]:
    """最新分點資料日往回 w 個官方交易日（新到舊）。

    官方日曆讀不到、或最新分點日不在官方日曆內時，退回舊行為（分點歷史的最近 w 個日期）。
    """
    if not broker_dates:
        return []
    anchor = broker_dates[0]
    if anchor not in official:
        return list(broker_dates[:w])
    return [d for d in official if d <= anchor][:w]


def export_main_force_rankings(history_df: pd.DataFrame, output_path: str):
    """主力(分點) N 日累計買賣超排名，N ∈ {3,5,10,20}。

    主力 = 各股每日前 15 大買超 + 前 15 大賣超分點的買賣超合計（張）。
    僅涵蓋爬取清單（熱門股 + 法人榜活躍股），非全市場——record 帶 coverage 說明。
    """
    if history_df.empty:
        return
    df = history_df.copy()
    df["full_date"] = df["full_date"].astype(str)
    dates = sorted(df["full_date"].unique(), reverse=True)
    names = _load_stock_names()
    official = _official_trade_dates()

    windows_out = {}
    window_meta = {}
    for w in (3, 5, 10, 20):
        # 視窗 = 最新分點資料日往回 w 個「官方交易日」（twse_flows.csv）。
        # 不可用 dates[:w]：分點歷史若有斷層（例如 2026-10-15~10-30 workflow 被停用），
        # dates[:w] 會把斷層前的舊日子拼進來，3 日榜實際橫跨兩週卻仍標「3 日」。
        # 分點資料是每日快照、事後無法回補，所以缺的日子只能如實標出。
        window = _window_dates(dates, official, w)
        recent = set(window)
        missing = sorted(set(window) - set(dates))
        window_meta[str(w)] = {"dates": sorted(window), "missing": missing}
        sub = df[df["full_date"].isin(recent)]
        if sub.empty:
            continue
        agg = sub.groupby("stock_code").agg(
            net_lots=("net_vol", "sum"),
            n_brokers=("broker_name", "nunique"),
            days=("full_date", "nunique"),
        ).reset_index()
        recs = []
        for _, r in agg.iterrows():
            code = str(r["stock_code"])
            name, market = names.get(code, ("", ""))
            recs.append({
                "code": code,
                "name": name,
                "market": market,
                "net_lots": int(r["net_lots"]),
                "n_brokers": int(r["n_brokers"]),
                "days": int(r["days"]),
            })
        recs.sort(key=lambda x: x["net_lots"], reverse=True)
        windows_out[str(w)] = recs

    result = {
        "updated": datetime.now().isoformat(),
        "date": dates[0] if dates else "",
        "coverage": int(df["stock_code"].nunique()),
        "note": "主力=每日前15大買超+前15大賣超分點合計(張)；僅涵蓋爬取清單(熱門股+法人榜活躍股)",
        "windows": windows_out,
        "window_meta": window_meta,
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"Saved main force rankings ({df['stock_code'].nunique()} stocks, "
          f"{len(dates)} days) to {output_path}")


def build_broker_history(new_trades: pd.DataFrame, history_path: str,
                         trade_date: Optional[str] = None) -> pd.DataFrame:
    """
    累積券商歷史交易數據
    
    Args:
        new_trades: 今日新交易數據
        history_path: 歷史數據 CSV 路徑
    
    Returns:
        合併後的歷史數據
    """
    # 以「成交日」標記（最近一個官方已公布交易日），非爬取日
    trade_date = trade_date or get_latest_trade_date()
    new_trades = new_trades.copy()
    new_trades["full_date"] = trade_date

    # 載入歷史數據
    if os.path.exists(history_path):
        history = pd.read_csv(history_path)
        # 移除同一成交日的舊數據（重跑覆蓋）
        history = history[history["full_date"] != trade_date]
        # 合併
        combined = pd.concat([history, new_trades], ignore_index=True)
    else:
        combined = new_trades
    
    # 只保留最近 60 天
    if "full_date" in combined.columns:
        combined["full_date"] = pd.to_datetime(combined["full_date"])
        cutoff = datetime.now() - timedelta(days=60)
        combined = combined[combined["full_date"] >= cutoff]
        combined["full_date"] = combined["full_date"].dt.strftime("%Y-%m-%d")
    
    # 儲存
    combined.to_csv(history_path, index=False, encoding="utf-8-sig")
    
    return combined


def export_broker_trends(history_df: pd.DataFrame, output_path: str):
    """
    匯出券商買賣超趨勢數據供前端繪圖
    
    生成格式：
    {
        "updated": "...",
        "brokers": {
            "摩根大通": [
                {"date": "2024-12-01", "net_vol": 1234, "cumulative": 5678},
                ...
            ]
        }
    }
    """
    if history_df.empty:
        return
    
    # 只處理目標券商
    target_df = filter_target_brokers(history_df)
    if target_df.empty:
        print("No target broker history found")
        return
    
    # 按券商和日期彙總
    daily = target_df.groupby(["broker_name", "full_date"]).agg({
        "net_vol": "sum"
    }).reset_index()
    
    daily = daily.sort_values(["broker_name", "full_date"])
    
    # 計算累計買賣超
    brokers_data = {}
    for broker_name in daily["broker_name"].unique():
        broker_df = daily[daily["broker_name"] == broker_name].copy()
        broker_df = broker_df.sort_values("full_date")
        broker_df["cumulative"] = broker_df["net_vol"].cumsum()
        
        brokers_data[broker_name] = broker_df[["full_date", "net_vol", "cumulative"]].rename(
            columns={"full_date": "date"}
        ).to_dict(orient="records")
    
    result = {
        "updated": datetime.now().isoformat(),
        "brokers": brokers_data
    }
    
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    print(f"Saved broker trends to {output_path}")




def find_missing_broker_dates(history_dates: list[str], official: list[str],
                              max_days: int, not_before: str = "") -> list[str]:
    """分點歷史範圍內、官方有交易但分點缺資料的日子（舊到新，最多取最新 max_days 天）。

    not_before：保留期限（build_broker_history 只留 60 天），更早的補了也會被立刻刪掉。
    """
    have = set(str(d) for d in history_dates)
    if not have:
        return []
    lo, hi = max(min(have), not_before), max(have)
    missing = sorted((d for d in official if lo <= d < hi and d not in have), reverse=True)
    return sorted(missing[:max_days])


def backfill_missing_days(history_path: str, stock_codes: list[str], delay: float,
                          max_days: int, ref_stocks: int, min_coverage: float = 0.8,
                          max_consecutive_empty: int = 20) -> list[str]:
    """回補分點歷史的斷層（例如 workflow 被停用的日子）。回傳成功補上的日期。

    分點來源可用「自設區間」查指定成交日，頁面日期對不上就視為無資料。
    當天抓到的股票數未達今日的 min_coverage，整天不寫入，下次執行再補，
    避免寫進殘缺的一天後被當成「已有資料」而永遠不再重抓。
    """
    from fetch_broker_data import fetch_broker_trading, close_browser

    if max_days <= 0 or not os.path.exists(history_path):
        return []
    hist_dates = pd.read_csv(history_path, usecols=["full_date"])["full_date"].astype(str).unique()
    keep_from = (datetime.now() - timedelta(days=59)).strftime("%Y-%m-%d")
    todo = find_missing_broker_dates(list(hist_dates), _official_trade_dates(), max_days,
                                     not_before=keep_from)
    if not todo:
        print("[BACKFILL] 分點歷史沒有斷層")
        return []
    print(f"[BACKFILL] 分點歷史缺 {len(todo)} 天：{', '.join(todo)}")

    need = max(1, int(ref_stocks * min_coverage))
    done = []
    try:
        for d in todo:
            frames, empty_run = [], 0
            for i, code in enumerate(stock_codes, 1):
                try:
                    df = fetch_broker_trading(code, on_date=d)
                except Exception as e:  # noqa: BLE001
                    print(f"  [{d}] {code} error: {e}")
                    df = pd.DataFrame()
                if df.empty:
                    empty_run += 1
                    if empty_run >= max_consecutive_empty:
                        print(f"  [{d}] 連續 {empty_run} 檔無資料，停止這一天")
                        break
                else:
                    empty_run = 0
                    frames.append(df)
                if i < len(stock_codes):
                    time.sleep(delay)
            got = sum(f["stock_code"].nunique() for f in frames)
            if got < need:
                print(f"  [{d}] 只抓到 {got} 檔（需要 ≥ {need}），不寫入，下次再補")
                continue
            build_broker_history(pd.concat(frames, ignore_index=True), history_path, trade_date=d)
            done.append(d)
            print(f"  [{d}] 補上 {got} 檔")
    finally:
        close_browser()
    return done


def main():
    """主程式"""
    parser = argparse.ArgumentParser(description="更新券商分點交易數據")
    parser.add_argument("--all", action="store_true", help="抓取所有上市櫃股票（約 1700+，需數小時）")
    parser.add_argument("--active", action="store_true",
                        help="抓取熱門股 + 法人榜活躍股（約 100~150 檔，每日更新建議模式）")
    parser.add_argument("--top50", action="store_true", help="抓取前 50 支熱門股")
    parser.add_argument("--top100", action="store_true", help="抓取前 100 支熱門股")
    parser.add_argument("--delay", type=float, default=1.5, help="每次請求間隔秒數（預設 1.5）")
    parser.add_argument("--backfill-days", type=int, default=12,
                        help="自動回補分點歷史斷層的最多天數（0 = 不回補）")
    args = parser.parse_args()
    
    print("=" * 60)
    print("Update Broker Trading Data")
    print(f"Time: {datetime.now().isoformat()}")
    print("=" * 60)
    
    ensure_dirs()
    
    # 決定要抓取的股票清單
    if args.all:
        stock_codes = get_all_stock_codes()
        print(f"\n[MODE] Full crawl: {len(stock_codes)} stocks")
    elif args.active:
        stock_codes = get_active_codes()
        print(f"\n[MODE] Active stocks (hot + 法人榜): {len(stock_codes)} stocks")
    elif args.top100:
        stock_codes = get_all_stock_codes(limit=100)
        print(f"\n[MODE] Top 100 stocks")
    elif args.top50:
        stock_codes = get_all_stock_codes(limit=50)
        print(f"\n[MODE] Top 50 stocks")
    else:
        stock_codes = HOT_STOCKS
        print(f"\n[MODE] Hot stocks: {len(stock_codes)} stocks")
    
    # 1. 抓取分點數據
    print(f"\nFetching broker data for {len(stock_codes)} stocks...")
    all_trades = fetch_all_broker_data(stock_codes, delay=args.delay)
    
    if all_trades.empty:
        print("[WARN] No broker trades fetched, aborting.")
        return
    
    print(f"\nTotal records fetched: {len(all_trades)}")
    
    # 2. 儲存原始數據到 CSV
    today = date.today().isoformat()
    csv_path = os.path.join(BROKER_DATA_DIR, f"broker_trades_{today}.csv")
    all_trades.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"Saved raw data to {csv_path}")
    
    # 3. 統計券商排名
    broker_stats = aggregate_broker_stats(all_trades)
    print(f"\nBroker stats: {len(broker_stats)} brokers")
    
    # 4. 匯出 JSON 供前端使用
    # 4.1 券商排名
    ranking_path = os.path.join(DOCS_DIR, "broker_ranking.json")
    export_broker_ranking(broker_stats, ranking_path)
    
    # 4.2 今日交易明細
    trades_path = os.path.join(DOCS_DIR, "broker_trades_latest.json")
    export_broker_trades(all_trades, trades_path)
    
    # 4.3 目標券商交易
    target_path = os.path.join(DOCS_DIR, "target_broker_trades.json")
    export_target_broker_trades(all_trades, target_path)

    # 4.3b 個股主力買賣超彙總
    main_force_path = os.path.join(DOCS_DIR, "main_force_latest.json")
    export_main_force_latest(all_trades, main_force_path)
    
    # 4.4 累積歷史數據並生成趨勢圖數據
    history_path = os.path.join(BROKER_DATA_DIR, "broker_history.csv")
    history_df = build_broker_history(all_trades, history_path)

    # 4.4b 回補斷層（例如 2026-10-15~10-30 workflow 被 GitHub 停用）
    filled = backfill_missing_days(history_path, stock_codes, delay=args.delay,
                                   max_days=args.backfill_days,
                                   ref_stocks=int(all_trades["stock_code"].nunique()))
    if filled:
        history_df = pd.read_csv(history_path)
    print(f"Broker history: {len(history_df)} records from {history_df['full_date'].nunique()} days")
    
    # 4.5 匯出券商趨勢數據
    trends_path = os.path.join(DOCS_DIR, "broker_trends.json")
    export_broker_trends(history_df, trends_path)

    # 4.6 主力(分點) 3/5/10/20 日累計買賣超排名
    mf_rank_path = os.path.join(DOCS_DIR, "main_force_rankings.json")
    export_main_force_rankings(history_df, mf_rank_path)

    
    # 5. 輸出統計摘要
    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"Stocks crawled: {len(stock_codes)}")
    print(f"Total trades: {len(all_trades)}")
    print(f"Buy side: {len(all_trades[all_trades['side'] == 'buy'])}")
    print(f"Sell side: {len(all_trades[all_trades['side'] == 'sell'])}")
    print(f"Unique brokers: {all_trades['broker_name'].nunique()}")
    
    # 顯示前 10 名買超券商
    print("\nTop 10 Net Buy Brokers:")
    top_buy = broker_stats.head(10)
    for _, row in top_buy.iterrows():
        print(f"  {row['broker_name']}: {row['total_net_vol']:+,} 張")
    
    print("\n[INFO] update_broker.py completed successfully.")


if __name__ == "__main__":
    main()

