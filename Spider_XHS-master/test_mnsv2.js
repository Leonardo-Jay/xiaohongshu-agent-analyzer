// Simulate execjs wrapping
var fs = require('fs');
function readJs(f) {
    return fs.readFileSync('static/'+f, 'utf8').replace(/\u0399/g, 'I');
}

var common = readJs('xs-common-1128.js').replace(
    'delete global;',
    'delete global; if (typeof global === "undefined") { global = globalThis; }'
);
var xsc = readJs('xhs_xs_xsc_56.js').replace(
    'console.log(window.mnsv2(f, c, d))\n',
    '// removed test call\n'
);

// Simulate execjs IIFE wrapper
(function() {
    eval(common);
    console.log('[after common] mnsv2 on window:', typeof window.mnsv2);
    console.log('[after common] mnsv2 on globalThis:', typeof globalThis.mnsv2);
    eval(xsc);
    console.log('[after xsc] mnsv2 on window:', typeof window.mnsv2);
    try {
        var r = get_x_s();
        console.log('xs:', String(r.xs).slice(0,30));
    } catch(e) {
        console.log('ERROR:', e.message);
    }
})();
