#!/usr/bin/env python

import asyncio
import datetime
import websockets
import logging
import json
import time
import queue

from multiprocessing import Process
from multiprocessing import Queue

logger = logging.getLogger(__name__)


clients = set()

class CoreWs(object):

    def __init__(self, queue):
        self.queue = queue
        self.start_server = websockets.serve(self.ws_ari, '0.0.0.0', 8000)

    def run(self):
        asyncio.get_event_loop().run_until_complete(self.start_server)
        asyncio.get_event_loop().run_forever()

    @asyncio.coroutine
    def ws_ari(self, websocket, path):
        clients.add(websocket)
        while True:
            if not websocket.open:
                return
            try:
                msg = self.queue.get(block=False)
            except queue.Empty:
                msg = None
            if msg:
                for c in clients:
                    try:
                        yield from c.send(msg)
                    except:
                        clients.remove(c)
            yield from asyncio.sleep(0.5)


class CoreCallControl(object):

    def __init__(self, queue):
        self.queue = queue

    @asyncio.coroutine
    def ws(self):
        asterisk = "ws://192.168.1.124:5039/ari/events?app=callcontrol&api_key=xivo:Nasheow8Eag"
        while True:
            websocket = yield from websockets.connect(asterisk)
            ari_ws = yield from websocket.recv()
            self.queue.put(ari_ws)
            yield from websocket.close()

    def run(self):
        asyncio.get_event_loop().run_until_complete(self.ws())

class Controller(object):

    def __init__(self):
        subscribeMsgQueue = Queue()
        self.callcontrol = CoreCallControl(subscribeMsgQueue)
        self.ws = CoreWs(subscribeMsgQueue)


    def run(self):
        cc_process = Process(target=self.callcontrol.run, name='callcontrol_process')
        cc_process.start()

        ws_process = Process(target=self.ws.run, name='ws_process')
        ws_process.start()


if __name__ == '__main__':
    start = Controller()
    start.run()
