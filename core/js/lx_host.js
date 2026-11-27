'use strict';
/**
 * LX Music custom-source host.
 *
 * Runs one LX source script inside a `vm` context and implements the `lx`
 * global the script expects:
 *
 *   lx.EVENT_NAMES  { request, inited, updateAlert }
 *   lx.on(name, fn) / lx.send(name, data)
 *   lx.request(url, options, callback)
 *   lx.utils.buffer.{from,bufToString,bufConcat}
 *   lx.utils.crypto.{md5,randomBytes,aesEncrypt,aesDecrypt,rsaEncrypt}
 *   lx.env / lx.version / lx.currentScriptInfo
 *
 * The parent process (Python) drives it over stdin/stdout with one JSON
 * object per line, so no third-party JS dependency is required.
 */

const vm = require('vm');
const fs = require('fs');
const http = require('http');
const https = require('https');
const crypto = require('crypto');
const zlib = require('zlib');
const { URL, URLSearchParams } = require('url');

const DEFAULT_TIMEOUT = 15000;
const MAX_REDIRECTS = 5;

let handlers = new Map();
let scriptInfo = {};
let initedSent = false;

function emit(payload) {
  try {
    process.stdout.write(JSON.stringify(payload) + '\n');
  } catch (err) {
    /* stdout gone: parent exited */
  }
}

function log(...args) {
  const text = args
    .map((value) => {
      if (typeof value === 'string') return value;
      try {
        return JSON.stringify(value);
      } catch (err) {
        return String(value);
      }
    })
    .join(' ');
  emit({ t: 'log', msg: text });
}

// ---------------------------------------------------------------- http

function decodeBody(buffer, headers) {
  const encoding = String(headers['content-encoding'] || '').toLowerCase();
  try {
    if (encoding.includes('gzip')) return zlib.gunzipSync(buffer);
    if (encoding.includes('deflate')) return zlib.inflateSync(buffer);
    if (encoding.includes('br')) return zlib.brotliDecompressSync(buffer);
  } catch (err) {
    return buffer;
  }
  return buffer;
}

function normalizeOptions(options) {
  const opts = Object.assign({}, options || {});
  if (!opts.method) opts.method = opts.body ? 'POST' : 'GET';
  opts.method = String(opts.method).toUpperCase();
  opts.headers = Object.assign({}, opts.headers || {});

  if (opts.form && typeof opts.form === 'object') {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(opts.form)) {
      if (value !== undefined && value !== null) params.append(key, String(value));
    }
    opts.body = params.toString();
    if (!hasHeader(opts.headers, 'content-type')) {
      opts.headers['Content-Type'] = 'application/x-www-form-urlencoded';
    }
  }

  if (opts.json && typeof opts.json === 'object') {
    opts.body = JSON.stringify(opts.json);
    if (!hasHeader(opts.headers, 'content-type')) {
      opts.headers['Content-Type'] = 'application/json';
    }
  }
  return opts;
}

function hasHeader(headers, name) {
  return Object.keys(headers).some((key) => key.toLowerCase() === name);
}

function performRequest(url, options, callback, redirectsLeft) {
  let target;
  try {
    target = new URL(url);
  } catch (err) {
    return callback(new Error('无效的 URL: ' + url));
  }

  const opts = normalizeOptions(options);
  const transport = target.protocol === 'http:' ? http : https;
  const timeout = Number(opts.timeout) > 0 ? Number(opts.timeout) : DEFAULT_TIMEOUT;

  let bodyBuffer = null;
  if (opts.body !== undefined && opts.body !== null && opts.method !== 'GET') {
    bodyBuffer = Buffer.isBuffer(opts.body) ? opts.body : Buffer.from(String(opts.body));
    if (!hasHeader(opts.headers, 'content-length')) {
      opts.headers['Content-Length'] = bodyBuffer.length;
    }
  }

  const requestOptions = {
    method: opts.method,
    headers: opts.headers,
    timeout: timeout,
    // Music APIs frequently serve broken certificate chains.
    rejectUnauthorized: opts.rejectUnauthorized === true,
  };

  let settled = false;
  const finish = (err, response) => {
    if (settled) return;
    settled = true;
    callback(err, response);
  };

  const req = transport.request(target, requestOptions, (res) => {
    const status = res.statusCode || 0;
    const location = res.headers.location;

    if (location && status >= 300 && status < 400 && (redirectsLeft || 0) > 0) {
      res.resume();
      const next = new URL(location, target).toString();
      const nextMethod = status === 303 ? 'GET' : opts.method;
      const nextOptions = Object.assign({}, opts, { method: nextMethod });
      if (nextMethod === 'GET') nextOptions.body = undefined;
      return performRequest(next, nextOptions, callback, redirectsLeft - 1);
    }

    const chunks = [];
    res.on('data', (chunk) => chunks.push(chunk));
    res.on('end', () => {
      const raw = decodeBody(Buffer.concat(chunks), res.headers);
      const binary = opts.binary === true;
      finish(null, {
        statusCode: status,
        statusMessage: res.statusMessage || '',
        headers: res.headers,
        bytes: raw.length,
        body: binary ? raw : raw.toString(opts.charset || 'utf8'),
      });
    });
    res.on('error', (err) => finish(err));
  });

  req.on('timeout', () => {
    req.destroy(new Error('请求超时'));
  });
  req.on('error', (err) => finish(err));

  if (bodyBuffer) req.write(bodyBuffer);
  req.end();
}

function lxRequest(url, options, callback) {
  if (typeof options === 'function') {
    callback = options;
    options = {};
  }
  performRequest(url, options || {}, (err, response) => {
    if (typeof callback !== 'function') return;
    setImmediate(() => callback(err, response));
  }, MAX_REDIRECTS);
}

// ---------------------------------------------------------------- utils

function bufToString(buffer, format) {
  if (buffer === undefined || buffer === null) return '';
  const buf = Buffer.isBuffer(buffer) ? buffer : Buffer.from(String(buffer));
  const encoding = format === 'base64' ? 'base64' : format === 'hex' ? 'hex' : 'utf8';
  if (encoding === 'base64') return buf.toString('base64');
  if (encoding === 'hex') return buf.toString('hex');
  // LX defaults to utf8 and drops a trailing NUL that some APIs append.
  return buf.toString('utf8').replace(/\u0000+$/, '');
}

const lxBuffer = {
  from(data, format) {
    if (Buffer.isBuffer(data)) return data;
    if (data instanceof ArrayBuffer) return Buffer.from(data);
    if (ArrayBuffer.isView(data)) return Buffer.from(data.buffer, data.byteOffset, data.byteLength);
    const encoding = format === 'base64' ? 'base64' : format === 'hex' ? 'hex' : 'utf8';
    return Buffer.from(String(data), encoding);
  },
  bufToString,
  bufConcat(buffers) {
    return Buffer.concat(Array.isArray(buffers) ? buffers.filter(Boolean) : []);
  },
};

const lxCrypto = {
  md5(value) {
    return crypto.createHash('md5').update(Buffer.isBuffer(value) ? value : String(value)).digest('hex');
  },
  sha1(value) {
    return crypto.createHash('sha1').update(Buffer.isBuffer(value) ? value : String(value)).digest('hex');
  },
  randomBytes(size) {
    return crypto.randomBytes(Number(size) || 0);
  },
  aesEncrypt(buffer, mode, key, iv) {
    const cipher = crypto.createCipheriv(mode, toKey(key, mode), iv ? toKey(iv) : null);
    return Buffer.concat([cipher.update(toBuf(buffer)), cipher.final()]);
  },
  aesDecrypt(buffer, mode, key, iv) {
    const decipher = crypto.createDecipheriv(mode, toKey(key, mode), iv ? toKey(iv) : null);
    return Buffer.concat([decipher.update(toBuf(buffer)), decipher.final()]);
  },
  rsaEncrypt(buffer, key) {
    // LX accepts a PEM string or an already-parsed KeyObject.
    const publicKey = typeof key === 'string' ? key : key;
    return crypto.publicEncrypt(
      { key: publicKey, padding: crypto.constants.RSA_PKCS1_PADDING },
      toBuf(buffer),
    );
  },
};

function toBuf(value) {
  return Buffer.isBuffer(value) ? value : Buffer.from(String(value));
}

function toKey(value, mode) {
  if (Buffer.isBuffer(value)) return value;
  const text = String(value);
  // aes-128-* expects 16 bytes, aes-192-* 24, aes-256-* 32.
  const sizes = { 'aes-128': 16, 'aes-192': 24, 'aes-256': 32 };
  const prefix = String(mode || '').slice(0, 7);
  const size = sizes[prefix];
  if (size && text.length !== size) {
    return Buffer.from(text.padEnd(size, '\0').slice(0, size), 'utf8');
  }
  return Buffer.from(text, 'utf8');
}

// ---------------------------------------------------------------- script

function buildSandbox() {
  const lx = {
    EVENT_NAMES: {
      request: 'request',
      inited: 'inited',
      updateAlert: 'updateAlert',
    },
    version: 2,
    env: 'desktop',
    currentScriptInfo: scriptInfo,
    on(name, handler) {
      if (typeof handler !== 'function') return;
      handlers.set(String(name), handler);
    },
    send(name, data) {
      const event = String(name);
      if (event === 'inited') {
        initedSent = true;
        emit({ t: 'inited', data: sanitize(data) });
        return;
      }
      emit({ t: 'event', name: event, data: sanitize(data) });
    },
    request: lxRequest,
    utils: { buffer: lxBuffer, crypto: lxCrypto, zlib },
  };

  const sandbox = {
    lx,
    console: {
      log: (...a) => log(...a),
      info: (...a) => log(...a),
      warn: (...a) => log(...a),
      error: (...a) => log(...a),
      debug: (...a) => log(...a),
    },
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    setImmediate,
    queueMicrotask,
    Buffer,
    URL,
    URLSearchParams,
    TextDecoder,
    TextEncoder,
    atob,
    btoa,
    AbortController,
    fetch,
    zlib,
    process: { env: {}, platform: process.platform, version: process.version, arch: process.arch },
    navigator: { userAgent: 'lx-music/desktop', platform: process.platform, language: 'zh-CN' },
  };
  return vm.createContext(sandbox, { name: 'lx-source' });
}

/** Strip non-serialisable values so JSON.stringify cannot throw. */
function sanitize(value, depth) {
  if (depth === undefined) depth = 0;
  if (depth > 6) return null;
  if (value === null || value === undefined) return null;
  const type = typeof value;
  if (type === 'string' || type === 'number' || type === 'boolean') return value;
  if (type === 'function') return undefined;
  if (Buffer.isBuffer(value)) return value.toString('base64');
  if (Array.isArray(value)) return value.map((item) => sanitize(item, depth + 1));
  if (type === 'object') {
    const out = {};
    for (const [key, item] of Object.entries(value)) {
      const clean = sanitize(item, depth + 1);
      if (clean !== undefined) out[key] = clean;
    }
    return out;
  }
  return null;
}

function readMeta(code) {
  // Scripts declare themselves in a leading /*! ... */ banner.
  const banner = code.slice(0, 4000).match(/\/\*[\s\S]{0,3000}?\*\//);
  const source = banner ? banner[0] : code.slice(0, 2000);
  const field = (name) => {
    const hit = source.match(new RegExp('@' + name + '\\s+([^\\n\\r*]+)'));
    return hit ? hit[1].trim() : '';
  };
  return {
    name: field('name'),
    description: field('description'),
    version: field('version'),
    author: field('author'),
    homepage: field('homepage'),
  };
}

function loadCode(code, info) {
  handlers = new Map();
  initedSent = false;
  scriptInfo = Object.assign({}, info || {}, readMeta(code));

  const context = buildSandbox();
  emit({ t: 'meta', data: scriptInfo });

  try {
    vm.runInContext(code, context, { filename: 'lx-source.js', timeout: 60000 });
  } catch (err) {
    emit({ t: 'fatal', message: '脚本执行失败：' + (err && err.message ? err.message : String(err)) });
    return;
  }
  emit({ t: 'loaded' });
}

async function callHandler(message) {
  const handler = handlers.get('request');
  if (!handler) {
    emit({ t: 'error', id: message.id, message: '脚本未监听 request 事件' });
    return;
  }
  const payload = {
    source: message.source,
    action: message.action,
    info: message.info || {},
  };
  try {
    const result = await handler(payload);
    emit({ t: 'result', id: message.id, data: sanitize(result) });
  } catch (err) {
    const message_ = err && err.message ? err.message : String(err);
    emit({ t: 'error', id: message.id, message: message_ });
  }
}

// ---------------------------------------------------------------- stdin

let pending = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  pending += chunk;
  let index = pending.indexOf('\n');
  while (index >= 0) {
    const line = pending.slice(0, index).trim();
    pending = pending.slice(index + 1);
    index = pending.indexOf('\n');
    if (!line) continue;
    let message;
    try {
      message = JSON.parse(line);
    } catch (err) {
      emit({ t: 'fatal', message: '父进程指令解析失败' });
      continue;
    }
    if (message.cmd === 'load') {
      loadCode(message.code, message.info);
    } else if (message.cmd === 'call') {
      callHandler(message);
    } else if (message.cmd === 'ping') {
      emit({ t: 'pong' });
    }
  }
});

process.stdin.on('end', () => process.exit(0));

process.on('unhandledRejection', (reason) => {
  emit({ t: 'log', msg: 'unhandledRejection: ' + (reason && reason.message ? reason.message : String(reason)) });
});
process.on('uncaughtException', (err) => {
  emit({ t: 'fatal', message: '未捕获错误：' + (err && err.message ? err.message : String(err)) });
});
