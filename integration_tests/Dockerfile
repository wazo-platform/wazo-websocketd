FROM wazopbx/xivo-websocketd

ADD . /usr/src/xivo-websocketd
WORKDIR /usr/src/xivo-websocketd

ENV PYTHONDONTWRITEBYTECODE='disabled'

RUN python setup.py develop
