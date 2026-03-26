#!/usr/bin/env node
/**
 * XHS sign runner — called as a subprocess by xhs_util.py.
 * stdin: JSON {api, data, a1, method}
 * stdout: JSON {xs, xt, xs_common}
 */

var fs = require('fs');
var path = require('path');

// xhs_xs_xsc_56.js is a self-contained module that defines get_request_headers_params.
// Load it directly via require() so its internal require('crypto-js') resolves correctly.
var sign = require(path.join(__dirname, 'xhs_xs_xsc_56.js'));

var input = JSON.parse(fs.readFileSync(0, 'utf8'));  // fd 0 = stdin, works on Windows & Unix
var r = sign.get_request_headers_params(input.api, input.data || '', input.a1, input.method || 'POST');
process.stdout.write(JSON.stringify({xs: String(r.xs), xt: String(r.xt), xs_common: String(r.xs_common)}) + '\n');
