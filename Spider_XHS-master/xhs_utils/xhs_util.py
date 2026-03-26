import json
import math
import os
import random
import subprocess
import sys
import execjs
from xhs_utils.cookie_util import trans_cookies

_STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static')
_RUNNER = os.path.join(_STATIC_DIR, 'xhs_sign_runner.js')

def _read_js(filename):
    """Read JS file and replace non-ASCII lookalike chars that break execjs on Windows."""
    content = open(os.path.join(_STATIC_DIR, filename), 'r', encoding='utf-8').read()
    content = content.replace('\u0399', 'I')
    return content

def _load_js(filename):
    content = _read_js(filename)
    # 把 xhs_xray.js 里的相对路径 require 替换为绝对路径，避免 execjs cwd 不是 static 目录时找不到文件
    content = content.replace("require('./xhs_xray_pack1.js')", f"require({repr(_STATIC_DIR + '/xhs_xray_pack1.js')})")
    content = content.replace("require('./xhs_xray_pack2.js')", f"require({repr(_STATIC_DIR + '/xhs_xray_pack2.js')})")
    return execjs.compile(content)

xray_js = _load_js('xhs_xray.js')

def generate_x_b3_traceid(len=16):
    x_b3_traceid = ""
    for t in range(len):
        x_b3_traceid += "abcdef0123456789"[math.floor(16 * random.random())]
    return x_b3_traceid

def generate_xs_xs_common(a1, api, data='', method='POST'):
    payload = json.dumps({'api': api, 'data': data or '', 'a1': a1, 'method': method or 'POST'})
    result = subprocess.run(
        ['node', _RUNNER],
        input=payload.encode('utf-8'),
        capture_output=True,
        timeout=30,
        cwd=os.path.dirname(_RUNNER),  # 确保 require('crypto-js') 能找到 node_modules
    )
    # stdout 可能含有 [Error] 等非 JSON 行，找第一个以 { 开头的行
    stdout_text = result.stdout.decode('utf-8', errors='replace')
    json_line = next((l for l in stdout_text.splitlines() if l.strip().startswith('{')), None)
    if result.returncode != 0 or not json_line:
        stderr = result.stderr.decode('utf-8', errors='replace').strip() or stdout_text.strip() or '(无输出)'
        raise RuntimeError(f'xhs_sign_runner.js 失败 (exit={result.returncode}): {stderr}')
    ret = json.loads(json_line)
    return ret['xs'], ret['xt'], ret['xs_common']

def generate_xs(a1, api, data=''):
    ret = js.call('get_xs', api, data, a1)
    xs, xt = ret['X-s'], ret['X-t']
    return xs, xt

def generate_xray_traceid():
    return xray_js.call('traceId')
def get_common_headers():
    return {
        "authority": "www.xiaohongshu.com",
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "accept-language": "zh-CN,zh;q=0.9",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "referer": "https://www.xiaohongshu.com/",
        "sec-ch-ua": "\"Chromium\";v=\"122\", \"Not(A:Brand\";v=\"24\", \"Google Chrome\";v=\"122\"",
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": "\"Windows\"",
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }
def get_request_headers_template():
    return {
        "authority": "edith.xiaohongshu.com",
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "cache-control": "no-cache",
        "content-type": "application/json;charset=UTF-8",
        "origin": "https://www.xiaohongshu.com",
        "pragma": "no-cache",
        "referer": "https://www.xiaohongshu.com/",
        "sec-ch-ua": "\"Not A(Brand\";v=\"99\", \"Microsoft Edge\";v=\"121\", \"Chromium\";v=\"121\"",
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": "\"Windows\"",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
        "x-b3-traceid": "",
        "x-mns": "unload",
        "x-s": "",
        "x-s-common": "",
        "x-t": "",
        "x-xray-traceid": generate_xray_traceid()
    }

def generate_headers(a1, api, data='', method='POST'):
    xs, xt, xs_common = generate_xs_xs_common(a1, api, data, method)
    x_b3_traceid = generate_x_b3_traceid()
    headers = get_request_headers_template()
    headers['x-s'] = xs
    headers['x-t'] = str(xt)
    headers['x-s-common'] = xs_common
    headers['x-b3-traceid'] = x_b3_traceid
    if data:
        data = json.dumps(data, separators=(',', ':'), ensure_ascii=False)
    return headers, data

def generate_request_params(cookies_str, api, data='', method='POST'):
    cookies = trans_cookies(cookies_str)
    a1 = cookies['a1']
    headers, data = generate_headers(a1, api, data, method)
    return headers, cookies, data

def splice_str(api, params):
    url = api + '?'
    for key, value in params.items():
        if value is None:
            value = ''
        url += key + '=' + value + '&'
    return url[:-1]

