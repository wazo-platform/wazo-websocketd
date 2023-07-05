# Changelog

## 23.10

* Default configuration for `auth_check_strategy` has been changed from `static`
  to `dynamic`

## 20.09

* Deprecate SSL configuration

## 20.08

* Add a ping operation to allow client-side ping.

## 19.14

* V2 protocol has been added:

  * The new protocol allow to pass commands after "start" have been called.
  * With the v2 protocol events are now surrounded by:

  ```
  {"op": "event", "code": 0, "msg": <event>}
  ```

  * When command are send, websocketd will always return a command result.
  * To start a v2 sessions `&version=2` must be added to the websocket url.

* V1 protocol does no longer provide the `msg` attribute that was always empty
