#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ENCRYPTOS -- cvd_engine.py

Modulo standalone (nao mexe no collector.py existente) que calcula:
- CVD (Cumulative Volume Delta) via klines futuros da Binance
- Trades por segundo

Fonte: endpoint publico /fapi/v1/klines (gratuito, sem chave, peso leve).
Cada kline (1m) ja vem com "numero de trades" e "taker buy base volume" --
nao precisamos somar trade a trade (isso seria aggTrades, mais pesado).

CVD delta = taker_buy_volume - taker_sell_volume (taker_sell = volume total - taker_buy)
Se positivo: mais agressao compradora que vendedora na janela.
Se negativo: mais agressao vendedora -- ATENCAO a divergencia preco/CVD.

Filosofia (conforme conhecimento anexado ao SOUL.md):
- OI = tamanho do combustivel
- CVD = direcao da agressao (quem esta "batendo a mercado")
- Preco sobe + CVD desce = suspeita de armadilha (venda disfarcada em book raso)
- Preco sobe + CVD sobe = demanda real confirmada

Saida: /opt/encryptos/out/cvd-latest.json
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

FAPI = "https://fapi.binance.com"
HEADERS = {"User-Agent": "encryptos-cvd/1.0"}
TIMEOUT = 10

LOOKBACK_MINUTES = 15  # janela de klines de 1m usada para o calculo
OUT_DIR = "/opt/encryptos/out"
OUT_FILE = os.path.join(OUT_DIR, "cvd-latest.json")

# fonte da lista de simbolos: reaproveita o feed do collector.py existente
LIQUIDEZ_FEED = "/opt/encryptos/out/liquidez-latest.json"

REQUEST_DELAY = 0.05  # pausa entre chamadas, para nao estourar rate limit


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{ts} {msg}", file=sys.stderr)


def _get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def get_symbols():
    if not os.path.exists(LIQUIDEZ_FEED):
        log(f"ERRO: {LIQUIDEZ_FEED} nao encontrado. Rode o collector.py primeiro.")
        sys.exit(1)
    with open(LIQUIDEZ_FEED, "r", encoding="utf-8") as f:
        data = json.load(f)
    symbols = sorted(data.get("data", {}).keys())
    return [s for s in symbols if s.isascii()]


def compute_cvd_and_trades(symbol):
    """
    Retorna dict com cvd_delta, trades_total, trades_per_second, price_direction
    baseado nas ultimas LOOKBACK_MINUTES klines de 1m.
    """
    url = f"{FAPI}/fapi/v1/klines?symbol={symbol}&interval=1m&limit={LOOKBACK_MINUTES}"
    try:
        klines = _get(url)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        return None, str(e)

    if not klines or len(klines) < 2:
        return None, "dados insuficientes"

    total_trades = 0
    total_volume = 0.0
    total_taker_buy = 0.0

    for k in klines:
        # indices do kline: [0]open_time [5]volume [8]num_trades [9]taker_buy_base_vol
        volume = float(k[5])
        num_trades = int(k[8])
        taker_buy = float(k[9])
        total_volume += volume
        total_trades += num_trades
        total_taker_buy += taker_buy

    taker_sell = total_volume - total_taker_buy
    cvd_delta = total_taker_buy - taker_sell

    window_seconds = LOOKBACK_MINUTES * 60
    trades_per_second = round(total_trades / window_seconds, 2)

    price_open = float(klines[0][1])
    price_close = float(klines[-1][4])
    price_change_pct = ((price_close - price_open) / price_open * 100) if price_open else 0.0

    # divergencia: preco sobe mas CVD negativo (ou vice-versa) = sinal de atencao
    divergence = None
    if price_change_pct > 0.1 and cvd_delta < 0:
        divergence = "preco_sobe_cvd_desce_atencao"
    elif price_change_pct < -0.1 and cvd_delta > 0:
        divergence = "preco_desce_cvd_sobe_possivel_suporte_real"

    result = {
        "cvd_delta": round(cvd_delta, 2),
        "total_volume": round(total_volume, 2),
        "taker_buy_volume": round(total_taker_buy, 2),
        "taker_sell_volume": round(taker_sell, 2),
        "trades_total": total_trades,
        "trades_per_second": trades_per_second,
        "price_change_pct_window": round(price_change_pct, 3),
        "divergence": divergence,
        "window_minutes": LOOKBACK_MINUTES,
    }
    return result, None


def main():
    log("=== CVD Engine -- iniciando rodada ===")
    symbols = get_symbols()
    log(f"Simbolos a processar: {len(symbols)}")

    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "binance_futures_klines",
        "window_minutes": LOOKBACK_MINUTES,
        "data": {},
    }

    ok_count = 0
    err_count = 0

    for symbol in symbols:
        result, err = compute_cvd_and_trades(symbol)
        if result:
            output["data"][symbol] = result
            ok_count += 1
        else:
            err_count += 1
        time.sleep(REQUEST_DELAY)

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=1)

    log(f"OK: {ok_count} | Erros: {err_count}")
    log(f"Salvo em {OUT_FILE}")
    log("=== Rodada concluida ===")


if __name__ == "__main__":
    main()
