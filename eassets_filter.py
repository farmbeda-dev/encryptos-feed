#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ENCRYPTOS — eassets_filter.py

Pre-filtro do JSON eassets.ia. Roda 100% em Python puro, SEM chamar nenhuma IA.
Aplica o checklist matematico do SOUL.md (RSI, Range Level, EXP BTC, OI Trend,
LSR Trend, Trades Level) e produz um DIGEST compacto so com os candidatos que
ja passaram no filtro basico.

Por que isso importa: em vez de mandar o JSON de 530 ativos inteiro pro Claude
(centenas de milhares de tokens, caro), este script reduz pra ~5-15 candidatos
com so os campos relevantes (poucos KB). O Claude so entra depois, pra dar a
leitura fina/contextual dos candidatos ja filtrados -- nao para peneirar tudo.

Uso:
    python3 eassets_filter.py /opt/encryptos/input/eassets-latest.json

Saida:
    /opt/encryptos/output/eassets-digest-<timestamp>.txt  (para colar no Moach)
    /opt/encryptos/output/eassets-digest-latest.txt        (sempre o mais recente)
"""

import json
import os
import sys
from datetime import datetime, timezone

INPUT_DEFAULT = "/opt/encryptos/input/eassets-latest.json"
OUTPUT_DIR = "/opt/encryptos/output"

# Timeframe preferencial para leitura de cada indicador (pode ajustar)
TF_PRIMARY = "1h"
TF_TREND = "4h"

# Limiares do checklist Encryptos (SOUL.md)
RSI_MIN, RSI_MAX = 30, 60
RANGE_LEVEL_MIN = 3
EXP_BTC_MIN = 0
OI_TREND_MIN = 0
TRADES_LEVEL_MIN = 1


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{ts} {msg}")


def get_field(symbol_data, field, tf):
    """Busca campo com timeframe sufixo, ex: 'rsi:1h'. Retorna None se ausente/null."""
    key = f"{field}:{tf}"
    val = symbol_data.get(key)
    return val


def score_symbol(symbol, data):
    """
    Score 0-100 baseado no checklist do SOUL.md, usando dados do eassets.
    Retorna (score, reasons, snapshot_relevante)
    """
    score = 0
    reasons = []
    snap = {}

    rsi = get_field(data, "rsi", TF_PRIMARY)
    range_level = get_field(data, "range_level", TF_PRIMARY)
    exp_btc = get_field(data, "exp_btc", TF_PRIMARY)
    oi_trend = get_field(data, "oi_trend", TF_TREND)
    lsr_trend = get_field(data, "lsr_trend", TF_TREND)
    trades_level = get_field(data, "trades_level", TF_PRIMARY)
    price = data.get("price")
    price_change = get_field(data, "price_change", TF_PRIMARY)

    snap["price"] = price
    snap["price_change_1h"] = price_change
    snap["rsi_1h"] = rsi
    snap["range_level_1h"] = range_level
    snap["exp_btc_1h"] = exp_btc
    snap["oi_trend_4h"] = oi_trend
    snap["lsr_trend_4h"] = lsr_trend
    snap["trades_level_1h"] = trades_level

    # 1) RSI na zona fria/neutra (nao morto, nao esticado)
    if rsi is not None and RSI_MIN <= rsi <= RSI_MAX:
        score += 20
        reasons.append(f"RSI {rsi:.1f} na zona neutra/fria ({RSI_MIN}-{RSI_MAX})")

    # 2) Range Level -- acumulacao
    if range_level is not None and range_level >= RANGE_LEVEL_MIN:
        score += 25
        reasons.append(f"Range Level {range_level} >= {RANGE_LEVEL_MIN}, acumulacao forte")

    # 3) EXP BTC positivo -- forca relativa
    if exp_btc is not None and exp_btc > EXP_BTC_MIN:
        score += 25
        reasons.append(f"EXP BTC {exp_btc:.2f} positivo, dominando o Bitcoin")

    # 4) OI Trend positivo -- capital entrando
    if oi_trend is not None and oi_trend > OI_TREND_MIN:
        score += 15
        reasons.append(f"OI Trend {oi_trend:.2f} positivo, capital novo entrando")

    # 5) Trades Level -- spike de atividade (robots ligando)
    if trades_level is not None and trades_level >= TRADES_LEVEL_MIN:
        score += 15
        reasons.append(f"Trades Level {trades_level}, atividade acima do normal")

    return score, reasons, snap


def format_digest(candidates, total_symbols, threshold):
    lines = []
    lines.append("=== DIGEST ENCRYPTOS — eassets.ia (pre-filtrado) ===")
    lines.append(f"Gerado: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"Símbolos analisados: {total_symbols} | Candidatos (score>={threshold}): {len(candidates)}")
    lines.append("")
    lines.append("IMPORTANTE: isto e um pre-filtro matematico, NAO substitui o checklist completo.")
    lines.append("Confirma macro (BTCD/DXY/S&P500) e demais itens antes de qualquer entrada.")
    lines.append("")

    for symbol, score, reasons, snap in candidates:
        lines.append(f"--- {symbol} | Score: {score}/100 ---")
        lines.append(f"Preço: {snap.get('price')} | Var 1h: {snap.get('price_change_1h')}")
        for r in reasons:
            lines.append(f"  • {r}")
        lines.append("")

    if not candidates:
        lines.append("Nenhum símbolo passou do limiar nesta leitura.")

    return "\n".join(lines)


def main():
    input_path = sys.argv[1] if len(sys.argv) > 1 else INPUT_DEFAULT

    if not os.path.exists(input_path):
        log(f"ERRO: arquivo nao encontrado: {input_path}")
        log("Coloca o JSON do eassets.ia em /opt/encryptos/input/eassets-latest.json")
        sys.exit(1)

    with open(input_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    data_map = raw.get("data", {})
    log(f"Símbolos no arquivo: {len(data_map)}")

    threshold = 60  # mais permissivo que o alertas_engine (aqui e so pre-filtro pro Claude ler)
    scored = []
    for symbol, sdata in data_map.items():
        score, reasons, snap = score_symbol(symbol, sdata)
        if score >= threshold:
            scored.append((symbol, score, reasons, snap))

    scored.sort(key=lambda x: x[1], reverse=True)
    # Limita a top 15 para manter o digest realmente pequeno
    scored = scored[:15]

    digest = format_digest(scored, len(data_map), threshold)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = os.path.join(OUTPUT_DIR, f"eassets-digest-{ts}.txt")
    latest_path = os.path.join(OUTPUT_DIR, "eassets-digest-latest.txt")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(digest)
    with open(latest_path, "w", encoding="utf-8") as f:
        f.write(digest)

    log(f"Digest gerado: {out_path}")
    log(f"Candidatos encontrados: {len(scored)}")
    print("\n" + digest)


if __name__ == "__main__":
    main()
