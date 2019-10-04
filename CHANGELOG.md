# Changelog

## 19.14

* V2 protocol has been added:

  * The new protocol allow to pass commands after "start" have been called.
  * With the v2 protocol events are now surrounded by:

  ```
  {"op": "event", "code": "OK", "msg": <event>}
  ```

  * When command are send, websocketd will always return a command result.
  * To start a v2 sessions:

  ```
  {"op": "start", "data": {"version": 2}}
  ```

* V1 protocol does no longer provide the `msg` attribute that was always empty
