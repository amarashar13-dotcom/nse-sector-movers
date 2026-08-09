import os
import threading
import time
from datetime import date, timedelta

from flask import Flask, jsonify, render_template, request

from nse import NSE

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_FOLDER = os.path.join(BASE_DIR, ".nse_cache")
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

app = Flask(__name__)

nse_client = NSE(download_folder=DOWNLOAD_FOLDER)

# (canonical index name shown on the website, name used by the constituents API)
SECTOR_INDICES = [
    ("NIFTY BANK", "NIFTY BANK"),
    ("NIFTY FINANCIAL SERVICES", "NIFTY FIN SERVICE"),
    ("NIFTY PRIVATE BANK", "NIFTY PVT BANK"),
    ("NIFTY PSU BANK", "NIFTY PSU BANK"),
    ("NIFTY IT", "NIFTY IT"),
    ("NIFTY PHARMA", "NIFTY PHARMA"),
    ("NIFTY HEALTHCARE", "NIFTY HEALTHCARE"),
    ("NIFTY AUTO", "NIFTY AUTO"),
    ("NIFTY FMCG", "NIFTY FMCG"),
    ("NIFTY METAL", "NIFTY METAL"),
    ("NIFTY MEDIA", "NIFTY MEDIA"),
    ("NIFTY REALTY", "NIFTY REALTY"),
    ("NIFTY ENERGY", "NIFTY ENERGY"),
    ("NIFTY OIL & GAS", "NIFTY OIL AND GAS"),
    ("NIFTY INFRA", "NIFTY INFRA"),
    ("NIFTY CONSUMER DURABLES", "NIFTY CONSUMER DURABLES"),
    ("NIFTY INDIA CONSUMPTION", "NIFTY CONSUMPTION"),
    ("NIFTY COMMODITIES", "NIFTY COMMODITIES"),
]
CANONICAL_TO_API = {c: a for c, a in SECTOR_INDICES}

_cache = {}
_cache_lock = threading.Lock()
_last_market_status = ""


def _mk_status(raw):
    if isinstance(raw, dict):
        return (raw.get("marketStatus") or raw.get("marketStatusMessage") or "").strip()
    return (raw or "").strip()


def _cached(key, ttl, fn):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    data = fn()
    with _cache_lock:
        _cache[key] = (now, data)
    return data


def _all_indices():
    payload = nse_client.listIndices()
    by_name = {d.get("index"): d for d in payload.get("data", [])}
    sectors = []
    for canonical, _api in SECTOR_INDICES:
        d = by_name.get(canonical)
        if d is None:
            continue
        sectors.append({
            "name": canonical,
            "last": d.get("last"),
            "change": d.get("variation"),
            "pChange": d.get("percentChange"),
            "open": d.get("open"),
            "high": d.get("high"),
            "low": d.get("low"),
        })
    sectors.sort(key=lambda s: (s["pChange"] is None, -(s["pChange"] or 0)))
    return sectors, payload.get("timestamp"), payload.get("marketStatus")


def _sector_stocks(canonical):
    api_name = CANONICAL_TO_API.get(canonical)
    if api_name is None:
        raise ValueError("Unknown sector: %s" % canonical)
    payload = nse_client.listEquityStocksByIndex(api_name)
    stocks = []
    for s in payload.get("data", []):
        if s.get("series") != "EQ":
            continue
        stocks.append({
            "symbol": s.get("symbol"),
            "price": s.get("lastPrice"),
            "change": s.get("change"),
            "pChange": s.get("pChange"),
            "open": s.get("open"),
            "high": s.get("dayHigh"),
            "low": s.get("dayLow"),
            "prevClose": s.get("previousClose"),
            "volume": s.get("totalTradedVolume"),
            "turnover": s.get("totalTradedValue"),
        })
    gainers = sorted(stocks, key=lambda x: (x["pChange"] is None, -(x["pChange"] or 0)))
    losers = sorted(stocks, key=lambda x: (x["pChange"] is None, x["pChange"] or 0))
    return stocks, gainers[:8], losers[:8], _mk_status(payload.get("marketStatus")), payload.get("timestamp")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/sectors")
def api_sectors():
    global _last_market_status
    try:
        sectors, ts, ms = _cached("sectors", 10, _all_indices)
        if not _last_market_status:
            _last_market_status = _mk_status(ms)
        return jsonify({
            "ok": True,
            "marketStatus": _last_market_status,
            "timestamp": ts,
            "sectors": sectors,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": "NSE error: %s" % e}), 502


@app.route("/api/sector")
def api_sector():
    global _last_market_status
    name = request.args.get("name", "").strip()
    if name not in CANONICAL_TO_API:
        return jsonify({"ok": False, "error": "Unknown sector"}), 400
    try:
        stocks, gainers, losers, ms, ts = _cached(
            "sector:" + name, 15, lambda: _sector_stocks(name)
        )
        if ms:
            _last_market_status = ms
        return jsonify({
            "ok": True,
            "sector": name,
            "marketStatus": ms,
            "timestamp": ts,
            "stocks": stocks,
            "gainers": gainers,
            "losers": losers,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": "NSE error: %s" % e}), 502


@app.route("/api/history")
def api_history():
    symbol = request.args.get("symbol", "").strip().upper()
    days_raw = request.args.get("days", "90")
    try:
        days = min(max(int(days_raw), 5), 365)
    except ValueError:
        days = 90
    if not symbol:
        return jsonify({"ok": False, "error": "Missing symbol"}), 400
    try:
        to_date = date.today()
        from_date = to_date - timedelta(days=days)
        rows = _cached(
            "hist:" + symbol + ":" + str(days),
            300,
            lambda: nse_client.fetch_equity_historical_data(
                symbol, from_date, to_date
            ),
        )
        candles = [
            {
                "date": r.get("mtimestamp"),
                "open": r.get("chOpeningPrice"),
                "high": r.get("chTradeHighPrice"),
                "low": r.get("chTradeLowPrice"),
                "close": r.get("chClosingPrice"),
                "volume": r.get("chTotTradedQty"),
            }
            for r in rows
        ]
        return jsonify({"ok": True, "symbol": symbol, "candles": candles})
    except Exception as e:
        return jsonify({"ok": False, "error": "NSE error: %s" % e}), 502


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
