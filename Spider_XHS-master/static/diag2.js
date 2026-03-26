#!/usr/bin/env node
/**
 * 诊断2：在 Spider_XHS-master 目录下运行，确保 node_modules 可以被找到
 */
process.chdir(__dirname + '/..');
console.log('cwd:', process.cwd());

var fs = require('fs');
var path = require('path');
var staticDir = __dirname;

function readJs(name) {
    return fs.readFileSync(path.join(staticDir, name), 'utf8').replace(/\u0399/g, 'I');
}

// 先测试 crypto-js require
try {
    var CJ = require('crypto-js');
    console.log('[OK] crypto-js required in top scope');
} catch(e) {
    console.log('[FAIL] crypto-js:', e.message);
}

var commonSrc = readJs('xs-common-1128.js').replace(
    'delete global;',
    '/* patched */ global = globalThis;'
);

var xscSrc = readJs('xhs_xs_xsc_56.js').replace(
    'console.log(window.mnsv2(f, c, d))\n',
    'console.log("[diag] mnsv2 at test point:", typeof window.mnsv2);\n'
).replace(
    'var s = window.mnsv2(fullStr, c, d);',
    'var s = (window.mnsv2 || globalThis.mnsv2)(fullStr, c, d);'
);

eval(commonSrc);
console.log('[after common] window.mnsv2:', typeof window.mnsv2);

eval(xscSrc);
console.log('[after xsc] window.mnsv2:', typeof window.mnsv2);
