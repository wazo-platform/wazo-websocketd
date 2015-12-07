#!/usr/bin/env python

import asyncio
import asynqp
import websockets
import logging
import queue
import ssl

from functools import partial
from websockets.exceptions import InvalidState

from xivo_auth_client import Client as client_auth
from contextlib import contextmanager

logger = logging.getLogger(__name__)


clients = set()
msgQueue = queue.Queue()

config = {
    'ws': {
        'listen': '0.0.0.0',
        'port': 9600,
        'certificate': '/usr/share/xivo-certs/server.crt',
        'private_key': '/usr/share/xivo-certs/server.key',
        'ciphers': 'ALL:!aNULL:!eNULL:!LOW:!EXP:!RC4:!3DES:!SEED:+HIGH:+MEDIUM',
    },
    'bus': {
        'username': 'xivo',
        'password': 'xivo',
        'host': '192.168.1.124',
        'port': 5672,
        'exchange_name': 'xivo',
        'exchange_type': 'topic',
        'exchange_durable': True,
    },
    'auth': {
        'host': '192.168.1.124',
        'port': 9497,
        'verify_certificate': False,
    },
}


@asyncio.coroutine
def keep_alive(websocket, ping_period=30):
    while True:
        yield from asyncio.sleep(ping_period)

        try:
            yield from websocket.ping()
        except InvalidState:
            print('Got exception when trying to keep connection alive, giving up.')
            return

@asyncio.coroutine
def ws_client(websocket, path):
    if not check_auth(get_token(websocket.raw_request_headers, path)):
        print("Auth invalid...")
        yield from websocket.close()

    asyncio.async(keep_alive(websocket))
    clients.add(websocket)

    try:
       while True:
           if not websocket.open:
               return

           msg = yield from get_messages()
           for client in clients:
               yield from client.send(msg)
    except Exception as e:
        print("Ouch... {} -> {}".format(e, websocket))
    finally:
        clients.remove(websocket)

    yield from websocket.close()

@asyncio.coroutine
def get_messages():
    loop = asyncio.get_event_loop()
    return (yield from loop.run_in_executor(None, msgQueue.get))

@asyncio.coroutine
def bus_consumer(config, msgQueue):
    print("Bus Connected. amqp://{}:{}@{}:{}".format(config['username'], config['password'], config['host'], config['port']))
    connection = yield from asynqp.connect(host=config['host'], port=config['port'], username=config['username'], password=config['password'])
    channel = yield from connection.open_channel()

    exchange = yield from channel.declare_exchange(config['exchange_name'], config['exchange_type'], durable=config['exchange_durable'])
    queue = yield from channel.declare_queue(name='', exclusive=True)

    yield from queue.bind(exchange=exchange, routing_key='calls')

    while True:
        received_message = yield from queue.get()

        if received_message:
            msgQueue.put(received_message.body.decode("utf-8"))
            received_message.ack()

    yield from channel.close()
    yield from connection.close()


def check_auth(token):
    if token:
        with new_auth_client(config['auth']) as auth:
            print(token)
            return auth.token.is_valid(token)
    return False

def get_token(headers, path):
    try:
        token = path.split('=')[1]
        return token
    except:
        pass

    for key, value in headers:
        if key.lower() == 'x-auth-token':
            return value
    return None

@contextmanager
def new_auth_client(config):
    yield client_auth(**config)


def main():
    asyncio.async(bus_consumer(config['bus'], msgQueue))
    print('Starting service on {}:{}'.format(config['ws']['listen'], config['ws']['port']))

    ssl_context = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
    ssl_context.load_cert_chain(config['ws']['certificate'], config['ws']['private_key'])
    ssl_context.set_ciphers(config['ws']['ciphers'])
    kwds = {'ssl': ssl_context}

    start_server = websockets.serve(
        partial(ws_client),
        config['ws']['listen'],
        config['ws']['port'],
        **kwds)

    asyncio.get_event_loop().run_until_complete(start_server)
    asyncio.get_event_loop().run_forever()

if __name__ == '__main__':
    main()
