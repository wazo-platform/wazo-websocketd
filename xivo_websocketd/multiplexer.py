# Copyright 2016 Avencall
# SPDX-License-Identifier: GPL-3.0+

import asyncio
import logging

logger = logging.getLogger(__name__)


class Multiplexer(object):

    def __init__(self, loop):
        self._loop = loop
        self._stopped = False
        self._handles = []
        self._futures = []
        self._queue = asyncio.Queue(loop=self._loop)

    def call_soon(self, function, *args):
        t = _CallSoonTask(function, args)
        self._queue.put_nowait(t)

    def call_later(self, delay, function, *args):
        t = _CallLaterTask(self, function, args)
        handle = self._loop.call_later(delay, self._queue.put_nowait, t)
        t._handle = handle
        self._handles.append(handle)

    def call_when_done(self, future_or_coro, function, *args):
        if asyncio.iscoroutine(future_or_coro):
            future = self._loop.create_task(future_or_coro)
        else:
            future = future_or_coro
        t = _FutureTask(self, future, function, args)
        future.add_done_callback(t.future_done)
        self._futures.append(future)

    def raise_exception(self, exception):
        t = _RaiseTask(exception)
        self._queue.put_nowait(t)

    @asyncio.coroutine
    def close(self):
        for handle in self._handles:
            handle.cancel()
        for future in self._futures:
            future.cancel()
        for future in self._futures:
            try:
                yield from future
                # if the task was already completed, we could execute something here ?
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception('unexpected error while waiting for task in multiplexer')

    def stop(self):
        self._queue.put_nowait(None)

    @asyncio.coroutine
    def run(self):
        while True:
            # XXX maybe we would be better with only an Event and a normal list
            item = yield from self._queue.get()
            if item is None:
                break
            result = item.run()
            if asyncio.iscoroutine(result):
                yield from result


class _CallSoonTask(object):

    def __init__(self, function, args):
        self._function = function
        self._args = args

    def run(self):
        return self._function(*self._args)


class _CallLaterTask(object):

    def __init__(self, multiplexer, function, args):
        self._multiplexer = multiplexer
        self._function = function
        self._args = args
        self._handle = None

    def run(self):
        self._multiplexer._handles.remove(self._handle)
        return self._function(*self._args)


class _FutureTask(object):

    def __init__(self, multiplexer, future, function, args):
        self._multiplexer = multiplexer
        self._function = function
        self._args = args
        self._future = future

    def future_done(self, future):
        self._multiplexer._queue.put_nowait(self)

    def run(self):
        self._multiplexer._futures.remove(self._future)
        return self._function(self._future, *self._args)


class _RaiseTask(object):

    def __init__(self, exception):
        self._exception = exception

    def run(self):
        raise self._exception
