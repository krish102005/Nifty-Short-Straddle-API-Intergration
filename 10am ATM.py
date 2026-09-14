import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, time as clock_time
from urllib.request import urlopen
from zoneinfo import ZoneInfo

import pyotp
from logzero import logger
from SmartApi.smartConnect import SmartConnect


# =========================
# USER SETTINGS
# =========================
API_KEY = "dVyqagA1"
CLIENT_CODE = "AACC999941"
PIN = "1470"
TOTP_SECRET = "D6F5XBOF7CZPYPX5SKIPIK6BZA"

# Keep this True until you verify the generated order params.
DRY_RUN = True
WAIT_UNTIL_10AM = True

ORDER_LOTS = 1
STOP_LOSS_PERCENT = 30.0
PRODUCT_TYPE = "INTRADAY"

NIFTY_INDEX_EXCHANGE = "NSE"
NIFTY_INDEX_TRADING_SYMBOLS = ["NIFTY", "NIFTY 50", "Nifty 50"]
NIFTY_INDEX_TOKEN = "26000"

OPTION_EXCHANGE = "NFO"
OPTION_NAME = "NIFTY"
NIFTY_STRIKE_STEP = 50

STOPLOSS_ORDER_TYPE = "STOPLOSS_MARKET"
STOPLOSS_LIMIT_BUFFER = 1.0
ORDER_POLL_SECONDS = 30
TICK_SIZE = 0.05

SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class OptionContract:
    option_type: str
    trading_symbol: str
    symbol_token: str
    exchange: str
    expiry: datetime
    strike: int
    lot_size: int


def wait_until_10am():
    if not WAIT_UNTIL_10AM:
        return

    now = datetime.now(IST)
    run_at = datetime.combine(now.date(), clock_time(hour=10, minute=0), tzinfo=IST)
    if now < run_at:
        seconds = (run_at - now).total_seconds()
        logger.info("Waiting until 10:00 AM IST: %s seconds remaining.", int(seconds))
        time.sleep(seconds)
    else:
        logger.info("10:00 AM IST has already passed; running immediately.")
## if we run this script after 10 am, it will execute the order immediately?

def connect():
    smart_api = SmartConnect(API_KEY)
    totp = pyotp.TOTP(TOTP_SECRET).now()
    session = smart_api.generateSession(CLIENT_CODE, PIN, totp)

    if not session or not session.get("status"):
        raise RuntimeError("Angel One login failed: {}".format(session))

    logger.info("Angel One SmartAPI login successful.")
    return smart_api, session


def get_profile(smart_api, session):
    try:
        refresh_token = session["data"]["refreshToken"]
        profile = smart_api.getProfile(refresh_token)
        logger.info("Profile response: %s", profile)
    except Exception as exc:
        logger.warning("Profile fetch skipped/failed: %s", exc)


def extract_ltp(response, label):
    if not response or not response.get("status"):
        raise RuntimeError("LTP fetch failed for {}: {}".format(label, response))

    data = response.get("data") or {}
    ltp = data.get("ltp") or data.get("LTP")
    if ltp is None:
        raise RuntimeError("LTP missing for {}: {}".format(label, response))
    return float(ltp)


def fetch_ltp(smart_api, exchange, trading_symbol, token):
    response = smart_api.ltpData(exchange, trading_symbol, token)
    return extract_ltp(response, "{}:{}:{}".format(exchange, trading_symbol, token))


def fetch_nifty_spot(smart_api):
    last_error = None

    for trading_symbol in NIFTY_INDEX_TRADING_SYMBOLS:
        try:
            spot = fetch_ltp(smart_api, NIFTY_INDEX_EXCHANGE, trading_symbol, NIFTY_INDEX_TOKEN)
            logger.info("NIFTY spot LTP from %s: %s", trading_symbol, spot)
            return spot
        except Exception as exc:
            last_error = exc
            logger.warning("NIFTY spot fetch failed with symbol %s: %s", trading_symbol, exc)

    raise RuntimeError("Could not fetch NIFTY spot price. Last error: {}".format(last_error))


def nearest_atm_strike(spot, step):
    return int(round(spot / step) * step)


def fetch_scrip_master():
    with urlopen(SCRIP_MASTER_URL, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_expiry(value):
    value = str(value).strip().upper()
    for fmt in ("%d%b%Y", "%d%b%y", "%d-%b-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=IST)
        except ValueError:
            pass
    return None


def normalized_strike(value):
    strike = float(value)
    if strike > 100000:
        strike = strike / 100
    return int(round(strike))


def find_option_contracts(instruments, strike):
    today = datetime.now(IST).date()
    matches = []

    for item in instruments:
        if str(item.get("exch_seg", "")).upper() != OPTION_EXCHANGE:
            continue
        if str(item.get("name", "")).upper() != OPTION_NAME:
            continue
        if "OPT" not in str(item.get("instrumenttype", "")).upper():
            continue

        symbol = str(item.get("symbol", "")).upper()
        option_type = symbol[-2:]
        if option_type not in ("CE", "PE"):
            continue

        try:
            item_strike = normalized_strike(item.get("strike"))
        except (TypeError, ValueError):
            continue
        if item_strike != strike:
            continue

        expiry = parse_expiry(str(item.get("expiry", "")))
        if expiry is None or expiry.date() < today:
            continue

        matches.append(
            OptionContract(
                option_type=option_type,
                trading_symbol=symbol,
                symbol_token=str(item.get("token")),
                exchange=OPTION_EXCHANGE,
                expiry=expiry,
                strike=item_strike,
                lot_size=int(float(item.get("lotsize", 0) or 0)),
            )
        )

    if not matches:
        raise RuntimeError("No NIFTY option contracts found for strike {}.".format(strike))

    nearest_expiry = min(contract.expiry for contract in matches)
    selected = {}
    for contract in matches:
        if contract.expiry == nearest_expiry:
            selected[contract.option_type] = contract

    if "CE" not in selected or "PE" not in selected:
        raise RuntimeError("Missing CE/PE for strike {}, expiry {}.".format(strike, nearest_expiry.date()))

    return selected


def money(value):
    return "{:.2f}".format(value)


def round_up_to_tick(value, tick_size):
    return round(math.ceil(value / tick_size) * tick_size, 2)


def build_sell_order(contract, quantity):
    return {
        "variety": "NORMAL",
        "tradingsymbol": contract.trading_symbol,
        "symboltoken": contract.symbol_token,
        "transactiontype": "SELL",
        "exchange": contract.exchange,
        "ordertype": "MARKET",
        "producttype": PRODUCT_TYPE,
        "duration": "DAY",
        "price": "0",
        "squareoff": "0",
        "stoploss": "0",
        "quantity": str(quantity),
    }


def build_stoploss_order(contract, quantity, trigger_price):
    order = {
        "variety": "STOPLOSS",
        "tradingsymbol": contract.trading_symbol,
        "symboltoken": contract.symbol_token,
        "transactiontype": "BUY",
        "exchange": contract.exchange,
        "ordertype": STOPLOSS_ORDER_TYPE,
        "producttype": PRODUCT_TYPE,
        "duration": "DAY",
        "price": "0",
        "triggerprice": money(trigger_price),
        "quantity": str(quantity),
    }

    if STOPLOSS_ORDER_TYPE == "STOPLOSS_LIMIT":
        order["price"] = money(trigger_price + STOPLOSS_LIMIT_BUFFER)

    return order


def place_order(smart_api, order_params):
    if DRY_RUN:
        logger.info("DRY RUN order: %s", order_params)
        return {"status": True, "dry_run": True, "data": {"orderid": "DRY-RUN"}}

    response = smart_api.placeOrderFullResponse(order_params)
    if not response or not response.get("status"):
        raise RuntimeError("Order placement failed: {}".format(response))

    logger.info("Order placed: %s", response)
    return response


def extract_order_id(response):
    data = response.get("data") if isinstance(response, dict) else None
    if isinstance(data, dict):
        return str(data.get("orderid") or data.get("orderId") or "")
    return None


def find_order_in_book(order_book, order_id):
    orders = order_book.get("data") if isinstance(order_book, dict) else []
    if not isinstance(orders, list):
        return None

    for order in orders:
        if str(order.get("orderid") or order.get("orderId")) == order_id:
            return order
    return None


def get_average_fill_price(smart_api, order_id, timeout_seconds):
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        order_book = smart_api.orderBook()
        order = find_order_in_book(order_book, order_id)
        if order:
            status = str(order.get("orderstatus") or order.get("status") or "").lower()
            average_price = order.get("averageprice") or order.get("averagePrice")
            if status in ("complete", "completed", "filled", "traded") and average_price:
                return float(average_price)
            if status in ("rejected", "cancelled", "canceled"):
                raise RuntimeError("Sell order {} ended with status {}: {}".format(order_id, status, order))
        time.sleep(2)

    return None


def execute_leg(smart_api, contract, quantity):
    option_ltp = fetch_ltp(smart_api, contract.exchange, contract.trading_symbol, contract.symbol_token)
    sell_order = build_sell_order(contract, quantity)
    sell_response = place_order(smart_api, sell_order)

    entry_price = option_ltp
    sell_order_id = extract_order_id(sell_response)

    if not DRY_RUN and sell_order_id:
        average_fill = get_average_fill_price(smart_api, sell_order_id, ORDER_POLL_SECONDS)
        if average_fill is not None:
            entry_price = average_fill
        else:
            logger.warning("Average fill unavailable for %s; using pre-order LTP %s.", sell_order_id, option_ltp)

    trigger_price = round_up_to_tick(entry_price * (1 + STOP_LOSS_PERCENT / 100), TICK_SIZE)
    stoploss_order = build_stoploss_order(contract, quantity, trigger_price)
    stoploss_response = place_order(smart_api, stoploss_order)

    return {
        "contract": contract,
        "entry_price_used": entry_price,
        "stoploss_trigger": trigger_price,
        "sell_order": sell_order,
        "sell_response": sell_response,
        "stoploss_order": stoploss_order,
        "stoploss_response": stoploss_response,
    }


def main():
    if ORDER_LOTS < 1:
        raise RuntimeError("ORDER_LOTS must be at least 1.")
    if STOP_LOSS_PERCENT <= 0:
        raise RuntimeError("STOP_LOSS_PERCENT must be greater than 0.")

    logger.info("DRY_RUN=%s. Set DRY_RUN=False only when you are ready for live orders.", DRY_RUN)
    wait_until_10am()

    smart_api, session = connect()
    get_profile(smart_api, session)

    spot = fetch_nifty_spot(smart_api)
    atm_strike = nearest_atm_strike(spot, NIFTY_STRIKE_STEP)
    logger.info("NIFTY spot=%s; selected ATM strike=%s.", spot, atm_strike)

    instruments = fetch_scrip_master()
    contracts = find_option_contracts(instruments, atm_strike)

    ce_contract = contracts["CE"]
    pe_contract = contracts["PE"]
    if ce_contract.lot_size <= 0 or pe_contract.lot_size <= 0:
        raise RuntimeError("Invalid lot size for selected contracts: CE={}, PE={}".format(ce_contract, pe_contract))

    quantity = min(ce_contract.lot_size, pe_contract.lot_size) * ORDER_LOTS
    logger.info("Selected CE=%s, PE=%s, quantity=%s.", ce_contract.trading_symbol, pe_contract.trading_symbol, quantity)

    results = [
        execute_leg(smart_api, ce_contract, quantity),
        execute_leg(smart_api, pe_contract, quantity),
    ]

    for result in results:
        contract = result["contract"]
        logger.info(
            "%s done: sell=%s, SL trigger=%s, SL=%s",
            contract.option_type,
            result["sell_response"],
            result["stoploss_trigger"],
            result["stoploss_response"],
        )


if __name__ == "__main__":
    main()
