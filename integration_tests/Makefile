.PHONY: test-setup egg-info test

test-setup: egg-info
	docker pull rabbitmq
	docker pull wazopbx/wait
	docker pull wazopbx/wazo-auth-mock
	docker build --pull -t wazopbx/wazo-websocketd ..
	docker build --no-cache -t wazo-websocketd-test -f Dockerfile ..

egg-info:
	cd .. && python3 setup.py egg_info

test:
	pytest suite
