// Zero-dependency static file server for the local job-search dashboard.
// Serves this folder on http://localhost:8420 so dashboard.html can fetch()
// the CSV files with fresh data on every reload. Also exposes write
// endpoints so the dashboard can:
//   - mark a job as Offer/Rejected (POST /api/update-status)
//   - add/edit/delete an upcoming calendar event, which also stamps the
//     job's current_stage in job_pool.csv (POST /api/calendar/*)
const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const PORT = Number(process.env.JOBHUNT_DASHBOARD_PORT || 8420);
const ROOT = __dirname;
const JOB_POOL_PATH = path.join(ROOT, 'job_pool.csv');
const TODOS_PATH = path.join(ROOT, 'todos.json');
const FOLLOW_UP_PATH = path.join(ROOT, 'follow_up.csv');
const DSH_HANDOFF_PATH = path.join(ROOT, 'dsh_handoff.json');

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.csv': 'text/csv; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
};

// Same quoted-field CSV dialect job_pool.csv already uses (every field
// quoted, "" for an embedded quote, CRLF line endings).
function parseCSV(text) {
  const rows = [];
  let row = [], field = '', inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i], next = text[i + 1];
    if (inQuotes) {
      if (c === '"' && next === '"') { field += '"'; i++; }
      else if (c === '"') { inQuotes = false; }
      else { field += c; }
    } else {
      if (c === '"') { inQuotes = true; }
      else if (c === ',') { row.push(field); field = ''; }
      else if (c === '\r') { /* skip */ }
      else if (c === '\n') { row.push(field); rows.push(row); row = []; field = ''; }
      else { field += c; }
    }
  }
  if (field.length || row.length) { row.push(field); rows.push(row); }
  while (rows.length && rows[rows.length - 1].length === 1 && rows[rows.length - 1][0] === '') rows.pop();
  return rows;
}

function stringifyField(f) {
  return '"' + String(f == null ? '' : f).replace(/"/g, '""') + '"';
}

function stringifyCSV(rows) {
  return rows.map(r => r.map(stringifyField).join(',')).join('\r\n') + '\r\n';
}

function readCSVRows(filePath) {
  const text = fs.readFileSync(filePath, 'utf8');
  const rows = parseCSV(text);
  return { header: rows[0], dataRows: rows.slice(1) };
}

function writeCSVRows(filePath, header, dataRows) {
  fs.writeFileSync(filePath, stringifyCSV([header, ...dataRows]));
}

function readJSONBody(req) {
  return new Promise((resolve, reject) => {
    let body = '';
    req.on('data', chunk => {
      body += chunk;
      if (body.length > 1e6) req.destroy(); // guard against runaway payloads
    });
    req.on('end', () => {
      try { resolve(JSON.parse(body)); }
      catch (e) { reject(new Error('Invalid JSON body')); }
    });
    req.on('error', reject);
  });
}

function sendJSON(res, statusCode, obj) {
  res.writeHead(statusCode, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(obj));
}

function readHandoffs() {
  if (!fs.existsSync(DSH_HANDOFF_PATH)) return [];
  const parsed = JSON.parse(fs.readFileSync(DSH_HANDOFF_PATH, 'utf8'));
  if (!Array.isArray(parsed)) throw new Error('dsh_handoff.json must contain a JSON array');
  return parsed;
}

function writeHandoffs(items) {
  const tempPath = DSH_HANDOFF_PATH + '.tmp';
  fs.writeFileSync(tempPath, JSON.stringify(items, null, 2) + '\n', 'utf8');
  fs.renameSync(tempPath, DSH_HANDOFF_PATH);
}

function cleanText(value, maxLength = 5000) {
  return typeof value === 'string' ? value.trim().slice(0, maxLength) : '';
}

async function handleDshHandoffRequest(req, res) {
  let payload;
  try { payload = await readJSONBody(req); }
  catch (e) { return sendJSON(res, 400, { ok: false, error: e.message }); }

  const company = cleanText(payload?.company, 200);
  const jobTitle = cleanText(payload?.job_title, 300);
  const jobUrl = cleanText(payload?.job_url, 2000);
  if (!company || !jobTitle || !/^https?:\/\//i.test(jobUrl)) {
    return sendJSON(res, 400, { ok: false, error: 'company, job_title and an http(s) job_url are required' });
  }

  let items;
  try { items = readHandoffs(); }
  catch (e) { return sendJSON(res, 500, { ok: false, error: e.message }); }

  const existing = items.find(item =>
    item.company === company && item.job_title === jobTitle && item.job_url === jobUrl &&
    ['pending', 'answered'].includes(item.status)
  );
  if (existing) return sendJSON(res, 200, { ok: true, deduplicated: true, handoff: existing });

  const now = new Date().toISOString();
  const handoff = {
    id: crypto.randomUUID(),
    status: 'pending',
    created_at: now,
    updated_at: now,
    company,
    job_title: jobTitle,
    job_url: jobUrl,
    source_platform: cleanText(payload?.source_platform, 100),
    requested_checks: Array.isArray(payload?.requested_checks)
      ? payload.requested_checks.map(v => cleanText(v, 300)).filter(Boolean).slice(0, 20)
      : [],
    context: cleanText(payload?.context, 10000),
    response: null
  };
  items.push(handoff);
  try { writeHandoffs(items); }
  catch (e) { return sendJSON(res, 500, { ok: false, error: e.message }); }
  sendJSON(res, 201, { ok: true, handoff });
}

function handleDshHandoffList(req, res) {
  let items;
  try { items = readHandoffs(); }
  catch (e) { return sendJSON(res, 500, { ok: false, error: e.message }); }
  const requestUrl = new URL(req.url, 'http://127.0.0.1');
  const status = cleanText(requestUrl.searchParams.get('status'), 30);
  if (status) items = items.filter(item => item.status === status);
  sendJSON(res, 200, { ok: true, handoffs: items });
}

function handleDshHandoffStatus(req, res) {
  const requestUrl = new URL(req.url, 'http://127.0.0.1');
  const id = cleanText(requestUrl.searchParams.get('id'), 100);
  let items;
  try { items = readHandoffs(); }
  catch (e) { return sendJSON(res, 500, { ok: false, error: e.message }); }
  const handoff = items.find(item => item.id === id);
  if (!handoff) return sendJSON(res, 404, { ok: false, error: 'handoff not found' });
  sendJSON(res, 200, { ok: true, handoff });
}

async function handleDshHandoffRespond(req, res) {
  let payload;
  try { payload = await readJSONBody(req); }
  catch (e) { return sendJSON(res, 400, { ok: false, error: e.message }); }
  const id = cleanText(payload?.id, 100);
  const decision = cleanText(payload?.decision, 30);
  if (!id || !['proceed', 'skip', 'needs_user'].includes(decision)) {
    return sendJSON(res, 400, { ok: false, error: 'id and decision (proceed|skip|needs_user) are required' });
  }
  let items;
  try { items = readHandoffs(); }
  catch (e) { return sendJSON(res, 500, { ok: false, error: e.message }); }
  const handoff = items.find(item => item.id === id);
  if (!handoff) return sendJSON(res, 404, { ok: false, error: 'handoff not found' });
  handoff.status = 'answered';
  handoff.updated_at = new Date().toISOString();
  handoff.response = {
    decision,
    responder: cleanText(payload?.responder, 100) || 'DeepSeek Harness',
    official_entry_verified: payload?.official_entry_verified === true,
    cohort_verified: payload?.cohort_verified === true,
    duplicate_found: payload?.duplicate_found === true,
    application_limit: cleanText(payload?.application_limit, 500),
    resume_variant: cleanText(payload?.resume_variant, 500),
    notes: cleanText(payload?.notes, 10000)
  };
  try { writeHandoffs(items); }
  catch (e) { return sendJSON(res, 500, { ok: false, error: e.message }); }
  sendJSON(res, 200, { ok: true, handoff });
}

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const TIME_RE = /^\d{2}:\d{2}$/;

async function handleUpdateStatus(req, res) {
  let payload;
  try {
    payload = await readJSONBody(req);
  } catch (e) {
    return sendJSON(res, 400, { ok: false, error: e.message });
  }

  const { rowIndex, company, job_title, status } = payload || {};
  if (!['Offer', 'Rejected'].includes(status)) {
    return sendJSON(res, 400, { ok: false, error: 'status must be Offer or Rejected' });
  }
  if (!Number.isInteger(rowIndex) || rowIndex < 0) {
    return sendJSON(res, 400, { ok: false, error: 'rowIndex must be a non-negative integer' });
  }

  let header, dataRows;
  try {
    ({ header, dataRows } = readCSVRows(JOB_POOL_PATH));
  } catch (e) {
    return sendJSON(res, 500, { ok: false, error: 'Could not read job_pool.csv: ' + e.message });
  }

  const companyCol = header.indexOf('company');
  const titleCol = header.indexOf('job_title');
  const statusCol = header.indexOf('status');

  if (statusCol === -1 || companyCol === -1 || titleCol === -1) {
    return sendJSON(res, 500, { ok: false, error: 'job_pool.csv is missing an expected column' });
  }
  if (rowIndex >= dataRows.length) {
    return sendJSON(res, 409, { ok: false, error: 'rowIndex out of range — the file may have changed, please refresh' });
  }

  const target = dataRows[rowIndex];
  // job_pool.csv may have been rewritten (e.g. by the agent) between page
  // load and this click, which would shift row positions — confirm the row
  // at this index is still the same job before overwriting its status.
  if (target[companyCol] !== company || target[titleCol] !== job_title) {
    return sendJSON(res, 409, { ok: false, error: 'This row no longer matches — the dashboard data changed, please refresh and try again' });
  }

  target[statusCol] = status;

  try {
    writeCSVRows(JOB_POOL_PATH, header, dataRows);
  } catch (e) {
    return sendJSON(res, 500, { ok: false, error: 'Could not write job_pool.csv: ' + e.message });
  }

  sendJSON(res, 200, { ok: true });
}

// Locate + verify a job_pool.csv row by index, checking it still matches the
// company/job_title the client last saw (same staleness guard as above).
// Returns { header, dataRows, companyCol, titleCol, stageCol, target } or
// throws an Error with an httpStatus property for the caller to relay.
function locateJobRow(jobRowIndex, company, job_title) {
  if (!Number.isInteger(jobRowIndex) || jobRowIndex < 0) {
    const e = new Error('jobRowIndex must be a non-negative integer'); e.httpStatus = 400; throw e;
  }
  let header, dataRows;
  try {
    ({ header, dataRows } = readCSVRows(JOB_POOL_PATH));
  } catch (err) {
    const e = new Error('Could not read job_pool.csv: ' + err.message); e.httpStatus = 500; throw e;
  }
  const companyCol = header.indexOf('company');
  const titleCol = header.indexOf('job_title');
  const statusCol = header.indexOf('status');
  const stageCol = header.indexOf('current_stage');
  if ([companyCol, titleCol, statusCol, stageCol].includes(-1)) {
    const e = new Error('job_pool.csv is missing an expected column (company/job_title/status/current_stage)'); e.httpStatus = 500; throw e;
  }
  if (jobRowIndex >= dataRows.length) {
    const e = new Error('jobRowIndex out of range — the file may have changed, please refresh'); e.httpStatus = 409; throw e;
  }
  const target = dataRows[jobRowIndex];
  if (target[companyCol] !== company || target[titleCol] !== job_title) {
    const e = new Error('This job row no longer matches — the dashboard data changed, please refresh and try again'); e.httpStatus = 409; throw e;
  }
  if (target[statusCol] !== 'Submitted') {
    const e = new Error('This job is not in the Submitted/Applied bucket — calendar events are only for already-applied jobs'); e.httpStatus = 409; throw e;
  }
  return { header, dataRows, statusCol, stageCol, target };
}

async function handleCalendarAdd(req, res) {
  let payload;
  try {
    payload = await readJSONBody(req);
  } catch (e) {
    return sendJSON(res, 400, { ok: false, error: e.message });
  }

  const { jobRowIndex, company, job_title, date, time, event_type } = payload || {};
  if (!DATE_RE.test(date)) return sendJSON(res, 400, { ok: false, error: 'date must be YYYY-MM-DD' });
  if (!TIME_RE.test(time)) return sendJSON(res, 400, { ok: false, error: 'time must be HH:MM' });
  if (typeof event_type !== 'string' || !event_type.trim()) {
    return sendJSON(res, 400, { ok: false, error: 'event_type is required' });
  }

  let jobRow;
  try {
    jobRow = locateJobRow(jobRowIndex, company, job_title);
  } catch (e) {
    return sendJSON(res, e.httpStatus || 500, { ok: false, error: e.message });
  }

  // current_stage is the event content verbatim — no auto-suffix. Every
  // company's process reads differently, so don't guess a shared phrasing
  // convention on top of what the user typed.
  const stage = event_type.trim();
  jobRow.target[jobRow.stageCol] = stage;

  let fuHeader, fuDataRows;
  try {
    ({ header: fuHeader, dataRows: fuDataRows } = readCSVRows(FOLLOW_UP_PATH));
  } catch (e) {
    return sendJSON(res, 500, { ok: false, error: 'Could not read follow_up.csv: ' + e.message });
  }
  const cols = ['date', 'company', 'job_title', 'contact', 'channel', 'event_type', 'deadline', 'next_action', 'status', 'notes', 'time'];
  if (cols.some(c => fuHeader.indexOf(c) === -1)) {
    return sendJSON(res, 500, { ok: false, error: 'follow_up.csv is missing an expected column' });
  }
  const newRow = new Array(fuHeader.length).fill('');
  newRow[fuHeader.indexOf('date')] = date;
  newRow[fuHeader.indexOf('company')] = company;
  newRow[fuHeader.indexOf('job_title')] = job_title;
  newRow[fuHeader.indexOf('event_type')] = event_type.trim();
  newRow[fuHeader.indexOf('status')] = 'Scheduled';
  newRow[fuHeader.indexOf('time')] = time;
  fuDataRows.push(newRow);
  const followUpRowIndex = fuDataRows.length - 1;

  try {
    writeCSVRows(JOB_POOL_PATH, jobRow.header, jobRow.dataRows);
    writeCSVRows(FOLLOW_UP_PATH, fuHeader, fuDataRows);
  } catch (e) {
    return sendJSON(res, 500, { ok: false, error: 'Could not write dashboard files: ' + e.message });
  }

  sendJSON(res, 200, { ok: true, followUpRowIndex, current_stage: stage });
}

async function handleCalendarUpdate(req, res) {
  let payload;
  try {
    payload = await readJSONBody(req);
  } catch (e) {
    return sendJSON(res, 400, { ok: false, error: e.message });
  }

  const { followUpRowIndex, jobRowIndex, company, job_title, date, time, event_type } = payload || {};
  if (!Number.isInteger(followUpRowIndex) || followUpRowIndex < 0) {
    return sendJSON(res, 400, { ok: false, error: 'followUpRowIndex must be a non-negative integer' });
  }
  if (!DATE_RE.test(date)) return sendJSON(res, 400, { ok: false, error: 'date must be YYYY-MM-DD' });
  if (!TIME_RE.test(time)) return sendJSON(res, 400, { ok: false, error: 'time must be HH:MM' });
  if (typeof event_type !== 'string' || !event_type.trim()) {
    return sendJSON(res, 400, { ok: false, error: 'event_type is required' });
  }

  let fuHeader, fuDataRows;
  try {
    ({ header: fuHeader, dataRows: fuDataRows } = readCSVRows(FOLLOW_UP_PATH));
  } catch (e) {
    return sendJSON(res, 500, { ok: false, error: 'Could not read follow_up.csv: ' + e.message });
  }
  const fuCompanyCol = fuHeader.indexOf('company');
  const fuTitleCol = fuHeader.indexOf('job_title');
  if (followUpRowIndex >= fuDataRows.length) {
    return sendJSON(res, 409, { ok: false, error: 'This event no longer exists — the calendar may have changed, please refresh' });
  }
  const fuTarget = fuDataRows[followUpRowIndex];
  if (fuTarget[fuCompanyCol] !== company || fuTarget[fuTitleCol] !== job_title) {
    return sendJSON(res, 409, { ok: false, error: 'This event no longer matches — the calendar may have changed, please refresh' });
  }

  let jobRow;
  try {
    jobRow = locateJobRow(jobRowIndex, company, job_title);
  } catch (e) {
    return sendJSON(res, e.httpStatus || 500, { ok: false, error: e.message });
  }

  fuTarget[fuHeader.indexOf('date')] = date;
  fuTarget[fuHeader.indexOf('time')] = time;
  fuTarget[fuHeader.indexOf('event_type')] = event_type.trim();

  // Same rule as add: current_stage is the event content verbatim.
  const stage = event_type.trim();
  jobRow.target[jobRow.stageCol] = stage;

  try {
    writeCSVRows(FOLLOW_UP_PATH, fuHeader, fuDataRows);
    writeCSVRows(JOB_POOL_PATH, jobRow.header, jobRow.dataRows);
  } catch (e) {
    return sendJSON(res, 500, { ok: false, error: 'Could not write dashboard files: ' + e.message });
  }

  sendJSON(res, 200, { ok: true, current_stage: stage });
}

async function handleCalendarDelete(req, res) {
  let payload;
  try {
    payload = await readJSONBody(req);
  } catch (e) {
    return sendJSON(res, 400, { ok: false, error: e.message });
  }

  const { followUpRowIndex, company, job_title, event_type, date, time } = payload || {};
  if (!Number.isInteger(followUpRowIndex) || followUpRowIndex < 0) {
    return sendJSON(res, 400, { ok: false, error: 'followUpRowIndex must be a non-negative integer' });
  }

  let fuHeader, fuDataRows;
  try {
    ({ header: fuHeader, dataRows: fuDataRows } = readCSVRows(FOLLOW_UP_PATH));
  } catch (e) {
    return sendJSON(res, 500, { ok: false, error: 'Could not read follow_up.csv: ' + e.message });
  }
  if (followUpRowIndex >= fuDataRows.length) {
    return sendJSON(res, 409, { ok: false, error: 'This event no longer exists — the calendar may have changed, please refresh' });
  }
  const fuTarget = fuDataRows[followUpRowIndex];
  const matches = (col, val) => fuTarget[fuHeader.indexOf(col)] === val;
  if (!matches('company', company) || !matches('job_title', job_title) || !matches('event_type', event_type) || !matches('date', date) || !matches('time', time)) {
    return sendJSON(res, 409, { ok: false, error: 'This event no longer matches — the calendar may have changed, please refresh' });
  }

  fuDataRows.splice(followUpRowIndex, 1);

  try {
    writeCSVRows(FOLLOW_UP_PATH, fuHeader, fuDataRows);
  } catch (e) {
    return sendJSON(res, 500, { ok: false, error: 'Could not write follow_up.csv: ' + e.message });
  }

  // Deliberately does not revert job_pool.csv's current_stage — there's no
  // reliable "previous stage" to roll back to. Edit the stage manually if
  // deleting this event should also change what's shown on the job card.
  sendJSON(res, 200, { ok: true });
}

// ── 待办事项（todos.json）：勾选/取消勾选 ────────────────────────────
// 只在文件里改 done / done_at 两个字段，其余条目原样保留。
async function handleTodoToggle(req, res) {
  let payload;
  try { payload = await readJSONBody(req); }
  catch (e) { return sendJSON(res, 400, { ok: false, error: e.message }); }

  const id = payload && payload.id;
  if (!id) return sendJSON(res, 400, { ok: false, error: 'id is required' });

  if (!fs.existsSync(TODOS_PATH)) {
    return sendJSON(res, 500, { ok: false, error: 'todos.json not found' });
  }
  let doc;
  try { doc = JSON.parse(fs.readFileSync(TODOS_PATH, 'utf8')); }
  catch (e) { return sendJSON(res, 500, { ok: false, error: 'Could not read todos.json: ' + e.message }); }

  const items = Array.isArray(doc.items) ? doc.items : [];
  const item = items.find(x => x && x.id === id);
  if (!item) return sendJSON(res, 404, { ok: false, error: 'todo not found: ' + id });

  // done 未显式给出时按「取反」处理，便于前端只传 id
  const nextDone = (typeof payload.done === 'boolean') ? payload.done : !item.done;
  item.done = nextDone;
  item.done_at = nextDone ? new Date().toISOString().slice(0, 10) : '';
  doc.updated_at = new Date().toISOString().slice(0, 10);

  try { fs.writeFileSync(TODOS_PATH, JSON.stringify(doc, null, 2) + '\n'); }
  catch (e) { return sendJSON(res, 500, { ok: false, error: 'Could not write todos.json: ' + e.message }); }

  const openCount = items.filter(x => !x.done).length;
  sendJSON(res, 200, { ok: true, id: id, done: item.done, done_at: item.done_at, open: openCount, total: items.length });
}

const ROUTES = {
  '/api/update-status': handleUpdateStatus,
  '/api/calendar/add': handleCalendarAdd,
  '/api/calendar/update': handleCalendarUpdate,
  '/api/calendar/delete': handleCalendarDelete,
  '/api/todos/toggle': handleTodoToggle,
  '/api/dsh-handoff/request': handleDshHandoffRequest,
  '/api/dsh-handoff/respond': handleDshHandoffRespond,
};

const server = http.createServer((req, res) => {
  const urlPath = decodeURIComponent(req.url.split('?')[0]);

  if (req.method === 'GET' && urlPath === '/api/dsh-handoff/list') {
    handleDshHandoffList(req, res);
    return;
  }
  if (req.method === 'GET' && urlPath === '/api/dsh-handoff/status') {
    handleDshHandoffStatus(req, res);
    return;
  }

  if (req.method === 'POST' && ROUTES[urlPath]) {
    ROUTES[urlPath](req, res);
    return;
  }

  if (req.method !== 'GET') {
    res.writeHead(405);
    res.end('Method not allowed');
    return;
  }

  const servedPath = urlPath === '/' ? '/dashboard.html' : urlPath;
  const filePath = path.join(ROOT, servedPath);

  // Prevent escaping the dashboard folder.
  if (!filePath.startsWith(ROOT)) {
    res.writeHead(403);
    res.end('Forbidden');
    return;
  }

  fs.readFile(filePath, (err, content) => {
    if (err) {
      res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
      res.end('Not found: ' + urlPath);
      return;
    }
    const ext = path.extname(filePath);
    res.writeHead(200, {
      'Content-Type': MIME[ext] || 'application/octet-stream',
      'Cache-Control': 'no-store',
    });
    res.end(content);
  });
});

// Bind to localhost only — this server can now write to job_pool.csv, so it
// shouldn't be reachable from other devices on the network.
server.listen(PORT, '127.0.0.1', () => {
  console.log('Dashboard running / 仪表盘已启动: http://localhost:' + PORT + '/dashboard.html');
  console.log('Keep this window open to keep serving; close it or press Ctrl+C to stop.');
  console.log('保持这个窗口开着；关掉窗口或按 Ctrl+C 即可停止服务。');
});
