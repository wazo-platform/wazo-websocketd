FROM wazopbx/wazo-websocketd

ADD . /usr/src/wazo-websocketd
WORKDIR /usr/src/wazo-websocketd

ENV PYTHONDONTWRITEBYTECODE='true'

RUN python setup.py develop
