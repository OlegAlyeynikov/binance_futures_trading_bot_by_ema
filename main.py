import asyncio
import os
from dotenv import load_dotenv
from async_trading_bot.trading_bot import TradingBot
from async_trading_bot.utils import load_config_async


load_dotenv()

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET_KEY")


async def main():
    config = await load_config_async(os.getenv("CONFIG_PATH"))
    trade_bot = TradingBot(API_KEY, API_SECRET, config)
    await trade_bot.init_client()
    print("Init client")
    last_action = {}
    earning = 0.0
    balance = await trade_bot.get_balance('USDT')
    print(f"Balance: {balance}")
    stop_price = 0.0
    last_action['side'] = None
    websocket_task = asyncio.create_task(trade_bot.start_websocket())
    while True:
        try:
            order_amount = None
            close_prices = await trade_bot.get_historical_data(trade_bot.ema_interval)
            short_ema, long_ema = await trade_bot.calculate_ema(close_prices)
            latest_price = await trade_bot.get_latest_price()

            if short_ema > long_ema and last_action['side'] != 'BUY':
                trade_bot.side = 'BUY'
                await trade_bot.close_order()
                new_balance = await trade_bot.get_balance('USDT')
                order_amount = balance - new_balance
                earning += (new_balance - balance)
                balance = new_balance
                print("Create new BUY order")
                order_response, stop_loss_response = await trade_bot.futures_create_order_with_stop_loss(
                    trade_bot.leverage, trade_bot.order_size)
                trade_bot.stop_loss_price = stop_loss_response["stopPrice"]
                if order_response and order_response['status'] == trade_bot.client.ORDER_STATUS_NEW:
                    last_action = order_response
                    print(last_action)
                    await trade_bot.send_telegram_message(
                        f"Placed BUY order at {latest_price}. Symbol: {trade_bot.symbol} Qty: {order_amount} USDT, "
                        f"Short EMA: {short_ema}, Long EMA: {long_ema}  StopPrice: {stop_price} Earning: {earning}")

            elif short_ema < long_ema and last_action['side'] != 'SELL':
                trade_bot.side = 'SELL'
                await trade_bot.close_order()
                new_balance = await trade_bot.get_balance('USDT')
                order_amount = balance - new_balance
                earning += (new_balance - balance)
                balance = new_balance
                order_response, stop_loss_response = await trade_bot.futures_create_order_with_stop_loss(
                    trade_bot.leverage, trade_bot.order_size)
                trade_bot.stop_loss_price = stop_loss_response["stopPrice"]
                if order_response and order_response['status'] == trade_bot.client.ORDER_STATUS_NEW:
                    last_action = order_response
                    print(last_action)
                    await trade_bot.send_telegram_message(
                        f"Placed SELL order at {latest_price}.  Symbol: {trade_bot.symbol} Qty: {order_amount} USDT, "
                        f"Short EMA: {short_ema}, Long EMA: {long_ema} StopPrice: {stop_price} Earning: {earning}")
            # print(
            #     f"Time: {datetime.datetime.now()} Symbol: {trade_bot.symbol} Balance: ${balance} Earning: ${earning} Latest price: ${latest_price}. "
            #     f"Qty: {order_amount} USDT, Short EMA: ${short_ema}, Long EMA: ${long_ema} StopPrice: {trade_bot.stop_loss_price} Current_price: {trade_bot.current_price}")
            await asyncio.sleep(5)     # Wait for 1 minute before the next iteration
        except Exception as e:
            await trade_bot.send_telegram_message(f"Timeout error when communicating with Binance's API. Retrying. {e}")
            print(f"Timeout error when communicating with Binance's API. Retrying... {e}")
            continue


if __name__ == "__main__":
    asyncio.run(main())
