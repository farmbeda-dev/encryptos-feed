#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ENCRYPTOS — collector.py
Camada de LIQUIDEZ que falta no eassets: book (spot+futuros), funding, OI e LSR
direto da API PUBLICA e GRATUITA da Binance (sem chave de API).

Saida: um JSON unificado por simbolo, pronto pra ser lido junto com o eassets.
Roda em qualquer lugar (so usa a stdlib do Python 3). Ideal num cron no Hetzner.

Conceitos ENCRYPTOS embutidos:
- book = liquidez; pressao de % cima/baixo (o "book pressory" do Melo)
- ordens discrepantes >= limite (imã / onde vao estopar)
- funding negativo = shorts pagando = alta "real"
- LSR / OI como confirmacao (nunca uma variavel so)
- guarda historico: cada run e um snapshot -> diff vira "heatmap caseiro"
  (a ordem "nascendo e crescendo", que e o que o tradelight/coinglass mostram)

NAO faz: heatmap de liquidacao e OI agregado entre exchanges -> isso e Coinglass.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ----------------------- CONFIG -----------------------
SYMBOLS = None                    # None = descobre sozinho todos os perpetuos USDT ativos
SYMBOL_QUOTE = "USDT"             # filtro de par (USDT, USDC, etc)
LARGE_ORDER_USD = 50_000          # ordem de "baleia" em altcoin (conceito: >=50k)
NEAR_BAND_PCT = 2.0               # +/- % em torno do preco p/ medir pressao do book
DEPTH_LIMIT = 1000                # niveis do book (max 1000 nos futuros)
LSR_PERIOD = "5m"                 # periodo do long/short ratio
OUT_DIR = os.path.expanduser("./out")          # JSON unificado
HIST_DIR = os.path.expanduser("./out/history") # snapshots p/ heatmap caseiro
TIMEOUT = 12

FAPI = "https://fapi.binance.com"   # futuros USD-M
API = "https://api.binance.com"     # spot
HEADERS = {"User-Agent": "encryptos-collector/1.0"}
# ------------------------------------------------------


def _get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def safe_get(url):
    """GET com tolerancia: retorna (data, erro). Nao derruba o run inteiro."""
    try:
        return _get(url), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, str(e)


def analyze_book(levels_bids, levels_asks, mark, large_usd, near_pct):
    """
    levels_*: lista [[preco, qtd], ...] (strings da Binance).
    Retorna paredes grandes, pressao % perto do preco e a parede mais proxima
    de cada lado.
    """
    def norm(levels, side):
        out = []
        for p, q in levels:
            price = float(p); qty = float(q)
            notional = price * qty
            dist = (price - mark) / mark * 100.0 if mark else 0.0
            out.append({
                "side": side, "price": price, "qty": qty,
                "notional_usd": round(notional, 2),
                "dist_pct": round(dist, 3),
            })
        return out

    bids = norm(levels_bids, "bid")  # compras (suporte)
    asks = norm(levels_asks, "ask")  # vendas (resistencia)

    big = sorted(
        [o for o in (bids + asks) if o["notional_usd"] >= large_usd],
        key=lambda o: o["notional_usd"], reverse=True,
    )

    lo = mark * (1 - near_pct / 100.0)
    hi = mark * (1 + near_pct / 100.0)
    bid_near = sum(o["notional_usd"] for o in bids if lo <= o["price"] <= mark)
    ask_near = sum(o["notional_usd"] for o in asks if mark <= o["price"] <= hi)
    total_near = bid_near + ask_near
    bid_pct = round(bid_near / total_near * 100.0, 1) if total_near else None
    ask_pct = round(ask_near / total_near * 100.0, 1) if total_near else None

    wall_below = max((o for o in bids if o["notional_usd"] >= large_usd),
                     key=lambda o: o["notional_usd"], default=None)
    wall_above = max((o for o in asks if o["notional_usd"] >= large_usd),
                     key=lambda o: o["notional_usd"], default=None)

    return {
        "mark": mark,
        "near_band_pct": near_pct,
        "pressure": {  # o "book pressory" do Melo (ex.: 51% cima / 49% baixo)
            "bid_pct_support": bid_pct,
            "ask_pct_resistance": ask_pct,
            "bid_usd": round(bid_near, 2),
            "ask_usd": round(ask_near, 2),
        },
        "nearest_wall_below": wall_below,  # suporte grande mais perto
        "nearest_wall_above": wall_above,  # resistencia grande mais perto
        "large_orders": big[:25],          # paredes discrepantes (ima / stop)
        "large_orders_count": len(big),
    }


def fetch_symbol(sym):
    res = {"symbol": sym, "ts": datetime.now(timezone.utc).isoformat()}

    # --- funding + mark price (premiumIndex) ---
    pidx, err = safe_get(f"{FAPI}/fapi/v1/premiumIndex?symbol={sym}")
    mark = None
    if pidx and not err:
        mark = float(pidx.get("markPrice", 0)) or None
        fr = float(pidx.get("lastFundingRate", 0))
        res["funding"] = {
            "rate": fr,
            # conceito: negativo = shorts pagando longs = alta "real"
            "reading": "shorts_pagando_longs(alta_real)" if fr < 0
            else "longs_pagando_shorts" if fr > 0 else "neutro",
        }
    else:
        res["funding_error"] = err

    # --- open interest (Binance, em contratos -> converte p/ USD via mark) ---
    oi, err = safe_get(f"{FAPI}/fapi/v1/openInterest?symbol={sym}")
    if oi and not err:
        oi_contracts = float(oi.get("openInterest", 0))
        res["open_interest"] = {
            "contracts": oi_contracts,
            "usd_binance": round(oi_contracts * mark, 2) if mark else None,
            "note": "agregado entre exchanges = Coinglass (nao incluso aqui)",
        }
    else:
        res["oi_error"] = err

    # --- LSR top traders (account ratio) ---
    lsr, err = safe_get(
        f"{FAPI}/futures/data/topLongShortAccountRatio?symbol={sym}&period={LSR_PERIOD}&limit=1"
    )
    if lsr and not err and isinstance(lsr, list) and lsr:
        ratio = float(lsr[-1].get("longShortRatio", 0))
        res["lsr_top_accounts"] = {
            "ratio": ratio,
            "period": LSR_PERIOD,
            # conceito: <1 = short dominante (combustivel de squeeze)
            "reading": "short_dominante(combustivel_squeeze)" if ratio < 1
            else "long_dominante",
        }
    else:
        res["lsr_error"] = err

    # --- BOOK futuros (sempre existe se ha perpetuo) ---
    fdepth, err = safe_get(f"{FAPI}/fapi/v1/depth?symbol={sym}&limit={DEPTH_LIMIT}")
    if fdepth and not err and mark:
        res["book_futures"] = analyze_book(
            fdepth.get("bids", []), fdepth.get("asks", []),
            mark, LARGE_ORDER_USD, NEAR_BAND_PCT,
        )
    else:
        res["book_futures_error"] = err or "sem mark price"

    # --- BOOK spot (pode nao existir p/ a altcoin) ---
    sdepth, err = safe_get(f"{API}/api/v3/depth?symbol={sym}&limit=500")
    if sdepth and not err:
        ref = mark or (float(sdepth["bids"][0][0]) if sdepth.get("bids") else None)
        if ref:
            res["book_spot"] = analyze_book(
                sdepth.get("bids", []), sdepth.get("asks", []),
                ref, LARGE_ORDER_USD, NEAR_BAND_PCT,
            )
    else:
        # 400 = nao ha par spot dessa altcoin (normal). Conceito: so futuros =
        # sem referencia de stop ("descoberta de preco"), prende mais gente.
        res["book_spot_note"] = "sem par spot (so futuros)" if err and "400" in err else err

    return res


def write_history(sym, snapshot):
    """Guarda so as paredes grandes p/ diff entre runs (heatmap caseiro)."""
    os.makedirs(HIST_DIR, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = os.path.join(HIST_DIR, f"{sym}-{day}.jsonl")
    line = {
        "ts": snapshot["ts"],
        "futures_walls": (snapshot.get("book_futures") or {}).get("large_orders", []),
        "pressure": (snapshot.get("book_futures") or {}).get("pressure"),
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
  # Descobre símbolos automaticamente se SYMBOLS = None
    symbols_to_use = SYMBOLS
    if symbols_to_use is None:
        info, err = safe_get(f"{FAPI}/fapi/v1/exchangeInfo")
        if info and not err:
            symbols_to_use = [
                s["symbol"] for s in info.get("symbols", [])
                if s.get("contractType") == "PERPETUAL"
                and s.get("status") == "TRADING"
                and s.get("quoteAsset") == SYMBOL_QUOTE
            ]
            print(f"descobertos {len(symbols_to_use)} perpetuais {SYMBOL_QUOTE}")
        else:
            print(f"erro ao descobrir simbolos: {err}")
            return
    out = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "binance_public",
        "large_order_usd": LARGE_ORDER_USD,
        "near_band_pct": NEAR_BAND_PCT,
        "data": {},
    }
    for sym in symbols_to_use:
        snap = fetch_symbol(sym)
        out["data"][sym] = snap
        write_history(sym, snap)
        time.sleep(0.15)  # respeita rate limit

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    fname = os.path.join(OUT_DIR, f"liquidez-{stamp}.json")
    with open(fname, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    # tambem um "latest" fixo p/ facilitar leitura/automacao
    with open(os.path.join(OUT_DIR, "liquidez-latest.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(fname)


if __name__ == "__main__":
    main()
