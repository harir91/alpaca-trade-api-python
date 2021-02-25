"""
Stream V2
For historic reasons stream2.py contains the old api version.
Don't get confused
"""
import asyncio
import logging
import json
import msgpack
import os
import re

import websockets

from .common import get_base_url, get_data_stream_url, get_credentials, URL
from .entity import Entity
from .entity_v2 import quote_mapping_v2, trade_mapping_v2, bar_mapping_v2, \
    Trade, Quote, Bar

log = logging.getLogger(__name__)


def _ensure_coroutine(handler):
    if not asyncio.iscoroutinefunction(handler):
        raise ValueError('handler must be a coroutine function')


class DataStream:
    def __init__(self,
                 key_id: str,
                 secret_key: str,
                 base_url: URL,
                 raw_data: bool,
                 feed: str = 'iex'):
        self._key_id = key_id
        self._secret_key = secret_key
        self._feed = feed
        base_url = re.sub(r'^http', 'ws', base_url)
        self._endpoint = base_url + '/v2/' + self._feed
        self._trade_handlers = {}
        self._quote_handlers = {}
        self._bar_handlers = {}
        self._ws = None
        self._running = False
        self._raw_data = raw_data

    async def _connect(self):
        self._ws = await websockets.connect(
            self._endpoint,
            extra_headers={'Content-Type': 'application/msgpack'})
        r = await self._ws.recv()
        msg = msgpack.unpackb(r)
        if msg[0]['T'] != 'success' or msg[0]['msg'] != 'connected':
            raise ValueError('connected message not received')

    async def _auth(self):
        await self._ws.send(
            msgpack.packb({
                'action': 'auth',
                'key': self._key_id,
                'secret': self._secret_key,
            }))
        r = await self._ws.recv()
        msg = msgpack.unpackb(r)
        if msg[0]['T'] == 'error':
            raise ValueError(msg[0].get('msg', 'auth failed'))
        if msg[0]['T'] != 'success' or msg[0]['msg'] != 'authenticated':
            raise ValueError('failed to authenticate')

    def _cast(self, msg_type, msg):
        result = msg
        if not self._raw_data:
            # convert msgpack timestamp to nanoseconds
            if 't' in msg:
                msg['t'] = msg['t'].seconds * int(1e9) + msg['t'].nanoseconds

            if msg_type == 't':
                result = Trade({
                    trade_mapping_v2[k]: v
                    for k, v in msg.items() if k in trade_mapping_v2
                })
            elif msg_type == 'q':
                result = Quote({
                    quote_mapping_v2[k]: v
                    for k, v in msg.items() if k in quote_mapping_v2
                })
            elif msg_type == 'b':
                result = Bar({
                    bar_mapping_v2[k]: v
                    for k, v in msg.items() if k in bar_mapping_v2
                })
            else:
                result = Entity(msg)
        return result

    async def _dispatch(self, msg):
        msg_type = msg.get('T')
        symbol = msg.get('S')
        if msg_type == 't':
            handler = self._trade_handlers.get(
                symbol, self._trade_handlers.get('*', None))
            if handler:
                await handler(self._cast(msg_type, msg))
        elif msg_type == 'q':
            handler = self._quote_handlers.get(
                symbol, self._quote_handlers.get('*', None))
            if handler:
                await handler(self._cast(msg_type, msg))
        elif msg_type == 'b':
            handler = self._bar_handlers.get(symbol,
                                             self._bar_handlers.get('*', None))
            if handler:
                await handler(self._cast(msg_type, msg))
        elif msg_type == 'subscription':
            log.info(f'subscribed to trades: {msg.get("trades", [])}, ' +
                     f'quotes: {msg.get("quotes", [])} ' +
                     f'and bars: {msg.get("bars", [])}')
        elif msg_type == 'error':
            log.error(f'error: {msg.get("msg")} ({msg.get("code")})')

    async def _subscribe_all(self):
        if self._trade_handlers or self._quote_handlers or self._bar_handlers:
            await self._ws.send(
                msgpack.packb({
                    'action': 'subscribe',
                    'trades': tuple(self._trade_handlers.keys()),
                    'quotes': tuple(self._quote_handlers.keys()),
                    'bars': tuple(self._bar_handlers.keys()),
                }))

    def _subscribe(self, handler, symbols, handlers):
        _ensure_coroutine(handler)
        for symbol in symbols:
            handlers[symbol] = handler
        if self._running:
            asyncio.get_event_loop().run_until_complete(self._subscribe_all())

    async def _unsubscribe(self, trades=(), quotes=(), bars=()):
        if trades or quotes or bars:
            await self._ws.send(
                msgpack.packb({
                    'action': 'unsubscribe',
                    'trades': trades,
                    'quotes': quotes,
                    'bars': bars,
                }))

    def subscribe_trades(self, handler, *symbols):
        self._subscribe(handler, symbols, self._trade_handlers)

    def subscribe_quotes(self, handler, *symbols):
        self._subscribe(handler, symbols, self._quote_handlers)

    def subscribe_bars(self, handler, *symbols):
        self._subscribe(handler, symbols, self._bar_handlers)

    def unsubscribe_trades(self, *symbols):
        if self._running:
            asyncio.get_event_loop().run_until_complete(
                self._unsubscribe(trades=symbols))
        for symbol in symbols:
            del self._trade_handlers[symbol]

    def unsubscribe_quotes(self, *symbols):
        if self._running:
            asyncio.get_event_loop().run_until_complete(
                self._unsubscribe(quotes=symbols))
        for symbol in symbols:
            del self._quote_handlers[symbol]

    def unsubscribe_bars(self, *symbols):
        if self._running:
            asyncio.get_event_loop().run_until_complete(
                self._unsubscribe(bars=symbols))
        for symbol in symbols:
            del self._bar_handlers[symbol]

    async def _start_ws(self):
        await self._connect()
        await self._auth()
        log.info(f'connected to: {self._endpoint}')
        await self._subscribe_all()

    async def _consume(self):
        while True:
            r = await self._ws.recv()
            msgs = msgpack.unpackb(r)
            for msg in msgs:
                await self._dispatch(msg)

    async def _run_forever(self):
        # do not start the websocket connection until we subscribe to something
        while not (self._trade_handlers or self._quote_handlers
                   or self._bar_handlers):
            await asyncio.sleep(0.1)
        log.info('started data stream')
        retries = 0
        while True:
            self._running = False
            try:
                await self._start_ws()
                self._running = True
                retries = 0
                await self._consume()
            except websockets.WebSocketException as wse:
                retries += 1
                if retries > int(os.environ.get('APCA_RETRY_MAX', 3)):
                    raise ConnectionError("max retries exceeded")
                if retries > 1:
                    await asyncio.sleep(
                        int(os.environ.get('APCA_RETRY_WAIT', 3)))
                logging.warn('websocket error, restarting connection: ' +
                             str(wse))
            finally:
                await self.close()

    def run(self):
        try:
            asyncio.get_event_loop().run_until_complete(self._run_forever())
        except KeyboardInterrupt:
            log.info('exited')

    async def close(self):
        if self._ws:
            await self._ws.close()
            self._ws = None


class TradingStream:
    def __init__(self, key_id: str, secret_key: str, base_url: URL):
        self._key_id = key_id
        self._secret_key = secret_key
        base_url = re.sub(r'^http', 'ws', base_url)
        self._endpoint = base_url + '/stream/'
        self._trade_updates_handler = None
        self._ws = None
        self._running = False

    async def _connect(self):
        self._ws = await websockets.connect(self._endpoint)

    async def _auth(self):
        await self._ws.send(
            json.dumps({
                'action': 'authenticate',
                'data': {
                    'key_id': self._key_id,
                    'secret_key': self._secret_key,
                }
            }))
        r = await self._ws.recv()
        msg = json.loads(r)
        if msg.get('data').get('status') != 'authorized':
            raise ValueError('failed to authenticate')

    async def _dispatch(self, msg):
        stream = msg.get('stream')
        if stream == 'trade_updates':
            if self._trade_updates_handler:
                await self._trade_updates_handler(Entity(msg.get('data')))

    async def _subscribe_trade_updates(self):
        if self._trade_updates_handler:
            await self._ws.send(
                json.dumps({
                    'action': 'listen',
                    'data': {
                        'streams': ['trade_updates']
                    }
                }))

    def subscribe_trade_updates(self, handler):
        _ensure_coroutine(handler)
        self._trade_updates_handler = handler
        if self._running:
            asyncio.get_event_loop().run_until_complete(
                self._subscribe_trade_updates())

    async def _start_ws(self):
        await self._connect()
        await self._auth()
        log.info(f'connected to: {self._endpoint}')
        await self._subscribe_trade_updates()

    async def _consume(self):
        while True:
            r = await self._ws.recv()
            msg = json.loads(r)
            await self._dispatch(msg)

    async def _run_forever(self):
        # do not start the websocket connection until we subscribe to something
        while not self._trade_updates_handler:
            await asyncio.sleep(0.1)
        log.info('started trading stream')
        retries = 0
        while True:
            self._running = False
            try:
                await self._start_ws()
                self._running = True
                retries = 0
                await self._consume()
            except asyncio.CancelledError:
                log.info('cancelled, closing trading stream connection')
                return
            except websockets.WebSocketException as wse:
                retries += 1
                if retries > int(os.environ.get('APCA_RETRY_MAX', 3)):
                    raise ConnectionError("max retries exceeded")
                if retries > 1:
                    await asyncio.sleep(
                        int(os.environ.get('APCA_RETRY_WAIT', 3)))
                logging.warn('websocket error, restarting connection: ' +
                             str(wse))
            finally:
                await self.close()

    def run(self):
        try:
            asyncio.get_event_loop().run_until_complete(self._run_forever())
        except KeyboardInterrupt:
            log.info('exited')

    async def close(self):
        if self._ws:
            await self._ws.close()
            self._ws = None


class Stream:
    def __init__(self,
                 key_id: str = None,
                 secret_key: str = None,
                 base_url: URL = None,
                 data_stream_url: URL = None,
                 data_feed: str = 'iex',
                 raw_data: bool = False):
        self._key_id, self._secret_key, _ = get_credentials(key_id, secret_key)
        self._base_url = base_url or get_base_url()
        self._data_steam_url = data_stream_url or get_data_stream_url()
        print(self._data_steam_url)

        self._trading_ws = TradingStream(self._key_id, self._secret_key,
                                         self._base_url)
        self._data_ws = DataStream(self._key_id, self._secret_key,
                                   self._data_steam_url, raw_data, data_feed)

    def subscribe_trade_updates(self, handler):
        self._trading_ws.subscribe_trade_updates(handler)

    def subscribe_trades(self, handler, *symbols):
        self._data_ws.subscribe_trades(handler, *symbols)

    def subscribe_quotes(self, handler, *symbols):
        self._data_ws.subscribe_quotes(handler, *symbols)

    def subscribe_bars(self, handler, *symbols):
        self._data_ws.subscribe_bars(handler, *symbols)

    def on_trade_update(self, func):
        self.subscribe_trade_updates(func)
        return func

    def on_trade(self, *symbols):
        def decorator(func):
            self.subscribe_trades(func, *symbols)
            return func

        return decorator

    def on_quote(self, *symbols):
        def decorator(func):
            self.subscribe_quotes(func, *symbols)
            return func

        return decorator

    def on_bar(self, *symbols):
        def decorator(func):
            self.subscribe_bars(func, *symbols)
            return func

        return decorator

    def unsubscribe_trades(self, *symbols):
        self._data_ws.unsubscribe_trades(*symbols)

    def unsubscribe_quotes(self, *symbols):
        self._data_ws.unsubscribe_quotes(*symbols)

    def unsubscribe_bars(self, *symbols):
        self._data_ws.unsubscribe_bars(*symbols)

    async def _run_forever(self):
        await asyncio.gather(self._trading_ws._run_forever(),
                             self._data_ws._run_forever())

    def run(self):
        loop = asyncio.get_event_loop()
        try:
            loop.run_until_complete(self._run_forever())
        except KeyboardInterrupt:
            print('keyboard interrupt, bye')
            pass
