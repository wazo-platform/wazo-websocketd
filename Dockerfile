FROM python:3.7-buster

ADD . /usr/src/wazo-websocketd
ADD etc/wazo-websocketd/config.yml /etc/wazo-websocketd/
RUN mkdir /etc/wazo-websocketd/conf.d/
RUN adduser --quiet --system --group --no-create-home --home /var/lib/wazo-websocketd wazo-websocketd

WORKDIR /usr/src/wazo-websocketd

RUN pip install -r requirements.txt
RUN python setup.py install

WORKDIR /
RUN rm -rf /usr/src/wazo-websocketd

EXPOSE 9502

CMD ["wazo-websocketd", "-d"]
