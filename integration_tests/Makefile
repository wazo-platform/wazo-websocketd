.PHONY: test-setup egg-info test

test-setup: egg-info
	docker pull python:3.5.3
	docker pull mongooseim/mongooseim:2.1.0
	docker pull rabbitmq:3.6.6
	docker pull waisbrot/wait
	docker pull wazopbx/xivo-auth-mock
	docker build -t wazopbx/xivo-websocketd ..
	docker build --no-cache -t xivo-websocketd-test -f Dockerfile ..

egg-info:
	cd .. && python3 setup.py egg_info

test:
	nosetests suite
