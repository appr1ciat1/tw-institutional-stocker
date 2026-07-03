# -*- coding: utf-8 -*-
"""Update & export Taiwan institutional (三大法人) holdings data.

功能重點：
- 自動抓 TWSE/TPEX 三大法人日交易 + 外資持股；
- 以 inst_baseline.csv 為基準點，校正投信 / 自營商持股；
- 計算三大法人持股比重；
- 計算多視窗變化：5 / 20 / 60 / 120 日；
- 輸出 ranking JSON + 每檔股票時序 JSON。
"""
import json
import os
import csv
import time
from io import StringIO
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from typing import Optional
import math
import ssl
import requests
from requests.adapters import HTTPAdapter
import pandas as pd

from utils_columns import find_col_any, normalize_columns


# ---------- HTTP session（修正 TPEX SSL 憑證問題）----------
# TWSE/TPEX 部分憑證鏈缺少 Subject Key Identifier，OpenSSL 3.x 嚴格模式
# (VERIFY_X509_STRICT) 會直接拒絕，導致 SSLCertVerificationError。
# 這裡保留正常憑證驗證，只關閉過嚴的 STRICT 檢查，讓抓取在各環境都穩定。
class _LenientTLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


def _make_session() -> requests.Session:
    s = requests.Session()
    adapter = _LenientTLSAdapter()
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


SESSION = _make_session()

DATA_DIR = "data"
DOCS_DIR = os.path.join("docs", "data")
TIMESERIES_DIR = os.path.join(DOCS_DIR, "timeseries")
INST_BASELINE_PATH = os.path.join(DATA_DIR, "inst_baseline.csv")

# 變化指標計算窗（含 3/10 供券商慣例對帳，60/120 供長窗研究）
WINDOWS = [3, 5, 10, 20, 60, 120]
# 分類排名輸出窗——與台灣券商 App 慣例一致（3/5/10/20 日買賣超）
CATEGORY_WINDOWS = [3, 5, 10, 20]
# 舊版合成估計指標排名（向下相容既有消費端）
LEGACY_WINDOWS = [5, 20, 60, 120]
FLOW_COLUMNS = ["date", "code", "name", "foreign_net", "trust_net", "dealer_net", "market"]
FOREIGN_COLUMNS = ["date", "code", "name", "market", "total_shares", "foreign_shares", "foreign_ratio"]
# 首次抓取回溯天數 / 逐日抓取間隔秒數，可用環境變數覆寫（歷史重建時放大）
INIT_FETCH_DAYS = int(os.environ.get("TW_INST_INIT_DAYS", "60"))
BACKFILL_LOOKBACK_DAYS = 120
FETCH_SLEEP_TWSE = float(os.environ.get("TW_INST_SLEEP_TWSE", "3.0"))
FETCH_SLEEP_TPEX = float(os.environ.get("TW_INST_SLEEP_TPEX", "1.5"))


# ---------- generic helpers ----------

def ensure_dirs():
    for p in (DATA_DIR, DOCS_DIR, TIMESERIES_DIR):
        os.makedirs(p, exist_ok=True)


def get_taipei_today() -> date:
    tz = ZoneInfo("Asia/Taipei")
    return datetime.now(tz).date()


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5  # 5=Sat, 6=Sun


def get_target_trade_date() -> date:
    """目標交易日：台北 16:35 前一律用「前一個平日」，之後才含當天。

    當日三大法人 / 外資資料約 16:30 收盤後公布。實測 TWSE 在盤中對
    「今天」的 T86 查詢偶爾回傳**部分快照**（例：884/1288 列、內部自洽），
    若照單全收會產生幽靈交易日污染排名。故 16:35 前不抓今天；
    18:00 / 22:00 排程不受影響。
    """
    tz = ZoneInfo("Asia/Taipei")
    now = datetime.now(tz)
    target = now.date()
    if (now.hour, now.minute) < (16, 35):
        target -= timedelta(days=1)
    while is_weekend(target):
        target -= timedelta(days=1)
    return target


def get_last_date_from_csv(path: str):
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path, usecols=["date"])
    if df.empty:
        return None
    return pd.to_datetime(df["date"]).dt.date.max()


def iter_trading_days(start: date, end: date):
    cur = start
    while cur <= end:
        if not is_weekend(cur):
            yield cur
        cur += timedelta(days=1)


def numeric_series(series: pd.Series, to_float: bool = False) -> pd.Series:
    s = series.astype(str)

    # 1. 去掉千分位
    s = s.str.replace(",", "", regex=False)

    # 2. 統一各種 minus / plus 符號
    s = (
        s.str.replace("\u2212", "-", regex=False)  # ‘−’
         .str.replace("\uFF0D", "-", regex=False)  # 全形『－』
         .str.replace("\uFF0B", "+", regex=False)  # 全形『＋』
         .str.strip()
    )

    # 3. 括號負數: (1234) -> -1234
    mask_paren = s.str.match(r"^\([\d\.]+\)$")
    s.loc[mask_paren] = "-" + s.loc[mask_paren].str.strip("()")

    # 4. 純缺值 token -> 0
    missing_tokens = {"", "nan", "NaN", "None", "--"}
    s = s.where(~s.isin(missing_tokens), "0")

    if to_float:
        return pd.to_numeric(s, errors="coerce").fillna(0.0)

    return pd.to_numeric(s, errors="coerce").fillna(0).astype("Int64")


def empty_flows_df() -> pd.DataFrame:
    return pd.DataFrame(columns=FLOW_COLUMNS)


def empty_foreign_df() -> pd.DataFrame:
    return pd.DataFrame(columns=FOREIGN_COLUMNS)


def ensure_columns(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    out = df.copy()
    for col in columns:
        if col not in out.columns:
            out[col] = pd.NA
    return out


def restore_column_from_index(df: pd.DataFrame, col: str) -> pd.DataFrame:
    if col in df.columns:
        return df
    if isinstance(df.index, pd.MultiIndex) and col in df.index.names:
        return df.reset_index(level=col)
    if df.index.name == col:
        return df.reset_index()
    return df


def read_csv_table_with_header(text: str) -> pd.DataFrame:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    rows: list[list[str]] = []
    for line in lines:
        try:
            row = next(csv.reader([line]))
        except csv.Error:
            continue
        rows.append([str(x).replace("\ufeff", "").strip() for x in row])

    if not rows:
        return pd.DataFrame()

    header_idx = 0
    for idx, row in enumerate(rows[:40]):
        joined = "".join(row)
        has_code = ("代號" in joined) or ("證券代號" in joined)
        has_name = ("名稱" in joined) or ("證券名稱" in joined)
        if has_code and has_name:
            header_idx = idx
            break

    header = rows[header_idx]
    width = len(header)
    if width == 0:
        return pd.DataFrame()

    body: list[list[str]] = []
    for row in rows[header_idx + 1:]:
        if not any(str(x).strip() for x in row):
            continue
        if len(row) < width:
            row = row + [""] * (width - len(row))
        elif len(row) > width:
            row = row[:width]
        body.append(row)

    return pd.DataFrame(body, columns=header)


def read_first_html_table(text: str) -> pd.DataFrame:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(text, "html.parser")
    table = soup.find("table")
    if table is None:
        return pd.DataFrame()

    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        rows.append([cell.get_text(" ", strip=True) for cell in cells])

    if not rows:
        return pd.DataFrame()

    header_idx = 0
    for idx, row in enumerate(rows[:20]):
        joined = "".join(row)
        has_code = ("代號" in joined) or ("證券代號" in joined)
        has_name = ("名稱" in joined) or ("證券名稱" in joined)
        if has_code and has_name:
            header_idx = idx
            break

    header = [str(x).strip() for x in rows[header_idx]]
    width = len(header)
    if width == 0:
        return pd.DataFrame()

    body: list[list[str]] = []
    for row in rows[header_idx + 1:]:
        if not any(str(x).strip() for x in row):
            continue
        if len(row) < width:
            row = row + [""] * (width - len(row))
        elif len(row) > width:
            row = row[:width]
        body.append([str(x).strip() for x in row])

    return pd.DataFrame(body, columns=header)


def get_existing_dates(path: str) -> set[date]:
    if not os.path.exists(path):
        return set()
    try:
        df = pd.read_csv(path, usecols=["date"])
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] failed reading date column from {path}: {e}")
        return set()

    if df.empty:
        return set()

    d = pd.to_datetime(df["date"], errors="coerce").dt.date.dropna()
    return set(d.tolist())


def calc_fetch_dates(
    path: str,
    target_date: date,
    init_fetch_days: int = INIT_FETCH_DAYS,
    lookback_days: int = BACKFILL_LOOKBACK_DAYS,
) -> list[date]:
    existing = get_existing_dates(path)

    if not existing:
        start = target_date - timedelta(days=init_fetch_days)
        while is_weekend(start):
            start += timedelta(days=1)
        return list(iter_trading_days(start, target_date))

    last_date = max(existing)
    forward_dates = set(iter_trading_days(last_date + timedelta(days=1), target_date))

    min_existing = min(existing)
    repair_start = max(min_existing, target_date - timedelta(days=lookback_days))
    missing_dates = {d for d in iter_trading_days(repair_start, target_date) if d not in existing}

    return sorted(forward_dates | missing_dates)


# ---------- TWSE: T86 (daily flows) ----------

def fetch_twse_t86(trade_date: date, max_attempts: int = 3) -> pd.DataFrame:
    """三大法人買賣超統計資訊 (T86) for TWSE，含「回應損壞自動重試」。

    實測 TWSE 在連續請求壓力下會回傳部分損壞的 CSV（整批列數值錯位/缺漏，
    2026-07 重建時 190 天中有 33 天出現 11~61 列不平衡；隔幾秒重抓同一天即正常）。
    故以「三大法人合計」逐列驗證，若有不一致列則重試，保留不一致最少的回應。
    """
    best = None
    best_bad = None
    for attempt in range(max_attempts):
        if attempt:
            time.sleep(5.0)
        try:
            out, n_bad = _fetch_twse_t86_once(trade_date)
        except Exception as e:  # noqa: BLE001 — 重試中容忍單次失敗
            if attempt == max_attempts - 1 and best is None:
                raise
            print(f"[WARN] TWSE T86 {trade_date} attempt {attempt + 1} failed: {e}")
            continue
        if n_bad == 0:
            return out
        print(f"[WARN] TWSE T86 {trade_date} attempt {attempt + 1}: {n_bad} inconsistent rows")
        if best_bad is None or n_bad < best_bad:
            best, best_bad = out, n_bad
    if best is None:
        return empty_flows_df()
    print(f"[WARN] TWSE T86 {trade_date}: keeping best attempt with {best_bad} rows dropped")
    return best


def _fetch_twse_t86_once(trade_date: date):
    """單次抓取 + 解析 + 一致性驗證。回傳 (通過驗證的資料, 不一致列數)。

    注意：/fund/T86 是 Big5 編碼，必須用 cp950 解碼，否則欄位會是亂碼。
    """
    datestr = trade_date.strftime("%Y%m%d")
    url = "https://www.twse.com.tw/fund/T86"
    params = {
        "response": "csv",
        "date": datestr,
        "selectType": "ALLBUT0999",
    }
    resp = SESSION.get(url, params=params, timeout=20)

    csv_text = resp.content.decode("cp950", errors="ignore")
    df = pd.read_csv(StringIO(csv_text), header=1)

    df = df.dropna(how="all", axis=0)
    df = df.dropna(how="all", axis=1)
    df = normalize_columns(df)

    if df.empty or len(df.columns) == 0:
        return empty_flows_df(), 0

    code_col = find_col_any(df, ["證券代號"])
    name_col = find_col_any(df, ["證券名稱"])

    col_foreign_ex_net = find_col_any(
        df,
        [
            "外陸資買賣超股數(不含外資自營商)",
            "外資及陸資(不含外資自營商)買賣超股數",
            "外資及陸資買賣超股數(不含外資自營商)",
        ],
    )
    col_foreign_self_net = find_col_any(df, ["外資自營商買賣超股數"])
    col_trust_net = find_col_any(df, ["投信買賣超股數"])
    col_dealer_net = find_col_any(
        df,
        [
            "自營商買賣超股數合計",
            "自營商買賣超股數",
        ],
    )

    df["code"] = df[code_col].astype(str).str.replace("=", "").str.replace('"', "")
    df["code"] = df["code"].str.strip().str.zfill(4)
    df["name"] = df[name_col].astype(str).str.strip()

    foreign_ex = numeric_series(df[col_foreign_ex_net])
    foreign_self = numeric_series(df[col_foreign_self_net])
    trust_net = numeric_series(df[col_trust_net])
    dealer_net = numeric_series(df[col_dealer_net])

    out = pd.DataFrame(
        {
            "date": trade_date,
            "code": df["code"],
            "name": df["name"],
            "foreign_net": (foreign_ex + foreign_self),
            "trust_net": trust_net,
            "dealer_net": dealer_net,
            "market": "TWSE",
        }
    )

    mask = out["code"].str.match(r"^\d{4,5}[A-Z]*$")
    out = out[mask]

    # 完整度下限：正常 T86 全市場 ~1,100-1,300 列。實測 TWSE 對「當日」的
    # 盤中查詢偶爾回傳部分快照（例 884 列且內部自洽），照單全收會產生
    # 幽靈交易日。低於門檻視為無效回應（整日丟棄，之後排程自動補抓）。
    if len(out) < 1000:
        print(f"[WARN] TWSE T86 {trade_date}: only {len(out)} rows (<1000), "
              f"discarding partial snapshot")
        return empty_flows_df(), 0

    # 逐列驗證：外資 + 投信 + 自營商 == 官方「三大法人買賣超股數」。
    # 雙重目的：(1) 防欄位飄移（2026-07 前 dealer 曾因子字串比對抓成
    # 「外資自營商買賣超股數」恆 0 整批寫錯）；(2) 偵測 TWSE 高壓下的損壞回應，
    # 由外層 fetch_twse_t86 重試。
    n_bad = 0
    col_total = find_col_any(df, ["三大法人買賣超股數"], required=False)
    if col_total is not None:
        total = numeric_series(df[col_total])[mask]
        consistent = (out["foreign_net"] + out["trust_net"] + out["dealer_net"]) == total
        n_bad = int((~consistent).sum())
        if n_bad:
            out = out[consistent]

    return out[FLOW_COLUMNS], n_bad


# ---------- TWSE: MI_QFIIS (foreign holdings) ----------

def fetch_twse_mi_qfiis(trade_date: date) -> pd.DataFrame:
    """外資及陸資投資持股統計 (MI_QFIIS) for TWSE.

    若當日查無資料或格式異常，直接回傳空 DataFrame，避免後續 find_col_any 崩潰。
    """
    datestr = trade_date.strftime("%Y%m%d")
    url = "https://www.twse.com.tw/rwd/zh/fund/MI_QFIIS"
    params = {
        "response": "csv",
        "date": datestr,
        "selectType": "ALLBUT0999",
    }
    resp = SESSION.get(url, params=params, timeout=20)

    # TWSE MI_QFIIS is Big5/CP950 encoded, not UTF-8
    csv_text = resp.content.decode("cp950", errors="ignore")

    try:
        df = pd.read_csv(StringIO(csv_text), header=1)
    except Exception:
        return empty_foreign_df()

    df = df.dropna(how="all", axis=0)
    df = df.dropna(how="all", axis=1)
    df = normalize_columns(df)

    if df.empty or len(df.columns) == 0:
        return empty_foreign_df()

    code_col = find_col_any(df, ["證券代號"])
    name_col = find_col_any(df, ["證券名稱"])
    issued_col = find_col_any(df, ["發行股數"])
    foreign_shares_col = find_col_any(df, ["全體外資及陸資持有股數"])
    foreign_ratio_col = find_col_any(df, ["全體外資及陸資持股比率"])

    out = pd.DataFrame()
    out["code"] = df[code_col].astype(str).str.replace("=", "").str.replace('"', "").str.strip().str.zfill(4)
    out["name"] = df[name_col].astype(str).str.strip()

    mask = out["code"].str.match(r"^\d{4,5}[A-Z]*$")
    out = out[mask]

    if out.empty:
        return empty_foreign_df()

    out["total_shares"] = numeric_series(df.loc[mask, issued_col])
    out["foreign_shares"] = numeric_series(df.loc[mask, foreign_shares_col])
    out["foreign_ratio"] = numeric_series(df.loc[mask, foreign_ratio_col], to_float=True)
    out["date"] = trade_date
    out["market"] = "TWSE"

    return out[FOREIGN_COLUMNS]


# ---------- TPEX helpers ----------

def roc_date(d: date) -> str:
    y = d.year - 1911
    return f"{y:03d}/{d.month:02d}/{d.day:02d}"


# ---------- TPEX: 三大法人 daily flows ----------

def _tpex_num(x) -> int:
    try:
        return int(float(str(x).replace(",", "").strip() or 0))
    except ValueError:
        return 0


def fetch_tpex_flows(trade_date: date) -> pd.DataFrame:
    """上櫃股票三大法人買賣明細（含避險表, se=EW）.

    改用 o=json 的結構化輸出。歷史教訓：o=htm 的表格是「兩層表頭」——
    第一層是法人類別（外資及陸資(不含外資自營商)/外資自營商/外資及陸資/投信/
    自營商(自行買賣)/自營商(避險)/自營商），第二層才是各自的 買進/賣出/買賣超。
    先前用單層表頭去對 24 欄的資料列，造成整批錯位：投信欄其實是
    「外資自營商買進股數」、自營商欄其實是「外資及陸資買進股數」，
    使 TPEX 投信/自營商淨額全錯（例：世界 5347 於 2026-06-26 投信實買 10,171,449
    股卻記成 0）。JSON 版欄位索引固定：
      [0]代號 [1]名稱
      [2..4]   外資及陸資(不含外資自營商) 買進/賣出/買賣超
      [5..7]   外資自營商 買進/賣出/買賣超
      [8..10]  外資及陸資(合計) 買進/賣出/買賣超
      [11..13] 投信 買進/賣出/買賣超
      [14..16] 自營商(自行買賣) 買進/賣出/買賣超
      [17..19] 自營商(避險) 買進/賣出/買賣超
      [20..22] 自營商(合計) 買進/賣出/買賣超
      [23]     三大法人買賣超股數合計
    並以「外資合計 + 投信 + 自營商合計 == 三大法人合計」逐列驗證，
    對不上的列直接丟棄並告警，避免再次靜默寫入錯位資料。
    """
    url = "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php"
    params = {
        "d": roc_date(trade_date),
        "l": "zh-tw",
        "o": "json",
        "s": "0",
        "se": "EW",
        "t": "D",
    }
    resp = SESSION.get(url, params=params, timeout=20)
    try:
        payload = resp.json()
    except ValueError:
        print(f"[WARN] TPEX flows: non-JSON response at {trade_date}")
        return empty_flows_df()

    rows = []
    if isinstance(payload.get("tables"), list) and payload["tables"]:
        rows = payload["tables"][0].get("data") or []
    if not rows:
        rows = payload.get("aaData") or []
    if not rows:
        return empty_flows_df()

    records = []
    dropped = 0
    for r in rows:
        if len(r) < 24:
            dropped += 1
            continue
        code = str(r[0]).strip()
        foreign_net = _tpex_num(r[10])
        trust_net = _tpex_num(r[13])
        dealer_net = _tpex_num(r[22])
        total = _tpex_num(r[23])
        if foreign_net + trust_net + dealer_net != total:
            dropped += 1
            continue
        records.append(
            {
                "date": trade_date,
                "code": code.zfill(4),
                "name": str(r[1]).strip(),
                "foreign_net": foreign_net,
                "trust_net": trust_net,
                "dealer_net": dealer_net,
                "market": "TPEX",
            }
        )

    if dropped:
        print(f"[WARN] TPEX flows {trade_date}: dropped {dropped} malformed/inconsistent rows")
    if not records:
        return empty_flows_df()

    out = pd.DataFrame(records)
    mask = out["code"].str.match(r"^\d{4,5}[A-Z]*$")
    out = out[mask]

    # 完整度下限：正常上櫃全市場 ~850-950 列，防部分快照（同 TWSE 幽靈日問題）
    if len(out) < 600:
        print(f"[WARN] TPEX flows {trade_date}: only {len(out)} rows (<600), "
              f"discarding partial snapshot")
        return empty_flows_df()

    return out[FLOW_COLUMNS]


# ---------- TPEX: 外資持股比例 (QFII) ----------

def fetch_tpex_qfii(trade_date: date) -> pd.DataFrame:
    """僑外資及陸資持股統計 (上櫃)."""
    url = "https://www.tpex.org.tw/web/stock/3insti/qfii/qfii_result.php"
    params = {
        "d": roc_date(trade_date),
        "l": "zh-tw",
        "o": "data",
    }
    resp = SESSION.get(url, params=params, timeout=20)
    resp.encoding = "utf-8"
    try:
        df = read_csv_table_with_header(resp.text)
        if df.empty:
            df = pd.read_csv(
                StringIO(resp.text),
                engine="python",
                on_bad_lines="skip",
            )
    except Exception:
        return empty_foreign_df()

    df = df.dropna(how="all", axis=0)
    df = df.dropna(how="all", axis=1)
    df = normalize_columns(df)
    if df.empty or len(df.columns) == 0:
        return empty_foreign_df()

    code_col = find_col_any(df, ["證券代號", "代號"])
    name_col = find_col_any(df, ["證券名稱", "名稱"])
    shares_col = find_col_any(df, ["發行股數"])
    foreign_shares_col = find_col_any(df, ["僑外資及陸資持有股數"])
    foreign_ratio_col = find_col_any(df, ["僑外資及陸資持股比率"])

    out = pd.DataFrame()
    out["code"] = df[code_col].astype(str).str.strip().str.zfill(4)
    out["name"] = df[name_col].astype(str).str.strip()

    mask = out["code"].str.match(r"^\d{4,5}[A-Z]*$")
    out = out[mask]

    if out.empty:
        return empty_foreign_df()

    out["total_shares"] = numeric_series(df.loc[mask, shares_col])
    out["foreign_shares"] = numeric_series(df.loc[mask, foreign_shares_col])
    out["foreign_ratio"] = numeric_series(df.loc[mask, foreign_ratio_col], to_float=True)
    out["date"] = trade_date
    out["market"] = "TPEX"

    return out[FOREIGN_COLUMNS]


# ---------- history append helpers ----------

def append_history(df_new: pd.DataFrame, path: str, key_cols: list[str]) -> pd.DataFrame:
    if df_new.empty:
        if os.path.exists(path):
            return pd.read_csv(path)
        return df_new.copy()

    df_new = ensure_columns(df_new, key_cols)
    df_new = df_new.copy()
    df_new["date"] = pd.to_datetime(df_new["date"], errors="coerce").dt.date
    df_new = df_new.dropna(subset=["date"])

    if os.path.exists(path):
        df_old = pd.read_csv(path)
        df_old = ensure_columns(df_old, key_cols)
        df_old["date"] = pd.to_datetime(df_old["date"], errors="coerce").dt.date
        df_old = df_old.dropna(subset=["date"])
        df_all = pd.concat([df_old, df_new], ignore_index=True)
    else:
        df_all = df_new

    df_all = df_all.drop_duplicates(subset=key_cols).sort_values(["date", "code"])
    df_all.to_csv(path, index=False, date_format="%Y-%m-%d")
    return df_all


# ---------- model: holdings estimation ----------

def build_foreign_master(twse: pd.DataFrame, tpex: pd.DataFrame) -> pd.DataFrame:
    all_df = pd.concat([twse, tpex], ignore_index=True)
    if all_df.empty:
        return all_df
    all_df = restore_column_from_index(all_df, "code")
    all_df = ensure_columns(all_df, ["code", "date"])
    all_df = all_df.dropna(subset=["code", "date"])
    if all_df.empty:
        return all_df
    all_df = all_df.sort_values(["code", "date"])
    all_df["date"] = pd.to_datetime(all_df["date"], errors="coerce").dt.date
    all_df = all_df.dropna(subset=["date"])
    if all_df.empty:
        return all_df
    all_df = (
        all_df.set_index(["code", "date"])
        .sort_index()
        .groupby(level=0)
        .ffill()
        .reset_index()
    )
    return all_df


def build_estimated_holdings(
    flows: pd.DataFrame,
    foreign_master: pd.DataFrame,
    baseline: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """建立三大法人持股估計，支援 baseline 校正。"""
    flows = restore_column_from_index(flows.copy(), "code")
    foreign_master = restore_column_from_index(foreign_master.copy(), "code")

    flows = ensure_columns(flows, ["date", "code", "market", "trust_net", "dealer_net"])
    foreign_master = ensure_columns(
        foreign_master, ["date", "code", "market", "total_shares", "foreign_ratio"]
    )

    flows["date"] = pd.to_datetime(flows["date"], errors="coerce").dt.date
    foreign_master["date"] = pd.to_datetime(foreign_master["date"], errors="coerce").dt.date
    flows = flows.dropna(subset=["date", "code", "market"])
    foreign_master = foreign_master.dropna(subset=["date", "code", "market"])
    if flows.empty:
        return flows

    merged = flows.merge(
        foreign_master[
            [
                "date",
                "code",
                "market",
                "total_shares",
                "foreign_ratio",
            ]
        ],
        on=["date", "code", "market"],
        how="left",
    )

    if baseline is not None and not baseline.empty and "date" in baseline.columns:
        base = restore_column_from_index(baseline.copy(), "code")
        base = ensure_columns(base, ["date", "code", "trust_shares_base", "dealer_shares_base"])
        base["date"] = pd.to_datetime(
            base["date"], format="%Y-%m-%d", errors="coerce"
        )
        base = base.dropna(subset=["date"])
        if not base.empty:
            base["date"] = base["date"].dt.date
            merged = merged.merge(
                base[["date", "code", "trust_shares_base", "dealer_shares_base"]],
                on=["date", "code"],
                how="left",
            )
        else:
            merged["trust_shares_base"] = pd.NA
            merged["dealer_shares_base"] = pd.NA
    else:
        merged["trust_shares_base"] = pd.NA
        merged["dealer_shares_base"] = pd.NA

    merged = restore_column_from_index(merged, "code")
    merged = ensure_columns(
        merged,
        [
            "code",
            "date",
            "trust_net",
            "dealer_net",
            "total_shares",
            "foreign_ratio",
            "trust_shares_base",
            "dealer_shares_base",
        ],
    )
    merged = merged.dropna(subset=["code", "date"])
    if merged.empty:
        return merged

    merged["code"] = merged["code"].astype(str).str.strip()
    merged = merged.sort_values(["code", "date"]).reset_index(drop=True)

    # total_shares 先轉 float，避免後面 replace/where 中 extension array 爆炸
    merged["total_shares"] = pd.to_numeric(
        merged["total_shares"], errors="coerce"
    ).fillna(0.0)
    merged["trust_net"] = pd.to_numeric(merged["trust_net"], errors="coerce").fillna(0.0)
    merged["dealer_net"] = pd.to_numeric(merged["dealer_net"], errors="coerce").fillna(0.0)

    merged["trust_cum"] = merged.groupby("code")["trust_net"].cumsum()
    merged["dealer_cum"] = merged.groupby("code")["dealer_net"].cumsum()

    # baseline 轉數值，避免 NAType
    base_trust = pd.to_numeric(merged["trust_shares_base"], errors="coerce")
    base_dealer = pd.to_numeric(merged["dealer_shares_base"], errors="coerce")

    base_trust_ff = base_trust.groupby(merged["code"]).ffill().fillna(0.0)
    base_dealer_ff = base_dealer.groupby(merged["code"]).ffill().fillna(0.0)

    trust_cum_at_base = (
        merged["trust_cum"]
        .where(base_trust.notna())
        .groupby(merged["code"])
        .ffill()
        .fillna(0.0)
    )
    dealer_cum_at_base = (
        merged["dealer_cum"]
        .where(base_dealer.notna())
        .groupby(merged["code"])
        .ffill()
        .fillna(0.0)
    )

    merged["trust_shares_est"] = base_trust_ff + (merged["trust_cum"] - trust_cum_at_base)
    merged["dealer_shares_est"] = base_dealer_ff + (merged["dealer_cum"] - dealer_cum_at_base)

    # 若沒有任何 baseline，退化為純 cumsum 模型
    no_base_by_code = (
        (base_trust_ff == 0.0) & (base_dealer_ff == 0.0)
    ).groupby(merged["code"]).transform("all")
    merged.loc[no_base_by_code, "trust_shares_est"] = merged.loc[no_base_by_code, "trust_cum"]
    merged.loc[no_base_by_code, "dealer_shares_est"] = merged.loc[no_base_by_code, "dealer_cum"]

    # total_shares 已在前面轉成 float 並 fillna(0.0)
    denom = merged["total_shares"].astype("float64")
    valid = denom > 0.0

    # 先給預設 0，只有有總股數資訊時才算比重
    merged["trust_ratio_est"] = 0.0
    merged["dealer_ratio_est"] = 0.0

    merged.loc[valid, "trust_ratio_est"] = (
            merged.loc[valid, "trust_shares_est"].astype(float) / denom[valid] * 100.0
    )
    merged.loc[valid, "dealer_ratio_est"] = (
            merged.loc[valid, "dealer_shares_est"].astype(float) / denom[valid] * 100.0
    )

    merged["foreign_ratio"] = merged["foreign_ratio"].fillna(0.0)

    merged["three_inst_ratio_est"] = (
            merged["foreign_ratio"] + merged["trust_ratio_est"] + merged["dealer_ratio_est"]
    )
    return merged


def add_change_metrics(merged: pd.DataFrame, windows: list[int]) -> pd.DataFrame:
    """計算各視窗變化指標。

    - three_inst_ratio_change_w：舊版合成指標（向下相容）。
    - foreign_ratio_change_w：官方外資持股比率的 N 日變化（pp），可與官方數據直接對帳。
    - trust_net_sum_w / dealer_net_sum_w：投信 / 自營商 N 日累計買賣超（股）。
      這是官方每日公布的真實買賣超之視窗加總，不含任何持股估計成分。
    """
    merged = restore_column_from_index(merged.copy(), "code")
    merged = ensure_columns(merged, ["code", "date", "three_inst_ratio_est"])
    merged["date"] = pd.to_datetime(merged["date"], errors="coerce").dt.date
    merged = merged.dropna(subset=["date"])
    if merged.empty:
        for w in windows:
            merged[f"three_inst_ratio_change_{w}"] = pd.NA
        return merged

    merged["code"] = merged["code"].astype(str).str.strip()
    if (merged["code"] == "").all():
        for w in windows:
            merged[f"three_inst_ratio_change_{w}"] = pd.NA
        return merged

    for col in ("three_inst_ratio_est", "foreign_ratio",
                "foreign_net", "trust_net", "dealer_net"):
        if col in merged.columns:
            merged[col] = pd.to_numeric(merged[col], errors="coerce").fillna(0.0)
        else:
            merged[col] = 0.0

    merged = merged.sort_values(["code", "date"]).reset_index(drop=True)

    # 外資比率：官方資料偶有單日缺漏（被 fillna 成 0），0→NaN 後組內 ffill，
    # 避免 diff 出現「-30pp 隔日 +30pp」的假尖峰。
    fr = merged["foreign_ratio"].mask(merged["foreign_ratio"] <= 0.0)
    merged["_foreign_ratio_ff"] = fr.groupby(merged["code"]).ffill()
    # 三大法人單日合計買賣超（＝官方「三大法人買賣超股數」，抓取時已逐列驗證）
    merged["_three_net"] = merged["foreign_net"] + merged["trust_net"] + merged["dealer_net"]

    grouped = merged.groupby("code")
    for w in windows:
        merged[f"three_inst_ratio_change_{w}"] = grouped["three_inst_ratio_est"].diff(periods=w)
        merged[f"foreign_ratio_change_{w}"] = grouped["_foreign_ratio_ff"].diff(periods=w)
        for cat, src in (("foreign", "foreign_net"), ("trust", "trust_net"),
                         ("dealer", "dealer_net"), ("three", "_three_net")):
            merged[f"{cat}_net_sum_{w}"] = (
                grouped[src].rolling(w, min_periods=w).sum().reset_index(level=0, drop=True)
            )
    merged = merged.drop(columns=["_foreign_ratio_ff"])
    return merged


# ---------- export JSON ----------

def export_change_rankings(
    merged: pd.DataFrame, windows: list[int], out_dir: str = DOCS_DIR
):
    if merged.empty or "date" not in merged.columns:
        return
    latest_date = pd.to_datetime(merged["date"]).dt.date.max()
    if pd.isna(latest_date):
        return
    latest = merged[merged["date"] == latest_date].copy()

    import json
    os.makedirs(out_dir, exist_ok=True)
    date_str = latest_date.isoformat()

    for w in windows:
        col = f"three_inst_ratio_change_{w}"
        if col not in latest.columns:
            continue
        tmp = latest[latest[col].notna()].copy()
        if tmp.empty:
            continue

        # 過濾估計發散的冷門股：three_inst_ratio_est 對流動性低的小型股會
        # 累積發散到數百 %（例：蜜望實 388%），其 N 日變化也跟著暴衝（+135pp），
        # 被 sort desc 推上榜首、把真正的法人買賣超龍頭擠掉。故只保留持股比率
        # 落在 [0,100]、且 N 日變化幅度 <= 40pp 的合理樣本後再排名。
        tmp["three_inst_ratio_est"] = pd.to_numeric(
            tmp["three_inst_ratio_est"], errors="coerce"
        )
        tmp = tmp[
            tmp["three_inst_ratio_est"].between(0.0, 100.0)
            & tmp[col].abs().le(40.0)
        ]
        if tmp.empty:
            continue

        up = tmp.sort_values(col, ascending=False).head(200)
        down = tmp.sort_values(col, ascending=True).head(200)

        def to_dict_list(df: pd.DataFrame):
            cols = ["code", "name", "market", "three_inst_ratio_est", col]
            records = []
            for _, row in df[cols].iterrows():
                records.append(
                    {
                        "code": row["code"],
                        "name": row["name"],
                        "market": row["market"],
                        "three_inst_ratio": float(row["three_inst_ratio_est"]),
                        "change": float(row[col]),
                        "date": date_str,
                    }
                )
            return records

        up_json = to_dict_list(up)
        down_json = to_dict_list(down)

        up_path = os.path.join(out_dir, f"top_three_inst_change_{w}_up.json")
        down_path = os.path.join(out_dir, f"top_three_inst_change_{w}_down.json")

        with open(up_path, "w", encoding="utf-8") as f:
            json.dump(up_json, f, ensure_ascii=False, indent=2)
        with open(down_path, "w", encoding="utf-8") as f:
            json.dump(down_json, f, ensure_ascii=False, indent=2)

def export_category_rankings(
    merged: pd.DataFrame, windows: list[int], out_dir: str = DOCS_DIR
):
    """外資 / 投信 / 自營商 / 三大法人合計 分開輸出排名（2026-07 起）。

    口徑與台灣券商 App 一致：一律依「N 日累計買賣超（股數）」排序——
    這是官方每日公布數據的加總，可與券商軟體逐檔對帳。

    - top_foreign_change_{w}_{up,down}.json：外資買賣超排名。
      另帶 ratio（最新官方外資持股%）與 change（持股比率 N 日變化 pp，
      供舊報表相容顯示）。
    - top_trust_change_{w}_* / top_dealer_change_{w}_*：投信 / 自營商買賣超排名，
      change=pct_cap（佔股本 pp，舊報表以 |change|≤40 防呆）。
    - top_three_inst_net_{w}_*：三大法人「合計」買賣超排名（外資+投信+自營商，
      等於官方「三大法人買賣超股數」欄），並帶三類各自的張數分解。
    """
    if merged.empty or "date" not in merged.columns:
        return
    latest_date = pd.to_datetime(merged["date"]).dt.date.max()
    if pd.isna(latest_date):
        return
    latest = merged[merged["date"] == latest_date].copy()
    os.makedirs(out_dir, exist_ok=True)
    date_str = latest_date.isoformat()
    denom_all = pd.to_numeric(latest.get("total_shares"), errors="coerce")

    def dump(records, fname):
        with open(os.path.join(out_dir, fname), "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)

    def base_record(r, idx, net_col):
        shares = clean_float(r[net_col])
        ts = denom_all.get(idx)
        pct = shares / ts * 100.0 if ts and ts > 0 else 0.0
        return {
            "code": r["code"],
            "name": r["name"],
            "market": r["market"],
            "net_shares": int(shares),
            "net_lots": int(round(shares / 1000.0)),
            "pct_cap": round(clean_float(pct), 4),
        }, pct

    for w in windows:
        # ── 外資：N 日累計買賣超（張）排序；附官方持股% 與其變化 ──
        net_col = f"foreign_net_sum_{w}"
        ratio_chg_col = f"foreign_ratio_change_{w}"
        if net_col in latest.columns:
            tmp = latest[latest[net_col].notna() & (latest[net_col] != 0)].copy()
            if not tmp.empty:
                def foreign_records(df):
                    recs = []
                    for idx, r in df.iterrows():
                        rec, _pct = base_record(r, idx, net_col)
                        ratio = clean_float(r.get("foreign_ratio"))
                        chg = clean_float(r.get(ratio_chg_col))
                        rec["ratio"] = ratio if 0.0 <= ratio <= 100.0 else 0.0
                        rec["change"] = chg if abs(chg) <= 40.0 else 0.0
                        rec["date"] = date_str
                        recs.append(rec)
                    return recs

                dump(foreign_records(tmp.sort_values(net_col, ascending=False).head(200)),
                     f"top_foreign_change_{w}_up.json")
                dump(foreign_records(tmp.sort_values(net_col, ascending=True).head(200)),
                     f"top_foreign_change_{w}_down.json")

        # ── 投信 / 自營商：N 日累計買賣超 ──
        for category in ("trust", "dealer"):
            net_col = f"{category}_net_sum_{w}"
            if net_col not in latest.columns:
                continue
            tmp = latest[latest[net_col].notna() & (latest[net_col] != 0)].copy()
            if tmp.empty:
                continue

            def net_records(df, _net_col=net_col):
                recs = []
                for idx, r in df.iterrows():
                    rec, pct = base_record(r, idx, _net_col)
                    # change 統一為 pp 尺度（=pct_cap），下游報表以
                    # abs(change)>40 防呆、以 "±X.Xpp" 顯示，股數放這裡會整批被濾掉
                    rec["change"] = round(clean_float(pct), 4)
                    rec["date"] = date_str
                    recs.append(rec)
                return recs

            dump(net_records(tmp.sort_values(net_col, ascending=False).head(200)),
                 f"top_{category}_change_{w}_up.json")
            dump(net_records(tmp.sort_values(net_col, ascending=True).head(200)),
                 f"top_{category}_change_{w}_down.json")

        # ── 三大法人合計：N 日累計買賣超（外資+投信+自營商）──
        net_col = f"three_net_sum_{w}"
        if net_col in latest.columns:
            tmp = latest[latest[net_col].notna() & (latest[net_col] != 0)].copy()
            if tmp.empty:
                continue

            def three_records(df, w=w):
                recs = []
                for idx, r in df.iterrows():
                    rec, pct = base_record(r, idx, net_col)
                    rec["foreign_lots"] = int(round(clean_float(r.get(f"foreign_net_sum_{w}")) / 1000.0))
                    rec["trust_lots"] = int(round(clean_float(r.get(f"trust_net_sum_{w}")) / 1000.0))
                    rec["dealer_lots"] = int(round(clean_float(r.get(f"dealer_net_sum_{w}")) / 1000.0))
                    rec["change"] = round(clean_float(pct), 4)
                    rec["date"] = date_str
                    recs.append(rec)
                return recs

            dump(three_records(tmp.sort_values(net_col, ascending=False).head(200)),
                 f"top_three_inst_net_{w}_up.json")
            dump(three_records(tmp.sort_values(net_col, ascending=True).head(200)),
                 f"top_three_inst_net_{w}_down.json")


def clean_float(val, default: float = 0.0) -> float:
    """把 NaN / inf / 非數字 清成 safe float，避免寫出非法 JSON。"""
    if val is None:
        return default
    try:
        f = float(val)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return f


def export_timeseries_by_code(
    merged: pd.DataFrame,
    out_root: str = TIMESERIES_DIR,
    primary_window: int = 20,
):
    os.makedirs(out_root, exist_ok=True)

    merged = restore_column_from_index(merged.copy(), "code")
    merged = ensure_columns(merged, ["code", "date"])
    merged = merged.dropna(subset=["code", "date"])
    if merged.empty:
        return

    merged = merged.sort_values(["code", "date"])
    col_change = f"three_inst_ratio_change_{primary_window}"

    for code, g in merged.groupby("code"):
        records = []
        for _, row in g.iterrows():
            date_str = (
                row["date"].strftime("%Y-%m-%d")
                if not isinstance(row["date"], str)
                else row["date"]
            )

            rec = {
                "date": date_str,
                "code": row.get("code", code),
                "name": row.get("name", ""),
                "market": row.get("market", ""),
                "foreign_ratio": clean_float(row.get("foreign_ratio", 0.0)),
                "trust_ratio": clean_float(row.get("trust_ratio_est", 0.0)),
                "dealer_ratio": clean_float(row.get("dealer_ratio_est", 0.0)),
                "three_inst_ratio": clean_float(row.get("three_inst_ratio_est", 0.0)),
                # 每日官方買賣超（股）——外資/投信/自營商分開檢視用
                "foreign_net": int(clean_float(row.get("foreign_net", 0))),
                "trust_net": int(clean_float(row.get("trust_net", 0))),
                "dealer_net": int(clean_float(row.get("dealer_net", 0))),
            }

            if col_change in g.columns:
                rec[col_change] = clean_float(row.get(col_change, 0.0))

            records.append(rec)

        out_path = os.path.join(out_root, f"{code}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)


# ---------- main orchestration ----------

def main():
    ensure_dirs()

    twse_flows_path = os.path.join(DATA_DIR, "twse_flows.csv")
    tpex_flows_path = os.path.join(DATA_DIR, "tpex_flows.csv")
    twse_foreign_path = os.path.join(DATA_DIR, "twse_foreign.csv")
    tpex_foreign_path = os.path.join(DATA_DIR, "tpex_foreign.csv")

    target_date = get_target_trade_date()
    print(f"[INFO] target trade date (Taipei): {target_date}")

    flow_days_twse = calc_fetch_dates(twse_flows_path, target_date)
    flow_days_tpex = calc_fetch_dates(tpex_flows_path, target_date)
    flow_days_twse_set = set(flow_days_twse)
    flow_days_tpex_set = set(flow_days_tpex)
    flow_days = sorted(flow_days_twse_set | flow_days_tpex_set)

    foreign_days_twse = calc_fetch_dates(twse_foreign_path, target_date)
    foreign_days_tpex = calc_fetch_dates(tpex_foreign_path, target_date)
    foreign_days_twse_set = set(foreign_days_twse)
    foreign_days_tpex_set = set(foreign_days_tpex)
    foreign_days = sorted(foreign_days_twse_set | foreign_days_tpex_set)

    if flow_days:
        print(
            f"[INFO] flows fetch plan: {flow_days[0]} -> {flow_days[-1]} "
            f"(TWSE={len(flow_days_twse_set)}, TPEX={len(flow_days_tpex_set)}, union={len(flow_days)})"
        )
    else:
        print("[INFO] flows fetch plan: no missing/new trade date.")

    if foreign_days:
        print(
            f"[INFO] foreign fetch plan: {foreign_days[0]} -> {foreign_days[-1]} "
            f"(TWSE={len(foreign_days_twse_set)}, TPEX={len(foreign_days_tpex_set)}, union={len(foreign_days)})"
        )
    else:
        print("[INFO] foreign fetch plan: no missing/new trade date.")

    # --- update flows ---
    flows_new_list = []
    for d in flow_days:
        print(f"[INFO] fetching flows for {d} ...")
        if d in flow_days_twse_set:
            try:
                twse_df = fetch_twse_t86(d)
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] TWSE T86 fetch failed at {d}: {e}")
                twse_df = empty_flows_df()
            if not twse_df.empty:
                flows_new_list.append(twse_df)
            if len(flow_days_twse_set) > 1:
                time.sleep(FETCH_SLEEP_TWSE)  # TWSE 對高頻請求會封鎖 IP

        if d in flow_days_tpex_set:
            try:
                tpex_df = fetch_tpex_flows(d)
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] TPEX flows fetch failed at {d}: {e}")
                tpex_df = empty_flows_df()
            if not tpex_df.empty:
                flows_new_list.append(tpex_df)
            if len(flow_days_tpex_set) > 1:
                time.sleep(FETCH_SLEEP_TPEX)

    if flows_new_list:
        flows_new = pd.concat(flows_new_list, ignore_index=True)
        twse_new = flows_new[flows_new["market"] == "TWSE"].copy()
        tpex_new = flows_new[flows_new["market"] == "TPEX"].copy()

        if not twse_new.empty:
            twse_flows_all = append_history(
                twse_new, twse_flows_path, ["date", "code", "market"]
            )
        else:
            twse_flows_all = (
                pd.read_csv(twse_flows_path) if os.path.exists(twse_flows_path) else empty_flows_df()
            )

        if not tpex_new.empty:
            tpex_flows_all = append_history(
                tpex_new, tpex_flows_path, ["date", "code", "market"]
            )
        else:
            tpex_flows_all = (
                pd.read_csv(tpex_flows_path) if os.path.exists(tpex_flows_path) else empty_flows_df()
            )
    else:
        print("[INFO] no new flows fetched.")
        twse_flows_all = (
            pd.read_csv(twse_flows_path) if os.path.exists(twse_flows_path) else empty_flows_df()
        )
        tpex_flows_all = (
            pd.read_csv(tpex_flows_path) if os.path.exists(tpex_flows_path) else empty_flows_df()
        )

    # --- update foreign holdings ---
    foreign_new_list_twse = []
    foreign_new_list_tpex = []

    for d in foreign_days:
        print(f"[INFO] fetching foreign holdings for {d} ...")
        if d in foreign_days_twse_set:
            try:
                twse_f = fetch_twse_mi_qfiis(d)
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] TWSE MI_QFIIS fetch failed at {d}: {e}")
                twse_f = empty_foreign_df()
            if not twse_f.empty:
                foreign_new_list_twse.append(twse_f)
            if len(foreign_days_twse_set) > 1:
                time.sleep(FETCH_SLEEP_TWSE)

        if d in foreign_days_tpex_set:
            try:
                tpex_f = fetch_tpex_qfii(d)
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] TPEX QFII fetch failed at {d}: {e}")
                tpex_f = empty_foreign_df()
            if not tpex_f.empty:
                foreign_new_list_tpex.append(tpex_f)
            if len(foreign_days_tpex_set) > 1:
                time.sleep(FETCH_SLEEP_TPEX)

    if foreign_new_list_twse:
        twse_foreign_new = pd.concat(foreign_new_list_twse, ignore_index=True)
        twse_foreign_all = append_history(
            twse_foreign_new, twse_foreign_path, ["date", "code", "market"]
        )
    else:
        twse_foreign_all = (
            pd.read_csv(twse_foreign_path) if os.path.exists(twse_foreign_path) else empty_foreign_df()
        )

    if foreign_new_list_tpex:
        tpex_foreign_new = pd.concat(foreign_new_list_tpex, ignore_index=True)
        tpex_foreign_all = append_history(
            tpex_foreign_new, tpex_foreign_path, ["date", "code", "market"]
        )
    else:
        tpex_foreign_all = (
            pd.read_csv(tpex_foreign_path) if os.path.exists(tpex_foreign_path) else empty_foreign_df()
        )

    twse_flows_all = ensure_columns(restore_column_from_index(twse_flows_all, "code"), FLOW_COLUMNS)
    tpex_flows_all = ensure_columns(restore_column_from_index(tpex_flows_all, "code"), FLOW_COLUMNS)
    twse_foreign_all = ensure_columns(restore_column_from_index(twse_foreign_all, "code"), FOREIGN_COLUMNS)
    tpex_foreign_all = ensure_columns(restore_column_from_index(tpex_foreign_all, "code"), FOREIGN_COLUMNS)

    if twse_flows_all.empty and tpex_flows_all.empty:
        print("[WARN] no flows history available, aborting model/export.")
        return

    flows_all = pd.concat(
        [df for df in (twse_flows_all, tpex_flows_all) if not df.empty],
        ignore_index=True,
    )
    if twse_foreign_all.empty and tpex_foreign_all.empty:
        print("[WARN] no foreign holdings history available, aborting model/export.")
        return

    foreign_master = build_foreign_master(twse_foreign_all, tpex_foreign_all)
    if foreign_master.empty:
        print("[WARN] foreign_master is empty, aborting model/export.")
        return

    # baseline 校正
    if os.path.exists(INST_BASELINE_PATH):
        baseline_df = pd.read_csv(INST_BASELINE_PATH, comment="#")
        if baseline_df.empty:
            baseline_df = None
    else:
        baseline_df = None

    merged = build_estimated_holdings(flows_all, foreign_master, baseline=baseline_df)
    merged = add_change_metrics(merged, windows=WINDOWS)

    export_change_rankings(merged, windows=LEGACY_WINDOWS, out_dir=DOCS_DIR)
    export_category_rankings(merged, windows=CATEGORY_WINDOWS, out_dir=DOCS_DIR)
    export_timeseries_by_code(merged, out_root=TIMESERIES_DIR, primary_window=20)

    print("[INFO] update_all.py completed successfully.")


if __name__ == "__main__":
    main()
