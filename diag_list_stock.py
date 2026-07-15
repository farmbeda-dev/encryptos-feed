#!/usr/bin/env python3
"""Testa /list_stock isolado, sem depender do Telegram, pra achar o erro exato."""
import sys, traceback
sys.path.insert(0, "/opt/encryptos")
sys.path.insert(0, "/opt/sellplanner")

print("--- 1. Importando monitor_stock ---")
try:
    from monitor_stock import STOCK_ALVOS
    print(f"OK — {len(STOCK_ALVOS)} tickers: {list(STOCK_ALVOS.keys())}")
except Exception:
    print("FALHOU import monitor_stock:")
    traceback.print_exc()
    sys.exit(1)

print("\n--- 2. Importando watchlist_manager ---")
try:
    import watchlist_manager as wm
    print("OK")
except Exception:
    print("FALHOU import watchlist_manager:")
    traceback.print_exc()
    sys.exit(1)

print("\n--- 3. Rodando cmd_list_stock ---")
try:
    resultado = wm.cmd_list_stock({"positions": {}, "watch": {}}, [])
    print("RESULTADO:")
    print(resultado)
except Exception:
    print("FALHOU cmd_list_stock:")
    traceback.print_exc()
