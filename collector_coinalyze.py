#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ENCRYPTOS -- collector_coinalyze.py (v2)
Corrige: rate limit real (2 chamadas por lote = dobra o ritmo), retry com
backoff em 429, filtro de simbolos nao-ascii.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

COINALYZE_API = "https://api.coinalyze.net/v1"
API_KEY = os.environ.get("COINALYZE_API_KEY", "")
TIMEOUT = 12
OUT_DIR = "/opt/encryptos/out"
OUT_FILE = os.path.join(OUT_DIR, "liquidez-coinalyze-latest.json")
REQUEST_DELAY = 3.2  # 2 chamadas por lote -> precisa do DOBRO do delay minimo


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{ts} {msg}", file=sys.stderr)


def _get(url, tentativas=3):
    req = urllib.request.Request(url, headers={"api_key": API_KEY, "User-Agent": "encryptos-coinalyze/2.0"})
    for i in range(tentativas):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                log(f"  rate limited, esperando 15s (tentativa {i+1}/{tentativas})...")
                time.sleep(15)
                continue
            raise
    raise Exception("rate limit persistente apos varias tentativas")


def get_future_markets():
    return _get(f"{COINALYZE_API}/future-markets")


def get_oi_aggregated(symbols_csv):
    return _get(f"{COINALYZE_API}/open-interest?symbols={symbols_csv}")


def get_funding_aggregated(symbols_csv):
    return _get(f"{COINALYZE_API}/funding-rate?symbols={symbols_csv}")


def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def main():
    if not API_KEY:
        log("ERRO: COINALYZE_API_KEY nao configurada.")
        sys.exit(1)

    log("=== Coinalyze Collector v2 -- iniciando ===")
    markets = get_future_markets()

    usdt_symbols = [
        m["symbol"] for m in markets
        if m.get("base_asset") and "USDT" in m.get("symbol", "")
        and m.get("is_perpetual", True)
        and m.get("symbol", "").isascii()  # filtra simbolos com caracteres nao-ascii
    ]
    log(f"Mercados USDT encontrados (ascii): {len(usdt_symbols)}")

    output = {"timestamp": datetime.now(timezone.utc).isoformat(), "source": "coinalyze_aggregated", "data": {}}
    ok, err = 0, 0

    for batch in chunks(usdt_symbols, 20):
        symbols_csv = ",".join(batch)
        try:
            oi_data = get_oi_aggregated(symbols_csv)
            time.sleep(REQUEST_DELAY)
            funding_data = get_funding_aggregated(symbols_csv)
            time.sleep(REQUEST_DELAY)

            oi_map = {item["symbol"]: item for item in oi_data} if isinstance(oi_data, list) else {}
            funding_map = {item["symbol"]: item for item in funding_data} if isinstance(funding_data, list) else {}

            for sym in batch:
                oi_item = oi_map.get(sym, {})
                funding_item = funding_map.get(sym, {})
                if oi_item or funding_item:
                    output["data"][sym] = {
                        "open_interest": {"value_aggregated": oi_item.get("value"), "update": oi_item.get("update")},
                        "funding": {"rate_aggregated": funding_item.get("value")},
                    }
                    ok += 1
                else:
                    err += 1
        except Exception as e:
            log(f"Lote com erro definitivo: {e}")
            err += len(batch)
            time.sleep(REQUEST_DELAY)

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=1)

    log(f"OK: {ok} | Erros: {err}")
    log(f"Salvo em {OUT_FILE}")


if __name__ == "__main__":
    main()
