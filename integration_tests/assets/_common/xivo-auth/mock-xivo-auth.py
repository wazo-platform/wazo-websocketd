# Copyright 2016 Avencall
# SPDX-License-Identifier: GPL-3.0+

import sys

from flask import Flask, jsonify, request

app = Flask(__name__)

port = int(sys.argv[1])

context = ('/usr/local/share/ssl/auth/server.crt', '/usr/local/share/ssl/auth/server.key')

valid_tokens = {
    'valid-token': {
        'token': 'valid-token',
    }
}
unauthorized_tokens = [
    'unauthorized-token',
]
dynamic_tokens = {
}


@app.route("/0.1/token/<token_id>", methods=['HEAD'])
def token_head(token_id):
    if token_id in valid_tokens or token_id in dynamic_tokens:
        return '', 204
    elif token_id in unauthorized_tokens:
        return '', 403
    return '', 404


@app.route("/0.1/token/<token_id>", methods=['GET'])
def token_get(token_id):
    token = None
    if token_id in valid_tokens:
        token = valid_tokens[token_id]
    elif token_id in dynamic_tokens:
        token = dynamic_tokens[token_id]
    elif token_id in unauthorized_tokens:
        return '', 403
    else:
        return '', 404

    return jsonify({'data': token})


@app.route("/_control/token/<token_id>", methods=['PUT'])
def set_token(token_id):
    token = request.get_json(force=True)
    if token is None:
        return 'get_json() returned None\n', 400
    token['token'] = token_id
    dynamic_tokens[token_id] = token
    return '', 204


@app.route("/_control/token/<token_id>", methods=['DELETE'])
def remove_token(token_id):
    try:
        del dynamic_tokens[token_id]
    except KeyError:
        return '', 404
    else:
        return '', 204


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=port, ssl_context=context, debug=True)
