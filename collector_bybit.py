#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ENCRYPTOS -- collector_bybit.py

Mesmo padrao do collector.py original (Binance), mas pra Bybit.
API publica e gratuita, sem chave necessaria.

Coleta: funding rate, open interest, LSR (long/short ratio), book pressure.
Saida: /opt/encryptos/out/liquidez-bybit-latest.json

NAO substitui o collector.py da Binance -- roda em paralelo, arquivo proprio.
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

BYBIT_API = "https://api.bybit.com"
HEADERS = {"User-Agent": "encryptos-collector-bybit/1.0"}
TIMEOUT = 12
LARGE_ORDER_USD = 50_000
NEAR_BAND_PCT = 2.0
OUT_DIR = "/opt/encryptos/out"
OUT_FILE = os.path.join(OUT_DIR, "liquidez-bybit-latest.json")


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{ts} {msg}", file=sys.stderr)


def _get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def get_symbols():
    """Lista todos os perpetuos USDT ativos na Bybit."""
    url = f"{BYBIT_API}/v5/market/instruments-info?category=linear"
    data = _get(url)
    symbols = []
    for item in data.get("result", {}).get("list", []):
        if item.get("quoteCoin") == "USDT" and item.get("status") == "Trading" and "-" not in item.get("symbol", ""):
            symbols.append(item["symbol"])
    return symbols


def get_funding(symbol):
    url = f"{BYBIT_API}/v5/market/tickers?category=linear&symbol={symbol}"
    data = _get(url)
    lst = data.get("result", {}).get("list", [])
    if not lst:
        return None
    t = lst[0]
    return {
        "rate": float(t.get("fundingRate", 0)),
        "mark_price": float(t.get("markPrice", 0)),
        "oi_contracts": float(t.get("openInterest", 0)),
        "oi_usd": float(t.get("openInterestValue", 0)),
    }


def get_lsr(symbol):
    """Long/Short Ratio da Bybit (contas)."""
    url = f"{BYBIT_API}/v5/market/account-ratio?category=linear&symbol={symbol}&period=5min&limit=1"
    try:
        data = _get(url)
        lst = data.get("result", {}).get("list", [])
        if not lst:
            return None
        item = lst[0]
        buy_ratio = float(item.get("buyRatio", 0))
        sell_ratio = float(item.get("sellRatio", 0))
        if sell_ratio == 0:
            return None
        return {"ratio": round(buy_ratio / sell_ratio, 4), "period": "5min"}
    except Exception:
        return None


def get_book_pressure(symbol):
    url = f"{BYBIT_API}/v5/market/orderbook?category=linear&symbol={symbol}&limit=50"
    try:
        data = _get(url)
        result = data.get("result", {})
        bids = result.get("b", [])
        asks = result.get("a", [])
        if not bids or not asks:
            return None
        mark = (float(bids[0][0]) + float(asks[0][0])) / 2
        band_low = mark * (1 - NEAR_BAND_PCT / 100)
        band_high = mark * (1 + NEAR_BAND_PCT / 100)

        bid_usd = sum(float(p) * float(q) for p, q in bids if float(p) >= band_low)
        ask_usd = sum(float(p) * float(q) for p, q in asks if float(p) <= band_high)
        total = bid_usd + ask_usd
        if total == 0:
            return None

        large_orders = []
        for p, q in bids:
            notional = float(p) * float(q)
            if notional >= LARGE_ORDER_USD:
                large_orders.append({"side": "bid", "price": float(p), "notional_usd": round(notional, 2)})
        for p, q in asks:
            notional = float(p) * float(q)
            if notional >= LARGE_ORDER_USD:
                large_orders.append({"side": "ask", "price": float(p), "notional_usd": round(notional, 2)})

        return {
            "mark": mark,
            "pressure": {
                "bid_pct_support": round(bid_usd / total * 100, 2),
                "ask_pct_resistance": round(ask_usd / total * 100, 2),
                "bid_usd": round(bid_usd, 2),
                "ask_usd": round(ask_usd, 2),
            },
            "large_orders": large_orders,
        }
    except Exception:
        return None


def main():
    log("=== Bybit Collector -- iniciando ===")
    symbols = get_symbols()
    log(f"Simbolos encontrados: {len(symbols)}")

    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "bybit_public",
        "exchange": "bybit_linear",
        "data": {},
    }

    ok, err = 0, 0
    for symbol in symbols:
        try:
            funding = get_funding(symbol)
            lsr = get_lsr(symbol)
            book = get_book_pressure(symbol)
            if funding and book:
                output["data"][symbol] = {
                    "funding": {"rate": funding["rate"]},
                    "open_interest": {
                        "contracts": funding["oi_contracts"],
                        "usd_bybit": funding["oi_usd"],
                    },
                    "lsr_top_accounts": lsr or {},
                    "book_futures": book,
                }
                ok += 1
            else:
                err += 1
        except Exception as e:
            err += 1
            log(f"{symbol}: erro {e}")

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=1)

    log(f"OK: {ok} | Erros: {err}")
    log(f"Salvo em {OUT_FILE}")


if __name__ == "__main__":
    main()
