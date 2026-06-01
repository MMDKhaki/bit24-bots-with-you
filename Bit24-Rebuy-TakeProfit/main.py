"""
ADA/IRT DCA Trading Bot — Bit24 Exchange
Strategy: 3-step grid entry with independent take-profit per position
API Docs : https://docs.bit24.cash/#api-24
Repo     : https://github.com/shayanghad0/Bit24-Easy-To-use
"""

import requests
import hashlib
import hmac
import time
import json
import signal
import sys
from urllib.parse import urlencode

# ─────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────
PAIR_BASE  = "ADA"
PAIR_QUOTE = "IRT"
QTY        = "2"          # ADA per position

# Grid offsets from initial entry price
GRID_1_PCT = 0.0021       # -0.21 %
GRID_2_PCT = 0.0042       # -0.42 %

# Take-profit targets per position (from entry price)
TP_INITIAL = 0.005        # +0.5 %
TP_GRID1   = 0.007        # +0.7 %
TP_GRID2   = 0.010        # +1.0 %

POLL_INTERVAL = 3         # seconds between market checks

BASE_URL_PRO   = "https://rest.bit24.cash/pro/capi/v1"
BASE_URL_ASSET = "https://rest.bit24.cash/asset/capi/v1"

TRADE_LOG = "trade.json"
TP_STATE  = "tp.json"

# ─────────────────────────────────────────────
#  GLOBALS (filled at runtime)
# ─────────────────────────────────────────────
API_KEY    = ""
SECRET_KEY = ""

positions = {
    "initial": {"order_id": None, "entry_price": None, "tp_price": None,
                "filled": False, "tp_done": False, "qty": QTY},
    "limit1":  {"order_id": None, "entry_price": None, "tp_price": None,
                "filled": False, "tp_done": False, "qty": QTY},
    "limit2":  {"order_id": None, "entry_price": None, "tp_price": None,
                "filled": False, "tp_done": False, "qty": QTY},
}

trade_log = []


# ─────────────────────────────────────────────
#  SIGNATURE
# ─────────────────────────────────────────────
def sign(params: dict, secret: str) -> str:
    """HMAC-SHA256 signature expected by Bit24 authenticated endpoints."""
    p = dict(params)
    p.pop("signature", None)
    p = dict(sorted(p.items()))
    query_string = urlencode(p, quote_via=lambda s, *_: s)   # RFC 3986
    return hmac.new(secret.encode(), query_string.encode(), hashlib.sha256).hexdigest()


# ─────────────────────────────────────────────
#  HTTP HELPERS
# ─────────────────────────────────────────────
def _headers() -> dict:
    return {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "X-BIT24-APIKEY": API_KEY,
    }


def get(path: str, params: dict = None) -> dict:
    url = BASE_URL_PRO + path
    resp = requests.get(url, params=params, headers=_headers(), timeout=10)
    return resp.json()


def post(path: str, params: dict, base: str = BASE_URL_PRO) -> dict:
    params["signature"] = sign(params, SECRET_KEY)
    url = base + path
    resp = requests.post(url, data=params, headers=_headers(), timeout=10)
    return resp.json()


def asset_get(path: str, params: dict = None) -> dict:
    url = BASE_URL_ASSET + path
    resp = requests.get(url, params=params, headers=_headers(), timeout=10)
    return resp.json()


# ─────────────────────────────────────────────
#  MARKET DATA
# ─────────────────────────────────────────────
def get_best_bid() -> float | None:
    """Return the highest buy-order price from the order book."""
    r = get("/markets/order-books", {"base_coin": PAIR_BASE, "quote_coin": PAIR_QUOTE})
    if r.get("success") and r["data"]["buy_orders"]:
        return float(r["data"]["buy_orders"][0]["price"])
    return None


def get_order_status(order_id: int) -> dict | None:
    """Fetch a single order by ID."""
    r = get("/orders", {"id": order_id})
    if r.get("success"):
        return r["data"]["order"]
    return None


# ─────────────────────────────────────────────
#  WALLET
# ─────────────────────────────────────────────
def get_available_balance(symbol: str) -> float:
    r = asset_get("/wallet/assets", {"name": symbol, "without_zero": "1"})
    if r.get("success"):
        for asset in r["data"].get("asset", []):
            if asset["symbol"].upper() == symbol.upper():
                return float(asset["available_balance"])
    return 0.0


# ─────────────────────────────────────────────
#  ORDER ACTIONS
# ─────────────────────────────────────────────
def place_market_buy(qty: str) -> dict | None:
    """Market (instant) buy — uses quote_coin_amount logic for market orders."""
    # For a market buy on Bit24, category_type=1 + quote_coin_amount
    # But here we want to buy a fixed base quantity; we convert via best bid.
    best_bid = get_best_bid()
    if best_bid is None:
        print("[ERROR] Cannot fetch order book for market buy.")
        return None
    quote_amount = str(int(float(qty) * best_bid * 1.002))  # tiny buffer for fills

    params = {
        "base_coin_symbol":  PAIR_BASE,
        "quote_coin_symbol": PAIR_QUOTE,
        "type":              "1",          # buy
        "category_type":     "1",          # market/instant
        "quote_coin_amount": quote_amount,
    }
    r = post("/orders/submit", params)
    if r.get("success"):
        order = r["data"]["order"]
        print(f"[MARKET BUY] order_id={order['id']}  ~{quote_amount} IRT")
        return order
    else:
        print(f"[ERROR] Market buy failed: {r}")
        return None


def place_limit_buy(price: float, qty: str) -> dict | None:
    params = {
        "base_coin_symbol":  PAIR_BASE,
        "quote_coin_symbol": PAIR_QUOTE,
        "type":              "1",   # buy
        "category_type":     "0",   # limit
        "price":             str(int(price)),
        "amount":            qty,
    }
    r = post("/orders/submit", params)
    if r.get("success"):
        order = r["data"]["order"]
        print(f"[LIMIT BUY ] order_id={order['id']}  price={int(price):,}  qty={qty}")
        return order
    else:
        print(f"[ERROR] Limit buy failed: {r}")
        return None


def place_market_sell(qty: str) -> dict | None:
    params = {
        "base_coin_symbol":  PAIR_BASE,
        "quote_coin_symbol": PAIR_QUOTE,
        "type":              "0",   # sell
        "category_type":     "1",   # market
        "amount":            qty,
    }
    r = post("/orders/submit", params)
    if r.get("success"):
        order = r["data"]["order"]
        print(f"[MARKET SELL] order_id={order['id']}  qty={qty}")
        return order
    else:
        print(f"[ERROR] Market sell failed: {r}")
        return None


def cancel_order(order_id: int) -> bool:
    params = {"order_id": order_id}
    r = post("/orders/cancel", params)
    ok = r.get("success", False)
    print(f"[CANCEL] order_id={order_id}  success={ok}")
    return ok


# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────
def _save_json(path: str, data) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def log_trade(event: str, detail: dict) -> None:
    entry = {"event": event, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), **detail}
    trade_log.append(entry)
    _save_json(TRADE_LOG, trade_log)


def save_tp_state() -> None:
    _save_json(TP_STATE, positions)


# ─────────────────────────────────────────────
#  GRACEFUL SHUTDOWN
# ─────────────────────────────────────────────
def shutdown(sig=None, frame=None) -> None:
    print("\n[SHUTDOWN] Cancelling open orders and liquidating balance …")

    for name, pos in positions.items():
        if pos["order_id"] and not pos["filled"]:
            cancel_order(pos["order_id"])

    balance = get_available_balance(PAIR_BASE)
    if balance > 0.01:
        print(f"[SHUTDOWN] Selling remaining {balance:.4f} {PAIR_BASE}")
        qty_str = f"{balance:.6f}".rstrip("0").rstrip(".")
        result = place_market_sell(qty_str)
        if result:
            log_trade("emergency_sell", {"qty": qty_str, "order_id": result["id"]})

    save_tp_state()
    _save_json(TRADE_LOG, trade_log)
    print("[SHUTDOWN] Done. Logs saved.")
    sys.exit(0)


signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)


# ─────────────────────────────────────────────
#  BOT LOGIC
# ─────────────────────────────────────────────
def initialise_positions(start_price: float) -> None:
    """Set entry and TP prices for all three positions."""
    positions["initial"]["entry_price"] = start_price
    positions["initial"]["tp_price"]    = round(start_price * (1 + TP_INITIAL))

    p1 = round(start_price * (1 - GRID_1_PCT))
    positions["limit1"]["entry_price"] = p1
    positions["limit1"]["tp_price"]    = round(p1 * (1 + TP_GRID1))

    p2 = round(start_price * (1 - GRID_2_PCT))
    positions["limit2"]["entry_price"] = p2
    positions["limit2"]["tp_price"]    = round(p2 * (1 + TP_GRID2))


def check_fills() -> None:
    """Poll open limit orders and mark them filled if status == 1."""
    for name in ("limit1", "limit2"):
        pos = positions[name]
        if pos["order_id"] and not pos["filled"]:
            status = get_order_status(pos["order_id"])
            if status and status["status"] == 1:
                pos["filled"] = True
                print(f"[FILL] {name} filled at ~{pos['entry_price']:,}")
                log_trade("fill", {"position": name, "price": pos["entry_price"],
                                   "order_id": pos["order_id"]})
                save_tp_state()


def check_take_profits(bid: float) -> None:
    """Trigger market sells when best bid crosses a TP target."""
    for name, pos in positions.items():
        if pos["filled"] and not pos["tp_done"] and pos["tp_price"]:
            if bid >= pos["tp_price"]:
                print(f"[TP HIT] {name}  bid={bid:,}  target={pos['tp_price']:,}")
                result = place_market_sell(pos["qty"])
                if result:
                    pos["tp_done"] = True
                    log_trade("take_profit", {
                        "position": name,
                        "entry":    pos["entry_price"],
                        "tp_price": pos["tp_price"],
                        "bid":      bid,
                        "order_id": result["id"],
                    })
                    save_tp_state()


def run() -> None:
    global API_KEY, SECRET_KEY

    print("=== Bit24 ADA/IRT DCA Bot ===")
    API_KEY    = input("API Key    : ").strip()
    SECRET_KEY = input("Secret Key : ").strip()

    # ── Step 1: market buy initial position ──────────────────────────
    print("\n[STEP 1] Placing initial market buy …")
    init_order = place_market_buy(QTY)
    if not init_order:
        print("[FATAL] Could not place initial order. Exiting.")
        sys.exit(1)

    # Allow a moment for the fill to register, then fetch filled price
    time.sleep(2)
    filled = get_order_status(init_order["id"])
    start_price = float(filled["mean_value"]) if filled and filled.get("mean_value") else None

    if not start_price or start_price == 0:
        # Fallback: use best bid at time of order
        start_price = get_best_bid()
        print(f"[WARN] mean_value unavailable; using best bid as entry: {start_price:,}")

    positions["initial"]["order_id"] = init_order["id"]
    positions["initial"]["filled"]   = True
    log_trade("buy", {"position": "initial", "price": start_price,
                      "order_id": init_order["id"]})

    # ── Step 2: compute grid + TP prices ────────────────────────────
    initialise_positions(start_price)
    print(f"\n[PRICES] start={start_price:,}")
    for name, pos in positions.items():
        print(f"  {name:8s}  entry={pos['entry_price']:,}  TP={pos['tp_price']:,}")

    # ── Step 3: place grid limit buys ───────────────────────────────
    print("\n[STEP 3] Placing grid limit orders …")
    for name, pct_off in (("limit1", GRID_1_PCT), ("limit2", GRID_2_PCT)):
        pos = positions[name]
        order = place_limit_buy(pos["entry_price"], QTY)
        if order:
            pos["order_id"] = order["id"]
            log_trade("limit_buy_placed", {"position": name,
                                           "price": pos["entry_price"],
                                           "order_id": order["id"]})
    save_tp_state()

    # ── Step 4: main polling loop ────────────────────────────────────
    print("\n[RUNNING] Monitoring market every 3 s  (Ctrl+C to stop) …\n")
    while True:
        try:
            bid = get_best_bid()
            if bid:
                check_fills()
                check_take_profits(bid)

                done = sum(1 for p in positions.values() if p["tp_done"])
                print(f"  bid={int(bid):,}  TP done={done}/3", end="\r")

                if done == 3:
                    print("\n[DONE] All three positions closed. Bot finished.")
                    save_tp_state()
                    break
        except Exception as exc:
            print(f"\n[WARN] Loop error: {exc}")

        time.sleep(POLL_INTERVAL)


# ─────────────────────────────────────────────
if __name__ == "__main__":
    run()
