FROM wazoplatform/wazo-websocketd

ADD . /usr/src/wazo-websocketd
WORKDIR /usr/src/wazo-websocketd

ENV PYTHONDONTWRITEBYTECODE='true'

RUN python3 -m pip install -e .
