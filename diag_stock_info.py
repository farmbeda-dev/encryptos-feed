#!/usr/bin/env python3
"""Testa quais campos o yfinance .info realmente traz pros seus 15 tickers
(volume, market cap, short interest) -- pra não inventar nome de campo."""
import sys

sys.path.insert(0, "/opt/sellplanner")
try:
    from monitor_stock import STOCK_ALVOS
except ImportError:
    print("Não consegui importar STOCK_ALVOS de /opt/sellplanner/monitor_stock.py")
    sys.exit(1)

import yfinance as yf

CAMPOS_DE_INTERESSE = [
    "marketCap",
    "volume",
    "averageVolume",
    "averageVolume10days",
    "sharesShort",
    "shortPercentOfFloat",
    "shortRatio",
    "sharesShortPriorMonth",
    "dateShortInterest",
]

for ticker in STOCK_ALVOS:
    print(f"\n--- {ticker} ---")
    try:
        info = yf.Ticker(ticker).info
    except Exception as e:
        print(f"  ERRO ao buscar .info: {e}")
        continue

    if not info:
        print("  .info veio vazio")
        continue

    for campo in CAMPOS_DE_INTERESSE:
        valor = info.get(campo, "<<AUSENTE>>")
        print(f"  {campo}: {valor}")
