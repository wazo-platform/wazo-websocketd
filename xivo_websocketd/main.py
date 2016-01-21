#!/usr/bin/env python

import asyncio
import asynqp
import websockets
import logging
import queue
import ssl
import sys
import yaml

from functools import partial
from websockets.exceptions import InvalidState

from xivo_auth_client import Client as client_auth
from contextlib import contextmanager

logger = logging.getLogger(__name__)


clients = set()
msgQueue = queue.Queue()

config_filename = '/etc/xivo-websocketd/config.yml'
config = {
    'websocket': {
        'listen': '0.0.0.0',
        'port': 9600,
        'certificate': '/usr/share/xivo-certs/server.crt',
        'private_key': '/usr/share/xivo-certs/server.key',
        'ciphers': 'ALL:!aNULL:!eNULL:!LOW:!EXP:!RC4:!3DES:!SEED:+HIGH:+MEDIUM',
    },
    'bus': {
        'username': 'xivo',
        'password': 'xivo',
        'host': '192.168.32.80',
        'port': 5672,
        'exchange_name': 'xivo',
        'exchange_type': 'topic',
        'exchange_durable': True,
    },
    'auth': {
        'host': '192.168.32.80',
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
def is_authentified(websocket, ping_period=30, token=None):
    while True:
        if not check_auth(token):
            print('Token is not valid!')
            yield from websocket.close()
            return

        yield from asyncio.sleep(ping_period)

@asyncio.coroutine
def ws_client(websocket, path):
    asyncio.async(is_authentified(websocket,
                                  ping_period=10,
                                  token=get_token(websocket.raw_request_headers, path)))
    clients.add(websocket)

    asyncio.async(keep_alive(websocket, ping_period=30))


    while True:
        if not websocket.open:
            clients.remove(websocket)
            return

        msg = yield from get_messages()
        active_clients = set([elem for elem in clients if elem.open])
        for client in active_clients:
            if client.open:
                try:
                    yield from client.send(msg)
                    yield from asyncio.sleep(0.1)
                except Exception as e:
                    print(e)

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

    yield from queue.bind(exchange=exchange, routing_key='calls.#')
    yield from queue.bind(exchange=exchange, routing_key='chat.message.#')

    while True:
        received_message = yield from queue.get()

        if received_message:
            msgQueue.put(received_message.body.decode("utf-8"))
            received_message.ack()

        yield from asyncio.sleep(0.1)

    yield from channel.close()
    yield from connection.close()


def check_auth(token):
    if token:
        with new_auth_client(config['auth']) as auth:
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
    load_config()
    wait_for_rabbitmq()

    asyncio.async(bus_consumer(config['bus'], msgQueue))
    print('Starting service on {}:{}'.format(config['websocket']['listen'], config['websocket']['port']))

    ssl_context = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
    ssl_context.load_cert_chain(config['websocket']['certificate'], config['websocket']['private_key'])
    ssl_context.set_ciphers(config['websocket']['ciphers'])
    kwds = {'ssl': ssl_context}

    start_server = websockets.serve(
        partial(ws_client),
        config['websocket']['listen'],
        config['websocket']['port'],
        **kwds)

    asyncio.get_event_loop().run_until_complete(start_server)
    asyncio.get_event_loop().run_forever()


def load_config():
    # poor's man load_config (can't use xivo.config_helpers from python3), just
    # for at least have a first integration tests working
    try:
        with open(config_filename) as fobj:
            file_config = yaml.load(fobj)
    except EnvironmentError as e:
        print('Could not read config file {}: {}'.format(config_filename, e), file=sys.stderr)
        print(e)
    else:
        for k, v in file_config.items():
            if isinstance(v, dict):
                config.setdefault(k, {})
                config[k].update(v)
            else:
                config[k] = v


def wait_for_rabbitmq():
    # XXX temporary solution for the integrations tests to work -- real
    #     solution is to reconnect to rabbitmq if connection failed
    import time
    time.sleep(3)


if __name__ == '__main__':
    main()
