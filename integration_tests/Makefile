.PHONY: test-setup egg-info test

test-setup: egg-info
	docker pull python:3.4.2
	docker pull p0bailey/docker-flask
	docker pull n3llyb0y/wait
	docker pull rabbitmq
	docker build -t xivo/xivo-websocketd ..
	docker build --no-cache -t xivo-websocketd-test -f docker/Dockerfile-websocketd-test ..

egg-info:
	cd .. && python3 setup.py egg_info

test:
	nosetests suite
