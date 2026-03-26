#!/usr/bin/env node
/**
 * 诊断脚本：逐步检查 mnsv2 在各阶段是否可用
 */
var fs = require('fs');
var path = require('path');
var staticDir = path.join(__dirname);

function readJs(name) {
    return fs.readFileSync(path.join(staticDir, name), 'utf8').replace(/\u0399/g, 'I');
}

var commonSrc = readJs('xs-common-1128.js').replace(
    'delete global;',
    '/* patched */ global = globalThis;'
);

var xscSrc = readJs('xhs_xs_xsc_56.js').replace(
    'console.log(window.mnsv2(f, c, d))\n',
    '// removed top-level test call\n'
).replace(
    'var s = window.mnsv2(fullStr, c, d);',
    'var s = (window.mnsv2 || globalThis.mnsv2)(fullStr, c, d);'
);

console.log('[before common] typeof global:', typeof global);
console.log('[before common] typeof window:', typeof window);
console.log('[before common] global===globalThis:', global === globalThis);

eval(commonSrc);

console.log('[after common] typeof mnsv2:', typeof mnsv2);
console.log('[after common] typeof window:', typeof window);
console.log('[after common] typeof window.mnsv2:', typeof window.mnsv2);
console.log('[after common] typeof globalThis.mnsv2:', typeof globalThis.mnsv2);
console.log('[after common] window===globalThis:', window === globalThis);
console.log('[after common] window===global:', window === global);

eval(xscSrc);

console.log('[after xsc] typeof window:', typeof window);
console.log('[after xsc] typeof window.mnsv2:', typeof window.mnsv2);
console.log('[after xsc] typeof globalThis.mnsv2:', typeof globalThis.mnsv2);

try {
    var r = get_request_headers_params('/api/sns/web/v1/search/notes', '', 'testcookie', 'GET');
    console.log('[SUCCESS] xs:', String(r.xs).slice(0, 20));
} catch(e) {
    console.log('[FAILED] get_request_headers_params:', e.message);
}
