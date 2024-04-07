import json
import asyncio
import os
import math
import time
import httpx
import talib
import numpy as np
import websockets
from binance.client import AsyncClient
from binance.exceptions import BinanceAPIException
from dotenv import load_dotenv
from async_trading_bot.utils import retry_on_fail

load_dotenv()


class TradingBot:
    def __init__(self, api_key, api_secret, config):
        self.api_key = api_key
        self.api_secret = api_secret
        self.client = None
        self.telegram_api = os.getenv("TELEGRAM_API")
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.risk_percentage = config["risk_percentage"]
        self.price_increase_trigger = config["price_increase_trigger"]
        self.symbol = config["symbol"]
        self.is_position_open = False
        self.side = None
        self.stop_loss_price = None
        self.current_price = None
        self.short_ema_period = config["short_ema_period"]
        self.long_ema_period = config["long_ema_period"]
        self.ema_interval = config["ema_interval"]
        self.leverage = config["leverage"]
        self.order_size = config["order_size"]

    async def init_client(self):
        self.client = await AsyncClient.create(self.api_key, self.api_secret)

    async def send_telegram_message(self, message):
        url = f"https://api.telegram.org/bot{self.telegram_api}/sendMessage"
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, params={'chat_id': self.telegram_chat_id, 'text': message})
                response.raise_for_status()  # Raises an exception for 4XX/5XX responses
        except httpx.RequestError as e:
            print(f"Request failed: {e}")
        except httpx.HTTPStatusError as e:
            print(f"Error response {e.response.status_code} while sending message: {e}")
        except Exception as e:
            print(f"An unexpected error occurred while sending a message: {e}")

    @retry_on_fail()
    async def get_latest_price(self):
        ticker = await self.client.futures_ticker(symbol=self.symbol)
        return float(ticker['lastPrice'])

    @retry_on_fail()
    async def get_balance(self, asset):    # asset='USDT'
        account_info = await self.client.futures_account()
        return next((float(a['walletBalance']) for a in account_info['assets'] if a['asset'] == asset), 0.0)

    @retry_on_fail()
    async def get_historical_data(self, interval):
        klines = await self.client.futures_klines(symbol=self.symbol, interval=interval)
        return [float(k[4]) for k in klines]  # closing prices

    async def calculate_ema(self, close_prices):
        short_ema = talib.EMA(np.array(close_prices), timeperiod=self.short_ema_period)[-1]
        long_ema = talib.EMA(np.array(close_prices), timeperiod=self.long_ema_period)[-1]
        return short_ema, long_ema

    async def adjust_precision(self, value, precision):
        format_string = "{:0.0f}"
        return format_string.format(value, precision)

    # Additional methods (calculate_quantity, futures_create_order_with_stop_loss, main logic, etc.) go here

    # Takes :param side: Order side ('BUY' or 'SELL').
    async def get_position_entry_price(self):
        if self.side == "BUY":
            target_position_side = "LONG"  # Assuming LONG for BUY orders
        elif self.side == "SELL":
            target_position_side = "SHORT"  # Assuming SHORT for SELL orders
        else:
            raise ValueError(f"Unsupported side provided: {self.side}. Expected 'BUY' or 'SELL'.")

        try:
            position_info = await self.client.futures_position_information(symbol=self.symbol)
            for pos in position_info:
                if pos['symbol'] == self.symbol and (
                        pos['positionSide'] == target_position_side or pos['positionSide'] == 'BOTH'):
                    positionAmt = float(pos['positionAmt'])
                    if positionAmt != 0:  # Ensure there's an active position
                        entryPrice = float(pos['entryPrice'])
                        # print(
                        #     f"Position side: {target_position_side}, PositionAmt: {positionAmt}, Entry Price: {entryPrice}")
                        return entryPrice, abs(positionAmt)
        except BinanceAPIException as e:
            print(f"Error getting position information for {self.symbol}: {e}")

        return None, 0  # Return None explicitly if no matching position or in case of errors

    @retry_on_fail()
    async def precision_for_stop_loss(self):
        exchange_info = await self.client.futures_exchange_info()
        symbol_info = None
        for s in exchange_info['symbols']:
            if s['symbol'] == self.symbol:
                symbol_info = s
                break

        if symbol_info:
            # For futures, the precision information is inside the filters. Example for lot size (quantity precision):
            lot_size_filter = next(
                (filter_ for filter_ in symbol_info['filters'] if filter_['filterType'] == 'LOT_SIZE'),
                None)
            quantity_precision = 0
            price_precision = 0
            if lot_size_filter:
                quantity_precision = int(-math.log10(float(lot_size_filter['stepSize'])))

            # Example for price precision:
            price_filter = next((filter for filter in symbol_info['filters'] if filter['filterType'] == 'PRICE_FILTER'),
                                None)
            if price_filter:
                price_precision = int(-math.log10(float(price_filter['tickSize'])))

            return quantity_precision, price_precision
        else:
            print("Symbol not found.")

    async def cancel_stop_loss_orders(self):
        try:
            # Retrieve all open orders for the symbol
            open_orders = await self.client.futures_get_open_orders(symbol=self.symbol)

            # Filter out stop loss orders
            stop_loss_orders = [order for order in open_orders if
                                order['type'] == 'STOP_MARKET' or order['type'] == 'STOP_LOSS_LIMIT']

            # Cancel each stop loss order
            for order in stop_loss_orders:
                await self.client.futures_cancel_order(symbol=self.symbol, orderId=order['orderId'])
                print(f"Cancelled stop loss order {order['orderId']}")

        except BinanceAPIException as e:
            print(f"Error cancelling stop loss orders: {e}")
            raise

    async def close_order(self):
        """Close existing order and manage stop loss based on the side (BUY/SELL)."""
        try:
            # Cancel existing stop loss orders before placing a new order.
            await self.cancel_stop_loss_orders()

            # Fetch current positions to identify the one to close
            position_info = await self.client.futures_position_information(symbol=self.symbol)
            for pos in position_info:
                side = "BUY" if float(pos['positionAmt']) > 0 else "SELL"
                if (side == 'BUY' and float(pos['positionAmt']) > 0) or (
                        side == 'SELL' and float(pos['positionAmt']) < 0):
                    # Calculate the quantity to close based on the position amount
                    quantity_to_close = abs(float(pos['positionAmt']))

                    # Place an order to close the position
                    close_position_response = await self.client.futures_create_order(
                        symbol=self.symbol,
                        side='SELL' if side == 'BUY' else 'BUY',  # Opposite action to close
                        type='MARKET',
                        quantity=quantity_to_close)
                    print(f"Closed position for {self.symbol} with order: {close_position_response}")

        except BinanceAPIException as e:
            print(f"Error closing order for {self.symbol}: {e}")
            raise

    async def calculate_quantity(self, percentage):
        # Fetch exchange information
        exchange_info = await self.client.get_exchange_info()
        if self.symbol == "1000PEPEUSDT":
            symbol_new = "PEPEUSDT"
        else:
            symbol_new = self.symbol
        pair_info = next((item for item in exchange_info['symbols'] if item['symbol'] == symbol_new), None)

        if not pair_info:
            raise ValueError(f"Information for {self.symbol} not found. Cannot calculate quantity.")

        # Extracting precision for quantity
        lot_size_filter = next(filter for filter in pair_info['filters'] if filter['filterType'] == 'LOT_SIZE')
        quantity_precision = int(-math.log(float(lot_size_filter['stepSize']), 10))

        # Assuming all trading pairs are with USDT and calculating based on the wallet balance
        account_balance = await self.get_balance('USDT')
        if account_balance == 0:
            raise ValueError("Insufficient balance to place order.")
        # Calculate the desired quantity
        latest_price = await self.get_latest_price()
        desired_quantity_value = (percentage / 100) * account_balance / latest_price
        adjusted_quantity = await self.adjust_precision(desired_quantity_value, quantity_precision)
        return adjusted_quantity

    @retry_on_fail()
    async def futures_create_order_with_stop_loss(self, leverage: int, percentage: float,
                                            order_type: str = 'MARKET', price: float = None,
                                            time_in_force: str = 'GTC'):
        """
        Places a futures order and sets a stop-loss order on Binance Futures.

        Required Parameters:
        :param side: Order side ('BUY' or 'SELL').
        :param leverage: The leverage for the futures order.
        :param percentage: The percentage of the balance.
        :param stop_loss_price: The price at which to trigger the stop loss.

        Optional Parameters:
        :param order_type: Type of the primary order ('MARKET' or 'LIMIT'). Default is 'MARKET'.
        :param price: Required if order_type is 'LIMIT'. The price of the limit order.
        :param time_in_force: Time in force for the order (e.g., 'GTC' for Good Till Cancel). Relevant for limit orders.
        """
        try:
            # Set the leverage for the symbol
            await self.client.futures_change_leverage(symbol=self.symbol, leverage=leverage)

            quantity = await self.calculate_quantity(percentage)

            timestamp = int(time.time() * 1000)

            # Place the primary futures order
            if order_type == 'MARKET':
                order_response = await self.client.futures_create_order(symbol=self.symbol, side=self.side, type=order_type,
                                                                  quantity=quantity,
                                                                  timestamp=timestamp)
            elif order_type == 'LIMIT':
                assert price is not None, "Price must be specified for LIMIT orders."
                order_response = await self.client.futures_create_order(symbol=self.symbol, side=self.side, type=order_type,
                                                                  quantity=quantity,
                                                                  timestamp=timestamp,
                                                                  price=price, timeInForce=time_in_force)
            else:
                raise ValueError("Unsupported order type provided.")
            print(f"Order placed: {order_response}")
            await asyncio.sleep(2)
            self.is_position_open = True
            entry_price, position_size = await self.get_position_entry_price()
            stop_loss_price = entry_price * (1 - self.risk_percentage if self.side == 'BUY' else 1 + self.risk_percentage)
            print(stop_loss_price)

            stop_loss_response = await self.create_stop_loss_order(stop_loss_price, position_size)
            return order_response, stop_loss_response

        except BinanceAPIException as e:
            print(f"Failed to create order for {self.symbol}. Error: {e}")
            raise
        except AssertionError as e:
            print(f"Assertion Error: {e}")
            raise

    @retry_on_fail()
    async def create_stop_loss_order(self, new_stop_loss_price, position_size):
        if not self.is_position_open or self.side is None:
            print("No open position to set a stop-loss order for.")
            return
        try:
            quantity_precision, price_precision = await self.precision_for_stop_loss()
            adjusted_stop_loss_price = round(new_stop_loss_price, price_precision)
            stop_loss_response = await self.client.futures_create_order(
                symbol=self.symbol,
                side='SELL' if self.side == 'BUY' else 'BUY',  # Opposite action for stop-loss
                type='STOP_MARKET',
                quantity=round(float(position_size), quantity_precision),  # Adjust as necessary for partial stop losses
                stopPrice=adjusted_stop_loss_price
            )
            print(f"Stop-loss order placed: {stop_loss_response}")
            await self.send_telegram_message(f"Stop-loss order placed. Stop loss price: {adjusted_stop_loss_price}")
            return stop_loss_response

        except BinanceAPIException as e:
            print(f"Failed to create order for {self.symbol}. Error: {e}")
            raise
        except AssertionError as e:
            print(f"Assertion Error: {e}")
            raise

    async def adjust_stop_loss_on_exchange(self, new_stop_loss_price, position_size):
        """Adjust the stop loss order to the new price."""
        # Example: Cancel the existing stop loss order (you'll need its order ID)
        await self.cancel_stop_loss_orders()
        await asyncio.sleep(2)
        stop_loss_resp = await self.create_stop_loss_order(new_stop_loss_price, position_size)
        print(f"Updated stop loss order with new price: {new_stop_loss_price} Stop-loss order placed: {stop_loss_resp}")

    async def start_websocket(self):
        while True:  # Keep attempting to reconnect if the connection is lost
            try:
                async with websockets.connect(f'wss://fstream.binance.com/ws/{self.symbol.lower()}@kline_1m') as ws:
                    await self.process_websocket_messages(ws)
            except websockets.exceptions.ConnectionClosed as e:
                print(f"WebSocket connection closed: {e}")
                await asyncio.sleep(7)  # Wait before attempting to reconnect
            except Exception as e:
                print(f"An error occurred: {e}")
                await asyncio.sleep(7)  # Wait before attempting to reconnect

    async def process_websocket_messages(self, ws):
        async for message in ws:
            try:
                if not self.is_position_open:
                    return
                data = json.loads(message)
                new_stop_loss_price = 0.0
                if data.get('e') == 'error':
                    print(f"Websocket error {data.get('m')}")
                    return
                if data.get('e') == 'kline':
                    entry_price, position_size = await self.get_position_entry_price()
                    self.current_price = float(data['k']['c'])

                    if position_size == 0:
                        return  # No open position, nothing to adjust.

                    if self.side == 'BUY':
                        # For a long position, adjust stop loss if the current price is significantly higher than entry.
                        if self.current_price >= entry_price * (1 + self.price_increase_trigger):
                            new_stop_loss_price = self.current_price - (self.current_price * self.risk_percentage)
                            if new_stop_loss_price != self.stop_loss_price and new_stop_loss_price > 0:
                                print(
                                    f"Entry price: {entry_price}, Current_price: {self.current_price} New stop loss: {new_stop_loss_price} | "
                                    f"if {self.current_price} >= {entry_price * (1 + self.price_increase_trigger)}")
                                self.price_increase_trigger += 0.06
                                await self.adjust_stop_loss_on_exchange(new_stop_loss_price, abs(position_size))
                                self.stop_loss_price = new_stop_loss_price  # Update the stop price for subsequent adjustments.
                                print(f"New stop loss: {self.stop_loss_price}")
                            else:
                                print(f"Invalid stop-loss price calculated: {new_stop_loss_price}")

                    elif self.side == 'SELL':
                        # For a short position, adjust stop loss if the current price is significantly lower than entry.
                        if self.current_price <= entry_price * (1 - self.price_increase_trigger):
                            new_stop_loss_price = self.current_price + (self.current_price * self.risk_percentage)
                            if new_stop_loss_price != self.stop_loss_price and new_stop_loss_price > 0:
                                print(
                                    f"Entry price: {entry_price}, Current_price: {self.current_price} New stop loss: {new_stop_loss_price} | "
                                    f"if {self.current_price} >= {entry_price * (1 + self.price_increase_trigger)}")
                                self.price_increase_trigger += 0.06
                                await self.adjust_stop_loss_on_exchange(new_stop_loss_price, abs(position_size))
                                self.stop_loss_price = new_stop_loss_price  # Update the stop price for subsequent adjustments.
                                print(f"New stop loss: {self.stop_loss_price}")
                            else:
                                print(f"Invalid stop-loss price calculated: {new_stop_loss_price}")

            except json.JSONDecodeError as e:
                print(f"Error decoding message: {e}")
