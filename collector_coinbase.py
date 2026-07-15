#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ENCRYPTOS -- collector_coinbase.py

Coleta dados SPOT publicos da Coinbase Exchange (gratuito, sem chave).
Coinbase nao tem futuros/OI/funding pra varejo -- serve como camada de
CONFIRMACAO spot (comparar com o CVD/book dos futuros da Binance).

Uso pratico: comparar pressao de compra/venda SPOT (Coinbase) vs FUTUROS
(Binance/Bybit) -- se divergem, e sinal de atencao (conforme a filosofia
CVD spot vs perp que ja documentamos no SOUL.md).

Saida: /opt/encryptos/out/liquidez-coinbase-latest.json
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

COINBASE_API = "https://api.exchange.coinbase.com"
HEADERS = {"User-Agent": "encryptos-collector-coinbase/1.0"}
TIMEOUT = 10
NEAR_BAND_PCT = 2.0
LARGE_ORDER_USD = 50_000
OUT_DIR = "/opt/encryptos/out"
OUT_FILE = os.path.join(OUT_DIR, "liquidez-coinbase-latest.json")
REQUEST_DELAY = 0.3


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{ts} {msg}", file=sys.stderr)


def _get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def get_usd_products():
    """Lista pares USD spot ativos na Coinbase."""
    data = _get(f"{COINBASE_API}/products")
    return [
        p["id"] for p in data
        if p.get("quote_currency") == "USD" and p.get("status") == "online"
    ]


def get_book_pressure(product_id):
    try:
        data = _get(f"{COINBASE_API}/products/{product_id}/book?level=2")
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        if not bids or not asks:
            return None

        mark = (float(bids[0][0]) + float(asks[0][0])) / 2
        band_low = mark * (1 - NEAR_BAND_PCT / 100)
        band_high = mark * (1 + NEAR_BAND_PCT / 100)

        bid_usd = sum(float(p) * float(q) for p, q, *_ in bids if float(p) >= band_low)
        ask_usd = sum(float(p) * float(q) for p, q, *_ in asks if float(p) <= band_high)
        total = bid_usd + ask_usd
        if total == 0:
            return None

        large_orders = []
        for p, q, *_ in bids:
            notional = float(p) * float(q)
            if notional >= LARGE_ORDER_USD:
                large_orders.append({"side": "bid", "price": float(p), "notional_usd": round(notional, 2)})
        for p, q, *_ in asks:
            notional = float(p) * float(q)
            if notional >= LARGE_ORDER_USD:
                large_orders.append({"side": "ask", "price": float(p), "notional_usd": round(notional, 2)})

        return {
            "mark": mark,
            "pressure": {
                "bid_pct_support": round(bid_usd / total * 100, 2),
                "ask_pct_resistance": round(ask_usd / total * 100, 2),
            },
            "large_orders_spot": large_orders,
        }
    except Exception:
        return None


def main():
    log("=== Coinbase Collector (Spot) -- iniciando ===")
    products = get_usd_products()
    log(f"Pares USD encontrados: {len(products)}")

    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "coinbase_public_spot",
        "nota": "Spot apenas -- Coinbase nao tem futuros/OI/funding pra varejo. Usar como confirmacao spot vs perp.",
        "data": {},
    }

    ok, err = 0, 0
    for product_id in products:
        book = get_book_pressure(product_id)
        if book:
            output["data"][product_id] = {"book_spot": book}
            ok += 1
        else:
            err += 1
        time.sleep(REQUEST_DELAY)

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=1)

    log(f"OK: {ok} | Erros: {err}")
    log(f"Salvo em {OUT_FILE}")


if __name__ == "__main__":
    main()
