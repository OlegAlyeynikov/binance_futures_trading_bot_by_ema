import time
from requests.exceptions import RequestException
from binance.exceptions import BinanceAPIException
import asyncio
import functools
import aiofiles
import json


async def load_config_async(path):
    async with aiofiles.open(path, 'r') as file:
        contents = await file.read()
        config = json.loads(contents)
        return config


def retry_on_fail(attempts=3, delay=2):
    """
    This version of the retry_on_fail decorator checks if the function it decorates is an asynchronous coroutine
    function using asyncio.iscoroutinefunction(func). Based on this check, it returns the appropriate wrapper function:

    wrapper_async for async functions, utilizing await for both the function call and the sleep delay.
    wrapper_sync for synchronous functions, using standard blocking time.sleep for delays.
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper_async(*args, **kwargs):
            for attempt in range(attempts):
                try:
                    if asyncio.iscoroutinefunction(func):
                        return await func(*args, **kwargs)
                    else:
                        return func(*args, **kwargs)
                except (BinanceAPIException, RequestException) as e:
                    print(f"Attempt {attempt + 1} failed for {func.__name__}: {e}")
                    if attempt < attempts - 1:
                        await asyncio.sleep(delay)  # Use asyncio.sleep for async functions
                    else:
                        raise

        @functools.wraps(func)
        def wrapper_sync(*args, **kwargs):
            for attempt in range(attempts):
                try:
                    return func(*args, **kwargs)
                except (BinanceAPIException, RequestException) as e:
                    print(f"Attempt {attempt + 1} failed for {func.__name__}: {e}")
                    if attempt < attempts - 1:
                        time.sleep(delay)
                    else:
                        raise

        if asyncio.iscoroutinefunction(func):
            return wrapper_async
        else:
            return wrapper_sync

    return decorator
