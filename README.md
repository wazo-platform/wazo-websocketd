# wazo-websocketd

wazo-websocketd is a WebSocket server that delivers Wazo related events to clients.

## Dependencies

* python >= 3.4
* see requirements.txt

## Running integration tests

You need Docker installed on your machine.

1. `cd integration_tests`
2. `pip install -r test-requirements.txt`
3. `make test-setup`
4. `make test`

## Benchmark

Using [artillery](https://artillery.io/docs/getting-started/)

  * `npm install -g artillery`
  * Create scenarios in `benchmark.yml`:
```
config:
  target: "wss://<host>:9502?token=<token_id>"
  phases:
    - duration: 120
      arrivalRate: 8
    - pause: 120
  ws:
    # Ignore SSL certificate errors
    # - useful in *development* with self-signed certs
    rejectUnauthorized: false
scenarios:
  - engine: "ws"
    flow:
      - think: 5
      - send: '{"op": "start"}'
      - think: 240
```
  * `artillery run benchmark.yml`

Note: You may need to increase file descriptors of `root`: `/etc/security/limits.d/asterisk.conf`
