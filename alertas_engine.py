#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ENCRYPTOS -- alertas_engine.py (v6)

Novidades desta versao:
- Forca relativa vs BTC via par direto XXXBTC (ex: ETHBTC), nao aproximacao
- Filtro de OI minimo (do conhecimento dos videos: <5-10M = risco alto)
- Historico agora grava topico + link direto pra mensagem no Telegram
- Horario sempre em Jerusalem, sem escrever a palavra "Jerusalem"
- Header sem "RADAR ENCRYPTOS", so score + resultado (texto puro, sem HTML)
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

FEED_PATH = "/opt/encryptos/out/liquidez-latest.json"
CVD_PATH = "/opt/encryptos/out/cvd-latest.json"
STATE_DIR = "/opt/encryptos/alertas/state"
STATE_FILE = os.path.join(STATE_DIR, "previous_snapshot.json")
COOLDOWN_FILE = os.path.join(STATE_DIR, "cooldown.json")
DELIST_STATE_FILE = os.path.join(STATE_DIR, "known_symbols.json")
HISTORY_FILE = "/opt/encryptos/alertas/history.jsonl"

COOLDOWN_HOURS = 6
SCORE_THRESHOLD = 70
COILED_PRICE_MAX_PCT = 1.0
COILED_OI_MIN_PCT = 3.0
OI_MINIMO_USD = 8_000_000  # do video 5: abaixo disso, risco de manipulacao alto

TELEGRAM_BOT_TOKEN = os.environ.get("ALERTAS_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("ALERTAS_CHAT_ID", "")
TOPIC_ALTA_CONVICCAO = os.environ.get("ALERTAS_THREAD_ALTA", "967")
TOPIC_RADAR_GERAL = os.environ.get("ALERTAS_THREAD_GERAL", "1")
TOPIC_COILED = os.environ.get("ALERTAS_THREAD_COILED", "1106")
TOPIC_DELISTING = os.environ.get("ALERTAS_THREAD_DELIST", TOPIC_RADAR_GERAL)

DRY_RUN = os.environ.get("ALERTAS_DRY_RUN", "1") == "1"
FUNDING_HOURS_UTC = [0, 8, 16]
JERUSALEM_TZ = ZoneInfo("Asia/Jerusalem")
FAPI = "https://fapi.binance.com"
HTTP_HEADERS = {"User-Agent": "encryptos-alertas/6.0"}


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{ts} {msg}")


def load_json(path):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def append_history(entry):
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def format_jerusalem_time(iso_ts):
    """So a hora, sem escrever 'Jerusalem'."""
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return "n/d"
    dt_jeru = dt.astimezone(JERUSALEM_TZ)
    return dt_jeru.strftime("%Y-%m-%d %H:%M:%S")


def next_funding_time():
    now = datetime.now(timezone.utc)
    candidates = []
    for h in FUNDING_HOURS_UTC:
        candidate = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        candidates.append(candidate)
    nxt = min(candidates)
    nxt_jeru = nxt.astimezone(JERUSALEM_TZ)
    delta = nxt - now
    hours = delta.seconds // 3600
    minutes = (delta.seconds % 3600) // 60
    return f"{nxt_jeru.strftime('%H:%M')} (em {hours}h{minutes:02d}m)"


def _get(url):
    req = urllib.request.Request(url, headers=HTTP_HEADERS)
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


def get_btc_pair_strength(symbol):
    """
    Forca relativa REAL via par direto XXXBTC (ex: ETHBTC), nao aproximacao.
    So funciona pra ativos que tem par direto contra BTC na Binance.
    Retorna variacao % do par BTC nas ultimas 4h (klines).
    """
    base = symbol.replace("USDT", "")
    if base == "BTC":
        return None  # BTC nao compara contra si mesmo
    pair = f"{base}BTC"
    try:
        url = f"{FAPI}/fapi/v1/klines?symbol={pair}&interval=1h&limit=4"
        klines = _get(url)
        if len(klines) < 2:
            return None
        open_price = float(klines[0][1])
        close_price = float(klines[-1][4])
        if open_price == 0:
            return None
        variacao_pct = ((close_price - open_price) / open_price) * 100
        return round(variacao_pct, 3)
    except Exception:
        return None


def calcular_ema(precos, periodo):
    """EMA classica. precos = lista de closes, mais antigo primeiro."""
    if len(precos) < periodo:
        return None
    k = 2 / (periodo + 1)
    ema = sum(precos[:periodo]) / periodo
    for preco in precos[periodo:]:
        ema = preco * k + ema * (1 - k)
    return ema


def calcular_rsi(precos, periodo=14):
    """RSI classico de Wilder."""
    if len(precos) < periodo + 1:
        return None
    ganhos, perdas = [], []
    for i in range(1, len(precos)):
        delta = precos[i] - precos[i - 1]
        ganhos.append(max(delta, 0))
        perdas.append(max(-delta, 0))
    avg_gain = sum(ganhos[:periodo]) / periodo
    avg_loss = sum(perdas[:periodo]) / periodo
    for i in range(periodo, len(ganhos)):
        avg_gain = (avg_gain * (periodo - 1) + ganhos[i]) / periodo
        avg_loss = (avg_loss * (periodo - 1) + perdas[i]) / periodo
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


TIMEFRAMES_TECNICOS = ["5m", "15m", "30m", "1h", "4h", "1d"]


def get_ema_rsi_um_timeframe(symbol, interval, limit=150):
    """Um kline call por timeframe -- calcula RSI + as 4 EMAs de uma vez."""
    try:
        url = f"{FAPI}/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
        klines = _get(url)
        closes = [float(k[4]) for k in klines]
        highs = [float(k[2]) for k in klines]
        if len(closes) < 20:
            return None
        return {
            "ema7": calcular_ema(closes, 7),
            "ema29": calcular_ema(closes, 29),
            "ema46": calcular_ema(closes, 46),
            "ema99": calcular_ema(closes, 99) if len(closes) >= 99 else None,
            "rsi14": calcular_rsi(closes, 14),
            "ath_periodo": max(highs),
        }
    except Exception:
        return None


def get_ema_rsi_multi(symbol):
    """Roda get_ema_rsi_um_timeframe para os 6 timeframes definidos."""
    resultado = {}
    for tf in TIMEFRAMES_TECNICOS:
        r = get_ema_rsi_um_timeframe(symbol, tf)
        if r:
            resultado[tf] = r
    return resultado


def get_daily_ohlc(symbol):
    try:
        url = f"{FAPI}/fapi/v1/klines?symbol={symbol}&interval=1d&limit=1"
        klines = _get(url)
        if not klines:
            return None, None
        low = float(klines[0][3])
        high = float(klines[0][2])
        return low, high
    except Exception:
        return None, None


def score_symbol(symbol, current, previous_data, cvd_data):
    score = 0
    reasons = []
    snap = {}
    coiled = False

    funding = current.get("funding", {})
    lsr = current.get("lsr_top_accounts", {})
    oi = current.get("open_interest", {})
    book = current.get("book_futures", {})

    fr = funding.get("rate", 0)
    ratio = lsr.get("ratio")
    lsr_period = lsr.get("period", "n/d")
    pressure = book.get("pressure", {})
    bid_pct = pressure.get("bid_pct_support", 0)
    ask_pct = pressure.get("ask_pct_resistance", 0)
    oi_usd = oi.get("usd_binance")
    price = book.get("mark")

    snap["price"] = price
    snap["oi_usd"] = oi_usd
    snap["lsr_ratio"] = ratio
    snap["lsr_period"] = lsr_period
    snap["wall_below"] = book.get("nearest_wall_below", {})
    snap["wall_above"] = book.get("nearest_wall_above", {})

    # Filtro de OI minimo (video 5) -- so aviso, nao descarta o alerta
    if oi_usd is not None and oi_usd < OI_MINIMO_USD:
        reasons.append(f"⚠️ OI baixo (${oi_usd:,.0f}, abaixo de ${OI_MINIMO_USD:,.0f}) -- risco de manipulacao, confirma com mais cuidado")

    if fr < 0:
        score += 25
        reasons.append(f"Funding negativo ({fr:.6f}) = shorts pagando, alta real")

    if ratio is not None and ratio < 1:
        score += 25
        reasons.append(f"LSR {ratio:.3f} < 1, short dominante = combustivel squeeze")

    if bid_pct > ask_pct and bid_pct >= 55:
        score += 20
        reasons.append(f"Book pressure bid {bid_pct:.1f}% vs ask {ask_pct:.1f}%, suporte real")

    oi_change_pct = None
    price_change_pct = None
    if previous_data:
        prev_oi = previous_data.get("open_interest", {}).get("usd_binance")
        if prev_oi and prev_oi > 0 and oi_usd:
            oi_change_pct = ((oi_usd - prev_oi) / prev_oi) * 100
            if oi_change_pct > 0.5:
                score += 15
                reasons.append(f"OI subindo {oi_change_pct:.2f}% desde ultima leitura, capital entrando")
        prev_price = previous_data.get("book_futures", {}).get("mark")
        if prev_price and prev_price > 0 and price:
            price_change_pct = ((price - prev_price) / prev_price) * 100
    else:
        reasons.append("OI trend: n/d (primeira leitura, sem historico ainda)")

    snap["oi_change_pct"] = oi_change_pct

    large_orders = book.get("large_orders", [])
    bid_whales = [o for o in large_orders if o.get("side") == "bid"]
    whale_usd = sum(o.get("notional_usd", 0) for o in bid_whales)
    if bid_whales:
        if whale_usd >= 500_000:
            score += 15
            reasons.append(f"${whale_usd:,.0f} em ordens grandes de compra no book")
        reasons.append("🐋 Whale order detectada SOMENTE em futuros (sem confirmacao spot)")

    cvd_veto = False
    cvd_info = None
    if cvd_data and symbol in cvd_data.get("data", {}):
        c = cvd_data["data"][symbol]
        cvd_info = c
        divergence = c.get("divergence")
        if divergence == "preco_sobe_cvd_desce_atencao":
            cvd_veto = True
            reasons.append("🚫 CVD diverge do preco (venda disfarcada) -- ALERTA DESCARTADO")
        elif c.get("cvd_delta", 0) > 0:
            reasons.append(f"CVD positivo ({c['cvd_delta']:,.0f}), agressao compradora confirma")
    snap["cvd_info"] = cvd_info

    if (
        oi_change_pct is not None and oi_change_pct >= COILED_OI_MIN_PCT
        and price_change_pct is not None and abs(price_change_pct) <= COILED_PRICE_MAX_PCT
        and fr < 0
    ):
        coiled = True

    return score, reasons, snap, cvd_veto, coiled


def load_cooldown():
    return load_json(COOLDOWN_FILE) or {}


def save_cooldown(cooldown):
    save_json(COOLDOWN_FILE, cooldown)


def is_in_cooldown(key, cooldown):
    last = cooldown.get(key)
    if not last:
        return False
    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 3600
    return elapsed < COOLDOWN_HOURS


def mark_cooldown(key, cooldown):
    cooldown[key] = datetime.now(timezone.utc).isoformat()


def send_telegram(text, thread_id):
    """Retorna (sucesso, message_id) -- message_id serve pra montar link direto."""
    if DRY_RUN:
        log(f"[DRY_RUN] Mensagem que seria enviada:\n{text}\n")
        return True, None
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("ERRO: ALERTAS_BOT_TOKEN ou ALERTAS_CHAT_ID nao configurados.")
        return False, None

    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}
    if thread_id:
        payload["message_thread_id"] = thread_id
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read().decode("utf-8"))
            if not resp.get("ok"):
                log(f"ERRO Telegram: {resp}")
                return False, None
            message_id = resp.get("result", {}).get("message_id")
            return True, message_id
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        log(f"ERRO Telegram (HTTP {e.code}): {body}")
        return False, None
    except urllib.error.URLError as e:
        log(f"ERRO ao enviar Telegram: {e}")
        return False, None


def build_message_link(chat_id, message_id):
    chat_id_str = str(chat_id)
    if chat_id_str.startswith("-100"):
        chat_id_clean = chat_id_str[4:]
    elif chat_id_str.startswith("-"):
        chat_id_clean = chat_id_str[1:]
    else:
        chat_id_clean = chat_id_str
    return f"https://t.me/c/{chat_id_clean}/{message_id}"


def pick_thread(score, coiled):
    if coiled:
        return TOPIC_COILED
    if score >= 85:
        return TOPIC_ALTA_CONVICCAO
    return TOPIC_RADAR_GERAL


def format_alert(symbol, score, reasons, snap, timestamp, coiled, btc_pair_pct, tecnicos=None, btc_tps=None):
    price = snap.get("price")
    wall_below = snap.get("wall_below") or {}
    wall_above = snap.get("wall_above") or {}
    oi_usd = snap.get("oi_usd")
    lsr_ratio = snap.get("lsr_ratio")
    lsr_period = snap.get("lsr_period")
    cvd_info = snap.get("cvd_info")
    daily_low = snap.get("daily_low")
    daily_high = snap.get("daily_high")

    header = "COMPRIMIDO -- pode nao ter explodido ainda" if coiled else "RADAR"

    lines = [
        f"{header} -- {symbol}",
        "",
        f"Score: {score}/100 | Preço: ${price}",
    ]
    if daily_low or daily_high:
        lines.append(f"Low/High diário: ${daily_low} / ${daily_high}")

    lines += [
        "",
        f"Última leitura: {format_jerusalem_time(timestamp)}",
        f"Próximo funding: {next_funding_time()}",
        "",
    ]

    oi_txt = f"${oi_usd:,.0f}" if oi_usd else "n/d"
    lines.append(f"OI (Binance): {oi_txt}")

    lsr_txt = f"{lsr_ratio:.3f}" if lsr_ratio is not None else "n/d"
    lines.append(f"LSR: {lsr_txt} (período {lsr_period})")

    if btc_pair_pct is not None:
        direcao = "força vs BTC" if btc_pair_pct > 0 else "fraqueza vs BTC"
        lines.append(f"Par BTC (4h): {btc_pair_pct:+.2f}% ({direcao})")
    else:
        lines.append("Par BTC: n/d (sem par direto ou dado indisponível)")

    if cvd_info:
        cvd_delta = cvd_info.get("cvd_delta")
        tps = cvd_info.get("trades_per_second", "n/d")
        window = cvd_info.get("window_minutes", 15)
        cvd_txt = f"{cvd_delta:,.0f}" if isinstance(cvd_delta, (int, float)) else "n/d"
        lines.append(f"CVD Futuros ({window}min): {cvd_txt}")
        lines.append(f"Trades/seg (média {window}min): {tps}")
    else:
        lines.append("CVD Futuros: n/d")
        lines.append("Trades/seg: n/d")

    if btc_tps is not None:
        lines.append(f"Trades/seg BTC (referencia): {btc_tps}")

    if tecnicos:
        lines.append("")
        lines.append("Técnicos por timeframe:")
        for tf, dados in tecnicos.items():
            ema_txt = []
            for periodo in ["ema7", "ema29", "ema46", "ema99"]:
                v = dados.get(periodo)
                if v:
                    ema_txt.append(f"{periodo.upper()}=${v:.6g}")
            rsi_txt = f"RSI={dados['rsi14']}" if dados.get("rsi14") is not None else ""
            linha = f"  {tf}: " + " | ".join(ema_txt + ([rsi_txt] if rsi_txt else []))
            lines.append(linha)

        # ATH usa o timeframe mais longo disponivel (1d, senao 4h)
        ath_ref = tecnicos.get("1d") or tecnicos.get("4h")
        if ath_ref and ath_ref.get("ath_periodo") and price:
            dist_ath = ((ath_ref["ath_periodo"] - price) / ath_ref["ath_periodo"]) * 100
            lines.append(f"Distância do topo recente: -{dist_ath:.1f}%")

    lines.append("")

    if wall_below.get("price"):
        lines.append(f"Suporte: ${wall_below['price']} (${wall_below.get('notional_usd', 0):,.0f})")
    if wall_above.get("price"):
        lines.append(f"Resistência: ${wall_above['price']} (${wall_above.get('notional_usd', 0):,.0f})")
    if wall_below.get("price") or wall_above.get("price"):
        lines.append("")

    lines.append("Confluência detectada:")
    lines.append("")
    for r in reasons:
        lines.append(f"• {r}")
        lines.append("")

    lines.append(
        "Isto é radar da camada de liquidez, não gatilho de entrada. "
        "Confirma EXP BTC, RSI e Range Level no checklist antes de qualquer posição."
    )

    return "\n".join(lines)


def check_delistings(current_symbols):
    known = load_json(DELIST_STATE_FILE)
    known_set = set(known) if known else set()
    if known_set:
        vanished = known_set - current_symbols
        for symbol in vanished:
            msg = f"POSSÍVEL DELISTING -- {symbol}\n\nSumiu do feed da Binance nesta leitura."
            send_telegram(msg, TOPIC_DELISTING)
            log(f"{symbol}: possivel delisting detectado")
    save_json(DELIST_STATE_FILE, sorted(current_symbols))


def main():
    log("=== Encryptos alertas_engine v6 -- iniciando rodada ===")
    current_snapshot = load_json(FEED_PATH)
    if not current_snapshot:
        log(f"ERRO: nao foi possivel ler {FEED_PATH}")
        sys.exit(1)

    previous_snapshot = load_json(STATE_FILE)
    previous_data_map = previous_snapshot.get("data", {}) if previous_snapshot else {}
    cvd_data = load_json(CVD_PATH)

    cooldown = load_cooldown()
    symbols_data = current_snapshot.get("data", {})
    timestamp = current_snapshot.get("timestamp", "n/d")
    log(f"Símbolos no feed: {len(symbols_data)}")

    check_delistings(set(symbols_data.keys()))

    alerts_fired = 0
    coiled_fired = 0
    scored = []

    for symbol, current in symbols_data.items():
        previous_data = previous_data_map.get(symbol)
        score, reasons, snap, veto, coiled = score_symbol(symbol, current, previous_data, cvd_data)
        scored.append((symbol, score))

        if veto:
            continue

        should_fire = score >= SCORE_THRESHOLD or coiled
        if not should_fire:
            continue

        cooldown_key = f"{symbol}_{'coiled' if coiled else 'score'}"
        if is_in_cooldown(cooldown_key, cooldown):
            continue

        daily_low, daily_high = get_daily_ohlc(symbol)
        snap["daily_low"] = daily_low
        snap["daily_high"] = daily_high
        btc_pair_pct = get_btc_pair_strength(symbol)
        tecnicos = get_ema_rsi_multi(symbol)
        btc_tps = None
        if cvd_data and "BTCUSDT" in cvd_data.get("data", {}):
            btc_tps = cvd_data["data"]["BTCUSDT"].get("trades_per_second")

        text = format_alert(symbol, score, reasons, snap, timestamp, coiled, btc_pair_pct, tecnicos, btc_tps)
        thread = pick_thread(score, coiled)
        sent, message_id = send_telegram(text, thread)
        if sent:
            mark_cooldown(cooldown_key, cooldown)
            link = build_message_link(TELEGRAM_CHAT_ID, message_id) if message_id else None
            append_history({
                "ts": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "score": score,
                "price": snap.get("price"),
                "coiled": coiled,
                "topic_id": thread,
                "message_link": link,
            })
            if coiled:
                coiled_fired += 1
            else:
                alerts_fired += 1
            log(f"{symbol}: ALERTA disparado (score {score}, coiled={coiled})")

    scored.sort(key=lambda x: x[1], reverse=True)
    log("Top 5 scores da rodada:")
    for symbol, score in scored[:5]:
        log(f"  {symbol}: {score}")

    save_json(STATE_FILE, current_snapshot)
    save_cooldown(cooldown)
    log(f"=== Rodada concluída. {alerts_fired} alerta(s) + {coiled_fired} coiled disparado(s). ===\n")


if __name__ == "__main__":
    main()
