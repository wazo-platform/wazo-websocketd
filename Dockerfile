FROM python:3.4.2

ADD . /usr/src/xivo-websocketd
ADD ./contribs/docker/certs /usr/share/xivo-certs
WORKDIR /usr/src/xivo-websocketd

RUN pip install -r requirements.txt
RUN python setup.py install

WORKDIR /
RUN rm -rf /usr/src/xivo-websocketd

EXPOSE 9502

CMD xivo-websocketd
