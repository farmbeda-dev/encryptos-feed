#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ENCRYPTOS -- macro_fetch.py

Busca DXY, S&P500 e VIX via yfinance (mesma lib ja usada no sell planner
de stocks -- reaproveitando o que ja esta validado, sem nova dependencia).

BTCD e Dom USDT ja vem no proprio eassets.ia, nao precisam disso aqui.

Saida: /opt/encryptos/out/macro-latest.json
Roda via cron a cada 15-30 min (dados macro nao mudam segundo a segundo).
"""

import json
import os
from datetime import datetime, timezone

OUT_FILE = "/opt/encryptos/out/macro-latest.json"

TICKERS = {
    "DX-Y.NYB": "DXY",
    "^GSPC": "SP500",
    "^VIX": "VIX",
}


def fetch_macro():
    import yfinance as yf
    resultado = {}
    for ticker, nome in TICKERS.items():
        try:
            data = yf.Ticker(ticker).history(period="2d")
            if len(data) >= 1:
                atual = float(data["Close"].iloc[-1])
                anterior = float(data["Close"].iloc[-2]) if len(data) >= 2 else atual
                variacao_pct = ((atual - anterior) / anterior) * 100 if anterior else 0
                resultado[nome] = {
                    "valor": round(atual, 2),
                    "variacao_pct": round(variacao_pct, 2),
                    "tendencia": "subindo" if variacao_pct > 0 else "caindo" if variacao_pct < 0 else "estavel",
                }
            else:
                resultado[nome] = {"valor": None, "erro": "sem dados"}
        except Exception as e:
            resultado[nome] = {"valor": None, "erro": str(e)}
    return resultado


def main():
    macro = fetch_macro()
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fonte": "yfinance",
        "data": macro,
    }
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
