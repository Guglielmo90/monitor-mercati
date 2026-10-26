#!/usr/bin/env python3
"""
Agente settimanale mercati
--------------------------
1. Scarica le chiusure giornaliere (Yahoo Finance) di tutti i titoli in config/tickers.yaml
2. Scarica i rendimenti dei titoli di Stato (FRED, BCE)
3. Converte in dati settimanali (chiusura del venerdì)
4. Calcola indicatori e un punteggio 0-100 per ogni titolo, all'interno della sua area
5. Salva:
     data/storico_settimanale.csv           prezzi settimanali di tutti i titoli
     data/report_settimanale.csv            indicatori dell'ultima settimana
     data/archivio/report_AAAA-MM-GG.csv    una fotografia per ogni settimana
     docs/data.js                           dati letti dalla dashboard

Uso:
    python scripts/fetch_data.py            # dati reali
    python scripts/fetch_data.py --demo     # dati simulati (per provare senza internet)

NB: strumento di analisi, non costituisce consulenza finanziaria.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "tickers.yaml"
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
ROME = ZoneInfo("Europe/Rome")

HISTORY_PERIOD = "3y"      # profondità storica scaricata
MIN_SUCCESS_RATIO = 0.5    # sotto questa quota di titoli scaricati non si sovrascrive nulla


# ----------------------------------------------------------------------------
#  Configurazione
# ----------------------------------------------------------------------------
def load_config() -> dict:
    with open(CONFIG, encoding="utf-8") as f:
        return yaml.safe_load(f)


def all_instruments(cfg: dict) -> list[dict]:
    out = []
    for area_key, area in cfg["aree"].items():
        for t in area["titoli"]:
            out.append({**t, "area": area_key})
    return out


def guess_currency(ticker: str) -> str:
    suffix_map = {".MI": "EUR", ".AS": "EUR", ".DE": "EUR", ".PA": "EUR",
                  ".CO": "DKK", ".SW": "CHF", ".L": "GBP"}
    for suf, cur in suffix_map.items():
        if ticker.upper().endswith(suf):
            return cur
    if ticker.upper() == "TTF=F":
        return "EUR"
    return "USD"


# ----------------------------------------------------------------------------
#  Download prezzi (Yahoo Finance)
# ----------------------------------------------------------------------------
def _extract_close(raw: pd.DataFrame, ticker: str) -> pd.Series | None:
    try:
        if isinstance(raw.columns, pd.MultiIndex):
            if ticker in raw.columns.get_level_values(0):
                s = raw[ticker]["Close"]
            else:
                s = raw["Close"][ticker]
        else:
            s = raw["Close"]
        s = s.dropna()
        return s if len(s) > 10 else None
    except Exception:
        return None


def download_prices(tickers: list[str], errors: list[str]) -> pd.DataFrame:
    import yfinance as yf

    closes: dict[str, pd.Series] = {}
    raw = yf.download(tickers, period=HISTORY_PERIOD, interval="1d", auto_adjust=True,
                      group_by="ticker", threads=True, progress=False)
    for t in tickers:
        s = _extract_close(raw, t)
        if s is not None:
            closes[t] = s

    # nuovi tentativi, uno alla volta, per i titoli mancanti (Yahoo a volte limita le richieste)
    missing = [t for t in tickers if t not in closes]
    for attempt in range(1, 3):
        if not missing:
            break
        time.sleep(5 * attempt)
        still = []
        for t in missing:
            try:
                r = yf.download(t, period=HISTORY_PERIOD, interval="1d", auto_adjust=True,
                                progress=False)
                s = _extract_close(r, t)
                if s is not None:
                    closes[t] = s
                else:
                    still.append(t)
            except Exception:
                still.append(t)
            time.sleep(1)
        missing = still

    for t in missing:
        errors.append(f"Prezzi non disponibili su Yahoo Finance per '{t}' (ticker errato o delistato?)")

    df = pd.DataFrame(closes)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df.sort_index()


# ----------------------------------------------------------------------------
#  Download rendimenti (FRED / BCE) – endpoint CSV pubblici, nessuna chiave
# ----------------------------------------------------------------------------
def _fetch_yield(fonte: str, sid: str, headers: dict, timeout: int) -> pd.Series:
    import requests

    if fonte == "fred":
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
        r = requests.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        d = pd.read_csv(io.StringIO(r.text))
        d.columns = ["date", "value"]
    elif fonte == "ecb":
        url = f"https://data-api.ecb.europa.eu/service/data/{sid}?format=csvdata&startPeriod=2020-01-01"
        r = requests.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        d = pd.read_csv(io.StringIO(r.text))[["TIME_PERIOD", "OBS_VALUE"]]
        d.columns = ["date", "value"]
    elif fonte == "yahoo":
        import yfinance as yf
        raw = yf.download(sid, period=HISTORY_PERIOD, interval="1d", progress=False)
        s = _extract_close(raw, sid)
        if s is None:
            raise ValueError("nessun dato")
        d = pd.DataFrame({"date": s.index, "value": s.values})
    else:
        raise ValueError(f"fonte sconosciuta {fonte}")
    d["date"] = pd.to_datetime(d["date"])
    d["value"] = pd.to_numeric(d["value"], errors="coerce")
    s = d.dropna().set_index("date")["value"]
    s.index = s.index.tz_localize(None) if s.index.tz is not None else s.index
    s = s[s.index >= pd.Timestamp.today() - pd.DateOffset(years=3)]
    if s.empty:
        raise ValueError("serie vuota")
    return s


def download_yields(cfg: dict, errors: list[str]) -> dict[str, pd.Series]:
    """Per ogni rendimento prova la fonte principale e poi le alternative (campo 'alternative')."""
    out = {}
    headers = {"User-Agent": "Mozilla/5.0 (weekly-market-dashboard)"}
    down: set[str] = set()   # fonti che non rispondono: non le riprovo per gli altri titoli
    for y in cfg.get("rendimenti", []):
        tentativi = [(y["fonte"], y["id"])] + [(a["fonte"], a["id"]) for a in y.get("alternative", [])]
        problemi = []
        for fonte, sid in tentativi:
            if fonte in down:
                problemi.append(f"{fonte}: non raggiungibile")
                continue
            try:
                for attempt in range(2):
                    try:
                        out[y["id"]] = _fetch_yield(fonte, sid, headers, timeout=45)
                        break
                    except Exception:
                        if attempt == 1:
                            raise
                        time.sleep(5)
                break
            except Exception as e:
                problemi.append(f"{fonte}: {str(e)[:80]}")
                if "timed out" in str(e).lower() or "connection" in str(e).lower():
                    down.add(fonte)
        if y["id"] not in out:
            errors.append(f"Rendimento '{y['nome']}' non scaricato ({'; '.join(problemi)})")
        elif len(problemi):
            errors.append(f"Rendimento '{y['nome']}' preso dalla fonte alternativa ({'; '.join(problemi)})")
    return out


def previous_yields() -> dict:
    """Rendimenti salvati nell'aggiornamento precedente (usati se oggi le fonti non rispondono)."""
    try:
        t = (DOCS_DIR / "data.js").read_text(encoding="utf-8")
        d = json.loads(t[t.index("=") + 1:].strip().rstrip(";"))
        return {} if d.get("meta", {}).get("demo") else d.get("rendimenti", {})
    except Exception:
        return {}


# ----------------------------------------------------------------------------
#  Dati dimostrativi (random walk) per provare la pipeline senza internet
# ----------------------------------------------------------------------------
def demo_prices(tickers: list[str]) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    end = pd.Timestamp.now(tz=ROME).normalize().tz_localize(None)
    idx = pd.bdate_range(end=end, periods=780)
    data = {}
    for i, t in enumerate(tickers):
        start = float(rng.uniform(5, 400))
        drift = rng.normal(0.0003, 0.0004)
        vol = rng.uniform(0.008, 0.028)
        rets = rng.normal(drift, vol, len(idx))
        # qualche regime di trend per rendere i grafici realistici
        rets += np.sin(np.linspace(0, rng.uniform(2, 8), len(idx)) + i) * vol * 0.15
        data[t] = start * np.exp(np.cumsum(rets))
    return pd.DataFrame(data, index=idx)


def demo_yields(cfg: dict) -> dict[str, pd.Series]:
    rng = np.random.default_rng(7)
    base = {"DGS10": 4.2, "DGS2": 3.8, "IRLTLT01ITM156N": 3.6,
            "IRLTLT01DEM156N": 2.6}
    out = {}
    for y in cfg.get("rendimenti", []):
        monthly = "M156N" in y["id"]
        idx = pd.date_range(end=pd.Timestamp.today(), periods=36 if monthly else 780,
                            freq="MS" if monthly else "B")
        b = base.get(y["id"], 2.7)
        out[y["id"]] = pd.Series(b + np.cumsum(rng.normal(0, 0.04 if monthly else 0.02, len(idx))),
                                 index=idx)
    return out


# ----------------------------------------------------------------------------
#  Indicatori
# ----------------------------------------------------------------------------
def pct(a: float, b: float) -> float | None:
    if b is None or a is None or b == 0 or pd.isna(a) or pd.isna(b):
        return None
    return (a / b - 1) * 100


def rsi(series: pd.Series, n: int = 14) -> float | None:
    d = series.diff().dropna()
    if len(d) < n + 1:
        return None
    gain = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain.iloc[-1] / loss.iloc[-1] if loss.iloc[-1] != 0 else math.inf
    return 100 - 100 / (1 + rs)


def compute_metrics(w: pd.Series) -> dict:
    """w = serie settimanale delle chiusure (venerdì)."""
    w = w.dropna()
    last = w.iloc[-1]

    def back(n):
        return w.iloc[-1 - n] if len(w) > n else None

    last52 = w.iloc[-52:]
    rets = w.pct_change().dropna()
    vol = rets.iloc[-26:].std() * math.sqrt(52) * 100 if len(rets) >= 10 else None
    ma10 = w.iloc[-10:].mean() if len(w) >= 10 else None
    ma30 = w.iloc[-30:].mean() if len(w) >= 30 else None
    year_start = w[w.index.year < w.index[-1].year]
    ytd_base = year_start.iloc[-1] if len(year_start) else None

    if ma10 is not None and ma30 is not None:
        if last > ma10 > ma30:
            trend = "rialzista"
        elif last < ma10 < ma30:
            trend = "ribassista"
        else:
            trend = "laterale"
    else:
        trend = "n.d."

    m = {
        "ultimo": float(last),
        "var_1s": pct(last, back(1)),
        "var_4s": pct(last, back(4)),
        "var_12s": pct(last, back(12)),
        "var_26s": pct(last, back(26)),
        "var_52s": pct(last, back(52)),
        "ytd": pct(last, ytd_base),
        "max_52s": float(last52.max()),
        "min_52s": float(last52.min()),
        "dist_max_52s": pct(last, last52.max()),
        "dist_min_52s": pct(last, last52.min()),
        "rsi": rsi(w),
        "volatilita": vol,
        "mm10": float(ma10) if ma10 is not None else None,
        "mm30": float(ma30) if ma30 is not None else None,
        "trend": trend,
    }
    m["rend_rischio"] = (m["var_12s"] / vol) if (m["var_12s"] is not None and vol) else None
    return m


def score_area(rows: list[dict]) -> None:
    """Punteggio 0-100 relativo all'area (percentili)."""
    if not rows:
        return
    df = pd.DataFrame(rows)

    def prank(col):
        s = pd.to_numeric(df[col], errors="coerce")
        return s.rank(pct=True).fillna(0.5)

    trend_pts = df["trend"].map({"rialzista": 1.0, "laterale": 0.5, "ribassista": 0.0}).fillna(0.5)
    score = (0.30 * prank("var_12s") + 0.20 * prank("var_4s") + 0.20 * trend_pts
             + 0.15 * prank("rend_rischio") + 0.15 * prank("dist_max_52s")) * 100
    rsi_s = pd.to_numeric(df["rsi"], errors="coerce")
    score = score - np.where(rsi_s >= 75, 10, 0)          # ipercomprato estremo: penalità
    score = score.clip(0, 100)

    for i, r in enumerate(rows):
        r["punteggio"] = round(float(score.iloc[i]), 1)
        r["segnale"] = classify(r)
    rows.sort(key=lambda r: r["punteggio"], reverse=True)
    for pos, r in enumerate(rows, 1):
        r["posizione"] = pos


def classify(r: dict) -> str:
    rsi_v = r.get("rsi")
    if rsi_v is not None and rsi_v <= 30:
        return "Ipervenduto"
    if rsi_v is not None and rsi_v >= 70:
        return "Ipercomprato"
    uptrend = r.get("mm10") is not None and r.get("mm30") is not None and r["mm10"] > r["mm30"]
    if uptrend and (r.get("var_1s") or 0) <= -2 and (rsi_v or 50) < 55:
        return "Ribasso in trend positivo"
    if r["punteggio"] >= 70 and r["trend"] == "rialzista":
        return "Momentum forte"
    if r["punteggio"] <= 30 and r["trend"] == "ribassista":
        return "Debole"
    return "Neutro"


def round_floats(obj, nd=4):
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else round(obj, nd)
    if isinstance(obj, dict):
        return {k: round_floats(v, nd) for k, v in obj.items()}
    if isinstance(obj, list):
        return [round_floats(v, nd) for v in obj]
    return obj


# ----------------------------------------------------------------------------
#  Main
# ----------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="usa dati simulati")
    args = ap.parse_args()

    cfg = load_config()
    instruments = all_instruments(cfg)
    tickers = [i["ticker"] for i in instruments]
    errors: list[str] = []

    print(f"[1/4] Download prezzi di {len(tickers)} titoli ({'DEMO' if args.demo else 'Yahoo Finance'})")
    daily = demo_prices(tickers) if args.demo else download_prices(tickers, errors)
    ok = [t for t in tickers if t in daily.columns]
    print(f"      scaricati {len(ok)}/{len(tickers)}")
    if len(ok) < MIN_SUCCESS_RATIO * len(tickers):
        print("ERRORE: troppi titoli non scaricati, non aggiorno la dashboard.", file=sys.stderr)
        for e in errors:
            print("  -", e, file=sys.stderr)
        return 1

    print("[2/4] Download rendimenti titoli di Stato")
    yields = demo_yields(cfg) if args.demo else download_yields(cfg, errors)

    print("[3/4] Calcolo settimanale e indicatori")
    # Chiusura settimanale = ultimo prezzo disponibile entro il venerdì
    weekly = daily.resample("W-FRI").last()
    weekly = weekly.dropna(how="all")
    week_end = weekly.index[-1]
    now_rome = datetime.now(ROME)
    partial = now_rome.date() < week_end.date() or (
        now_rome.date() == week_end.date() and now_rome.hour < 22)
    # settimana in corso: l'etichetta è la data dell'ultimo prezzo, non il venerdì futuro
    last_daily = daily.dropna(how="all").index[-1]
    if last_daily < week_end:
        weekly = weekly.rename(index={week_end: last_daily})
        week_end = last_daily

    areas_out = {}
    for area_key, area in cfg["aree"].items():
        rows = []
        for t in area["titoli"]:
            tk = t["ticker"]
            if tk not in weekly.columns or weekly[tk].dropna().shape[0] < 5:
                continue
            m = compute_metrics(weekly[tk])
            # dato vecchio? (es. titolo sospeso o futures scaduto)
            last_date = daily[tk].dropna().index[-1]
            m["ultimo_dato"] = last_date.strftime("%Y-%m-%d")
            if (week_end - last_date).days > 10:
                errors.append(f"'{tk}': ultimo prezzo disponibile al {m['ultimo_dato']} (dato non aggiornato)")
            rows.append({"ticker": tk, "nome": t["nome"], "gruppo": t.get("gruppo", ""),
                         "area": area_key, "valuta": guess_currency(tk), **m})
        score_area(rows)
        areas_out[area_key] = {"nome": area["nome"], "titoli": rows}

    # Indice d'area equipesato (base 100) sulle ultime 156 settimane
    area_index = {}
    for area_key, a in areas_out.items():
        cols = [r["ticker"] for r in a["titoli"]]
        if not cols:
            continue
        r = weekly[cols].pct_change(fill_method=None).iloc[-156:]
        eq = r.mean(axis=1, skipna=True).fillna(0)
        idx = (1 + eq).cumprod() * 100
        area_index[area_key] = {"d": [d.strftime("%Y-%m-%d") for d in idx.index],
                                "v": [float(x) for x in idx.values]}

    series = {}
    for t in ok:
        s = weekly[t].dropna().iloc[-156:]
        series[t] = {"d": [d.strftime("%Y-%m-%d") for d in s.index], "c": [float(x) for x in s.values]}

    yields_out = {}
    prev = {} if args.demo else previous_yields()
    for y in cfg.get("rendimenti", []):
        s = yields.get(y["id"])
        if s is None or s.empty:
            if y["id"] in prev:   # tengo l'ultimo dato valido
                yields_out[y["id"]] = {**prev[y["id"]], "nome": y["nome"] + " (dato precedente)"}
            continue
        monthly = "M156N" in y["id"]
        ws = s if monthly else s.resample("W-FRI").last().dropna()
        yields_out[y["id"]] = {"nome": y["nome"], "mensile": monthly,
                               "d": [d.strftime("%Y-%m-%d") for d in ws.index],
                               "v": [float(x) for x in ws.values],
                               "ultimo": float(ws.iloc[-1]),
                               "var_1": float(ws.iloc[-1] - ws.iloc[-2]) if len(ws) > 1 else None}

    payload = {
        "meta": {
            "generato_il": now_rome.strftime("%Y-%m-%d %H:%M"),
            "settimana_al": week_end.strftime("%Y-%m-%d"),
            "settimana_parziale": bool(partial),
            "demo": args.demo,
            "fonti": ["Simulazione (dati dimostrativi)"] if args.demo
                     else ["Yahoo Finance", "FRED – Federal Reserve Bank of St. Louis", "BCE – Statistical Data Warehouse"],
            "titoli_richiesti": len(tickers),
            "titoli_scaricati": len(ok),
            "avvisi": errors,
        },
        "aree": areas_out,
        "indici_area": area_index,
        "serie": series,
        "rendimenti": yields_out,
    }
    payload = round_floats(payload)

    print("[4/4] Salvataggio file")
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "archivio").mkdir(exist_ok=True)
    DOCS_DIR.mkdir(exist_ok=True)

    weekly.round(4).to_csv(DATA_DIR / "storico_settimanale.csv", index_label="settimana")
    report = pd.DataFrame([r for a in areas_out.values() for r in a["titoli"]])
    report.insert(0, "settimana", week_end.strftime("%Y-%m-%d"))
    report.round(3).to_csv(DATA_DIR / "report_settimanale.csv", index=False)
    if not partial:
        report.round(3).to_csv(DATA_DIR / "archivio" / f"report_{week_end:%Y-%m-%d}.csv", index=False)

    js = "window.DASHBOARD_DATA = " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ";\n"
    (DOCS_DIR / "data.js").write_text(js, encoding="utf-8")

    print(f"OK – settimana al {week_end:%d/%m/%Y}, {len(ok)} titoli, {len(errors)} avvisi")
    for e in errors:
        print("  avviso:", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
