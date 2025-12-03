#!/usr/bin/env python3
# bybit_impulse_async_v8.py
# Полная версия: CYAN / GREEN / BLUE / 15s timeout / Telegram / RSI closed + realtime

import asyncio
import httpx
import time
from datetime import datetime, timezone
from colorama import Fore, Style, init as colorama_init

colorama_init(autoreset=True)

# ============================================================
#                 GLOBAL SETTINGS
# ============================================================

TELEGRAM_BOT_TOKEN = "8296046647:AAFsFyJYPhca7OKA5uBrbQM_y_HoRw9NaSY"
ALLOWED_USERS = [7635689852]

BYBIT_TICKERS_URL = "https://api.bybit.com/v5/market/tickers?category=spot"
BYBIT_KLINE_URL   = "https://api.bybit.com/v5/market/kline"

TOP_N = 20
REFRESH_SEC = 1
TIMEOUT_SEC = 15
RSI_CACHE_TTL = 2     # closed-candle RSI refresh

client = None

first_seen = {}         # for YELLOW
state = {}              # WHITE / YELLOW / CYAN / GREEN / BLUE
last_active_ts = {}     # for timeout
rsi_cache = {}          # closed-candle RSI cache


# ============================================================
#                 HELPERS
# ============================================================

def fmt_volume(v):
    v = float(v)
    if v >= 1e9: return f"{v/1e9:.1f}B"
    if v >= 1e6: return f"{v/1e6:.1f}M"
    if v >= 1e3: return f"{v/1e3:.0f}K"
    return f"{v:.0f}"

def fmt_price(p):
    return f"{p:.8f}".rstrip("0").rstrip(".")

def color_of(st):
    return {
        "WHITE": Fore.WHITE,
        "YELLOW": Fore.YELLOW,
        "CYAN": Fore.CYAN,
        "GREEN": Fore.GREEN,
        "BLUE": Fore.BLUE
    }.get(st, Fore.WHITE)

async def tg_send(text):
    for uid in ALLOWED_USERS:
        try:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data={"chat_id": uid, "text": text, "parse_mode": "HTML"}
            )
        except:
            pass

def can_alert(sym, typ):
    now = time.time()
    key = (sym, typ)
    if key in rsi_cache and now - rsi_cache[key].get("alert_ts", 0) < 3600:
        return False
    if sym not in rsi_cache:
        rsi_cache[sym] = {}
    rsi_cache[sym]["alert_ts"] = now
    return True

async def safe_json(url, params=None):
    try:
        r = await client.get(url, params=params)
        if r.status_code != 200:
            return None
        return r.json()
    except:
        return None


# ============================================================
#                 FETCH TICKERS
# ============================================================

async def fetch_tickers():
    data = await safe_json(BYBIT_TICKERS_URL)
    if not data:
        return []

    items = data.get("result", {}).get("list", [])
    out = []

    for it in items:
        sym = it["symbol"]
        if not sym.endswith("USDT"):
            continue

        try:
            last = float(it["lastPrice"])
            vol24 = float(it.get("turnover24h") or 0)
            pct24 = float(it.get("price24hPcnt") or 0) * 100
            out.append({
                "symbol": sym,
                "last": last,
                "vol24": vol24,
                "change24": pct24
            })
        except:
            continue

    out.sort(key=lambda x: x["change24"], reverse=True)
    return out[:TOP_N]


# ============================================================
#        CALCULATE RSI (Closed Candle — old script logic)
# ============================================================

def compute_rsi_wilder(closes):
    """
    Wilder RSI, как в старом скрипте.
    closes — список закрытых close.
    """
    if len(closes) < 15:
        return None

    period = 14
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

    if len(gains) < period:
        return None

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain*(period-1) + gains[i]) / period
        avg_loss = (avg_loss*(period-1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    rsi = 100 - (100/(1+rs))
    return round(rsi, 2)


async def fetch_rsi_closed_1h(symbol):
    """
    Копия твоего старого RSI:
    - 200 свечей 1h
    - упорядочивание
    - Wilder smoothing
    - кэширование
    """

    now = datetime.now(timezone.utc)

    cached = rsi_cache.get(symbol)
    if cached and "closed_rsi" in cached and (now - cached["ts"]).total_seconds() < RSI_CACHE_TTL:
        return cached["closed_rsi"]

    params = {
        "category": "spot",
        "symbol": symbol,
        "interval": "60",
        "limit": 200
    }

    data = await safe_json(BYBIT_KLINE_URL, params)
    if not data:
        return None

    klist = data.get("result", {}).get("list", [])
    if not klist:
        return None

    # Fix order
    if int(klist[0][0]) > int(klist[-1][0]):
        klist = list(reversed(klist))

    closes = [float(x[4]) for x in klist]

    rsi = compute_rsi_wilder(closes)
    if rsi is None:
        return None

    if symbol not in rsi_cache:
        rsi_cache[symbol] = {}

    rsi_cache[symbol]["closed_rsi"] = rsi
    rsi_cache[symbol]["ts"] = now
    return rsi


# ============================================================
#            REALTIME RSI (detectors only)
# ============================================================

def realtime_rsi(closes, price_now):
    """
    realtime RSI:
    14 closed + текущая цена как close текущей свечи.
    """
    arr = closes[:-1] + [price_now]
    return compute_rsi_wilder(arr)


# ============================================================
#                 FETCH KLINE (generic)
# ============================================================

async def fetch_kline(sym, interval, limit):
    data = await safe_json(
        BYBIT_KLINE_URL,
        {"category": "spot", "symbol": sym, "interval": interval, "limit": limit}
    )
    if not data:
        return []

    k = data.get("result", {}).get("list", [])
    if not k:
        return []

    if int(k[0][0]) > int(k[-1][0]):
        k = list(reversed(k))

    return k
    
    # ============================================================
#                 EMA / VWAP HELPERS
# ============================================================

def ema(values, period):
    if len(values) < period:
        return None
    sma = sum(values[:period]) / period
    mult = 2 / (period + 1)
    e = sma
    for v in values[period:]:
        e = (v - e) * mult + e
    return e

def compute_vwap(k):
    pv = 0
    vv = 0
    for c in k:
        h = float(c[2]); l = float(c[3]); cl = float(c[4])
        v = float(c[5])
        typ = (h + l + cl) / 3
        pv += typ * v
        vv += v
    if vv == 0:
        return None
    return pv / vv


# ============================================================
#                 15M IMPULSE (CYAN)
# ============================================================

def detect_15m(k15, price, rsi15):
    """
    Условия для CYAN:
      a) EMA7 > EMA14 > EMA20
      b) Цена > EMA7
      c) Цена > VWAP * 1.01
      d) Свеча +1.3% или больше
      e) RSI15m >= 70 (REALTIME RSI)
      f) Volume >= MA5 * 2
    """

    if not k15 or len(k15) < 20:
        return None

    closes = [float(x[4]) for x in k15]
    vols = [float(x[5]) for x in k15]

    e7  = ema(closes, 7)
    e14 = ema(closes, 14)
    e20 = ema(closes, 20)

    if not (e7 and e14 and e20 and e7 > e14 > e20):
        return None

    if price <= e7:
        return None

    vwap = compute_vwap(k15)
    if not vwap or price <= vwap * 1.01:
        return None

    try:
        open15 = float(k15[-1][1])
        chg15 = (price - open15) / open15 * 100
    except:
        return None

    if chg15 < 1.3:
        return None

    # REALTIME RSI (с текущей свечой)
    if not rsi15 or rsi15 < 70:
        return None

    # Volume spike: last_vol >= MA5 * 2
    if len(vols) < 6:
        return None

    ma5 = sum(vols[-6:-1]) / 5
    if ma5 == 0:
        return None

    vol_mult = vols[-1] / ma5
    if vol_mult < 2:
        return None

    return {
        "chg": chg15,
        "rsi": rsi15,
        "vol_mult": vol_mult
    }


# ============================================================
#                 1H IMPULSE (BLUE)
# ============================================================

def detect_1h(k1h, price, rsi1h):
    """
    Условия BLUE:
      1) Цена от open 1h >= +4%
      2) RSI1h >= 73 (REALTIME RSI)
      3) EMA7 > EMA14 > EMA20
      4) Цена > VWAP
      5) Volume >= MA5 * 2
    """
    if not k1h or len(k1h) < 20:
        return None

    closes = [float(x[4]) for x in k1h]
    vols = [float(x[5]) for x in k1h]

    e7  = ema(closes, 7)
    e14 = ema(closes, 14)
    e20 = ema(closes, 20)

    if not (e7 and e14 and e20 and e7 > e14 > e20):
        return None

    if price <= e7:
        return None

    vwap = compute_vwap(k1h)
    if not vwap or price <= vwap:
        return None

    try:
        open1h = float(k1h[-1][1])
        chg1h = (price - open1h) / open1h * 100
    except:
        return None

    if chg1h < 4:
        return None

    # REALTIME RSI
    if not rsi1h or rsi1h < 73:
        return None

    # Volume spike
    if len(vols) < 6:
        return None
    ma5 = sum(vols[-6:-1]) / 5
    if ma5 == 0:
        return None
    vol_mult = vols[-1] / ma5

    if vol_mult < 2:
        return None

    return {
        "chg": chg1h,
        "rsi": rsi1h,
        "vol_mult": vol_mult
    }


# ============================================================
#             1H TREND (GREEN)
# ============================================================

def detect_green(k1h, price, rsi1h):
    """
    Условия GREEN:
      • EMA7 > EMA14 > EMA20
      • Цена > EMA7
      • Цена > VWAP * 1.005
      • Рост свечи 0.5–4%
      • Volume >= MA5 * 1.2
      • RSI любой (только тренд, без импульса)
    """

    if not k1h or len(k1h) < 20:
        return None

    closes = [float(x[4]) for x in k1h]
    vols = [float(x[5]) for x in k1h]

    e7  = ema(closes, 7)
    e14 = ema(closes, 14)
    e20 = ema(closes, 20)

    if not (e7 and e14 and e20 and e7 > e14 > e20):
        return None

    if price <= e7:
        return None

    vwap = compute_vwap(k1h)
    if not vwap or price <= vwap * 1.005:
        return None

    try:
        open1h = float(k1h[-1][1])
        chg1h = (price - open1h) / open1h * 100
    except:
        return None

    if not (0.5 <= chg1h < 4):
        return None

    if len(vols) < 6:
        return None

    ma5 = sum(vols[-6:-1]) / 5
    if ma5 == 0:
        return None

    vol_mult = vols[-1] / ma5
    if vol_mult < 1.2:
        return None

    return True


# ============================================================
#             TELEGRAM MESSAGE FORMATTERS
# ============================================================

def msg_cyan(sym, price, vol24, data):
    return (
        f"⚡ IMP 15m | <u>{sym}</u> | 24h: {fmt_volume(vol24)}$\n\n"
        f"Цена: {fmt_price(price)} (+{data['chg']:.2f}%)\n"
        f"Свеча:+{data['chg']:.2f}% | RSI:{data['rsi']:.0f} | "
        f"EMA↑ | VWAP↑ | Vol x{data['vol_mult']:.2f} (MA5)"
    )


def msg_blue(sym, price, vol24, data):
    return (
        f"🚀 IMP 1h | <u>{sym}</u> | 24h: {fmt_volume(vol24)}$\n\n"
        f"Цена: {fmt_price(price)} (+{data['chg']:.2f}%)\n"
        f"Свеча:+{data['chg']:.2f}% | RSI:{data['rsi']:.0f} | "
        f"EMA↑ | VWAP↑ | Vol x{data['vol_mult']:.2f} (MA5)"
    )
    
    # ============================================================
#                 TERMINAL CLEAR
# ============================================================

def clear():
    print("\033[2J\033[H", end="")


# ============================================================
#                 MAIN ASYNC LOOP V8
# ============================================================

async def main_async():
    global client
    client = httpx.AsyncClient(timeout=6, verify=False)

    print("Starting Bybit Impulse Bot V8 (CYAN / GREEN / BLUE)...")

    try:
        while True:
            cycle_start = time.time()
            now_dt = datetime.now(timezone.utc)

            # ---------------- LOAD TOP TICKERS ----------------
            tickers = await fetch_tickers()
            if not tickers:
                await asyncio.sleep(1)
                continue

            symbols = [t["symbol"] for t in tickers]

            # Mark new coins = YELLOW
            for t in tickers:
                sym = t["symbol"]
                if sym not in first_seen:
                    first_seen[sym] = now_dt
                    state[sym] = "YELLOW"
                    last_active_ts[sym] = time.time()

            # --------------- LOAD KLINES (15m + 1h) ---------------
            tasks_15 = [fetch_kline(sym, "15", 40) for sym in symbols]
            tasks_1h = [fetch_kline(sym, "60", 60) for sym in symbols]
            kl15, kl1h = await asyncio.gather(
                asyncio.gather(*tasks_15),
                asyncio.gather(*tasks_1h)
            )

            # --------------- CLOSED RSI (for terminal) ---------------
            tasks_closed_rsi = [fetch_rsi_closed_1h(sym) for sym in symbols]
            rsis_closed = await asyncio.gather(*tasks_closed_rsi)

            rows = []

            # ===================================================
            #                 PER SYMBOL PROCESSING
            # ===================================================
            for i, t in enumerate(tickers):
                sym = t["symbol"]
                price = t["last"]
                vol24 = t["vol24"]
                change24 = t["change24"]

                k15 = kl15[i]
                k1h = kl1h[i]
                rsi_closed = rsis_closed[i]  # terminal RSI

                # ============ REALTIME RSI for detectors ============
                # only if klines exist
                rsi15 = None
                rsi1h = None

                if k15 and len(k15) >= 15:
                    closes_15 = [float(x[4]) for x in k15]
                    rsi15 = realtime_rsi(closes_15, price)

                if k1h and len(k1h) >= 15:
                    closes_1h = [float(x[4]) for x in k1h]
                    rsi1h = realtime_rsi(closes_1h, price)

                # ============ CYAN (15m impulse) ============
                cyan = detect_15m(k15, price, rsi15)
                if cyan:
                    state[sym] = "CYAN"
                    last_active_ts[sym] = time.time()

                    if can_alert(sym, "IMP15"):
                        asyncio.create_task(
                            tg_send(msg_cyan(sym, price, vol24, cyan))
                        )

                # ============ BLUE (1h impulse) ============
                blue = detect_1h(k1h, price, rsi1h)
                if blue:
                    state[sym] = "BLUE"
                    last_active_ts[sym] = time.time()

                    if can_alert(sym, "IMP1H"):
                        asyncio.create_task(
                            tg_send(msg_blue(sym, price, vol24, blue))
                        )

                # ============ GREEN (1h trend) ============
                if state[sym] not in ("BLUE", "CYAN"):
                    if detect_green(k1h, price, rsi1h):
                        state[sym] = "GREEN"
                        last_active_ts[sym] = time.time()
                    else:
                        # timeout → WHITE
                        if (time.time() - last_active_ts[sym]) > TIMEOUT_SEC:
                            state[sym] = "WHITE"

                # ============ YELLOW (new coin) ============
                if (now_dt - first_seen[sym]).total_seconds() < 10:
                    state[sym] = "YELLOW"
                    last_active_ts[sym] = time.time()

                # ============ BUILD OUTPUT ROW ============
                rows.append({
                    "symbol": sym,
                    "price": price,
                    "vol24": vol24,
                    "change24": change24,
                    "rsi_closed": rsi_closed if rsi_closed else "N/A",
                    "state": state[sym]
                })

            # ============================================================
            #                 TERMINAL OUTPUT
            # ============================================================

            clear()
            print(
                Fore.CYAN +
                f"Top {TOP_N} Active Coins — {now_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}"
                + Style.RESET_ALL
            )
            print("-" * 105)

            for r in rows:
                sym = r["symbol"]
                p = fmt_price(r["price"])
                vol = fmt_volume(r["vol24"])
                ch = f"{r['change24']:+.2f}%"
                st = r["state"]

                col = color_of(st)

                print(
                    col
                    + f"{sym:<10} | "
                    + f"Vol:{vol:<7} | "
                    + f"RSI:{r['rsi_closed']:<6} | "
                    + Fore.GREEN + f"{p} ({ch})"
                    + Style.RESET_ALL
                )

            print("-" * 105)

            elapsed = time.time() - cycle_start
            if elapsed < REFRESH_SEC:
                await asyncio.sleep(REFRESH_SEC - elapsed)

    except KeyboardInterrupt:
        print("Stopped by user")

    finally:
        await client.aclose()


# ============================================================
#                ENTRY POINT
# ============================================================

if __name__ == "__main__":
    asyncio.run(main_async())