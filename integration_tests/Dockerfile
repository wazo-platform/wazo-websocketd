FROM wazopbx/xivo-websocketd

ADD . /usr/src/xivo-websocketd
WORKDIR /usr/src/xivo-websocketd

ENV PYTHONDONTWRITEBYTECODE='true'

RUN python setup.py develop
