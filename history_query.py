#!/usr/bin/env python3
"""
history_query.py -- le /opt/encryptos/alertas/history.jsonl e monta resumo.
Pode rodar direto no terminal OU ser importado como comando /history
no watchlist_manager.py (ver bloco de integracao no final).
"""

import json
import os
from collections import defaultdict
from datetime import datetime, timezone, timedelta

HISTORY_FILE = "/opt/encryptos/alertas/history.jsonl"


def load_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    entries = []
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def _parse_ts(ts_str):
    """Converte o timestamp string do history.jsonl pra datetime UTC."""
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _format_delta(delta):
    """Formata um timedelta em texto curto tipo '2h14m' ou '3d'."""
    total_seconds = int(delta.total_seconds())
    if total_seconds < 60:
        return "agora"
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days > 0:
        return f"{days}d{hours}h"
    if hours > 0:
        return f"{hours}h{minutes}m"
    return f"{minutes}m"


def summarize(entries, symbol_filter=None, only_today=False):
    """
    Agrupa por symbol: quantas vezes apareceu (hoje / 7 dias / total),
    primeiro/ultimo preco visto, variacao % entre a primeira e a ultima
    aparicao, e ha quanto tempo foi o ultimo alerta.
    """
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    week_ago = now - timedelta(days=7)

    by_symbol = defaultdict(list)
    for e in entries:
        if symbol_filter and e["symbol"] != symbol_filter:
            continue
        if only_today and not e["ts"].startswith(today_str):
            continue
        by_symbol[e["symbol"]].append(e)

    resumo = []
    for symbol, lst in by_symbol.items():
        lst.sort(key=lambda x: x["ts"])
        primeiro = lst[0]
        ultimo = lst[-1]

        var_pct = None
        if primeiro["price"] is not None and ultimo["price"] is not None:
            var_pct = ((ultimo["price"] - primeiro["price"]) / primeiro["price"]) * 100

        count_hoje = sum(1 for e in lst if e["ts"].startswith(today_str))
        count_semana = 0
        for e in lst:
            dt = _parse_ts(e["ts"])
            if dt is not None and dt >= week_ago:
                count_semana += 1

        ultimo_dt = _parse_ts(ultimo["ts"])
        tempo_desde_ultimo = _format_delta(now - ultimo_dt) if ultimo_dt else "n/d"
        ultima_vez_fmt = ultimo_dt.strftime("%d/%m %H:%M") if ultimo_dt else ultimo["ts"]

        resumo.append({
            "symbol": symbol,
            "aparicoes": len(lst),          # total (mantido p/ compatibilidade)
            "count_hoje": count_hoje,
            "count_semana": count_semana,
            "count_all": len(lst),
            "primeiro_score": primeiro["score"],
            "ultimo_score": ultimo["score"],
            "primeiro_preco": primeiro["price"],
            "ultimo_preco": ultimo["price"],
            "var_pct": var_pct,
            "primeira_vez": primeiro["ts"],
            "ultima_vez": ultimo["ts"],
            "ultima_vez_fmt": ultima_vez_fmt,
            "tempo_desde_ultimo": tempo_desde_ultimo,
        })

    resumo.sort(key=lambda x: x["aparicoes"], reverse=True)
    return resumo


def format_telegram(resumo, titulo="Resumo de Alertas"):
    if not resumo:
        return f"📊 <b>{titulo}</b>\n\nNenhum alerta registrado ainda."

    # Tabela monoespaçada via <pre> pra leitura rápida (Telegram HTML parse_mode)
    col_symbol, col_num = 12, 5
    header = (
        f"{'Symbol':<{col_symbol}}{'Hoje':>{col_num}}{'7d':>{col_num}}"
        f"{'All':>{col_num}}{'Var%':>9}  Último / há quanto"
    )
    separador = "-" * len(header)

    lines = [f"📊 <b>{titulo}</b>", "", "<pre>", header, separador]
    for r in resumo[:20]:  # limita pra nao estourar mensagem
        var_txt = f"{r['var_pct']:+.2f}%" if r["var_pct"] is not None else "   n/d"
        lines.append(
            f"{r['symbol']:<{col_symbol}}{r['count_hoje']:>{col_num}}"
            f"{r['count_semana']:>{col_num}}{r['count_all']:>{col_num}}"
            f"{var_txt:>9}  {r['ultima_vez_fmt']} ({r['tempo_desde_ultimo']})"
        )
        lines.append("")  # linha em branco entre moedas
    lines.append("</pre>")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    entries = load_history()
    only_today = "--hoje" in sys.argv
    resumo = summarize(entries, only_today=only_today)
    titulo = "Resumo de Alertas (hoje)" if only_today else "Resumo de Alertas (todo histórico)"
    print(format_telegram(resumo, titulo))
