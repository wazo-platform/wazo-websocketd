.PHONY: test-setup egg-info test

test-setup: egg-info
	docker build --pull -t wazoplatform/wazo-websocketd ..
	docker build --no-cache -t wazo-websocketd-test -f Dockerfile ..

egg-info:
	cd .. && python3 setup.py egg_info

test:
	pytest suite
