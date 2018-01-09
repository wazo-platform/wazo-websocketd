FROM python:3.5.3

ADD . /usr/src/xivo-websocketd
ADD ./contribs/docker/certs /usr/share/xivo-certs
ADD etc/xivo-websocketd/config.yml /etc/xivo-websocketd/
RUN mkdir /etc/xivo-websocketd/conf.d/
RUN adduser --quiet --system --group --no-create-home --home /var/lib/xivo-websocketd xivo-websocketd
RUN install -d -o xivo-websocketd -g www-data /var/run/xivo-websocketd/

WORKDIR /usr/src/xivo-websocketd

RUN pip install -r requirements.txt
RUN python setup.py install

WORKDIR /
RUN rm -rf /usr/src/xivo-websocketd

EXPOSE 9502

CMD ["xivo-websocketd", "-fd"]
