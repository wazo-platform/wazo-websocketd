from python:3.4

RUN pip install -r requirements.txt

ADD src/ /usr/src/ws

WORKDIR /usr/src/ws
