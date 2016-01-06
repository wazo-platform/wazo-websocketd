from python:3.4

ADD . /usr/src/ws
WORKDIR /usr/src/ws
RUN pip install -r requirements.txt
WORKDIR /usr/src/
RUN rm -rf /usr/src/ws

ADD contribs/docker/certs/* /usr/share/xivo-certs/
