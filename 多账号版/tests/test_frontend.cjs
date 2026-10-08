// Real Vue methods, fake HTTP/storage/timers only: never contacts an account.
const fs=require('fs'), path=require('path'), vm=require('vm'), assert=require('assert/strict');
const root=path.resolve(__dirname,'..'), notices=[], scheduled=[], stored=new Map();
let options;
const doc={hidden:false,addEventListener(){},removeEventListener(){},querySelector(){return null},documentElement:{dataset:{},style:{setProperty(){}}},body:{classList:{add(){},remove(){},toggle(){}}}};
const sandbox={
  Vue:{createApp(c){options=c;return{use(){return this},mount(){}}}},
  ElementPlus:{ElMessage:Object.fromEntries(['error','success','info','warning'].map(kind=>[kind,text=>notices.push({kind,text})])),ElMessageBox:{confirm:async()=>true}},
  axios:{create(){return{interceptors:{response:{use(){}}}}}}, document:doc,
  localStorage:{getItem(k){return stored.get(k)??null},removeItem(k){stored.delete(k)},setItem(k,v){assert(!/token|auth|session|cookie/i.test(k),'credentials must not persist');stored.set(k,String(v))}},
  setTimeout(fn,delay){scheduled.push({fn,delay});return scheduled.length},clearTimeout(){},
  addEventListener(){},removeEventListener(){},matchMedia(){return{matches:false,addEventListener(){},removeEventListener(){}}},
  SparkMotion:{setEnabled(){},refresh(){},destroy(){},status(){return{reducedMotion:false,webgl:true}}},FormData:class{append(){}},URL,Date,console
};
sandbox.window=sandbox;
vm.runInNewContext(fs.readFileSync(path.join(root,'static/multi-app.js'),'utf8'),sandbox,{filename:'multi-app.js'});
options=sandbox.SparkMultiAppOptions||options;
assert(options?.data&&options?.methods,'isolated tests require exposed app options');
const clone=v=>JSON.parse(JSON.stringify(v));
const error=(status=503,detail='fake backend failure')=>Object.assign(new Error(detail),{response:{status,data:{detail}}});
const accounts=[{id:'alpha',display_name:'模拟主账号',enabled:true,has_state:true,ledger_total:3,ledger_selected:2},{id:'beta',display_name:'模拟朋友账号',enabled:true,has_state:true,ledger_total:2,ledger_selected:1}];
const ledgers={alpha:[{display_name:'Alpha One',selected:true,streak_days:20},{display_name:'Alpha Two',selected:true,streak_days:40},{display_name:'Alpha Three',selected:false,streak_days:8}],beta:[{display_name:'Beta One',selected:true,streak_days:3},{display_name:'Beta Two',selected:false,streak_days:17}]};
const configs={alpha:{max_friends_per_run:20,send_gap_min:2,send_gap_max:3,auto_run_enabled:false,messages:['[续火花吧]']},beta:{max_friends_per_run:70,send_gap_min:3,send_gap_max:4,auto_run_enabled:false,messages:['[续火花吧]']}};
function fakeHTTP(overrides={}){
 const calls=[],ledger=clone(ledgers),config=clone(configs);
 let state={accounts:clone(accounts),state:{running:false},jobs:[],max_accounts:10,auto_run_enabled:false,backup_enabled:false,next_run:null,next_backup:null};
 async function request(method,url,body){
  calls.push({method,url,body:body===undefined?undefined:clone(body)});
  if(overrides[method])return overrides[method](url,body,{calls,ledger,config,state});
  const aid=url.match(/\/accounts\/([^/]+)\//)?.[1];
  if(method==='get'){
   if(url==='/api/multi/state')return{data:clone(state)};
   if(url==='/api/multi/backups')return{data:{slots:[],slot_count:7}};
   if(url.endsWith('/ledger'))return{data:{entries:clone(ledger[aid]||[])}};
   if(url.endsWith('/config'))return{data:{config:clone(config[aid]||{})}};
   if(url.endsWith('/logs'))return{data:{logs:aid+' logs'}};
   if(url.endsWith('/runtime'))return{data:{runtime:{}}};
   if(url.endsWith('/contacts/status'))return{data:{fetching:true,job_id:'fetch-'+aid}};
  }
  if(method==='post'&&url.endsWith('/ledger/selection')){ledger[aid].forEach(r=>r.selected=body.selected_names.includes(r.display_name));return{data:{ok:true,selected_count:body.selected_names.length}}}
  if(method==='put'&&url.endsWith('/config')){config[aid]=clone(body.config||body);return{data:{ok:true,config:clone(config[aid])}}}
  if(method==='post'&&url.endsWith('/remove')){state.accounts=state.accounts.filter(a=>a.id!==aid);return{data:{ok:true,removed:aid}}}
  return{data:{ok:true,started:true,job_id:'fake-job'}};
 }
 return{get:u=>request('get',u),post:(u,b)=>request('post',u,b),put:(u,b)=>request('put',u,b),patch:(u,b)=>request('patch',u,b),calls,ledger,config,setState(v){state=v}};
}
function component(http=fakeHTTP(),page='overview',aid='alpha'){
 const value=Object.assign(options.data(),{http});
 for(const[k,fn]of Object.entries(options.methods))value[k]=fn.bind(value);
 for(const[k,fn]of Object.entries(options.computed||{}))Object.defineProperty(value,k,{get:(typeof fn==='function'?fn:fn.get).bind(value),configurable:true});
 value.$nextTick=async fn=>fn&&fn();value.$refs={};value.accounts=clone(accounts);value.ready=true;value.selectionInitialized=true;value.selectedAccountId=aid;value.page=page;
 return value;
}
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return{promise,resolve,reject}}
const tick=async()=>{await Promise.resolve();await Promise.resolve()}, results=[];
async function scenario(name,fn){await fn();results.push(name)}
async function guarded(value,page,aid,choice){const p=value.navigate(page,aid);await tick();assert.equal(value.guardVisible,true,'dirty view must ask');await value.resolveUnsaved(choice);await p}
(async()=>{
 await scenario('Backend errors preserve direct access and clear loading',async()=>{const v=component(fakeHTTP({get:async()=>{throw error(503)}}));await v.load();assert.equal(v.ready,true);assert(v.loadError);assert.equal(v._loadPromise,null);assert.equal(v.loading,false)});
 await scenario('Cancel switch preserves account and friend draft',async()=>{const h=fakeHTTP(),v=component(h,'friends');await v.loadFriends();v.toggleFriend('Alpha Three');assert(v.friendsDirty);await guarded(v,'friends','beta','cancel');assert.equal(v.selectedAccountId,'alpha');assert(v.friendsDirty);assert(v.friendsSelected.includes('Alpha Three'));assert.equal(h.calls.filter(c=>c.method==='post').length,0)});
 await scenario('Discard switches account without saving friend draft',async()=>{const h=fakeHTTP(),v=component(h,'friends');await v.loadFriends();v.toggleFriend('Alpha Three');await guarded(v,'friends','beta','discard');assert.equal(v.selectedAccountId,'beta');assert.equal(v.friendsDirty,false);assert(v.friendsRows.every(r=>r.display_name.startsWith('Beta')));assert.equal(h.calls.filter(c=>c.method==='post').length,0)});
 await scenario('Save and switch saves captured old account selection',async()=>{const h=fakeHTTP(),v=component(h,'friends');await v.loadFriends();v.toggleFriend('Alpha Three');await guarded(v,'friends','beta','save');const c=h.calls.find(c=>c.url.endsWith('/ledger/selection'));assert.equal(c.url,'/api/multi/accounts/alpha/ledger/selection');assert(c.body.selected_names.includes('Alpha Three'));assert.equal(v.selectedAccountId,'beta');assert.equal(v.friendsDirty,false)});
 await scenario('Failed guarded save retains dialog and draft until cancel',async()=>{const v=component(fakeHTTP({post:async()=>{throw error(503)}}),'friends');await v.loadFriends();v.toggleFriend('Alpha Three');const p=v.navigate('friends','beta');await tick();await v.resolveUnsaved('save');await tick();assert.equal(v.guardVisible,true);assert.equal(v.selectedAccountId,'alpha');assert(v.friendsDirty);assert.equal(v.guardSaving,false);assert.equal(v.savingFriends,false);await v.resolveUnsaved('cancel');await p});
 await scenario('Parameters save and page switch commit captured account',async()=>{const h=fakeHTTP(),v=component(h,'params');await v.loadConfig();v.configForm.max_friends_per_run=31;assert(v.configDirty);await guarded(v,'logs','alpha','save');const c=h.calls.find(c=>c.method==='put');assert.equal(c.url,'/api/multi/accounts/alpha/config');assert.equal(c.body.config.max_friends_per_run,31);assert.equal(v.page,'logs');assert.equal(v.logsText,'alpha logs')});
 await scenario('Failed parameters save preserves editable draft',async()=>{const v=component(fakeHTTP({put:async()=>{throw error(409)}}),'params');await v.loadConfig();v.configForm.send_gap_min=5;v.configForm.send_gap_max=6;assert.equal(await v.saveConfig(),false);assert(v.configDirty);assert.equal(v.configForm.send_gap_min,5);assert.equal(v.savingConfig,false);assert.equal(v.page,'params')});
 await scenario('Empty selection cannot be saved',async()=>{const h=fakeHTTP(),v=component(h,'friends');await v.loadFriends();v.friendsSelected=[];assert.equal(await v.saveFriends(),false);assert(v.friendsDirty);assert.equal(h.calls.filter(c=>c.url.endsWith('/selection')).length,0)});
 await scenario('Slow old account friends cannot overwrite new account',async()=>{const d=deferred(),h=fakeHTTP({get:async u=>u.includes('/alpha/')?d.promise:{data:{entries:clone(ledgers.beta)}}}),v=component(h,'friends');const old=v.loadFriends();v.clearAccountView();v.selectedAccountId='beta';await v.loadFriends();d.resolve({data:{entries:clone(ledgers.alpha)}});await old;assert(v.friendsRows.every(r=>r.display_name.startsWith('Beta')));assert.equal(v.selectedAccountId,'beta')});
 await scenario('Real navigation can leave a slow account load and discard its response',async()=>{const d=deferred(),h=fakeHTTP({get:async u=>u.includes('/alpha/')?d.promise:{data:{entries:clone(ledgers.beta)}}}),v=component(h);const first=v.navigate('friends','alpha');await tick();assert.equal(v.navigationBusy,false);assert.equal(await v.navigate('friends','beta'),true);assert.equal(v.selectedAccountId,'beta');d.resolve({data:{entries:clone(ledgers.alpha)}});await first;assert(v.friendsRows.every(r=>r.display_name.startsWith('Beta')));assert.equal(v.friendsDirty,false)});
 await scenario('Slow old parameters cannot overwrite new account',async()=>{const d=deferred(),h=fakeHTTP({get:async u=>u.includes('/alpha/')?d.promise:{data:{config:clone(configs.beta)}}}),v=component(h,'params');const old=v.loadConfig();v.clearAccountView();v.selectedAccountId='beta';await v.loadConfig();d.resolve({data:{config:clone(configs.alpha)}});await old;assert.equal(v.configForm.max_friends_per_run,70);assert.equal(v.configDirty,false)});
 await scenario('Friend response after leaving page is discarded',async()=>{const d=deferred(),v=component(fakeHTTP({get:async()=>d.promise}),'friends');const old=v.loadFriends();v.clearAccountView();v.page='logs';v.logsText='keep log';d.resolve({data:{entries:clone(ledgers.alpha)}});await old;assert.equal(v.friendsRows.length,0);assert.equal(v.logsText,'keep log')});
 await scenario('Older same-account log response cannot overwrite latest',async()=>{const d=deferred();let n=0;const v=component(fakeHTTP({get:async()=>++n===1?d.promise:{data:{logs:'newest logs'}}}),'logs');const old=v.refreshLogs();await v.refreshLogs();d.resolve({data:{logs:'old logs'}});await old;assert.equal(v.logsText,'newest logs')});
 await scenario('Friend load failure keeps loaded draft',async()=>{const h=fakeHTTP(),v=component(h,'friends');await v.loadFriends();v.toggleFriend('Alpha Three');h.get=async()=>{throw error(503)};await v.loadFriends();assert.equal(v.friendsRows.length,3);assert(v.friendsSelected.includes('Alpha Three'));assert(v.friendsDirty)});
 await scenario('State refresh does not overwrite dirty parameters',async()=>{const v=component(fakeHTTP(),'params');await v.loadConfig();v.configForm.max_friends_per_run=33;await v.load(true);assert.equal(v.configForm.max_friends_per_run,33);assert(v.configDirty)});
 await scenario('Collection task survives account and page navigation',async()=>{const v=component();v.fetchingIds=['alpha'];await v.navigate('logs','beta');assert(v.fetchingIds.includes('alpha'));assert.equal(v.selectedAccountId,'beta');assert.equal(v.page,'logs')});
 await scenario('Transient collection failure preserves pending task',async()=>{const v=component(fakeHTTP({get:async()=>{throw error(503)}}));v.fetchingIds=['alpha'];await v.pollFetch();assert(v.fetchingIds.includes('alpha'))});
 await scenario('Removal clears selected account without selecting another',async()=>{const v=component();await v.removeAccount(v.selectedAccount);assert.equal(v.selectedAccountId,'');assert.equal(v.page,'overview');assert.equal(v.accounts.length,1);assert.equal(v.accounts[0].id,'beta')});
 await scenario('External account removal clears current view',async()=>{const h=fakeHTTP(),v=component(h,'friends');await v.loadFriends();h.setState({accounts:[clone(accounts[1])],state:{},jobs:[],max_accounts:10});await v.load(true);assert.equal(v.selectedAccountId,'');assert.equal(v.page,'overview');assert.equal(v.friendsRows.length,0)});
 await scenario('Polling intervals distinguish idle busy and hidden',async()=>{const v=component();doc.hidden=false;v.schedulePoll();assert.equal(scheduled.at(-1).delay,10000);v.fetchingIds=['alpha'];v.schedulePoll();assert.equal(scheduled.at(-1).delay,2500);v.fetchingIds=[];v.running=true;v.schedulePoll();assert.equal(scheduled.at(-1).delay,2500);doc.hidden=true;v.schedulePoll();assert.equal(scheduled.at(-1).delay,30000);doc.hidden=false});
 await scenario('Background collection completion preserves friend draft',async()=>{const h=fakeHTTP(),v=component(h,'friends');await v.loadFriends();v.toggleFriend('Alpha Three');v.fetchingIds=['alpha'];const original=h.get;h.get=async u=>u.endsWith('/contacts/status')?{data:{fetching:false,contacts_error:null}}:original(u);await v.pollFetch();assert(v.friendsSelected.includes('Alpha Three'));assert(v.friendsDirty);assert(!v.fetchingIds.includes('alpha'))});
 await scenario('Dirty page navigation cancel retains current page',async()=>{const v=component(fakeHTTP(),'params');await v.loadConfig();v.configForm.max_friends_per_run=32;await guarded(v,'overview','alpha','cancel');assert.equal(v.page,'params');assert.equal(v.configForm.max_friends_per_run,32);assert(v.configDirty)});
 await scenario('Dirty parameters discard returns overview without writes',async()=>{const h=fakeHTTP(),v=component(h,'params');await v.loadConfig();v.configForm.max_friends_per_run=32;await guarded(v,'overview','alpha','discard');assert.equal(v.page,'overview');assert.equal(v.configDirty,false);assert.equal(h.calls.filter(c=>c.method==='put').length,0)});
 await scenario('Parameter validation keeps invalid editor without network write',async()=>{const h=fakeHTTP(),v=component(h,'params');await v.loadConfig();v.configForm.send_gap_min=8;v.configForm.send_gap_max=3;assert.equal(await v.saveConfig(),false);assert(v.configDirty);assert.equal(h.calls.filter(c=>c.method==='put').length,0)});
 await scenario('Navigation cannot replace a pending unsaved decision',async()=>{const v=component(fakeHTTP(),'friends');await v.loadFriends();v.toggleFriend('Alpha Three');const first=v.navigate('friends','beta');await tick();assert.equal(await v.navigate('overview','alpha'),false);assert.equal(v.guardVisible,true);await v.resolveUnsaved('cancel');await first;assert.equal(v.page,'friends');assert.equal(v.selectedAccountId,'alpha')});
 await scenario('Account select resets native UI value after cancel',async()=>{const v=component(fakeHTTP(),'friends');await v.loadFriends();v.toggleFriend('Alpha Three');const select={value:'beta'},p=v.requestAccountChange({target:select});await tick();await v.resolveUnsaved('cancel');await p;assert.equal(select.value,'alpha');assert.equal(v.selectedAccountId,'alpha')});
 await scenario('Appearance switch stores only motion preference',async()=>{const v=component();v.setMotion(false);assert.equal(v.motionEnabled,false);assert.equal(stored.get('spark_background_motion'),'off');v.setMotion(true);assert.equal(v.motionEnabled,true);assert.equal(stored.get('spark_background_motion'),'on')});
 await scenario('Blank numeric fields remain dirty and never become unlimited',async()=>{const h=fakeHTTP(),v=component(h,'params');await v.loadConfig();v.configForm.max_friends_per_run=0;assert.equal(await v.saveConfig(),true);const before=h.calls.filter(c=>c.method==='put').length;v.configForm.max_friends_per_run='';assert(v.configDirty);assert.equal(await v.saveConfig(),false);assert.equal(h.calls.filter(c=>c.method==='put').length,before);assert.equal(v.configForm.max_friends_per_run,'')});
 console.log('Frontend: '+results.length+' isolated regression scenarios passed');results.forEach(n=>console.log('  PASS '+n));
})().catch(e=>{console.error(e);process.exitCode=1});

