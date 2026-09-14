# -*- coding: utf-8 -*-
"""
Created on Sat Apr 25 15:10:36 2026

@author: HP
"""
import csv
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.request import urlopen

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


START_DATE = "2026-01-01"
END_DATE = "2026-04-24"

ENTRY_TIME = "10:00"
EXIT_TIME = "15:15"
INTERVAL = "ONE_MINUTE"

ORDER_LOTS = 1
STOP_LOSS_PERCENT = 30.0
CAPITAL_FOR_RETURN = 100000.0

NIFTY_INDEX_EXCHANGE = "NSE"
NIFTY_INDEX_TOKEN = "99926000"

OPTION_EXCHANGE = "NFO"
OPTION_NAME = "NIFTY"
NIFTY_STRIKE_STEP = 50

TICK_SIZE = 0.05
API_SLEEP_SECONDS = 0.4
OUTPUT_CSV = "nifty_short_straddle_backtest.csv"
CANDLE_CACHE_DIR = "candle_cache"
CANDLE_FETCH_RETRIES = 4
CANDLE_RETRY_SLEEP_SECONDS = 5
SUPPRESS_SMARTAPI_INTERNAL_ERRORS = True

SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
SCRIP_MASTER_CACHE_FILE = "OpenAPIScripMaster.json"
SCRIP_MASTER_TIMEOUT_SECONDS = 120
SCRIP_MASTER_RETRIES = 3


# =========================
# CHARGE / TAX SETTINGS
# =========================
# Defaults are editable estimates for NFO option trades. Your Angel contract note is final.
BROKERAGE_PER_ORDER = 20.0
STT_SELL_RATE = 0.0015
NSE_OPTION_TRANSACTION_RATE = 0.0003552
SEBI_CHARGE_RATE = 0.000001
IPFT_RATE = 0.000000001
GST_RATE = 0.18
STAMP_DUTY_BUY_RATE = 0.00003


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class OptionContract:
    option_type: str
    trading_symbol: str
    symbol_token: str
    exchange: str
    expiry: datetime
    strike: int
    lot_size: int


@dataclass(frozen=True)
class LegResult:
    option_type: str
    symbol: str
    token: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    stop_loss: float
    exit_reason: str
    points: float
    pnl: float


def parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_time_on_date(day, value):
    parsed_time = datetime.strptime(value, "%H:%M").time()
    return datetime.combine(day, parsed_time)


def connect():
    smart_api = SmartConnect(API_KEY)
    totp = pyotp.TOTP(TOTP_SECRET).now()
    session = smart_api.generateSession(CLIENT_CODE, PIN, totp)

    if not session or not session.get("status"):
        raise RuntimeError("Angel One login failed: {}".format(session))

    logger.info("Angel One SmartAPI login successful.")
    return smart_api


def load_cached_scrip_master():
    if not os.path.exists(SCRIP_MASTER_CACHE_FILE):
        return None

    logger.info("Loading scrip master from local cache: %s", os.path.abspath(SCRIP_MASTER_CACHE_FILE))
    with open(SCRIP_MASTER_CACHE_FILE, "r", encoding="utf-8") as file:
        instruments = json.load(file)
    logger.info("Loaded %s instruments from cache.", len(instruments))
    return instruments


def download_scrip_master():
    logger.info("Downloading Angel scrip master from %s", SCRIP_MASTER_URL)
    with urlopen(SCRIP_MASTER_URL, timeout=SCRIP_MASTER_TIMEOUT_SECONDS) as response:
        chunks = []
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)

    text = b"".join(chunks).decode("utf-8")
    instruments = json.loads(text)

    with open(SCRIP_MASTER_CACHE_FILE, "w", encoding="utf-8") as file:
        file.write(text)

    logger.info("Downloaded %s instruments and saved cache: %s", len(instruments), os.path.abspath(SCRIP_MASTER_CACHE_FILE))
    return instruments


def fetch_scrip_master():
    last_error = None

    for attempt in range(1, SCRIP_MASTER_RETRIES + 1):
        try:
            return download_scrip_master()
        except Exception as exc:
            last_error = exc
            logger.warning("Scrip master download attempt %s/%s failed: %s", attempt, SCRIP_MASTER_RETRIES, exc)
            time.sleep(2 * attempt)

    cached = load_cached_scrip_master()
    if cached is not None:
        logger.warning("Using cached scrip master because fresh download failed.")
        return cached

    raise RuntimeError(
        "Could not download Angel scrip master and no local cache exists. "
        "Download it manually from {} and save it as {}. Last error: {}".format(
            SCRIP_MASTER_URL,
            os.path.abspath(SCRIP_MASTER_CACHE_FILE),
            last_error,
        )
    )


def parse_expiry(value):
    value = str(value).strip().upper()
    for fmt in ("%d%b%Y", "%d%b%y", "%d-%b-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    return None


def normalized_strike(value):
    strike = float(value)
    if strike > 100000:
        strike = strike / 100
    return int(round(strike))


def find_option_contracts(instruments, strike, trade_day):
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
        if expiry is None or expiry.date() < trade_day:
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
        raise RuntimeError("No CE/PE contracts found for strike {} on {}.".format(strike, trade_day))

    nearest_expiry = min(contract.expiry for contract in matches)
    selected = {}
    for contract in matches:
        if contract.expiry == nearest_expiry:
            selected[contract.option_type] = contract

    if "CE" not in selected or "PE" not in selected:
        raise RuntimeError("Missing CE/PE for strike {}, expiry {}.".format(strike, nearest_expiry.date()))

    return selected


def parse_candle_timestamp(value):
    text = str(value).strip().replace("T", " ")
    if "+" in text:
        text = text.split("+")[0]
    if text.endswith("Z"):
        text = text[:-1]

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass

    return datetime.fromisoformat(str(value))


def row_to_candle(row):
    return Candle(
        timestamp=parse_candle_timestamp(row[0]),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=float(row[5]) if len(row) > 5 and row[5] is not None else 0.0,
    )


def candle_cache_path(exchange, symbol_token, day):
    filename = "{}_{}_{}_{}.json".format(exchange, symbol_token, INTERVAL, day.strftime("%Y%m%d"))
    return os.path.join(CANDLE_CACHE_DIR, filename)


def load_cached_candle_rows(exchange, symbol_token, day):
    path = candle_cache_path(exchange, symbol_token, day)
    if not os.path.exists(path):
        return None

    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def save_cached_candle_rows(exchange, symbol_token, day, rows):
    if not os.path.exists(CANDLE_CACHE_DIR):
        os.makedirs(CANDLE_CACHE_DIR)

    path = candle_cache_path(exchange, symbol_token, day)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(rows, file)


def get_candle_data_safely(smart_api, params):
    if not SUPPRESS_SMARTAPI_INTERNAL_ERRORS:
        return smart_api.getCandleData(params)

    logger.disabled = True
    try:
        return smart_api.getCandleData(params)
    finally:
        logger.disabled = False


def describe_empty_rows(exchange, symbol_token, day):
    if exchange == NIFTY_INDEX_EXCHANGE and symbol_token == NIFTY_INDEX_TOKEN:
        return (
            "No NIFTY index candle rows returned for token {} on {}. "
            "This can happen on exchange holidays or when Angel has missing index data."
        ).format(symbol_token, day)

    return (
        "No option candle rows returned for {} token {} on {}. "
        "This usually means Angel does not have historical candles for that expired option token."
    ).format(exchange, symbol_token, day)


def fetch_candles(smart_api, exchange, symbol_token, day):
    cached_rows = load_cached_candle_rows(exchange, symbol_token, day)
    if cached_rows is not None:
        candles = [row_to_candle(row) for row in cached_rows]
        candles.sort(key=lambda candle: candle.timestamp)
        return candles

    params = {
        "exchange": exchange,
        "symboltoken": symbol_token,
        "interval": INTERVAL,
        "fromdate": "{} 09:15".format(day.strftime("%Y-%m-%d")),
        "todate": "{} 15:30".format(day.strftime("%Y-%m-%d")),
    }

    last_error = None
    for attempt in range(1, CANDLE_FETCH_RETRIES + 1):
        try:
            response = get_candle_data_safely(smart_api, params)
            time.sleep(API_SLEEP_SECONDS)

            if not response or not response.get("status"):
                raise RuntimeError("Candle fetch failed for {} {}: {}".format(exchange, symbol_token, response))

            rows = response.get("data") or []
            if not rows:
                raise RuntimeError(describe_empty_rows(exchange, symbol_token, day))

            save_cached_candle_rows(exchange, symbol_token, day, rows)
            candles = [row_to_candle(row) for row in rows]
            candles.sort(key=lambda candle: candle.timestamp)
            return candles
        except Exception as exc:
            last_error = exc
            if attempt < CANDLE_FETCH_RETRIES:
                sleep_seconds = CANDLE_RETRY_SLEEP_SECONDS * attempt
                logger.warning(
                    "Candle fetch retry %s/%s for %s token %s on %s after error: %s",
                    attempt,
                    CANDLE_FETCH_RETRIES,
                    exchange,
                    symbol_token,
                    day,
                    exc,
                )
                time.sleep(sleep_seconds)

    raise RuntimeError("Candle fetch failed after {} attempts: {}".format(CANDLE_FETCH_RETRIES, last_error))


def candle_at_or_after(candles, target_time):
    for candle in candles:
        if candle.timestamp >= target_time:
            return candle
    return None


def candle_at_or_before(candles, target_time):
    selected = None
    for candle in candles:
        if candle.timestamp <= target_time:
            selected = candle
        else:
            break
    return selected


def nearest_atm_strike(spot, step):
    return int(round(spot / step) * step)


def round_up_to_tick(value, tick_size):
    return round(math.ceil(value / tick_size) * tick_size, 2)


def simulate_short_leg(contract, candles, entry_at, exit_at, quantity):
    entry_candle = candle_at_or_after(candles, entry_at)
    if entry_candle is None:
        raise RuntimeError("No entry candle found for {}".format(contract.trading_symbol))

    entry_price = entry_candle.open
    stop_loss = round_up_to_tick(entry_price * (1 + STOP_LOSS_PERCENT / 100), TICK_SIZE)

    exit_price = None
    exit_time = None
    exit_reason = None

    for candle in candles:
        if candle.timestamp < entry_candle.timestamp:
            continue
        if candle.timestamp > exit_at:
            break
        if candle.high >= stop_loss:
            exit_price = stop_loss
            exit_time = candle.timestamp
            exit_reason = "SL_HIT"
            break

    if exit_price is None:
        exit_candle = candle_at_or_before(candles, exit_at)
        if exit_candle is None:
            raise RuntimeError("No exit candle found for {}".format(contract.trading_symbol))
        exit_price = exit_candle.close
        exit_time = exit_candle.timestamp
        exit_reason = "TIME_EXIT"

    points = entry_price - exit_price
    pnl = points * quantity

    return LegResult(
        option_type=contract.option_type,
        symbol=contract.trading_symbol,
        token=contract.symbol_token,
        entry_time=entry_candle.timestamp,
        exit_time=exit_time,
        entry_price=entry_price,
        exit_price=exit_price,
        stop_loss=stop_loss,
        exit_reason=exit_reason,
        points=points,
        pnl=pnl,
    )


def calculate_charges(legs, quantity):
    sell_turnover = sum(leg.entry_price * quantity for leg in legs)
    buy_turnover = sum(leg.exit_price * quantity for leg in legs)
    total_turnover = sell_turnover + buy_turnover

    executed_orders = len(legs) * 2
    brokerage = executed_orders * BROKERAGE_PER_ORDER
    stt = sell_turnover * STT_SELL_RATE
    transaction = total_turnover * NSE_OPTION_TRANSACTION_RATE
    sebi = total_turnover * SEBI_CHARGE_RATE
    ipft = total_turnover * IPFT_RATE
    gst = (brokerage + transaction + sebi + ipft) * GST_RATE
    stamp = buy_turnover * STAMP_DUTY_BUY_RATE

    total = brokerage + stt + transaction + sebi + ipft + gst + stamp

    return {
        "sell_turnover": sell_turnover,
        "buy_turnover": buy_turnover,
        "total_turnover": total_turnover,
        "brokerage": brokerage,
        "stt": stt,
        "transaction": transaction,
        "sebi": sebi,
        "ipft": ipft,
        "gst": gst,
        "stamp": stamp,
        "total_charges": total,
    }


def money(value):
    return round(float(value), 2)


def percent(value):
    return round(float(value), 4)


def build_result_row(day, spot_price, atm_strike, expiry, quantity, ce_leg, pe_leg, charges):
    gross_pnl = ce_leg.pnl + pe_leg.pnl
    net_pnl = gross_pnl - charges["total_charges"]
    return_percent = (net_pnl / CAPITAL_FOR_RETURN) * 100 if CAPITAL_FOR_RETURN else 0.0

    return {
        "date": day.strftime("%Y-%m-%d"),
        "spot_entry": money(spot_price),
        "atm_strike": atm_strike,
        "expiry": expiry.strftime("%Y-%m-%d"),
        "quantity": quantity,
        "ce_symbol": ce_leg.symbol,
        "ce_entry": money(ce_leg.entry_price),
        "ce_exit": money(ce_leg.exit_price),
        "ce_sl": money(ce_leg.stop_loss),
        "ce_exit_reason": ce_leg.exit_reason,
        "ce_points": money(ce_leg.points),
        "ce_pnl": money(ce_leg.pnl),
        "pe_symbol": pe_leg.symbol,
        "pe_entry": money(pe_leg.entry_price),
        "pe_exit": money(pe_leg.exit_price),
        "pe_sl": money(pe_leg.stop_loss),
        "pe_exit_reason": pe_leg.exit_reason,
        "pe_points": money(pe_leg.points),
        "pe_pnl": money(pe_leg.pnl),
        "gross_pnl": money(gross_pnl),
        "charges": money(charges["total_charges"]),
        "net_pnl": money(net_pnl),
        "return_percent": percent(return_percent),
        "brokerage": money(charges["brokerage"]),
        "stt": money(charges["stt"]),
        "transaction_charges": money(charges["transaction"]),
        "sebi": money(charges["sebi"]),
        "ipft": money(charges["ipft"]),
        "gst": money(charges["gst"]),
        "stamp_duty": money(charges["stamp"]),
        "status": "TRADED",
        "note": "",
    }


def build_skipped_row(day, note):
    return {
        "date": day.strftime("%Y-%m-%d"),
        "spot_entry": "",
        "atm_strike": "",
        "expiry": "",
        "quantity": "",
        "ce_symbol": "",
        "ce_entry": "",
        "ce_exit": "",
        "ce_sl": "",
        "ce_exit_reason": "",
        "ce_points": "",
        "ce_pnl": "",
        "pe_symbol": "",
        "pe_entry": "",
        "pe_exit": "",
        "pe_sl": "",
        "pe_exit_reason": "",
        "pe_points": "",
        "pe_pnl": "",
        "gross_pnl": "",
        "charges": "",
        "net_pnl": "",
        "return_percent": "",
        "brokerage": "",
        "stt": "",
        "transaction_charges": "",
        "sebi": "",
        "ipft": "",
        "gst": "",
        "stamp_duty": "",
        "status": "SKIPPED",
        "note": note,
    }


def backtest_day(smart_api, instruments, day):
    entry_at = parse_time_on_date(day, ENTRY_TIME)
    exit_at = parse_time_on_date(day, EXIT_TIME)

    spot_candles = fetch_candles(smart_api, NIFTY_INDEX_EXCHANGE, NIFTY_INDEX_TOKEN, day)
    spot_candle = candle_at_or_after(spot_candles, entry_at)
    if spot_candle is None:
        raise RuntimeError("No NIFTY spot candle at or after {}".format(ENTRY_TIME))

    spot_price = spot_candle.open
    atm_strike = nearest_atm_strike(spot_price, NIFTY_STRIKE_STEP)
    contracts = find_option_contracts(instruments, atm_strike, day)

    ce_contract = contracts["CE"]
    pe_contract = contracts["PE"]
    quantity = min(ce_contract.lot_size, pe_contract.lot_size) * ORDER_LOTS
    if quantity <= 0:
        raise RuntimeError("Invalid quantity for {} / {}".format(ce_contract, pe_contract))

    ce_candles = fetch_candles(smart_api, ce_contract.exchange, ce_contract.symbol_token, day)
    pe_candles = fetch_candles(smart_api, pe_contract.exchange, pe_contract.symbol_token, day)

    ce_leg = simulate_short_leg(ce_contract, ce_candles, entry_at, exit_at, quantity)
    pe_leg = simulate_short_leg(pe_contract, pe_candles, entry_at, exit_at, quantity)

    charges = calculate_charges([ce_leg, pe_leg], quantity)
    expiry = min(ce_contract.expiry, pe_contract.expiry)
    return build_result_row(day, spot_price, atm_strike, expiry, quantity, ce_leg, pe_leg, charges)


def date_range(start_day, end_day):
    current = start_day
    while current <= end_day:
        yield current
        current = current + timedelta(days=1)


def write_csv(rows):
    if not rows:
        return

    fieldnames = list(rows[0].keys())
    with open(OUTPUT_CSV, "w", newline="") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def print_summary(rows):
    traded = [row for row in rows if row["status"] == "TRADED"]
    skipped = [row for row in rows if row["status"] == "SKIPPED"]

    gross = sum(float(row["gross_pnl"]) for row in traded)
    charges = sum(float(row["charges"]) for row in traded)
    net = sum(float(row["net_pnl"]) for row in traded)
    wins = len([row for row in traded if float(row["net_pnl"]) > 0])
    losses = len([row for row in traded if float(row["net_pnl"]) < 0])
    return_percent = (net / CAPITAL_FOR_RETURN) * 100 if CAPITAL_FOR_RETURN else 0.0

    logger.info("Backtest completed.")
    logger.info("Trading days: %s", len(traded))
    logger.info("Skipped days: %s", len(skipped))
    logger.info("Winning days: %s", wins)
    logger.info("Losing days: %s", losses)
    logger.info("Gross P&L: Rs. %s", money(gross))
    logger.info("Charges/taxes: Rs. %s", money(charges))
    logger.info("Net P&L: Rs. %s", money(net))
    logger.info("Return on configured capital: %s%%", percent(return_percent))
    logger.info("CSV saved: %s", os.path.abspath(OUTPUT_CSV))


def main():
    if ORDER_LOTS < 1:
        raise RuntimeError("ORDER_LOTS must be at least 1.")
    if STOP_LOSS_PERCENT <= 0:
        raise RuntimeError("STOP_LOSS_PERCENT must be greater than 0.")

    start_day = parse_date(START_DATE)
    end_day = parse_date(END_DATE)

    smart_api = connect()
    instruments = fetch_scrip_master()

    rows = []
    for day in date_range(start_day, end_day):
        if day.weekday() >= 5:
            logger.info("%s skipped: weekend.", day)
            rows.append(build_skipped_row(day, "Weekend"))
            continue

        try:
            logger.info("Backtesting %s...", day)
            row = backtest_day(smart_api, instruments, day)
            rows.append(row)
            logger.info(
                "%s net=%s gross=%s charges=%s",
                day,
                row["net_pnl"],
                row["gross_pnl"],
                row["charges"],
            )
        except Exception as exc:
            logger.warning("%s skipped: %s", day, exc)
            rows.append(build_skipped_row(day, str(exc)))

    write_csv(rows)
    print_summary(rows)


if __name__ == "__main__":
    main()
