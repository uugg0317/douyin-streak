// Credential extraction Vue methods: only fake HTTP, fake time and in-memory data.
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert/strict');

const appSource = fs.readFileSync(path.resolve(__dirname, '../static/multi-app.js'), 'utf8');
const copy = value => JSON.parse(JSON.stringify(value));
const failure = (status = 503, detail = 'fake credential request failure') =>
  Object.assign(new Error(detail), {response: {status, data: {detail}}});
const accounts = [
  {id: 'alpha', display_name: '模拟主账号', enabled: true, has_state: true},
  {id: 'beta', display_name: '模拟朋友账号', enabled: true, has_state: false},
];
const now = Date.now();
const snapshot = (status = 'ready', aid = 'alpha', job = 'extract-alpha') => ({
  job_id: job, account_id: aid, display_name: accounts.find(a => a.id === aid)?.display_name || '',
  status, running: ['starting', 'waiting', 'ready', 'saving', 'cancelling', 'stopping'].includes(status),
  count: status === 'ready' ? 12 : 0, started_at: now / 1000 - 5,
  deadline: now / 1000 + 295, remaining_seconds: 295, error: null,
});
const idle = () => ({job_id: null, account_id: null, display_name: '', status: 'idle', running: false,
  count: 0, started_at: null, deadline: null, remaining_seconds: 0, error: null});

function fakeHTTP(overrides = {}) {
  const calls = [], store = {task: null, accounts: copy(accounts)};
  async function request(method, url, body) {
    calls.push({method, url, body: body === undefined ? undefined : copy(body)});
    if (overrides[method]) return overrides[method](url, body, store);
    if (method === 'get') {
      if (url === '/api/multi/credentials/extract-status') return {data: copy(store.task || idle())};
      if (url === '/api/multi/state') return {data: {accounts: copy(store.accounts), state: {}, jobs: []}};
      if (url === '/api/multi/backups') return {data: {slots: []}};
      return {data: {}};
    }
    if (method === 'post' && url.endsWith('/credentials/extract')) {
      const aid = url.match(/\/accounts\/([^/]+)\//)[1];
      store.task = snapshot('waiting', aid, 'extract-' + aid);
      return {data: copy(store.task)};
    }
    if (method === 'post' && /\/credentials\/extract\/[^/]+\/confirm$/.test(url)) {
      store.task = {...store.task, status: 'success', running: false};
      const aid = url.match(/\/accounts\/([^/]+)\//)[1];
      const account = store.accounts.find(a => a.id === aid); if (account) account.has_state = true;
      return {data: {ok: true, ...copy(store.task)}};
    }
    if (method === 'post' && /\/credentials\/extract\/[^/]+\/cancel$/.test(url)) {
      store.task = {...store.task, status: 'cancelled', running: false};
      return {data: {ok: true, ...copy(store.task)}};
    }
    return {data: {ok: true}};
  }
  return {get: url => request('get', url), post: (url, body) => request('post', url, body),
    put: (url, body) => request('put', url, body), patch: (url, body) => request('patch', url, body), calls, store};
}

function component(http = fakeHTTP(), task = null) {
  let options;
  const notices = [], dialogs = [], timers = [];
  const sandbox = {
    Vue: {createApp(c) {options = c; return {use() {return this;}, mount() {}};}},
    ElementPlus: {
      ElMessage: Object.fromEntries(['error', 'success', 'info', 'warning'].map(kind => [kind, text => notices.push({kind, text})])),
      ElMessageBox: {async confirm(text, title) {dialogs.push({text, title}); return true;}},
    },
    document: {hidden: false, addEventListener() {}, removeEventListener() {}},
    localStorage: {getItem() {return null;}, removeItem() {}, setItem(key) {assert(!/auth|token|cookie|session/i.test(key));}},
    setTimeout(fn, delay) {timers.push({fn, delay}); return timers.length;}, clearTimeout() {},
    addEventListener() {}, removeEventListener() {}, console, Date,
  };
  sandbox.window = sandbox;
  vm.runInNewContext(appSource, sandbox, {filename: 'multi-app.js'});
  options = sandbox.SparkMultiAppOptions || options;
  const value = Object.assign(options.data(), {http});
  for (const [key, fn] of Object.entries(options.methods)) value[key] = fn.bind(value);
  for (const [key, fn] of Object.entries(options.computed || {})) {
    Object.defineProperty(value, key, {get: (typeof fn === 'function' ? fn : fn.get).bind(value), configurable: true});
  }
  value.$nextTick = async fn => fn && fn(); value.$refs = {};
  value.ready = true; value.accounts = copy(accounts); value.selectionInitialized = true;
  value.selectedAccountId = 'alpha'; value.page = 'credentials'; value.credentialNow = now;
  if (task) {value.credentialTask = copy(task); http.store.task = copy(task);}
  return {value, http, notices, dialogs, sandbox, timers};
}

function deferred() {let resolve; const promise = new Promise(yes => {resolve = yes;}); return {promise, resolve};}
const tick = async () => {await Promise.resolve(); await Promise.resolve();};
const results = [];
async function scenario(name, test) {await test(); results.push(name);}

(async () => {
  await scenario('Starting extraction freezes account even after navigating to another account', async () => {
    const {value: v, http} = component();
    await v.startCredentialExtract();
    assert.equal(v.credentialTask.account_id, 'alpha');
    await v.navigate('credentials', 'beta');
    assert.equal(v.selectedAccountId, 'beta'); assert.equal(v.credentialTask.account_id, 'alpha');
    assert.equal(v.credentialTask.display_name, '模拟主账号'); assert(v.credentialTargetMismatch);
    assert.equal(http.calls.find(c => c.url.endsWith('/credentials/extract')).url, '/api/multi/accounts/alpha/credentials/extract');
  });
  await scenario('Late start callback retains originally captured target', async () => {
    const d = deferred(), http = fakeHTTP({post: async () => d.promise});
    const {value: v} = component(http); const start = v.startCredentialExtract(); await tick();
    v.selectedAccountId = 'beta';
    d.resolve({data: snapshot('waiting')}); await start;
    assert.equal(v.credentialTask.account_id, 'alpha'); assert(v.credentialTargetMismatch);
    assert.equal(v.credentialStarting, false);
  });
  await scenario('Confirmation writes frozen task account and job rather than selected account', async () => {
    const {value: v, http} = component(fakeHTTP(), snapshot()); v.selectedAccountId = 'beta';
    await v.confirmCredentialExtract();
    const confirm = http.calls.find(c => c.url.endsWith('/confirm'));
    assert.equal(confirm.url, '/api/multi/accounts/alpha/credentials/extract/extract-alpha/confirm');
    assert(!JSON.stringify(confirm.body || {}).includes('cookies'));
    assert.equal(v.credentialTask.status, 'success'); assert.equal(v.credentialActionBusy, false);
  });
  await scenario('Failed confirmation preserves ready draft and permits retry', async () => {
    let fail = true;
    const http = fakeHTTP({post: async () => {if (fail) throw failure(503); return {data: {...snapshot(), status: 'success', running: false}};}});
    const {value: v} = component(http, snapshot());
    await v.confirmCredentialExtract();
    assert.equal(v.credentialTask.status, 'ready'); assert(v.credentialReady); assert(v.credentialError);
    assert(v.credentialError.includes('原凭据已保留'));
    assert.equal(v.credentialActionBusy, false);
    fail = false; await v.confirmCredentialExtract(); assert.equal(v.credentialTask.status, 'success');
  });
  await scenario('Cancellation retains original frozen account and never confirms', async () => {
    const {value: v, http} = component(fakeHTTP(), snapshot('waiting')); v.selectedAccountId = 'beta';
    await v.cancelCredentialExtract();
    assert.equal(http.calls.find(c => c.url.endsWith('/cancel')).url, '/api/multi/accounts/alpha/credentials/extract/extract-alpha/cancel');
    assert.equal(v.credentialTask.status, 'cancelled'); assert(!v.credentialActive);
    assert.equal(http.calls.filter(c => c.url.endsWith('/confirm')).length, 0);
  });
  await scenario('Failed cancel keeps current task visible and retryable', async () => {
    const {value: v} = component(fakeHTTP({post: async () => {throw failure(503);}}), snapshot());
    await v.cancelCredentialExtract();
    assert.equal(v.credentialTask.status, 'ready'); assert(v.credentialActive); assert(v.credentialError);
    assert.equal(v.credentialActionBusy, false);
  });
  await scenario('Out-of-order polling cannot overwrite newer ready status', async () => {
    const d = deferred(); let count = 0;
    const http = fakeHTTP({get: async () => ++count === 1 ? d.promise : {data: snapshot()}});
    const {value: v} = component(http, snapshot('waiting'));
    const old = v.pollCredentialExtract(); await v.pollCredentialExtract();
    d.resolve({data: snapshot('waiting')}); await old;
    assert.equal(v.credentialTask.status, 'ready');
  });
  await scenario('Old task poll cannot overwrite a newly started session', async () => {
    const d = deferred(), {value: v} = component(fakeHTTP({get: async () => d.promise}), snapshot('waiting'));
    const old = v.pollCredentialExtract();
    v.credentialGeneration++; v.credentialTask = snapshot('waiting', 'beta', 'extract-beta');
    d.resolve({data: snapshot()}); await old;
    assert.equal(v.credentialTask.job_id, 'extract-beta'); assert.equal(v.credentialTask.account_id, 'beta');
  });
  await scenario('Old confirmation callback cannot overwrite a new extraction session', async () => {
    const d = deferred(), {value: v} = component(fakeHTTP({post: async () => d.promise}), snapshot());
    const pending = v.confirmCredentialExtract(); await tick();
    v.credentialGeneration++; v.credentialTask = snapshot('waiting', 'beta', 'extract-beta');
    d.resolve({data: {...snapshot(), status: 'success', running: false}}); await pending;
    assert.equal(v.credentialTask.job_id, 'extract-beta'); assert.equal(v.credentialTask.status, 'waiting');
  });
  await scenario('Polling failure keeps direct access and the ready task', async () => {
    const {value: v} = component(fakeHTTP({get: async () => {throw failure(401);}}), snapshot());
    await v.pollCredentialExtract(); assert.equal(v.ready, true); assert.equal(v.credentialTask.status, 'ready');
  });
  await scenario('Server timeout remains authoritative and never auto-saves', async () => {
    const expired = {...snapshot('timeout'), running: false, deadline: now / 1000 - 1, remaining_seconds: 0, error: 'mock deadline'};
    const {value: v, http} = component(fakeHTTP({get: async () => ({data: expired})}), snapshot());
    await v.pollCredentialExtract(); assert.equal(v.credentialTask.status, 'timeout'); assert(!v.credentialActive);
    assert.equal(v.credentialRemainingSeconds, 0); assert.equal(http.calls.filter(c => c.url.endsWith('/confirm')).length, 0);
  });
  await scenario('A second start cannot replace an active extraction', async () => {
    const {value: v, http} = component(fakeHTTP(), snapshot('waiting'));
    await v.startCredentialExtract(accounts[1]); assert.equal(v.credentialTask.account_id, 'alpha');
    assert.equal(http.calls.filter(c => c.method === 'post' && c.url.endsWith('/credentials/extract')).length, 0);
  });
  await scenario('Snapshot processing retains safe metadata and drops credential payloads', async () => {
    const {value: v} = component();
    v.applyCredentialTask({...snapshot(), cookies: [{name: 'fake-cookie', value: 'secret-must-not-be-in-ui'}], origins: [], storage_state: {cookies: []}});
    assert.equal(v.credentialTask.account_id, 'alpha');
    assert.equal('cookies' in v.credentialTask, false); assert.equal('origins' in v.credentialTask, false);
    assert.equal('storage_state' in v.credentialTask, false);
  });
  await scenario('Transient polling failure preserves ready task and displays retry context', async () => {
    const {value: v} = component(fakeHTTP({get: async () => {throw failure(503);}}), snapshot());
    await v.pollCredentialExtract(); assert.equal(v.credentialTask.status, 'ready');
    assert(v.credentialError); assert.equal(v.ready, true); assert(v.credentialActive);
  });
  await scenario('Expired ready task refreshes server status without sending confirm', async () => {
    const expired = {...snapshot(), deadline: Date.now() / 1000 - 10, remaining_seconds: 0};
    const http = fakeHTTP({get: async () => ({data: {...expired, status: 'timeout', running: false}})});
    const {value: v} = component(http, expired); await v.confirmCredentialExtract();
    assert.equal(http.calls.filter(c => c.url.endsWith('/confirm')).length, 0);
    assert.equal(v.credentialTask.status, 'timeout'); assert(!v.credentialReady);
  });
  await scenario('Deadline parser accepts seconds, milliseconds and ISO timestamps', async () => {
    const {value: v} = component(); const stamp = 1790000000000;
    assert.equal(v.credentialDeadline({deadline: stamp / 1000}), stamp);
    assert.equal(v.credentialDeadline({deadline: String(stamp / 1000)}), stamp);
    assert.equal(v.credentialDeadline({deadline: stamp}), stamp);
    assert.equal(v.credentialDeadline({deadline: new Date(stamp).toISOString()}), stamp);
    assert.equal(v.credentialDeadline({deadline: 'invalid'}), 0);
  });
  await scenario('Server restart idle status ends an existing task instead of claiming save', async () => {
    const {value: v} = component(fakeHTTP({get: async () => ({data: idle()})}), snapshot('waiting'));
    await v.pollCredentialExtract(); assert.equal(v.credentialTask.status, 'failed');
    assert(!v.credentialActive); assert(v.credentialError); assert.equal(v.credentialTask.account_id, 'alpha');
  });
  await scenario('Conflicting start response remains safely bound to original account', async () => {
    const {value: v} = component(fakeHTTP({post: async () => ({data: snapshot('waiting', 'beta', 'wrong-task')})}));
    await v.startCredentialExtract(); assert.equal(v.credentialTask.account_id, 'alpha');
    assert.equal(v.credentialTask.status, 'failed'); assert(v.credentialError); assert.equal(v.credentialStarting, false);
  });
  await scenario('Conflicting confirmation response keeps original ready task', async () => {
    const {value: v} = component(fakeHTTP({post: async () => ({data: snapshot('success', 'beta', 'wrong-task')})}), snapshot());
    await v.confirmCredentialExtract(); assert.equal(v.credentialTask.job_id, 'extract-alpha');
    assert.equal(v.credentialTask.status, 'ready'); assert(v.credentialError); assert.equal(v.credentialActionBusy, false);
  });
  await scenario('Unsaved editor cancellation blocks extraction before any request', async () => {
    const {value: v, http} = component(); v.page = 'params'; v.configLoaded = true;
    v.configForm.max_friends_per_run = 31; const pending = v.startCredentialExtract(); await tick();
    assert(v.guardVisible); await v.resolveUnsaved('cancel'); await pending;
    assert.equal(v.credentialTask, null); assert(v.configDirty);
    assert.equal(http.calls.filter(c => c.url.endsWith('/credentials/extract')).length, 0);
  });
  await scenario('Concurrent start clicks issue a single extraction request', async () => {
    const d = deferred(); const http = fakeHTTP({post: async () => d.promise});
    const {value: v} = component(http);
    const first = v.startCredentialExtract(), second = v.startCredentialExtract(); await tick();
    assert.equal(http.calls.filter(c => c.url.endsWith('/credentials/extract')).length, 1);
    d.resolve({data: snapshot('waiting')}); await Promise.all([first, second]);
    assert.equal(v.credentialTask.account_id, 'alpha');
  });
  await scenario('Stopping remains active and prevents replacement until server cleanup', async () => {
    const {value: v, http} = component(fakeHTTP(), snapshot('stopping'));
    assert(v.credentialActive); assert(!v.credentialReady);
    await v.startCredentialExtract(accounts[1]);
    assert.equal(http.calls.filter(c => c.url.endsWith('/credentials/extract')).length, 0);
    assert.equal(v.credentialTask.status, 'stopping');
  });
  await scenario('Global extraction reservation makes all account operations busy', async () => {
    const {value: v} = component(fakeHTTP(), snapshot('waiting'));
    v.jobs = [{job_id: 'extract-alpha', account_id: 'alpha', kind: 'credential_extract', global_scope: true}];
    assert(v.accountBusy(accounts[0])); assert(v.accountBusy(accounts[1]));
  });
  await scenario('Local extraction also makes other accounts busy before state refresh', async () => {
    const {value: v} = component(fakeHTTP(), snapshot('waiting')); v.jobs = [];
    assert(v.accountBusy(accounts[0])); assert(v.accountBusy(accounts[1]));
  });
  await scenario('Newer task from another client can replace old identity during polling', async () => {
    const newer = {...snapshot('waiting', 'beta', 'other-client-task'), started_at: now / 1000 + 1};
    const {value: v} = component(fakeHTTP({get: async () => ({data: newer})}), snapshot('success'));
    const generation = v.credentialGeneration; await v.pollCredentialExtract();
    assert.equal(v.credentialTask.job_id, 'other-client-task'); assert.equal(v.credentialTask.account_id, 'beta');
    assert(v.credentialGeneration > generation); assert(v.credentialActive);
  });
  await scenario('Older or undated foreign task cannot replace current task', async () => {
    for (const started_at of [now / 1000 - 10, null]) {
      const foreign = {...snapshot('waiting', 'beta', 'stale-task'), started_at};
      const {value: v} = component(fakeHTTP({get: async () => ({data: foreign})}), snapshot());
      await v.pollCredentialExtract(); assert.equal(v.credentialTask.job_id, 'extract-alpha');
      assert.equal(v.credentialTask.account_id, 'alpha');
    }
  });
  await scenario('Late old poll cannot replace accepted newer client task', async () => {
    const d = deferred(); let count = 0;
    const newer = {...snapshot('waiting', 'beta', 'other-client-task'), started_at: now / 1000 + 1};
    const {value: v} = component(fakeHTTP({get: async () => ++count === 1 ? d.promise : {data: newer}}), snapshot('success'));
    const old = v.pollCredentialExtract(); await v.pollCredentialExtract();
    d.resolve({data: snapshot()}); await old; assert.equal(v.credentialTask.job_id, 'other-client-task');
  });
  await scenario('Global credential job label shows target account instead of all accounts', async () => {
    const {value: v} = component(fakeHTTP(), snapshot('waiting'));
    const job = {job_id: 'extract-alpha', account_id: 'alpha', kind: 'credential_extract', global_scope: true};
    assert.equal(v.jobAccount(job), '模拟主账号'); assert.notEqual(v.jobAccount(job), '全部账号');
  });
  await scenario('Active extraction blocks all write actions before any HTTP call', async () => {
    const {value: v, http} = component(fakeHTTP(), snapshot('waiting')); v.selectedAccountId = 'beta';
    v.addForm = {id: 'gamma', display_name: '模拟新号'};
    v.editTarget = accounts[1]; v.editForm = {display_name: '模拟改名', enabled: false};
    v.copyTarget = accounts[1]; v.copyForm = {source_id: 'alpha', copy_ledger: false};
    v.friendsLoaded = true; v.friendsSelected = ['模拟好友']; v.configLoaded = true;
    await v.runOne(accounts[1], true); await v.runAll(true); await v.runBackup(); await v.createAccount();
    await v.saveEdit(); await v.toggleEnabled(accounts[1], false); await v.removeAccount(accounts[1]);
    await v.fetchContacts(accounts[1]); await v.reviewAccount(accounts[1]); await v.saveCopy();
    assert.equal(await v.saveFriends(), false); assert.equal(await v.saveConfig(), false);
    v.uploadState(accounts[1]);
    assert.equal(http.calls.length, 0);
  });
  await scenario('Dismissed terminal task stays hidden when polling returns the same result', async () => {
    const {value: v} = component(fakeHTTP(), snapshot('success'));
    v.dismissCredentialTask(); assert.equal(v.credentialTask, null);
    await v.pollCredentialExtract(); assert.equal(v.credentialTask, null);
    assert.equal(v.credentialDismissedJobId, 'extract-alpha');
  });
  await scenario('A new server task is visible after a prior result was dismissed', async () => {
    const http = fakeHTTP(), {value: v} = component(http, snapshot('success'));
    v.dismissCredentialTask(); http.store.task = {...snapshot('waiting', 'beta', 'new-task'), started_at: now / 1000 + 1};
    await v.pollCredentialExtract(); assert.equal(v.credentialTask.job_id, 'new-task');
    assert.equal(v.credentialTask.account_id, 'beta'); assert(v.credentialTaskVisible);
  });
  await scenario('A late result response cannot restore a just-dismissed task', async () => {
    const d = deferred(), {value: v} = component(fakeHTTP({get: async () => d.promise}), snapshot('success'));
    const pending = v.pollCredentialExtract(); v.dismissCredentialTask();
    d.resolve({data: snapshot('success')}); await pending; assert.equal(v.credentialTask, null);
  });
  await scenario('An active task cannot be dismissed accidentally', async () => {
    const {value: v} = component(fakeHTTP(), snapshot('waiting'));
    v.dismissCredentialTask(); assert.equal(v.credentialTask.job_id, 'extract-alpha'); assert(v.credentialActive);
  });
  console.log(`Credentials frontend: ${results.length} isolated regression scenarios passed`);
  results.forEach(name => console.log('  PASS ' + name));
})().catch(error => {console.error(error); process.exitCode = 1;});
