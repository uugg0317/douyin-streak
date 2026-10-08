/* Multi-account view controller. Server jobs and account editors have separate state. */
(() => {
  'use strict';
  const LABELS = {ok:'成功',partial:'部分成功',failed:'失败',logged_out:'登录失效',rate_limited:'需要验证',timeout:'超时',executor_error:'执行异常',unknown:'待人工确认',manual_required:'待人工处理',no_state:'未接入凭据',breaker_skipped:'冷却跳过',empty:'未勾选好友',success:'成功',pending:'待发送',busy:'任务繁忙'};
  const NAV = [
    {id:'overview',label:'概览',icon:'grid',description:'所有账号的状态，一眼看清。'},
    {id:'accounts',label:'账号',icon:'users',description:'管理你的账号，以及各自的运行状态。'},
    {id:'friends',label:'好友',icon:'heart',description:'为当前账号选择需要续火花的好友。',account:true},
    {id:'tasks',label:'任务',icon:'clock',description:'查看全局调度，发起演练或发送任务。'},
    {id:'credentials',label:'凭据',icon:'key',description:'管理当前账号的登录状态。',account:true},
    {id:'logs',label:'日志',icon:'terminal',description:'查看当前账号的近期运行记录。',account:true},
    {id:'params',label:'参数',icon:'sliders',description:'设置当前账号的发送数量与间隔。',account:true},
    {id:'backup',label:'备份',icon:'archive',description:'查看备份记录，保存所有账号的数据。'},
    {id:'appearance',label:'外观',icon:'sparkles',description:'让控制台保持你喜欢的状态。'},
    {id:'more',label:'更多',icon:'more',description:'凭据、日志、参数、备份与外观。'}
  ];
  const ICONS = {
    grid:'<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/>',
    users:'<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M17 4a4 4 0 0 1 0 8M22 21v-2a4 4 0 0 0-3-3.87"/><circle cx="9" cy="7" r="4"/>',
    heart:'<path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8Z"/>',
    clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    key:'<circle cx="7.5" cy="15.5" r="4.5"/><path d="m11 12 9-9 2 2-2 2 2 2-3 3-2-2-3 3"/>',
    terminal:'<path d="m4 6 6 6-6 6M13 18h7"/>',
    sliders:'<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 10h6M9 8h6M17 16h6"/>',
    archive:'<path d="M4 7v14h16V7M9 12h6"/><rect x="2" y="3" width="20" height="4" rx="1"/>',
    sparkles:'<path d="m12 3 2.3 6.7L21 12l-6.7 2.3L12 21l-2.3-6.7L3 12l6.7-2.3L12 3ZM21 2v4M19 4h4"/>',
    more:'<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
    refresh:'<path d="M20 7v-4l-4 1M4 17v4l4-1M20 7a9 9 0 0 0-15-3M4 17a9 9 0 0 0 15 3"/>',
    plus:'<path d="M12 5v14M5 12h14"/>',
    arrow:'<path d="M5 12h14m-5-5 5 5-5 5"/>',
    upload:'<path d="M12 16V3m-4 4 4-4 4 4M4 16v5h16v-5"/>',
    play:'<path d="m8 4 12 8-12 8V4Z"/>',
    check:'<path d="m5 12 4 4L19 6"/>',
    shield:'<path d="m12 3 8 4v6c0 4-8 8-8 8s-8-4-8-8V7l8-4Z"/><path d="m8 12 3 3 5-6"/>',
    fire:'<path d="M12 2c2 4 5 4 5 8 0 1-.2 2-.7 3 2-1 3-3 2.7-5 3 3 4 5 3 8a9 9 0 0 1-18 0c-1-4 1-7 4-9-1 3 0 5 2 6C8 8 10 6 12 2Z"/>'
  };
  const defaults = () => ({max_friends_per_run:20,send_gap_min:1,send_gap_max:2});
  const selectionKey = names => JSON.stringify([...new Set(names || [])].sort());
  const configKey = value => JSON.stringify(['max_friends_per_run','send_gap_min','send_gap_max'].map(k=>value[k]===''?'__empty__':value[k]==null?'__missing__':Number(value[k])));
  const EXTRACT_ACTIVE = ['starting','waiting','ready','saving','cancelling','stopping'];
  const EXTRACT_LABELS = {idle:'尚未开始',starting:'正在打开浏览器',waiting:'等待本人登录',ready:'等待核对并保存',saving:'正在保存',cancelling:'正在取消',stopping:'正在结束登录任务',success:'登录凭据已保存',cancelled:'已取消',timeout:'已超过登录期限',failed:'登录任务失败'};
  const options = {
    data(){
      let motionEnabled=true;
      try{motionEnabled=localStorage.getItem('spark_background_motion')!=='off';}catch(_){}
      return {
        ready:true,requestEpoch:0,
        page:'overview',navItems:NAV,selectedAccountId:'',selectionInitialized:false,
        accounts:[],orch:{},jobs:[],running:false,maxAccounts:10,nextRun:'',nextBackup:'',autoRunEnabled:null,backupEnabled:null,scheduleTime:'00:00',
        loading:false,loadError:'',lastUpdated:'',timer:null,fetchingIds:[],fetchingId:'',lastJobId:'',
        friendsRows:[],friendsSelected:[],friendsBaseline:'[]',friendsFilter:'',friendsLoaded:false,friendsLoading:false,friendsRequestSeq:0,savingFriends:false,
        configForm:defaults(),configBaseline:configKey(defaults()),configLoaded:false,configLoading:false,configRequestSeq:0,savingConfig:false,
        logsText:'',logsLoading:false,logsRequestSeq:0,requestVersion:0,viewError:'',
        backup:null,backupBusy:false,backupLoading:false,
        addDialog:false,addForm:{id:'',display_name:''},adding:false,
        editDialog:false,editTarget:null,editForm:{display_name:'',enabled:true},editing:false,
        copyDialog:false,copyTarget:null,copyForm:{source_id:'',copy_ledger:false},copying:false,
        uploadTarget:null,uploadBusy:false,uploadName:'',
        credentialTask:null,credentialStarting:false,credentialActionBusy:false,credentialRequestSeq:0,credentialGeneration:0,
        credentialError:'',credentialErrorSource:'',credentialNow:Date.now(),credentialTaskReceivedAt:0,credentialNoticeKey:'',credentialDismissedJobId:'',
        guardVisible:false,guardSaving:false,guardAccountName:'',guardEditorName:'',navigationBusy:false,
        motionEnabled,motionPreferenceReduced:false,motionAvailable:true
      };
    },
    computed:{
      selectedAccount(){return this.accounts.find(a=>a.id===this.selectedAccountId)||null;},
      currentNav(){return NAV.find(n=>n.id===this.page)||NAV[0];},
      desktopNav(){return NAV.filter(n=>n.id!=='more');},
      mobileNav(){return NAV.filter(n=>['overview','accounts','friends','tasks','more'].includes(n.id));},
      moreNav(){return NAV.filter(n=>['credentials','logs','params','backup','appearance'].includes(n.id));},
      lastSummary(){return this.orch.last_summary||null;},
      summaryTotals(){return this.lastSummary&&this.lastSummary.totals||{};},
      filteredFriends(){const q=this.friendsFilter.trim().toLowerCase();return this.friendsRows.filter(f=>!q||f.display_name.toLowerCase().includes(q));},
      friendsDirty(){return this.friendsLoaded&&selectionKey(this.friendsSelected)!==this.friendsBaseline;},
      configDirty(){return this.configLoaded&&configKey(this.configForm)!==this.configBaseline;},
      hasUnsaved(){return this.friendsDirty||this.configDirty;},
      readyCount(){return this.accounts.filter(a=>a.has_state).length;},
      enabledCount(){return this.accounts.filter(a=>a.enabled).length;},
      selectedFriendCount(){return this.accounts.reduce((n,a)=>n+Number(a.ledger_selected||0),0);},
      attentionCount(){return this.accounts.filter(a=>a.manual_required||this.isPaused(a)||!a.has_state).length;},
      activeJobs(){return this.jobs.filter(j=>!['done','failed'].includes(j.phase));},
      backupUsed(){return this.backup&&this.backup.slots?this.backup.slots.filter(s=>s.created_at).length:0;},
      copySources(){return this.accounts.filter(a=>!this.copyTarget||a.id!==this.copyTarget.id);},
      mobileMoreActive(){return !['overview','accounts','friends','tasks'].includes(this.page);},
      credentialActive(){return !!(this.credentialStarting||this.credentialTask&&(this.credentialTask.running||EXTRACT_ACTIVE.includes(this.credentialTask.status)));},
      credentialTaskVisible(){return !!(this.credentialTask&&this.credentialTask.status!=='idle');},
      credentialTargetMismatch(){return !!(this.credentialTask&&this.credentialTask.account_id!==this.selectedAccountId);},
      credentialRemainingSeconds(){
        if(!this.credentialTask||!this.credentialActive)return 0;
        const deadline=this.credentialDeadline(this.credentialTask);
        if(deadline)return Math.max(0,Math.ceil((deadline-this.credentialNow)/1000));
        const remaining=Number(this.credentialTask.remaining_seconds);
        return Number.isFinite(remaining)?Math.max(0,Math.ceil(remaining-(this.credentialNow-this.credentialTaskReceivedAt)/1000)):300;
      },
      credentialProgress(){return Math.max(0,Math.min(300,300-this.credentialRemainingSeconds));},
      credentialCountdown(){const s=this.credentialRemainingSeconds;return Math.floor(s/60)+':'+String(s%60).padStart(2,'0');},
      credentialReady(){return !!(this.credentialTask&&this.credentialTask.status==='ready'&&!this.credentialActionBusy&&this.credentialRemainingSeconds>0);},
      credentialStatusLabel(){return this.credentialTask?EXTRACT_LABELS[this.credentialTask.status]||'正在同步登录状态':'尚未开始';}
    },
    mounted(){
      this.http=axios.create({timeout:20000});
      this._beforeUnload=e=>{if(this.hasUnsaved||this.credentialActive){e.preventDefault();e.returnValue='';}};
      this._visibility=()=>{if(!document.hidden&&this.ready){this.load(true).then(()=>this.pollCredentialExtract());}};
      window.addEventListener('beforeunload',this._beforeUnload);
      document.addEventListener('visibilitychange',this._visibility);
      if(window.matchMedia){this._motionMedia=window.matchMedia('(prefers-reduced-motion: reduce)');this._motionPreferenceChange=()=>{this.motionPreferenceReduced=this._motionMedia.matches;};if(this._motionMedia.addEventListener)this._motionMedia.addEventListener('change',this._motionPreferenceChange);else this._motionMedia.addListener(this._motionPreferenceChange);}
      if(window.SparkMotion){window.SparkMotion.init(document.getElementById('fluid-canvas'));window.SparkMotion.setEnabled(this.motionEnabled);this.syncMotionStatus();}
      this.load().then(ok=>{if(ok){this.loadBackup();this.pollCredentialExtract();}});
      this.schedulePoll();
    },
    beforeUnmount(){
      this._stopped=true;clearTimeout(this.timer);
      this.credentialGeneration++;this.credentialRequestSeq++;
      window.removeEventListener('beforeunload',this._beforeUnload);
      document.removeEventListener('visibilitychange',this._visibility);
      if(this._motionMedia){if(this._motionMedia.removeEventListener)this._motionMedia.removeEventListener('change',this._motionPreferenceChange);else this._motionMedia.removeListener(this._motionPreferenceChange);}
      if(window.SparkMotion)window.SparkMotion.dispose();
      if(this._guardResolve)this.resolveUnsaved('cancel');
    },
    methods:{
      icon(name){return `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]||ICONS.grid}</svg>`;},
      fmt(t){if(!t)return'—';const value=typeof t==='number'&&t<1e12?t*1000:t;const d=new Date(value);return Number.isNaN(d.getTime())?'—':d.toLocaleString('zh-CN',{hour12:false});},
      statusText(s){return LABELS[s]||'待运行';},
      errDetail(e){const d=e&&e.response&&e.response.data&&e.response.data.detail;return typeof d==='string'?d:e&&e.message||'请求失败，请重试';},
      notify(kind,message){ElementPlus.ElMessage[kind](message);},
      handleRequestError(){return false;},
      schedulePoll(){
        const delay=document.hidden?30000:this.running||this.fetchingIds.length||this.fetchingId||this.credentialActive?2500:10000;
        this.timer=setTimeout(async()=>{this.credentialNow=Date.now();if(this.ready===true){await this.load(true);await this.pollFetch();await this.pollCredentialExtract();}if(!this._stopped)this.schedulePoll();},delay);
      },
      async load(silent=false){
        if(this._loadPromise)return this._loadPromise;
        const epoch=this.requestEpoch;
        if(!silent)this.loading=true;
        this._loadPromise=(async()=>{
          try{
            const {data}=await this.http.get('/api/multi/state');
            if(epoch!==this.requestEpoch)return false;
            this.ready=true;this.accounts=data.accounts||[];this.orch=data.state||{};this.jobs=data.jobs||[];
            this.maxAccounts=data.max_accounts||10;this.nextRun=data.next_run||'';this.nextBackup=data.next_backup||'';
            this.autoRunEnabled=typeof data.auto_run_enabled==='boolean'?data.auto_run_enabled:null;
            this.backupEnabled=typeof data.backup_enabled==='boolean'?data.backup_enabled:null;
            this.scheduleTime=data.schedule_time||'00:00';
            this.running=!!this.orch.running||this.jobs.some(j=>j.kind==='send');
            for(const j of this.jobs){if(j.kind==='contacts'&&!this.fetchingIds.includes(j.account_id))this.fetchingIds.push(j.account_id);}
            this.fetchingId=this.fetchingIds[0]||'';
            if(!this.selectionInitialized&&this.accounts.length){this.selectedAccountId=this.accounts[0].id;this.selectionInitialized=true;}
            else if(this.selectedAccountId&&!this.accounts.some(a=>a.id===this.selectedAccountId)){
              this.selectedAccountId='';this.page='overview';this.clearAccountView();this.selectionInitialized=true;
              if(this._guardResolve)this.resolveUnsaved('cancel');
              this.notify('warning','原账号已移除，已返回概览，请重新选择账号');
            }
            this.lastUpdated=new Date().toISOString();this.loadError='';return true;
          }catch(e){if(epoch!==this.requestEpoch)return false;if(!this.handleRequestError(e,epoch))this.loadError='状态暂时无法更新：'+this.errDetail(e);return false;}
          finally{this.loading=false;this._loadPromise=null;}
        })();
        return this._loadPromise;
      },
      async refreshCurrent(){
        if(!await this.checkUnsaved())return;
        await this.load();await this.loadPage();
      },
      clearAccountView(){
        this.requestVersion++;this.friendsRequestSeq++;this.configRequestSeq++;this.logsRequestSeq++;
        this.friendsRows=[];this.friendsSelected=[];this.friendsBaseline='[]';this.friendsFilter='';this.friendsLoaded=false;this.friendsLoading=false;
        this.configForm=defaults();this.configBaseline=configKey(this.configForm);this.configLoaded=false;this.configLoading=false;
        this.logsText='';this.logsLoading=false;this.viewError='';
      },
      async requestAccountChange(event){const select=event.target;await this.selectAccount(select.value);select.value=this.selectedAccountId;},
      async selectAccount(id){return this.navigate(this.page,id);},
      async navigate(page,accountId=this.selectedAccountId){
        if(!NAV.some(n=>n.id===page))return false;
        if(page===this.page&&accountId===this.selectedAccountId)return true;
        if(this.navigationBusy||this.savingFriends||this.savingConfig)return false;
        this.navigationBusy=true;
        let viewVersion=null;
        try{
          if(!await this.checkUnsaved())return false;
          if(accountId&&!this.accounts.some(a=>a.id===accountId)){this.notify('warning','这个账号已移除，请重新选择');return false;}
          this.clearAccountView();this.selectedAccountId=accountId||'';this.page=page;
          viewVersion=this.requestVersion;this.navigationBusy=false;
          await this.loadPage();return true;
        }finally{if(viewVersion===null||viewVersion===this.requestVersion)this.navigationBusy=false;}
      },
      async loadPage(){if(this.page==='friends'&&this.selectedAccount)return this.loadFriends();if(this.page==='params'&&this.selectedAccount)return this.loadConfig();if(this.page==='logs'&&this.selectedAccount)return this.refreshLogs();if(this.page==='backup')return this.loadBackup();if(this.page==='credentials')return this.pollCredentialExtract();return true;},
      askUnsaved(){if(this._guardResolve)return Promise.resolve('cancel');this.guardAccountName=this.selectedAccount?this.selectedAccount.display_name:'当前账号';this.guardEditorName=this.friendsDirty?'好友选择':'发送参数';this.guardVisible=true;return new Promise(resolve=>{this._guardResolve=resolve;});},
      async resolveUnsaved(choice){
        if(choice==='save'){
          if(this.guardSaving)return;
          this.guardSaving=true;
          try{if(!await this.saveActiveEdits())return;}finally{this.guardSaving=false;}
        }
        const resolve=this._guardResolve;this._guardResolve=null;this.guardVisible=false;
        if(resolve)resolve(choice);
      },
      closeGuard(done){if(this.guardSaving)return;this.resolveUnsaved('cancel');if(done)done();},
      async checkUnsaved(){
        if(!this.hasUnsaved)return true;
        const choice=await this.askUnsaved();
        if(choice==='cancel')return false;
        if(choice==='discard'){this.friendsSelected=JSON.parse(this.friendsBaseline);this.configForm={...this.configForm,...Object.fromEntries(['max_friends_per_run','send_gap_min','send_gap_max'].map((k,i)=>[k,JSON.parse(this.configBaseline)[i]]))};}
        return !this.hasUnsaved;
      },
      async saveActiveEdits(){if(this.friendsDirty&&!await this.saveFriends())return false;if(this.configDirty&&!await this.saveConfig())return false;return true;},
      async openFriends(a){return this.navigate('friends',a.id);},
      async openConfig(a){return this.navigate('params',a.id);},
      async openLogs(a){return this.navigate('logs',a.id);},
      async loadFriends(){
        const aid=this.selectedAccountId,version=this.requestVersion,seq=++this.friendsRequestSeq,epoch=this.requestEpoch;
        if(!aid)return false;this.friendsLoading=true;this.viewError='';
        try{
          const {data}=await this.http.get('/api/multi/accounts/'+aid+'/ledger');
          if(aid!==this.selectedAccountId||version!==this.requestVersion||seq!==this.friendsRequestSeq||this.page!=='friends'||epoch!==this.requestEpoch)return false;
          this.friendsRows=(data.entries||[]).map(e=>({...e,display_name:e.display_name||e.nickname||'',last_status:e.last_status||'pending'}));
          this.friendsSelected=this.friendsRows.filter(f=>f.selected).map(f=>f.display_name);
          this.friendsBaseline=selectionKey(this.friendsSelected);this.friendsLoaded=true;return true;
        }catch(e){if(aid===this.selectedAccountId&&version===this.requestVersion&&seq===this.friendsRequestSeq){this.handleRequestError(e,epoch);this.viewError='好友列表加载失败：'+this.errDetail(e);}return false;}
        finally{if(seq===this.friendsRequestSeq)this.friendsLoading=false;}
      },
      toggleFriend(name){if(this.savingFriends||!this.friendsLoaded)return;const i=this.friendsSelected.indexOf(name);if(i>=0)this.friendsSelected.splice(i,1);else this.friendsSelected.push(name);},
      selectFiltered(on){if(this.savingFriends||!this.friendsLoaded)return;const names=this.filteredFriends.map(f=>f.display_name);if(on)this.friendsSelected=[...new Set(this.friendsSelected.concat(names))];else{const drop=new Set(names);this.friendsSelected=this.friendsSelected.filter(n=>!drop.has(n));}},
      async saveFriends(){
        if(!this.friendsLoaded||!this.selectedAccountId||this.savingFriends||this.accountBusy(this.selectedAccount))return false;
        if(!this.friendsSelected.length){this.notify('warning','至少选择一位好友后再保存');return false;}
        const aid=this.selectedAccountId,version=this.requestVersion,names=[...this.friendsSelected];this.savingFriends=true;
        try{await this.http.post('/api/multi/accounts/'+aid+'/ledger/selection',{selected_names:names});if(aid===this.selectedAccountId&&version===this.requestVersion)this.friendsBaseline=selectionKey(names);this.notify('success','好友选择已保存');await this.load(true);return true;}
        catch(e){this.handleRequestError(e);this.notify('error','保存失败，修改已保留：'+this.errDetail(e));return false;}
        finally{this.savingFriends=false;}
      },
      async loadConfig(){
        const aid=this.selectedAccountId,version=this.requestVersion,seq=++this.configRequestSeq,epoch=this.requestEpoch;
        if(!aid)return false;this.configLoading=true;this.viewError='';
        try{const {data}=await this.http.get('/api/multi/accounts/'+aid+'/config');if(aid!==this.selectedAccountId||version!==this.requestVersion||seq!==this.configRequestSeq||this.page!=='params'||epoch!==this.requestEpoch)return false;const c=data.config||{};this.configForm={...defaults(),...c};this.configBaseline=configKey(this.configForm);this.configLoaded=true;return true;}
        catch(e){if(aid===this.selectedAccountId&&version===this.requestVersion&&seq===this.configRequestSeq){this.handleRequestError(e,epoch);this.viewError='参数加载失败：'+this.errDetail(e);}return false;}
        finally{if(seq===this.configRequestSeq)this.configLoading=false;}
      },
      async saveConfig(){
        if(!this.configLoaded||!this.selectedAccountId||this.savingConfig||this.accountBusy(this.selectedAccount))return false;
        if(['max_friends_per_run','send_gap_min','send_gap_max'].some(k=>this.configForm[k]===''||this.configForm[k]==null)){this.notify('warning','请填写完整的好友数量与发送间隔');return false;}
        const config=Object.fromEntries(['max_friends_per_run','send_gap_min','send_gap_max'].map(k=>[k,Number(this.configForm[k])]));
        if(Object.values(config).some(v=>!Number.isFinite(v))||!Number.isInteger(config.max_friends_per_run)||config.max_friends_per_run<0||config.max_friends_per_run>500||config.send_gap_min<0||config.send_gap_max<config.send_gap_min||config.send_gap_max>60){this.notify('warning','请检查数量和间隔；最大间隔应不小于最小间隔');return false;}
        const aid=this.selectedAccountId,version=this.requestVersion;this.savingConfig=true;
        try{const {data}=await this.http.put('/api/multi/accounts/'+aid+'/config',{config});if(aid===this.selectedAccountId&&version===this.requestVersion){this.configForm={...this.configForm,...config,...(data.config||{})};this.configBaseline=configKey(this.configForm);}this.notify('success','发送参数已保存');await this.load(true);return true;}
        catch(e){this.handleRequestError(e);this.notify('error','保存失败，修改已保留：'+this.errDetail(e));return false;}
        finally{this.savingConfig=false;}
      },
      async refreshLogs(){
        const aid=this.selectedAccountId,version=this.requestVersion,seq=++this.logsRequestSeq,epoch=this.requestEpoch;
        if(!aid)return false;this.logsLoading=true;this.viewError='';
        try{const {data}=await this.http.get('/api/multi/accounts/'+aid+'/logs',{params:{n:400}});if(aid!==this.selectedAccountId||version!==this.requestVersion||seq!==this.logsRequestSeq||this.page!=='logs'||epoch!==this.requestEpoch)return false;this.logsText=data.logs||'暂无日志。任务执行后，记录会显示在这里。';return true;}
        catch(e){if(aid===this.selectedAccountId&&version===this.requestVersion&&seq===this.logsRequestSeq){this.handleRequestError(e,epoch);this.viewError='日志加载失败：'+this.errDetail(e);}return false;}
        finally{if(seq===this.logsRequestSeq)this.logsLoading=false;}
      },
      async createAccount(){
        if(this.adding||this.credentialActive)return;const body={display_name:this.addForm.display_name.trim()};if(this.addForm.id.trim())body.id=this.addForm.id.trim();
        if(body.id&&!/^[a-z0-9_-]{3,20}$/.test(body.id)){this.notify('warning','账号 ID 需为 3–20 位小写字母、数字、下划线或连字符');return;}
        this.adding=true;try{const {data}=await this.http.post('/api/multi/accounts',body);this.addDialog=false;this.addForm={id:'',display_name:''};await this.load(true);if(data.account&&!this.hasUnsaved)await this.navigate('accounts',data.account.id);this.notify('success','账号已添加');}catch(e){this.handleRequestError(e);this.notify('error',this.errDetail(e));}finally{this.adding=false;}
      },
      openEdit(a){if(!a)return;this.editTarget={...a};this.editForm={display_name:a.display_name,enabled:!!a.enabled};this.editDialog=true;},
      async saveEdit(){if(!this.editTarget||this.editing||this.accountBusy(this.editTarget))return;this.editing=true;try{await this.http.patch('/api/multi/accounts/'+this.editTarget.id,{display_name:this.editForm.display_name.trim(),enabled:this.editForm.enabled});this.editDialog=false;await this.load(true);this.notify('success','账号设置已保存');}catch(e){this.handleRequestError(e);this.notify('error',this.errDetail(e));}finally{this.editing=false;}},
      async toggleEnabled(a,value){if(!a||this.accountBusy(a))return;try{await this.http.patch('/api/multi/accounts/'+a.id,{enabled:!!value});await this.load(true);}catch(e){this.handleRequestError(e);this.notify('error',this.errDetail(e));}},
      async removeAccount(a){
        if(!a||this.accountBusy(a))return;
        if(a.id===this.selectedAccountId&&!await this.checkUnsaved())return;
        try{await ElementPlus.ElMessageBox.confirm('从列表移除「'+a.display_name+'」？账号数据会保留，可以用原 ID 重新添加。','移除账号',{confirmButtonText:'移除',cancelButtonText:'取消',type:'warning'});await this.http.post('/api/multi/accounts/'+a.id+'/remove',{delete_dir:false});await this.load(true);this.notify('success','账号已从列表移除');}catch(e){if(e!=='cancel'&&e!=='close'){this.handleRequestError(e);this.notify('error',this.errDetail(e));}}
      },
      uploadState(a=this.selectedAccount){if(!a||this.uploadBusy||this.accountBusy(a))return;this.uploadTarget={id:a.id,display_name:a.display_name};this.$refs.fileInput.value='';this.$refs.fileInput.click();},
      async onFilePicked(event){
        const file=event.target.files&&event.target.files[0],target=this.uploadTarget;this.uploadTarget=null;if(!file||!target||this.uploadBusy)return;
        if(this.accountBusy(target)){this.notify('warning','这个账号正在执行任务，请完成或取消任务后再上传凭据');event.target.value='';return;}
        if(file.size>5*1024*1024){this.notify('warning','凭据文件不能超过 5 MB');return;}
        const body=new FormData();body.append('file',file);this.uploadBusy=true;this.uploadName=target.display_name;
        try{await this.http.post('/api/multi/accounts/'+target.id+'/state',body);this.notify('success','凭据已更新：'+target.display_name);await this.load(true);}catch(e){this.handleRequestError(e);this.notify('error','上传失败：'+this.errDetail(e));}finally{this.uploadBusy=false;this.uploadName='';event.target.value='';}
      },
      credentialDeadline(task){
        const value=task&&task.deadline;
        if(typeof value==='number'&&Number.isFinite(value)&&value>0)return value<1e12?value*1000:value;
        if(typeof value==='string'&&value){const numeric=Number(value);if(Number.isFinite(numeric)&&numeric>0)return numeric<1e12?numeric*1000:numeric;const parsed=Date.parse(value);if(Number.isFinite(parsed))return parsed;}
        return 0;
      },
      applyCredentialTask(data,fallback=null){
        const raw=data&&typeof data==='object'?data:{};
        const task={...fallback,...raw};
        if(fallback&&fallback.account_id){task.account_id=fallback.account_id;task.display_name=fallback.display_name||raw.display_name;}
        if(fallback&&fallback.job_id)task.job_id=fallback.job_id;
        if(!task.status)task.status=task.running?'waiting':'idle';
        if(task.job_id===this.credentialDismissedJobId&&!task.running&&!EXTRACT_ACTIVE.includes(task.status)){this.credentialTask=null;return;}
        if(task.status==='idle'){this.credentialTask=null;if(this.credentialErrorSource==='poll'){this.credentialError='';this.credentialErrorSource='';}return;}
        this.credentialTask={job_id:task.job_id||null,account_id:task.account_id||null,display_name:task.display_name||fallback&&fallback.display_name||'目标账号',status:task.status,running:typeof task.running==='boolean'?task.running:EXTRACT_ACTIVE.includes(task.status),count:Math.max(0,Number(task.count)||0),started_at:task.started_at||null,deadline:task.deadline||null,remaining_seconds:task.remaining_seconds,error:typeof task.error==='string'?task.error:''};
        this.credentialNow=Date.now();this.credentialTaskReceivedAt=this.credentialNow;
        if(this.credentialErrorSource==='poll'||task.status==='success'){this.credentialError='';this.credentialErrorSource='';}
        if(task.error){this.credentialError=this.credentialTask.error;this.credentialErrorSource='task';}
      },
      async startCredentialExtract(a=this.selectedAccount){
        if(this.ready!==true||!a||this.credentialStarting||this.credentialActionBusy||this.credentialActive||this.uploadBusy||this.accountBusy(a))return false;
        const target={account_id:a.id,display_name:a.display_name},epoch=this.requestEpoch;
        if(!await this.checkUnsaved()||epoch!==this.requestEpoch||this.ready!==true)return false;
        if(this.credentialStarting||this.credentialActionBusy||this.credentialActive||this.uploadBusy||this.accountBusy(a))return false;
        const generation=++this.credentialGeneration;this.credentialRequestSeq++;
        this.credentialStarting=true;this.credentialError='';this.credentialErrorSource='';this.credentialNoticeKey='';this.credentialDismissedJobId='';this.credentialNow=Date.now();
        this.credentialTask={...target,job_id:null,status:'starting',running:true,deadline:this.credentialNow+300000,count:0,error:''};
        try{
          const {data}=await this.http.post('/api/multi/accounts/'+encodeURIComponent(target.account_id)+'/credentials/extract',{},{timeout:60000});
          if(epoch!==this.requestEpoch||generation!==this.credentialGeneration||this.ready!==true)return false;
          if(!data||!data.job_id||data.account_id&&data.account_id!==target.account_id)throw new Error('登录任务响应异常，请刷新状态后重试');
          this.applyCredentialTask(data,target);this.notify('info','已为「'+target.display_name+'」打开服务电脑上的登录浏览器');await this.load(true);return true;
        }catch(e){
          if(epoch!==this.requestEpoch||generation!==this.credentialGeneration||this.ready!==true)return false;
          this.handleRequestError(e,epoch);this.credentialError=(e&&e.response?'浏览器登录暂时未能开始：':'登录启动结果暂时无法确认，请刷新状态：')+this.errDetail(e);this.credentialErrorSource='action';
          this.credentialTask={...target,job_id:null,status:'failed',running:false,count:0,error:'',deadline:null};return false;
        }finally{if(epoch===this.requestEpoch&&generation===this.credentialGeneration)this.credentialStarting=false;}
      },
      async pollCredentialExtract(){
        if(this.ready!==true||this.credentialStarting||this.credentialActionBusy)return false;
        const epoch=this.requestEpoch,generation=this.credentialGeneration,seq=++this.credentialRequestSeq,expectedJob=this.credentialTask&&this.credentialTask.job_id;
        try{
          const {data}=await this.http.get('/api/multi/credentials/extract-status',{timeout:40000});
          if(epoch!==this.requestEpoch||generation!==this.credentialGeneration||seq!==this.credentialRequestSeq||this.ready!==true||expectedJob!==(this.credentialTask&&this.credentialTask.job_id))return false;
          const snapshot=data&&typeof data==='object'?data:{status:'idle',running:false};
          if(expectedJob&&snapshot.job_id&&snapshot.job_id!==expectedJob){
            const incomingStarted=this.credentialDeadline({deadline:snapshot.started_at}),currentStarted=this.credentialDeadline({deadline:this.credentialTask.started_at});
            if(!incomingStarted||incomingStarted<=currentStarted)return false;
            this.credentialGeneration++;this.credentialRequestSeq++;
            this.credentialError='';this.credentialErrorSource='';this.applyCredentialTask(snapshot);this.announceCredentialResult();return true;
          }
          if(expectedJob&&snapshot.account_id&&snapshot.account_id!==this.credentialTask.account_id)return false;
          if(expectedJob&&snapshot.status==='idle')this.applyCredentialTask({status:'failed',running:false,error:'登录任务已结束或服务已重启，请重新发起。'},this.credentialTask);
          else this.applyCredentialTask(snapshot,expectedJob?this.credentialTask:null);
          this.announceCredentialResult();return true;
        }catch(e){
          if(epoch!==this.requestEpoch||generation!==this.credentialGeneration||seq!==this.credentialRequestSeq||this.ready!==true)return false;
          if(!this.handleRequestError(e,epoch)&&this.credentialActive){this.credentialError='登录状态暂时无法更新，请刷新重试；原凭据保持不变。';this.credentialErrorSource='poll';}return false;
        }
      },
      announceCredentialResult(){
        const task=this.credentialTask;if(!task||this.credentialActive||!task.job_id)return;
        const key=task.job_id+':'+task.status;if(key===this.credentialNoticeKey)return;this.credentialNoticeKey=key;
        if(task.status==='success')this.notify('success','登录凭据已保存到「'+task.display_name+'」');
        else if(task.status==='timeout')this.notify('warning','「'+task.display_name+'」的浏览器登录已超时，原凭据未更改');
        else if(task.status==='cancelled')this.notify('info','已取消「'+task.display_name+'」的登录任务，原凭据未更改');
      },
      async confirmCredentialExtract(){
        const task=this.credentialTask;if(this.ready!==true||!task||!task.job_id||task.status!=='ready'||this.credentialActionBusy)return false;
        const deadline=this.credentialDeadline(task);if(deadline&&deadline<=Date.now()){this.credentialError='已超过五分钟登录期限，请刷新任务状态后重新发起。';this.credentialErrorSource='action';await this.pollCredentialExtract();return false;}
        return this.actCredentialExtract('confirm');
      },
      async cancelCredentialExtract(){
        if(this.ready!==true||!this.credentialTask||!this.credentialTask.job_id||!this.credentialActive||this.credentialActionBusy)return false;
        return this.actCredentialExtract('cancel');
      },
      async actCredentialExtract(action){
        const task={...this.credentialTask},epoch=this.requestEpoch,generation=++this.credentialGeneration;this.credentialRequestSeq++;
        this.credentialActionBusy=true;this.credentialError='';this.credentialErrorSource='';this.credentialTask={...task,status:action==='confirm'?'saving':'cancelling',running:true};
        try{
          const {data}=await this.http.post('/api/multi/accounts/'+encodeURIComponent(task.account_id)+'/credentials/extract/'+encodeURIComponent(task.job_id)+'/'+action,{},{timeout:60000});
          if(epoch!==this.requestEpoch||generation!==this.credentialGeneration||this.ready!==true||!this.credentialTask||this.credentialTask.job_id!==task.job_id)return false;
          if(data&&(data.job_id&&data.job_id!==task.job_id||data.account_id&&data.account_id!==task.account_id))throw new Error('任务响应不一致，请刷新状态后重试');
          this.applyCredentialTask(data&&data.status?data:{status:action==='confirm'?'success':'cancelled',running:false},task);this.announceCredentialResult();await this.load(true);return true;
        }catch(e){
          if(epoch!==this.requestEpoch||generation!==this.credentialGeneration||this.ready!==true)return false;
          this.handleRequestError(e,epoch);this.credentialTask=task;this.credentialError=(action==='confirm'?(e&&e.response?'保存失败，原凭据已保留，请刷新状态后重试：':'保存结果暂时无法确认，请刷新登录状态后再操作：'):(e&&e.response?'取消暂未完成，请重试：':'取消结果暂时无法确认，请刷新登录状态：'))+this.errDetail(e);this.credentialErrorSource='action';return false;
        }finally{if(epoch===this.requestEpoch&&generation===this.credentialGeneration)this.credentialActionBusy=false;}
      },
      dismissCredentialTask(){if(this.credentialActive||this.credentialActionBusy)return;this.credentialDismissedJobId=this.credentialTask&&this.credentialTask.job_id||'';this.credentialRequestSeq++;this.credentialTask=null;this.credentialError='';this.credentialErrorSource='';},
      async openCredentialTask(){return this.navigate('credentials',this.credentialTask&&this.accounts.some(a=>a.id===this.credentialTask.account_id)?this.credentialTask.account_id:this.selectedAccountId);},
      async fetchContacts(a=this.selectedAccount){if(!a||this.accountBusy(a))return;if(!await this.checkUnsaved())return;try{const {data}=await this.http.post('/api/multi/accounts/'+a.id+'/contacts/fetch');this.lastJobId=data.job_id||'';if(!this.fetchingIds.includes(a.id))this.fetchingIds.push(a.id);this.fetchingId=this.fetchingIds[0]||'';this.notify('info','正在采集「'+a.display_name+'」的好友，可以继续查看其他账号');await this.load(true);}catch(e){this.handleRequestError(e);this.notify('error',this.errDetail(e));}},
      async pollFetch(){
        const ids=[...new Set(this.fetchingIds.concat(this.fetchingId?[this.fetchingId]:[]))];
        for(const aid of ids){try{const {data}=await this.http.get('/api/multi/accounts/'+aid+'/contacts/status');if(!data.fetching){this.fetchingIds=this.fetchingIds.filter(id=>id!==aid);this.fetchingId=this.fetchingIds[0]||'';const a=this.accounts.find(a=>a.id===aid);this.notify(data.contacts_error?'error':'success',(a?a.display_name:'账号')+'：'+(data.contacts_error||'好友采集完成'));if(this.page==='friends'&&this.selectedAccountId===aid&&!this.friendsDirty)await this.loadFriends();await this.load(true);}}catch(e){this.handleRequestError(e);}}
      },
      openCopy(a=this.selectedAccount){if(!a)return;this.copyTarget={...a};const source=this.accounts.find(s=>s.id!==a.id);this.copyForm={source_id:source?source.id:'',copy_ledger:false};this.copyDialog=true;},
      async saveCopy(){
        if(!this.copyTarget||!this.copyForm.source_id||this.copying||this.accountBusy(this.copyTarget)){if(!this.copyForm.source_id)this.notify('warning','请选择源账号');return;}
        if(this.copyTarget.id===this.selectedAccountId&&!await this.checkUnsaved())return;
        this.copying=true;try{await this.http.post('/api/multi/accounts/'+this.copyTarget.id+'/copy-from',{...this.copyForm});this.copyDialog=false;await this.load(true);await this.loadPage();this.notify('success','配置已复制');}catch(e){this.handleRequestError(e);this.notify('error',this.errDetail(e));}finally{this.copying=false;}
      },
      async loadBackup(){this.backupLoading=true;try{const {data}=await this.http.get('/api/multi/backups');this.backup=data;return true;}catch(e){this.handleRequestError(e);if(this.page==='backup')this.viewError='备份记录加载失败：'+this.errDetail(e);return false;}finally{this.backupLoading=false;}},
      async runBackup(){if(this.backupBusy||this.credentialActive)return;this.backupBusy=true;try{const {data}=await this.http.post('/api/multi/backups/run');this.notify('success','备份完成，已保存 '+(data.files||0)+' 个文件');await this.loadBackup();}catch(e){this.handleRequestError(e);this.notify('error','备份失败：'+this.errDetail(e));}finally{this.backupBusy=false;}},
      async confirmReal(text){try{await ElementPlus.ElMessageBox.confirm(text,'真实发送确认',{confirmButtonText:'确认发送',cancelButtonText:'取消',type:'warning'});return true;}catch(_){return false;}},
      async runAll(dry){if(this.credentialActive||!await this.checkUnsaved())return;if(!dry&&!await this.confirmReal('向全部启用账号的已保存好友名单串行发送，确认继续？'))return;if(this.credentialActive)return;try{const {data}=await this.http.post('/api/multi/run',{dry_run:!!dry});this.lastJobId=data.job_id||'';this.notify('success',dry?'全部账号演练已启动':'全部账号发送已启动');await this.load(true);}catch(e){this.handleRequestError(e);this.notify('error',this.errDetail(e));}},
      async runOne(a=this.selectedAccount,dry=true){if(!a||this.accountBusy(a)||!await this.checkUnsaved())return;if(!dry&&!await this.confirmReal('向「'+a.display_name+'」的已保存好友名单真实发送，确认继续？'))return;if(this.accountBusy(a))return;try{const {data}=await this.http.post('/api/multi/run',{account_id:a.id,dry_run:!!dry});this.lastJobId=data.job_id||'';this.notify('success',(dry?'演练':'发送')+'已启动：'+a.display_name);await this.load(true);}catch(e){this.handleRequestError(e);this.notify('error',this.errDetail(e));}},
      async reviewAccount(a=this.selectedAccount){if(!a||this.accountBusy(a))return;try{await ElementPlus.ElMessageBox.confirm('请先在抖音中核对「'+a.display_name+'」的安全状态和待确认消息。恢复任务后，当天成功和待确认联系人仍会跳过。','人工核对后恢复',{confirmButtonText:'我已核对，恢复任务',cancelButtonText:'取消',type:'warning'});if(this.accountBusy(a))return;await this.http.post('/api/multi/accounts/'+a.id+'/review',{confirmed:true});await this.load(true);this.notify('success','账号任务已恢复，当天待确认联系人仍会跳过');}catch(e){if(e!=='cancel'&&e!=='close'){this.handleRequestError(e);this.notify('error',this.errDetail(e));}}},
      async resetOrch(){try{await this.http.post('/api/multi/reset');await this.load(true);this.notify('success','残留运行状态已复位');}catch(e){this.handleRequestError(e);this.notify('error',this.errDetail(e));}},
      syncMotionStatus(){if(!window.SparkMotion)return;const s=window.SparkMotion.status();this.motionPreferenceReduced=!!s.reducedMotion;this.motionAvailable=!!s.webgl;},
      setMotion(value){this.motionEnabled=!!value;try{localStorage.setItem('spark_background_motion',this.motionEnabled?'on':'off');}catch(_){}if(window.SparkMotion){window.SparkMotion.setEnabled(this.motionEnabled);this.syncMotionStatus();}},
      isPaused(a){return !!(a&&a.auto_paused_until&&a.auto_paused_until*1000>Date.now());},
      accountBusy(a){return !!(a&&(this.credentialActive||this.jobs.some(j=>j.global_scope||j.account_id===a.id)));},
      acctTagType(a){if(!a)return'info';if(a.manual_required||this.isPaused(a))return'warning';if(!a.has_state)return'warning';if(a.running||this.accountBusy(a))return'primary';const l=a.last_run;if(l&&(l.logged_out||l.failed&&!l.ok))return'danger';if(l&&(l.rate_limited||l.unknown||l.failed))return'warning';return l&&l.ok?'success':'info';},
      acctTagText(a){if(!a)return'未选择';if(a.manual_required)return'待人工处理';if(this.isPaused(a))return'冷却中';if(!a.has_state)return'未接入';if(a.running||this.accountBusy(a))return'运行中';const l=a.last_run;if(!l)return'待运行';if(l.logged_out)return'登录失效';if(l.unknown)return'待确认';if(l.rate_limited)return'需要验证';if(l.failed)return l.ok?'部分失败':'失败';return l.ok?(l.dry_run?'演练正常':'正常'):'待运行';},
      lastRunText(a){const l=a&&a.last_run;if(!l)return'还没有运行记录';if(l.logged_out)return'登录状态已失效';const out=[];if(l.ok)out.push('成功 '+l.ok);if(l.failed)out.push('失败 '+l.failed);if(l.unknown)out.push('待确认 '+l.unknown);if(l.skipped)out.push('跳过 '+l.skipped);return(l.dry_run?'演练 · ':'')+(out.join(' · ')||'本轮没有发送');},
      jobAccount(j){if(['credential_extract','credentials'].includes(j.kind))return (this.accounts.find(a=>a.id===j.account_id)||{}).display_name||this.credentialTask&&this.credentialTask.account_id===j.account_id&&this.credentialTask.display_name||'登录目标账号';return j.global_scope?'全部账号':(this.accounts.find(a=>a.id===j.account_id)||{}).display_name||'账号任务';},
      jobKind(j){return({send:'发送',contacts:'好友采集',backup:'备份',state:'凭据更新',credential_extract:'浏览器登录',credentials:'浏览器登录',review:'人工复核',config:'参数更新',copy:'复制配置'})[j.kind]||'账号管理';}
    }
  };
  window.SparkMultiAppOptions=options;
  Vue.createApp(options).use(ElementPlus).mount('#app');
})();
