# xivo-websocketd

xivo-websocketd is a WebSocket server that delivers XiVO related events to clients.

Contrary to most other XiVO components, this is a python 3 only project. Both
the code, the unit tests and the integration tests must be run with python 3.

## Dependencies

* python >= 3.4
* see requirements.txt

## Running integration tests

You need Docker installed on your machine.

1. `cd integration_tests`
2. `pip install -r test-requirements.txt`
3. `make test-setup`
4. `make test`
