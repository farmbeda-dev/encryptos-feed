#!/usr/bin/env python3
"""
watchlist_manager.py
---------------------
Gerenciador de alarmes/watchlist em tempo real via Telegram (@hatrahaBot).

Roda em DOIS "mundos paralelos" — mesmos comandos, lógica equivalente, mas
CADA UM com seu próprio estado, fonte de preço e regras, sem se misturar:

  - vps.alerts (crypto)   — CRYPTO_CHAT_ID       — só o dono usa comandos
                             preço: liquidez-latest.json (Binance Futures mark)
                             SL 3% / TP1 50% ALAVANCADO (leverage padrão 3x)
                             contexto extra: funding, LSR, open interest

  - sell planner (stocks) — SELL_PLANNER_CHAT_ID — qualquer membro do grupo
                             preço: yfinance
                             SL 3% / TP opcional (sem alavancagem)
                             contexto extra: status do mercado NYSE/NASDAQ

Comandos (funcionam nos dois mundos, comportamento adaptado por chat):
  /watch ATIVO
  /position ATIVO entry=PRECO [side=long|short] [leverage=3] [tp=PRECO]
    (leverage só é usada no mundo crypto; no stock é ignorada)
  /unwatch ATIVO  (ou /close ATIVO)
  /alert ATIVO price=PRECO
  /list            — minha watchlist + posições (do mundo de onde eu chamei)
  /list_stock  (ou /liststock)  — os 15 stocks-alvo do sell_planner (STOCK_ALVOS)
  /history [SYMBOL] [--hoje]  — só crypto (dado só existe lá)
  /help

Diferente do alertas_engine.py (que roda sozinho 24/7 escaneando 530+ symbols)
e do monitor_stock.py (que monitora STOCK_ALVOS sozinho), este script só
reage a comando manual e mantém listas provisórias por usuário/chat.

Rodar como serviço systemd próprio. Usa Bot API (long-polling via
getUpdates), NÃO Telethon — comandos de barra exigem Bot API.
"""

import json
import os
import sys
import time
import re
import logging
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

# history_query.py precisa estar no mesmo diretório (/opt/encryptos/) que
# este script, ou em algum lugar do PYTHONPATH, pra esse import funcionar.
from history_query import load_history, summarize, format_telegram as format_history_telegram

# STOCK_ALVOS vive no sell_planner (diretório diferente) — importamos de lá
# em vez de duplicar os 15 tickers aqui, pra ter uma única fonte de verdade.
sys.path.insert(0, "/opt/sellplanner")
try:
    from monitor_stock import STOCK_ALVOS
except ImportError:
    STOCK_ALVOS = {}
    _stock_import_error = True
else:
    _stock_import_error = False

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("HATRAHA_BOT_TOKEN", "")

OWNER_USER_ID = 2063032651

CRYPTO_CHAT_ID = -1004412115335        # grupo vps.alerts
SELL_PLANNER_CHAT_ID = -1004298034731  # grupo sell planner

# Tópicos do sell planner (message_thread_id) — informativo/documentação.
# As respostas já respeitam automaticamente o tópico de onde o comando foi
# mandado (ver handle_update), não precisamos restringir comando por tópico.
SELL_PLANNER_TOPICS = {"stock": 37, "crypto": 42}

# "owner_only": True = só OWNER_USER_ID pode usar comandos nesse chat.
ALLOWED_CHATS = {
    CRYPTO_CHAT_ID: {"name": "vps.alerts", "owner_only": True},
    SELL_PLANNER_CHAT_ID: {"name": "sell planner", "owner_only": False},
}

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

STATE_DIR = "/opt/encryptos/state"
BOT_STATE_FILE = os.path.join(STATE_DIR, "bot_state.json")  # só o update_offset (compartilhado)

LIQUIDEZ_FEED = "/opt/encryptos/out/liquidez-latest.json"
CVD_FEED = "/opt/encryptos/out/cvd-latest.json"

POLL_TIMEOUT = 30  # segundos, long-polling do getUpdates

# --- regras de risco padrão (definidas pelo usuário) ---
SL_PCT = 0.03             # 3% de distância do entry (os dois mundos)
TP1_LEVERAGED_PCT = 0.50  # 50% de retorno ALAVANCADO no TP1 — só crypto
PROXIMITY_PCT = 0.01      # dispara alerta quando faltar 1% pro alvo
RESET_HYSTERESIS_PCT = 0.02  # só permite alertar de novo se afastar > 2%
ALERT_CHECK_INTERVAL = 60    # segundos entre checagens de proximidade

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [watchlist_manager] %(levelname)s: %(message)s",
)
log = logging.getLogger("watchlist_manager")

if _stock_import_error:
    log.warning(
        "Não consegui importar STOCK_ALVOS de /opt/sellplanner/monitor_stock.py "
        "— /list_stock vai responder vazio até isso ser corrigido."
    )


def is_stock_chat(chat_id):
    return chat_id == SELL_PLANNER_CHAT_ID


# ---------------------------------------------------------------------------
# STATE (persistência simples em JSON, um arquivo por chat — mundos separados)
# ---------------------------------------------------------------------------
# Estrutura do state (por chat):
# {
#   "watch": {"BTCUSDT": {"added_at": "..."}, ...},
#   "positions": {"BTCUSDT": {"entry": 65000.0, ...}, ...},
#   "price_alerts": {"BTCUSDT": [{"target": 70000, "alertado": False}]},
#   "last_thread_id": 37
# }

def state_path(chat_id):
    return os.path.join(STATE_DIR, f"watchlist_{chat_id}.json")


def load_state(chat_id):
    path = state_path(chat_id)
    if not os.path.exists(path):
        return {"watch": {}, "positions": {}, "price_alerts": {}, "last_thread_id": None}
    with open(path, "r") as f:
        return json.load(f)


def save_state(chat_id, state):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = state_path(chat_id)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, path)  # write atômico, evita corromper o arquivo


def load_bot_offset():
    if not os.path.exists(BOT_STATE_FILE):
        return 0
    with open(BOT_STATE_FILE, "r") as f:
        return json.load(f).get("update_offset", 0)


def save_bot_offset(offset):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp_path = BOT_STATE_FILE + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump({"update_offset": offset}, f)
    os.replace(tmp_path, BOT_STATE_FILE)


# ---------------------------------------------------------------------------
# FEED DE DADOS — mundo crypto (liquidez-latest.json)
# ---------------------------------------------------------------------------

def load_liquidez_feed():
    try:
        with open(LIQUIDEZ_FEED, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log.warning("Não consegui ler %s: %s", LIQUIDEZ_FEED, e)
        return None


def get_symbol_data(symbol):
    """
    O feed liquidez-latest.json é um DICT com chave "data", indexado por symbol.
    Formato real: {"timestamp": "...", "data": {"BTCUSDT": {...}, "ETHUSDT": {...}}}
    """
    feed = load_liquidez_feed()
    if feed is None:
        return None
    return feed.get("data", {}).get(symbol)


def get_current_price(symbol):
    """O preço atual fica em data[symbol]["book_futures"]["mark"]."""
    data = get_symbol_data(symbol)
    if data is None:
        return None

    mark = data.get("book_futures", {}).get("mark")
    if mark is not None:
        return float(mark)

    log.warning(
        "Não encontrei book_futures.mark para %s. Campos disponíveis: %s",
        symbol, list(data.keys())
    )
    return None


def get_market_context(symbol):
    """
    Funding, LSR (long/short ratio) e open interest do feed de liquidez, pra
    anexar nos alertas de proximidade CRYPTO. Não tem volume negociado bruto
    nesse feed — só profundidade de book (bid/ask USD), que é outra coisa.
    """
    data = get_symbol_data(symbol)
    if data is None:
        return ""

    partes = []

    funding = data.get("funding", {})
    if "rate" in funding:
        partes.append(f"Funding {funding['rate']*100:.4f}% ({funding.get('reading', '')})")

    lsr = data.get("lsr_top_accounts", {})
    if "ratio" in lsr:
        partes.append(f"LSR {lsr['ratio']} ({lsr.get('reading', '')})")

    oi = data.get("open_interest", {})
    if "usd_binance" in oi:
        oi_bi = oi["usd_binance"] / 1_000_000_000
        partes.append(f"OI ${oi_bi:.2f}B")

    return " | " + " | ".join(partes) if partes else ""


# ---------------------------------------------------------------------------
# FEED DE DADOS — mundo stock (yfinance)
# ---------------------------------------------------------------------------

def get_stock_price(ticker):
    """
    Preço via yfinance.fast_info (rápido, mas com poucos campos).
    Import fica dentro da função (lazy) pra não derrubar o bot inteiro na
    inicialização se yfinance não estiver instalado nesse ambiente.
    """
    try:
        import yfinance as yf
    except ImportError:
        log.error("yfinance não está instalado. Rode: pip install yfinance --break-system-packages")
        return None

    try:
        info = yf.Ticker(ticker).fast_info
        price = info.get("lastPrice") if hasattr(info, "get") else None
        if price is None:
            price = getattr(info, "last_price", None)
        return float(price) if price is not None else None
    except Exception as e:
        log.warning("Erro pegando preço via yfinance pra %s: %s", ticker, e)
        return None


def get_stock_extra_info(ticker):
    """
    marketCap, volume, sharesShort, shortRatio, shortPercentOfFloat via
    yf.Ticker(ticker).info (mais lento que fast_info, só usado sob demanda
    no /list_stock, não no checador periódico).
    Campos confirmados via diagnóstico real em 15/07/2026 — 11 dos 15
    tickers têm sharesShort/shortRatio; GLGDF, PHYS, PSLV, SRUUF não têm
    (PHYS/PSLV são trusts físicos, sem short interest tradicional; GLGDF/
    SRUUF são OTC finos demais pro Yahoo reportar).
    """
    try:
        import yfinance as yf
    except ImportError:
        return {}

    try:
        info = yf.Ticker(ticker).info or {}
    except Exception as e:
        log.warning("Erro pegando .info via yfinance pra %s: %s", ticker, e)
        return {}

    return {
        "market_cap": info.get("marketCap"),
        "volume": info.get("volume"),
        "avg_volume": info.get("averageVolume"),
        "shares_short": info.get("sharesShort"),
        "short_ratio": info.get("shortRatio"),
        "short_pct_float": info.get("shortPercentOfFloat"),
    }


def get_stock_market_status():
    """
    Status do NYSE/NASDAQ agora, mesma lógica de horários do
    market_hours_alert_v2.py (pre-market 04:00, open 09:30, close 16:00,
    after-hours até 20:00, horário de Nova York).
    """
    tz = ZoneInfo("America/New_York")
    now = datetime.now(tz)
    if now.weekday() >= 5:
        return "🔕 Fechado (fim de semana)"

    hm = now.strftime("%H:%M")
    if "04:00" <= hm < "09:30":
        return "🌅 Pre-market"
    elif "09:30" <= hm < "16:00":
        return "🔔 Mercado ABERTO"
    elif "16:00" <= hm < "20:00":
        return "🌙 After-hours"
    else:
        return "🔕 Fechado"


# ---------------------------------------------------------------------------
# HELPERS "MUNDO PARALELO" — escolhem a lógica certa conforme o chat
# ---------------------------------------------------------------------------

def get_price_for_chat(chat_id, symbol):
    return get_stock_price(symbol) if is_stock_chat(chat_id) else get_current_price(symbol)


def get_alert_context_for_chat(chat_id, symbol):
    return get_stock_market_status() if is_stock_chat(chat_id) else get_market_context(symbol)


def calc_sl_tp(entry, side, leverage):
    """
    Mundo CRYPTO. SL = SL_PCT de distância do entry. TP1 = TP1_LEVERAGED_PCT
    de retorno ALAVANCADO -> convertido pra distância real de preço
    dividindo pela leverage.
    """
    price_move_tp = TP1_LEVERAGED_PCT / leverage
    if side == "long":
        sl_price = entry * (1 - SL_PCT)
        tp_price = entry * (1 + price_move_tp)
    else:  # short
        sl_price = entry * (1 + SL_PCT)
        tp_price = entry * (1 - price_move_tp)
    return sl_price, tp_price


def calc_sl_stock(entry, side):
    """
    Mundo STOCK. Sem alavancagem: só SL de SL_PCT. TP não é calculado
    automaticamente (não temos regra alavancada aplicável) — se o usuário
    quiser TP, passa explicitamente tp=PRECO no /position.
    """
    if side == "long":
        return entry * (1 - SL_PCT)
    return entry * (1 + SL_PCT)


# ---------------------------------------------------------------------------
# ALERTAS DE PROXIMIDADE (SL / TP / EP / price-alvo) — por chat
# ---------------------------------------------------------------------------

def check_price_alerts(chat_id, state):
    """
    Roda periodicamente (ALERT_CHECK_INTERVAL), uma vez por chat. Compara o
    preço atual de cada posição contra SL, TP e o próprio entry (EP), e cada
    price_alert contra seu alvo. Dispara quando a distância cai a
    PROXIMITY_PCT ou menos. RESET_HYSTERESIS_PCT evita spam.
    """
    mensagens = []
    mudou = False

    for symbol, pos in state["positions"].items():
        current = get_price_for_chat(chat_id, symbol)
        if current is None:
            continue

        entry = pos["entry"]
        sl = pos.get("sl_price")
        tp = pos.get("tp_price")  # pode ser None no mundo stock sem tp= explícito
        alertado = pos.setdefault("alertado", {"sl": False, "tp": False, "ep": False})

        alvos = {"sl": (sl, "🔴 SL"), "tp": (tp, "🟢 TP"), "ep": (entry, "🔵 EP (entry)")}

        for label, (target, rotulo) in alvos.items():
            if target is None or target == 0:
                continue
            dist_pct = abs(current - target) / target

            if dist_pct <= PROXIMITY_PCT and not alertado[label]:
                mensagens.append(
                    f"⚠️ {symbol} perto de {rotulo}: atual {current} "
                    f"vs alvo {target:.6g} (dist {dist_pct*100:.2f}%)"
                    f"{get_alert_context_for_chat(chat_id, symbol)}"
                )
                alertado[label] = True
                mudou = True
            elif dist_pct > RESET_HYSTERESIS_PCT and alertado[label]:
                alertado[label] = False
                mudou = True

    # --- alertas de preço-alvo simples (/alert ATIVO price=X) ---
    for symbol, alerts in state.get("price_alerts", {}).items():
        current = get_price_for_chat(chat_id, symbol)
        if current is None:
            continue
        for alerta in alerts:
            target = alerta["target"]
            if target == 0:
                continue
            dist_pct = abs(current - target) / target

            if dist_pct <= PROXIMITY_PCT and not alerta["alertado"]:
                mensagens.append(
                    f"🔔 {symbol} perto do alvo {target}: atual {current} "
                    f"(dist {dist_pct*100:.2f}%)"
                    f"{get_alert_context_for_chat(chat_id, symbol)}"
                )
                alerta["alertado"] = True
                mudou = True
            elif dist_pct > RESET_HYSTERESIS_PCT and alerta["alertado"]:
                alerta["alertado"] = False
                mudou = True

    if mudou:
        state["_mudou"] = True

    return mensagens


# ---------------------------------------------------------------------------
# TELEGRAM API HELPERS
# ---------------------------------------------------------------------------

def tg_get_updates(offset):
    resp = requests.get(
        f"{API_BASE}/getUpdates",
        params={"offset": offset, "timeout": POLL_TIMEOUT},
        timeout=POLL_TIMEOUT + 10,
    )
    resp.raise_for_status()
    return resp.json().get("result", [])


def set_bot_commands():
    """
    Registra os comandos no menu nativo do Telegram (botão '/' do teclado).
    Só comandos SEM argumento — tocar numa sugestão do menu ENVIA na hora,
    sem deixar digitar o resto.
    """
    commands = [
        {"command": "list", "description": "Minha watchlist + posições (crypto ou stock, conforme o chat)"},
        {"command": "list_stock", "description": "Os 15 stocks-alvo do sell_planner"},
        {"command": "liststock", "description": "Mesmo que /list_stock"},
        {"command": "help", "description": "Lista de comandos e como usar cada um"},
    ]
    try:
        resp = requests.post(
            f"{API_BASE}/setMyCommands", json={"commands": commands}, timeout=10
        )
        if resp.ok and resp.json().get("ok"):
            log.info("Comandos registrados no menu do Telegram.")
        else:
            log.warning("Falha ao registrar comandos no menu: %s", resp.text)
    except requests.RequestException as e:
        log.warning("Erro ao registrar comandos no menu: %s", e)


def tg_send_message(text, chat_id, parse_mode=None, message_thread_id=None):
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:  # None = manda texto puro, sem parse_mode (evita erro de parsing)
        payload["parse_mode"] = parse_mode
    if message_thread_id is not None:
        payload["message_thread_id"] = message_thread_id

    resp = requests.post(f"{API_BASE}/sendMessage", json=payload, timeout=10)
    if not resp.ok:
        log.error("Falha ao enviar mensagem: %s", resp.text)


# ---------------------------------------------------------------------------
# COMANDOS — comuns aos dois mundos (watch/unwatch/position/alert/list)
# ---------------------------------------------------------------------------

SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}USDT$")   # crypto
TICKER_RE = re.compile(r"^[A-Z]{1,6}$")            # stock


def normalize_symbol(raw):
    """Mundo crypto: normaliza pra XXXUSDT."""
    s = raw.strip().upper()
    if not s.endswith("USDT"):
        s += "USDT"
    return s


def normalize_ticker(raw):
    """Mundo stock: só maiúsculo, sem sufixo nenhum."""
    return raw.strip().upper()


def normalize_asset(chat_id, raw):
    if is_stock_chat(chat_id):
        return normalize_ticker(raw)
    return normalize_symbol(raw)


def asset_valido(chat_id, asset):
    if is_stock_chat(chat_id):
        return bool(TICKER_RE.match(asset))
    return bool(SYMBOL_RE.match(asset))


def cmd_watch(state, args, chat_id):
    if not args:
        return "Uso: /watch ATIVO (ex: /watch BTC no crypto, /watch AG no stock)"
    asset = normalize_asset(chat_id, args[0])
    if not asset_valido(chat_id, asset):
        return f"Ativo inválido: {asset}"

    state["watch"][asset] = {"added_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    return f"👁️ Adicionado à watchlist: {asset}"


def cmd_unwatch(state, args, chat_id):
    if not args:
        return "Uso: /unwatch ATIVO"
    asset = normalize_asset(chat_id, args[0])

    removed = False
    if asset in state["watch"]:
        del state["watch"][asset]
        removed = True
    if asset in state["positions"]:
        del state["positions"][asset]
        removed = True
    if asset in state.get("price_alerts", {}):
        del state["price_alerts"][asset]
        removed = True

    if removed:
        return f"🗑️ Removido: {asset}"
    return f"{asset} não estava na lista."


def cmd_position(state, args, chat_id):
    # crypto: /position SYMBOL entry=65000 [side=long|short] [leverage=3]
    # stock:  /position TICKER entry=9.40 [side=long|short] [tp=PRECO]
    if not args:
        if is_stock_chat(chat_id):
            return "Uso: /position TICKER entry=PRECO [side=long|short] [tp=PRECO]"
        return "Uso: /position SYMBOL entry=PRECO [side=long|short] [leverage=3]"

    asset = normalize_asset(chat_id, args[0])
    if not asset_valido(chat_id, asset):
        return f"Ativo inválido: {asset}"

    entry_price = None
    side = "long"       # default — avisa no reply pra poder corrigir se for short
    leverage = 3.0       # default — só usado no mundo crypto
    tp_explicito = None  # só usado no mundo stock

    for arg in args[1:]:
        low = arg.lower()
        if low.startswith("entry="):
            try:
                entry_price = float(arg.split("=", 1)[1])
            except ValueError:
                return f"Preço de entrada inválido: {arg}"
        elif low.startswith("side="):
            side = low.split("=", 1)[1]
            if side not in ("long", "short"):
                return f"side inválido: {side} (use long ou short)"
        elif low.startswith("leverage="):
            try:
                leverage = float(arg.split("=", 1)[1])
            except ValueError:
                return f"leverage inválida: {arg}"
        elif low.startswith("tp="):
            try:
                tp_explicito = float(arg.split("=", 1)[1])
            except ValueError:
                return f"tp inválido: {arg}"

    if entry_price is None:
        return "Faltou o entry=PRECO. Ex: /position ATIVO entry=65000"

    aviso_side = "" if any(a.lower().startswith("side=") for a in args[1:]) else " (assumindo LONG — use side=short se for venda)"

    if is_stock_chat(chat_id):
        sl_price = calc_sl_stock(entry_price, side)
        tp_price = tp_explicito  # None se o usuário não passou tp=
        state["positions"][asset] = {
            "entry": entry_price,
            "side": side,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "alertado": {"sl": False, "tp": False, "ep": False},
        }
        tp_txt = f"\n🟢 TP: {tp_price:.6g}" if tp_price is not None else "\n(sem TP definido — passe tp=PRECO se quiser um alvo)"
        return (
            f"📌 Posição registrada: {asset} @ entry {entry_price} "
            f"({side.upper()}){aviso_side}\n"
            f"🔴 SL: {sl_price:.6g} (−3%){tp_txt}"
        )

    if leverage <= 0:
        return "leverage precisa ser maior que 0."
    sl_price, tp_price = calc_sl_tp(entry_price, side, leverage)
    state["positions"][asset] = {
        "entry": entry_price,
        "side": side,
        "leverage": leverage,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "alertado": {"sl": False, "tp": False, "ep": False},
    }
    return (
        f"📌 Posição registrada: {asset} @ entry {entry_price} "
        f"({side.upper()}, {leverage}x){aviso_side}\n"
        f"🔴 SL: {sl_price:.6g} (−3%)\n"
        f"🟢 TP1: {tp_price:.6g} (+50% alavancado)"
    )


def cmd_alert(state, args, chat_id):
    if len(args) < 2:
        return "Uso: /alert ATIVO price=PRECO (ex: /alert BTC price=70000)"

    asset = normalize_asset(chat_id, args[0])
    if not asset_valido(chat_id, asset):
        return f"Ativo inválido: {asset}"

    price = None
    for arg in args[1:]:
        if arg.lower().startswith("price="):
            try:
                price = float(arg.split("=", 1)[1])
            except ValueError:
                return f"Preço inválido: {arg}"

    if price is None:
        return "Faltou o price=PRECO. Ex: /alert ATIVO price=70000"

    state.setdefault("price_alerts", {}).setdefault(asset, []).append(
        {"target": price, "alertado": False}
    )
    return (
        f"🔔 Alerta criado: {asset} @ {price} "
        f"(dispara quando faltar 1% pra esse preço)"
    )


def format_pnl_line(chat_id, symbol, pos):
    entry_price = pos["entry"]
    current = get_price_for_chat(chat_id, symbol)
    if current is None:
        return f"{symbol} — entry {entry_price} — preço atual indisponível"

    pnl_pct = ((current - entry_price) / entry_price) * 100
    sign = "🟢" if pnl_pct >= 0 else "🔴"

    sl = pos.get("sl_price")
    tp = pos.get("tp_price")
    extra = ""
    if sl is not None:
        extra += f" | SL {sl:.6g}"
    if tp is not None:
        extra += f" · TP {tp:.6g}"

    return (
        f"{sign} {symbol} — entry {entry_price} → atual {current} "
        f"({pnl_pct:+.2f}%){extra}"
    )


def format_watch_line(chat_id, symbol):
    current = get_price_for_chat(chat_id, symbol)
    if current is None:
        return f"👁️ {symbol} — preço atual indisponível"
    return f"👁️ {symbol} — atual {current}"


def cmd_list(state, args, chat_id):
    mundo = "stock" if is_stock_chat(chat_id) else "crypto"
    lines = [f"Minha lista ({mundo}):", ""]

    if state["positions"]:
        lines.append("Posições:")
        lines.append("")
        for symbol, info in state["positions"].items():
            lines.append(format_pnl_line(chat_id, symbol, info))
            lines.append("")

    if state["watch"]:
        lines.append("Watchlist:")
        lines.append("")
        for symbol in state["watch"]:
            lines.append(format_watch_line(chat_id, symbol))
            lines.append("")

    if not state["positions"] and not state["watch"]:
        lines.append("Nada sendo monitorado no momento.")

    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# COMANDO — lista fixa de stocks-alvo (STOCK_ALVOS, do monitor_stock.py)
# ---------------------------------------------------------------------------

def stock_row_data(ticker, alvo):
    """Monta uma linha de dados (tupla de strings já formatadas) pra tabela."""
    breakeven = alvo["breakeven"]
    tps = sorted(alvo["tps"])

    current = get_stock_price(ticker)
    if current is None:
        return (ticker, "n/d", "n/d", "n/d", "n/d", "n/d", "n/d")

    pnl_pct = ((current - breakeven) / breakeven) * 100

    proximo_tp = next((tp for tp in tps if tp > current), None)
    tp_txt = f"{proximo_tp:g}" if proximo_tp is not None else "OK 🎯"

    extra = get_stock_extra_info(ticker)
    vol_txt = f"{extra['volume']/1_000_000:.2f}M" if extra.get("volume") is not None else "-"
    mcap_txt = f"${extra['market_cap']/1_000_000_000:.2f}B" if extra.get("market_cap") is not None else "-"
    short_txt = str(extra["short_ratio"]) if extra.get("short_ratio") is not None else "n/d"

    return (
        ticker,
        f"{current:g}",
        f"{pnl_pct:+.1f}%",
        tp_txt,
        vol_txt,
        mcap_txt,
        short_txt,
    )


def cmd_list_stock(state, args, chat_id):
    if not STOCK_ALVOS:
        return (
            "⚠️ STOCK_ALVOS não carregado — confirme se "
            "/opt/sellplanner/monitor_stock.py existe e está acessível."
        )

    col_widths = [7, 8, 7, 8, 7, 7, 7]
    headers = ["Ticker", "Atual", "PnL%", "PróxTP", "Vol", "MCap", "ShortR"]

    def fmt_row(vals):
        return "".join(f"{v:<{w}}" for v, w in zip(vals, col_widths))

    header_line = fmt_row(headers)
    separador = "-" * len(header_line)

    linhas_tabela = [header_line, separador]
    for ticker, alvo in STOCK_ALVOS.items():
        linhas_tabela.append(fmt_row(stock_row_data(ticker, alvo)))

    texto = (
        f"Status do mercado (NYSE/NASDAQ): {get_stock_market_status()}\n\n"
        f"<pre>\n" + "\n".join(linhas_tabela) + "\n</pre>"
    )
    return texto, "HTML"


# ---------------------------------------------------------------------------
# COMANDOS — histórico e ajuda
# ---------------------------------------------------------------------------

def cmd_history(state, args, chat_id):
    """
    /history [SYMBOL] [--hoje]. Só mundo crypto — reaproveita history_query.py
    (lê /opt/encryptos/alertas/history.jsonl, gravado pelo alertas_engine.py).
    """
    only_today = "--hoje" in args
    symbol_filter = None
    for arg in args:
        if arg != "--hoje":
            symbol_filter = normalize_symbol(arg)

    entries = load_history()
    resumo = summarize(entries, symbol_filter=symbol_filter, only_today=only_today)

    titulo = "Resumo de Alertas"
    if symbol_filter:
        titulo += f" — {symbol_filter}"
    if only_today:
        titulo += " (hoje)"

    texto = format_history_telegram(resumo, titulo)
    return texto, "HTML"


def cmd_help(state, args, chat_id):
    mundo = "stock" if is_stock_chat(chat_id) else "crypto"
    linhas = [
        f"Comandos disponíveis (você está no mundo: {mundo}):",
        "",
        "/watch ATIVO — adiciona à watchlist",
        "/unwatch ATIVO (ou /close ATIVO) — remove da watchlist ou fecha posição",
    ]
    if is_stock_chat(chat_id):
        linhas.append(
            "/position TICKER entry=PRECO [side=long|short] [tp=PRECO] — "
            "registra posição, SL fixo em -3%, TP só se você informar"
        )
    else:
        linhas.append(
            "/position SYMBOL entry=PRECO [side=long|short] [leverage=3] — "
            "registra posição, calcula SL (−3%) e TP1 (+50% alavancado)"
        )
    linhas += [
        "/alert ATIVO price=PRECO — alerta simples quando o preço chegar perto (1%)",
        "/list — minha watchlist + posições nesse mundo",
        "/list_stock (ou /liststock) — os 15 stocks-alvo do sell_planner",
    ]
    if not is_stock_chat(chat_id):
        linhas.append(
            "/history [SYMBOL] [--hoje] — resumo de alertas do alertas_engine "
            "(contagem hoje/7d/all-time)"
        )
    linhas += [
        "/help — esta lista",
        "",
        "Alertas de proximidade (SL/TP/EP a 1%) rodam sozinhos a cada 60s, "
        "sem precisar de comando.",
    ]
    return "\n".join(linhas)


COMMANDS = {
    "/watch": cmd_watch,
    "/unwatch": cmd_unwatch,
    "/close": cmd_unwatch,          # alias mais intuitivo pra fechar posição
    "/position": cmd_position,
    "/alert": cmd_alert,
    "/list": cmd_list,
    "/listcrypto": cmd_list,        # alias
    "/list_stock": cmd_list_stock,
    "/liststock": cmd_list_stock,   # alias sem underscore
    "/history": cmd_history,
    "/help": cmd_help,
    "/start": cmd_help,             # alias — Telegram costuma mandar /start em bots novos
}

# /history depende de um arquivo que só existe pro mundo crypto
# (history.jsonl do alertas_engine.py) — não existe equivalente pra stock.
CRYPTO_ONLY_COMMANDS = {"/history"}


# ---------------------------------------------------------------------------
# LOOP PRINCIPAL
# ---------------------------------------------------------------------------

def parse_command(text):
    """Separa comando (com possível @BotName) dos argumentos."""
    parts = text.strip().split()
    if not parts:
        return None, []
    cmd = parts[0].split("@")[0].lower()
    return cmd, parts[1:]


def handle_update(update, states):
    """`states` é um dict {chat_id: state} com um state por chat autorizado."""
    message = update.get("message")
    if not message or "text" not in message:
        return

    chat_id = message.get("chat", {}).get("id")
    chat_cfg = ALLOWED_CHATS.get(chat_id)
    if chat_cfg is None:
        return  # chat não autorizado, ignora

    sender_id = message.get("from", {}).get("id")
    if chat_cfg["owner_only"] and sender_id != OWNER_USER_ID:
        log.warning(
            "Comando ignorado de user_id não autorizado no chat %s (%s): %s (texto: %r)",
            chat_id, chat_cfg["name"], sender_id, message.get("text"),
        )
        return  # silencioso — não confirma nem nega, evita enumeração

    state = states[chat_id]

    # Se o grupo tem Tópicos ativados, precisa responder no mesmo thread,
    # senão a resposta cai no tópico "Geral" e some do radar do usuário.
    thread_id = message.get("message_thread_id")
    if thread_id is not None:
        state["last_thread_id"] = thread_id

    cmd, args = parse_command(message["text"])
    handler = COMMANDS.get(cmd)
    if handler is None:
        return  # não é um comando nosso, ignora

    if cmd in CRYPTO_ONLY_COMMANDS and chat_id != CRYPTO_CHAT_ID:
        tg_send_message(
            f"⚠️ {cmd} só existe no mundo crypto (o dado vem do alertas_engine, "
            f"que não tem equivalente pra stock ainda). Use no vps.alerts.",
            chat_id=chat_id, message_thread_id=thread_id,
        )
        return

    parse_mode = None  # texto puro por padrão — Markdown quebrava com
                        # asterisco/underscore desencontrado em símbolos
    try:
        result = handler(state, args, chat_id)
        if isinstance(result, tuple):
            reply, parse_mode = result
        else:
            reply = result
    except Exception as e:
        log.exception("Erro ao processar comando %s", cmd)
        reply = f"⚠️ Erro ao processar {cmd}: {e}"
        parse_mode = None

    tg_send_message(reply, chat_id=chat_id, parse_mode=parse_mode, message_thread_id=thread_id)
    state["_mudou"] = True


def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "HATRAHA_BOT_TOKEN não definido no ambiente. "
            "Configure a env var antes de subir o serviço."
        )

    states = {chat_id: load_state(chat_id) for chat_id in ALLOWED_CHATS}
    offset = load_bot_offset()
    log.info(
        "watchlist_manager iniciado. Offset atual: %s. Chats: %s",
        offset, {cid: cfg["name"] for cid, cfg in ALLOWED_CHATS.items()},
    )
    set_bot_commands()

    last_alert_check = 0.0

    while True:
        try:
            updates = tg_get_updates(offset)
        except requests.RequestException as e:
            log.warning("Erro no getUpdates, tentando de novo em 5s: %s", e)
            time.sleep(5)
            continue

        for update in updates:
            handle_update(update, states)
            offset = update["update_id"] + 1

        if updates:
            save_bot_offset(offset)

        for chat_id, state in states.items():
            if state.pop("_mudou", False):
                save_state(chat_id, state)

        now = time.time()
        if now - last_alert_check >= ALERT_CHECK_INTERVAL:
            last_alert_check = now
            for chat_id, state in states.items():
                try:
                    for msg in check_price_alerts(chat_id, state):
                        tg_send_message(
                            msg, chat_id=chat_id,
                            message_thread_id=state.get("last_thread_id"),
                        )
                    if state.pop("_mudou", False):
                        save_state(chat_id, state)
                except Exception:
                    log.exception(
                        "Erro na checagem periódica de alertas (chat %s)", chat_id
                    )


if __name__ == "__main__":
    main()
