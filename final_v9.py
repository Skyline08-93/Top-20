#!/usr/bin/env python3
# FINAL V9 — realtime RSI A / New 15m & 1h detectors / Hold system / TG queue

import asyncio
import httpx
import time
from datetime import datetime, timezone
from colorama import Fore, Style, init as colorama_init

colorama_init(autoreset=True)

# ---------------------------------------------------------------
# GLOBAL
# ---------------------------------------------------------------
TELEGRAM_BOT_TOKEN = "8296046647:AAFsFyJYPhca7OKA5uBrbQM_y_HoRw9NaSY"
ALLOWED_USERS = [7635689852]

BYBIT_TICKERS_URL = "https://api.bybit.com/v5/market/tickers?category=spot"
BYBIT_KLINE_URL   = "https://api.bybit.com/v5/market/kline"

TOP_N = 20
REFRESH_SEC = 1
TIMEOUT_SEC = 15

client = None
tg_client = None
tg_queue = asyncio.Queue()

TG_PER_MIN = 10
TG_MIN_INTERVAL = 1
tg_sent_timestamps = []

first_seen = {}
state = {}
last_active_ts = {}
inactive_counts = {}
hold_until = {}
rsi_cache = {}

# ---------------- DETECTOR PARAMETERS --------------------------

# --- 1h IMPULSE (BLUE)
MIN_CHG_1H = 4.0
MIN_RSI_1H = 60
MIN_VOL_MULT_1H = 1.3
VWAP_MULT_1H = 0.999
EMA_APPROX_TOL = 0.1

# --- 15m IMPULSE (CYAN)
VWAP_MULT_15 = 1.005
MIN_CHG_15 = 0.8
MIN_RSI_15 = 65
MIN_VOL_MULT_15 = 1.5

# --- GREEN (trend)
VWAP_MULT_GREEN = 1.002
MIN_VOL_MULT_GREEN = 1.1

# Hold
MIN_HOLD_SEC = 15
N_INACTIVE_CYCLES = 3

# ---------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------
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

async def safe_json(url, params=None):
    try:
        r = await client.get(url, params=params)
        if r.status_code != 200:
            return None
        return r.json()
    except:
        return None

# ---------------------------------------------------------------
# TG SENDER
# ---------------------------------------------------------------
async def tg_worker():
    global tg_sent_timestamps
    while True:
        text = await tg_queue.get()
        now = time.time()
        tg_sent_timestamps = [t for t in tg_sent_timestamps if now - t < 60]

        if len(tg_sent_timestamps) >= TG_PER_MIN:
            wait = 60 - (now - tg_sent_timestamps[0]) + 0.1
            await asyncio.sleep(wait)

        for uid in ALLOWED_USERS:
            try:
                r = await tg_client.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    data={"chat_id": uid, "text": text, "parse_mode": "HTML"},
                    timeout=20.0
                )
            except:
                pass
            await asyncio.sleep(TG_MIN_INTERVAL)

        tg_sent_timestamps.append(time.time())
        tg_queue.task_done()

async def enqueue_tg(text):
    try:
        tg_queue.put_nowait(text)
    except:
        pass

# ---------------------------------------------------------------
# RSI
# ---------------------------------------------------------------
def compute_rsi_wilder(closes):
    if len(closes) < 15:
        return None
    period = 14
    deltas = [closes[i]-closes[i-1] for i in range(1,len(closes))]
    gains = [d if d>0 else 0 for d in deltas]
    losses = [-d if d<0 else 0 for d in deltas]
    if len(gains) < period:
        return None
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period,len(gains)):
        avg_gain = (avg_gain*(period-1)+gains[i]) / period
        avg_loss = (avg_loss*(period-1)+losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100/(1+rs)), 2)

def realtime_rsi_from_closes(closes, price_now, period=14):
    if len(closes) < period:
        return None
    last = closes[-period:]
    arr = last + [price_now]
    return compute_rsi_wilder(arr)

async def fetch_rsi_closed_1h(symbol):
    now = datetime.now(timezone.utc)
    c = rsi_cache.get(symbol)
    if c and "closed_rsi" in c and (now - c["ts"]).total_seconds() < 2:
        return c["closed_rsi"]
    data = await safe_json(BYBIT_KLINE_URL, {
        "category":"spot","symbol":symbol,"interval":"60","limit":200
    })
    if not data: return None
    k = data.get("result",{}).get("list",[])
    if not k: return None
    if int(k[0][0])>int(k[-1][0]): k=list(reversed(k))
    closes=[float(x[4]) for x in k]
    r = compute_rsi_wilder(closes)
    if symbol not in rsi_cache: rsi_cache[symbol]={}
    rsi_cache[symbol]["closed_rsi"]=r
    rsi_cache[symbol]["ts"]=now
    return r

# ---------------------------------------------------------------
# EMA / VWAP
# ---------------------------------------------------------------
def ema(values, period):
    if len(values)<period: return None
    sma=sum(values[:period])/period
    mult=2/(period+1)
    e=sma
    for v in values[period:]:
        e=(v-e)*mult+e
    return e

def compute_vwap(k):
    pv=0; vv=0
    for c in k:
        h=float(c[2]); l=float(c[3]); cl=float(c[4]); v=float(c[5])
        typ=(h+l+cl)/3
        pv+=typ*v
        vv+=v
    if vv==0: return None
    return pv/vv

# ---------------------------------------------------------------
# KLINE & TICKERS
# ---------------------------------------------------------------
async def fetch_kline(sym, interval, limit):
    d = await safe_json(BYBIT_KLINE_URL, {
        "category":"spot","symbol":sym,"interval":interval,"limit":limit
    })
    if not d: return []
    k=d.get("result",{}).get("list",[])
    if not k: return []
    if int(k[0][0]) > int(k[-1][0]):
        k=list(reversed(k))
    return k

async def fetch_tickers():
    data = await safe_json(BYBIT_TICKERS_URL)
    if not data: return []
    items = data.get("result",{}).get("list",[])
    out=[]
    for it in items:
        sym=it["symbol"]
        if not sym.endswith("USDT"): continue
        try:
            last=float(it["lastPrice"])
            vol24=float(it.get("turnover24h") or 0)
            pct=float(it.get("price24hPcnt") or 0)*100
            out.append({
                "symbol":sym,
                "last":last,
                "vol24":vol24,
                "change24":pct
            })
        except:
            continue
    out.sort(key=lambda x:x["change24"], reverse=True)
    return out[:TOP_N]

# ---------------------------------------------------------------
# DETECTORS
# ---------------------------------------------------------------
def detect_15m(k15, price, rsi15):
    if not k15 or len(k15)<20: return None
    closes=[float(x[4]) for x in k15]
    vols=[float(x[5]) for x in k15]

    e7=ema(closes,7); e14=ema(closes,14); e20=ema(closes,20)
    if not(e7 and e14 and e20 and e7>e14>e20): return None
    vwap=compute_vwap(k15)
    if not vwap or price <= vwap * VWAP_MULT_15: return None
    op=float(k15[-1][1])
    chg=(price-op)/op*100
    if chg < MIN_CHG_15: return None
    if not rsi15 or rsi15 < MIN_RSI_15: return None
    if len(vols)<6: return None
    ma5=sum(vols[-6:-1])/5
    if ma5==0: return None
    vm=vols[-1]/ma5
    if vm < MIN_VOL_MULT_15: return None
    return {"chg":chg,"rsi":rsi15,"vol_mult":vm}

def detect_1h(k1h, price, rsi1h):
    if not k1h or len(k1h)<20: return None
    closes=[float(x[4]) for x in k1h]
    vols=[float(x[5]) for x in k1h]
    e7=ema(closes,7); e14=ema(closes,14); e20=ema(closes,20)
    if not(e7 and e14): return None
    if e7 <= e14: return None
    if abs(e14 - e20) > EMA_APPROX_TOL: return None
    vwap=compute_vwap(k1h)
    if not vwap or price <= vwap * VWAP_MULT_1H: return None
    op=float(k1h[-1][1])
    chg=(price-op)/op*100
    if chg < MIN_CHG_1H: return None
    if not rsi1h or rsi1h < MIN_RSI_1H: return None
    if len(vols)<6: return None
    ma5=sum(vols[-6:-1])/5
    if ma5==0: return None
    vm=vols[-1]/ma5
    if vm < MIN_VOL_MULT_1H: return None
    return {"chg":chg,"rsi":rsi1h,"vol_mult":vm}

def detect_green(k1h, price, rsi1h):
    if not k1h or len(k1h)<20: return None
    closes=[float(x[4]) for x in k1h]
    vols=[float(x[5]) for x in k1h]

    e7=ema(closes,7); e14=ema(closes,14); e20=ema(closes,20)
    if not(e7 and e14 and e20 and e7>e14>e20): return None

    vwap=compute_vwap(k1h)
    if not vwap or price <= vwap * VWAP_MULT_GREEN: return None

    op=float(k1h[-1][1])
    chg=(price-op)/op*100
    if not(0.5 <= chg < 4): return None

    if len(vols)<6: return None
    ma5=sum(vols[-6:-1])/5
    if ma5==0: return None
    vm=vols[-1]/ma5
    if vm < MIN_VOL_MULT_GREEN: return None

    return True

# ---------------------------------------------------------------
# MESSAGES
# ---------------------------------------------------------------
def msg_cyan(sym, price, vol24, d):
    return (
        f"⚡ IMP 15m | <u>{sym}</u> | 24h:{fmt_volume(vol24)}$\n\n"
        f"Цена:{fmt_price(price)} (+{d['chg']:.2f}%)\n"
        f"RSI:{d['rsi']:.0f} | Vol x{d['vol_mult']:.2f}"
    )

def msg_blue(sym, price, vol24, d):
    return (
        f"🚀 IMP 1h | <u>{sym}</u> | 24h:{fmt_volume(vol24)}$\n\n"
        f"Цена:{fmt_price(price)} (+{d['chg']:.2f}%)\n"
        f"RSI:{d['rsi']:.0f} | Vol x{d['vol_mult']:.2f}"
    )

# ---------------------------------------------------------------
def clear():
    print("\033[2J\033[H",end="")

# ---------------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------------
async def main_async():
    global client, tg_client
    client = httpx.AsyncClient(timeout=20, verify=True)
    tg_client = httpx.AsyncClient(timeout=20, verify=True)

    asyncio.create_task(tg_worker())
    print("Starting FINAL V9...")

    try:
        while True:
            cycle_start=time.time()
            now_dt=datetime.now(timezone.utc)

            tickers=await fetch_tickers()
            if not tickers:
                await asyncio.sleep(1)
                continue
            syms=[t["symbol"] for t in tickers]

            for t in tickers:
                s=t["symbol"]
                if s not in first_seen:
                    first_seen[s]=now_dt
                    state[s]="YELLOW"
                    last_active_ts[s]=time.time()
                    inactive_counts[s]=0
                    hold_until[s]=0

            tasks15=[fetch_kline(s,"15",40) for s in syms]
            tasks1h=[fetch_kline(s,"60",60) for s in syms]
            kl15,kl1h=await asyncio.gather(
                asyncio.gather(*tasks15),
                asyncio.gather(*tasks1h)
            )

            rsic_tasks=[fetch_rsi_closed_1h(s) for s in syms]
            rsic=await asyncio.gather(*rsic_tasks)

            rows=[]

            for i,t in enumerate(tickers):
                sym=t["symbol"]
                price=t["last"]
                vol24=t["vol24"]
                change24=t["change24"]

                k15=kl15[i]
                k1h=kl1h[i]

                closes15=[float(x[4]) for x in k15] if k15 else []
                closes1h=[float(x[4]) for x in k1h] if k1h else []

                rsi_closed=rsic[i]
                rsi15=realtime_rsi_from_closes(closes15, price) if closes15 else None
                rsi1h=realtime_rsi_from_closes(closes1h, price) if closes1h else None

                cyan = detect_15m(k15, price, rsi15)
                if cyan:
                    state[sym]="CYAN"
                    last_active_ts[sym]=time.time()
                    hold_until[sym]=time.time()+MIN_HOLD_SEC
                    inactive_counts[sym]=0
                    if (sym, "IMP15") not in rsi_cache:
                        rsi_cache[(sym,"IMP15")] = time.time()
                        asyncio.create_task(enqueue_tg(msg_cyan(sym,price,vol24,cyan)))

                blue = detect_1h(k1h, price, rsi1h)
                if blue:
                    state[sym]="BLUE"
                    last_active_ts[sym]=time.time()
                    hold_until[sym]=time.time()+MIN_HOLD_SEC
                    inactive_counts[sym]=0

                    last = rsi_cache.get((sym,"IMP1H"),0)
                    if time.time() - last >= 3600:
                        rsi_cache[(sym,"IMP1H")] = time.time()
                        asyncio.create_task(enqueue_tg(msg_blue(sym,price,vol24,blue)))

                if state[sym] not in ("BLUE","CYAN"):
                    if detect_green(k1h, price, rsi1h):
                        state[sym]="GREEN"
                        last_active_ts[sym]=time.time()
                        inactive_counts[sym]=0
                        hold_until[sym]=time.time()+MIN_HOLD_SEC
                    else:
                        inactive_counts[sym]+=1
                        if time.time()>hold_until[sym] and inactive_counts[sym]>=N_INACTIVE_CYCLES:
                            state[sym]="WHITE"

                if (now_dt - first_seen[sym]).total_seconds()<10:
                    state[sym]="YELLOW"
                    last_active_ts[sym]=time.time()
                    inactive_counts[sym]=0
                    hold_until[sym]=time.time()+MIN_HOLD_SEC

                rows.append({
                    "symbol":sym,
                    "price":price,
                    "vol24":vol24,
                    "change24":change24,
                    "rsi_closed":rsi_closed if rsi_closed else "N/A",
                    "rsi_real":rsi1h if rsi1h else "N/A",
                    "state":state[sym]
                })

            clear()
            print(Fore.CYAN + f"Top {TOP_N} — {now_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}" + Style.RESET_ALL)
            print("-"*110)

            for r in rows:
                col=color_of(r["state"])
                p=fmt_price(r["price"])
                ch=f"{r['change24']:+.2f}%"
                vol=fmt_volume(r["vol24"])

                print(
                    col +
                    f"{r['symbol']:<10} | "
                    f"Vol:{vol:<7} | "
                    f"RSI:{r['rsi_real']:<6} | "
                    + Fore.GREEN + f"{p} ({ch})" +
                    Style.RESET_ALL
                )

            print("-"*110)

            elapsed=time.time()-cycle_start
            if elapsed<REFRESH_SEC:
                await asyncio.sleep(REFRESH_SEC-elapsed)

    except KeyboardInterrupt:
        pass
    finally:
        await client.aclose()
        await tg_client.aclose()


if __name__=="__main__":
    asyncio.run(main_async())
