FROM python:3.7-slim-buster AS compile-image
LABEL maintainer="Wazo Maintainers <dev@wazo.community>"

RUN python -m venv /opt/venv
# Activate virtual env
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt /usr/src/wazo-websocketd/
WORKDIR /usr/src/wazo-websocketd
RUN pip install -r requirements.txt

COPY setup.py /usr/src/wazo-websocketd/
COPY wazo_websocketd /usr/src/wazo-websocketd/wazo_websocketd
RUN python setup.py install

FROM python:3.7-slim-buster AS build-image
COPY --from=compile-image /opt/venv /opt/venv

COPY ./etc/wazo-websocketd /etc/wazo-websocketd
RUN true \
  && adduser --quiet --system --group wazo-websocketd \
  && mkdir -p /etc/wazo-websocketd/conf.d \
  && install -o wazo-websocketd -g wazo-websocketd -d /run/wazo-websocketd \
  && install -o wazo-websocketd -g wazo-websocketd /dev/null /var/log/wazo-websocketd.log

EXPOSE 9502

# Activate virtual env
ENV PATH="/opt/venv/bin:$PATH"
CMD ["wazo-websocketd"]
