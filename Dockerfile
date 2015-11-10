from python:3.4

RUN pip install websockets

ADD src/ /usr/src/ws
