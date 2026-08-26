import crypto from 'node:crypto';

const BASE = 'https://app.ecourts.gov.in/services_DC_4.0/';
const CNR = process.env.ECOURTS_TEST_CNR || 'PBFZC00032292025';
const UID = `${crypto.randomUUID()}:in.gov.ecourts.eCourtsServices`;
const REQ_KEY = Buffer.from('4D6251655468576D5A7134743677397A','hex');
const RES_KEY = Buffer.from('3273357638782F413F4428472B4B6250','hex');
const PREFIXES = ['556A586E32723575','34743777217A2543','413F4428472B4B62','48404D635166546A','614E645267556B58','655368566D597133'];

function encrypt(data) {
  const index = crypto.randomInt(PREFIXES.length);
  const low = crypto.randomBytes(8).toString('hex');
  const iv = Buffer.from(PREFIXES[index] + low, 'hex');
  const cipher = crypto.createCipheriv('aes-128-cbc', REQ_KEY, iv);
  const ct = Buffer.concat([cipher.update(JSON.stringify(data),'utf8'), cipher.final()]);
  return low + String(index) + ct.toString('base64');
}
function decode(text) {
  const body = text.trim();
  try { return JSON.parse(body); } catch {}
  if (body.length > 32 && /^[0-9a-f]{32}/i.test(body)) {
    const iv = Buffer.from(body.slice(0,32),'hex');
    const ct = Buffer.from(body.slice(32),'base64');
    const d = crypto.createDecipheriv('aes-128-cbc', RES_KEY, iv);
    const plain = Buffer.concat([d.update(ct), d.final()]).toString('utf8');
    return JSON.parse(plain);
  }
  return {raw: body};
}
function tokenFrom(x) {
  if (!x || typeof x !== 'object') return null;
  if (typeof x.token === 'string' && x.token) return x.token;
  for (const v of Object.values(x)) { const t = tokenFrom(v); if (t) return t; }
  return null;
}
function unauthorized(x) {
  if (!x || typeof x !== 'object') return false;
  const c = x.status_code ?? x.statusCode ?? x.code;
  return String(c) === '401';
}
async function call(endpoint, params, token, label) {
  const url = BASE + endpoint + '?params=' + encodeURIComponent(encrypt(params));
  const headers = {'Accept':'*/*','User-Agent':'eCourtGlass-compat-test/3'};
  if (token !== undefined && token !== null) headers.Authorization = 'Bearer ' + encrypt(token);
  const r = await fetch(url, {method:'GET', headers, redirect:'manual'});
  const text = await r.text();
  console.log(`\n[${label}] HTTP ${r.status} ${r.statusText}`);
  console.log('content-type:', r.headers.get('content-type'));
  console.log('body-prefix:', JSON.stringify(text.slice(0,180)));
  let decoded;
  try { decoded = decode(text); } catch (e) { console.log('decode-error:', e.message); decoded = {raw:text}; }
  console.log('decoded:', JSON.stringify(decoded).slice(0,1200));
  return {status:r.status, decoded};
}

let boot = await call('appReleaseWebService.php', {version:'4.0', uid:UID}, null, 'bootstrap v4.0');
if (boot.status === 404) throw new Error('services_DC_4.0 bootstrap endpoint returned 404');
let token = tokenFrom(boot.decoded);
console.log('token-present:', Boolean(token));

let params = {cinum:CNR, language_flag:'english', bilingual_flag:'0'};
let result = await call('caseHistoryWebService.php', params, token, 'case history');
if (unauthorized(result.decoded)) {
  result = await call('caseHistoryWebService.php', {...params, uid:UID}, tokenFrom(result.decoded) || token, 'case history uid retry');
}
if (result.status === 404) throw new Error('caseHistoryWebService.php returned 404');
const s = JSON.stringify(result.decoded).toUpperCase();
if (!s.includes(CNR) && !s.includes('HISTORY')) {
  throw new Error('eCourts route responded but no case/history data was recognized');
}
console.log('\nLIVE_ECOURTS_4_OK');
