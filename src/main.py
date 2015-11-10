#!/usr/bin/env python

import asyncio
import datetime
import websockets
import logging
import json
import time

from multiprocessing import Process
from multiprocessing import Queue

logger = logging.getLogger(__name__)


class CoreWs(object):

    def __init__(self, queue):
        self.queue = queue
        self.start_server = websockets.serve(self.time, '0.0.0.0', 8000)

    def run(self):
        asyncio.get_event_loop().run_until_complete(self.start_server)
        asyncio.get_event_loop().run_forever()

    @asyncio.coroutine
    def time(self, websocket, path):
        while True:
            if not websocket.open:
                return
            yield from websocket.send(self.queue.get())
            yield from asyncio.sleep(1)



class CoreCallControl(object):

    def __init__(self, queue):
        self.queue = queue

    def run(self):
        while True:
            message = json.dumps({'message': 'hello world'})
            print("callcontrol:", message)
            self.queue.put(message)
            time.sleep(5)



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
