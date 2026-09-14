from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, time as clock_time
from typing import Any
from urllib.request import urlopen
from zoneinfo import ZoneInfo

import pyotp
from logzero import logger
from SmartApi.smartConnect import SmartConnect

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - lets the script still run without python-dotenv.
    load_dotenv = None


IST = ZoneInfo("Asia/Kolkata")
SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"


@dataclass(frozen=True)
class Config:
    api_key: str
    client_code: str
    pin: str
    totp_secret: str
    dry_run: bool = True
    wait_until_10am: bool = True
    order_lots: int = 1
    stop_loss_percent: float = 30.0
    product_type: str = "INTRADAY"
    index_exchange: str = "NSE"
    index_trading_symbol: str = "NIFTY"
    index_symbol_token: str = "26000"
    option_exchange: str = "NFO"
    option_name: str = "NIFTY"
    strike_step: int = 50
    order_poll_seconds: int = 30
    tick_size: float = 0.05
    stoploss_order_type: str = "STOPLOSS_MARKET"
    stoploss_limit_buffer: float = 1.0


@dataclass(frozen=True)
class OptionContract:
    option_type: str
    trading_symbol: str
    symbol_token: str
    exchange: str
    expiry: datetime
    strike: int
    lot_size: int


def as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_config() -> Config:
    if load_dotenv is not None:
        load_dotenv()

    return Config(
        api_key=require_env("ANGEL_API_KEY"),
        client_code=require_env("ANGEL_CLIENT_CODE"),
        pin=require_env("ANGEL_PIN"),
        totp_secret=require_env("ANGEL_TOTP_SECRET"),
        dry_run=as_bool(os.getenv("DRY_RUN"), True),
        wait_until_10am=as_bool(os.getenv("WAIT_UNTIL_10AM"), True),
        order_lots=int(os.getenv("ORDER_LOTS", "1")),
        stop_loss_percent=float(os.getenv("STOP_LOSS_PERCENT", "30")),
        product_type=os.getenv("PRODUCT_TYPE", "INTRADAY").strip().upper(),
        index_exchange=os.getenv("NIFTY_INDEX_EXCHANGE", "NSE").strip().upper(),
        index_trading_symbol=os.getenv("NIFTY_INDEX_TRADING_SYMBOL", "NIFTY").strip(),
        index_symbol_token=os.getenv("NIFTY_INDEX_TOKEN", "26000").strip(),
        option_exchange=os.getenv("OPTION_EXCHANGE", "NFO").strip().upper(),
        option_name=os.getenv("OPTION_NAME", "NIFTY").strip().upper(),
        strike_step=int(os.getenv("NIFTY_STRIKE_STEP", "50")),
        order_poll_seconds=int(os.getenv("ORDER_POLL_SECONDS", "30")),
        tick_size=float(os.getenv("TICK_SIZE", "0.05")),
        stoploss_order_type=os.getenv("STOPLOSS_ORDER_TYPE", "STOPLOSS_MARKET").strip().upper(),
        stoploss_limit_buffer=float(os.getenv("STOPLOSS_LIMIT_BUFFER", "1")),
    )


def wait_until_10am(enabled: bool) -> None:
    if not enabled:
        return

    now = datetime.now(IST)
    run_at = datetime.combine(now.date(), clock_time(hour=10, minute=0), tzinfo=IST)
    if now < run_at:
        seconds = (run_at - now).total_seconds()
        logger.info(f"Waiting until 10:00 AM IST: {int(seconds)} seconds remaining.")
        time.sleep(seconds)
        return

    logger.info("10:00 AM IST has already passed; running immediately.")


def connect(config: Config) -> SmartConnect:
    smart_api = SmartConnect(config.api_key)
    totp = pyotp.TOTP(config.totp_secret).now()
    session = smart_api.generateSession(config.client_code, config.pin, totp)

    if not session or not session.get("status"):
        raise RuntimeError(f"Angel One login failed: {session}")

    logger.info("Angel One SmartAPI login successful.")
    return smart_api


def extract_ltp(response: dict[str, Any], label: str) -> float:
    if not response or not response.get("status"):
        raise RuntimeError(f"LTP fetch failed for {label}: {response}")

    data = response.get("data") or {}
    ltp = data.get("ltp") or data.get("LTP")
    if ltp is None:
        raise RuntimeError(f"LTP missing for {label}: {response}")
    return float(ltp)


def fetch_ltp(smart_api: SmartConnect, exchange: str, trading_symbol: str, token: str) -> float:
    response = smart_api.ltpData(exchange, trading_symbol, token)
    return extract_ltp(response, f"{exchange}:{trading_symbol}:{token}")


def fetch_nifty_spot(smart_api: SmartConnect, config: Config) -> float:
    candidates = [
        config.index_trading_symbol,
        "NIFTY 50",
        "Nifty 50",
    ]
    last_error: Exception | None = None

    for trading_symbol in dict.fromkeys(candidates):
        try:
            spot = fetch_ltp(smart_api, config.index_exchange, trading_symbol, config.index_symbol_token)
            logger.info(f"NIFTY spot LTP from {trading_symbol}: {spot}")
            return spot
        except Exception as exc:  # pragma: no cover - depends on broker response.
            last_error = exc
            logger.warning(f"NIFTY spot fetch failed with symbol {trading_symbol}: {exc}")

    raise RuntimeError(f"Could not fetch NIFTY spot price. Last error: {last_error}")


def nearest_atm_strike(spot: float, step: int) -> int:
    return int(round(spot / step) * step)


def fetch_scrip_master(url: str = SCRIP_MASTER_URL) -> list[dict[str, Any]]:
    with urlopen(url, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_expiry(value: str) -> datetime | None:
    value = str(value).strip().upper()
    for fmt in ("%d%b%Y", "%d%b%y", "%d-%b-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def normalized_strike(value: Any) -> int:
    strike = float(value)
    if strike > 100000:
        strike = strike / 100
    return int(round(strike))


def find_option_contracts(
    instruments: list[dict[str, Any]],
    option_name: str,
    exchange: str,
    strike: int,
) -> dict[str, OptionContract]:
    today = datetime.now(IST).date()
    matches: list[OptionContract] = []

    for item in instruments:
        if str(item.get("exch_seg", "")).upper() != exchange:
            continue
        if str(item.get("name", "")).upper() != option_name:
            continue
        if "OPT" not in str(item.get("instrumenttype", "")).upper():
            continue

        symbol = str(item.get("symbol", "")).upper()
        option_type = symbol[-2:]
        if option_type not in {"CE", "PE"}:
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
                exchange=exchange,
                expiry=expiry,
                strike=item_strike,
                lot_size=int(float(item.get("lotsize", 0) or 0)),
            )
        )

    if not matches:
        raise RuntimeError(f"No NIFTY option contracts found for strike {strike}.")

    nearest_expiry = min(contract.expiry for contract in matches)
    selected = {contract.option_type: contract for contract in matches if contract.expiry == nearest_expiry}

    missing = {"CE", "PE"} - set(selected)
    if missing:
        raise RuntimeError(f"Missing option legs for strike {strike}, expiry {nearest_expiry.date()}: {missing}")

    return selected


def money(value: float) -> str:
    return f"{value:.2f}"


def round_up_to_tick(value: float, tick_size: float) -> float:
    return round(math.ceil(value / tick_size) * tick_size, 2)


def build_sell_order(contract: OptionContract, quantity: int, product_type: str) -> dict[str, str]:
    return {
        "variety": "NORMAL",
        "tradingsymbol": contract.trading_symbol,
        "symboltoken": contract.symbol_token,
        "transactiontype": "SELL",
        "exchange": contract.exchange,
        "ordertype": "MARKET",
        "producttype": product_type,
        "duration": "DAY",
        "price": "0",
        "squareoff": "0",
        "stoploss": "0",
        "quantity": str(quantity),
    }


def build_stoploss_order(
    contract: OptionContract,
    quantity: int,
    product_type: str,
    trigger_price: float,
    config: Config,
) -> dict[str, str]:
    order = {
        "variety": "STOPLOSS",
        "tradingsymbol": contract.trading_symbol,
        "symboltoken": contract.symbol_token,
        "transactiontype": "BUY",
        "exchange": contract.exchange,
        "ordertype": config.stoploss_order_type,
        "producttype": product_type,
        "duration": "DAY",
        "price": "0",
        "triggerprice": money(trigger_price),
        "quantity": str(quantity),
    }

    if config.stoploss_order_type == "STOPLOSS_LIMIT":
        order["price"] = money(trigger_price + config.stoploss_limit_buffer)

    return order


def place_order(smart_api: SmartConnect, order_params: dict[str, str], dry_run: bool) -> dict[str, Any]:
    if dry_run:
        logger.info(f"DRY RUN order: {order_params}")
        return {"status": True, "dry_run": True, "data": {"orderid": "DRY-RUN"}}

    response = smart_api.placeOrderFullResponse(order_params)
    if not response or not response.get("status"):
        raise RuntimeError(f"Order placement failed: {response}")

    logger.info(f"Order placed: {response}")
    return response


def extract_order_id(response: dict[str, Any]) -> str | None:
    data = response.get("data") if isinstance(response, dict) else None
    if isinstance(data, dict):
        return str(data.get("orderid") or data.get("orderId") or "")
    return None


def find_order_in_book(order_book: dict[str, Any], order_id: str) -> dict[str, Any] | None:
    orders = order_book.get("data") if isinstance(order_book, dict) else []
    if not isinstance(orders, list):
        return None

    for order in orders:
        if str(order.get("orderid") or order.get("orderId")) == order_id:
            return order
    return None


def get_average_fill_price(smart_api: SmartConnect, order_id: str, timeout_seconds: int) -> float | None:
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        order_book = smart_api.orderBook()
        order = find_order_in_book(order_book, order_id)
        if order:
            status = str(order.get("orderstatus") or order.get("status") or "").lower()
            average_price = order.get("averageprice") or order.get("averagePrice")
            if status in {"complete", "completed", "filled", "traded"} and average_price:
                return float(average_price)
            if status in {"rejected", "cancelled", "canceled"}:
                raise RuntimeError(f"Sell order {order_id} ended with status {status}: {order}")
        time.sleep(2)

    return None


def execute_leg(smart_api: SmartConnect, contract: OptionContract, quantity: int, config: Config) -> dict[str, Any]:
    option_ltp = fetch_ltp(smart_api, contract.exchange, contract.trading_symbol, contract.symbol_token)
    sell_order = build_sell_order(contract, quantity, config.product_type)
    sell_response = place_order(smart_api, sell_order, config.dry_run)

    entry_price = option_ltp
    sell_order_id = extract_order_id(sell_response)

    if not config.dry_run and sell_order_id:
        average_fill = get_average_fill_price(smart_api, sell_order_id, config.order_poll_seconds)
        if average_fill is not None:
            entry_price = average_fill
        else:
            logger.warning(f"Average fill unavailable for {sell_order_id}; using pre-order LTP {option_ltp}.")

    trigger_price = round_up_to_tick(entry_price * (1 + config.stop_loss_percent / 100), config.tick_size)
    stoploss_order = build_stoploss_order(contract, quantity, config.product_type, trigger_price, config)
    stoploss_response = place_order(smart_api, stoploss_order, config.dry_run)

    return {
        "contract": contract,
        "entry_price_used": entry_price,
        "stoploss_trigger": trigger_price,
        "sell_order": sell_order,
        "sell_response": sell_response,
        "stoploss_order": stoploss_order,
        "stoploss_response": stoploss_response,
    }


def main() -> None:
    config = load_config()
    if config.order_lots < 1:
        raise RuntimeError("ORDER_LOTS must be at least 1.")
    if config.stop_loss_percent <= 0:
        raise RuntimeError("STOP_LOSS_PERCENT must be greater than 0.")

    logger.info(f"DRY_RUN={config.dry_run}. Set DRY_RUN=false only when you are ready for live orders.")
    wait_until_10am(config.wait_until_10am)

    smart_api = connect(config)
    spot = fetch_nifty_spot(smart_api, config)
    atm_strike = nearest_atm_strike(spot, config.strike_step)
    logger.info(f"NIFTY spot={spot}; selected ATM strike={atm_strike}.")

    instruments = fetch_scrip_master(os.getenv("SCRIP_MASTER_URL", SCRIP_MASTER_URL))
    contracts = find_option_contracts(instruments, config.option_name, config.option_exchange, atm_strike)

    ce_contract = contracts["CE"]
    pe_contract = contracts["PE"]
    if ce_contract.lot_size <= 0 or pe_contract.lot_size <= 0:
        raise RuntimeError(f"Invalid lot size for selected contracts: CE={ce_contract}, PE={pe_contract}")

    quantity = min(ce_contract.lot_size, pe_contract.lot_size) * config.order_lots
    logger.info(f"Selected CE={ce_contract.trading_symbol}, PE={pe_contract.trading_symbol}, quantity={quantity}.")

    results = [
        execute_leg(smart_api, ce_contract, quantity, config),
        execute_leg(smart_api, pe_contract, quantity, config),
    ]

    for result in results:
        contract = result["contract"]
        logger.info(
            f"{contract.option_type} done: sell={result['sell_response']}, "
            f"SL trigger={result['stoploss_trigger']}, SL={result['stoploss_response']}"
        )


if __name__ == "__main__":
    main()

