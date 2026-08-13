(function () {
  'use strict';

  const EXPERIMENT_LABELS = {
    DRAFT:'草稿',CREATIVE_GENERATING:'素材生成中',CREATIVE_REVIEW:'素材审核',CREATIVE_REJECTED:'素材驳回',
    WAITING_CREATE_APPROVAL:'待确认',CREATING_PAUSED_OBJECTS:'等待创建',CREATION_PARTIAL_FAILURE:'部分创建待核验',
    META_REVIEW_PENDING:'Meta 审核中',READY_FOR_ACTIVATION:'等待启用',RUNNING:'投放观察',MATURING:'样本成熟中',
    RECOMMENDATION_READY:'建议已生成',WAITING_ADJUSTMENT_APPROVAL:'等待调整确认',ADJUSTING:'调整中',
    EVALUATING_ADJUSTMENT:'效果评价中',EFFECTIVE:'调整有效',INEFFECTIVE:'调整无效',INCONCLUSIVE:'证据不足',
    DATA_INCOMPLETE:'数据不完整',MIXED_CHANGE:'混合调整',PAUSED:'已暂停',ARCHIVED:'已完成'
  };
  const EPISODE_LABELS = {CREATED:'待执行',ACTION_EXECUTING:'执行中',WAITING_OUTCOME:'等待结果',OUTCOME_READY:'结果已回收',LESSON_REVIEW:'经验复盘',COMPLETED:'已完成'};
  const KNOWLEDGE_LABELS = {RAW:'待审核',REVIEWED:'已审核',ACTIVE:'生效中',ARCHIVED:'已归档'};
  const EXECUTION_LABELS = {QUEUED:'广告尚未创建',RUNNING:'正在创建',VERIFYING:'正在核对创建结果',SUCCESS:'暂停态广告已创建',MANUAL_REVIEW:'需要人工核对'};
  const RECEIPT_STEP_LABELS = {IMAGE_UPLOAD:'准备广告图片',CAMPAIGN_CREATE:'创建广告系列',CREATIVE_CREATE:'创建广告素材',ADSET_CREATE:'创建广告组',AD_CREATE:'创建广告',VERIFY:'核对 Meta 实际状态',RECEIPT:'保存最终结果'};
  const RECEIPT_STATUS_LABELS = {SUCCESS:'已完成',VERIFIED:'已核对',UNKNOWN:'结果待确认',FAILED:'失败'};
  const LOW_RISK_AUTOMATIC_ACTIONS = ['OBSERVE', 'CHECK_DATA'];
  const STAGES = ['方案','确认','演练','观察','复盘','下一轮'];
  const LAUNCH_PROGRESS_STORAGE_KEY = 'growth-new-account-launch-progress-v2';
  const LEGACY_LAUNCH_PROGRESS_STORAGE_KEY = 'growth-new-account-launch-progress-v1';
  const LAUNCH_ORDERS_CACHE_KEY = 'growth-launch-orders-cache-v1';
  const BULK_REBUILD_STORAGE_KEY = 'growth-bulk-rebuild-approval-v1';
  const state = {experiments:[],activeExperiment:'',workBucket:'action_required',taskSearch:'',detail:null,postKeys:{},createStep:1,createSource:'recommendation',workspaceReturn:null,coverageScope:new Set()};

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
  }

  function pretty(value) {
    if (value == null || value === '' || (typeof value === 'object' && !Object.keys(value).length)) return '暂无记录';
    return JSON.stringify(value, null, 2);
  }

  function statusLabel(value) { return EXPERIMENT_LABELS[value] || EPISODE_LABELS[value] || KNOWLEDGE_LABELS[value] || EXECUTION_LABELS[value] || value || '待记录'; }

  function stableJson(value) {
    if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
    if (value && typeof value === 'object') return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(',')}}`;
    return JSON.stringify(value);
  }

  function stableHash(value) {
    let hash = 2166136261;
    for (const char of stableJson(value)) { hash ^= char.charCodeAt(0); hash = Math.imul(hash, 16777619); }
    return (hash >>> 0).toString(36);
  }

  function postHeaders(scope, requestIdentity) {
    const signature = `${scope}:${stableHash(requestIdentity)}`;
    if (!state.postKeys[signature]) state.postKeys[signature] = `growth-ui-${signature}`;
    return {'Content-Type':'application/json','Idempotency-Key':state.postKeys[signature],'X-Request-ID':`growth-ui-request-${stableHash(signature)}`};
  }

  function bulkRebuildHeaders(batchId, recommendationId, phase) {
    const identity=`gle-bulk-rebuild:${batchId}:${recommendationId}:${phase}`;
    return {'Content-Type':'application/json','Idempotency-Key':identity,'X-Request-ID':`gle-bulk-request-${stableHash(identity)}`};
  }

  function readableError(error) {
    const raw = String(error && error.message || error || '操作失败');
    if (raw.includes('idempotency_key_payload_conflict')) return '这次请求与已提交内容不一致，请刷新后重新操作。';
    if (raw.includes('single_operator_second_confirmation_required')) return '需要再次确认当前不可变计划后才能批准。';
    if (raw.includes('active_knowledge_not_found')) return '当前没有匹配的生效知识，请先积累更多已完成实验。';
    if (raw.includes('automatic_action_not_low_risk')) return '该动作不是低风险观察动作，不能自动执行。';
    if (raw.includes('operation_action_not_approvable')) return '该调整已经推进，不能重复提交。';
    if (raw.includes('fresh_plan_and_approval_required')||raw.includes('operation_approval_expired')) return '当前方案已过期，请重新生成并确认创建方案。';
    if (raw.includes('meta_live_execution_not_available')) return '真实创建通道尚未开启，广告没有创建。你的确认已保留，请不要重复提交。';
    if (raw.includes('meta_reactivate_ad_not_available')) return '广告启用通道尚未开放，本次没有修改 Meta 广告。请联系管理员开启该账户的广告启用权限。';
    if (raw.includes('meta_advertiser_verification_required')) return 'Meta 还没有完成广告主验证。当前方案和已创建对象都已保留；请先在 Meta 完成验证，再回来重新检查并继续。';
    if (raw.includes('approved_creative_image_required_for_current_experiment')||raw.includes('approved_creative_image_required_for_every_cell')||raw.includes('approved_creative_image_required')) return '当前实验还没有有效的已审核素材。请先在素材审核区通过最新图片，系统会自动同步到这里。';
    if (raw.includes('approved_creative_file_missing')) return '已审核素材文件暂时不可用，系统已停止生成方案；请重新生成或上传素材后再继续。';
    if ([502,503,504].includes(Number(error?.status||0)) || /HTTP (502|503|504)\b/.test(raw)) return '服务刚刚更新或暂时繁忙，系统会自动恢复；请勿重复提交。';
    return raw;
  }

  async function api(path, options={}) {
    const method=String(options.method||'GET').toUpperCase(),attempts=method==='GET'?3:1;
    for(let attempt=0;attempt<attempts;attempt+=1){
      let response;
      try { response = await fetch(path, options); }
      catch (_) {
        if(attempt+1<attempts){await new Promise(resolve=>setTimeout(resolve,[1500,4000][attempt]||4000));continue;}
        const error = new Error('网络连接中断，可以安全重试。'); error.retryable = true; throw error;
      }
      const payload = await response.json().catch(() => ({}));
      if (response.ok) return payload;
      if([502,503,504].includes(response.status)&&attempt+1<attempts){await new Promise(resolve=>setTimeout(resolve,[1500,4000][attempt]||4000));continue;}
      const detail = payload.detail || {};
      const error = new Error(detail.message || detail.code || `HTTP ${response.status}`);
      error.status = response.status;
      error.detail = detail;
      throw error;
    }
  }

  function install() {
    const recommendationPanel = document.getElementById('adDailyRecommendationPanel');
    if (!recommendationPanel || document.getElementById('growthWorkspacePanel')) return;
    installStyles();
    const embeddedMount = document.getElementById('adGleTaskWorkbenchMount');
    if (!embeddedMount) {
      const actions = recommendationPanel.querySelector('.ad-report-actions') || recommendationPanel.querySelector('.ad-panel-head');
      const entry = document.createElement('button');
      entry.type = 'button';
      entry.id = 'growthWorkspaceEntry';
      entry.className = 'growth-entry';
      entry.innerHTML = '广告任务 <span id="growthWorkspaceCount" aria-label="待处理任务">0</span><span id="growthLaunchProgressBadge" hidden></span>';
      entry.addEventListener('click', () => openLaunchWorkspace({taskView:'orders'}));
      actions.appendChild(entry);
    }

    const panel = document.createElement('section');
    panel.id = 'growthWorkspacePanel';
    panel.className = embeddedMount ? 'growth-layer growth-layer-embedded' : 'growth-layer';
    panel.hidden = !embeddedMount;
    panel.innerHTML = embeddedMount ? `
      <aside class="growth-drawer" role="region" aria-label="覆盖广告关联任务">
        <header class="growth-drawer-head growth-embedded-head"><button type="button" class="growth-back-button growth-nav-back" data-growth-back aria-label="返回关联任务" hidden>←</button><div><span id="growthDrawerTitle">关联任务</span><small id="growthDrawerContext">只显示与覆盖广告精确绑定的任务</small></div><button type="button" class="growth-embedded-refresh" id="growthRefresh">刷新</button></header>
        <nav class="growth-queue-tabs" aria-label="关联任务分类"><button type="button" class="is-active" data-growth-bucket="action_required">待确认 <span>0</span></button><button type="button" data-growth-bucket="system_work">AI 处理中 <span>0</span></button><button type="button" data-growth-bucket="exception">异常 <span>0</span></button><button type="button" data-growth-bucket="observing">观察中 <span>0</span></button></nav>
        <div id="growthToolbarStatus" class="growth-notice" hidden></div>
        <main id="growthDetail" class="growth-detail"><div class="growth-empty"><div><b>正在读取关联任务</b><span>任务会自动按当前覆盖范围筛选</span></div></div></main>
      </aside>
      <section id="growthModal" class="growth-modal-layer" hidden></section>` : `
      <div class="growth-backdrop" data-growth-close aria-hidden="true"></div>
      <aside class="growth-drawer" role="dialog" aria-modal="true" aria-label="广告任务详情">
        <header class="growth-drawer-head"><button type="button" class="growth-back-button growth-nav-back" data-growth-back aria-label="返回上一步" hidden>←</button><div><span id="growthDrawerTitle">广告任务</span><small id="growthDrawerContext">查看任务详情与下一步</small></div><button type="button" class="growth-icon-button" data-growth-close aria-label="关闭">×</button></header>
        <nav class="growth-queue-tabs" aria-label="广告任务分类"><button type="button" class="is-active" data-growth-bucket="action_required">需要你处理 <span>0</span></button><button type="button" data-growth-bucket="system_work">AI 处理中 <span>0</span></button><button type="button" data-growth-bucket="exception">异常 <span>0</span></button><button type="button" data-growth-bucket="observing">观察中 <span>0</span></button></nav>
        <div class="growth-workbar"><input id="growthTaskSearch" type="search" aria-label="搜索广告任务" placeholder="搜索广告或订单"><button type="button" id="growthAuditAll" class="growth-tertiary">全部</button><button type="button" id="growthRefresh">刷新</button></div>
        <div id="growthToolbarStatus" class="growth-notice" hidden></div>
        <main id="growthDetail" class="growth-detail"><div class="growth-empty"><div><b>正在读取实验</b></div></div></main>
      </aside>
      <section id="growthModal" class="growth-modal-layer" hidden></section>`;
    if (embeddedMount) embeddedMount.replaceChildren(panel);
    else document.body.appendChild(panel);
    const launchPanel = document.createElement('section');
    launchPanel.id = 'growthLaunchPanel';
    launchPanel.className = 'growth-launch-layer';
    launchPanel.hidden = true;
    launchPanel.innerHTML = '<div class="growth-launch-backdrop" data-launch-close aria-hidden="true"></div><aside class="growth-launch-drawer" role="dialog" aria-modal="true" aria-label="广告任务与创建"><main id="growthLaunchContent" class="growth-launch-shell"></main></aside><section id="growthLaunchModal" class="growth-modal-layer" hidden></section>';
    document.body.appendChild(launchPanel);
    launchPanel.addEventListener('click',event=>{
      const button=event.target.closest?.('[data-launch-enable-order]');
      if(!button||!launchPanel.contains(button))return;
      openLaunchActivationConfirmation(button);
    });
    panel.querySelectorAll('[data-growth-close]').forEach(button => button.addEventListener('click', closeWorkspace));
    panel.querySelector('[data-growth-back]')?.addEventListener('click', event => {
      event.preventDefault();
      event.stopPropagation();
      backWorkspace();
    });
    panel.querySelector('#growthRefresh')?.addEventListener('click', loadList);
    panel.querySelector('#growthCreateExperiment')?.addEventListener('click', openCreateFlow);
    panel.querySelectorAll('[data-growth-bucket]').forEach(button => button.addEventListener('click', () => { closeModal();state.workBucket=button.dataset.growthBucket||'action_required';state.activeExperiment='';renderQueueTabs();renderExperimentQueue(); }));
    panel.querySelector('#growthAuditAll')?.addEventListener('click', () => { closeModal();state.workBucket='all';state.activeExperiment='';renderQueueTabs();renderExperimentQueue(); });
    panel.querySelector('#growthTaskSearch')?.addEventListener('input', event => { state.taskSearch=event.target.value.trim();state.activeExperiment='';renderExperimentQueue(); });
    window.addEventListener('creative-pro-workbench-updated', event => syncLaunchProgress(event.detail || {}));
    window.addEventListener('gle-coverage-scope-updated', event => setCoverageScope(event.detail?.experimentIds || []).catch(error => console.error('growth_coverage_scope_failed', error)));
    document.addEventListener('keydown',event=>{if(event.key!=='Escape')return;const bulkModal=document.querySelector('#growthBulkRebuildModal:not([hidden])');if(bulkModal){closeBulkRebuildModal();return;}const activeModal=document.querySelector('#growthModal:not([hidden]),#growthLaunchModal:not([hidden])');if(activeModal){closeModal();return;}const launch=document.getElementById('growthLaunchPanel');if(launch&&!launch.hidden){closeLaunchWorkspace();return;}const workspace=document.getElementById('growthWorkspacePanel');if(workspace&&!workspace.hidden)closeWorkspace();});
    restoreLaunchProgress();
    restoreLaunchOrdersCache();
    updateLaunchProgressBadge();
    loadLaunchOrders({badgeOnly:true});
    loadUnifiedTaskIndex();
    if (embeddedMount) {
      let initialIds=[];
      try { initialIds = JSON.parse(embeddedMount.dataset.experimentIds || '[]'); } catch (_) { initialIds=[]; }
      if (Array.isArray(initialIds) && (initialIds.length || embeddedMount.dataset.scopeReady === '1')) setCoverageScope(initialIds).catch(error => console.error('growth_coverage_initial_scope_failed', error));
    }
  }

  function installStyles() {
    const style = document.createElement('style');
    style.textContent = `
      .growth-mini-steps span{border:1px solid transparent}.growth-mini-steps span.done{background:#f7f9fc;color:#52617a;font-weight:750}.growth-mini-steps span.current{border-color:#c9d7fb;background:#edf3ff;color:var(--blue);font-weight:800}.growth-batch-progress{margin-top:12px;padding:13px 14px;border:1px solid #dce5f3;border-radius:8px;background:#fff}.growth-batch-progress-head{display:flex;align-items:center;justify-content:space-between;gap:12px;color:#344054;font-size:12px}.growth-batch-progress-head strong{font-size:14px}.growth-batch-progress-head span{color:#315fd8;font-weight:800}.growth-batch-progress-track{height:7px;margin:11px 0 8px;overflow:hidden;border-radius:999px;background:#e5eaf2}.growth-batch-progress-track i{display:block;height:100%;border-radius:inherit;background:#3569e8;transition:width .25s ease}.growth-batch-progress small{display:block;color:#667085;font-size:10px}.growth-batch-ledger div.is-current{border:1px solid #c9d7fb;background:#f3f6fd}.growth-batch-ledger div.is-done{background:#f4faf7}.growth-batch-ledger em{color:#7d899d;font-size:10px;font-style:normal;font-weight:750}.growth-batch-ledger .is-current em{color:#315fd8}.growth-batch-ledger .is-done em{color:#079455}.growth-batch-connection{margin-top:7px;color:#b54708;font-size:10px}
      .growth-entry{position:relative;border:1px solid #315fd8!important;background:#3569e8!important;color:#fff!important;font-weight:800!important}.growth-entry span{display:inline-grid;place-items:center;min-width:19px;height:19px;margin-left:5px;padding:0 5px;border-radius:10px;background:rgba(255,255,255,.2);color:#fff;font-size:11px}.growth-entry span[hidden]{display:none}.growth-entry span.is-ready{background:#eaf8ef;color:#087a46}.growth-entry span.is-failed{background:#fff0ef;color:#b42318}.growth-entry:focus-visible{outline:3px solid rgba(53,105,232,.25)!important;outline-offset:2px!important}
      .growth-layer{--ink:#17233d;--muted:#6c788e;--line:#e0e6ef;--blue:#3569e8;position:fixed;inset:0;z-index:1500}.growth-layer[hidden],.growth-modal-layer[hidden]{display:none}.growth-backdrop{position:absolute;inset:0;background:rgba(20,32,55,.18);backdrop-filter:blur(.6px)}
      .growth-drawer{position:absolute;inset:0 0 0 auto;width:clamp(620px,42vw,860px);min-width:0;display:flex;flex-direction:column;color:var(--ink);background:#fff;border-left:1px solid #d9e1ec;box-shadow:-18px 0 44px rgba(27,43,76,.16);font:14px/1.45 Inter,"PingFang SC","Microsoft YaHei",sans-serif}
      #growthWorkspacePanel button{min-width:0!important;width:auto;margin:0!important;min-height:40px!important;padding:0 17px!important;border:1px solid #cfd8e6!important;border-radius:7px!important;background:#fff!important;color:#34425d!important;font:800 14px/1 Inter,"PingFang SC","Microsoft YaHei",sans-serif!important;box-shadow:none!important;white-space:nowrap;text-transform:none;letter-spacing:normal!important}
      .growth-drawer-head{min-height:64px;padding:0 20px;display:grid;grid-template-columns:36px minmax(0,1fr) 36px;align-items:center;gap:10px;border-bottom:1px solid var(--line)}.growth-drawer-head>div{display:flex;min-width:0;flex-direction:column;gap:2px}.growth-drawer-head span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:17px;font-weight:800}.growth-drawer-head small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--muted);font-size:11px}#growthWorkspacePanel .growth-icon-button,#growthWorkspacePanel .growth-back-button{width:36px;height:36px;min-height:36px!important;padding:0!important;border:0!important;border-radius:8px!important;background:transparent!important;color:#667085!important;cursor:pointer}#growthWorkspacePanel .growth-icon-button{font-size:24px}#growthWorkspacePanel .growth-back-button{font-size:18px}#growthWorkspacePanel .growth-icon-button:hover,#growthWorkspacePanel .growth-back-button:hover{background:#f1f4f8!important}
      .growth-queue-tabs{display:grid;grid-template-columns:repeat(4,1fr);gap:4px;padding:8px 24px 0;border-bottom:1px solid var(--line)}#growthWorkspacePanel .growth-queue-tabs button{min-height:42px!important;padding:0 8px!important;border:0!important;border-bottom:2px solid transparent!important;border-radius:0!important;color:#69768b!important;font-size:12px!important}#growthWorkspacePanel .growth-queue-tabs button.is-active{border-bottom-color:var(--blue)!important;color:var(--blue)!important;background:#f6f8ff!important}.growth-queue-tabs span{display:inline-grid;place-items:center;min-width:18px;height:18px;margin-left:4px;padding:0 4px;border-radius:9px;background:#eef2f7;font-size:10px}
      .growth-workbar{min-height:64px;padding:11px 24px;display:flex;align-items:center;gap:9px;border-bottom:1px solid var(--line);overflow:hidden}.growth-workbar input,.growth-workbar button{flex:0 0 auto;height:38px;border:1px solid #d4dce8;border-radius:7px;background:#fff;color:#34425c;font:inherit;font-weight:700}.growth-workbar input{min-width:0;flex:1 1 260px;padding:0 12px;font-weight:500}.growth-workbar button{min-height:38px!important;padding:0 13px!important;cursor:pointer}.growth-workbar .primary,#growthWorkspacePanel .growth-primary{border-color:var(--blue)!important;background:var(--blue)!important;color:#fff!important}#growthWorkspacePanel .growth-danger{border-color:#f1b7b7!important;background:#fff5f5!important;color:#b42318!important}#growthWorkspacePanel .growth-tertiary{border-color:transparent!important;background:transparent!important;color:#65738a!important;font-size:12px!important}.growth-workbar #growthRefresh{margin-left:0}
      .growth-detail{min-width:0;min-height:0;flex:1 1 auto;overflow:auto;padding:25px 28px 30px}.growth-empty{min-height:320px;height:100%;display:grid;place-items:center;text-align:center;color:var(--muted)}.growth-empty div{display:flex;flex-direction:column;gap:6px}.growth-empty b{color:var(--ink);font-size:15px}.growth-error,.growth-notice{margin:12px 24px;padding:10px 12px;border-left:3px solid #3f70ed;background:#f2f6ff;color:#4b5d7a;font-size:12px}.growth-error{border-color:#d92d20;background:#fff1f0;color:#9a271f}.growth-chip{display:inline-flex;align-items:center;gap:6px;color:#3466e7;font-size:11px;font-weight:800}.growth-chip:before{content:'';width:7px;height:7px;border-radius:50%;background:currentColor}.growth-chip.warn{color:#c27a16}.growth-chip.good{color:#15956d}
      .growth-queue-head{display:flex;align-items:flex-start;justify-content:space-between;gap:18px;margin-bottom:16px}.growth-queue-head h2{margin:0 0 4px;font-size:18px}.growth-queue-head p{margin:0;color:var(--muted);font-size:12px}.growth-queue-limit{color:var(--muted);font-size:11px;white-space:nowrap}.growth-task-list{display:grid;gap:10px}.growth-task-card{width:100%!important;min-height:82px!important;height:auto!important;padding:14px 16px!important;display:grid!important;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:18px;border:1px solid var(--line)!important;border-radius:9px!important;text-align:left;cursor:pointer}.growth-task-card:hover{border-color:#b7c9f5!important;background:#f8faff!important}.growth-task-card strong{display:block;margin-bottom:4px;color:var(--ink);font-size:14px;line-height:1.35}.growth-task-card p{margin:0;color:#53627a;font-size:12px;line-height:1.45;white-space:normal}.growth-task-meta{display:flex;gap:8px;margin-top:7px;color:#8290a4;font-size:10px;line-height:1.3}.growth-task-next{max-width:190px;text-align:right;color:var(--blue);font-size:11px;font-weight:800;line-height:1.45;white-space:normal}.growth-queue-back{margin:0 0 14px!important;padding:0!important;border:0!important;background:transparent!important;color:#5570ae!important;font-size:12px!important}.growth-no-action{margin-right:auto;color:#66758b;font-size:12px;font-weight:700}
      .growth-task-group{overflow:hidden;border:1px solid #dfe5ee;border-radius:10px;background:#fff}.growth-task-group>header{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:13px 15px;border-bottom:1px solid #e8edf4;color:#667085;font-size:11px}.growth-task-group>header b{overflow:hidden;color:#26344d;font-size:13px;text-overflow:ellipsis;white-space:nowrap}.growth-task-group>header span{white-space:nowrap}.growth-task-group-list{display:grid}.growth-task-group-row{width:100%!important;min-height:66px!important;padding:0 15px!important;display:grid!important;grid-template-columns:minmax(0,1fr) auto 18px;align-items:center;gap:12px;border:0!important;border-bottom:1px solid #edf1f5!important;border-radius:0!important;background:#fff!important;text-align:left;cursor:pointer}.growth-task-group-row:last-child{border-bottom:0!important}.growth-task-group-row:hover{background:#f7f9fd!important}.growth-task-group-copy{min-width:0;display:grid;gap:3px}.growth-task-group-copy strong{overflow:hidden;color:#27364f;font-size:13px;text-overflow:ellipsis;white-space:nowrap}.growth-task-group-copy small{overflow:hidden;color:#7d899a;font-size:10px;font-weight:600;text-overflow:ellipsis;white-space:nowrap}.growth-task-group-row>span{color:#69778d;font-size:11px;white-space:nowrap}.growth-task-group-row i{color:#315fd8;font-size:15px;font-style:normal}
      .growth-task-group-head-actions{display:flex;align-items:center;gap:10px}.growth-task-group-action{min-height:32px!important;padding:0 11px!important;border-color:#3569e8!important;background:#3569e8!important;color:#fff!important;font-size:10px!important}.growth-task-group-action:focus-visible{outline:3px solid rgba(53,105,232,.22)!important;outline-offset:2px!important}
      .growth-detail-head{display:flex;align-items:flex-start;justify-content:space-between;gap:18px}.growth-detail-head h2{margin:0 0 5px;font-size:19px;letter-spacing:-.02em}.growth-detail-head p{margin:0;color:var(--muted);font-size:12px}.growth-stepper{display:grid;grid-template-columns:repeat(6,1fr);margin:18px 0 22px;padding:12px;border:1px solid var(--line);border-radius:8px;background:#fafbfd}.growth-step{position:relative;display:flex;flex-direction:column;align-items:center;gap:6px;color:#7a879b;font-size:10px}.growth-step:before{content:'';position:absolute;right:50%;top:11px;width:100%;border-top:1px solid #d5dce7}.growth-step:first-child:before{display:none}.growth-step i{position:relative;z-index:1;width:23px;height:23px;display:grid;place-items:center;border:1px solid #ccd5e2;border-radius:50%;background:#fff;font-size:10px;font-style:normal}.growth-step.done i{color:var(--blue);border-color:#c6d5fb}.growth-step.current{color:var(--blue);font-weight:800}.growth-step.current i{color:#fff;border-color:var(--blue);background:var(--blue)}
      .growth-phase{padding:14px 16px;border:1px solid #dbe6fa;border-radius:8px;background:#f4f7ff}.growth-phase small{color:#6f7f99;font-weight:800}.growth-phase h3{margin:5px 0 3px;font-size:17px}.growth-phase p{margin:0;color:#51637f}.growth-section{padding-top:20px}.growth-section h3{margin:0 0 12px;font-size:14px}.growth-metric-table{width:100%;border-collapse:collapse;font-size:12px}.growth-metric-table th,.growth-metric-table td{padding:10px 7px;border-bottom:1px solid var(--line);text-align:right}.growth-metric-table th:first-child,.growth-metric-table td:first-child{text-align:left}.growth-metric-table th{color:#8792a5;font-weight:600}.growth-good{color:#15956d}.growth-muted{color:var(--muted)}
      .growth-progress{height:8px;border-radius:5px;background:#e5eaf2;overflow:hidden}.growth-progress i{display:block;height:100%;border-radius:inherit;background:var(--blue)}.growth-progress-copy{margin-top:11px;display:flex;justify-content:space-between;color:var(--muted);font-size:11px}.growth-progress-copy b{color:var(--blue)}.growth-actions{min-height:96px;display:flex;align-items:center;justify-content:flex-end;gap:11px;border-bottom:1px solid var(--line)}.growth-actions button:disabled{opacity:.5;cursor:not-allowed}.growth-status-panel{margin-top:18px;padding:20px;border:1px solid #dbe3ef;border-radius:10px;background:#fff}.growth-status-panel>small{display:block;margin-bottom:6px;color:#6f7f99;font-weight:800}.growth-status-panel h3{margin:0;font-size:18px}.growth-status-panel p{margin:7px 0 0;color:#52627c;line-height:1.65}.growth-status-next{margin-top:14px;padding:11px 13px;border-radius:7px;background:#f4f7fd;color:#3f516f;font-size:12px}.growth-status-actions{display:flex;justify-content:flex-end;gap:10px;margin-top:18px;padding-top:16px;border-top:1px solid var(--line)}.growth-technical{margin-top:15px;border-bottom:1px solid var(--line)}.growth-technical summary{padding:15px 4px;color:#58667d;font-size:12px;cursor:pointer}.growth-technical pre{max-height:230px;overflow:auto;padding:12px;border-radius:8px;background:#f5f7fb;white-space:pre-wrap;font:11px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace}.growth-timeline{display:grid;gap:8px;margin-bottom:15px}.growth-timeline div{padding-left:10px;border-left:2px solid #b8c8e0;color:#55647d;font-size:11px}.growth-timeline b{color:#2f3d57}.growth-footer{min-height:57px;padding:0 24px;display:flex;align-items:center;justify-content:space-between;border-top:1px solid var(--line);color:#7990a4;font-size:11px}#growthWorkspacePanel .growth-footer button{min-height:34px!important;padding:0!important;border:0!important;background:transparent!important;color:#4567b9!important;font-size:12px!important;font-weight:800!important;cursor:pointer}
      .growth-modal-layer{position:absolute;inset:0;z-index:4;display:grid;place-items:center;background:rgba(19,31,56,.3)}.growth-modal{width:min(590px,calc(100vw - 36px));max-height:calc(100vh - 48px);overflow:auto;border-radius:13px;background:#fff;box-shadow:0 24px 70px rgba(24,39,71,.27)}.growth-modal.growth-modal-compact{width:min(440px,calc(100vw - 36px))}.growth-modal-compact .growth-modal-head{min-height:58px}.growth-modal-compact .growth-modal-body{padding:20px 22px}.growth-modal-compact .growth-modal-foot{min-height:64px}.growth-modal-head{min-height:70px;padding:0 22px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line)}.growth-modal-head div{display:flex;flex-direction:column}.growth-modal-head b{font-size:18px}.growth-modal-head small{color:var(--muted)}.growth-modal-body{padding:24px}.growth-modal-body h3{margin:0 0 7px;font-size:19px}.growth-modal-body>p{margin:0 0 20px;color:var(--muted);font-size:12px}.growth-modal-foot{min-height:72px;padding:0 22px;display:flex;align-items:center;justify-content:flex-end;gap:10px;border-top:1px solid var(--line)}.growth-mini-steps{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:22px}.growth-mini-steps span{padding:8px;border-radius:6px;background:#f2f4f7;color:#7d899d;text-align:center;font-size:11px}.growth-mini-steps span.active{background:#edf3ff;color:var(--blue);font-weight:800}.growth-bulk-modal-layer{--ink:#17233d;--muted:#6c788e;--line:#e0e6ef;--blue:#3569e8;position:fixed;z-index:1900;padding:18px;color:var(--ink);backdrop-filter:blur(2px);font:14px/1.45 Inter,"PingFang SC","Microsoft YaHei",sans-serif}.growth-bulk-modal-layer .growth-modal{width:min(720px,calc(100vw - 36px))}.growth-bulk-modal-layer .growth-timeline{max-height:300px;overflow:auto}.growth-bulk-modal-layer button{min-height:40px;padding:0 17px;border:1px solid #cfd8e6;border-radius:7px;background:#fff;color:#34425d;font:800 14px/1 Inter,"PingFang SC","Microsoft YaHei",sans-serif;cursor:pointer}.growth-bulk-modal-layer .growth-icon-button{width:36px;height:36px;min-height:36px;padding:0;border:0;background:transparent;color:#667085;font-size:24px}.growth-bulk-modal-layer .growth-primary{border-color:var(--blue);background:var(--blue);color:#fff}.growth-bulk-modal-layer button:disabled{opacity:.5;cursor:not-allowed}#growthWorkspacePanel .growth-choice{width:100%;display:flex;gap:11px;align-items:center;padding:14px!important;border:1px solid #d9e1ec!important;border-radius:8px!important;margin:9px 0!important;cursor:pointer}#growthWorkspacePanel .growth-choice.selected{border-color:#5d84ec!important;background:#f5f8ff!important}.growth-choice span{display:flex;flex-direction:column;gap:3px}.growth-choice small{color:var(--muted)}.growth-form{display:grid;gap:13px}.growth-form label{display:grid;gap:6px;color:#536077;font-size:12px;font-weight:700}.growth-form input,.growth-form select,.growth-form textarea{width:100%;padding:10px 11px;border:1px solid #d4dce8;border-radius:7px;color:#26344d;background:#fff;font:inherit}.growth-form textarea{min-height:70px;resize:vertical}.growth-review-card{padding:15px;border-radius:9px;background:#f1f6ff;color:#51637f}.growth-review-card b{display:block;margin-bottom:4px;color:#27364f}.growth-operator-issue{display:grid;gap:10px;margin:14px 0;padding:14px;border:1px solid #f3b6b0;border-radius:9px;background:#fff7f6}.growth-operator-issue>strong{color:#912018;font-size:13px}.growth-operator-issue dl{display:grid;grid-template-columns:88px minmax(0,1fr);gap:8px 12px;margin:0;font-size:11px;line-height:1.55}.growth-operator-issue dt{color:#7b8799;font-weight:800}.growth-operator-issue dd{margin:0;color:#344054}.growth-operator-issue small{color:#667085}.growth-delivery-row{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-top:9px;padding:9px 0 0;border-top:1px solid #dbe6fa}.growth-delivery-row span{display:grid;gap:2px;color:#27364f;font-weight:750}.growth-delivery-row small{color:#7a879a;font:10px/1.3 ui-monospace,SFMono-Regular,Menlo,monospace;font-weight:500}.growth-delivery-row strong{color:#2458cf;font-size:11px}.growth-observation-metrics,.growth-observation-maturity{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:12px}.growth-observation-metrics span,.growth-observation-maturity span{display:grid;gap:2px;padding:8px;border:1px solid #dbe6fa;border-radius:7px;background:#fff;font-size:11px}.growth-observation-metrics strong,.growth-observation-maturity strong{color:#27364f;font-size:13px}.growth-observation-score{display:flex;align-items:center;gap:12px;margin-top:12px;padding:10px 12px;border-radius:7px;background:#dfeaff}.growth-observation-score strong{font-size:20px;color:#244fb8}.growth-observation-score span{font-size:11px}.growth-observation-source{display:block;margin-top:12px;padding-top:10px;border-top:1px solid #dbe6fa;line-height:1.55}.growth-safety{margin-top:12px;padding:10px;border-radius:7px;background:#fff7e8;color:#815000;font-size:11px}.growth-plan-form{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:13px}.growth-plan-form label{display:grid;gap:5px;color:#536077;font-size:11px;font-weight:700}.growth-plan-form label.wide{grid-column:1/-1}.growth-plan-form input,.growth-plan-form select,.growth-plan-form textarea{width:100%;padding:9px;border:1px solid #d4dce8;border-radius:7px;font:inherit}.growth-plan-form textarea{min-height:64px;font:11px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}
      .growth-modal.growth-batch-modal{width:min(1180px,calc(100vw - 28px))}.growth-batch-modal .growth-modal-head{min-height:58px}.growth-batch-modal .growth-modal-body{padding:16px 22px}.growth-batch-modal .growth-modal-foot{min-height:60px}.growth-plan-context{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:11px;padding:8px 10px;border-radius:8px;background:#f5f7fb;color:#58677e;font-size:10px}.growth-plan-context b{color:#26344d}.growth-batch-invariants{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}.growth-batch-invariants span{padding:8px 10px;border-radius:7px;background:#f5f7fb;color:#5f6d83;font-size:10px}.growth-batch-invariants b{margin-right:5px;color:#26344d}.growth-structure-head{display:grid;grid-template-columns:230px minmax(260px,.95fr) minmax(310px,1.15fr);gap:9px;margin-bottom:5px;padding:0 3px;color:#77849a;font-size:10px;font-weight:800}.growth-structure-layout{display:grid;grid-template-columns:230px minmax(0,1fr);gap:9px;align-items:start}.growth-structure-rows{display:grid;gap:8px}.growth-structure-row{display:grid;grid-template-columns:minmax(260px,.95fr) minmax(310px,1.15fr);gap:9px}.growth-structure-card{min-width:0;padding:10px;border:1px solid #dce3ee;border-radius:9px;background:#fff}.growth-structure-card.is-campaign{position:sticky;top:0;background:#f8faff}.growth-structure-card.is-baseline{border-color:#6f91ee;box-shadow:0 0 0 2px #edf3ff inset}.growth-structure-card>header{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:7px}.growth-structure-card>header b{overflow:hidden;color:#24324a;font-size:11px;text-overflow:ellipsis;white-space:nowrap}.growth-structure-card>header span{color:#6d7b91;font-size:9px}.growth-structure-fields{display:grid;gap:6px}.growth-structure-fields.two{grid-template-columns:1fr 1fr}.growth-structure-fields label{display:grid;gap:3px;color:#637088;font-size:9px;font-weight:750}.growth-structure-fields label.wide{grid-column:1/-1}.growth-structure-fields input,.growth-structure-fields select,.growth-structure-fields textarea{width:100%;min-width:0;padding:6px 8px;border:1px solid #d4dce8;border-radius:6px;color:#26344d;background:#fff;font:inherit}.growth-structure-fields textarea{min-height:40px;resize:vertical}.growth-structure-facts{display:grid;gap:4px;margin-top:7px;padding-top:6px;border-top:1px solid #e7ebf1;color:#68768b;font-size:9px}.growth-structure-facts b{color:#34425a}.growth-batch-role{display:inline-flex;align-items:center;gap:4px;color:#52617a;font-size:9px;font-weight:750}.growth-batch-role input{accent-color:#3569e8}.growth-batch-ledger{display:grid;gap:7px;margin-top:14px}.growth-batch-ledger div{display:grid;grid-template-columns:32px minmax(0,1fr) auto;gap:8px;align-items:center;padding:9px 10px;border-radius:7px;background:#f6f8fc;color:#53627a;font-size:11px}.growth-batch-ledger b{color:#21304a}.growth-batch-status{display:grid;gap:5px;margin-top:12px;padding:12px;border-left:3px solid #3569e8;background:#f3f6fd;color:#4c5f7e;font-size:12px}.growth-batch-status b{color:#20314f;font-size:13px}.growth-batch-status small{color:#315fd8;font-weight:750}.growth-incident-columns{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}.growth-incident-columns section{padding:13px;border:1px solid #dfe5ee;border-radius:8px;background:#fff;color:#53627a;font-size:11px}.growth-incident-columns b{color:#20314f;font-size:12px}.growth-incident-columns ul{display:grid;gap:5px;margin:9px 0 0;padding-left:18px}.growth-batch-technical{margin-top:8px;color:#77849a;font-size:10px}.growth-batch-technical summary{cursor:pointer}.growth-batch-technical span{display:block;margin-top:5px}
      .growth-incident-pages{display:grid;gap:7px;margin-top:10px}.growth-incident-page{display:flex;align-items:center;gap:9px;padding:10px 11px;border:1px solid #dfe5ee;border-radius:7px;background:#fff;color:#344054;cursor:pointer}.growth-incident-page:has(input:checked){border-color:#6f91ee;background:#f6f8ff}.growth-incident-page input{width:16px;height:16px;margin:0;accent-color:#3569e8}.growth-incident-page span{display:grid;gap:2px}.growth-incident-page small{color:#667085;font-size:10px}.growth-incident-page:focus-within{outline:3px solid rgba(53,105,232,.16);outline-offset:1px}
      .growth-modal.growth-delete-modal{width:min(560px,calc(100vw - 32px));max-height:calc(100vh - 32px);overflow:hidden;display:grid;grid-template-rows:auto minmax(0,1fr) auto;border-radius:14px}.growth-delete-modal .growth-modal-head{min-height:64px;padding:0 24px}.growth-delete-modal .growth-modal-head b{color:#17233d;font-size:17px;font-weight:700;letter-spacing:-.01em}.growth-delete-modal .growth-modal-head small{margin-top:3px;color:#667085;font-size:11px}.growth-delete-modal .growth-icon-button{width:32px!important;height:32px!important;min-height:32px!important;padding:0!important;border:0!important;border-radius:8px!important;background:#f2f4f7!important;color:#475467!important;font-size:18px!important;line-height:1!important}.growth-delete-modal .growth-icon-button:hover{background:#e9edf3!important;color:#17233d!important}.growth-delete-modal .growth-modal-body{min-height:0;overflow:auto;padding:22px 24px}.growth-delete-modal .growth-modal-body h3{margin:0 0 6px;color:#17233d;font-size:14px;font-weight:700}.growth-delete-modal .growth-modal-body>p{max-width:68ch;margin:0 0 16px;color:#5f6c80;font-size:11px;line-height:1.55}.growth-delete-modal .growth-delete-modes{gap:8px}.growth-delete-modal .growth-delete-mode{grid-template-columns:16px minmax(0,1fr);align-items:center;gap:11px;min-height:62px;padding:11px 13px;border-color:#dfe5ee;border-radius:9px;background:#fff}.growth-delete-modal .growth-delete-mode:hover{border-color:#b8c6dd;background:#fafbfc}.growth-delete-modal .growth-delete-mode:has(input:checked){border-color:#5b82ed;background:#f7f9ff;box-shadow:0 0 0 1px rgba(53,105,232,.05)}.growth-delete-modal .growth-delete-mode input[type="radio"]{appearance:none!important;width:16px!important;height:16px!important;min-width:16px!important;margin:0!important;padding:0!important;border:1.5px solid #98a2b3!important;border-radius:50%!important;background:#fff!important;box-shadow:none!important}.growth-delete-modal .growth-delete-mode input[type="radio"]:checked{border-color:#3569e8!important;background:#3569e8!important;box-shadow:inset 0 0 0 4px #fff!important}.growth-delete-modal .growth-delete-mode:focus-within{outline:3px solid rgba(53,105,232,.15);outline-offset:1px}.growth-delete-modal .growth-delete-mode span{gap:2px;color:#27364f;font-size:12px;font-weight:650;line-height:1.35}.growth-delete-modal .growth-delete-mode small{color:#667085;font-size:10px;font-weight:400;line-height:1.45}.growth-delete-modal .growth-meta-delete-preview{margin-top:10px;padding:11px 13px;border-color:#f0b7af;border-radius:9px;background:#fff7f5;color:#7a271a;font-size:10px;line-height:1.45}.growth-meta-delete-summary{display:flex;align-items:baseline;justify-content:space-between;gap:12px}.growth-meta-delete-summary b{font-size:11px}.growth-meta-delete-summary span{color:#8f3b30}.growth-meta-delete-checks{margin-top:5px;color:#6b413b}.growth-meta-delete-warning{margin-top:7px;padding-top:7px;border-top:1px solid #f3cbc6;color:#8f2418;font-weight:700}.growth-delete-modal .growth-meta-delete-ack{display:grid;grid-template-columns:18px minmax(0,1fr);align-items:start;gap:10px;margin-top:10px;padding:11px 13px;border:1px solid #dfe5ee;border-radius:9px;background:#fff;color:#344054;cursor:pointer}.growth-delete-modal .growth-meta-delete-ack:has(input:checked){border-color:#e89c92;background:#fff9f7}.growth-delete-modal .growth-meta-delete-ack input[type="checkbox"]{appearance:none!important;width:18px!important;height:18px!important;min-width:18px!important;margin:1px 0 0!important;padding:0!important;display:grid!important;place-items:center;border:1.5px solid #98a2b3!important;border-radius:5px!important;background:#fff!important;box-shadow:none!important}.growth-delete-modal .growth-meta-delete-ack input[type="checkbox"]:checked{border-color:#3569e8!important;background:#3569e8!important}.growth-delete-modal .growth-meta-delete-ack input[type="checkbox"]:checked:after{content:"✓";color:#fff;font-size:13px;font-weight:800;line-height:1}.growth-delete-modal .growth-meta-delete-ack:focus-within{outline:3px solid rgba(53,105,232,.15);outline-offset:1px}.growth-delete-modal .growth-meta-delete-ack span{display:grid;gap:2px}.growth-delete-modal .growth-meta-delete-ack b{color:#27364f;font-size:11px;font-weight:700}.growth-delete-modal .growth-meta-delete-ack small{color:#667085;font-size:10px;font-weight:400;line-height:1.5}.growth-delete-modal .growth-modal-foot{min-height:64px;padding:0 24px}.growth-delete-modal .growth-modal-foot button{min-height:38px!important;padding:0 14px!important;border-radius:8px!important;font-size:12px!important;font-weight:650!important}.growth-delete-modal .growth-modal-foot .growth-primary{background:#d92d20!important;border-color:#d92d20!important}.growth-delete-modal .growth-modal-foot .growth-primary:hover{background:#b42318!important;border-color:#b42318!important}@media(max-width:620px){.growth-modal.growth-delete-modal{width:calc(100vw - 20px);max-height:calc(100vh - 20px)}.growth-delete-modal .growth-modal-head,.growth-delete-modal .growth-modal-body,.growth-delete-modal .growth-modal-foot{padding-left:16px;padding-right:16px}.growth-meta-delete-summary{display:grid;gap:2px}.growth-delete-modal .growth-modal-foot{justify-content:stretch}.growth-delete-modal .growth-modal-foot button{flex:1}}
      .growth-delete-modal .growth-delete-mode input[type="radio"]{-webkit-appearance:none!important;appearance:none!important;box-sizing:border-box!important;display:block!important;flex:0 0 16px!important;width:16px!important;height:16px!important;min-width:16px!important;min-height:16px!important;max-width:16px!important;max-height:16px!important;margin:0!important;padding:0!important}.growth-delete-modal .growth-meta-delete-ack input[type="checkbox"]{-webkit-appearance:none!important;appearance:none!important;box-sizing:border-box!important;display:grid!important;place-items:center!important;flex:0 0 18px!important;width:18px!important;height:18px!important;min-width:18px!important;min-height:18px!important;max-width:18px!important;max-height:18px!important;margin:1px 0 0!important;padding:0!important}.growth-delete-modal .growth-delete-mode:focus-within,.growth-delete-modal .growth-meta-delete-ack:focus-within{outline:none}.growth-delete-modal .growth-delete-mode:has(input:focus-visible),.growth-delete-modal .growth-meta-delete-ack:has(input:focus-visible){outline:3px solid rgba(53,105,232,.15);outline-offset:1px}.growth-delete-modal .growth-icon-button{min-width:32px!important;max-width:32px!important;max-height:32px!important;margin:0!important;box-shadow:none!important;transform:none!important}.growth-delete-modal .growth-modal-foot button{margin:0!important;box-shadow:none!important;transform:none!important}.growth-delete-modal .growth-modal-foot button:not(.growth-primary){border:1px solid #d0d5dd!important;background:#fff!important;color:#344054!important}.growth-delete-modal .growth-modal-foot button:not(.growth-primary):hover{border-color:#98a2b3!important;background:#f9fafb!important;color:#101828!important}.growth-delete-modal .growth-modal-foot .growth-primary{color:#fff!important;box-shadow:none!important}
      @media(max-width:1100px){.growth-drawer{width:100vw}.growth-workbar{padding:10px 16px}.growth-detail{padding:20px}.growth-plan-form{grid-template-columns:1fr}.growth-plan-form label.wide{grid-column:auto}.growth-structure-head{display:none}.growth-structure-layout{grid-template-columns:1fr}.growth-structure-card.is-campaign{position:static}.growth-structure-row{grid-template-columns:1fr 1fr}}
      .growth-launch-layer{--launch-blue:#3569e8;--launch-ink:#17233d;--launch-muted:#6c788e;position:fixed;inset:0;z-index:1600;overflow:auto;background:radial-gradient(circle at 0 50%,#edf3ff 0,transparent 32%),#f7f9fd;font:14px/1.45 Inter,"PingFang SC","Microsoft YaHei",sans-serif;color:var(--launch-ink)}.growth-launch-layer[hidden]{display:none}.growth-launch-shell{min-height:100%;display:grid;grid-template-columns:210px minmax(0,1fr)}.growth-launch-nav{padding:28px 10px;border-right:1px solid #dfe6f0}.growth-launch-brand{height:54px;padding:0 18px;font-size:18px;font-weight:850}.growth-launch-nav button{width:100%;min-height:48px;margin:4px 0;padding:0 17px;border:0;border-radius:8px;background:transparent;color:#26344e;text-align:left;font-weight:750;cursor:pointer}.growth-launch-nav button.is-active{color:var(--launch-blue);background:#fff;box-shadow:0 7px 20px rgba(43,64,108,.11),inset 3px 0 var(--launch-blue)}.growth-launch-main{min-width:0;padding:24px 28px}.growth-launch-page{max-width:1260px;min-height:calc(100vh - 48px);margin:auto;padding:24px;border:1px solid #e0e6ef;border-radius:10px;background:#fff;box-shadow:0 14px 38px rgba(34,52,87,.07)}.growth-launch-page h1{margin:7px 0 8px;font-size:28px}.growth-launch-page>p{margin:0;color:var(--launch-muted)}.growth-launch-kicker{color:var(--launch-blue);font-size:12px;font-weight:850}.growth-launch-routes{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:38px}.growth-launch-routes button{min-height:235px;padding:27px;display:flex;flex-direction:column;align-items:flex-start;gap:14px;border:1px solid #dbe3ee;border-radius:11px;background:#fff;text-align:left;cursor:pointer}.growth-launch-routes button:last-child{border-color:#5b82ed;background:#f7faff}.growth-launch-routes b{font-size:21px}.growth-launch-routes span{color:#69768c;line-height:1.7}.growth-launch-routes em{margin-top:auto;color:var(--launch-blue);font-style:normal;font-weight:800}.growth-launch-form{max-width:760px;margin-top:30px;display:grid;grid-template-columns:1fr 1fr;gap:16px}.growth-launch-form label{display:grid;gap:7px;color:#4f5d75;font-size:12px;font-weight:750}.growth-launch-form input,.growth-launch-form select{height:44px;padding:0 12px;border:1px solid #d5deea;border-radius:8px;background:#fff;color:#25334c}.growth-launch-actions{grid-column:1/-1;margin-top:16px;display:flex;justify-content:flex-end;gap:10px}.growth-launch-actions button,.growth-launch-primary,.growth-launch-secondary{min-height:42px;padding:0 18px;border-radius:7px;font-weight:800;cursor:pointer}.growth-launch-primary{border:1px solid var(--launch-blue)!important;background:var(--launch-blue)!important;color:#fff!important}.growth-launch-secondary{border:1px solid #d3dce9!important;background:#fff!important;color:#40506a!important}.growth-launch-plan{display:grid;grid-template-columns:repeat(4,1fr);gap:13px;margin-top:30px}.growth-launch-plan article{min-height:190px;padding:19px;display:flex;flex-direction:column;gap:11px;border:1px solid #dce4ef;border-radius:9px}.growth-launch-plan small{margin-top:auto;color:#8793a6}.growth-launch-plan strong{font-size:17px}.growth-launch-head{display:flex;align-items:center;justify-content:space-between;gap:18px}.growth-launch-target{padding:10px 13px;border:1px solid #dce4ef;border-radius:8px;background:#f9fbff}.growth-launch-target b{color:var(--launch-blue)}.growth-launch-rail{margin:20px 0 16px;display:grid;grid-template-columns:repeat(4,1fr);border:1px solid #dce4ee;border-radius:9px;overflow:hidden}.growth-launch-step{min-height:205px;padding:20px;border-right:1px solid #edf0f5}.growth-launch-step:last-child{border:0}.growth-launch-step.is-current{background:#f7faff}.growth-launch-step h3{margin:0 0 20px;font-size:14px}.growth-launch-step h3 span{display:inline-grid;place-items:center;width:34px;height:34px;margin-right:9px;border-radius:50%;background:#edf0f5}.growth-launch-step.is-current h3{color:var(--launch-blue)}.growth-launch-step.is-current h3 span{color:#fff;background:var(--launch-blue)}.growth-launch-step small{color:#7e899d}.growth-launch-step strong{display:block;margin:4px 0 18px;font-size:18px}.growth-launch-step p{margin:7px 0;color:#6e7b91;font-size:11px}.growth-launch-step p.is-met{color:#159567}.growth-launch-grid{display:grid;grid-template-columns:minmax(0,1fr) 310px;gap:16px}.growth-launch-board,.growth-launch-ai{padding:20px;border:1px solid #dfe6ef;border-radius:9px}.growth-launch-board h2,.growth-launch-ai h2{margin:0 0 18px;font-size:17px}.growth-launch-facts{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-top:17px}.growth-launch-facts article{padding:12px;border:1px solid #e1e7ef;border-radius:8px;background:#fafbfd}.growth-launch-facts small{color:#718097}.growth-launch-facts strong{display:block;margin:5px 0;font-size:19px}.growth-launch-facts span{color:#159567;font-size:11px}.growth-launch-facts article:nth-child(n+4) span{color:#d78308}.growth-launch-rounds{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:14px}.growth-launch-rounds article{min-height:160px;padding:14px;border:1px solid #dce4ed;border-radius:8px}.growth-launch-rounds article.is-current{border-color:#4d7cf0;box-shadow:0 0 0 1px #4d7cf0 inset}.growth-launch-rounds h3{margin:0 0 10px;font-size:13px}.growth-launch-rounds p{margin:7px 0;color:#69768b;font-size:11px}.growth-launch-ai h3{margin:7px 0;font-size:22px}.growth-launch-ai>p{color:#65748b;font-size:12px;line-height:1.65}.growth-launch-ai .growth-launch-primary{width:100%;margin:12px 0}.growth-launch-safe{margin-top:16px;padding:11px;border:1px solid #e3e9f2;border-radius:8px;background:#f8faff;color:#64738a;font-size:11px}.growth-launch-audit{margin-top:14px;padding:12px;border-radius:8px;background:#f5f7fb;color:#64738a;font-size:11px}.growth-launch-audit p{margin:4px 0}.growth-launch-toast{position:fixed;left:50%;bottom:24px;z-index:1700;transform:translateX(-50%);padding:11px 17px;border-radius:8px;background:#22324d;color:#fff;box-shadow:0 12px 30px rgba(20,33,57,.24)}@media(max-width:1050px){.growth-launch-shell{grid-template-columns:170px 1fr}.growth-launch-grid{grid-template-columns:1fr}.growth-launch-plan{grid-template-columns:1fr 1fr}.growth-launch-facts{grid-template-columns:repeat(3,1fr)}}@media(max-width:760px){.growth-launch-shell{display:block}.growth-launch-nav{display:flex;padding:8px;position:sticky;top:0;z-index:2;background:#f7f9fd}.growth-launch-brand{display:none}.growth-launch-main{padding:8px}.growth-launch-page{padding:20px}.growth-launch-routes,.growth-launch-form,.growth-launch-plan,.growth-launch-rail,.growth-launch-rounds{grid-template-columns:1fr}.growth-launch-step{border-right:0;border-bottom:1px solid #edf0f5}.growth-launch-facts{grid-template-columns:1fr 1fr}}
    `;
    document.head.appendChild(style);
    const launchPolish = document.createElement('style');
    launchPolish.textContent = `
      .growth-delete-modes{display:grid;gap:9px}.growth-delete-mode{display:grid;grid-template-columns:18px minmax(0,1fr);gap:10px;padding:12px;border:1px solid #dfe5ee;border-radius:8px;color:#344054;cursor:pointer}.growth-delete-mode:has(input:checked){border-color:#6f91ee;background:#f7f9ff}.growth-delete-mode span{display:grid;gap:3px;font-size:12px;font-weight:750}.growth-delete-mode small{color:#667085;font-size:10px;font-weight:500}.growth-meta-delete-preview{margin-top:12px;padding:12px;border:1px solid #f2c7c2;border-radius:8px;background:#fff8f7;color:#7a271a;font-size:11px;line-height:1.55}.growth-meta-delete-preview.is-ready{border-color:#f0b7af;background:#fff5f3}.growth-meta-delete-ack{display:flex;align-items:flex-start;gap:8px;margin-top:10px;color:#7a271a;font-size:11px;font-weight:750}
      .growth-launch-layer{--launch-blue:#3569e8;--launch-ink:#101828;--launch-muted:#667085;--launch-line:#e3e8ef;--launch-line-strong:#cbd5e1;--launch-surface:#fff;--launch-surface-2:#fafbfc;--launch-shadow:0 1px 2px rgba(16,24,40,.04),0 10px 24px rgba(16,24,40,.045);background:#f6f7f9!important;color:var(--launch-ink)!important}
      .growth-launch-shell{display:block!important;min-height:100vh!important}.growth-launch-main{min-width:0!important;box-sizing:border-box!important;padding:22px 28px 52px!important;background:#f6f7f9!important}
      .growth-launch-page{width:min(1180px,100%)!important;max-width:none!important;min-height:0!important;box-sizing:border-box!important;margin:0 auto!important;padding:18px 20px 26px!important;border:1px solid var(--launch-line)!important;border-radius:8px!important;background:var(--launch-surface)!important;box-shadow:var(--launch-shadow)!important}
      .growth-launch-topbar{display:grid;grid-template-columns:auto 1fr auto;align-items:center;gap:14px;margin:0 0 24px;padding:0 0 16px;border-bottom:1px solid var(--launch-line)}
      #growthLaunchPanel .growth-launch-back-dashboard{min-height:36px!important;margin:0!important;padding:0 12px!important;border:1px solid var(--launch-line-strong)!important;border-radius:8px!important;background:#fff!important;color:#344054!important;box-shadow:none!important;font:750 12px/1 Inter,"PingFang SC","Microsoft YaHei",sans-serif!important;cursor:pointer!important;transform:none!important}
      #growthLaunchPanel .growth-launch-back-dashboard:hover{border-color:#98a2b3!important;background:#f9fafb!important;color:#101828!important}.growth-launch-breadcrumb{display:grid;gap:2px;color:#101828;font-size:13px;font-weight:800}.growth-launch-breadcrumb small{color:#667085;font-size:10px;font-weight:500}.growth-launch-state{display:inline-flex;align-items:center;gap:7px;padding:5px 9px;border-radius:999px;background:#ecfdf3;color:#047857;font-size:10px;font-weight:750}.growth-launch-state:before{content:'';width:6px;height:6px;border-radius:50%;background:#10b981}
      #growthLaunchPanel .growth-launch-back-dashboard:focus-visible,#growthLaunchPanel .growth-launch-routes button:focus-visible,#growthLaunchPanel .growth-launch-primary:focus-visible,#growthLaunchPanel .growth-launch-secondary:focus-visible{outline:3px solid rgba(53,105,232,.25)!important;outline-offset:2px!important}
      .growth-launch-page h1{max-width:760px;margin:7px 0 8px!important;color:#101828!important;font-size:24px!important;line-height:1.25!important;letter-spacing:-.02em!important}.growth-launch-page>p,.growth-route-hero>p{max-width:720px;margin:0!important;color:#667085!important;font-size:13px!important;line-height:1.6!important}.growth-launch-kicker{color:#3569e8!important;font-size:11px!important;font-weight:850!important;letter-spacing:.04em!important}
      .growth-route-question{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;margin-top:28px}.growth-route-question h2{margin:0;color:#344054;font-size:14px;font-weight:750}.growth-route-question span{color:#7a8494;font-size:11px}
      .growth-launch-routes{grid-template-columns:repeat(2,minmax(0,1fr))!important;gap:14px!important;margin-top:12px!important}
      #growthLaunchPanel .growth-launch-routes button{position:relative!important;min-height:300px!important;margin:0!important;padding:20px!important;display:flex!important;flex-direction:column!important;align-items:stretch!important;gap:0!important;overflow:hidden!important;border:1px solid var(--launch-line)!important;border-radius:8px!important;background:#fff!important;color:#101828!important;box-shadow:0 1px 2px rgba(16,24,40,.03)!important;text-align:left!important;transform:none!important;transition:border-color .16s ease,box-shadow .16s ease!important}
      #growthLaunchPanel .growth-launch-routes button:hover{border-color:#98a2b3!important;background:#fff!important;color:#101828!important;box-shadow:var(--launch-shadow)!important;transform:none!important}.growth-route-card-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.growth-route-index{color:#98a2b3;font:800 11px/1 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.08em}.growth-route-tag{padding:5px 8px;border:1px solid var(--launch-line);border-radius:999px;background:#f8fafc;color:#5f6b7a;font-size:10px;font-weight:750}.growth-launch-routes button:last-child .growth-route-tag{border-color:#bbf7d0;background:#f0fdf4;color:#047857}
      .growth-launch-routes b{display:block!important;margin:24px 0 8px!important;color:#101828!important;font-size:19px!important;line-height:1.25!important}.growth-launch-routes p{min-height:44px;margin:0!important;color:#667085!important;font-size:12px!important;line-height:1.6!important}.growth-launch-routes ul{margin:18px 0 22px;padding:15px 0 0;border-top:1px solid #edf0f4;list-style:none}.growth-launch-routes li{position:relative;margin:7px 0;padding-left:15px;color:#475467;font-size:11px}.growth-launch-routes li:before{content:'';position:absolute;left:0;top:.52em;width:5px;height:5px;border-radius:50%;background:#94a3b8}.growth-route-outcome{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:auto}.growth-route-outcome small{color:#7a8494;font-size:10px}.growth-launch-routes em{margin:0!important;color:#3569e8!important;font-size:11px!important;font-style:normal!important;font-weight:800!important}
      .growth-launch-shared{margin-top:24px;padding:20px;border:1px solid var(--launch-line);border-radius:8px;background:var(--launch-surface-2)}.growth-launch-shared header{display:flex;align-items:baseline;justify-content:space-between;gap:12px}.growth-launch-shared h2{margin:0;color:#344054;font-size:13px}.growth-launch-shared header span{color:#7a8494;font-size:10px}.growth-launch-shared ol{display:grid;grid-template-columns:repeat(5,1fr);gap:0;margin:16px 0 0;padding:0;list-style:none;counter-reset:launchflow}.growth-launch-shared li{position:relative;padding:0 18px 0 30px;color:#475467;font-size:11px;font-weight:650}.growth-launch-shared li:before{counter-increment:launchflow;content:counter(launchflow);position:absolute;left:0;top:-3px;display:grid;place-items:center;width:20px;height:20px;border:1px solid #cbd5e1;border-radius:50%;background:#fff;color:#64748b;font:750 9px/1 Inter,sans-serif}.growth-launch-shared li:not(:last-child):after{content:'';position:absolute;right:5px;top:7px;width:10px;border-top:1px solid #cbd5e1}
      #growthLaunchPanel .growth-launch-form input,#growthLaunchPanel .growth-launch-form select{width:100%!important;height:44px!important;box-sizing:border-box!important;margin:0!important;padding:0 12px!important;border:1px solid var(--launch-line-strong)!important;border-radius:8px!important;background:#fff!important;color:#101828!important;box-shadow:none!important}.growth-launch-form input:focus,.growth-launch-form select:focus{border-color:#3569e8!important;box-shadow:0 0 0 3px rgba(53,105,232,.12)!important}.growth-launch-form{margin-top:28px!important}.growth-launch-form label{font-size:12px!important;color:#475467!important}
      #growthLaunchPanel .growth-launch-primary,#growthLaunchPanel .growth-launch-actions .growth-launch-primary{background:#3569e8!important;border-color:#3569e8!important;color:#fff!important;box-shadow:none!important}#growthLaunchPanel .growth-launch-primary:hover{background:#2859d4!important;border-color:#2859d4!important;transform:none!important}#growthLaunchPanel .growth-launch-secondary{background:#fff!important;border-color:var(--launch-line-strong)!important;color:#344054!important;box-shadow:none!important}#growthLaunchPanel .growth-launch-secondary:hover{background:#f8fafc!important;border-color:#98a2b3!important;transform:none!important}
      .growth-launch-delivery{margin-top:18px;overflow:hidden;border:1px solid #d8e0ec;border-radius:10px;background:#fff}.growth-launch-delivery>header{display:flex;align-items:flex-start;justify-content:space-between;gap:18px;padding:16px 18px;border-bottom:1px solid #e8edf4;background:#f8faff}.growth-launch-delivery>header div{display:grid;gap:4px}.growth-launch-delivery>header b{color:#17233d;font-size:14px}.growth-launch-delivery>header span{color:#667085;font-size:11px}.growth-launch-delivery>header strong{flex:0 0 auto;color:#315fd8;font-size:11px}.growth-launch-delivery-list{display:grid}.growth-launch-delivery-row{display:grid;grid-template-columns:minmax(0,1fr) auto auto;align-items:center;gap:16px;padding:13px 18px;border-bottom:1px solid #edf1f5}.growth-launch-delivery-row:last-child{border-bottom:0}.growth-launch-delivery-copy{min-width:0;display:grid;gap:3px}.growth-launch-delivery-copy b{overflow:hidden;color:#24324a;font-size:13px;text-overflow:ellipsis;white-space:nowrap}.growth-launch-delivery-copy small{overflow:hidden;color:#7a8799;font:10px/1.35 ui-monospace,SFMono-Regular,Menlo,monospace;text-overflow:ellipsis;white-space:nowrap}.growth-launch-delivery-status{padding:5px 8px;border-radius:999px;background:#f2f4f7;color:#667085;font-size:10px;font-weight:800}.growth-launch-delivery-status.is-running{background:#ecfdf3;color:#047857}.growth-launch-delivery-status.is-pending{background:#fff7e8;color:#9a6200}#growthLaunchPanel .growth-launch-delivery-row button{min-width:92px!important;min-height:36px!important;margin:0!important;padding:0 12px!important;border-radius:7px!important}.growth-launch-delivery-note{padding:11px 18px;border-top:1px solid #dbe6fa;background:#f1f6ff;color:#53637c;font-size:10px;line-height:1.55}.growth-launch-rate-limit{display:flex;align-items:center;gap:10px;padding:10px 16px;border-bottom:1px solid #f1d79a;background:#fff9eb;color:#7c4a03}.growth-launch-rate-limit.is-checking{border-bottom-color:#dbe5f6;background:#f6f8fc;color:#53627a}.growth-launch-rate-limit>div{display:grid;gap:2px;min-width:0}.growth-launch-rate-limit b{font-size:11px}.growth-launch-rate-limit small{font-size:10px;line-height:1.45}.growth-launch-rate-dot{width:7px;height:7px;flex:0 0 auto;border-radius:50%;background:#d99116}.growth-launch-rate-limit.is-checking .growth-launch-rate-dot{background:#7a8799}
      .growth-launch-analysis{margin-bottom:14px;border:1px solid #dfe6f0;border-radius:8px;background:#fff}.growth-launch-analysis-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;padding:14px 16px;border-bottom:1px solid #edf1f5}.growth-launch-analysis-head>div{display:grid;gap:3px}.growth-launch-analysis-head b{color:#17233d;font-size:14px}.growth-launch-analysis-head span{color:#667085;font-size:10px}.growth-launch-analysis-badge{flex:0 0 auto;padding:5px 8px;border-radius:999px;background:#fff7e8;color:#9a6200;font-size:10px;font-weight:800}.growth-launch-analysis-badge.is-ready{background:#ecfdf3;color:#047857}.growth-launch-analysis-badge.is-empty{background:#f2f4f7;color:#667085}.growth-launch-metrics{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));border-bottom:1px solid #edf1f5}.growth-launch-metric{min-width:0;padding:12px 14px}.growth-launch-metric+.growth-launch-metric{border-left:1px solid #edf1f5}.growth-launch-metric small{display:block;margin-bottom:4px;color:#7a8799;font-size:9px}.growth-launch-metric strong{display:block;color:#24324a;font-size:15px;line-height:1.2}.growth-launch-metric em{display:block;margin-top:4px;color:#7a8799;font-size:9px;font-style:normal}.growth-launch-metric.is-alert strong,.growth-launch-metric.is-alert em{color:#b42318}.growth-launch-analysis-note{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.2fr);gap:16px;padding:12px 16px;background:#fafbfd}.growth-launch-analysis-note div{display:grid;gap:3px}.growth-launch-analysis-note b{color:#344054;font-size:11px}.growth-launch-analysis-note span{color:#667085;font-size:10px;line-height:1.5}.growth-launch-delivery-copy .growth-launch-row-metrics{font:10px/1.45 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#667085;white-space:normal}.growth-launch-no-data{padding:18px 16px;color:#667085;font-size:11px;line-height:1.55}.growth-launch-revisions{margin-bottom:14px;padding:14px 16px;border:1px solid #dfe6f0;border-radius:8px;background:#fff}.growth-launch-revisions>header{display:flex;justify-content:space-between;gap:12px;margin-bottom:10px}.growth-launch-revisions>header b{color:#17233d;font-size:13px}.growth-launch-revisions>header span{color:#667085;font-size:10px}.growth-launch-revision-grid{display:grid;gap:8px}.growth-launch-revision-card{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;padding:10px 12px;border:1px solid #edf1f5;border-radius:7px}.growth-launch-revision-card b{font-size:11px}.growth-launch-revision-card span,.growth-launch-revision-card small{color:#667085;font-size:10px}.growth-launch-revision-card strong{color:#344054;font-size:10px}.growth-launch-revision-history{margin-top:8px;color:#667085;font-size:10px}.growth-launch-revision-history summary{cursor:pointer;font-weight:750}
      @media(max-width:980px){.growth-launch-main{padding:16px!important}.growth-launch-routes{grid-template-columns:1fr!important}.growth-launch-shared ol{grid-template-columns:1fr 1fr;gap:14px}.growth-launch-shared li:after{display:none}}
      @media(max-width:700px){.growth-launch-main{padding:0!important}.growth-launch-page{width:100%!important;padding:14px 14px 24px!important;border-width:0 0 1px!important;border-radius:0!important}.growth-launch-topbar{position:sticky;top:0;z-index:3;grid-template-columns:auto 1fr;padding:10px 0 12px;background:#fff}.growth-launch-breadcrumb{display:none}.growth-launch-state{justify-self:end}.growth-launch-page h1{font-size:22px!important}.growth-route-question{align-items:flex-start;flex-direction:column;margin-top:26px}.growth-launch-shared ol{grid-template-columns:1fr}.growth-launch-form{grid-template-columns:1fr!important}.growth-launch-actions{grid-column:auto!important}.growth-launch-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.growth-launch-metric+.growth-launch-metric{border-left:0}.growth-launch-metric:nth-child(even){border-left:1px solid #edf1f5}.growth-launch-metric:nth-child(n+3){border-top:1px solid #edf1f5}.growth-launch-metric:last-child{grid-column:1/-1}.growth-launch-analysis-note{grid-template-columns:1fr}.growth-launch-delivery>header{flex-direction:column}.growth-launch-delivery-row{grid-template-columns:minmax(0,1fr) auto}.growth-launch-delivery-row button{grid-column:1/-1;width:100%!important}}
    `;
    document.head.appendChild(launchPolish);
    launchPolish.sheet?.insertRule('.growth-launch-delivery-status.is-rejected{background:#fff0ee;color:#b42318}');
    const batchReviewPolish = document.createElement('style');
    batchReviewPolish.textContent = `
      .growth-modal.growth-batch-review-modal{width:min(1180px,calc(100vw - 28px));max-height:calc(100vh - 28px);display:flex;overflow:hidden;flex-direction:column}.growth-batch-review-modal .growth-modal-head{min-height:60px;flex:0 0 auto;padding:0 22px}.growth-batch-review-modal .growth-modal-head b{font-size:18px;letter-spacing:-.02em}.growth-batch-review-modal .growth-modal-head small{margin-top:2px;font-size:11px}.growth-batch-review-modal .growth-modal-body{min-height:0;flex:1 1 auto;overflow:auto;padding:12px 22px}.growth-batch-review-modal .growth-modal-foot{min-height:60px;flex:0 0 auto;padding:0 22px;justify-content:flex-end;background:#fff}
      .growth-batch-review-summary{display:grid;grid-template-columns:minmax(280px,1fr) minmax(0,1.35fr);align-items:end;gap:16px;margin-bottom:10px;padding:10px 12px;border:1px solid #dce3ed;border-radius:9px;background:#fff}.growth-batch-review-summary>section{min-width:0}.growth-batch-review-summary label{display:block;margin-bottom:5px;color:#667085;font-size:10px;font-weight:750}.growth-batch-review-summary input{width:100%;height:36px;box-sizing:border-box;padding:0 10px;border:1px solid #cfd8e6;border-radius:7px;background:#fff;color:#17233d;font:700 13px/1 inherit}.growth-batch-review-meta{display:flex;align-items:center;justify-content:flex-end;gap:6px;min-height:36px;flex-wrap:wrap}.growth-batch-review-meta span{padding:6px 9px;border-radius:999px;background:#f3f6fa;color:#52617a;font-size:10px;font-weight:700;white-space:nowrap}.growth-batch-review-meta span:last-child{background:#edf7f2;color:#167456}
      .growth-batch-review-list{display:grid;gap:8px}.growth-batch-experiment{display:grid;grid-template-areas:'direction direction' 'group ad';grid-template-columns:250px minmax(0,1fr);overflow:hidden;border:1px solid #dce3ed;border-radius:9px;background:#fff;transition:border-color .16s ease,box-shadow .16s ease,background-color .16s ease}.growth-batch-experiment.is-baseline{border-color:#5f86ef;background:#fbfcff;box-shadow:0 0 0 1px rgba(53,105,232,.12)}.growth-batch-experiment>section{min-width:0;padding:10px 12px}.growth-batch-direction{grid-area:direction;display:grid;grid-template-columns:minmax(180px,auto) minmax(0,1fr);gap:14px;align-items:center;border-bottom:1px solid #e7ebf1}.growth-batch-direction label{display:grid;grid-template-columns:20px minmax(0,1fr);gap:8px;align-items:center;cursor:pointer}.growth-batch-direction input{width:18px;height:18px;margin:0;accent-color:#3569e8}.growth-batch-direction b{display:inline;color:#17233d;font-size:13px}.growth-batch-direction em{display:inline-block;margin-left:7px;padding:3px 7px;border-radius:999px;background:#f1f3f6;color:#667085;font-size:9px;font-style:normal;font-weight:800}.growth-batch-experiment.is-baseline .growth-batch-direction em{background:#eaf0ff;color:#315fd8}.growth-batch-direction p{overflow:hidden;margin:0;color:#667085;font-size:10px;line-height:1.45;text-align:right;text-overflow:ellipsis;white-space:nowrap}.growth-batch-group-fields{grid-area:group;border-right:1px solid #e7ebf1}.growth-batch-ad-fields{grid-area:ad}.growth-batch-group-fields,.growth-batch-ad-fields{display:grid;gap:8px}.growth-batch-group-fields label,.growth-batch-ad-fields label{display:grid;gap:4px;color:#667085;font-size:10px;font-weight:750}.growth-batch-ad-fields{grid-template-columns:minmax(180px,.9fr) minmax(180px,.9fr)}.growth-batch-ad-fields label.wide{grid-column:1/-1}.growth-batch-group-fields input,.growth-batch-ad-fields input,.growth-batch-ad-fields textarea{width:100%;min-width:0;box-sizing:border-box;border:1px solid #cfd8e6;border-radius:7px;background:#fff;color:#17233d;font:500 12px/1.45 Inter,"PingFang SC","Microsoft YaHei",sans-serif}.growth-batch-group-fields input,.growth-batch-ad-fields input{height:36px;padding:0 10px}.growth-batch-ad-fields textarea{min-height:52px;padding:8px 10px;resize:vertical}.growth-batch-group-fields input:focus,.growth-batch-ad-fields input:focus,.growth-batch-ad-fields textarea:focus,.growth-batch-review-summary input:focus{outline:0;border-color:#3569e8;box-shadow:0 0 0 3px rgba(53,105,232,.12)}.growth-batch-foot-actions{display:flex;flex:0 0 auto;gap:9px}
      @media(max-width:860px){.growth-batch-review-summary{grid-template-columns:1fr}.growth-batch-review-meta{justify-content:flex-start}.growth-batch-experiment{grid-template-columns:220px minmax(0,1fr)}.growth-batch-ad-fields{grid-template-columns:1fr}.growth-batch-ad-fields label.wide{grid-column:auto}}
      @media(max-width:700px){.growth-modal.growth-batch-review-modal{width:100vw;max-height:100vh;border-radius:0}.growth-batch-review-modal .growth-modal-head,.growth-batch-review-modal .growth-modal-body,.growth-batch-review-modal .growth-modal-foot{padding-right:14px;padding-left:14px}.growth-batch-experiment{display:block}.growth-batch-direction{display:block}.growth-batch-direction p{margin-top:7px;text-align:left;white-space:normal}.growth-batch-group-fields{border-right:0;border-bottom:1px solid #e7ebf1}.growth-batch-review-modal .growth-modal-foot{padding-top:10px;padding-bottom:10px}.growth-batch-foot-actions{display:grid;width:100%;grid-template-columns:1fr 1.5fr}.growth-batch-foot-actions button{width:100%!important}}
    `;
    document.head.appendChild(batchReviewPolish);
    const integratedShell = document.createElement('style');
    integratedShell.textContent = `
      .growth-layer,.growth-launch-layer{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
      .growth-drawer{width:min(720px,calc(100vw - 180px));background:#fff;box-shadow:-10px 0 28px rgba(16,24,40,.14)}
      .growth-detail{background:#f6f7f9;padding:18px 20px 24px}.growth-detail>.growth-detail-head,.growth-detail>.growth-stepper,.growth-detail>.growth-phase,.growth-detail>.growth-section,.growth-detail>.growth-actions,.growth-detail>#growthActionArea,.growth-detail>.growth-technical{max-width:680px;margin-left:auto;margin-right:auto}
      .growth-detail>.growth-section{margin-top:12px;padding:16px;border:1px solid #e3e8ef;border-radius:8px;background:#fff}.growth-detail>.growth-section h3{margin-bottom:10px}
      .growth-actions{min-height:72px;margin-top:12px;padding:0 14px;border:1px solid #e3e8ef;border-radius:8px;background:#fff}.growth-technical{margin-top:12px;border:1px solid #e3e8ef;border-radius:8px;background:#fff}.growth-technical summary{padding:14px 16px}.growth-technical .growth-timeline{padding:0 16px 14px}
      .growth-footer{min-height:44px;background:#fff;color:#7a8494}
      .growth-launch-layer{position:fixed!important;inset:0!important;z-index:1600!important;overflow:hidden!important;background:transparent!important}.growth-launch-layer[hidden]{display:none!important}.growth-launch-backdrop{position:absolute;inset:0;background:rgba(20,32,55,.18)}
      .growth-launch-drawer{position:absolute;inset:0 0 0 auto;width:min(720px,calc(100vw - 180px));overflow:auto;border-left:1px solid #d9e1ec;background:#f6f7f9;box-shadow:-10px 0 28px rgba(16,24,40,.14)}
      .growth-launch-shell,.growth-launch-main{min-height:100%!important}.growth-launch-main{padding:0!important;background:#f6f7f9!important}.growth-launch-page{width:100%!important;min-height:100vh!important;margin:0!important;padding:0 20px 28px!important;border:0!important;border-radius:0!important;background:#f6f7f9!important;box-shadow:none!important}
      .growth-launch-topbar{position:sticky;top:0;z-index:4;grid-template-columns:36px minmax(0,1fr) auto!important;min-height:64px;margin:0 -20px 18px!important;padding:0 20px!important;border-bottom:1px solid #e3e8ef!important;background:#fff!important}.growth-launch-breadcrumb{font-size:16px}.growth-launch-breadcrumb small{margin-top:2px;font-size:11px}.growth-launch-top-actions{display:flex;align-items:center;gap:4px}#growthLaunchPanel .growth-launch-orders-link{min-height:34px!important;padding:0 10px!important;border:0!important;background:transparent!important;color:#3569e8!important;font-size:11px!important}
      .growth-launch-topbar.is-root{grid-template-columns:minmax(0,1fr) auto!important}#growthLaunchPanel .growth-launch-topbar.is-root .growth-launch-breadcrumb{grid-column:1!important}#growthLaunchPanel .growth-launch-topbar.is-root .growth-launch-top-actions{grid-column:2!important}
      #growthLaunchPanel .growth-launch-back-dashboard,#growthLaunchPanel .growth-launch-close{width:36px!important;height:36px!important;min-height:36px!important;padding:0!important;border:0!important;border-radius:8px!important;background:transparent!important;color:#667085!important;font-size:19px!important}.growth-launch-close{cursor:pointer}.growth-launch-close:hover,#growthLaunchPanel .growth-launch-back-dashboard:hover{background:#f1f4f8!important}
      #growthWorkspacePanel .growth-nav-back,#growthLaunchPanel .growth-nav-back{display:inline-flex!important;align-items:center!important;justify-content:center!important;width:36px!important;height:36px!important;min-height:36px!important;padding:0!important;border:0!important;border-radius:8px!important;background:transparent!important;color:#667085!important;font:600 18px/1 Inter,"PingFang SC","Microsoft YaHei",sans-serif!important;cursor:pointer!important}
      #growthWorkspacePanel .growth-nav-back[hidden],#growthLaunchPanel .growth-nav-back[hidden]{visibility:hidden!important;pointer-events:none!important}
      #growthWorkspacePanel .growth-drawer-head>.growth-nav-back,#growthLaunchPanel .growth-launch-topbar>.growth-nav-back{grid-column:1!important}#growthWorkspacePanel .growth-drawer-head>div,#growthLaunchPanel .growth-launch-breadcrumb{grid-column:2!important}#growthWorkspacePanel .growth-drawer-head>.growth-icon-button,#growthLaunchPanel .growth-launch-top-actions{grid-column:3!important}
      .growth-route-hero h1,.growth-launch-page h1{margin:4px 0 6px!important;font-size:22px!important}.growth-route-hero>p,.growth-launch-page>p{font-size:12px!important;line-height:1.55!important}
      .growth-launch-routes{grid-template-columns:1fr!important;gap:10px!important;margin-top:18px!important}#growthLaunchPanel .growth-launch-routes button{min-height:138px!important;padding:16px!important}.growth-launch-routes b{margin:12px 0 5px!important;font-size:17px!important}.growth-launch-routes p{min-height:0!important}.growth-launch-routes ul{display:none}.growth-route-outcome{margin-top:14px!important;padding-top:12px;border-top:1px solid #edf0f4}
      .growth-launch-shared{display:flex;align-items:center;gap:12px;margin-top:12px!important;padding:13px 15px!important}.growth-launch-shared strong{font-size:12px}.growth-launch-shared span{color:#667085;font-size:11px}
      .growth-launch-product{display:grid;grid-template-columns:auto 1fr;align-items:center;gap:2px 12px;margin-top:16px;padding:14px 16px;border:1px solid #dbe6fa;border-radius:8px;background:#f4f7ff}.growth-launch-product>span{grid-row:1/3;color:#667085;font-size:11px}.growth-launch-product>b{font-size:16px}.growth-launch-product>small{color:#667085}
      .growth-launch-account{margin-top:12px;padding:14px 16px;border:1px solid #e3e8ef;border-radius:8px;background:#fff}.growth-launch-account header{display:flex;align-items:center;justify-content:space-between;gap:12px}.growth-launch-account header>div{display:grid;gap:2px}.growth-launch-account header small{color:#667085;font-size:10px}.growth-launch-account label{display:grid;gap:5px;margin-top:12px;color:#475467;font-size:11px;font-weight:700}.growth-launch-account select{width:100%;height:40px;margin:0;padding:0 10px;border:1px solid #cbd5e1;border-radius:8px;background:#fff;color:#101828}.growth-launch-account-empty{display:grid;gap:2px;margin-top:10px;padding:10px 12px;border-radius:7px;background:#f8fafc;color:#667085;font-size:11px}.growth-launch-account-empty b{color:#344054}
      .growth-launch-config-error{display:grid;gap:3px;margin-top:12px;padding:11px 13px;border:1px solid #fecaca;border-radius:8px;background:#fff5f5;color:#9f1d1d;font-size:11px}
      .growth-launch-goal-form{max-width:none!important;grid-template-columns:repeat(3,minmax(0,1fr))!important;gap:10px!important;margin-top:12px!important;padding:16px;border:1px solid #e3e8ef;border-radius:8px;background:#fff}.growth-launch-goal-form .growth-launch-actions{grid-column:1/-1!important;margin-top:4px}.growth-launch-audience-lock{grid-column:1/-1;display:grid;grid-template-columns:160px 1fr;gap:3px 14px;padding:14px 16px;border:1px solid #dbe5ff;border-radius:8px;background:#f5f8ff}.growth-launch-audience-lock span{grid-row:1/3;color:#667085;font-size:12px}.growth-launch-audience-lock b{font-size:14px;color:#172033}.growth-launch-audience-lock small{color:#3f5f9e}.growth-launch-primary:disabled{opacity:.45;cursor:not-allowed}
      .growth-launch-plan{grid-template-columns:1fr 1fr!important;gap:10px!important;margin-top:16px!important}.growth-launch-plan article{min-height:132px!important;padding:15px!important;background:#fff}.growth-launch-plan strong{font-size:15px}.growth-launch-plan span,.growth-launch-plan small{font-size:11px}
      .growth-target-modal{width:min(520px,calc(100vw - 32px))}.growth-target-modal .growth-modal-head{min-height:62px}.growth-target-modal .growth-modal-body{padding:16px 20px 18px}.growth-target-modal .growth-modal-foot{min-height:62px}.growth-target-summary{display:grid;grid-template-columns:1fr 1fr;gap:9px}.growth-target-summary article{display:grid;gap:3px;min-height:74px;padding:12px 14px;border:1px solid #e2e7ef;border-radius:8px;background:#fbfcfe}.growth-target-summary small,.growth-target-detail small{color:#7b8799;font-size:9px;font-weight:750}.growth-target-summary strong{color:#17233d;font-size:16px}.growth-target-summary span{color:#667085;font-size:10px}.growth-target-details{margin-top:10px;overflow:hidden;border:1px solid #e2e7ef;border-radius:8px}.growth-target-detail{display:grid;grid-template-columns:82px minmax(0,1fr) auto;align-items:center;gap:10px;min-height:48px;padding:0 13px}.growth-target-detail+.growth-target-detail{border-top:1px solid #edf0f4}.growth-target-detail>b{color:#344054;font-size:11px}.growth-target-detail>span{overflow:hidden;color:#667085;font-size:11px;text-overflow:ellipsis;white-space:nowrap}.growth-target-detail>small{white-space:nowrap}.growth-target-problem{display:grid;gap:3px;margin-top:10px;padding:10px 12px;border-left:3px solid #d92d20;border-radius:0 7px 7px 0;background:#fff5f5;color:#7a271a}.growth-target-problem b{font-size:11px}.growth-target-problem span{font-size:10px;line-height:1.5}.growth-page-repair-context{display:grid;gap:9px}.growth-page-repair-note{padding:10px 12px;border-radius:7px;background:#f5f8ff;color:#475467;font-size:10px;line-height:1.55}.growth-page-repair-identity{display:grid;gap:8px;padding:12px;border:1px solid #e2e7ef;border-radius:8px}.growth-page-repair-identity label{display:grid;grid-template-columns:100px minmax(0,1fr);align-items:center;gap:10px;color:#667085;font-size:10px}.growth-page-repair-identity strong{color:#344054;font-size:11px}.growth-page-repair-identity select{width:100%;height:40px;padding:0 10px;border:1px solid #cbd5e1;border-radius:7px;background:#fff;color:#17233d}.growth-page-repair-error{padding:10px 12px;border:1px solid #fecaca;border-radius:7px;background:#fff5f5;color:#b42318;font-size:10px}
      .growth-direction-head{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;margin-top:18px}.growth-direction-head>div{display:grid;gap:3px}.growth-direction-head b{font-size:15px}.growth-direction-head small{color:#667085;font-size:11px}.growth-direction-head em{color:#3569e8;font-size:11px;font-style:normal;font-weight:750}.growth-direction-list{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px}.growth-direction-card{min-width:0;padding:14px;border:1px solid #dfe5ed;border-radius:9px;background:#fff;transition:border-color .16s ease,box-shadow .16s ease,background-color .16s ease}.growth-direction-card.is-selected{border-color:#7ea1ee;background:#fbfcff;box-shadow:0 0 0 1px rgba(53,105,232,.12)}.growth-direction-card:focus-within{border-color:#3569e8;box-shadow:0 0 0 3px rgba(53,105,232,.12)}.growth-frozen-creative{display:grid;grid-template-columns:18px 72px minmax(0,1fr);align-items:center;gap:10px;cursor:pointer}.growth-frozen-creative>input{width:16px!important;height:16px!important}.growth-frozen-creative>img{width:72px;height:88px;object-fit:cover;border-radius:6px;background:#f2f4f7}.growth-frozen-creative>span{display:grid;gap:4px}.growth-frozen-creative small{color:#667085}.growth-direction-select{display:grid;grid-template-columns:20px minmax(0,1fr) auto;gap:9px;align-items:start;cursor:pointer}.growth-direction-select>input{width:17px!important;height:17px!important;margin:2px 0 0!important}.growth-direction-title{display:grid;gap:2px}.growth-direction-title b{color:#17233d;font-size:14px}.growth-direction-title small{color:#667085;font-size:10px}.growth-direction-rank{padding:2px 6px;border-radius:4px;background:#eef3ff;color:#3157b7;font-size:9px;font-weight:750;white-space:nowrap}.growth-direction-summary{min-height:34px;margin:10px 0 7px;color:#475467;font-size:11px;line-height:1.55}.growth-direction-reason{display:block;min-height:30px;color:#7b8799;font-size:9px;line-height:1.5}.growth-direction-controls{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px;margin-top:10px;padding-top:10px;border-top:1px solid #edf0f4}.growth-direction-budget{display:flex;align-items:center;gap:7px;color:#667085;font-size:10px}.growth-direction-budget input{width:62px;height:32px;padding:0 7px;box-sizing:border-box;border:1px solid #cbd5e1;border-radius:6px;background:#fff;color:#101828}.growth-direction-details{min-width:0}.growth-direction-details summary{color:#3569e8;font-size:10px;font-weight:750;cursor:pointer;list-style:none}.growth-direction-details summary::-webkit-details-marker{display:none}.growth-direction-details[open]{width:100%;margin-top:4px}.growth-direction-details textarea{width:100%;min-height:68px;margin-top:8px;padding:9px;box-sizing:border-box;border:1px solid #cbd5e1;border-radius:7px;background:#fff;color:#101828;resize:vertical;line-height:1.5}.growth-naming-preview{margin-top:12px;border-top:1px solid #e3e8ef}.growth-naming-preview summary{padding:11px 0;color:#475467;font-size:11px;font-weight:750;cursor:pointer}.growth-naming-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;padding-bottom:11px}.growth-naming-grid>div{min-width:0;padding:8px 10px;background:#f7f8fa}.growth-naming-grid span{display:block;margin-bottom:3px;color:#667085;font-size:9px}.growth-naming-grid code{display:block;overflow:hidden;color:#344054;font-size:10px;text-overflow:ellipsis;white-space:nowrap}.growth-history-note{display:grid;grid-template-columns:auto repeat(4,1fr);align-items:center;gap:0;margin-top:12px;border:1px solid #e3e8ef;border-radius:8px;background:#fff}.growth-history-note>b{padding:11px 13px;color:#344054;font-size:10px}.growth-history-note>span{padding:9px 11px;border-left:1px solid #edf0f4}.growth-history-note small{display:block;color:#98a2b3;font-size:9px}.growth-history-note strong{display:block;margin-top:2px;color:#344054;font-size:11px}.growth-launch-actions.is-sticky{position:sticky;bottom:0;z-index:2;margin:18px -16px -16px;padding:12px 16px;border-top:1px solid #e3e8ef;background:rgba(255,255,255,.96);backdrop-filter:blur(8px)}
      .growth-launch-rail{grid-template-columns:1fr 1fr!important}.growth-launch-step{min-height:150px!important}.growth-launch-grid{grid-template-columns:1fr!important}.growth-launch-facts{grid-template-columns:repeat(5,minmax(0,1fr))!important;gap:7px!important}.growth-launch-rounds{grid-template-columns:1fr!important}.growth-launch-rounds article{min-height:0!important}
      .growth-launch-progress-card{margin:16px 0;padding:15px;border:1px solid #dbe4f1;border-radius:9px;background:#f8faff}.growth-launch-progress-head,.growth-launch-progress-counts{display:flex;align-items:center;justify-content:space-between;gap:12px}.growth-launch-progress-head b{font-size:13px}.growth-launch-progress-head span,.growth-launch-progress-counts{color:#667085;font-size:11px}.growth-launch-progress-track{height:8px;margin:12px 0;border-radius:999px;background:#e4e9f2;overflow:hidden}.growth-launch-progress-track i{display:block;height:100%;border-radius:inherit;background:#3569e8;transition:width .2s ease}.growth-launch-progress-counts b{color:#344054}.growth-launch-progress-list{display:grid;gap:6px;margin-top:12px}.growth-launch-progress-item{display:flex;align-items:center;justify-content:space-between;gap:12px;padding-top:7px;border-top:1px solid #e8edf4;color:#475467;font-size:11px}.growth-launch-progress-item b{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.growth-launch-progress-item span{flex:0 0 auto;color:#667085}.growth-launch-progress-item span.is-ready{color:#079455}.growth-launch-progress-item span.is-failed{color:#d92d20}.growth-launch-background-note{margin:12px 0;padding:11px 12px;border-left:3px solid #3569e8;background:#f2f6ff;color:#475467;font-size:12px}.growth-launch-ai-actions{display:grid;gap:8px;margin-top:12px}.growth-launch-ai-actions .growth-launch-primary{margin:0!important}.growth-launch-ai-actions .growth-launch-secondary{width:100%!important}
      .growth-material-status-strip{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:16px;padding:10px 12px;border:1px solid #dbe5f5;border-radius:8px;background:#f7f9fd;color:#475467;font-size:11px}.growth-material-status-strip b{color:#26344d}.growth-material-status-strip.is-attention{border-color:#fecaca;background:#fff7f6;color:#b42318}.growth-material-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:14px}.growth-material-card{min-width:0;overflow:hidden;border:1px solid #dfe5ed;border-radius:9px;background:#fff}.growth-material-thumb{position:relative;display:block;width:100%;aspect-ratio:1/1;overflow:hidden;border:0;border-bottom:1px solid #e8edf4;background:#f2f4f7;cursor:pointer}.growth-material-thumb img{width:100%;height:100%;object-fit:cover}.growth-material-thumb-placeholder{display:grid;width:100%;height:100%;place-items:center;padding:18px;box-sizing:border-box;color:#98a2b3;font-size:11px;text-align:center}.growth-material-card-body{display:grid;gap:4px;padding:11px 12px}.growth-material-card-title{overflow:hidden;color:#26344d;font-size:11px;text-overflow:ellipsis;white-space:nowrap}.growth-material-card-meta{display:flex;align-items:center;justify-content:space-between;gap:8px;color:#98a2b3;font-size:9px}.growth-material-card-status{color:#667085;font-weight:750}.growth-material-card-status.is-ready{color:#079455}.growth-material-card-status.is-failed{color:#d92d20}.growth-material-card-actions{display:grid;grid-template-columns:1fr 1fr;gap:7px;padding:0 12px 12px}.growth-material-card-actions button{width:100%!important;min-height:34px!important;padding:0 8px!important;font-size:10px!important}.growth-material-card-actions .is-wide{grid-column:1/-1}.growth-material-preview{width:min(920px,calc(100vw - 32px))}.growth-material-preview-stage{display:grid;place-items:center;min-height:360px;padding:12px;border-radius:9px;background:#f3f5f8}.growth-material-preview-stage img{max-width:100%;max-height:65vh;object-fit:contain}.growth-material-preview .growth-modal-body{padding:16px}.growth-material-preview-copy{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:10px;color:#667085;font-size:11px}
      .growth-launch-cold-head{align-items:flex-end;padding:2px 0 18px;border-bottom:1px solid #e3e8ef}.growth-launch-cold-head h1{margin:0 0 5px!important}.growth-launch-cold-head .growth-launch-target{padding:0;border:0;border-radius:0;background:transparent;color:#667085;font-size:12px}.growth-launch-order-identity{display:grid;gap:4px}.growth-launch-order-identity>span{color:#667085;font-size:10px;font-weight:750}.growth-launch-order-identity h1{color:#17233d;font-size:22px!important;letter-spacing:-.01em}.growth-launch-order-identity p{margin:0;color:#667085;font-size:11px}.growth-launch-cold{padding:22px 0 0}.growth-launch-cold-status{display:grid;grid-template-columns:minmax(220px,.8fr) minmax(320px,1.2fr);align-items:start;gap:22px;padding:18px;border:1px solid #dfe6f1;border-radius:8px;background:#f8faff}.growth-launch-cold-status>div{display:grid;gap:6px}.growth-launch-cold-status .growth-launch-kicker{display:block}.growth-launch-cold-status h2{margin:0;font-size:18px;line-height:1.4}.growth-launch-cold-guidance{padding-left:20px;border-left:1px solid #dfe6f1}.growth-launch-cold-guidance b{color:#344054;font-size:11px}.growth-launch-cold-guidance p{margin:0;color:#667085;font-size:12px;line-height:1.65}.growth-launch-cold .growth-launch-progress-card{margin:18px 0 0;padding:0;border:0;border-radius:0;background:transparent}.growth-launch-cold .growth-launch-progress-list{margin-top:14px}.growth-launch-cold .growth-launch-progress-item{min-height:32px;padding:0;border-top:1px solid #edf0f4}.growth-launch-cold-actions{display:flex;justify-content:flex-end;margin-top:18px;padding-top:16px;border-top:1px solid #e3e8ef}.growth-launch-cold-actions .growth-launch-primary{min-width:210px}
      .growth-launch-delivery>header .growth-launch-delivery-actions{display:flex;align-items:center;justify-content:flex-end;gap:8px;flex-wrap:nowrap}.growth-launch-delivery-actions button{white-space:nowrap}
      .growth-launch-orders-head{display:flex;align-items:flex-start;justify-content:space-between;gap:18px;margin-bottom:16px}.growth-launch-orders-head h1{margin:4px 0 5px!important}.growth-launch-orders-head p{max-width:460px;margin:0;color:#667085;font-size:11px;line-height:1.55}.growth-launch-orders-head .growth-launch-primary{flex:0 0 auto;min-height:40px!important;padding:0 15px!important}.growth-launch-orders-summary{display:flex;align-items:center;margin-bottom:12px;border-bottom:1px solid #e3e8ef;color:#667085}#growthLaunchPanel .growth-launch-orders-summary button{position:relative!important;min-height:42px!important;padding:0 13px!important;border:0!important;border-radius:0!important;background:transparent!important;color:#667085!important;box-shadow:none!important;font-size:11px!important;font-weight:700!important;cursor:pointer!important;transform:none!important}#growthLaunchPanel .growth-launch-orders-summary button:hover{background:#f8fafc!important;color:#344054!important}#growthLaunchPanel .growth-launch-orders-summary button.is-active{background:#f6f8ff!important;color:#315fd8!important}#growthLaunchPanel .growth-launch-orders-summary button:focus-visible{outline:3px solid rgba(53,105,232,.2)!important;outline-offset:-3px!important}.growth-launch-orders-summary button.is-active:after{position:absolute;right:10px;bottom:-1px;left:10px;height:2px;border-radius:2px 2px 0 0;background:#3569e8;content:""}.growth-launch-order-list{display:grid;gap:9px}.growth-launch-order-card{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:16px;padding:15px 16px;border:1px solid #dfe5ee;border-radius:9px;background:#fff;box-shadow:0 1px 2px rgba(16,24,40,.03);transition:border-color .16s ease,box-shadow .16s ease}.growth-launch-order-card:hover{border-color:#b7c6db;box-shadow:0 4px 12px rgba(16,24,40,.05)}.growth-launch-order-card.is-attention{border-left:3px solid #d92d20}.growth-launch-order-card.is-ready{border-left:3px solid #12b76a}.growth-launch-order-card.is-archived{background:#fafbfd}.growth-launch-order-main{min-width:0}.growth-launch-order-title{display:flex;flex-wrap:wrap;align-items:center;gap:8px}.growth-launch-order-title b{color:#17233d;font-size:14px}.growth-launch-order-status{padding:3px 7px;border-radius:999px;background:#eef4ff;color:#315fd8;font-size:9px;font-weight:800}.growth-launch-order-status.is-attention{background:#fff1f0;color:#c4322b}.growth-launch-order-status.is-ready{background:#ecfdf3;color:#027a48}.growth-launch-order-context{margin-top:5px;color:#667085;font-size:10px}.growth-launch-order-facts{display:grid;grid-template-columns:1.1fr .9fr 1.25fr;gap:0;margin-top:12px;border:1px solid #edf0f4;border-radius:7px;background:#fafbfd}.growth-launch-order-facts>div{min-width:0;padding:9px 10px}.growth-launch-order-facts>div+div{border-left:1px solid #edf0f4}.growth-launch-order-facts small{display:block;margin-bottom:3px;color:#98a2b3;font-size:9px}.growth-launch-order-facts b{display:block;overflow:hidden;color:#344054;font-size:11px;text-overflow:ellipsis;white-space:nowrap}.growth-launch-order-facts .is-next b{color:#315fd8}.growth-launch-order-foot{display:flex;align-items:center;gap:8px;margin-top:8px;color:#98a2b3;font-size:9px}.growth-launch-order-id{overflow:hidden;max-width:150px;font:9px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;text-overflow:ellipsis;white-space:nowrap}.growth-launch-order-actions{display:flex;align-items:center;align-self:center;gap:6px}.growth-launch-order-archive{min-height:34px!important;padding:0 8px!important;border-color:transparent!important;background:transparent!important;color:#7a8494!important;font-size:10px!important}.growth-launch-order-archive:hover{background:#f2f4f7!important;color:#344054!important}.growth-launch-order-action{min-height:38px!important;padding:0 13px!important;align-self:center}.growth-launch-order-empty{padding:46px 20px;border:1px dashed #ced7e4;border-radius:9px;background:#fafbfd;text-align:center}.growth-launch-order-empty b{display:block;margin-bottom:6px;font-size:15px}.growth-launch-order-empty span{color:#667085;font-size:12px}
      .growth-launch-order-card.is-deleting{border-left:3px solid #3569e8;background:#fbfcff}.growth-launch-order-status.is-deleting{background:#eef3ff;color:#315fd8}.growth-launch-delete-progress{height:4px;margin-top:7px;overflow:hidden;border-radius:999px;background:#e2e8f2}.growth-launch-delete-progress i{display:block;height:100%;border-radius:inherit;background:#3569e8;transition:width .2s ease}.growth-launch-delete-running,.growth-launch-delete-review{display:grid;gap:3px;min-width:132px;padding:9px 10px;border-radius:7px;background:#f2f6ff}.growth-launch-delete-running b,.growth-launch-delete-review b{color:#315fd8;font-size:11px}.growth-launch-delete-running span,.growth-launch-delete-review span{color:#667085;font-size:9px}.growth-launch-delete-review{background:#fff4f2}.growth-launch-delete-review b{color:#b42318}
      .growth-plan-modal{width:min(720px,calc(100vw - 36px))}.growth-plan-confirm{display:grid;gap:12px;margin-top:14px}.growth-plan-confirm>section{padding:16px;border:1px solid #dfe5ed;border-radius:9px;background:#fff}.growth-plan-confirm>section>header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}.growth-plan-confirm>section>header b{color:#17233d;font-size:14px}.growth-plan-confirm>section>header span{padding:3px 7px;border-radius:5px;background:#eef3ff;color:#315fd8;font-size:10px;font-weight:750}.growth-plan-confirm>section>.growth-form{grid-template-columns:1fr 1fr}.growth-plan-confirm>section>small{display:block;margin-top:10px;color:#667085;font-size:10px}.growth-plan-confirm textarea{min-height:74px}
      #growthLaunchPanel{--ink:#17233d;--muted:#6c788e;--line:#e0e6ef;--blue:#3569e8}.growth-launch-orders-head p{max-width:500px}.growth-launch-orders-summary{margin-bottom:16px}.growth-unified-section-head{display:flex;align-items:baseline;justify-content:space-between;gap:14px;margin:2px 0 10px}.growth-unified-section-head b{color:#17233d;font-size:14px}.growth-unified-section-head span{color:#667085;font-size:10px}.growth-launch-order-facts b{overflow:visible;line-height:1.45;text-overflow:clip;white-space:normal}.growth-launch-delivery-copy span{overflow:hidden;color:#475467;font-size:11px;text-overflow:ellipsis;white-space:nowrap}
      #growthLaunchPanel .growth-launch-topbar.is-root .growth-launch-top-actions{gap:8px}#growthLaunchPanel .growth-launch-root-create{min-height:36px!important;padding:0 14px!important;border-radius:8px!important;font-size:12px!important}#growthLaunchPanel .growth-launch-orders-summary button{display:inline-flex!important;align-items:center!important;gap:5px!important}#growthLaunchPanel .growth-launch-orders-summary button.has-alert:not(.is-active){color:#b42318!important}#growthLaunchPanel .growth-launch-orders-summary button>span{display:inline-grid;place-items:center;min-width:18px;height:18px;padding:0 4px;border-radius:999px;background:#eef2f7;color:inherit;font-size:9px}#growthLaunchPanel .growth-launch-orders-summary button.has-alert>span{background:#fee4e2}
      .growth-task-tools{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:-2px 0 10px}.growth-task-tools span{color:#667085;font-size:10px}.growth-task-search{width:min(320px,100%);height:36px;box-sizing:border-box;padding:0 11px;border:1px solid #d6dee9;border-radius:7px;background:#fff;color:#344054;font:12px/1 Inter,"PingFang SC","Microsoft YaHei",sans-serif}.growth-task-search:focus{border-color:#84a2ef;outline:3px solid rgba(53,105,232,.14)}#growthLaunchPanel .growth-task-list{gap:8px}#growthLaunchPanel .growth-task-card{width:100%!important;min-height:70px!important;padding:12px 15px!important;border:1px solid #dfe5ee!important;border-radius:8px!important;background:#fff!important;color:#17233d!important;box-shadow:0 1px 2px rgba(16,24,40,.025)!important;transition:border-color .16s ease,background .16s ease,box-shadow .16s ease!important}#growthLaunchPanel .growth-task-card:hover{border-color:#b7c6db!important;background:#fafcff!important;box-shadow:0 3px 10px rgba(16,24,40,.045)!important}#growthLaunchPanel .growth-task-card:focus-visible{border-color:#6f91ee!important;background:#f6f8ff!important;outline:3px solid rgba(53,105,232,.18)!important;outline-offset:1px!important}#growthLaunchPanel .growth-task-card.is-observing{min-height:66px!important}#growthLaunchPanel .growth-task-card.is-observing p{display:inline-flex;margin-top:1px;padding:3px 7px;border-radius:999px;background:#f2f4f7;color:#475467;font-size:10px;font-weight:750}#growthLaunchPanel .growth-task-card .growth-task-next{color:#52637b;font-size:10px}#growthLaunchPanel .growth-task-card:hover .growth-task-next{color:#315fd8}
      @media(max-width:900px){.growth-drawer,.growth-launch-drawer{width:100vw}.growth-launch-goal-form{grid-template-columns:1fr!important}.growth-launch-goal-form .growth-launch-actions{grid-column:auto!important}.growth-launch-facts{grid-template-columns:1fr 1fr!important}.growth-launch-cold-status{grid-template-columns:1fr}.growth-launch-cold-guidance{padding:14px 0 0;border-top:1px solid #dfe6f1;border-left:0}.growth-material-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
      @media(max-width:620px){.growth-direction-list{grid-template-columns:1fr}.growth-history-note{grid-template-columns:1fr 1fr}.growth-history-note>b{grid-column:1/-1}.growth-history-note>span:nth-of-type(odd){border-left:0}.growth-naming-grid{grid-template-columns:1fr}}
      .growth-launch-cold-summary{color:#315fd8;font-size:12px;font-weight:800;white-space:nowrap}.growth-launch-technical{padding:0 16px 12px;border-top:1px solid #edf1f5;color:#667085;font-size:10px}.growth-launch-technical summary{padding:10px 0 0;cursor:pointer}.growth-launch-technical p{margin:8px 0 0;font:10px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace}
      @media(max-width:560px){.growth-queue-tabs{padding-left:8px;padding-right:8px}.growth-workbar{flex-wrap:wrap}.growth-workbar input{flex-basis:100%}.growth-task-card{grid-template-columns:1fr}.growth-task-next{text-align:left}.growth-task-group-row{grid-template-columns:minmax(0,1fr) 16px}.growth-task-group-row>span{display:none}.growth-detail{padding:14px 12px 20px}.growth-plan-context{align-items:flex-start;flex-direction:column}.growth-structure-row{grid-template-columns:1fr}.growth-structure-fields.two{grid-template-columns:1fr}.growth-structure-fields label.wide{grid-column:auto}.growth-launch-page{padding:0 14px 24px!important}.growth-launch-topbar{margin:0 -14px 14px!important;padding:0 14px!important}.growth-launch-breadcrumb{display:grid!important}.growth-launch-orders-link{display:none!important}.growth-launch-orders-head{align-items:stretch;flex-direction:column}.growth-launch-orders-head .growth-launch-primary{width:100%}.growth-launch-orders-summary{overflow-x:auto}.growth-launch-orders-summary button{flex:1 0 auto}.growth-launch-order-card{grid-template-columns:1fr;padding:14px}.growth-launch-order-facts{grid-template-columns:1fr 1fr}.growth-launch-order-facts>div:nth-child(3){grid-column:1/-1;border-top:1px solid #edf0f4;border-left:0}.growth-launch-order-actions{justify-content:flex-end;flex-wrap:wrap}.growth-launch-order-action{min-width:116px}.growth-launch-shared{align-items:flex-start;flex-direction:column}.growth-launch-plan,.growth-launch-rail,.growth-plan-confirm>section>.growth-form{grid-template-columns:1fr!important}.growth-launch-cold-head{align-items:flex-start}.growth-launch-cold-status{grid-template-columns:1fr;gap:10px;padding:14px}.growth-launch-cold-actions .growth-launch-primary{width:100%;min-width:0}.growth-launch-delivery-row{grid-template-columns:minmax(0,1fr) auto;gap:10px;padding:13px 14px}.growth-launch-delivery-row button{grid-column:1/-1;width:100%}.growth-launch-delivery>header{padding:14px}.growth-launch-delivery-note{padding:11px 14px}.growth-material-grid{grid-template-columns:1fr}.growth-material-status-strip{align-items:flex-start;flex-direction:column}.growth-material-preview-stage{min-height:260px}}
      @media(max-width:560px){.growth-task-tools{align-items:stretch;flex-direction:column}.growth-task-search{width:100%}}
      @media(max-width:560px){.growth-target-summary{grid-template-columns:1fr}.growth-target-detail{grid-template-columns:72px minmax(0,1fr) auto;gap:8px}.growth-page-repair-identity label{grid-template-columns:1fr;gap:4px}.growth-target-modal .growth-modal-foot{align-items:stretch;flex-direction:column-reverse;padding:12px 20px}.growth-target-modal .growth-modal-foot button{width:100%}}
    `;
    document.head.appendChild(integratedShell);
    const embeddedShell = document.createElement('style');
    embeddedShell.textContent = `
      .growth-layer-embedded{position:relative;inset:auto;z-index:1;height:auto;min-height:0;overflow:visible;border:0;border-radius:0;background:#fff}
      .growth-layer-embedded[hidden]{display:none}.growth-layer-embedded .growth-backdrop{display:none}
      .growth-layer-embedded .growth-drawer{position:relative;inset:auto;width:100%;height:auto;border:0;box-shadow:none}
      #growthWorkspacePanel.growth-layer-embedded:not(.is-detail-open) .growth-embedded-head{display:none}
      .growth-layer-embedded .growth-drawer-head{min-height:54px;padding:0 2px;background:#fff}
      #growthWorkspacePanel.growth-layer-embedded.is-detail-open .growth-embedded-head{grid-template-columns:32px minmax(0,1fr)}
      #growthWorkspacePanel.growth-layer-embedded.is-detail-open .growth-embedded-head>div{grid-column:2!important}
      #growthWorkspacePanel.growth-layer-embedded.is-detail-open .growth-embedded-refresh{display:none!important}
      .growth-layer-embedded .growth-drawer-head>.growth-icon-button{display:none!important}
      #growthWorkspacePanel .growth-embedded-refresh{min-height:32px!important;padding:0 10px!important;border-color:#d7deea!important;background:#fff!important;color:#475467!important;font-size:11px!important}
      #growthWorkspacePanel .growth-embedded-refresh:hover{border-color:#98a2b3!important;background:#f8fafc!important}
      .growth-layer-embedded .growth-queue-tabs{grid-template-columns:repeat(4,max-content);justify-content:start;gap:20px;overflow:auto;padding:0;border-bottom:1px solid var(--line);background:#fff}
      #growthWorkspacePanel.growth-layer-embedded .growth-queue-tabs button{min-height:42px!important;padding:0 2px!important;border:0!important;border-bottom:2px solid transparent!important;border-radius:0!important;background:transparent!important;color:#667085!important;font-size:12px!important}
      #growthWorkspacePanel.growth-layer-embedded .growth-queue-tabs button.is-active{border-bottom-color:#17233d!important;background:transparent!important;color:#17233d!important}
      .growth-layer-embedded .growth-queue-tabs span{min-width:18px;height:18px;margin-left:4px;padding:0 5px;background:#eef2f6;font-size:9px}
      .growth-layer-embedded .growth-detail{flex:0 0 auto;overflow:visible;padding:16px 0 2px;background:#fff}
      .growth-layer-embedded .growth-empty{min-height:180px;padding:20px;border:1px dashed #d8e0eb;border-radius:8px;background:#fafbfc}
      .growth-layer-embedded .growth-queue-head{align-items:flex-end;margin:0 0 12px;padding:0}
      .growth-layer-embedded .growth-queue-head h2{margin:0 0 3px;font-size:15px}
      .growth-layer-embedded .growth-queue-head p{font-size:11px;line-height:1.5}
      .growth-layer-embedded .growth-task-list{grid-template-columns:repeat(2,minmax(0,1fr));align-items:start;gap:10px}
      .growth-layer-embedded .growth-task-list>.growth-task-group:only-child,.growth-layer-embedded .growth-task-list>.growth-task-card:only-child{grid-column:1/-1}
      .growth-layer-embedded .growth-task-group{border-radius:8px}
      .growth-layer-embedded .growth-task-group>header{padding:10px 12px}
      .growth-layer-embedded .growth-task-group>header b{font-size:12px}
      .growth-layer-embedded .growth-task-group-row{min-height:58px!important;padding:0 12px!important;grid-template-columns:minmax(0,1fr) auto 15px;gap:9px}
      .growth-layer-embedded .growth-task-group-row>span{display:block;max-width:150px;overflow:hidden;text-overflow:ellipsis}
      .growth-layer-embedded .growth-task-group-copy strong{font-size:12px}
      .growth-layer-embedded .growth-task-group-copy small{font-size:10px}
      .growth-layer-embedded .growth-task-card{min-height:76px!important;padding:12px 14px!important;gap:12px}
      .growth-layer-embedded .growth-task-card strong{font-size:12px}
      .growth-layer-embedded .growth-task-card p{font-size:11px}
      .growth-layer-embedded .growth-task-meta{margin-top:5px}
      .growth-layer-embedded .growth-task-next{max-width:150px;font-size:10px}
      .growth-layer-embedded.is-detail-open{min-height:0}
      .growth-layer-embedded.is-detail-open .growth-detail{display:block;overflow:visible;padding:14px 0 2px;background:#fff}
      .growth-layer-embedded.is-detail-open .growth-detail.has-autonomy-panel{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(300px,.65fr);align-items:start;gap:0 16px}
      .growth-layer-embedded.is-detail-open .growth-detail>*{max-width:none;margin-left:0;margin-right:0}
      .growth-layer-embedded.is-detail-open .growth-detail.has-autonomy-panel>*:not(#growthAutonomyPanel):not(.growth-technical){grid-column:1}
      .growth-layer-embedded.is-detail-open #growthAutonomyPanel{grid-column:2;grid-row:1/span 6;margin-top:0;padding-top:0}
      .growth-layer-embedded.is-detail-open #growthAutonomyPanel .growth-review-card{margin-top:0}
      .growth-layer-embedded.is-detail-open .growth-technical{grid-column:1/-1}
      .growth-layer-embedded.is-detail-open .growth-section{margin-top:10px;padding:14px}
      .growth-layer-embedded.is-detail-open .growth-actions{min-height:62px;margin-top:10px;padding:0 12px}
      #growthWorkspacePanel.growth-layer-embedded.has-inline-action{display:grid;grid-template-columns:minmax(0,1fr) minmax(360px,430px);align-items:start;gap:16px}
      .growth-layer-embedded.has-inline-action .growth-drawer{min-width:0}
      .growth-layer-embedded.has-inline-action .growth-modal-layer{position:sticky;inset:auto;top:12px;z-index:2;min-width:0;display:block;place-items:unset;border:1px solid #dfe5ee;border-radius:10px;background:#fff;box-shadow:0 8px 24px rgba(26,43,76,.08)}
      .growth-layer-embedded.has-inline-action .growth-inline-context{display:grid;gap:2px;padding:14px 16px 11px;border-bottom:1px solid #e7ebf1;background:#f8fafc}
      .growth-layer-embedded.has-inline-action .growth-inline-context span{color:#17233d;font-size:12px;font-weight:800}
      .growth-layer-embedded.has-inline-action .growth-inline-context small{color:#667085;font-size:10px;line-height:1.45}
      .growth-layer-embedded.has-inline-action .growth-modal{width:100%;max-height:calc(100vh - 32px);border-radius:0 0 10px 10px;box-shadow:none}
      .growth-layer-embedded.has-inline-action .growth-modal-head{min-height:58px;padding:0 16px}
      .growth-layer-embedded.has-inline-action .growth-modal-head b{font-size:15px}
      .growth-layer-embedded.has-inline-action .growth-modal-body{padding:18px 16px}
      .growth-layer-embedded.has-inline-action .growth-modal-body h3{font-size:16px}
      .growth-layer-embedded.has-inline-action .growth-modal-foot{min-height:62px;padding:10px 16px;flex-wrap:wrap}
      .growth-layer-embedded.has-inline-action .growth-modal-foot button:focus-visible,.growth-layer-embedded.has-inline-action .growth-icon-button:focus-visible{outline:3px solid rgba(53,105,232,.2)!important;outline-offset:2px!important}
      @media(max-width:1180px){#growthWorkspacePanel.growth-layer-embedded.has-inline-action{grid-template-columns:1fr}.growth-layer-embedded.has-inline-action .growth-modal-layer{position:relative;top:auto}.growth-layer-embedded.has-inline-action .growth-modal{max-height:none}}
      @media(max-width:980px){.growth-layer-embedded .growth-task-list{grid-template-columns:1fr}.growth-layer-embedded.is-detail-open .growth-detail.has-autonomy-panel{display:block}.growth-layer-embedded.is-detail-open #growthAutonomyPanel{position:static;margin-top:10px;padding-top:14px}}
      @media(max-width:720px){.growth-layer-embedded .growth-queue-tabs{grid-template-columns:repeat(4,max-content);gap:14px}.growth-layer-embedded .growth-task-group-row{grid-template-columns:minmax(0,1fr) 15px}.growth-layer-embedded .growth-task-group-copy{display:grid!important}.growth-layer-embedded .growth-task-group-row>span:not(.growth-task-group-copy){display:none}.growth-layer-embedded.is-detail-open .growth-detail{padding:12px 0 2px}}
    `;
    document.head.appendChild(embeddedShell);
  }

  function setWorkspaceReturn(target) {
    state.workspaceReturn = target && target.kind ? target : null;
    const back = document.querySelector('#growthWorkspacePanel [data-growth-back]');
    if (back) back.hidden = !state.workspaceReturn;
  }

  function isEmbeddedWorkspace() {
    return document.getElementById('growthWorkspacePanel')?.classList.contains('growth-layer-embedded') === true;
  }

  function scopedExperiments() {
    if (!isEmbeddedWorkspace()) return state.experiments;
    return state.experiments.filter(item => state.coverageScope.has(String(item.experiment_id || '')));
  }

  function preferredCoverageBucket(items) {
    const buckets = items.map(item => String((item.workflow || {}).bucket || 'action_required'));
    return ['action_required', 'exception', 'system_work', 'observing', 'completed'].find(bucket => buckets.includes(bucket)) || 'action_required';
  }

  async function setCoverageScope(experimentIds, seedItems=[]) {
    state.coverageScope = new Set((Array.isArray(experimentIds) ? experimentIds : []).map(value => String(value || '').trim()).filter(Boolean));
    state.activeExperiment = state.coverageScope.has(state.activeExperiment) ? state.activeExperiment : '';
    const mount = document.getElementById('adGleTaskWorkbenchMount');
    if (mount) mount.dataset.experimentIds = JSON.stringify([...state.coverageScope]);
    const normalizedSeeds=(Array.isArray(seedItems)?seedItems:[]).filter(item=>state.coverageScope.has(String(item?.experiment_id||'')));
    if(normalizedSeeds.length===state.coverageScope.size){
      state.experiments=normalizedSeeds;
      const items=scopedExperiments();
      state.workBucket=preferredCoverageBucket(items);
      renderQueueTabs();
      renderExperimentQueue();
      return;
    }
    await loadList({silent:false, coverageScope:true});
    const items = scopedExperiments();
    if (!state.activeExperiment && !items.some(item => String((item.workflow || {}).bucket || 'action_required') === state.workBucket)) {
      state.workBucket = preferredCoverageBucket(items);
      renderQueueTabs();
      renderExperimentQueue();
    }
  }

  async function openWorkspace(experimentId, options={}) {
    const panel = document.getElementById('growthWorkspacePanel');
    if (options.resetReturn) setWorkspaceReturn(null);
    if (options.returnTarget) setWorkspaceReturn(options.returnTarget);
    panel.hidden = false;
    if (!isEmbeddedWorkspace()) document.body.style.overflow = 'hidden';
    const loaded = await loadList({select:experimentId});
    if (isEmbeddedWorkspace()) panel.scrollIntoView({behavior:'smooth',block:'start'});
    return loaded;
  }

  function showEmbeddedQueue({scroll=true}={}) {
    if (!isEmbeddedWorkspace()) return;
    closeModal();
    state.activeExperiment = '';
    state.detail = null;
    setWorkspaceReturn(null);
    renderQueueTabs();
    renderExperimentQueue();
    if (scroll) document.getElementById('growthWorkspacePanel')?.scrollIntoView({behavior:'smooth',block:'start'});
  }

  function backWorkspace() {
    if (isEmbeddedWorkspace()) {
      showEmbeddedQueue();
      return;
    }
    const target = state.workspaceReturn;
    if (!target) return;
    if (target.kind === 'embeddedQueue') {
      showEmbeddedQueue();
      return;
    }
    if (target.kind === 'taskHome') {
      closeWorkspace({preserveBodyLock:true});
      openLaunchWorkspace({taskView:String(target.taskView||'action_required')});
      return;
    }
    if (target.kind === 'launch' && target.launchId) {
      try {
        openLaunchWorkspace({openLaunchId:target.launchId});
        closeWorkspace({preserveBodyLock:true});
      } catch (error) {
        console.error('growth_workspace_back_failed', error);
      }
    }
  }

  function closeWorkspace(options={}) {
    const panel = document.getElementById('growthWorkspacePanel');
    if (isEmbeddedWorkspace()) {
      state.activeExperiment = '';
      state.detail = null;
      closeModal();
      setWorkspaceReturn(null);
      renderQueueTabs();
      renderExperimentQueue();
      return;
    }
    panel.hidden = true;
    closeModal();
    setWorkspaceReturn(null);
    if (!options.preserveBodyLock) document.body.style.overflow = '';
  }

  const launchState={screen:'orders',experimentMode:'creative_direction',audiencePreview:null,approvedCreatives:[],frozenCreativeId:'',copyVariants:[],audienceRound:0,launchArchiveView:false,launchOrderFilter:'all',taskHomeView:'orders',taskSearch:'',taskIndexLoading:false,target:{country:'BR',app:'Tugao',daily:'200',cpi:'0.30',gender:'female',age_min:'18',age_max:'40',language:'pt_BR',account_id:'',account_name:'',page_id:'',page_name:''},accounts:[],pages:[],countryPageIds:{},accountPageIds:{},accountPageOptions:{},directions:[],historyEvidence:{},namingRule:{},namingDate:'',regenerationRound:0,directionsLoading:false,accountsLoading:false,accountsLoaded:false,launches:[],launchesLoaded:false,launchesLoading:false,ordersNotice:'',orderRetryTimer:null,deletePollTimer:null,metaRateLimit:{loading:false,blocked:false,unknown:false,account_id:'',retry_after_seconds:0,blocked_until:0,reason:''},metaRateLimitTimer:null,metaRateLimitCountdownTimer:null,deliveryStatus:{loading:false,data:null,error:''},deliveryStatusTimer:null,performanceSync:{loading:false,error:''},batchWorkflowTimer:null,batchWorkflowPlanId:'',launch:null,batchPlan:null,jobs:[],experiments:[],experimentStates:[],orderPhase:'',orderStatusZh:'',orderDataMismatch:false,approved:false,lastNotifiedPhase:'',lastProgressSignature:'',error:''};
  let launchOrdersRequest=null,taskIndexRequest=null;

  function clearLaunchDeliveryStatusTimer(){if(launchState.deliveryStatusTimer){clearTimeout(launchState.deliveryStatusTimer);launchState.deliveryStatusTimer=null;}}
  function launchDeliveryCheckedLabel(value){if(!value)return '';try{return new Intl.DateTimeFormat('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false,timeZone:'Asia/Shanghai'}).format(new Date(value));}catch(_){return '';}}
  function launchPerformanceUpdatedLabel(value){if(!value)return '待下次数据同步';try{return new Intl.DateTimeFormat('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false,timeZone:'Asia/Shanghai'}).format(new Date(value));}catch(_){return String(value);}}
  function scheduleLaunchDeliveryStatusRefresh(){clearLaunchDeliveryStatusTimer();const panel=document.getElementById('growthLaunchPanel'),launchId=String((launchState.launch||{}).launch_id||'');if(!launchId||!panel||panel.hidden||launchState.screen!=='cold')return;launchState.deliveryStatusTimer=setTimeout(()=>refreshLaunchDeliveryStatus({render:true}),20000);}
  async function refreshLaunchDeliveryStatus(options={}){const launchId=String((launchState.launch||{}).launch_id||'');if(!launchId||launchState.deliveryStatus?.loading)return false;launchState.deliveryStatus={...launchState.deliveryStatus,loading:true,error:''};if(options.render)renderLaunch('cold',{preserveScroll:true});try{const data=await api(`/api/ops/ad-data-dashboard/new-account-launches/${encodeURIComponent(launchId)}/delivery-status`);if(String(data.launch_id||'')!==launchId)return false;launchState.deliveryStatus={loading:false,data,error:''};if(options.manual)showLaunchToast('状态已更新');return true;}catch(error){launchState.deliveryStatus={...launchState.deliveryStatus,loading:false,error:readableError(error)};if(options.manual)showLaunchToast('状态刷新失败，当前保留上次成功结果');return false;}finally{if(options.render)renderLaunch('cold',{preserveScroll:true});scheduleLaunchDeliveryStatusRefresh();}}
  async function refreshLaunchPerformanceData(){const launchId=String((launchState.launch||{}).launch_id||'');if(!launchId||launchState.performanceSync?.loading)return false;launchState.performanceSync={loading:true,error:''};renderLaunch('cold',{preserveScroll:true});const retainedDelivery=launchState.deliveryStatus,retainedRateLimit=launchState.metaRateLimit;try{const order=await api(`/api/ops/ad-data-dashboard/new-account-launches/${encodeURIComponent(launchId)}`);hydrateLaunchOrder(order);launchState.deliveryStatus=retainedDelivery;launchState.metaRateLimit=retainedRateLimit;launchState.performanceSync={loading:false,error:''};showLaunchToast('已读取最新同步的 T+1 效果数据');return true;}catch(error){launchState.performanceSync={loading:false,error:readableError(error)};showLaunchToast('效果数据同步失败，当前保留上次成功结果');return false;}finally{renderLaunch('cold',{preserveScroll:true});}}

  function clearLaunchMetaRateLimitTimers() {
    if(launchState.metaRateLimitTimer){clearTimeout(launchState.metaRateLimitTimer);launchState.metaRateLimitTimer=null;}
    if(launchState.metaRateLimitCountdownTimer){clearInterval(launchState.metaRateLimitCountdownTimer);launchState.metaRateLimitCountdownTimer=null;}
  }

  function launchMetaRateLimitBlocked() {
    const state=launchState.metaRateLimit||{};
    return Boolean(state.loading||state.unknown||(state.blocked&&Number(state.blocked_until||0)*1000>Date.now()));
  }

  function launchMetaRateLimitRecoveryLabel() {
    const timestamp=Number(launchState.metaRateLimit?.blocked_until||0);
    if(!timestamp)return '系统确认额度后';
    try{return new Intl.DateTimeFormat('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false,timeZone:'Asia/Shanghai'}).format(new Date(timestamp*1000));}catch(_){return new Date(timestamp*1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});}
  }

  function launchMetaRateLimitCountdownText() {
    const seconds=Math.max(0,Math.ceil(Number(launchState.metaRateLimit?.blocked_until||0)-Date.now()/1000));
    if(!seconds)return '即将恢复';
    const minutes=Math.ceil(seconds/60),hours=Math.floor(minutes/60),remaining=minutes%60;
    return hours?`${hours} 小时${remaining?` ${remaining} 分钟`:''}`:`${minutes} 分钟`;
  }

  function updateLaunchMetaRateLimitCountdown() {
    document.querySelectorAll('[data-meta-rate-countdown]').forEach(node=>{node.textContent=launchMetaRateLimitCountdownText();});
  }

  function scheduleLaunchMetaRateLimitRefresh() {
    clearLaunchMetaRateLimitTimers();
    const state=launchState.metaRateLimit||{},remaining=Math.max(0,Number(state.blocked_until||0)*1000-Date.now());
    if(!state.blocked||state.unknown||!remaining)return;
    launchState.metaRateLimitCountdownTimer=setInterval(updateLaunchMetaRateLimitCountdown,15000);
    launchState.metaRateLimitTimer=setTimeout(()=>{launchState.metaRateLimitTimer=null;refreshLaunchMetaRateLimit({render:true});},Math.min(2147483000,remaining+1200));
  }

  async function refreshLaunchMetaRateLimit(options={}) {
    const accountId=String(launchState.target?.account_id||'');
    if(!accountId){launchState.metaRateLimit={loading:false,blocked:false,unknown:false,account_id:'',retry_after_seconds:0,blocked_until:0,reason:''};clearLaunchMetaRateLimitTimers();return;}
    if(!launchState.metaRateLimit?.blocked)launchState.metaRateLimit={...launchState.metaRateLimit,loading:true,account_id:accountId};
    try{
      const payload=await api(`/api/ops/ad-data-dashboard/meta-rate-limit?account_id=${encodeURIComponent(accountId)}`),guard=payload.guard||{};
      launchState.metaRateLimit={loading:false,blocked:Boolean(guard.blocked),unknown:false,account_id:accountId,retry_after_seconds:Number(guard.retry_after_seconds||0),blocked_until:Number(guard.blocked_until||0),reason:String(guard.reason||'')};
    }catch(_){
      launchState.metaRateLimit={loading:false,blocked:true,unknown:true,account_id:accountId,retry_after_seconds:0,blocked_until:0,reason:'meta_rate_limit_status_unavailable'};
    }
    if(options.render&&String((launchState.launch||{}).launch_id||''))renderLaunch('cold',{preserveScroll:true});
    scheduleLaunchMetaRateLimitRefresh();
  }

  function launchMetaRateLimitNotice() {
    const state=launchState.metaRateLimit||{};
    if(state.loading)return '<div class="growth-launch-rate-limit is-checking"><span class="growth-launch-rate-dot"></span><div><b>正在检查 Meta 调用额度</b><small>检查完成前已暂停会继续访问 Meta 的操作。</small></div></div>';
    if(!launchMetaRateLimitBlocked())return '';
    if(state.unknown)return '<div class="growth-launch-rate-limit"><span class="growth-launch-rate-dot"></span><div><b>暂时无法确认 Meta 调用额度</b><small>系统会自动恢复检查；恢复前相关按钮已停用。</small></div></div>';
    return `<div class="growth-launch-rate-limit"><span class="growth-launch-rate-dot"></span><div><b>Meta 限流中 · 预计 ${esc(launchMetaRateLimitRecoveryLabel())} 恢复</b><small>约 <span data-meta-rate-countdown>${esc(launchMetaRateLimitCountdownText())}</span>；系统将在额度恢复后自动继续，恢复前不会再请求 Meta。</small></div></div>`;
  }

  function guardLaunchMetaAction() {
    if(!launchMetaRateLimitBlocked())return false;
    const state=launchState.metaRateLimit||{};
    showLaunchToast(state.loading?'正在检查 Meta 调用额度，请稍候':(state.unknown?'暂时无法确认 Meta 调用额度，系统正在自动恢复':`Meta 限流中，预计 ${launchMetaRateLimitRecoveryLabel()} 恢复`));
    return true;
  }

  function restoreLaunchOrdersCache() {
    try{
      const saved=JSON.parse(sessionStorage.getItem(LAUNCH_ORDERS_CACHE_KEY)||'null');
      if(!saved||!Array.isArray(saved.launches))return;
      launchState.launches=saved.launches;
      launchState.launchesLoaded=true;
    }catch(_){}
  }

  function persistLaunchOrdersCache() {
    try{sessionStorage.setItem(LAUNCH_ORDERS_CACHE_KEY,JSON.stringify({launches:launchState.launches,updated_at:new Date().toISOString()}));}catch(_){}
  }

  function fixedLaunchAudience(country) {
    return {BR:{gender:'female',age_min:'18',age_max:'40',language:'pt_BR',language_label:'葡萄牙语（巴西）'},MX:{gender:'female',age_min:'18',age_max:'40',language:'es_419',language_label:'西班牙语（拉美）'},ID:{gender:'female',age_min:'18',age_max:'40',language:'id_ID',language_label:'印度尼西亚语'}}[String(country||'BR').toUpperCase()]||null;
  }

  function applyFixedLaunchAudience(country) {
    const audience=fixedLaunchAudience(country);if(!audience)return false;
    launchState.target={...launchState.target,country:String(country).toUpperCase(),...audience};return true;
  }

  function launchProgressSnapshot() {
    const variants=((launchState.launch||{}).variants||[]),expected=variants.length;
    const failedStatuses=new Set(['failed','rejected','cancelled','expired']);
    const jobs=(launchState.jobs||[]).slice(0,expected||undefined);
    const approved=jobs.filter(job=>Boolean(job.approved_creative?.image_id)).length;
    const ready=jobs.filter(job=>String(job.status||'')==='pending_review').length;
    const failed=jobs.filter(job=>failedStatuses.has(String(job.status||''))).length;
    const missing=jobs.filter(job=>String(job.status||'')==='completed'&&!job.approved_creative?.image_id&&!job.latest_image?.image_id).length;
    const processing=Math.max(0,expected-approved-ready-failed-missing);
    const phase=!expected||jobs.length<expected?'creating':(approved===expected?'approved':(ready>0?'review':(failed>0?'failed':(missing>0?'needs_selection':'generating'))));
    return {variants,jobs,expected,approved,ready,failed,missing,processing,phase};
  }

  function launchHasDeliveryWorkflow() {
    const deliveryPhases=new Set(['PAUSED','META_REVIEW_PENDING','READY_FOR_ACTIVATION','RUNNING','AD_WORKFLOW','ARCHIVED']);
    const deliveryStates=new Set(['WAITING_CREATE_APPROVAL','CREATING_PAUSED_OBJECTS','CREATION_PARTIAL_FAILURE','META_REVIEW_PENDING','READY_FOR_ACTIVATION','RUNNING','MATURING','RECOMMENDATION_READY','WAITING_ADJUSTMENT_APPROVAL','ADJUSTING','EVALUATING_ADJUSTMENT','EFFECTIVE','INEFFECTIVE','INCONCLUSIVE','DATA_INCOMPLETE','MIXED_CHANGE','PAUSED','ARCHIVED']);
    return deliveryPhases.has(String(launchState.orderPhase||''))||(launchState.experimentStates||[]).some(state=>deliveryStates.has(String(state||'')));
  }

  function persistLaunchProgress() {
    if(!launchState.launch)return;
    try{localStorage.setItem(LAUNCH_PROGRESS_STORAGE_KEY,JSON.stringify({screen:'cold',experimentMode:launchState.experimentMode,target:launchState.target,launch:launchState.launch,jobs:launchState.jobs,approved:launchState.approved,lastNotifiedPhase:launchState.lastNotifiedPhase,orderPhase:launchState.orderPhase,experimentStates:launchState.experimentStates}));}catch(_){}
  }

  function restoreLaunchProgress() {
    try{
      localStorage.removeItem(LEGACY_LAUNCH_PROGRESS_STORAGE_KEY);
      const saved=JSON.parse(localStorage.getItem(LAUNCH_PROGRESS_STORAGE_KEY)||'null');
      if(!saved||!saved.launch||!Array.isArray(saved.launch.variants))return;
      launchState.experimentMode=String(saved.experimentMode||saved.launch?.experiment_mode||saved.launch?.target?.experiment_mode||'creative_direction');launchState.target={...launchState.target,...(saved.target||{})};launchState.launch=saved.launch;launchState.jobs=Array.isArray(saved.jobs)?saved.jobs:[];launchState.approved=Boolean(saved.approved);launchState.lastNotifiedPhase=String(saved.lastNotifiedPhase||'');launchState.orderPhase=String(saved.orderPhase||'');launchState.experimentStates=Array.isArray(saved.experimentStates)?saved.experimentStates.map(value=>String(value||'')):[];
    }catch(_){}
  }

  function resetLaunchOrderContext(options={}) {
    clearLaunchMetaRateLimitTimers();
    clearLaunchDeliveryStatusTimer();
    launchState.metaRateLimit={loading:false,blocked:false,unknown:false,account_id:'',retry_after_seconds:0,blocked_until:0,reason:''};
    launchState.deliveryStatus={loading:false,data:null,error:''};launchState.order=null;launchState.launch=null;launchState.batchPlan=null;launchState.jobs=[];launchState.experiments=[];launchState.experimentStates=[];launchState.orderPhase='';launchState.orderStatusZh='';launchState.orderDataMismatch=false;launchState.approved=false;launchState.lastNotifiedPhase='';launchState.lastProgressSignature='';
    if(options.clearStored!==false){try{localStorage.removeItem(LAUNCH_PROGRESS_STORAGE_KEY);localStorage.removeItem(LEGACY_LAUNCH_PROGRESS_STORAGE_KEY);}catch(_){}}
  }

  function clearStoredLaunchProgress() {
    resetLaunchOrderContext();
  }

  function reconcileStoredLaunchProgress(listComplete=false) {
    const launchId=String(launchState.launch?.launch_id||'');if(!launchId)return;
    const current=launchState.launches.find(item=>String(item.launch_id||'')===launchId);
    if(current?.archived||(!current&&listComplete))clearStoredLaunchProgress();
  }

  function hydrateLaunchOrder(order) {
    const previousLaunchId=String((launchState.launch||{}).launch_id||'');
    const target=order?.target||order?.launch?.target||{},audience=target.audience||{};
    launchState.order=order||null;
    launchState.target={
      ...launchState.target,
      country:String(target.country||launchState.target.country||'BR'),
      app:'Tugao',
      daily:String(target.daily_spend_target||launchState.target.daily||'200'),
      cpi:String(target.cpi_target||launchState.target.cpi||'0.30'),
      gender:String(audience.gender||launchState.target.gender||'all'),
      age_min:String(audience.age_min||launchState.target.age_min||'18'),
      age_max:String(audience.age_max||launchState.target.age_max||'40'),
      language:String(audience.language||launchState.target.language||'pt_BR'),
      account_id:String(target.account_id||''),
      account_name:String(target.account_name||launchState.accounts.find(item=>item.account_id===String(target.account_id||''))?.name||''),
      page_id:String(target.page_id||''),
      page_name:String(launchState.pages.find(item=>item.page_id===String(target.page_id||''))?.name||''),
    };
    launchState.launch=order?.launch||null;
    if(previousLaunchId&&previousLaunchId!==String(launchState.launch?.launch_id||''))launchState.deliveryStatus={loading:false,data:null,error:''};
    launchState.experimentMode=String(target.experiment_mode||target.test_variable||launchState.launch?.experiment_mode||'creative_direction');
    launchState.batchPlan=order?.batch_plan||null;
    launchState.jobs=Array.isArray(order?.jobs)?order.jobs:[];
    const variants=Array.isArray(launchState.launch?.variants)?launchState.launch.variants:[],experiments=Array.isArray(order?.experiments)?order.experiments:[];
    const launchId=String(order?.launch_id||launchState.launch?.launch_id||''),country=String(target.country||'').toUpperCase(),campaign=String(variants.find(item=>String(item.meta_names?.campaign||''))?.meta_names?.campaign||'');
    const variantIds=new Set(variants.map(item=>String(item.experiment_id||'')).filter(Boolean));
    const scoped=variants.length===experiments.length&&experiments.every(item=>variantIds.has(String(item.experiment_id||''))&&String(item.launch_id||'')===launchId&&String(item.country||'').toUpperCase()===country&&String(item.campaign_name||'')===campaign);
    launchState.experiments=scoped?experiments:[];
    launchState.experimentStates=launchState.experiments.map(item=>String(item.state||''));
    launchState.orderDataMismatch=!scoped;
    launchState.orderPhase=String(order?.phase||'');
    launchState.orderStatusZh=String(order?.status_zh||'');
    launchState.approved=Boolean(launchState.jobs.length);
    launchState.lastProgressSignature='';
    clearLaunchMetaRateLimitTimers();
    launchState.metaRateLimit={loading:true,blocked:false,unknown:false,account_id:String(target.account_id||''),retry_after_seconds:0,blocked_until:0,reason:''};
    persistLaunchProgress();
    updateLaunchProgressBadge();
  }

  async function loadLaunchOrders(options={}) {
    if(launchState.orderRetryTimer){clearTimeout(launchState.orderRetryTimer);launchState.orderRetryTimer=null;}
    const owner=!launchOrdersRequest;
    if(owner){
      launchState.launchesLoading=true;
      launchOrdersRequest=(async()=>{
        let payload,lastError;
        for(let attempt=0;attempt<2;attempt+=1){
          try{payload=await api('/api/ops/ad-data-dashboard/new-account-launches?limit=100&include_archived=true');break;}
          catch(error){lastError=error;const transient=Boolean(error?.retryable||[502,503,504].includes(Number(error?.status||0)));if(!transient||attempt===1)throw error;await new Promise(resolve=>setTimeout(resolve,700));}
        }
        if(!payload)throw lastError||new Error('订单暂时无法读取');
        const previouslyDeleting=new Set(launchState.launches.filter(item=>String(item.permanent_delete?.status||'')==='STARTED').map(item=>String(item.launch_id||'')));
        launchState.launches=Array.isArray(payload.launches)?payload.launches:[];
        const currentIds=new Set(launchState.launches.map(item=>String(item.launch_id||'')));
        if([...previouslyDeleting].some(launchId=>launchId&&!currentIds.has(launchId)))showLaunchToast('订单及关联 Meta 对象已全部删除。');
        launchState.launchesLoaded=true;
        reconcileStoredLaunchProgress(launchState.launches.length<100);
        persistLaunchOrdersCache();
        updateLaunchProgressBadge();
        launchState.ordersNotice='';
        launchState.error='';
        if(launchState.deletePollTimer){clearTimeout(launchState.deletePollTimer);launchState.deletePollTimer=null;}
        if(launchState.launches.some(item=>String(item.permanent_delete?.status||'')==='STARTED')){
          launchState.deletePollTimer=setTimeout(()=>{launchState.deletePollTimer=null;loadLaunchOrders({deferredRetry:true});},1800);
        }
        return payload;
      })();
    }
    if(!options.badgeOnly&&launchState.screen==='orders'&&!launchState.launchesLoaded)renderLaunch('orders',{preserveScroll:true});
    try{
      await launchOrdersRequest;
      if(options.openLaunchId){
        const order=launchState.launches.find(item=>item.launch_id===options.openLaunchId);
        if(order){hydrateLaunchOrder(order);renderLaunch('cold');refreshLaunchMetaRateLimit({render:true});refreshLaunchDeliveryStatus({render:true});return;}
      }
    }catch(error){
      if(owner){
        launchState.launchesLoaded=launchState.launches.length>0;
        updateLaunchProgressBadge();
        const hasCachedOrders=launchState.launches.length>0;
        launchState.ordersNotice=hasCachedOrders?'刷新暂时失败，当前展示上次成功结果；系统正在自动恢复。':'订单暂时无法读取，系统正在自动重试。';
        launchState.error='';
        if(!options.deferredRetry)launchState.orderRetryTimer=setTimeout(()=>{launchState.orderRetryTimer=null;loadLaunchOrders({...options,deferredRetry:true});},4000);
      }
    }finally{
      if(owner){launchState.launchesLoading=false;launchOrdersRequest=null;}
      if(!options.badgeOnly&&launchState.screen==='orders')renderLaunch('orders',{preserveScroll:true});
    }
  }

  async function openLaunchOrder(launchId) {
    const cached=launchState.launches.find(item=>item.launch_id===launchId);
    if(cached){hydrateLaunchOrder(cached);renderLaunch('cold');refreshLaunchMetaRateLimit({render:true});refreshLaunchDeliveryStatus({render:true});return;}
    try{
      const order=await api(`/api/ops/ad-data-dashboard/new-account-launches/${encodeURIComponent(launchId)}`);
      hydrateLaunchOrder(order);
      renderLaunch('cold');
      refreshLaunchMetaRateLimit({render:true});
      refreshLaunchDeliveryStatus({render:true});
    }catch(error){
      launchState.error=readableError(error);
      renderLaunch('orders');
    }
  }

  async function changeLaunchArchiveState(launchId,action) {
    const archived=action==='archive';
    try{
      await api(`/api/ops/ad-data-dashboard/new-account-launches/${encodeURIComponent(launchId)}/${archived?'archive':'restore'}`,{method:'POST',headers:postHeaders(`new-account-launch-${action}`,{launch_id:launchId,action}),body:'{}'});
      if(archived&&String((launchState.launch||{}).launch_id||'')===launchId){
        launchState.launch=null;launchState.jobs=[];launchState.approved=false;
        try{localStorage.removeItem(LAUNCH_PROGRESS_STORAGE_KEY);}catch(_){}
        updateLaunchProgressBadge();
      }
      await Promise.all([loadLaunchOrders(),loadUnifiedTaskIndex()]);
      showLaunchToast(archived?'订单已移到回收站，可随时恢复。':'订单已恢复到全部订单。');
    }catch(error){
      launchState.error=readableError(error);
      renderLaunch('orders',{preserveScroll:true});
    }
  }

  function confirmArchiveLaunch(launchId) {
    showModal(`<section class="growth-modal growth-modal-compact"><header class="growth-modal-head"><b>移到回收站？</b><button type="button" class="growth-icon-button" data-modal-close>×</button></header><div class="growth-modal-body"><p>订单将从当前列表隐藏，可在回收站恢复。</p></div><footer class="growth-modal-foot"><button type="button" data-modal-close>取消</button><button type="button" class="growth-primary" id="growthArchiveLaunchConfirm">移到回收站</button></footer></section>`);
    document.getElementById('growthArchiveLaunchConfirm')?.addEventListener('click',async event=>{event.currentTarget.disabled=true;closeModal();await changeLaunchArchiveState(launchId,'archive');});
  }

  function confirmPermanentDeleteLaunch(launchId) {
    showModal(`<section class="growth-modal growth-delete-modal"><header class="growth-modal-head"><div><b>永久删除订单</b><small>${esc(launchId)}</small></div><button type="button" class="growth-icon-button" data-modal-close aria-label="关闭">×</button></header><div class="growth-modal-body"><h3>删除范围</h3><p>默认只删除 GLE 订单。选择联动删除后，系统会核对对象归属、共享关系与层级；ACTIVE 对象会立即停止投放。</p><div class="growth-delete-modes"><label class="growth-delete-mode"><input type="radio" name="growthDeleteMode" value="ORDER_ONLY" checked><span>仅删除订单（推荐）<small>保留 Meta 广告系列、广告组、广告及审计记录。</small></span></label><label class="growth-delete-mode"><input type="radio" name="growthDeleteMode" value="DELETE_ORDER_AND_META_OBJECTS"><span>删除订单及关联 Meta 对象<small>删除本订单独占的广告、广告组和广告系列；素材与历史审计保留。</small></span></label></div><div id="growthMetaDeletePreview" class="growth-meta-delete-preview" hidden></div><label id="growthMetaDeleteAckWrap" class="growth-meta-delete-ack" hidden><input type="checkbox" id="growthMetaDeleteAck"><span><b>确认删除顺序和投放影响</b><small>按广告 → 广告组 → 广告系列永久删除；ACTIVE 对象将立即停止投放。</small></span></label><div id="growthPermanentDeleteStatus" class="growth-notice" role="status" aria-live="polite" hidden></div></div><footer class="growth-modal-foot"><button type="button" data-modal-close>取消</button><button type="button" class="growth-primary" id="growthPermanentDeleteConfirm">确认永久删除</button></footer></section>`);
    let preview=null,loading=false;
    const button=document.getElementById('growthPermanentDeleteConfirm'),previewNode=document.getElementById('growthMetaDeletePreview'),ackWrap=document.getElementById('growthMetaDeleteAckWrap'),ack=document.getElementById('growthMetaDeleteAck');
    const reasonText={launch_must_be_archived_first:'订单必须先移到回收站',meta_delete_execution_unavailable:'Meta 删除能力当前未开放',meta_objects_shared_by_other_orders:'对象仍被其他订单共享',meta_object_ownership_not_verified:'无法确认对象归属本订单',meta_object_relationship_mismatch:'Meta 对象层级关系与订单不一致',meta_object_readback_failed:'Meta 状态读取失败，请稍后重试'};
    const selectedMode=()=>String(document.querySelector('input[name="growthDeleteMode"]:checked')?.value||'ORDER_ONLY');
    const updateButton=()=>{button.disabled=loading||(selectedMode()==='DELETE_ORDER_AND_META_OBJECTS'&&(!preview?.eligible||!ack.checked));};
    const loadPreview=async()=>{
      loading=true;preview=null;previewNode.hidden=false;previewNode.classList.remove('is-ready');previewNode.textContent='正在核对对象归属、共享关系、层级和当前状态…';ackWrap.hidden=true;ack.checked=false;updateButton();
      try{preview=await api(`/api/ops/ad-data-dashboard/new-account-launches/${encodeURIComponent(launchId)}/permanent-delete-preview`);const counts=preview.counts||{},activeCount=Number(preview.active_object_count||0);if(preview.eligible){previewNode.classList.add('is-ready');previewNode.innerHTML=`<div class="growth-meta-delete-summary"><b>可联动删除 ${Number(counts.ads||0)+Number(counts.adsets||0)+Number(counts.campaigns||0)} 个对象</b><span>${Number(counts.ads||0)} 条广告 · ${Number(counts.adsets||0)} 个广告组 · ${Number(counts.campaigns||0)} 个广告系列</span></div><div class="growth-meta-delete-checks">已确认归属本订单、未被共享、层级一致。</div>${activeCount?`<div class="growth-meta-delete-warning">其中 ${activeCount} 个对象当前为 ACTIVE，删除后将立即停止投放。</div>`:''}`;ackWrap.hidden=false;}else{previewNode.innerHTML=`<b>当前不能删除 Meta 对象</b><br>${esc((preview.blocked_reasons||[]).map(item=>reasonText[item]||item).join('；')||'安全检查未通过')}`;}}
      catch(error){previewNode.innerHTML=`<b>当前不能删除 Meta 对象</b><br>${esc(readableError(error))}`;}
      finally{loading=false;updateButton();}
    };
    document.querySelectorAll('input[name="growthDeleteMode"]').forEach(input=>input.addEventListener('change',()=>{if(selectedMode()==='DELETE_ORDER_AND_META_OBJECTS')loadPreview();else{preview=null;previewNode.hidden=true;ackWrap.hidden=true;ack.checked=false;updateButton();}}));
    ack?.addEventListener('change',updateButton);
    document.getElementById('growthPermanentDeleteConfirm')?.addEventListener('click',async event=>{
      const button=event.currentTarget,status=document.getElementById('growthPermanentDeleteStatus');
      try{
        button.disabled=true;
        const mode=selectedMode(),deletingMeta=mode==='DELETE_ORDER_AND_META_OBJECTS';
        if(deletingMeta&&(!preview?.eligible||!ack.checked))throw new Error('请先完成 Meta 对象安全检查与二次确认。');
        const body={mode,confirmation:deletingMeta?'DELETE_ORDER_AND_META_OBJECTS':'',plan_hash:deletingMeta?String(preview.plan_hash||''):''};
        button.textContent=deletingMeta?'正在建立删除任务…':'正在删除订单…';
        status.hidden=false;status.textContent=deletingMeta?'系统正在建立后台删除任务，建立后可以关闭此窗口。':'正在永久删除订单…';
        await api(`/api/ops/ad-data-dashboard/new-account-launches/${encodeURIComponent(launchId)}/permanent-delete`,{method:'POST',headers:postHeaders('new-account-launch-permanent-delete',{launch_id:launchId,mode,plan_hash:body.plan_hash}),body:JSON.stringify(body)});
        closeModal();await Promise.all([loadLaunchOrders(),loadUnifiedTaskIndex()]);showLaunchToast(deletingMeta?'删除任务已开始，可以关闭页面；完成后订单会自动消失。':'订单已永久删除，Meta 对象保持不变。');
      }catch(error){button.disabled=false;button.textContent='确认永久删除';status.hidden=false;status.textContent=readableError(error);}
    });
    updateButton();
  }

  function updateLaunchProgressBadge() {
    const badge=document.getElementById('growthLaunchProgressBadge');if(!badge)return;
    badge.className='';badge.hidden=true;badge.textContent='';
    if(!launchState.launchesLoaded)return;
    const active=launchState.launches.filter(order=>!order.archived),actionable=active.filter(order=>['ATTENTION_REQUIRED','CREATIVE_REVIEW','READY_FOR_PLAN','CREATIVE_SETUP_REQUIRED'].includes(String(order.phase||''))),jobs=actionable.flatMap(order=>Array.isArray(order.jobs)?order.jobs:[]),failedStatuses=new Set(['failed','rejected','cancelled','expired']);
    const failed=jobs.filter(job=>failedStatuses.has(String(job.status||''))).length;
    const missing=jobs.filter(job=>String(job.status||'')==='completed'&&!job.approved_creative?.image_id).length;
    const pendingReview=jobs.filter(job=>String(job.status||'')==='pending_review').length;
    const attention=active.filter(order=>String(order.phase||'')==='ATTENTION_REQUIRED').length;
    const readyPlan=active.filter(order=>String(order.phase||'')==='READY_FOR_PLAN').length;
    const setup=active.filter(order=>String(order.phase||'')==='CREATIVE_SETUP_REQUIRED').length;
    if(failed){badge.textContent=`${failed} 生成失败`;badge.classList.add('is-failed');}
    else if(missing){badge.textContent=`${missing} 需选素材`;badge.classList.add('is-failed');}
    else if(attention){badge.textContent=`${attention} 需处理`;badge.classList.add('is-failed');}
    else if(pendingReview){badge.textContent=`${pendingReview} 待审核`;badge.classList.add('is-ready');}
    else if(readyPlan){badge.textContent=`${readyPlan} 待建广告`;badge.classList.add('is-ready');}
    else if(setup){badge.textContent=`${setup} 待建素材`;badge.classList.add('is-ready');}
    else return;
    badge.hidden=false;
  }

  function syncLaunchProgress(payload) {
    if(!launchState.launch||!launchState.jobs.length)return;
    if(launchHasDeliveryWorkflow()){
      try{localStorage.removeItem(LAUNCH_PROGRESS_STORAGE_KEY);localStorage.removeItem(LEGACY_LAUNCH_PROGRESS_STORAGE_KEY);}catch(_){}
      launchState.lastNotifiedPhase='';
      return;
    }
    const latest=[...(payload.jobs||[]),...(payload.completed_jobs||[])];
    const byId=new Map(latest.map(job=>[String(job.job_id||''),job]));
    launchState.jobs=launchState.jobs.map(job=>{const incoming=byId.get(String(job.job_id||''));if(!incoming)return job;return {...job,...incoming,approved_creative:incoming.approved_creative?.image_id?incoming.approved_creative:(job.approved_creative||{}),latest_image:incoming.latest_image?.image_id?incoming.latest_image:(job.latest_image||{})};});
    const progress=launchProgressSnapshot(),previous=launchState.lastNotifiedPhase;
    const signature=stableHash(progress.jobs.map(job=>({job_id:job.job_id,status:job.status,error_code:job.error_code,completed_at:job.completed_at})));
    if(progress.phase!==previous&&['review','approved','failed'].includes(progress.phase)){
      const message=progress.phase==='approved'?'首批素材已全部通过，AI 正在自动创建暂停态广告':(progress.phase==='review'?`${progress.ready} 组素材正在由 AI 审核`:`${progress.failed} 组素材生成失败，系统已停止并记录异常`);
      showLaunchToast(message);
    }
    launchState.lastNotifiedPhase=progress.phase;
    persistLaunchProgress();updateLaunchProgressBadge();
    loadLaunchOrders({badgeOnly:true,deferredRetry:true});
    const changed=signature!==launchState.lastProgressSignature;
    launchState.lastProgressSignature=signature;
    const panel=document.getElementById('growthLaunchPanel');if(changed&&panel&&!panel.hidden&&launchState.screen==='cold')renderLaunch('cold',{preserveScroll:true});
  }

  function refreshLaunchConfiguration() {
    const configured=typeof window.getAdLaunchConfiguration==='function'?window.getAdLaunchConfiguration():{};
    launchState.target={...launchState.target,app:'Tugao',account_id:String(configured.account_id||launchState.target.account_id||''),account_name:String(configured.account_name||launchState.target.account_name||'')};
    return configured;
  }

  async function validateLaunchPagesForAccount(accountId, options={}) {
    const normalized=String(accountId||'');
    if(!normalized)return;
    const payload=await api('/api/ops/ad-data-dashboard/meta-accounts/page-eligibility',{
      method:'POST',headers:postHeaders('new-account-page-eligibility',{account_id:normalized,country:launchState.target.country,force:options.force===true}),
      body:JSON.stringify({account_id:normalized,country:launchState.target.country,force:options.force===true})
    });
    const verifiedPages=Array.isArray(payload.pages)?payload.pages:[];
    const byId=new Map(launchState.pages.map(item=>[String(item.page_id||''),item]));
    verifiedPages.forEach(item=>byId.set(String(item.page_id||''),item));
    launchState.pages=[...byId.values()];
    launchState.accountPageIds={...launchState.accountPageIds,...(payload.account_page_ids||{})};
    launchState.accountPageOptions={...launchState.accountPageOptions,...(payload.account_page_options||{})};
    const pageId=String(payload.default_page_id||'');
    const selectedPage=verifiedPages.find(item=>item.permission_verified===true&&item.eligible===true&&item.page_id===pageId);
    launchState.target={...launchState.target,page_id:String(selectedPage?.page_id||''),page_name:String(selectedPage?.name||'')};
  }

  async function loadLaunchAccounts(options={}) {
    if(launchState.accountsLoading)return;
    launchState.accountsLoading=true;
    if(launchState.screen==='goal')renderLaunch('goal');
    try {
      const payload=await api('/api/ops/ad-data-dashboard/meta-accounts');
      launchState.accounts=Array.isArray(payload.accounts)?payload.accounts:[];
      launchState.pages=Array.isArray(payload.pages)?payload.pages:[];
      launchState.countryPageIds=payload.country_page_ids&&typeof payload.country_page_ids==='object'?payload.country_page_ids:{};
      launchState.countryAccountIds=payload.country_account_ids&&typeof payload.country_account_ids==='object'?payload.country_account_ids:{};
      launchState.accountPageIds=payload.account_page_ids&&typeof payload.account_page_ids==='object'?payload.account_page_ids:{};
      launchState.accountPageOptions={};
      launchState.accountsLoaded=true;
      const selectable=launchState.accounts.filter(item=>item.selectable===true);
      const recent=String(localStorage.getItem('growth-last-meta-account')||'');
      const countryAccountId=String(launchState.countryAccountIds[String(launchState.target.country||'').toUpperCase()]||'');
      const selected=selectable.find(item=>item.account_id===countryAccountId)||selectable.find(item=>item.account_id===launchState.target.account_id)||selectable.find(item=>item.account_id===recent)||selectable.find(item=>item.account_id===payload.default_account_id)||selectable[0];
      launchState.target={...launchState.target,account_id:String(selected?.account_id||''),account_name:String(selected?.name||''),page_id:'',page_name:''};
      if(selected)await validateLaunchPagesForAccount(selected.account_id,{force:options.force===true});
      launchState.error='';
    } catch(error) {
      launchState.accounts=[];launchState.pages=[];launchState.countryPageIds={};launchState.countryAccountIds={};launchState.accountPageIds={};launchState.accountPageOptions={};launchState.accountsLoaded=true;launchState.target={...launchState.target,account_id:'',account_name:'',page_id:'',page_name:''};launchState.error=readableError(error);
    } finally {
      launchState.accountsLoading=false;
      if(launchState.screen==='goal')renderLaunch('goal');
      else if(launchState.screen==='orders')renderLaunch('orders',{preserveScroll:true});
    }
  }

  function launchPagesForAccount(accountId=launchState.target.account_id) {
    const pageIds=new Set((launchState.accountPageOptions[String(accountId||'')]||[]).map(String));
    if(!pageIds.size)return [];
    return launchState.pages.filter(item=>item.permission_verified===true&&item.eligible===true&&pageIds.has(String(item.page_id||'')));
  }

  function selectedLaunchDirections() {
    return launchState.directions.filter(item=>item.selected);
  }

  async function previewLaunchDirections(options={}) {
    const target=launchState.target;
    launchState.directionsLoading=true;
    launchState.error='';
    if(options.render!==false)renderLaunch('plan');
    try{
      const round=options.regenerate?launchState.regenerationRound+1:launchState.regenerationRound;
      const body={target_app:'tugao',country:target.country,daily_spend_target:Number(target.daily),cpi_target:Number(target.cpi),regeneration_round:round};
      const payload=await api('/api/ops/ad-data-dashboard/new-account-launches/directions/preview',{method:'POST',headers:postHeaders('new-account-direction-preview',body),body:JSON.stringify(body)});
      launchState.directions=Array.isArray(payload.directions)?payload.directions:[];
      launchState.historyEvidence=payload.history_evidence||{};
      launchState.namingRule=payload.naming_rule||{};
      launchState.namingDate=String(payload.naming_date||'');
      launchState.regenerationRound=Number(payload.regeneration_round||0);
    }catch(error){
      launchState.directions=[];
      launchState.error=readableError(error);
    }finally{
      launchState.directionsLoading=false;
      renderLaunch('plan');
    }
  }

  function renderDirectionCandidates() {
    if(launchState.directionsLoading)return '<div class="growth-launch-account-empty"><b>正在读取固定方向库</b>系统会依据国家历史表现排序，不会新增未经治理的方向。</div>';
    if(!launchState.directions.length)return `<div class="growth-launch-config-error"><b>固定方向读取失败</b><span>${esc(launchState.error||'请返回目标页检查输入后重试。')}</span></div>`;
    const sourceLabels={historical_winner:'历史优先',core_catalog:'核心方向',controlled_exploration:'补充探索'};
    return launchState.directions.map((item,index)=>`<article class="growth-direction-card ${item.selected?'is-selected':''}" data-direction-index="${index}"><label class="growth-direction-select"><input type="checkbox" data-direction-select ${item.selected?'checked':''} aria-label="选择${esc(item.title)}方向"><span class="growth-direction-title"><b>${esc(item.title)}</b><small>${esc(item.code)}</small></span><span class="growth-direction-rank">${esc(sourceLabels[item.source]||'固定方向')}</span></label><p class="growth-direction-summary">${esc(item.summary||item.hypothesis)}</p><div class="growth-direction-controls"><label class="growth-direction-budget">首日预算 $<input data-direction-budget type="number" min="5" max="100" step="5" value="${esc(String(item.initial_daily_budget||20))}" aria-label="${esc(item.title)}首日预算"></label><details class="growth-direction-details"><summary>调整实验假设</summary><textarea data-direction-hypothesis maxlength="180" aria-label="${esc(item.title)}实验假设">${esc(item.hypothesis)}</textarea></details></div></article>`).join('');
  }

  function launchLayout(content,active) {
    const section=launchState.screen==='orders'?'广告任务':'创建广告';
    const orderLink=launchState.screen==='orders'?'':'<button type="button" class="growth-launch-orders-link" data-launch-orders>广告任务</button>';
    const canGoBack=!['orders','route'].includes(launchState.screen);
    const backButton=canGoBack?'<button type="button" class="growth-launch-back-dashboard growth-nav-back" data-launch-back-nav aria-label="返回上一步">←</button>':'';
    const title=canGoBack?`广告经营助手<small>广告数据看板 · ${section}</small>`:'广告任务';
    const rootCreate=launchState.screen==='orders'?'<button type="button" class="growth-launch-primary growth-launch-root-create" data-launch-new>创建广告</button>':'';
    return `<div class="growth-launch-main"><section class="growth-launch-page"><header class="growth-launch-topbar ${canGoBack?'':'is-root'}">${backButton}<span class="growth-launch-breadcrumb">${title}</span><span class="growth-launch-top-actions">${rootCreate}${orderLink}<button type="button" class="growth-launch-close" data-launch-close aria-label="关闭">×</button></span></header>${content}</section></div>`;
  }

  function bindLaunchShell() {
    const panel=document.getElementById('growthLaunchPanel');
    panel.querySelectorAll('[data-launch-close]').forEach(button=>button.addEventListener('click',closeLaunchWorkspace));
    panel.querySelectorAll('[data-launch-back-nav]').forEach(button=>button.addEventListener('click',()=>navigateLaunchBack()));
    panel.querySelectorAll('[data-launch-existing]').forEach(button=>button.addEventListener('click',()=>{closeLaunchWorkspace();openWorkspace();}));
    panel.querySelectorAll('[data-launch-new]').forEach(button=>button.addEventListener('click',()=>{resetLaunchOrderContext();launchState.taskHomeView='orders';renderLaunch('mode');}));
    panel.querySelectorAll('[data-launch-orders]').forEach(button=>button.addEventListener('click',()=>{launchState.taskHomeView='orders';renderLaunch('orders');loadLaunchOrders();}));
  }

  function navigateLaunchBack() {
    const previous={cold:'orders','audience-plan':'goal',plan:'goal',goal:'mode',mode:'orders'}[launchState.screen];
    if (!previous) return;
    if(previous==='orders')launchState.taskHomeView='orders';
    renderLaunch(previous);
    if (previous==='orders') loadLaunchOrders();
  }

  function openLaunchWorkspace(options={}) { refreshLaunchConfiguration();const panel=document.getElementById('growthLaunchPanel');panel.hidden=false;document.body.style.overflow='hidden';const requestedView=String(options.taskView||'orders');launchState.taskHomeView=['orders','pending','system','exception','archived'].includes(requestedView)?requestedView:'orders';if(options.openLaunchId)launchState.taskHomeView='orders';renderLaunch(options.startCreate?'mode':'orders');loadLaunchOrders({openLaunchId:String(options.openLaunchId||'')});if(!options.startCreate&&!options.openLaunchId)loadUnifiedTaskIndex();if(!launchState.accountsLoaded)loadLaunchAccounts(); }
  function closeLaunchWorkspace() { clearLaunchMetaRateLimitTimers();clearLaunchDeliveryStatusTimer();const panel=document.getElementById('growthLaunchPanel');if(panel)panel.hidden=true;document.body.style.overflow=''; }

  async function loadUnifiedTaskIndex() {
    if(taskIndexRequest){
      await taskIndexRequest.catch(()=>{});
      if(launchState.screen==='orders')renderLaunch('orders',{preserveScroll:true});
      return;
    }
    launchState.taskIndexLoading=true;
    taskIndexRequest=(async()=>{
      const payload=await api('/api/ops/ad-data-dashboard/experiments?limit=200&task_index=true');
      state.experiments=Array.isArray(payload.items)?payload.items:[];
      const count=document.getElementById('growthWorkspaceCount');
      if(count)count.textContent=String(state.experiments.filter(item=>['action_required','exception'].includes(String((item.workflow||{}).bucket||'action_required'))).length);
    })();
    try{
      await taskIndexRequest;
    }catch(error){
      if(!state.experiments.length)launchState.ordersNotice=readableError(error);
    }finally{
      launchState.taskIndexLoading=false;
      taskIndexRequest=null;
      if(launchState.screen==='orders')renderLaunch('orders',{preserveScroll:true});
    }
  }

  function launchCampaignName(order=launchState.launch||{}) {
    const explicit=String((order.batch_plan||{}).campaign_name||'').trim();
    if(explicit)return explicit;
    const variant=(order.variants||[]).find(item=>String((item.meta_names||{}).campaign||'').trim());
    if(variant)return String(variant.meta_names.campaign).trim();
    const target=order.target||{};
    const mode=String(target.test_variable||target.experiment_mode||'creative_direction');
    const modeLabel={creative_direction:'素材实验',audience_strategy:'受众实验',copy_variant:'文案实验'}[mode]||'广告实验';
    return `${String(target.country||'').toUpperCase()||'-'} · Tugao · ${modeLabel}`;
  }

  function launchModeLabel(order=launchState.launch||{}) {
    const target=order.target||{};
    return {creative_direction:'素材实验',audience_strategy:'受众实验',copy_variant:'文案实验'}[String(target.test_variable||target.experiment_mode||'creative_direction')]||'广告实验';
  }

  function launchAccountName(order=launchState.launch||{}) {
    const target=order.target||{};
    const accountId=String(target.account_id||'').trim();
    const launchId=String(order.launch_id||(order.launch||{}).launch_id||'').trim();
    const indexed=state.experiments.find(item=>String((item.workflow||{}).launch_id||'')===launchId);
    const discovered=launchState.accounts.find(item=>String(item.account_id||'')===accountId);
    const explicit=String(target.account_name||(indexed?.workflow||{}).account_name||discovered?.name||'').trim();
    return explicit||(accountId?`广告账户 ····${accountId.slice(-4)}`:'广告账户');
  }

  function launchTaskItems(bucket) {
    const buckets=new Set(Array.isArray(bucket)?bucket:[bucket]);
    const query=String(launchState.taskSearch||'').trim().toLowerCase();
    return state.experiments.filter(item=>{
      const workflow=item.workflow||{};
      if(!buckets.has(String(workflow.bucket||'action_required')))return false;
      if(!query)return true;
      return `${experimentTitle(item)} ${experimentCampaignName(item)} ${workflow.current_action||''} ${item.country||''}`.toLowerCase().includes(query);
    }).sort((left,right)=>taskPriority(left)-taskPriority(right)||String(right.updated_at||'').localeCompare(String(left.updated_at||'')));
  }

  function renderLaunchOrders() {
    const launches=launchState.launches||[];
    const activeLaunches=launches.filter(item=>!item.archived),archivedLaunches=launches.filter(item=>item.archived);
    const pendingTasks=launchTaskItems(['action_required','exception']),systemTasks=launchTaskItems('system_work'),observingTasks=launchTaskItems('observing'),exceptionTasks=launchTaskItems('exception');
    const aiTasks=[...systemTasks,...observingTasks].sort((left,right)=>taskPriority(left)-taskPriority(right)||String(right.updated_at||'').localeCompare(String(left.updated_at||'')));
    const readyPhases=new Set(['CREATIVE_REVIEW','READY_FOR_PLAN','AD_WORKFLOW','META_REVIEW_PENDING','READY_FOR_ACTIVATION','PAUSED']);
    const visibleOrders=launchState.taskHomeView==='archived'?archivedLaunches:activeLaunches;
    const statusClass=phase=>phase==='ATTENTION_REQUIRED'?'is-attention':(readyPhases.has(phase)?'is-ready':'');
    const nextAction=order=>{
      const deletion=order.permanent_delete||{};
      if(String(deletion.status||'')==='STARTED')return `正在删除 ${Number(deletion.completed_count||0)}/${Number(deletion.target_count||0)}`;
      if(String(deletion.status||'')==='MANUAL_REVIEW')return '删除结果需要核对';
      if(order.archived)return '恢复后继续处理';
      if(order.phase==='ATTENTION_REQUIRED')return '处理异常';
      if(order.phase==='CREATIVE_REVIEW')return '审核生成素材';
      if(order.phase==='READY_FOR_PLAN')return '确认广告计划';
      if(order.phase==='AD_WORKFLOW')return '继续广告创建';
      if(order.phase==='META_REVIEW_PENDING')return '选择要开启或继续暂停的广告';
      if(order.phase==='READY_FOR_ACTIVATION')return '复核并确认启用';
      if(order.phase==='PAUSED')return `启用现有 ${Number(order.experiment_count||0)} 条广告`;
      if(order.phase==='RUNNING')return '查看 D1 / D3 / D7 表现';
      return '查看当前进度';
    };
    const formatTime=value=>{const date=new Date(value);return Number.isNaN(date.getTime())?String(value||'-'):date.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});};
    const rows=visibleOrders.map(order=>{
      const target=order.target||{},jobs=order.jobs||[];
      const deletion=order.permanent_delete||{},deletionStatus=String(deletion.status||''),deleting=deletionStatus==='STARTED',deleteReview=deletionStatus==='MANUAL_REVIEW';
      const workflowMaterialComplete=['READY_FOR_PLAN','AD_WORKFLOW','META_REVIEW_PENDING','READY_FOR_ACTIVATION','PAUSED','RUNNING'].includes(String(order.phase||''));
      const completed=workflowMaterialComplete?Number(order.experiment_count||0):jobs.filter(job=>Boolean(job.approved_creative?.image_id)).length,pendingReview=jobs.filter(job=>job.status==='pending_review').length;
      const strategyLabels={BROAD:'广泛受众',DIGITAL_SELLER:'数字经营人群',FAMILY_HOME:'女性居家人群',SIDE_HUSTLE:'副业与灵活工作'};
      const strategyKeys=Array.isArray(target.audience_strategies)?target.audience_strategies:[];
      const testVariable=String(target.test_variable||target.experiment_mode);
      const audienceText=testVariable==='audience_strategy'
        ? `受众实验 · ${strategyKeys.map(key=>strategyLabels[String(key)]||String(key)).join(' vs ')}`
        : ('素材实验 · 广泛受众');
      const primaryActionLabel=['META_REVIEW_PENDING','PAUSED'].includes(order.phase)?'管理投放':(order.phase==='RUNNING'?'查看投放':'继续处理');
      const actions=deleting
        ? `<div class="growth-launch-delete-running" role="status" aria-live="polite"><b>正在删除 ${Number(deletion.completed_count||0)}/${Number(deletion.target_count||0)}</b><span>可以离开此页面</span></div>`
        : (deleteReview
          ? `<div class="growth-launch-delete-review"><b>删除需要核对</b><span>系统不会自动重复删除</span></div>`
          : (order.archived
        ? `<button type="button" class="growth-launch-secondary growth-launch-order-archive" data-launch-permanent-delete="${esc(order.launch_id)}" title="永久删除订单；投放审计记录会单独保留">永久删除</button><button type="button" class="growth-launch-secondary growth-launch-order-action" data-launch-restore="${esc(order.launch_id)}">恢复订单</button>`
        : `<button type="button" class="growth-launch-secondary growth-launch-order-archive" data-launch-archive="${esc(order.launch_id)}">移至回收站</button><button type="button" class="growth-launch-primary growth-launch-order-action" data-launch-order="${esc(order.launch_id)}">${primaryActionLabel}</button>`));
      const retentionText=order.archived?` · ${esc(String(order.retention_days||7))} 天后自动移除（${esc(formatTime(order.purge_after))}）`:'';
      const displayStatus=deleting?'正在删除':(deleteReview?'删除需核对':(order.archived?'已归档':(order.status_zh||'处理中')));
      const deleteProgress=deleting?`<div class="growth-launch-delete-progress" aria-hidden="true"><i style="width:${Math.max(4,Math.min(100,Number(deletion.progress_percent||0)))}%"></i></div>`:'';
      return `<article class="growth-launch-order-card ${deleting?'is-deleting':(deleteReview?'is-attention':(order.archived?'is-archived':statusClass(order.phase)))}"><div class="growth-launch-order-main"><div class="growth-launch-order-title"><b>${esc(launchCampaignName(order))}</b><span class="growth-launch-order-status ${deleting?'is-deleting':(deleteReview?'is-attention':statusClass(order.phase))}">${esc(displayStatus)}</span></div><div class="growth-launch-order-context">${esc(launchAccountName(order))} · ${esc(String(target.country||'-').toUpperCase())} · ${esc(audienceText||'受众待确认')}</div><div class="growth-launch-order-facts"><div><small>经营目标</small><b>$${esc(String(target.daily_spend_target||'-'))}/天 · CPI ≤ $${esc(String(target.cpi_target||'-'))}</b></div><div><small>素材</small><b>${completed}/${esc(String(order.experiment_count||0))} 已通过${pendingReview?` · ${pendingReview} 待审`:''}</b></div><div class="is-next"><small>下一步</small><b>${esc(nextAction(order))}</b>${deleteProgress}</div></div><div class="growth-launch-order-foot"><span>${order.archived?'归档于':'更新于'} ${esc(formatTime(order.archived_at||order.updated_at))}${retentionText}</span></div></div><div class="growth-launch-order-actions">${actions}</div></article>`;
    }).join('');
    const emptyCopy={
      orders:['还没有广告订单','点击右上角“创建广告”开始第一轮实验。'],
      pending:['没有需要你处理的任务','需要确认、修复或核对异常回执的任务都会显示在这里。'],
      system:['当前没有 AI 运行中的任务','素材生成、方案校验、广告创建、投放观察和下一轮优化都会自动出现在这里。'],
      exception:['当前没有异常','只有系统无法安全判定的真实异常才会出现在这里。'],
      archived:['回收站为空','移入回收站的订单会保留 7 天。'],
    }[launchState.taskHomeView]||['还没有广告订单',''];
    const empty=`<div class="growth-launch-order-empty"><b>${emptyCopy[0]}</b><span>${emptyCopy[1]}</span></div>`;
    const taskItems=launchState.taskHomeView==='pending'?pendingTasks:(launchState.taskHomeView==='system'?aiTasks:(launchState.taskHomeView==='exception'?exceptionTasks:null));
    const taskBody=taskItems===null?'':(launchState.taskIndexLoading&&!state.experiments.length?'<div class="growth-launch-order-empty"><b>正在读取待处理任务</b></div>':(taskItems.length?`<div class="growth-task-list">${taskGroupsHtml(taskItems)}</div>`:empty));
    const body=taskItems!==null?taskBody:(launchState.launchesLoading?'<div class="growth-launch-order-empty"><b>正在读取广告订单</b></div>':(rows||empty));
    const tab=(key,label,count)=>`<button type="button" class="${launchState.taskHomeView===key?'is-active ':''}${key==='exception'&&count?'has-alert':''}" data-launch-task-view="${key}" aria-current="${launchState.taskHomeView===key?'page':'false'}">${label}${count?`<span>${count}</span>`:''}</button>`;
    return `<nav class="growth-launch-orders-summary" aria-label="广告任务筛选">${tab('orders','全部订单',activeLaunches.length)}${tab('pending','需要你处理',pendingTasks.length)}${tab('system','AI 运行中',aiTasks.length)}${tab('exception','异常',exceptionTasks.length)}${tab('archived','回收站',archivedLaunches.length)}</nav>${launchState.ordersNotice?`<div class="growth-notice" role="status">${esc(launchState.ordersNotice)}</div>`:''}<div class="growth-launch-order-list">${body}</div>`;
  }

  async function previewLaunchAudienceExperiment() {
    launchState.error='';launchState.directionsLoading=true;renderLaunch('goal');
    try{
      const country=String(launchState.target.country||'BR').toUpperCase();
      const [preview,creativePayload]=await Promise.all([
        api(`/api/ops/ad-data-dashboard/new-account-launches/audiences/preview?country=${encodeURIComponent(country)}`),
        api('/api/ops/ad-data-dashboard/creative-images?limit=100&target_app=tugao'),
      ]);
      launchState.audiencePreview=preview;
      launchState.approvedCreatives=(creativePayload.images||[]).filter(item=>['approved','used_in_ad'].includes(String(item.review_status||'').toLowerCase()));
      if(!launchState.approvedCreatives.some(item=>String(item.image_id)===launchState.frozenCreativeId))launchState.frozenCreativeId=String(launchState.approvedCreatives[0]?.image_id||'');
      launchState.namingDate=new Date().toISOString().slice(0,10).replaceAll('-','');
      launchState.screen='audience-plan';renderLaunch('audience-plan');
    }catch(error){launchState.error=readableError(error);renderLaunch('goal');}
    finally{launchState.directionsLoading=false;}
  }

  function renderAudienceExperimentPlan() {
    const preview=launchState.audiencePreview||{},rounds=preview.experiment_policy?.rounds||[],strategies=preview.strategies||[],round=rounds[launchState.audienceRound]||rounds[0]||{},strategyByKey=Object.fromEntries(strategies.map(item=>[String(item.strategy_key),item]));
    const creativeCards=launchState.approvedCreatives.map(item=>`<label class="growth-direction-card growth-frozen-creative ${String(item.image_id)===launchState.frozenCreativeId?'is-selected':''}"><input type="radio" name="growthFrozenCreative" value="${esc(item.image_id)}" ${String(item.image_id)===launchState.frozenCreativeId?'checked':''}><img src="/api/ops/ad-data-dashboard/creative-images/${encodeURIComponent(String(item.image_id))}" alt="已审核素材"><span><b>${esc(item.ad_name||item.creative_direction||'已审核素材')}</b><small>${esc(item.market||'-')} · ${esc(String(item.created_at||'').slice(0,10))}</small></span></label>`).join('');
    const roundOptions=rounds.map((item,index)=>{const challenger=strategyByKey[String(item.challenger)]||{};return `<option value="${index}" ${index===launchState.audienceRound?'selected':''}>第 ${index+1} 轮：广泛受众 vs ${esc(challenger.label||item.challenger)}</option>`;}).join('');
    const baseline=strategyByKey[String(round.baseline)]||{},challenger=strategyByKey[String(round.challenger)]||{};
    const estimate=item=>item.delivery_estimate?.lower?`${Math.round(item.delivery_estimate.lower/10000)}–${Math.round(item.delivery_estimate.upper/10000)} 万`:'待估算';
    return `<span class="growth-launch-kicker">受众实验</span><h1>固定一张素材，只比较受众策略</h1><p>本轮素材、文案、预算和基础人群完全相同；唯一变量是 Meta 细分定位。</p><section class="growth-launch-account"><header><div><b>选择获胜素材</b><small>只显示已审核通过的 Tugao 素材；两组广告共用同一张图和同一套文案。</small></div></header><div class="growth-direction-list">${creativeCards||'<div class="growth-launch-account-empty"><b>没有可用的已审核素材</b>请先在素材工作台审核一张获胜素材。</div>'}</div></section><section class="growth-launch-account"><header><div><b>选择受众实验轮次</b><small>基础条件固定为巴西、女性 18–40 岁、葡萄牙语；赋能受众关闭。</small></div></header><label>实验轮次<select id="growthAudienceRound">${roundOptions}</select></label><div class="growth-launch-plan"><article><b>基准组</b><strong>${esc(baseline.label||'-')}</strong><span>预估覆盖 ${esc(estimate(baseline))}</span><small>无细分定位</small></article><article><b>挑战组</b><strong>${esc(challenger.label||'-')}</strong><span>预估覆盖 ${esc(estimate(challenger))}</span><small>${esc((challenger.meta_targeting_ids||[]).length)} 个已验证 Meta 定位 ID</small></article></div></section><div class="growth-safety"><b>单变量保护</b> 服务端会拒绝不同素材、不同文案、非等预算或未关闭 Advantage 受众扩展的方案。Meta 随机分流回读未通过前，真实创建保持关闭。</div>${launchState.error?`<div class="growth-error">${esc(launchState.error)}</div>`:''}<div class="growth-launch-actions is-sticky"><button type="button" class="growth-launch-secondary" data-launch-back>返回修改目标</button><button type="button" class="growth-launch-primary" data-launch-audience-approve ${launchState.frozenCreativeId&&round.baseline?'':'disabled'}>创建两组受众实验</button></div>`;
  }

  async function previewLaunchCopyExperiment() {
    launchState.error='';launchState.directionsLoading=true;renderLaunch('goal');
    try{
      const payload=await api('/api/ops/ad-data-dashboard/creative-images?limit=100&target_app=tugao');
      launchState.approvedCreatives=(payload.images||[]).filter(item=>['approved','used_in_ad'].includes(String(item.review_status||'').toLowerCase()));
      if(!launchState.approvedCreatives.some(item=>String(item.image_id)===launchState.frozenCreativeId))launchState.frozenCreativeId=String(launchState.approvedCreatives[0]?.image_id||'');
      const country=String(launchState.target.country||'BR').toUpperCase();
      const keys=['points_reward','easy_start'];
      launchState.copyVariants=keys.map((key,index)=>{const copy=batchAdCopy(country,{key}),evidence=batchAdCopyEvidence(country,{key});return {key,role:index===0?'BASELINE':'CHALLENGER',primary_text:copy[0],headline:copy[1],description:copy[2],hypothesis:evidence.copy_hypothesis,benchmark_version:evidence.copy_benchmark_version};});
      launchState.namingDate=new Date().toISOString().slice(0,10).replaceAll('-','');
      renderLaunch('copy-plan');
    }catch(error){launchState.error=readableError(error);renderLaunch('goal');}
    finally{launchState.directionsLoading=false;}
  }

  function renderCopyExperimentPlan() {
    const creativeCards=launchState.approvedCreatives.map(item=>`<label class="growth-direction-card growth-frozen-creative ${String(item.image_id)===launchState.frozenCreativeId?'is-selected':''}"><input type="radio" name="growthFrozenCreative" value="${esc(item.image_id)}" ${String(item.image_id)===launchState.frozenCreativeId?'checked':''}><img src="/api/ops/ad-data-dashboard/creative-images/${encodeURIComponent(String(item.image_id))}" alt="已审核素材"><span><b>${esc(item.ad_name||item.creative_direction||'已审核素材')}</b><small>${esc(item.market||'-')} · ${esc(String(item.created_at||'').slice(0,10))}</small></span></label>`).join('');
    const copies=launchState.copyVariants.map((item,index)=>`<article class="growth-direction-card is-selected" data-copy-variant="${index}"><label>实验角色<input value="${index===0?'基准文案':'挑战文案'}" readonly></label><label>标题<input data-copy-headline maxlength="80" value="${esc(item.headline)}"></label><label>主要文案<textarea data-copy-primary maxlength="500">${esc(item.primary_text)}</textarea></label><label>描述<input data-copy-description maxlength="120" value="${esc(item.description)}"></label><label>要验证什么<input data-copy-hypothesis maxlength="180" value="${esc(item.hypothesis)}"></label></article>`).join('');
    return `<span class="growth-launch-kicker">文案实验</span><h1>同一张图，只比较两套文案</h1><p>受众、预算、版位和素材保持一致；基准文案与挑战文案随机分流，结果按安装、CPI、CTR、真实入会和 CPA 评估。</p><section class="growth-launch-account"><header><div><b>选择已审核素材</b><small>两组广告共用这一张图。</small></div></header><div class="growth-direction-list">${creativeCards||'<div class="growth-launch-account-empty"><b>没有可用的已审核素材</b>请先审核一张素材。</div>'}</div></section><section class="growth-launch-account"><header><div><b>确认两套本地化文案</b><small>已按当前国家载入不同利益点；可以编辑，但两组必须保持明显差异。</small></div></header><div class="growth-direction-list">${copies}</div></section><div class="growth-safety"><b>单变量保护</b> 服务端会拒绝不同图片、不同受众、不同预算或相同文案。随机实验预检未通过前不会创建 Meta 对象。</div>${launchState.error?`<div class="growth-error">${esc(launchState.error)}</div>`:''}<div class="growth-launch-actions is-sticky"><button type="button" class="growth-launch-secondary" data-launch-back>返回修改目标</button><button type="button" class="growth-launch-primary" data-launch-copy-approve ${launchState.frozenCreativeId?'':'disabled'}>创建两组文案实验</button></div>`;
  }

  function renderLaunch(screen,options={}) {
    const previousScreen=launchState.screen;
    const preserveScroll=Boolean(options.preserveScroll&&previousScreen===screen);
    const panel=document.getElementById('growthLaunchPanel');
    const previousDrawer=panel?.querySelector('.growth-launch-drawer');
    const previousScrollTop=preserveScroll?Number(previousDrawer?.scrollTop||0):0;
    launchState.screen=screen;
    if(screen!=='cold')clearLaunchDeliveryStatusTimer();
    if(panel&&!preserveScroll)panel.scrollTop=0;
    const node=document.getElementById('growthLaunchContent');
    if(screen==='route') node.innerHTML=launchLayout(`<div class="growth-route-hero"><h1>选择任务</h1></div><div class="growth-launch-routes"><button type="button" data-launch-existing><span class="growth-route-card-head"><small class="growth-route-tag">已有投放</small></span><b>优化现有账户</b><p>查看指标、异常和调整建议。</p><em>进入优化闭环 →</em></button><button type="button" data-launch-new><span class="growth-route-card-head"><small class="growth-route-tag">从 0 开始</small></span><b>启动新账户</b><p>设置目标并创建首批素材实验。</p><em>设置经营目标 →</em></button></div>`,'route');
    if(screen==='orders') node.innerHTML=launchLayout(renderLaunchOrders(),'new');
    if(screen==='mode') node.innerHTML=launchLayout(`<span class="growth-launch-kicker">创建广告</span><h1>这次要验证什么？</h1><p>先确定唯一变量，后续计划、素材和受众都会按该模式锁定，不能混在同一轮里。</p><div class="growth-launch-routes"><button type="button" data-launch-mode="creative_direction"><span class="growth-route-card-head"><small class="growth-route-tag">探索新素材</small></span><b>素材实验</b><p>固定广泛受众，比较 2–4 个素材方向。</p><em>相同受众 · 不同素材 →</em></button><button type="button" data-launch-mode="copy_variant"><span class="growth-route-card-head"><small class="growth-route-tag">本轮推荐</small></span><b>文案实验</b><p>复用已验证素材，只比较两套本地化文案。</p><em>同图同受众 · 只改文案 →</em></button><button type="button" data-launch-mode="audience_strategy"><span class="growth-route-card-head"><small class="growth-route-tag">放大获胜素材</small></span><b>受众实验</b><p>固定一张已审核获胜素材，只比较两种受众策略。</p><em>相同素材 · 不同受众 →</em></button></div><div class="growth-launch-actions"><button type="button" class="growth-launch-secondary" data-launch-back>返回广告任务</button></div>`,'new');
    if(screen==='goal') {
      refreshLaunchConfiguration();applyFixedLaunchAudience(launchState.target.country);
      const selectable=launchState.accounts.filter(item=>item.selectable===true),accountPages=launchPagesForAccount(),accountReady=Boolean(launchState.target.account_id&&selectable.some(item=>item.account_id===launchState.target.account_id)),pageReady=Boolean(launchState.target.page_id&&accountPages.some(item=>item.page_id===launchState.target.page_id));
      const accountOptions=launchState.accounts.map(item=>`<option value="${esc(item.account_id)}" ${item.account_id===launchState.target.account_id?'selected':''} ${item.selectable?'':'disabled'}>${esc(item.name)} · 尾号 ${esc(item.account_id.slice(-4))} · ${esc(item.selectable?'可用':item.disabled_reason||'不可用')}</option>`).join('');
      const pageOptions=accountPages.map(item=>`<option value="${esc(item.page_id)}" ${item.page_id===launchState.target.page_id?'selected':''}>${esc(item.name)} · 尾号 ${esc(item.page_id.slice(-4))} · 权限实测通过</option>`).join('');
      const audience=fixedLaunchAudience(launchState.target.country);
      const modeAudience=launchState.experimentMode==='audience_strategy',modeCopy=launchState.experimentMode==='copy_variant';
      const modeTitle=modeAudience?'受众实验':(modeCopy?'文案实验':'素材实验');
      const modeIntro=modeAudience?'固定获胜素材，只比较不同受众策略。首轮目前只开放已验证的巴西两两实验。':(modeCopy?'复用同一张已审核素材，只比较两套墨西哥本地化文案。':'固定同一受众，比较不同素材方向。');
      const submitLabel=modeAudience?'选择获胜素材与受众':(modeCopy?'选择素材并确认两套文案':'推荐首批素材方向');
      node.innerHTML=launchLayout(`<span class="growth-launch-kicker">${modeTitle} · 经营目标</span><h1>设置 Tugao 新账户目标</h1><p>${modeIntro} 国家对应的基础人群由系统严格锁定，确认前不会创建广告。</p><div class="growth-launch-product"><span>推广应用</span><b>Tugao</b><small>固定应用配置由系统维护</small></div><section class="growth-launch-account"><header><div><b>Meta 投放身份</b><small>系统逐一实测该账户可用的公共主页；不按名称或国家预先排除，校验不会创建广告</small></div><button type="button" class="growth-launch-secondary" data-launch-sync>${launchState.accountsLoading?'验证中…':'重新同步并验证'}</button></header>${launchState.accountsLoading?'<div class="growth-launch-account-empty">正在验证广告账户与公共主页完整投放权限…</div>':`<label>广告账户${launchState.accounts.length?`<select id="growthLaunchAccount" aria-label="选择 Meta 广告账户">${accountOptions}</select>`:'<span class="growth-launch-account-empty"><b>未发现可用广告账户</b>请检查 Token 权限。</span>'}</label><label>公共主页${accountReady&&accountPages.length?`<select id="growthLaunchPage" aria-label="选择 Meta 公共主页">${pageOptions}</select>`:'<span class="growth-launch-account-empty"><b>未找到该账户可投放的公共主页</b>请检查账户、主页与 Tugao 应用的广告权限后重新同步。</span>'}</label>`}</section>${launchState.error?`<div class="growth-launch-config-error"><b>投放身份读取失败</b><span>${esc(launchState.error)}</span></div>`:''}<form id="growthLaunchGoal" class="growth-launch-form growth-launch-goal-form"><label>目标国家<select id="growthLaunchCountry" name="country" ${modeAudience?'disabled':''}><option value="BR" ${launchState.target.country==='BR'?'selected':''}>巴西（BR）</option><option value="MX" ${launchState.target.country==='MX'?'selected':''}>墨西哥（MX）</option><option value="ID" ${launchState.target.country==='ID'?'selected':''}>印度尼西亚（ID）</option></select></label><label>目标日消耗（美元）<input name="daily" value="${esc(launchState.target.daily)}" inputmode="decimal" required></label><label>CPI 上限（美元）<input name="cpi" value="${esc(launchState.target.cpi)}" inputmode="decimal" required></label><div class="growth-launch-audience-lock"><span>基础人群（国家规则）</span><b>女性 · 18–40 岁 · ${esc(audience?.language_label||'-')}</b><small>严格限制：性别和年龄“用作建议”均关闭，Advantage+ 受众扩展关闭。</small></div><div class="growth-launch-actions"><button type="button" class="growth-launch-secondary" data-launch-back>返回</button><button class="growth-launch-primary" ${accountReady&&pageReady?'':'disabled'}>${submitLabel}</button></div></form>`,'new');
    }
    if(screen==='plan') { const genderLabel={all:'不限性别',female:'女性',male:'男性'}[launchState.target.gender]||'不限性别',languageLabel={pt_BR:'葡萄牙语',es_419:'西班牙语（拉美）',id_ID:'印度尼西亚语',en_US:'英语'}[launchState.target.language]||launchState.target.language,selected=selectedLaunchDirections(),history=launchState.historyEvidence||{},historyHtml=history.available?`<div class="growth-history-note"><b>近 90 天参考</b><span><small>素材</small><strong>${esc(String(history.creative_count||0))}</strong></span><span><small>CPI</small><strong>$${esc(String(history.cpi??'-'))}</strong></span><span><small>CTR</small><strong>${esc(String(history.ctr??'-'))}%</strong></span><span><small>真实入会</small><strong>${esc(String(history.real_joins||0))}</strong></span></div>`:`<div class="growth-history-note"><b>历史证据不足</b><span><small>当前策略</small><strong>固定方向受控探索</strong></span></div>`;node.innerHTML=launchLayout(`<span class="growth-launch-kicker">方案确认</span><h1>选择首批素材实验</h1><p>推广应用：Tugao · ${esc(launchState.target.country)} · ${esc(genderLabel)} · ${esc(launchState.target.age_min)}-${esc(launchState.target.age_max)} 岁 · ${esc(languageLabel)}<br>广告账户：${esc(launchState.target.account_name||`尾号 ${launchState.target.account_id.slice(-4)}`)} · 公共主页：${esc(launchState.target.page_name||`尾号 ${launchState.target.page_id.slice(-4)}`)}</p>${historyHtml}<div class="growth-direction-head"><div><b>固定素材方向</b><small>方向由 Tugao 素材规范统一维护；你可以选择方向、调整实验假设和首日预算。</small></div><em>已选 ${selected.length} / ${launchState.directions.length}</em></div><div class="growth-direction-list">${renderDirectionCandidates()}</div><div class="growth-launch-plan"><article><b>实验结构</b><strong>1 个 Campaign · ${selected.length} 个实验格</strong><span>每个实验只改变一个素材方向</span><small>所有 Meta 对象保持暂停，启用另行确认</small></article><article><b>首日预算与验收</b><strong>$${esc(String(selected.reduce((sum,item)=>sum+Number(item.initial_daily_budget||0),0)))}</strong><span>安装、CPI、CTR、真实入会、CPA</span><small>D1 / D3 / D7 自动复盘</small></article></div>${launchState.error?`<div class="growth-error">${esc(launchState.error)}</div>`:''}<div class="growth-launch-actions is-sticky"><button type="button" class="growth-launch-secondary" data-launch-back>返回修改目标</button><button type="button" class="growth-launch-primary" data-launch-approve ${selected.length<2||launchState.directionsLoading?'disabled':''}>创建 ${selected.length} 个实验与素材任务</button></div>`,'new'); }
    if(screen==='audience-plan') node.innerHTML=launchLayout(renderAudienceExperimentPlan(),'new');
    if(screen==='copy-plan') node.innerHTML=launchLayout(renderCopyExperimentPlan(),'new');
    if(screen==='cold') {
      const readyView=['audience_strategy','copy_variant'].includes(launchState.experimentMode)?renderAudienceLaunchReady():renderColdLaunch();
      node.innerHTML=launchLayout(launchHasDeliveryWorkflow()?renderColdLaunch():readyView,'new');
    }
    const drawer=panel?.querySelector('.growth-launch-drawer');if(drawer)drawer.scrollTop=preserveScroll?previousScrollTop:0;
    bindLaunchShell();bindLaunchScreen(screen);bindLaunchMaterialActions(node);
  }

  function renderColdLaunch() {
    const variants=((launchState.launch||{}).variants||[]),initialTotal=variants.reduce((sum,item)=>sum+Number(item.initial_daily_budget||0),0),secondTotal=Math.min(Number(launchState.target.daily||0),initialTotal*2);
    const steps=[['目标设置','已完成',`$${initialTotal} / 天`,'目标、方向与 KPI 已确认'],['账户准备','已完成',`$${initialTotal} / 天`,'短码命名与实验结构已生成'],['冷启动样本','进行中',`$${secondTotal} / 天`, `安装 ≥ 100，CPI ≤ $${esc(launchState.target.cpi)}`],['稳定放量','未开放',`$${esc(launchState.target.daily)} / 天`,'五项核心指标达到放量门槛']];
    const rail=steps.map((item,index)=>`<article class="growth-launch-step ${index===2?'is-current':''}"><h3><span>${index<2?'✓':index+1}</span>${item[0]}<small> ${item[1]}</small></h3><small>${index===2?'当前预算档位':index===3?'目标预算档位':'预算档位'}</small><strong>${item[2]}</strong><p class="${index<3?'is-met':''}">${index<3?'✓':'○'} ${item[3]}</p>${index===2?'<p>○ 真实入会 ≥ 10（累计）</p><p>○ CPI、CTR、CPA 均可计算</p>':''}</article>`).join('');
    const facts=[['安装','0','待积累'],['CPI','-','待计算'],['CTR','-','待计算'],['真实入会','0','待积累'],['CPA','-','待计算']].map(item=>`<article><small>${item[0]}</small><strong>${item[1]}</strong><span>${item[2]}</span></article>`).join('');
    const progress=launchProgressSnapshot(),expectedCount=progress.expected,jobCount=progress.jobs.length,tasksCreated=expectedCount>0&&jobCount===expectedCount;
    const progressPercent=expectedCount?Math.round(((progress.ready+progress.approved+progress.failed+progress.missing)/expectedCount)*100):0;
    const statusLabels={pending:'排队中',pending_upload:'等待生成',claimed:'处理中',generating:'生成中',pending_review:'待审核',completed:'已完成',failed:'生成失败',rejected:'生成失败',cancelled:'已取消',expired:'已过期'};
    const materialCards=variants.map((variant,index)=>{
      const job=progress.jobs[index]||{},image=job.latest_image||{},status=String(job.status||'pending'),review=String(image.review_status||'').toLowerCase();
      const approved=Boolean(job.approved_creative?.image_id)||review==='approved',ready=Boolean(image.image_id)&&!approved&&!['archived','rejected','deleted'].includes(review);
      const failed=['failed','rejected','cancelled','expired'].includes(status),missing=status==='completed'&&!approved&&!image.image_id;
      const label=variant.meta_names?.ad||variant.experiment_code||`素材 ${index+1}`;
      const visibleStatus=status==='generating'&&image.image_id?'正在生成新版本':(approved?'已通过':(ready?'待审核':(failed?'生成失败':(missing?'素材未关联':(statusLabels[status]||status)))));
      const statusClass=approved||ready?'is-ready':(failed||missing?'is-failed':'');
      const imageUrl=image.image_id?`/api/ops/ad-data-dashboard/creative-images/${encodeURIComponent(image.image_id)}`:'';
      const thumb=imageUrl?`<img src="${esc(imageUrl)}" alt="${esc(label)} 素材预览" loading="lazy">`:`<span class="growth-material-thumb-placeholder">${failed?'生成未完成，请重新生成':(missing?'素材绑定信息缺失，请核对':'素材生成后会自动显示在这里')}</span>`;
      const approveAction=image.image_id&&ready?`<button type="button" class="growth-launch-primary" data-material-approve="${esc(image.image_id)}">通过</button>`:'<button type="button" class="growth-launch-secondary" disabled>通过</button>';
      const redoAction=job.job_id?`<button type="button" class="growth-launch-secondary" data-material-regenerate="${esc(job.job_id)}">重做</button>`:'<button type="button" class="growth-launch-secondary" disabled>重做</button>';
      const actions=`${approveAction}${redoAction}`;
      return `<article class="growth-material-card"><button type="button" class="growth-material-thumb" ${image.image_id?`data-material-preview="${esc(image.image_id)}" data-material-job="${esc(job.job_id||'')}" data-material-label="${esc(label)}"`:'disabled'}>${thumb}</button><div class="growth-material-card-body"><b class="growth-material-card-title" title="${esc(label)}">${esc(label)}</b><div class="growth-material-card-meta"><span>${esc(variant.creative_angle||'素材方向')}</span><span class="growth-material-card-status ${statusClass}">${esc(visibleStatus)}</span></div></div>${actions?`<div class="growth-material-card-actions">${actions}</div>`:''}</article>`;
    }).join('');
    const progressHtml=`<section class="growth-launch-progress-card" aria-live="polite"><div class="growth-launch-progress-head"><b>素材审核</b><span>${progress.approved} / ${expectedCount} 已通过</span></div><div class="growth-launch-progress-track"><i style="width:${progressPercent}%"></i></div><div class="growth-launch-progress-counts"><span>生成中 <b>${progress.processing}</b></span><span>待审核 <b>${progress.ready}</b></span><span>已通过 <b>${progress.approved}</b></span><span>待核对 <b>${progress.missing}</b></span><span>异常 <b>${progress.failed}</b></span></div><div class="growth-material-grid">${materialCards}</div></section>`;
    const workflowState=launchState.experimentStates.find(value=>['CREATION_PARTIAL_FAILURE','DATA_INCOMPLETE'].includes(value))||launchState.experimentStates.find(value=>['RUNNING','MATURING'].includes(value))||launchState.experimentStates.find(value=>value==='READY_FOR_ACTIVATION')||launchState.experimentStates.find(value=>value==='META_REVIEW_PENDING')||launchState.experimentStates.find(value=>value==='PAUSED')||'';
    const primaryLabel=!tasksCreated?'重试创建素材任务':'';
    const stripText=progress.phase==='approved'?'全部素材已通过，AI 正在自动创建暂停态广告':(progress.phase==='review'?`${progress.ready} 张素材正在由 AI 审核`:(progress.phase==='failed'?`${progress.failed} 张素材生成失败，系统已停止并记录异常`:(progress.phase==='needs_selection'?`${progress.missing} 张素材缺少绑定信息，请核对`:`正在生成 ${progress.processing}/${expectedCount} · 完成后自动审核并继续创建`)));
    const order=launchState.launch||{},campaignName=launchCampaignName(order),modeLabel=launchModeLabel(order),accountName=launchAccountName(order);
    if(launchState.orderDataMismatch)return `<header class="growth-launch-head growth-launch-cold-head"><div class="growth-launch-order-identity"><span>广告订单</span><h1>${esc(campaignName)}</h1><p>${esc(String(launchState.target.country||'-').toUpperCase())} · Tugao · 广告账户：${esc(accountName)} · ${esc(modeLabel)} · $${esc(launchState.target.daily)}/天 · CPI ≤ $${esc(launchState.target.cpi)}</p></div><button type="button" class="growth-launch-secondary" data-launch-view-target>查看目标</button></header><section class="growth-launch-cold"><div class="growth-launch-cold-status"><div><span class="growth-launch-kicker">系统核对中</span><h2>订单与广告信息暂未匹配</h2><p>系统已隐藏投放状态和操作，正在按当前订单重新读取；不会操作其他订单的广告。</p></div></div><div class="growth-launch-delivery-note">请刷新广告任务后重试；订单归属核对通过前不能开启或关闭广告。</div></section>`;
    const materialHeader=`<div class="growth-material-status-strip ${progress.phase==='failed'?'is-attention':''}"><b>${esc(stripText)}</b><span>点击图片查看大图，下方选择通过或重做</span></div>`;
    return `<header class="growth-launch-head growth-launch-cold-head"><div class="growth-launch-order-identity"><span>广告订单</span><h1>${esc(campaignName)}</h1><p>${esc(String(launchState.target.country||'-').toUpperCase())} · Tugao · 广告账户：${esc(accountName)} · ${esc(modeLabel)} · $${esc(launchState.target.daily)}/天 · CPI ≤ $${esc(launchState.target.cpi)}</p></div><button type="button" class="growth-launch-secondary" data-launch-view-target>查看目标</button></header><section class="growth-launch-cold">${workflowState?renderLaunchDeliveryControls():`${materialHeader}${progressHtml}`}${!workflowState&&primaryLabel?`<footer class="growth-launch-cold-actions"><button type="button" class="growth-launch-primary" data-launch-material>${primaryLabel}</button></footer>`:''}</section>`;
  }

  function materialJobByImage(imageId) { return (launchState.jobs||[]).find(job=>String(job.latest_image?.image_id||'')===String(imageId||''))||{}; }
  function openLaunchMaterialPreview(imageId,label='') {
    const job=materialJobByImage(imageId),image=job.latest_image||{},approved=Boolean(job.approved_creative?.image_id)||String(image.review_status||'').toLowerCase()==='approved',imageUrl=`/api/ops/ad-data-dashboard/creative-images/${encodeURIComponent(imageId)}`;
    showModal(`<section class="growth-modal growth-material-preview" role="dialog" aria-modal="true" aria-labelledby="growthMaterialPreviewTitle"><header class="growth-modal-head"><div><b id="growthMaterialPreviewTitle">素材预览</b><small>${esc(label||job.experiment_code||'当前素材')}</small></div><button type="button" class="growth-icon-button" data-modal-close aria-label="关闭">×</button></header><div class="growth-modal-body"><div class="growth-material-preview-stage"><img src="${esc(imageUrl)}" alt="${esc(label||'广告素材')} 大图预览"></div><div class="growth-material-preview-copy"><span>${approved?'已审核通过':'请确认画面、文案和风险信息后再通过'}</span></div></div><footer class="growth-modal-foot"><button type="button" class="growth-primary" ${approved?'disabled':`data-material-approve="${esc(imageId)}"`}>通过</button><button type="button" ${job.job_id?`data-material-regenerate="${esc(job.job_id)}"`:'disabled'}>重做</button></footer></section>`);bindLaunchMaterialActions(document.querySelector('#growthLaunchModal:not([hidden])'));
  }
  async function approveLaunchMaterial(imageId,button) { const body={review_status:'APPROVED',checks:{feed_static_ad_structure:true,simulation_review:true}};try{button.disabled=true;button.textContent='正在确认…';await api(`/api/ops/ad-data-dashboard/creative-images/${encodeURIComponent(imageId)}/review`,{method:'POST',headers:postHeaders('new-account-material-review',{image_id:imageId,...body}),body:JSON.stringify(body)});const launchId=String(launchState.launch?.launch_id||'');closeModal();await refreshLaunchDeliveryOrder(launchId);showLaunchToast('素材已通过审核');}catch(error){button.disabled=false;button.textContent='通过';const message=`审核失败：${readableError(error)}`;showModalError(message);showLaunchToast(message);} }
  async function regenerateLaunchMaterial(jobId,button) { if(!window.confirm('确认生成一个新版本？当前已通过素材会保留，不会删除或失效。'))return;const body={image_size:'1024x1024',candidate_count:1,max_attempts:3,force_regenerate:true};try{button.disabled=true;button.textContent='正在创建新版本…';await api(`/api/ops/creative-pro-jobs/${encodeURIComponent(jobId)}/start-generation`,{method:'POST',headers:postHeaders('new-account-material-regenerate',{job_id:jobId,...body}),body:JSON.stringify(body)});const launchId=String(launchState.launch?.launch_id||'');closeModal();await refreshLaunchDeliveryOrder(launchId);showLaunchToast('新版本已开始生成，当前素材继续保留');}catch(error){button.disabled=false;button.textContent='重做';const message=`重新生成失败：${readableError(error)}`;showModalError(message);showLaunchToast(message);} }
  function bindLaunchMaterialActions(root) { if(!root)return;root.querySelectorAll('[data-material-preview]').forEach(button=>button.addEventListener('click',()=>openLaunchMaterialPreview(button.dataset.materialPreview||'',button.dataset.materialLabel||'')));root.querySelectorAll('[data-material-approve]').forEach(button=>button.addEventListener('click',()=>approveLaunchMaterial(button.dataset.materialApprove||'',button)));root.querySelectorAll('[data-material-regenerate]').forEach(button=>button.addEventListener('click',()=>regenerateLaunchMaterial(button.dataset.materialRegenerate||'',button))); }

  function openLaunchTargetSummary() {
    const target=launchState.target||{},launch=launchState.launch||{},batch=launchState.batchPlan||{},campaignName=launchCampaignName(launch),modeLabel=launchModeLabel(launch);
    const genderLabel={female:'女性',male:'男性',all:'不限性别'}[String(target.gender||'all')]||String(target.gender||'-');
    const languageLabel={pt_BR:'葡萄牙语（巴西）',es_419:'西班牙语（拉美）',id_ID:'印度尼西亚语',en_US:'英语'}[String(target.language||'')]||String(target.language||'-');
    const accountLabel=target.account_name||`广告账户尾号 ${String(target.account_id||'').slice(-4)||'—'}`,pageId=String(batch.current_page_id||target.page_id||''),pageLabel=target.page_name||`公共主页尾号 ${pageId.slice(-4)||'—'}`,canRepair=batch.page_repair_available===true;
    showModal(`<section class="growth-modal growth-target-modal" role="dialog" aria-modal="true" aria-labelledby="growthLaunchTargetTitle"><header class="growth-modal-head"><div><b id="growthLaunchTargetTitle">订单目标</b><small>${esc(campaignName)}</small></div><button type="button" class="growth-icon-button" data-modal-close aria-label="关闭">×</button></header><div class="growth-modal-body"><div class="growth-target-summary"><article><small>经营目标</small><strong>$${esc(String(target.daily||'-'))}/天 · CPI ≤ $${esc(String(target.cpi||'-'))}</strong><span>${esc(String(target.country||'-'))} · Tugao</span></article><article><small>实验设置</small><strong>${esc(modeLabel)}</strong><span>单变量 · D1 / D3 / D7 评估</span></article></div><div class="growth-target-details"><div class="growth-target-detail"><b>固定受众</b><span>${esc(genderLabel)} · ${esc(String(target.age_min||'-'))}–${esc(String(target.age_max||'-'))} 岁 · ${esc(languageLabel)}</span><small>不可改</small></div><div class="growth-target-detail"><b>Meta 身份</b><span>${esc(accountLabel)} · ${esc(pageLabel)}</span><small>${canRepair?'主页异常':'已确认'}</small></div></div>${canRepair?'<div class="growth-target-problem"><b>当前公共主页未通过 Meta 校验</b><span>可在这个旧订单里只修改公共主页；广告账户、Campaign、广告组和素材保持不变。</span></div>':''}</div><footer class="growth-modal-foot"><button type="button" data-modal-close>关闭</button>${canRepair?'<button type="button" class="growth-primary" id="growthEditOrderPage">修改公共主页</button>':''}</footer></section>`);
    document.getElementById('growthEditOrderPage')?.addEventListener('click',()=>openLaunchPageRepair(String(batch.plan_id||''),pageId));
    document.querySelector('#growthLaunchModal:not([hidden]) [data-modal-close]')?.focus();
  }

  async function openLaunchPageRepair(planId,currentPageId='') {
    const launchId=String(launchState.launch?.launch_id||''),campaignName=launchCampaignName(launchState.launch||{}),accountId=String(launchState.target?.account_id||''),accountName=String(launchState.target?.account_name||`广告账户尾号 ${accountId.slice(-4)||'—'}`),oldPage=String(currentPageId||launchState.batchPlan?.current_page_id||launchState.target?.page_id||'');
    if(!planId||!accountId){showModalError('当前订单缺少可修复的投放身份记录，请刷新订单后重试。');return;}
    showModal(`<section class="growth-modal growth-modal-compact"><header class="growth-modal-head"><div><b>核对可用主页</b><small>${esc(campaignName)}</small></div></header><div class="growth-modal-body"><p>正在读取该广告账户已经验证过的公共主页…</p></div></section>`);
    await loadLaunchAccounts({force:true});
    const pages=launchPagesForAccount(accountId),options=pages.map(item=>`<option value="${esc(String(item.page_id||''))}">${esc(String(item.name||'公共主页'))} · 尾号 ${esc(String(item.page_id||'').slice(-4))}</option>`).join('');
    if(!options){showModal(`<section class="growth-modal growth-modal-compact"><header class="growth-modal-head"><div><b>没有可安全使用的主页</b><small>${esc(campaignName)}</small></div><button type="button" class="growth-icon-button" data-modal-close aria-label="关闭">×</button></header><div class="growth-modal-body"><div class="growth-page-repair-error">系统没有找到该广告账户历史投放中已验证成功的公共主页，因此不会让你盲选或再次提交。</div></div><footer class="growth-modal-foot"><button type="button" data-modal-close>关闭</button></footer></section>`);return;}
    showModal(`<section class="growth-modal growth-target-modal" role="dialog" aria-modal="true" aria-labelledby="growthPageRepairTitle"><header class="growth-modal-head"><div><b id="growthPageRepairTitle">修改当前订单的公共主页</b><small>${esc(campaignName)}</small></div><button type="button" class="growth-icon-button" data-modal-close aria-label="关闭">×</button></header><div class="growth-modal-body"><div class="growth-page-repair-context"><div class="growth-page-repair-note">只修改这个旧订单的投放身份。Campaign、广告组和已通过素材都会保留；系统会为新主页创建新的广告素材对象，再从未完成的广告继续，全部保持暂停。</div><div class="growth-page-repair-identity"><label><span>广告账户</span><strong>${esc(accountName)} · 尾号 ${esc(accountId.slice(-4))}</strong></label><label><span>当前异常主页</span><strong>尾号 ${esc(oldPage.slice(-4)||'—')}</strong></label><label><span>改为</span><select id="growthRepairPageSelect">${options}</select></label></div><div id="growthRepairPageError" class="growth-page-repair-error" hidden></div></div></div><footer class="growth-modal-foot"><button type="button" data-modal-close>取消</button><button type="button" class="growth-primary" id="growthSaveOrderPage">保存并继续当前订单</button></footer></section>`);
    document.getElementById('growthSaveOrderPage')?.addEventListener('click',async event=>{const button=event.currentTarget,select=document.getElementById('growthRepairPageSelect'),error=document.getElementById('growthRepairPageError'),pageId=String(select?.value||'');try{button.disabled=true;button.textContent='正在验证主页并继续…';const body={page_id:pageId,confirmation:'SAVE_PAGE_AND_CONTINUE_ORDER'},result=await api(`/api/ops/ad-data-dashboard/meta-plans/${encodeURIComponent(planId)}/repair-page`,{method:'POST',headers:postHeaders('existing-order-page-repair',{plan_id:planId,...body}),body:JSON.stringify(body)}),resumedPlanId=String(result.resumed_plan_id||planId);launchState.batchPlan={...launchState.batchPlan,plan_id:resumedPlanId,status:'QUEUED',current_page_id:pageId,page_repair_available:false};launchState.target={...launchState.target,page_id:pageId,page_name:String(pages.find(item=>String(item.page_id||'')===pageId)?.name||'')};closeModal();if(launchId)await refreshLaunchDeliveryOrder(launchId);await openLaunchBatchWorkflow(resumedPlanId);}catch(requestError){button.disabled=false;button.textContent='保存并继续当前订单';error.hidden=false;error.textContent=readableError(requestError);}});
  }

  function openRejectedCreativeReplacement(experiment) {
    const metaReview=experiment.workflow?.meta_review||{},planId=String(metaReview.replacement_plan_id||''),imageId=String(metaReview.replacement_image_id||''),adId=String(experiment.ad_id||'');
    if(!planId||!imageId){showLaunchToast('AI 正在根据拒审原因生成并审核替代素材，完成后会自动出现处理入口。');return;}
    const imageUrl=`/api/ops/ad-data-dashboard/creative-images/${encodeURIComponent(imageId)}`;
    showModal(`<section class="growth-modal growth-modal-compact" role="dialog" aria-modal="true" aria-labelledby="growthReplacementTitle"><header class="growth-modal-head"><div><b id="growthReplacementTitle">更换素材并重新送审</b><small>${esc(experiment.experiment_code||'广告被拒')}</small></div><button type="button" class="growth-icon-button" data-modal-close aria-label="关闭">×</button></header><div class="growth-modal-body"><div class="growth-material-preview-stage"><img src="${esc(imageUrl)}" alt="AI 生成的合规替代素材"></div><div class="growth-review-card"><b>确认后系统自动完成</b><span>上传这张新素材 → 创建新 Creative → 换绑原广告 → 回读 Meta 审核状态。</span></div><div class="growth-safety"><b>原广告 ID 保持不变</b> ${esc(adId||'待回读')}。不会新建重复广告，也不会改动同订单其他广告；审核通过后按原开启状态继续投放。</div><div id="growthReplacementStatus" class="growth-notice" hidden></div></div><footer class="growth-modal-foot"><button type="button" data-modal-close>返回</button><button type="button" class="growth-primary" id="growthConfirmReplacement">使用新素材重新送审</button></footer></section>`);
    document.getElementById('growthConfirmReplacement')?.addEventListener('click',event=>submitRejectedCreativeReplacement(experiment,planId,event.currentTarget));
  }

  async function submitRejectedCreativeReplacement(experiment,planId,button) {
    const status=document.getElementById('growthReplacementStatus'),launchId=String((launchState.launch||{}).launch_id||'');
    try{
      if(guardLaunchMetaAction())return;
      button.disabled=true;status.hidden=false;status.textContent='正在核对替代素材与原广告…';
      const approvalBody={confirmation:'APPROVE_EXACT_PLAN'};
      await api(`/api/ops/ad-data-dashboard/meta-plans/${encodeURIComponent(planId)}/approve`,{method:'POST',headers:postHeaders('rejected-creative-approve',{plan_id:planId,...approvalBody}),body:JSON.stringify(approvalBody)});
      status.textContent='安全检查中，不会重复创建广告…';
      const dryBody={execution_mode:'dry_run'};
      await api(`/api/ops/ad-data-dashboard/meta-plans/${encodeURIComponent(planId)}/execute`,{method:'POST',headers:postHeaders('rejected-creative-dry-run',{plan_id:planId,...dryBody}),body:JSON.stringify(dryBody)});
      status.textContent='正在换绑原广告并提交 Meta 重新审核…';
      const liveBody={execution_mode:'live',confirmation:'APPLY_APPROVED_CHANGE'};
      await api(`/api/ops/ad-data-dashboard/meta-plans/${encodeURIComponent(planId)}/execute`,{method:'POST',headers:postHeaders('rejected-creative-live',{plan_id:planId,...liveBody}),body:JSON.stringify(liveBody)});
      closeModal();await refreshLaunchDeliveryOrder(launchId);showLaunchToast('替代素材已提交；系统会自动回读重新审核状态');
    }catch(error){
      const message=readableError(error),waiting=message.includes('真实创建通道尚未开启');
      status.hidden=false;
      if(waiting){
        button.disabled=true;button.textContent='已确认，等待系统执行';
        status.innerHTML='<b>确认和安全演练均已保存</b><br>系统会在该账户的受控执行通道恢复后，沿原方案自动继续；无需再次点击。';
      }else{
        button.disabled=false;status.textContent=message;
      }
    }
  }

  function renderLaunchDeliveryControls() {
    const variants=((launchState.launch||{}).variants||[]),experiments=launchState.experiments||[];
    const liveStatus=launchState.deliveryStatus?.data||null,livePaths=new Map((liveStatus?.paths||[]).map(item=>[String(item.experiment_id||''),item]));
    const campaignPaused=String(liveStatus?.overall_state||'')==='CAMPAIGN_PAUSED',liveChecked=launchDeliveryCheckedLabel(liveStatus?.checked_at||'');
    const metaActionsBlocked=launchMetaRateLimitBlocked(),metaDisabled=metaActionsBlocked?' disabled aria-disabled="true"':'';
    const creationIncident=experiments.some(item=>String(item.state||'')==='CREATION_PARTIAL_FAILURE'||(String(item.state||'')==='DATA_INCOMPLETE'&&String((item.workflow||{}).plan_action_type||'')==='CREATE_PAUSED_AD'));
    const activationWorkflows=experiments.map(item=>item.workflow||{}).filter(item=>String(item.plan_action_type||'')==='REACTIVATE_AD');
    const activationChecking=activationWorkflows.some(item=>Boolean(item.activation_readback_pending)||['QUEUED','RUNNING','VERIFYING'].includes(String(item.execution_status||'').toUpperCase())||Boolean(item.failure?.auto_reconcilable));
    const activationIncident=!activationChecking&&experiments.some(item=>String(item.state||'')==='DATA_INCOMPLETE'&&String((item.workflow||{}).plan_action_type||'')==='REACTIVATE_AD');
    const runningStates=new Set(['RUNNING','MATURING','RECOMMENDATION_READY','EVALUATING_ADJUSTMENT','EFFECTIVE','INEFFECTIVE','INCONCLUSIVE']);
    const pendingStates=new Set(['WAITING_ADJUSTMENT_APPROVAL','ADJUSTING']);
    const rejectedExperiments=experiments.filter(item=>String((item.workflow?.meta_review||{}).effective_status||'').toUpperCase()==='DISAPPROVED');
    const canEnableOrder=experiments.length>=2&&experiments.every(item=>Boolean(item.campaign_id&&item.adset_id&&item.ad_id)&&['META_REVIEW_PENDING','READY_FOR_ACTIVATION','PAUSED'].includes(String(item.state||'')));
    const receipts=activationWorkflows.flatMap(item=>Array.isArray(item.receipts)?item.receipts:[]),latestReceiptByStep=new Map();
    receipts.forEach(receipt=>{const step=String(receipt.step_name||'').toUpperCase();if(!step.startsWith('RECONCILE')&&step!=='VERIFY'&&step!=='RECEIPT')latestReceiptByStep.set(step,receipt);});
    const expectedSteps=['CAMPAIGN_STATUS_UPDATE',...experiments.flatMap((_,index)=>[`C${index+1}_ADSET_STATUS_UPDATE`,`C${index+1}_AD_STATUS_UPDATE`])];
    const confirmedSteps=expectedSteps.filter(step=>String(latestReceiptByStep.get(step)?.step_status||'').toUpperCase()==='VERIFIED').length,uncertainSteps=expectedSteps.filter(step=>String(latestReceiptByStep.get(step)?.step_status||'').toUpperCase()==='UNKNOWN').length,untouchedSteps=Math.max(0,expectedSteps.length-confirmedSteps-uncertainSteps);
    const finalVerification=[...receipts].reverse().find(receipt=>['VERIFY','RECONCILE'].includes(String(receipt.step_name||'').toUpperCase())&&String(receipt.step_status||'').toUpperCase()==='VERIFIED'),effectiveStatuses=(finalVerification?.verification_result_json||finalVerification?.verification_json||{}).effective_object_statuses||{};
    const performance=launchState.order?.delivery_performance||{},metricMoney=value=>value==null?'-':`$${Number(value).toFixed(2)}`,metricNumber=value=>value==null?'-':Number(value).toLocaleString('zh-CN',{maximumFractionDigits:0}),metricPercent=value=>value==null?'-':`${(Number(value)*100).toFixed(2)}%`;
    const rows=experiments.map((experiment,index)=>{
      const variant=variants.find(item=>String(item.experiment_id||'')===String(experiment.experiment_id||''));
      if(!variant)return '';
      const stateName=String(experiment.state||''),localRunning=runningStates.has(stateName),pending=pendingStates.has(stateName),creationBlocked=stateName==='CREATION_PARTIAL_FAILURE'||(stateName==='DATA_INCOMPLETE'&&String((experiment.workflow||{}).plan_action_type||'')==='CREATE_PAUSED_AD');
      const metaReview=experiment.workflow?.meta_review||{},metaRejected=String(metaReview.effective_status||'').toUpperCase()==='DISAPPROVED';
      const replacementReady=metaRejected&&String(metaReview.remediation_status||'').toUpperCase()==='PLAN_READY'&&Boolean(metaReview.replacement_plan_id&&metaReview.replacement_image_id);
      const livePath=livePaths.get(String(experiment.experiment_id||''))||null,liveState=String(livePath?.delivery_state||''),running=!metaRejected&&(livePath?['ACTIVE','REVIEW_PENDING'].includes(liveState):localRunning);
      const idsReady=Boolean(experiment.campaign_id&&experiment.adset_id&&experiment.ad_id);
      const label=variant.meta_names?.ad||variant.creative_angle||experiment.experiment_code||`广告 ${index+1}`;
      const action=metaRejected||campaignPaused?'':(running?'关闭':(livePath&&idsReady?'开启':''));
      const adStep=latestReceiptByStep.get(`C${index+1}_AD_STATUS_UPDATE`),adStepStatus=String(adStep?.step_status||'').toUpperCase(),effectiveStatus=String(effectiveStatuses[`c${index+1}_ad_id`]||'').toUpperCase(),reviewPending=['PENDING_REVIEW','IN_PROCESS','WITH_ISSUES'].includes(effectiveStatus);
      const liveLabel=liveState==='CAMPAIGN_PAUSED'?'随广告系列关闭':(liveState==='ADSET_PAUSED'?'广告组已关闭':(liveState==='AD_PAUSED'?'已关闭':(liveState==='REVIEW_PENDING'?'审核中（已开启）':(liveState==='ACTIVE'?'已开启':''))));
      const status=metaRejected?'广告被拒':(liveLabel||((launchState.deliveryStatus?.loading&&!liveStatus)?'状态同步中':(activationChecking?(adStepStatus==='VERIFIED'?'已开启':(adStepStatus==='UNKNOWN'?'状态核对中':'等待开启')):(running?(reviewPending?'审核中（已开启）':'已开启'):(creationBlocked?'等待整单处理':(stateName==='PAUSED'?'已暂停':(pending?'等待确认':(idsReady?'已暂停':'创建中'))))))));
      const statusClass=metaRejected?'is-rejected':(running||adStepStatus==='VERIFIED'?'is-running':(pending||activationChecking?'is-pending':''));
      const adPerformance=experiment.performance||{},isolation=experiment.creative_performance_isolation||{},currentMaterial=isolation.current_material?.performance||{},displayPerformance=isolation.has_replacement?currentMaterial:adPerformance,metricPrefix=isolation.has_replacement?'当前素材表现 · ':'';
      const rowMetrics=displayPerformance.available?`${metricPrefix}消耗 ${metricMoney(displayPerformance.spend)} · 安装 ${metricNumber(displayPerformance.installs)} · CPI ${metricMoney(displayPerformance.cpi)} · CTR ${metricPercent(displayPerformance.ctr)} · 入会 ${metricNumber(displayPerformance.real_bind_count)}`:(isolation.has_replacement?'当前素材表现 · 等待首个完整统计日':'暂未回流广告级数据');
      const rejectedAction=replacementReady?`<button type="button" class="growth-launch-primary" data-launch-replacement="${esc(experiment.experiment_id)}">更换素材并重新送审</button>`:(metaRejected?'<span class="growth-launch-delivery-hint">AI 正在准备替代素材</span>':'');
      return `<div class="growth-launch-delivery-row"><div class="growth-launch-delivery-copy"><b>${esc(label)}</b><span>${esc(experiment.experiment_code||`广告 ${index+1}`)}</span><small class="growth-launch-row-metrics">${esc(rowMetrics)}</small></div><span class="growth-launch-delivery-status ${statusClass}">${status}</span>${rejectedAction}${action?`<button type="button" class="${running?'growth-launch-secondary':'growth-launch-primary'}" data-launch-delivery-experiment="${esc(experiment.experiment_id)}" data-launch-delivery-action="${running?'pause':'enable'}"${metaDisabled}>${action}</button>`:''}</div>`;
    }).join('');
    const isolatedExperiments=experiments.filter(item=>Boolean(item.creative_performance_isolation?.has_replacement));
    const revisionSummary=isolatedExperiments.length?`<section class="growth-launch-revisions"><header><b>素材版本统计</b><span>替换当天不参与素材优胜判断；广告累计数据仍完整保留</span></header><div class="growth-launch-revision-grid">${isolatedExperiments.map((experiment,index)=>{const isolation=experiment.creative_performance_isolation||{},current=isolation.current_material||{},metrics=current.performance||{},history=isolation.historical_materials||[],mixed=(isolation.mixed_dates||[]).join('、')||'无';return `<article class="growth-launch-revision-card"><div><b>当前素材表现 · ${esc(experiment.experiment_code||`广告 ${index+1}`)}</b><span>从 ${esc(String(current.effective_from||'').slice(0,10)||'-')} 重新累计 · 混合日 ${esc(mixed)}</span></div><strong>${metrics.available?`${metricMoney(metrics.spend)} · ${metricNumber(metrics.installs)} 安装 · CPI ${metricMoney(metrics.cpi)}`:'等待完整统计日'}</strong></article>${history.length?`<details class="growth-launch-revision-history"><summary>查看历史素材表现（${history.length}）</summary>${history.map(item=>{const old=item.performance||{};return `<p>${esc(String(item.effective_from||'').slice(0,10)||'-')} 至 ${esc(String(item.effective_to||'').slice(0,10)||'-')} · ${metricMoney(old.spend)} · ${metricNumber(old.installs)} 安装 · CPI ${metricMoney(old.cpi)}</p>`;}).join('')}</details>`:''}`;}).join('')}</div></section>`:'';
    const campaignId=String(experiments.find(item=>item.campaign_id)?.campaign_id||'待回读');
    const runningCount=liveStatus?Math.max(0,Number(liveStatus.configured_active_count||0)-rejectedExperiments.length):experiments.filter(item=>runningStates.has(String(item.state||''))&&!rejectedExperiments.includes(item)).length;
    const headerTitle=campaignPaused?'广告系列已关闭':(creationIncident?'创建结果':(activationChecking?'开启状态核对中':(activationIncident?'开启需要处理':(rejectedExperiments.length?`${rejectedExperiments.length} 条广告被拒`:(runningCount?`已开启 · ${runningCount}/${experiments.length}`:`已创建 · ${experiments.length} 组暂停`)))));
    const headerNote=rejectedExperiments.length?`其余 ${Math.max(0,experiments.length-rejectedExperiments.length)} 条保持当前状态；AI 已为拒审广告准备替代素材。`:(campaignPaused?`Meta 实时状态${liveChecked?` · 更新于 ${liveChecked}`:''}`:(creationIncident?'整单异常只需处理一次；系统不会重放旧任务。':(activationChecking?`${confirmedSteps} 项已确认 · ${uncertainSteps} 项待核对 · ${untouchedSteps} 项尚未执行`:(activationIncident?'系统已停止推进，等待明确的失败原因。':(runningCount?`审核中的广告将在审核通过后自动投放${liveChecked?` · 更新于 ${liveChecked}`:''}`:'开启后若仍在审核，审核通过将自动投放')))));
    const primaryAction=creationIncident&&launchState.batchPlan?.plan_id?`<button type="button" class="growth-launch-primary" data-launch-order-repair${metaDisabled}>处理创建异常</button>`:(activationIncident?'<button type="button" class="growth-launch-primary" data-launch-activation-exception>查看原因和下一步</button>':((campaignPaused||canEnableOrder)?`<button type="button" class="growth-launch-primary" data-launch-enable-order${metaDisabled}>开启整单投放</button>`:''));
    const performanceAction=`<button type="button" class="growth-launch-secondary" data-launch-refresh-performance${launchState.performanceSync?.loading?' disabled':''}>${launchState.performanceSync?.loading?'读取中…':'同步效果数据'}</button>`;
    const refreshAction=`<button type="button" class="growth-launch-secondary" data-launch-refresh-delivery${launchState.deliveryStatus?.loading?' disabled':''}>${launchState.deliveryStatus?.loading?'刷新中…':'刷新投放状态'}</button>`;
    const headerAction=`<div class="growth-launch-delivery-actions">${performanceAction}${refreshAction}${primaryAction}</div>`;
    const cpiAlert=performance.cpi_status==='ABOVE_TARGET',analysisBadge=!performance.available?'数据待回流':(performance.sample_status==='MATURE'?'样本已成熟':'样本积累中'),analysisClass=!performance.available?'is-empty':(performance.sample_status==='MATURE'?'is-ready':'');
    const analysis=performance.available?`<section class="growth-launch-analysis"><header class="growth-launch-analysis-head"><div><b>投放分析</b><span>T+1 日级数据 · 统计截止 ${esc(performance.statistics_cutoff_date||performance.latest_data_date||'-')} · 数据更新时间 ${esc(launchPerformanceUpdatedLabel(performance.data_updated_at))} · ${esc(performance.granularity_note||(performance.attribution_levels||[]).join(' / ')||'广告级')}</span></div><span class="growth-launch-analysis-badge ${analysisClass}">${analysisBadge}</span></header><div class="growth-launch-metrics"><div class="growth-launch-metric"><small>消耗</small><strong>${metricMoney(performance.spend)}</strong><em>${metricNumber(performance.impressions)} 展示</em></div><div class="growth-launch-metric"><small>安装</small><strong>${metricNumber(performance.installs)}</strong><em>目标样本 ${metricNumber(performance.minimum_installs)}</em></div><div class="growth-launch-metric ${cpiAlert?'is-alert':''}"><small>CPI</small><strong>${metricMoney(performance.cpi)}</strong><em>目标 ≤ ${metricMoney(performance.cpi_target)}</em></div><div class="growth-launch-metric"><small>CTR</small><strong>${metricPercent(performance.ctr)}</strong><em>${metricNumber(performance.clicks)} 次点击</em></div><div class="growth-launch-metric"><small>真实入会 / CPA</small><strong>${metricNumber(performance.real_bind_count)} / ${metricMoney(performance.real_bind_cpa)}</strong><em>目标样本 ${metricNumber(performance.minimum_real_joins)}</em></div></div><div class="growth-launch-analysis-note"><div><b>${esc(performance.conclusion_zh||'系统正在分析')}</b><span>${cpiAlert?'当前 CPI 高于目标，但小样本阶段不会据此自动判负。':'系统结合 CPI、CTR、安装和真实入会判断。'}</span></div><div><b>下一步</b><span>${esc(performance.next_step_zh||'继续采集数据。')}</span></div></div></section>`:`<section class="growth-launch-analysis"><header class="growth-launch-analysis-head"><div><b>投放分析</b><span>T+1 日级数据 · 系统按广告 ID 汇总 Meta 与 AppsFlyer / 真实入会数据</span></div><span class="growth-launch-analysis-badge is-empty">数据待回流</span></header><div class="growth-launch-no-data">首轮事实通常在次日数据同步后出现；若数据源已同步，系统会自动补齐订单映射，不需要反复刷新。</div></section>`;
    const readbackNotice=launchState.deliveryStatus?.error?`<div class="growth-launch-delivery-note">Meta 状态刷新失败；当前保留上次成功结果，系统会自动重试。</div>`:'';
    return `${analysis}${revisionSummary}<section class="growth-launch-delivery"><header><div><b>${headerTitle}</b><span>${headerNote}</span></div>${headerAction}</header>${launchMetaRateLimitNotice()}${readbackNotice}<div class="growth-launch-delivery-list">${rows||'<div class="growth-launch-delivery-note">系统正在自动读取广告对象。</div>'}</div><details class="growth-launch-technical"><summary>技术信息</summary><p>Campaign ${esc(campaignId)}<br>${experiments.map(item=>`Ad Set ${esc(item.adset_id||'未创建')} · Ad ${esc(item.ad_id||'未创建')}`).join('<br>')}</p></details></section>`;
  }

  function renderAudienceLaunchReady() {
    const variants=launchState.launch?.variants||[],target=launchState.launch?.target||{},frozen=target.frozen_creative||{},copyMode=String(target.test_variable||target.experiment_mode||launchState.experimentMode)==='copy_variant',labels=variants.map(item=>copyMode?(item.role==='BASELINE'?'已验证表达':'流程透明表达'):(item.audience_strategy?.label||item.audience_strategy?.strategy_key||'-'));
    const country=String(target.country||launchState.target.country||'-').toUpperCase(),experimentLabel=copyMode?'文案实验':'受众实验',constraint=copyMode?'两组共用同一素材、受众和预算，只改变文案。':`两组素材和文案必须完全相同。`;
    return `<header class="growth-launch-head growth-launch-cold-head"><div><h1>${esc(country)} · Tugao ${experimentLabel}</h1><span class="growth-launch-target">${copyMode?'同图同受众':'相同素材'} · ${esc(labels.join(' vs '))}</span></div><button type="button" class="growth-launch-secondary" data-launch-reset>修改目标</button></header><section class="growth-launch-cold"><header class="growth-launch-cold-status"><span class="growth-launch-kicker">下一步</span><h2>${experimentLabel}已保存，等待确认整单参数</h2><p>素材 ${esc(frozen.image_id||launchState.frozenCreativeId)} 已冻结；${constraint}</p></header><div class="growth-launch-plan"><article><b>实验结构</b><strong>1 Campaign · 2 Ad Sets · 2 Ads</strong><span>基准：${esc(labels[0]||'-')}</span><small>挑战：${esc(labels[1]||'-')}</small></article><article><b>投放护栏</b><strong>COST_CAP · $${esc(Number(launchState.target.cpi||0).toFixed(2))}</strong><span>每组 $${esc(String(variants[0]?.initial_daily_budget||20))}/天 · 创建后暂停</span><small>止损线会自动判定，暂停仍需确认</small></article></div><footer class="growth-launch-cold-actions"><button type="button" class="growth-launch-primary" data-launch-material>确认${experimentLabel}广告参数</button></footer></section>`;
  }

  const AD_COPY_MARKET_RULES=Object.freeze({
    BR:{locale:'pt-BR',tone:'direto, leve e confiável',app_term:'app',emoji:{points_reward:'📱',safe_compliance:'✅',easy_start:'🚀',guided_trust:'🤝'},avoid:['renda garantida','dinheiro fácil','fique rico','saque imediato garantido']},
    MX:{locale:'es-MX',tone:'claro, cercano y sin promesas exageradas',app_term:'app',emoji:{points_reward:'📱',safe_compliance:'✅',easy_start:'🚀',guided_trust:'🤝'},avoid:['ingreso garantizado','dinero fácil','hazte rico','retiro garantizado']},
    ID:{locale:'id-ID',tone:'ringkas, ramah, dan terpercaya',app_term:'aplikasi',emoji:{points_reward:'📱',safe_compliance:'✅',easy_start:'🚀',guided_trust:'🤝'},avoid:['penghasilan terjamin','uang mudah','cepat kaya','pasti cair']},
  });
  const AD_COPY_VARIANTS=Object.freeze({
    BR:{points_reward:['📱 Seu tempo livre pode render mais: encontre tarefas no Tugao, acompanhe o progresso e veja os pontos e recompensas disponíveis no app.','Transforme tempo livre em progresso','Veja tarefas e recompensas no Tugao.'],safe_compliance:['✅ Antes de começar, veja no Tugao o que a tarefa pede, quais são as etapas e como os pontos e recompensas funcionam.','Saiba o que fazer desde o início','Regras e etapas claras no app.'],easy_start:['🚀 Tem alguns minutos livres? Abra o Tugao, escolha uma tarefa e acompanhe cada etapa, seus pontos e as recompensas disponíveis.','Comece uma tarefa pelo celular','Tudo organizado no Tugao.'],guided_trust:['🤝 Não sabe por onde começar? O Tugao mostra a próxima etapa, seu progresso e os pontos acumulados para você seguir sem se perder.','Seu próximo passo está no Tugao','Acompanhe tudo pelo app.']},
    MX:{points_reward:['📱 Encuentra encuestas, apps para probar y tareas sencillas en Tugao. Completa actividades, acumula puntos y sigue tu avance. Instala la app y elige tu primera tarea.','Encuentra tareas y acumula puntos','Empieza hoy desde tu celular.'],safe_compliance:['✅ Antes de empezar, revisa en Tugao qué pide cada tarea, cuáles son los pasos y cómo funcionan los puntos y las recompensas.','Sabe qué hacer desde el inicio','Reglas y pasos claros en la app.'],easy_start:['✅ En Tugao ves qué tarea harás, cuántos pasos tiene y qué puntos puedes obtener antes de empezar. Instala la app, elige una tarea y sigue tu avance paso a paso.','Tareas claras, paso a paso','Revisa los pasos antes de comenzar.'],guided_trust:['🤝 ¿No sabes por dónde empezar? Tugao te muestra el siguiente paso, tu avance y los puntos acumulados para que sigas sin perderte.','Tu siguiente paso está en Tugao','Sigue todo desde la app.']},
    ID:{points_reward:['📱 Manfaatkan waktu luangmu: temukan tugas di Tugao, pantau progres, lalu lihat poin dan hadiah yang tersedia langsung di aplikasi.','Waktu luang jadi lebih berarti','Lihat tugas dan hadiah di Tugao.'],safe_compliance:['✅ Sebelum mulai, lihat apa yang perlu dikerjakan, tahapnya, serta cara kerja poin dan hadiah di aplikasi Tugao.','Tahu tugasnya sejak awal','Langkah dan ketentuan lebih jelas.'],easy_start:['🚀 Punya beberapa menit? Buka Tugao, pilih tugas, lalu pantau setiap tahap, poin, dan hadiah yang tersedia.','Mulai tugas langsung dari HP','Semua tersusun di Tugao.'],guided_trust:['🤝 Bingung mulai dari mana? Tugao menunjukkan langkah berikutnya, progres, dan poin yang sudah terkumpul agar kamu tetap terarah.','Langkah berikutnya ada di Tugao','Pantau semuanya di aplikasi.']},
  });

  function batchAdCopy(country,direction) {
    const market=Object.prototype.hasOwnProperty.call(AD_COPY_MARKET_RULES,String(country||'').toUpperCase())?String(country).toUpperCase():'BR';
    const key=String(direction?.key||direction?.direction_id||'points_reward');
    return AD_COPY_VARIANTS[market]?.[key]||AD_COPY_VARIANTS[market].points_reward;
  }

  function openLaunchBatchPlan() {
    const launch=launchState.launch||{},variants=Array.isArray(launch.variants)?launch.variants:[];
    const audienceMode=String(launch.target?.test_variable||launch.target?.experiment_mode||launchState.experimentMode)==='audience_strategy';
    const copyMode=String(launch.target?.test_variable||launch.target?.experiment_mode||launchState.experimentMode)==='copy_variant';
    if(launchState.batchPlan?.plan_id){openLaunchBatchWorkflow(String(launchState.batchPlan.plan_id));return;}
    if(variants.length<2||variants.length>4){showModal(`<section class="growth-modal growth-modal-compact"><header class="growth-modal-head"><b>素材方向数量不正确</b><button type="button" class="growth-icon-button" data-modal-close>×</button></header><div class="growth-modal-body"><p>首批实验需要选择 2–4 个素材方向；每个方向对应 1 个广告组和 1 条广告。</p></div><footer class="growth-modal-foot"><button type="button" data-modal-close>返回</button></footer></section>`);return;}
    const country=String(launchState.target.country||'BR').toUpperCase(),language={BR:'葡萄牙语（巴西）',MX:'西班牙语（墨西哥）',ID:'印度尼西亚语'}[country]||'-';
    const campaign=String(variants[0]?.meta_names?.campaign||`TG_${country}_INS_CS`);
    const budget=Number(variants[0]?.initial_daily_budget||20);
    const variableLabel=audienceMode?'受众策略':(copyMode?'广告文案':'素材方向');
    const batchSubtitle=`${esc(country)} · ${variants.length} 组 · ${esc(variableLabel)}实验 · 每组 $${esc(String(budget))}/天`;
    const frozenDirection={key:launch.target?.frozen_creative?.creative_direction||'points_reward'},sharedCopy=batchAdCopy(country,frozenDirection);
    if(copyMode&&variants.some(variant=>!String(variant.copy_variant?.primary_text||'').trim()||!String(variant.copy_variant?.headline||'').trim())){
      showModal(`<section class="growth-modal growth-modal-compact"><header class="growth-modal-head"><b>文案没有完整载入</b><button type="button" class="growth-icon-button" data-modal-close aria-label="关闭">×</button></header><div class="growth-modal-body"><p>这两组广告的正文或标题没有读全。系统已停止生成方案，不会创建空文案广告。</p><div class="growth-next-card"><b>下一步</b><span>关闭弹窗并刷新订单；若仍未恢复，请在异常任务中查看具体缺失字段。</span></div></div><footer class="growth-modal-foot"><button type="button" data-modal-close>返回订单</button></footer></section>`);
      return;
    }
    const cells=variants.map((variant,index)=>{const direction=audienceMode?frozenDirection:(variant.creative_direction||{}),storedCopy=copyMode?(variant.copy_variant||{}):{},copy=copyMode?[storedCopy.primary_text||'',storedCopy.headline||'',storedCopy.description||'']:(audienceMode?sharedCopy:batchAdCopy(country,direction)),strategy=variant.audience_strategy||{strategy_key:'BROAD',label:'广泛受众',meta_targeting_ids:[]},isBaseline=(audienceMode||copyMode)?String(variant.role)==='BASELINE':index===0,title=audienceMode?(strategy.label||strategy.strategy_key):(copyMode?(isBaseline?'已验证表达':'流程透明表达'):(direction.title||variant.creative_angle||`素材方向 ${index+1}`)),purpose=audienceMode?((strategy.meta_targeting_ids||[]).length?`${strategy.meta_targeting_ids.length} 个已验证 Meta 定位 ID`:'无细分定位'):(copyMode?String(storedCopy.hypothesis||'验证同图同受众下的文案差异。'):(direction.summary||direction.hypothesis||variant.hypothesis||'验证该素材方向在相同投放条件下的表现。'));return `<article class="growth-batch-experiment ${isBaseline?'is-baseline':''}" data-batch-cell="${index}" data-copy-benchmark-version="${esc(storedCopy.benchmark_version||'')}" data-copy-hypothesis="${esc(storedCopy.hypothesis||'')}"><section class="growth-batch-direction"><label><input type="radio" name="growthBatchBaseline" value="${index}" aria-label="将${esc(title)}设为基准组" ${isBaseline?'checked':''} ${(audienceMode||copyMode)?'disabled':''}><span><b>${esc(title)}</b><em data-batch-role-text>${isBaseline?'基准组':'挑战组'}</em></span></label><p>${esc(purpose)}</p></section><section class="growth-batch-group-fields"><label>每日预算（美元）<input data-batch-budget type="number" min="5" max="100" step="1" value="${esc(String(budget))}"></label><label>广告组名称<input data-batch-adset maxlength="80" value="${esc(variant.meta_names?.adset||`${country}_BD_C${index+1}`)}"></label></section><section class="growth-batch-ad-fields"><label>广告名称<input data-batch-ad maxlength="80" value="${esc(variant.meta_names?.ad||variant.experiment_code||`C${index+1}_ST_H1_V1`)}"></label><label>标题<input data-batch-headline maxlength="80" value="${esc(copy[1])}" ${audienceMode?'readonly':''}></label><label class="wide">主要文案<textarea data-batch-primary maxlength="500" ${audienceMode?'readonly':''}>${esc(copy[0])}</textarea></label><label class="wide">描述<input data-batch-description maxlength="120" value="${esc(copy[2])}" ${audienceMode?'readonly':''}></label></section></article>`;}).join('');
    const cpiCap=Number(launchState.target.cpi||0);
    const safetySummary=`<section class="growth-safety"><b>投放护栏</b><span>竞价：COST_CAP · CPI 上限 $${esc(cpiCap.toFixed(2))} · 每组 $${esc(String(budget))}/天 · 创建后暂停</span><small>止损：800 展示且 CTR &lt; 1.2%；D1 后消耗 ≥ $1.20 且 0 安装；安装 ≥ 10 且 CPI &gt; $0.55。触发后生成暂停待确认，不自动重复写入。</small></section>`;
    showModal(`<section class="growth-modal growth-batch-modal growth-batch-review-modal"><header class="growth-modal-head"><div><b>确认 ${variants.length} 组广告</b><small>${batchSubtitle}</small></div><button type="button" class="growth-icon-button" data-modal-close aria-label="关闭">×</button></header><div class="growth-modal-body"><div class="growth-batch-review-summary"><section><label for="growthBatchCampaign">广告系列名称</label><input id="growthBatchCampaign" maxlength="80" value="${esc(campaign)}"></section><section class="growth-batch-review-meta" aria-label="本轮固定条件"><span>${esc(country)} · 女性 18–40 岁</span><span>${esc(language)}</span><span>受众扩展：关</span><span>创建后暂停</span></section></div>${safetySummary}<div class="growth-batch-review-list">${cells}</div></div><footer class="growth-modal-foot"><div class="growth-batch-foot-actions"><button type="button" data-modal-close>返回修改</button><button type="button" class="growth-primary" id="growthConfirmBatchPlan">确认并生成方案</button></div></footer></section>`);
    document.querySelectorAll('input[name="growthBatchBaseline"]').forEach(radio=>radio.addEventListener('change',()=>{document.querySelectorAll('[data-batch-cell]').forEach((cell,index)=>{const selected=index===Number(radio.value);cell.classList.toggle('is-baseline',selected);const role=cell.querySelector('[data-batch-role-text]');if(role)role.textContent=selected?'基准组':'挑战组';});}));
    document.getElementById('growthConfirmBatchPlan')?.addEventListener('click',event=>submitLaunchBatchPlan(event.currentTarget));
  }

  async function submitLaunchBatchPlan(button) {
    const launch=launchState.launch||{},variants=launch.variants||[],baseline=Number(document.querySelector('input[name="growthBatchBaseline"]:checked')?.value??-1),campaignName=String(document.getElementById('growthBatchCampaign')?.value||'').trim();
    const audienceMode=String(launch.target?.test_variable||launch.target?.experiment_mode||launchState.experimentMode)==='audience_strategy';
    const copyMode=String(launch.target?.test_variable||launch.target?.experiment_mode||launchState.experimentMode)==='copy_variant';
    const randomizedMode=audienceMode||copyMode;
    const nodes=[...document.querySelectorAll('[data-batch-cell]')];
    const budgets=nodes.map(node=>Number(node.querySelector('[data-batch-budget]')?.value||0));
    if(baseline<0||!campaignName||nodes.length!==variants.length||nodes.length<2||nodes.length>4||budgets.some(value=>!(value>=5&&value<=100))||new Set(budgets).size!==1){showModalError('请选择一个基准组，并确认所有广告组使用相同的 5–100 美元日预算。');return;}
    const cells=nodes.map((node,index)=>({experiment_id:String(variants[index]?.experiment_id||''),role:index===baseline?'BASELINE':'CHALLENGER',audience_strategy:String(variants[index]?.audience_strategy?.strategy_key||'BROAD'),adset_name:String(node.querySelector('[data-batch-adset]')?.value||'').trim(),daily_budget_usd:budgets[index],ad_name:String(node.querySelector('[data-batch-ad]')?.value||'').trim(),primary_text:String(node.querySelector('[data-batch-primary]')?.value||'').trim(),headline:String(node.querySelector('[data-batch-headline]')?.value||'').trim(),description:String(node.querySelector('[data-batch-description]')?.value||'').trim(),call_to_action:'INSTALL_MOBILE_APP',copy_benchmark_version:String(node.dataset.copyBenchmarkVersion||''),copy_hypothesis:String(node.dataset.copyHypothesis||'')}));
    if(cells.some(cell=>!cell.experiment_id||!cell.adset_name||!cell.ad_name||!cell.primary_text||!cell.headline)){showModalError('请补齐每个广告组的名称、广告名称、主要文案和标题。');return;}
    const launchId=String(launch.launch_id||''),body={campaign_name:campaignName,audience_strategy:'BROAD',test_variable:audienceMode?'audience_strategy':(copyMode?'copy_variant':'creative_direction'),frozen_creative_id:randomizedMode?String(launch.target?.frozen_creative?.image_id||launchState.frozenCreativeId||''):'',cells,evaluation_window:{checkpoints:['D1','D3','D7']}};
    try{
      button.disabled=true;
      if(randomizedMode){
        button.textContent=copyMode?'正在核验文案随机实验资格…':'正在核验受众规模与随机实验资格…';
        const preflight=await api(`/api/ops/ad-data-dashboard/new-account-launches/${encodeURIComponent(launchId)}/audience-preflight`,{method:'POST',headers:postHeaders('new-account-audience-preflight',{launch_id:launchId}),body:'{}'});
        body.audience_preflight_id=String(preflight.preflight_id||'');
        if(!body.audience_preflight_id)throw new Error('实时受众预检没有生成有效凭证。');
        launchState.audiencePreflight=preflight;
      }
      button.textContent='正在固化整单 Plan…';
      const result=await api(`/api/ops/ad-data-dashboard/new-account-launches/${encodeURIComponent(launchId)}/create-plan/preview`,{method:'POST',headers:postHeaders('new-account-batch-plan',{launch_id:launchId,...body}),body:JSON.stringify(body)});
      const planId=String(result.plan_id||'');
      const approvalBody={confirmation:'APPROVE_EXACT_PLAN'};
      await api(`/api/ops/ad-data-dashboard/meta-plans/${encodeURIComponent(planId)}/approve`,{method:'POST',headers:postHeaders('new-account-batch-approve',{plan_id:planId,...approvalBody}),body:JSON.stringify(approvalBody)});
      launchState.batchPlan={plan_id:planId,status:'APPROVED'};closeModal();await openLaunchBatchWorkflow(planId,result.plan||{});
    }catch(error){button.disabled=false;button.textContent='确认并生成方案';showModalError(readableError(error));}
  }

  function clearLaunchBatchWorkflowTimer() {
    if(launchState.batchWorkflowTimer){clearTimeout(launchState.batchWorkflowTimer);launchState.batchWorkflowTimer=null;}
  }

  function launchBatchSteps(plan) {
    const cells=Array.isArray(plan.cells)?plan.cells:[],mode=String(plan.test_variable||'').toLowerCase(),steps=['CAMPAIGN_CREATE'];
    if(mode==='audience_strategy'){
      steps.push('C1_IMAGE_UPLOAD','C1_CREATIVE_CREATE');
      cells.forEach((cell,index)=>{const key=String(cell.cell_key||`C${index+1}`).toUpperCase();steps.push(`${key}_ADSET_CREATE`,`${key}_AD_CREATE`);});
      steps.push('STUDY_CREATE');
    }else{
      cells.forEach((cell,index)=>{const key=String(cell.cell_key||`C${index+1}`).toUpperCase();steps.push(`${key}_IMAGE_UPLOAD`,`${key}_CREATIVE_CREATE`,`${key}_ADSET_CREATE`,`${key}_AD_CREATE`);});
      if(mode==='copy_variant')steps.push('STUDY_CREATE');
    }
    return [...steps,'VERIFY','RECEIPT'];
  }

  function launchBatchProgress(plan,task,receipts) {
    const steps=launchBatchSteps(plan),completed=new Set((receipts||[]).filter(receipt=>['SUCCESS','VERIFIED'].includes(String(receipt.step_status||'').toUpperCase())).map(receipt=>String(receipt.step_name||'').toUpperCase())),status=String(task.status||'').toUpperCase();
    if(status==='SUCCESS')steps.forEach(step=>completed.add(step));
    const current=status==='SUCCESS'?'':(status==='VERIFYING'?'VERIFY':(steps.find(step=>!completed.has(step))||String(task.current_step||'VERIFY').toUpperCase())),done=steps.filter(step=>completed.has(step)).length;
    return {steps,completed,current,done,total:steps.length,percent:Math.round(done*100/Math.max(steps.length,1))};
  }

  function launchBatchStepLabel(step,plan) {
    const cells=Array.isArray(plan.cells)?plan.cells:[],match=String(step||'').match(/^(C\d+)_(.+)$/),cell=match?cells.find((item,index)=>String(item.cell_key||`C${index+1}`).toUpperCase()===match[1]):null,cellName=cell?(cell.role==='BASELINE'?'基准广告':(cell.steps?.CREATIVE_CREATE?.headline||cell.creative_direction?.title||cell.experiment_code||match[1])):'',action=match?match[2]:String(step||'');
    const labels={CAMPAIGN_CREATE:'创建广告系列',IMAGE_UPLOAD:'上传素材',CREATIVE_CREATE:'创建广告素材',ADSET_CREATE:'创建广告组',AD_CREATE:'创建广告',STUDY_CREATE:'建立随机实验',VERIFY:'核对全部暂停状态',RECEIPT:'保存创建结果'};
    return `${cellName?`${cellName} · `:''}${labels[action]||action}`;
  }

  function launchBatchCellStatus(cell,index,progress,plan) {
    const key=String(cell.cell_key||`C${index+1}`).toUpperCase(),mode=String(plan.test_variable||'').toLowerCase(),own=mode==='audience_strategy'?[`${key}_ADSET_CREATE`,`${key}_AD_CREATE`]:[`${key}_IMAGE_UPLOAD`,`${key}_CREATIVE_CREATE`,`${key}_ADSET_CREATE`,`${key}_AD_CREATE`],done=own.filter(step=>progress.completed.has(step)).length,current=own.includes(progress.current);
    if(done===own.length)return {className:'is-done',label:'已创建并回读'};
    if(current)return {className:'is-current',label:launchBatchStepLabel(progress.current,plan).split(' · ').pop()};
    if(done)return {className:'is-current',label:`已完成 ${done}/${own.length}`};
    return {className:'',label:'等待创建'};
  }

  function scheduleLaunchBatchWorkflowRefresh(planId,knownPlan) {
    clearLaunchBatchWorkflowTimer();launchState.batchWorkflowPlanId=String(planId||'');
    launchState.batchWorkflowTimer=setTimeout(()=>{const modal=document.querySelector('[data-growth-batch-plan-id]');if(modal&&String(modal.dataset.growthBatchPlanId||'')===String(planId||'')&&modal.closest('.growth-modal-layer')&&!modal.closest('.growth-modal-layer').hidden)openLaunchBatchWorkflow(planId,knownPlan,{automatic:true});},1800);
  }

  async function openLaunchBatchWorkflow(planId,knownPlan={},options={}) {
    clearLaunchBatchWorkflowTimer();
    try{
      const payload=await api(`/api/ops/ad-data-dashboard/meta-plans/${encodeURIComponent(planId)}/receipt`),detail=payload.plan||{},plan=detail.plan||knownPlan||{},cells=plan.cells||[],dry=payload.dry_run_receipt||{},task=payload.execution_task||{},receipts=payload.receipts||[],next=String(payload.next_step||'RUN_DRY_RUN');
      if(options.automatic&&launchState.batchWorkflowPlanId!==String(planId||''))return;
      if(payload.incident_resolution&&Object.keys(payload.incident_resolution).length){await openLaunchIncidentResolution(planId,payload,plan);return;}
      const audienceMode=String(plan.test_variable||'')==='audience_strategy',copyMode=String(plan.test_variable||'')==='copy_variant',planAllowsLive=plan.execution_policy?.live_creation_allowed!==false,liveAvailable=payload.live_execution_available===true,liveAllowed=planAllowsLive&&liveAvailable;
      const dryVerified=String(dry.status||'')==='DRY_RUN_VERIFIED',taskStatus=String(task.status||''),created=taskStatus==='SUCCESS',canResume=Boolean(payload.can_resume_same_plan)&&next==='RESUME_SAME_PLAN',externalPrerequisite=Boolean(payload.external_prerequisite_required),objectIds=task.meta_object_ids_json||{},progress=launchBatchProgress(plan,task,receipts);
      const rows=cells.map((cell,index)=>{const key=String(cell.cell_key||`C${index+1}`).toLowerCase(),adId=String(objectIds[`${key}_ad_id`]||''),cellStatus=launchBatchCellStatus(cell,index,progress,plan),title=audienceMode?(cell.audience_strategy?.label||cell.audience_strategy?.strategy_key):(copyMode?(cell.steps?.CREATIVE_CREATE?.headline||`文案 ${index+1}`):(cell.creative_direction?.title||cell.experiment_code||`实验 ${index+1}`));return `<div class="${cellStatus.className}"><span>${esc(cell.role==='BASELINE'?'基':'挑')}</span><b>${esc(title)}</b><em>${esc(created?`已暂停${adId?` · Ad ${adId}`:''}`:cellStatus.label)}</em></div>`;}).join('');
      if(created){launchState.experimentStates=['META_REVIEW_PENDING'];launchState.orderPhase='META_REVIEW_PENDING';launchState.orderStatusZh='广告已创建并暂停 · 可管理投放';}
      const preservedKinds=[objectIds.campaign_id?'Campaign':'',Object.keys(objectIds).some(key=>key.endsWith('_adset_id'))?'广告组':'',Object.keys(objectIds).some(key=>key.endsWith('_creative_id'))?'广告素材':''].filter(Boolean),pageMismatch=String(task.error_message||'').includes('1815645');
      let statusText='';
      if(created)statusText=`<b>已创建并保持暂停</b><span>1 个广告系列、${cells.length} 个广告组和 ${cells.length} 条广告已回读确认，不会自动开始花费。</span><small>审核通过后才开放“开启投放”。</small>`;
      else if(pageMismatch)statusText=`<b>广告未创建，系统已停止</b><span>Meta 拒绝了当前广告账户与公共主页的组合；已创建的 ${esc(preservedKinds.join('、')||'对象')} 已自动保存，不需要你补 ID。</span><small>系统正在按该账户历史成功路径核对正确主页；不会回退素材，也不会重复创建已有对象。</small>`;
      else if(canResume)statusText='<b>创建尚未完成</b><span>已确认对象和素材都已保存；系统会先反查 Meta，再从未完成步骤继续。</span><small>不需要提供任何 ID。</small>';
      else if(taskStatus)statusText=`<b>${esc(taskStatus==='MANUAL_REVIEW'?'创建尚未完成':`正在创建广告 · 已完成 ${progress.done}/${progress.total} 个步骤`)}</b><span>${taskStatus==='MANUAL_REVIEW'?'系统已保存成功步骤并停止重复写入。':`当前：${esc(launchBatchStepLabel(progress.current,plan))}`}</span><small>${taskStatus==='MANUAL_REVIEW'?'下一步由系统核对真实对象与账户配置。':'所有对象保持暂停，不会产生广告费用；面板会自动更新。'}</small>`;
      else if(dryVerified)statusText=liveAllowed?'<b>安全检查已通过</b><span>点击“创建广告”后会整单创建并自动回读，全部保持暂停。</span><small>这次确认覆盖整单创建，不会再要求逐条确认。</small>':'<b>安全检查已通过</b><span>真实创建通道尚未就绪。</span>';
      else statusText='<b>方案已确认</b><span>下一步运行安全检查，不会写入 Meta。</span>';
      const technical=taskStatus?`<details class="growth-batch-technical"><summary>查看技术记录</summary><span>任务状态：${esc(taskStatus)} · 当前步骤：${esc(task.current_step||'-')} · 回执：${receipts.length} 条</span></details>`:'',running=['QUEUED','RUNNING','VERIFYING'].includes(taskStatus);
      const progressHtml=taskStatus?`<div class="growth-batch-progress" role="status" aria-live="polite"><div class="growth-batch-progress-head"><strong>整单创建进度</strong><span>${progress.done}/${progress.total}</span></div><div class="growth-batch-progress-track" role="progressbar" aria-label="广告创建进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progress.percent}"><i style="width:${progress.percent}%"></i></div><small>${created?'创建与回读已完成':`正在处理：${esc(launchBatchStepLabel(progress.current,plan))}`}</small><div class="growth-batch-connection" data-growth-batch-connection hidden></div></div>`:'';
      const safetyStepClass=!dryVerified?'current':'done',creationStepClass=created?'done':(taskStatus?'current':'');
      showModal(`<section class="growth-modal growth-batch-modal" data-growth-batch-plan-id="${esc(planId)}"><header class="growth-modal-head"><div><b>广告创建状态</b><small>${esc(plan.campaign?.name||'-')} · ${audienceMode?'受众策略':(copyMode?'广告文案':'素材方向')}</small></div><button type="button" class="growth-icon-button" data-modal-close aria-label="关闭">×</button></header><div class="growth-modal-body"><div class="growth-mini-steps growth-mini-steps-compact"><span class="done">方案确认</span><span class="${safetyStepClass}">安全检查</span><span class="${creationStepClass}">创建与核验</span></div><div class="growth-batch-status" role="status" aria-live="polite">${statusText}</div>${progressHtml}<div class="growth-batch-ledger">${rows}</div>${technical}</div><footer class="growth-modal-foot"><button type="button" data-modal-close>${running?'关闭并在后台继续':'关闭'}</button>${created?'<button type="button" class="growth-primary" id="growthViewCreatedOrder">查看订单状态</button>':''}${pageMismatch?'<button type="button" class="growth-primary" id="growthRepairBatchPage">修改公共主页</button>':''}${!pageMismatch&&!dryVerified&&next!=='PLAN_EXPIRED_REPLAN'?'<button type="button" class="growth-primary" id="growthRunBatchDry">运行安全检查</button>':''}${next==='SUBMIT_PAUSED_OBJECT_CREATION'&&liveAllowed?'<button type="button" class="growth-primary" id="growthCreateBatchPaused">创建广告</button>':''}${canResume?`<button type="button" class="growth-primary" id="growthResumeSamePlan">${externalPrerequisite?'自动核对并继续':'继续当前订单'}</button>`:''}</footer></section>`);
      document.getElementById('growthViewCreatedOrder')?.addEventListener('click',()=>{closeModal();renderLaunch('cold');loadLaunchOrders({badgeOnly:true});});
      document.getElementById('growthRunBatchDry')?.addEventListener('click',event=>runLaunchBatchDryRun(planId,event.currentTarget));
      document.getElementById('growthCreateBatchPaused')?.addEventListener('click',event=>createLaunchBatchPaused(planId,event.currentTarget));
      document.getElementById('growthResumeSamePlan')?.addEventListener('click',event=>resumeLaunchBatchSamePlan(planId,event.currentTarget));
      document.getElementById('growthRepairBatchPage')?.addEventListener('click',()=>openLaunchPageRepair(planId,String(cells.map(cell=>cell?.steps?.CREATIVE_CREATE?.object_story_spec?.page_id||'').find(Boolean)||'')));
      if(running)scheduleLaunchBatchWorkflowRefresh(planId,plan);
    }catch(error){
      if(options.automatic&&launchState.batchWorkflowPlanId===String(planId||'')){const connection=document.querySelector('[data-growth-batch-connection]');if(connection){connection.hidden=false;connection.textContent='连接暂时中断，正在自动重新获取；已显示的进度不会丢失。';}scheduleLaunchBatchWorkflowRefresh(planId,knownPlan);}
      else if(!options.automatic)showModalError(readableError(error));
    }
  }

  async function openLaunchIncidentResolution(planId,payload,plan) {
    const incident=payload.incident_resolution||{},cells=Array.isArray(plan.cells)?plan.cells:[],accountId=String(plan.target_account_id||''),currentPageId=String(incident.current_page_id||'');
    let repairPages=[],repairPage=null,eligibilityError='';
    if(incident.repair_supported&&accountId){
      try{
        await validateLaunchPagesForAccount(accountId,{force:true});
        repairPages=launchPagesForAccount(accountId).filter(item=>String(item.page_id||'')!==currentPageId);
        repairPage=repairPages[0]||null;
      }catch(error){eligibilityError=readableError(error);}
    }
    const completed=(incident.completed||[]).map(item=>`<li>${esc(item)}</li>`).join('')||'<li>暂无可安全复用的对象</li>';
    const incomplete=(incident.incomplete||[]).map(item=>`<li>${esc(item)}</li>`).join('')||'<li>等待系统核对</li>';
    const canResume=Boolean(payload.can_resume_same_plan);
    const repairReady=Boolean(incident.repair_supported&&repairPage);
    const pageChoices=repairPages.map((page,index)=>`<label class="growth-incident-page"><input type="radio" name="growthRepairPage" value="${esc(page.page_id)}" ${index===0?'checked':''}><span><b>${esc(page.name||'已验证主页')}</b><small>主页 ${esc(page.page_id)} · 已通过该账户投放校验</small></span></label>`).join('');
    const recommendation=repairReady
      ?`系统找到 ${repairPages.length} 个可用主页。请选择一个，系统会生成新方案并从未完成步骤继续；已完成对象不会重建。`
      :(canResume?'系统已确认这次可以沿用原方案，从未完成步骤继续；不会重放已成功的写入。':(eligibilityError?`暂时无法读取可用主页：${esc(eligibilityError)}`:'系统尚未找到可安全自动继续的路径。'));
    const help=repairReady?pageChoices:(canResume?'<div class="growth-safety">这一步沿用你已确认的整单方案，不会开启投放；完成后广告仍保持暂停。</div>':'<div class="growth-safety">你无需核对技术回执。请先在 Meta 广告管理工具确认同名广告系列是否存在且保持暂停，再返回点击“重新检测”。</div>');
    const primary=repairReady?'<button type="button" class="growth-primary" id="growthConfirmRepairPlan">使用所选主页继续创建</button>':(canResume?'<button type="button" class="growth-primary" id="growthResumeIncidentPlan">核对并继续当前订单</button>':'<button type="button" class="growth-primary" id="growthRefreshIncident">重新检测</button>');
    const metaLink=!repairReady&&!canResume&&accountId?'<button type="button" id="growthOpenMetaManager">打开 Meta 广告管理工具</button>':'';
    showModal(`<section class="growth-modal growth-batch-modal"><header class="growth-modal-head"><div><b>处理整单创建异常</b><small>${esc(plan.campaign?.name||'-')} · 只处理这一次，${cells.length||'全部'} 项同步更新</small></div><button type="button" class="growth-icon-button" data-modal-close aria-label="关闭">×</button></header><div class="growth-modal-body"><div class="growth-batch-status"><b>发生了什么</b><span>${esc(incident.root_cause_zh||'系统无法确认完整创建结果。')}</span><small>系统已停止旧任务并保留已确认对象，当前不会产生花费。</small></div><div class="growth-incident-columns"><section><b>系统已保留</b><ul>${completed}</ul></section><section><b>还要完成</b><ul>${incomplete}</ul></section></div><div class="growth-batch-status"><b>${repairReady||canResume?'需要你确认 1 件事':'需要你完成 1 项外部核对'}</b><span>${recommendation}</span>${help}</div></div><footer class="growth-modal-foot"><button type="button" data-modal-close>稍后处理</button>${metaLink}${primary}</footer></section>`);
    document.querySelectorAll('input[name="growthRepairPage"]').forEach(input=>input.addEventListener('change',()=>{repairPage=repairPages.find(page=>String(page.page_id)===String(input.value))||repairPage;}));
    document.getElementById('growthConfirmRepairPlan')?.addEventListener('click',event=>confirmLaunchRepairPlan(planId,String(repairPage?.page_id||''),event.currentTarget));
    document.getElementById('growthResumeIncidentPlan')?.addEventListener('click',event=>resumeLaunchBatchSamePlan(planId,event.currentTarget));
    document.getElementById('growthRefreshIncident')?.addEventListener('click',()=>openLaunchBatchWorkflow(planId));
    document.getElementById('growthOpenMetaManager')?.addEventListener('click',()=>window.open(`https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=${encodeURIComponent(accountId.replace(/^act_/,''))}`,'_blank','noopener,noreferrer'));
  }

  async function confirmLaunchRepairPlan(planId,targetPageId,button) {
    const originalText=button.textContent;
    try{
      button.disabled=true;button.textContent='正在生成新方案并继续…';
      const body={target_page_id:targetPageId,confirmation:'APPROVE_REPAIR_PLAN'};
      const result=await api(`/api/ops/ad-data-dashboard/meta-plans/${encodeURIComponent(planId)}/repair-page-plan`,{method:'POST',headers:postHeaders('new-account-page-repair',{plan_id:planId,...body}),body:JSON.stringify(body)});
      launchState.batchPlan={plan_id:String(result.repair_plan_id||''),status:'QUEUED'};
      await openLaunchBatchWorkflow(String(result.repair_plan_id||''));
    }catch(error){button.disabled=false;button.textContent=originalText;showModalError(readableError(error));}
  }

  async function runLaunchBatchDryRun(planId,button) {
    try{button.disabled=true;button.textContent='正在校验实际广告结构…';const body={execution_mode:'dry_run'};await api(`/api/ops/ad-data-dashboard/meta-plans/${encodeURIComponent(planId)}/execute`,{method:'POST',headers:postHeaders('new-account-batch-dry-run',{plan_id:planId,...body}),body:JSON.stringify(body)});await openLaunchBatchWorkflow(planId);}catch(error){button.disabled=false;showModalError(readableError(error));}
  }

  async function createLaunchBatchPaused(planId,button) {
    try{button.disabled=true;button.textContent='正在提交整单创建…';const body={execution_mode:'live',confirmation:'CREATE_PAUSED_OBJECTS'};await api(`/api/ops/ad-data-dashboard/meta-plans/${encodeURIComponent(planId)}/execute`,{method:'POST',headers:postHeaders('new-account-batch-live',{plan_id:planId,...body}),body:JSON.stringify(body)});await openLaunchBatchWorkflow(planId);}catch(error){button.disabled=false;showModalError(readableError(error));}
  }

  async function resumeLaunchBatchSamePlan(planId,button) {
    const originalText=button.textContent;
    try{button.disabled=true;button.textContent='正在核对 Meta 并继续创建…';const body={confirmation:'CONTINUE_SAME_PLAN'};const result=await api(`/api/ops/ad-data-dashboard/meta-plans/${encodeURIComponent(planId)}/resume-same-plan`,{method:'POST',headers:postHeaders('new-account-batch-resume-same-plan',{plan_id:planId,...body}),body:JSON.stringify(body)});const resumedPlanId=String(result.resumed_plan_id||planId);launchState.batchPlan={plan_id:resumedPlanId,status:'QUEUED'};await openLaunchBatchWorkflow(resumedPlanId);}catch(error){button.disabled=false;button.textContent=originalText;showModalError(readableError(error));}
  }

  function bindLaunchScreen(screen) {
    const node=document.getElementById('growthLaunchContent');
    const existing=node.querySelector('[data-launch-existing]');if(existing)existing.addEventListener('click',()=>{closeLaunchWorkspace();openWorkspace();});
    const fresh=node.querySelector('[data-launch-new]');if(fresh)fresh.addEventListener('click',()=>{resetLaunchOrderContext();launchState.taskHomeView='orders';renderLaunch('mode');});
    node.querySelectorAll('[data-launch-mode]').forEach(button=>button.addEventListener('click',()=>{launchState.experimentMode=String(button.dataset.launchMode||'creative_direction');if(launchState.experimentMode==='audience_strategy')applyFixedLaunchAudience('BR');renderLaunch('goal');}));
    node.querySelectorAll('[data-launch-order-view]').forEach(button=>button.addEventListener('click',()=>{launchState.launchArchiveView=button.dataset.launchOrderView==='archived';renderLaunch('orders');}));
    node.querySelectorAll('[data-launch-order-filter]').forEach(button=>button.addEventListener('click',()=>{launchState.launchArchiveView=false;launchState.launchOrderFilter=String(button.dataset.launchOrderFilter||'all');renderLaunch('orders');}));
    node.querySelectorAll('[data-launch-archive]').forEach(button=>button.addEventListener('click',()=>confirmArchiveLaunch(button.dataset.launchArchive||'')));
    node.querySelectorAll('[data-launch-restore]').forEach(button=>button.addEventListener('click',()=>changeLaunchArchiveState(button.dataset.launchRestore||'','restore')));
    node.querySelectorAll('[data-launch-permanent-delete]').forEach(button=>button.addEventListener('click',()=>confirmPermanentDeleteLaunch(button.dataset.launchPermanentDelete||'')));
    node.querySelectorAll('[data-launch-order]').forEach(button=>button.addEventListener('click',()=>openLaunchOrder(String(button.dataset.launchOrder||''))));
    node.querySelectorAll('[data-launch-task-view]').forEach(button=>button.addEventListener('click',()=>{launchState.taskHomeView=String(button.dataset.launchTaskView||'orders');launchState.taskSearch='';renderLaunch('orders');}));
    const taskSearch=node.querySelector('[data-launch-task-search]');if(taskSearch)taskSearch.addEventListener('input',event=>{launchState.taskSearch=String(event.currentTarget.value||'');renderLaunch('orders',{preserveScroll:true});const replacement=node.querySelector('[data-launch-task-search]');if(replacement){replacement.focus();replacement.setSelectionRange(replacement.value.length,replacement.value.length);}});
    node.querySelectorAll('[data-growth-task]').forEach(button=>button.addEventListener('click',()=>{const taskView=launchState.taskHomeView;closeLaunchWorkspace();openWorkspace(button.dataset.growthTask,{returnTarget:{kind:'taskHome',taskView}});}));
    node.querySelectorAll('[data-launch-back]').forEach(button=>button.addEventListener('click',()=>renderLaunch(screen==='mode'?'orders':(screen==='goal'?'mode':'goal'))));
    const account=node.querySelector('#growthLaunchAccount');if(account)account.addEventListener('change',async()=>{const selected=launchState.accounts.find(item=>item.account_id===account.value&&item.selectable);launchState.target={...launchState.target,account_id:String(selected?.account_id||''),account_name:String(selected?.name||''),page_id:'',page_name:''};if(selected)localStorage.setItem('growth-last-meta-account',selected.account_id);launchState.accountsLoading=true;renderLaunch('goal');try{if(selected)await validateLaunchPagesForAccount(selected.account_id);}catch(error){launchState.error=readableError(error);}finally{launchState.accountsLoading=false;renderLaunch('goal');}});
    const page=node.querySelector('#growthLaunchPage');if(page)page.addEventListener('change',()=>{const selected=launchPagesForAccount().find(item=>item.page_id===page.value);launchState.target={...launchState.target,page_id:String(selected?.page_id||''),page_name:String(selected?.name||'')};if(selected)localStorage.setItem('growth-last-meta-page',selected.page_id);});
    const country=node.querySelector('#growthLaunchCountry');if(country)country.addEventListener('change',async()=>{applyFixedLaunchAudience(country.value);launchState.target={...launchState.target,page_id:'',page_name:''};launchState.accountsLoading=true;renderLaunch('goal');try{const targetAccountId=String(launchState.countryAccountIds?.[String(country.value).toUpperCase()]||'');const selected=launchState.accounts.find(item=>String(item.account_id||'')===targetAccountId&&item.selectable===true);if(selected)launchState.target={...launchState.target,account_id:String(selected.account_id),account_name:String(selected.name||'')};if(launchState.target.account_id)await validateLaunchPagesForAccount(launchState.target.account_id);}catch(error){launchState.error=readableError(error);}finally{launchState.accountsLoading=false;renderLaunch('goal');}});
    const sync=node.querySelector('[data-launch-sync]');if(sync)sync.addEventListener('click',()=>loadLaunchAccounts({force:true}));
    const form=node.querySelector('#growthLaunchGoal');if(form)form.addEventListener('submit',event=>{event.preventDefault();const data=new FormData(form);const country=launchState.experimentMode==='audience_strategy'?'BR':String(data.get('country')||'BR');if(!applyFixedLaunchAudience(country)){launchState.error='该国家尚未配置严格受众规则。';renderLaunch('goal');return;}launchState.target={...launchState.target,app:'Tugao',daily:String(data.get('daily')||'200'),cpi:String(data.get('cpi')||'0.30')};launchState.error='';launchState.regenerationRound=0;if(launchState.target.account_id)localStorage.setItem('growth-last-meta-account',launchState.target.account_id);if(launchState.experimentMode==='audience_strategy')previewLaunchAudienceExperiment();else if(launchState.experimentMode==='copy_variant')previewLaunchCopyExperiment();else previewLaunchDirections();});
    node.querySelectorAll('[data-direction-index]').forEach(card=>{const index=Number(card.dataset.directionIndex),item=launchState.directions[index];if(!item)return;const select=card.querySelector('[data-direction-select]'),hypothesis=card.querySelector('[data-direction-hypothesis]'),budget=card.querySelector('[data-direction-budget]');if(select)select.addEventListener('change',()=>{item.selected=select.checked;renderLaunch('plan');});if(hypothesis)hypothesis.addEventListener('input',()=>{item.hypothesis=hypothesis.value.slice(0,180);});if(budget)budget.addEventListener('change',()=>{item.initial_daily_budget=Math.max(5,Math.min(100,Number(budget.value||20)));renderLaunch('plan');});});
    const approve=node.querySelector('[data-launch-approve]');if(approve)approve.addEventListener('click',()=>createNewAccountLaunch(approve));
    const frozen=node.querySelectorAll('input[name="growthFrozenCreative"]');frozen.forEach(input=>input.addEventListener('change',()=>{launchState.frozenCreativeId=String(input.value||'');renderLaunch(screen==='copy-plan'?'copy-plan':'audience-plan');}));
    const audienceRound=node.querySelector('#growthAudienceRound');if(audienceRound)audienceRound.addEventListener('change',()=>{launchState.audienceRound=Number(audienceRound.value||0);renderLaunch('audience-plan');});
    const audienceApprove=node.querySelector('[data-launch-audience-approve]');if(audienceApprove)audienceApprove.addEventListener('click',()=>createNewAccountAudienceLaunch(audienceApprove));
    const copyApprove=node.querySelector('[data-launch-copy-approve]');if(copyApprove)copyApprove.addEventListener('click',()=>createNewAccountCopyLaunch(copyApprove));
    const reset=node.querySelector('[data-launch-reset]');if(reset)reset.addEventListener('click',()=>renderLaunch('goal'));
    const viewTarget=node.querySelector('[data-launch-view-target]');if(viewTarget)viewTarget.addEventListener('click',openLaunchTargetSummary);
    const orderRepair=node.querySelector('[data-launch-order-repair]');if(orderRepair)orderRepair.addEventListener('click',()=>{if(guardLaunchMetaAction())return;openLaunchBatchWorkflow(String(launchState.batchPlan?.plan_id||''));});
    const refreshDelivery=node.querySelector('[data-launch-refresh-delivery]');if(refreshDelivery)refreshDelivery.addEventListener('click',()=>refreshLaunchDeliveryStatus({manual:true,render:true}));
    const refreshPerformance=node.querySelector('[data-launch-refresh-performance]');if(refreshPerformance)refreshPerformance.addEventListener('click',refreshLaunchPerformanceData);
    node.querySelectorAll('[data-launch-replacement]').forEach(button=>button.addEventListener('click',()=>{const experiment=(launchState.experiments||[]).find(item=>String(item.experiment_id||'')===String(button.dataset.launchReplacement||''));if(experiment)openRejectedCreativeReplacement(experiment);}));
    const activationException=node.querySelector('[data-launch-activation-exception]');if(activationException)activationException.addEventListener('click',openLaunchActivationException);
    const dismiss=node.querySelector('[data-launch-dismiss]');if(dismiss)dismiss.addEventListener('click',()=>{persistLaunchProgress();closeLaunchWorkspace();showLaunchToast('素材继续在后台生成，完成后会在“广告任务”入口提醒你');});
    const material=node.querySelector('[data-launch-material]');if(material)material.addEventListener('click',()=>{if(['audience_strategy','copy_variant'].includes(launchState.experimentMode)){openLaunchBatchPlan();return;}const progress=launchProgressSnapshot();if(launchState.jobs.length<progress.expected){createLaunchCreativeJobs().catch(error=>{launchState.error=readableError(error);renderLaunch('cold');});return;}showLaunchToast('订单已确认，AI 会在后台自动继续');});
    node.querySelectorAll('[data-launch-delivery-experiment]').forEach(button=>button.addEventListener('click',async()=>{if(guardLaunchMetaAction())return;const experimentId=String(button.dataset.launchDeliveryExperiment||''),launchId=String((launchState.launch||{}).launch_id||''),experiment=(launchState.experiments||[]).find(item=>String(item.experiment_id||'')===experimentId),action=String(button.dataset.launchDeliveryAction||'inspect');if(!experimentId)return;button.disabled=true;try{if(action==='inspect'){closeLaunchWorkspace();await openWorkspace(experimentId,{returnTarget:{kind:'launch',launchId}});return;}const payload=await api(`/api/ops/ad-data-dashboard/experiments/${encodeURIComponent(experimentId)}`),detail=payload.experiment||experiment||{};if(action==='pause')openPausePlan(detail,payload,{launchId});else openActivationConfirmation(detail,payload,{launchId});}catch(error){showLaunchToast(readableError(error));}finally{button.disabled=launchMetaRateLimitBlocked();}}));
  }

  async function createNewAccountAudienceLaunch(button) {
    const target=launchState.target,preview=launchState.audiencePreview||{},rounds=preview.experiment_policy?.rounds||[],round=rounds[launchState.audienceRound]||rounds[0];
    if(!round||!launchState.frozenCreativeId){launchState.error='请选择一张已审核获胜素材和一轮受众实验。';renderLaunch('audience-plan');return;}
    const body={target_app:'tugao',country:'BR',account_id:target.account_id,account_name:target.account_name,daily_spend_target:Number(target.daily),cpi_target:Number(target.cpi),page_id:target.page_id,naming_date:launchState.namingDate,frozen_creative_id:launchState.frozenCreativeId,audience_strategies:[round.baseline,round.challenger],initial_daily_budget:20};
    try{button.disabled=true;button.textContent='正在保存受众实验…';const created=await api('/api/ops/ad-data-dashboard/new-account-launches/audience',{method:'POST',headers:postHeaders('new-account-audience-launch',body),body:JSON.stringify(body)});resetLaunchOrderContext();launchState.launch=created;await refreshCreatedLaunchOrder(created,[]);launchState.approved=true;launchState.error='';launchState.screen='cold';persistLaunchProgress();updateLaunchProgressBadge();renderLaunch('cold');showLaunchToast('两组受众实验已保存；Meta 写入 0');}
    catch(error){button.disabled=false;launchState.error=readableError(error);renderLaunch('audience-plan');}
  }

  async function createNewAccountCopyLaunch(button) {
    const target=launchState.target;
    const variants=[...document.querySelectorAll('[data-copy-variant]')].map((node,index)=>({
      primary_text:String(node.querySelector('[data-copy-primary]')?.value||'').trim(),
      headline:String(node.querySelector('[data-copy-headline]')?.value||'').trim(),
      description:String(node.querySelector('[data-copy-description]')?.value||'').trim(),
      hypothesis:String(node.querySelector('[data-copy-hypothesis]')?.value||'').trim(),
      benchmark_version:String(launchState.copyVariants[index]?.benchmark_version||AD_COPY_BENCHMARK_VERSION),
    }));
    if(!launchState.frozenCreativeId||variants.length!==2||variants.some(item=>!item.primary_text||!item.headline||!item.hypothesis)){launchState.error='请选择一张已审核素材，并补齐两套文案和实验假设。';renderLaunch('copy-plan');return;}
    const signatures=variants.map(item=>stableJson([item.primary_text,item.headline,item.description]));
    if(new Set(signatures).size!==2){launchState.error='两套文案完全相同，无法形成文案实验。';renderLaunch('copy-plan');return;}
    const body={target_app:'tugao',country:target.country,account_id:target.account_id,account_name:target.account_name,daily_spend_target:Number(target.daily),cpi_target:Number(target.cpi),page_id:target.page_id,naming_date:launchState.namingDate,frozen_creative_id:launchState.frozenCreativeId,copy_variants:variants,initial_daily_budget:10};
    try{button.disabled=true;button.textContent='正在保存文案实验…';launchState.launch=await api('/api/ops/ad-data-dashboard/new-account-launches/copy',{method:'POST',headers:postHeaders('new-account-copy-launch',body),body:JSON.stringify(body)});launchState.copyVariants=variants;launchState.jobs=[];launchState.approved=true;launchState.error='';launchState.screen='cold';persistLaunchProgress();updateLaunchProgressBadge();renderLaunch('cold');showLaunchToast('两组文案实验已保存；Meta 写入 0');}
    catch(error){button.disabled=false;launchState.error=readableError(error);renderLaunch('copy-plan');}
  }

  async function createNewAccountLaunch(button) {
    const target=launchState.target,directions=selectedLaunchDirections();
    if(!/^\d+$/.test(target.account_id)){launchState.error='Meta 广告账户 ID 必须是数字。';renderLaunch('plan');return;}
    if(directions.length<2||directions.length>4){launchState.error='请从固定方向库中选择 2–4 个素材方向。';renderLaunch('plan');return;}
    const creativeDirections=directions.map(item=>({direction_id:item.direction_id,key:item.key||item.direction_id,code:item.code,title:item.title,hypothesis:String(item.hypothesis||'').trim(),rationale:item.rationale||'',source:item.source||'core_catalog',initial_daily_budget:Number(item.initial_daily_budget||20)}));
    const body={target_app:'tugao',country:target.country,account_id:target.account_id,account_name:target.account_name,daily_spend_target:Number(target.daily),cpi_target:Number(target.cpi),page_id:target.page_id,naming_date:launchState.namingDate,creative_directions:creativeDirections};
    try{button.disabled=true;button.textContent=`正在创建 ${directions.length} 个持久实验…`;const created=await api('/api/ops/ad-data-dashboard/new-account-launches',{method:'POST',headers:postHeaders('new-account-launch',body),body:JSON.stringify(body)});resetLaunchOrderContext();launchState.launch=created;const jobs=await createLaunchCreativeJobs();await refreshCreatedLaunchOrder(created,jobs);launchState.error='';launchState.screen='cold';launchState.lastNotifiedPhase='generating';persistLaunchProgress();updateLaunchProgressBadge();renderLaunch('cold');showLaunchToast(`已创建 ${directions.length} 个实验与 ${directions.length} 个素材任务，Meta 写入 0`);}
    catch(error){launchState.error=readableError(error);renderLaunch('plan');}
  }

  async function refreshCreatedLaunchOrder(created,fallbackJobs=[]) {
    const launchId=String(created?.launch_id||'');
    if(!launchId)throw new Error('订单创建成功，但没有返回订单编号。');
    try{
      const order=await api(`/api/ops/ad-data-dashboard/new-account-launches/${encodeURIComponent(launchId)}`);
      hydrateLaunchOrder(order);
      await refreshLaunchMetaRateLimit();
      return true;
    }catch(error){
      resetLaunchOrderContext({clearStored:false});launchState.launch=created;launchState.jobs=Array.isArray(fallbackJobs)?fallbackJobs:[];launchState.orderDataMismatch=true;launchState.error=readableError(error);persistLaunchProgress();return false;
    }
  }

  async function createLaunchCreativeJobs() {
    const launch=launchState.launch||{},target=launchState.target,variants=launch.variants||[];
    if(!variants.length)throw new Error('启动实验尚未创建。');
    const jobs=[];
    for(const variant of variants){
      const genderLabel={all:'不限性别',female:'女性',male:'男性'}[target.gender]||'不限性别';
      const names=variant.meta_names||{},direction=variant.creative_direction||{};
      const baseTargeting={country:target.country,gender:target.gender,gender_label:genderLabel,age_min:Number(target.age_min),age_max:Number(target.age_max),language:target.language};
      const body={country:target.country,project:'Tugao',campaign:names.campaign||`TG_${target.country}_INS_CS`,ad_group:names.adset||`${target.country}_BD_C${variant.variant}`,ad:names.ad||`${direction.code||'EXP'}_ST_H1_V1`,objective:'真实入会',audience:'广泛受众',audience_strategy:'BROAD',audience_strategy_label:'广泛受众',base_targeting:baseTargeting,core_offer:variant.creative_angle,target_app:'tugao',account_id:target.account_id,recommendation_id:variant.recommendation_id,experiment_mode:'new_test',generation_count:1,candidate_count:1,production_task:{mode:'new_test',target_app:'tugao',account_id:target.account_id,growth_experiment_id:variant.experiment_id,launch_id:launch.launch_id,creative_angle:variant.creative_angle,creative_direction:direction,meta_names:names,initial_daily_budget:variant.initial_daily_budget,page_id:target.page_id,audience_strategy:'BROAD',audience_strategy_label:'广泛受众',base_targeting:baseTargeting,targeting:baseTargeting}};
      const result=await api('/api/ops/ad-data-dashboard/creative-images/generate',{method:'POST',headers:postHeaders('new-account-creative',body),body:JSON.stringify(body)});
      if(!result.ok||!result.job)throw new Error(result.message_cn||result.detail||'素材任务创建失败');
      jobs.push(result.job);
    }
    launchState.jobs=jobs;launchState.approved=true;persistLaunchProgress();updateLaunchProgressBadge();
    return jobs;
  }

  function showLaunchToast(message) { const old=document.querySelector('.growth-launch-toast');if(old)old.remove();const node=document.createElement('div');node.className='growth-launch-toast';node.textContent=message;document.body.appendChild(node);setTimeout(()=>node.remove(),2600); }

  async function loadList(options) {
    const detail = document.getElementById('growthDetail');
    if (detail && !options?.silent) detail.innerHTML = '<div class="growth-empty"><div><b>正在读取实验</b></div></div>';
    try {
      let payload;
      if (isEmbeddedWorkspace()) {
        const selectedId=String(options?.select||'').trim();
        const requestedIds=new Set(state.coverageScope);
        if(selectedId)requestedIds.add(selectedId);
        const results = await Promise.allSettled([...requestedIds].map(async id=>{
          const item=await api(`/api/ops/ad-data-dashboard/experiments/${encodeURIComponent(id)}`);
          return {...(item.experiment||{}),workflow:item.workflow||item.experiment?.workflow||{}};
        }));
        const items=results.filter(result=>result.status==='fulfilled').map(result=>result.value);
        if(selectedId&&!items.some(item=>String(item?.experiment_id||'')===selectedId)){
          throw new Error('任务详情暂时无法读取，请重试；系统未执行任何 Meta 操作。');
        }
        if(selectedId&&!state.coverageScope.has(selectedId)){
          state.coverageScope.add(selectedId);
          const mount=document.getElementById('adGleTaskWorkbenchMount');
          if(mount)mount.dataset.experimentIds=JSON.stringify([...state.coverageScope]);
        }
        payload={items};
      } else {
        payload=await api('/api/ops/ad-data-dashboard/experiments?limit=200');
      }
      state.experiments = payload.items || [];
      const count = document.getElementById('growthWorkspaceCount');
      if (count) count.textContent = String(state.experiments.filter(item=>String((item.workflow||{}).bucket||'action_required')==='action_required').length);
      const selectedItem=options?.select?scopedExperiments().find(item=>item.experiment_id===options.select):null;
      if(selectedItem)state.workBucket=String((selectedItem.workflow||{}).bucket||'action_required');
      renderQueueTabs();
      const desired = selectedItem?.experiment_id || (scopedExperiments().some(item=>item.experiment_id===state.activeExperiment)?state.activeExperiment:'');
      if (desired) await openAdExperiment(desired);
      else renderExperimentQueue();
      return true;
    } catch (error) {
      if (detail) detail.innerHTML = `<div class="growth-error">${esc(readableError(error))}</div>`;
      return false;
    }
  }

  function filteredExperiments() {
    const query=state.taskSearch.toLowerCase();
    const scoped=scopedExperiments();
    const bucketed=state.workBucket==='all'?scoped:scoped.filter(item=>String((item.workflow||{}).bucket||'action_required')===state.workBucket);
    return bucketed.filter(item=>!query||`${experimentTitle(item)} ${(item.workflow||{}).current_action||''} ${item.country||''} ${statusLabel(item.state)}`.toLowerCase().includes(query)).sort((left,right)=>taskPriority(left)-taskPriority(right)||String(right.updated_at||'').localeCompare(String(left.updated_at||'')));
  }

  function taskPriority(item) {
    const workflow=item.workflow||{},stateName=String(item.state||'');
    if(workflow.plan_expired)return 0;
    if(workflow.bucket==='exception')return 1;
    if(['WAITING_CREATE_APPROVAL','WAITING_ADJUSTMENT_APPROVAL','RECOMMENDATION_READY'].includes(stateName))return 2;
    if(workflow.bucket==='action_required')return 3;
    if(workflow.bucket==='observing')return 8;
    return 9;
  }

  function renderQueueTabs() {
    const counts={action_required:0,system_work:0,observing:0,exception:0,completed:0};
    scopedExperiments().forEach(item=>{const key=String((item.workflow||{}).bucket||'action_required');counts[key]=(counts[key]||0)+1;});
    document.querySelectorAll('[data-growth-bucket]').forEach(button=>{const key=button.dataset.growthBucket||'action_required';button.classList.toggle('is-active',key===state.workBucket);const badge=button.querySelector('span');if(badge)badge.textContent=String(counts[key]||0);});
    const audit=document.getElementById('growthAuditAll');if(audit)audit.classList.toggle('is-active',state.workBucket==='all');
  }

  function renderExperimentQueue() {
    setWorkspaceDetailMode(false);
    const node=document.getElementById('growthDetail');
    const items=filteredExperiments();
    if(!items.length){renderEmptyState();return;}
    const config={
      action_required:['今天需要你处理',`${items.length} 条任务已按风险和紧迫程度排序。`],
      system_work:['AI 处理中',`${items.length} 条任务正在自动推进，不需要你核对方案或重复操作。`],
      exception:['异常',`${items.length} 条结果不确定，系统已停止重复写入。`],
      observing:['观察中','这些广告由系统自动跟踪；没有触发条件时无需逐条查看。'],
      all:['全部记录','仅用于搜索和追溯，不是日常工作入口。'],
    }[state.workBucket]||['任务记录',''];
    const visible=items.slice(0,state.workBucket==='action_required'?50:20);
    node.innerHTML=`<section class="growth-queue-head"><div><h2>${esc(config[0])}</h2><p>${esc(config[1])}</p></div>${items.length>visible.length?`<span class="growth-queue-limit">显示最近 ${visible.length} 条 · 可搜索</span>`:''}</section><div class="growth-task-list">${taskGroupsHtml(visible)}</div>`;
    node.querySelectorAll('[data-growth-task]').forEach(button=>button.addEventListener('click',()=>openAdExperiment(button.dataset.growthTask)));
    node.querySelectorAll('[data-growth-order-incident-plan]').forEach(button=>button.addEventListener('click',()=>openLaunchBatchWorkflow(String(button.dataset.growthOrderIncidentPlan||''))));
    const drawerTitle=document.getElementById('growthDrawerTitle');if(drawerTitle)drawerTitle.textContent=isEmbeddedWorkspace()?'关联任务':'广告优化待办';
    const drawerContext=document.getElementById('growthDrawerContext');if(drawerContext)drawerContext.textContent=isEmbeddedWorkspace()?`${scopedExperiments().length} 项与覆盖广告精确绑定 · 点击查看判断依据和下一步`:'系统分析广告表现，你只处理需要确认的事项';
  }

  function taskGroupsHtml(items) {
    const groups=[];
    items.forEach(item=>{
      const launchId=String((item.workflow||{}).launch_id||'');
      if(!launchId){groups.push({launchId:'',items:[item]});return;}
      let group=groups.find(entry=>entry.launchId===launchId);
      if(!group){group={launchId,items:[]};groups.push(group);}
      group.items.push(item);
    });
    return groups.map(group=>{
      if(!group.launchId)return taskCardHtml(group.items[0]);
      const sample=group.items[0],groupTitle=experimentOrderGroupTitle(sample);
      const creationIncident=group.items.some(item=>(item.workflow||{}).bucket==='exception'&&(String(item.state||'')==='CREATION_PARTIAL_FAILURE'||(String(item.state||'')==='DATA_INCOMPLETE'&&String((item.workflow||{}).plan_action_type||'')==='CREATE_PAUSED_AD')));
      const incidentPlan=String((group.items.find(item=>String((item.workflow||{}).plan_id||''))?.workflow||{}).plan_id||'');
      const rows=group.items.map(item=>{const workflow=item.workflow||{},bucket=String(workflow.bucket||'action_required'),reactivation=String(item.state||'')==='WAITING_ADJUSTMENT_APPROVAL'&&String(workflow.plan_action_type||'')==='REACTIVATE_AD',stateStage={CREATIVE_REVIEW:'AI 审核素材',CREATIVE_APPROVED:'AI 生成方案',WAITING_CREATE_APPROVAL:'AI 安全演练',CREATING_PAUSED_OBJECTS:'AI 创建并回读',DRY_RUN_VERIFIED:'AI 准备创建'}[String(item.state||'')],stage=reactivation?'待确认开启投放':(bucket==='system_work'?(workflow.current_action||stateStage||'AI 处理中'):bucket==='exception'?(creationIncident?'需要处理：核对创建结果':`需要处理：${workflow.current_action||'核对异常回执'}`):bucket==='observing'?(workflow.next_checkpoint?`等待 ${workflow.next_checkpoint}`:'观察中'):(stateStage||statusLabel(item.state)));return `<button type="button" class="growth-task-group-row" data-growth-task="${esc(item.experiment_id)}"><span class="growth-task-group-copy"><strong>${esc(experimentAdSetName(item))}</strong><small>${esc(experimentTitle(item))}</small></span><span>${esc(stage)}</span><i>›</i></button>`;}).join('');
      const groupAction=creationIncident&&incidentPlan?`<button type="button" class="growth-task-group-action" data-growth-order-incident-plan="${esc(incidentPlan)}">处理整单异常</button>`:'';
      return `<section class="growth-task-group"><header><b title="${esc(groupTitle)}">${esc(groupTitle)}</b><div class="growth-task-group-head-actions"><span>${group.items.length} 项同步处理</span>${groupAction}</div></header><div class="growth-task-group-list">${rows}</div></section>`;
    }).join('');
  }

  function taskCardHtml(item) {
    const workflow=item.workflow||{},bucket=String(workflow.bucket||'action_required');
    const checkpoint=workflow.next_checkpoint?`下一检查点 ${workflow.next_checkpoint}`:'';
    const next=bucket==='system_work'?(workflow.current_action||'AI 自动处理中'):bucket==='observing'?(checkpoint||'系统持续观察'):bucket==='exception'?'查看异常原因':(workflow.current_action||'查看并处理');
    const action=bucket==='system_work'?'无需操作':bucket==='observing'?'查看观察进度':bucket==='exception'?'查看异常':(workflow.current_action||'查看并处理');
    return `<button type="button" class="growth-task-card is-${esc(bucket)}" data-growth-task="${esc(item.experiment_id)}" aria-label="${esc(`${experimentTitle(item)}，${next}`)}"><div><strong>${esc(experimentTitle(item))}</strong><p>${esc(next)}</p><span class="growth-task-meta"><span>${esc(String(item.target_app||'Tugao').replace(/^./,char=>char.toUpperCase()))}</span><span>${esc(item.country||'-')}</span><span>${esc(statusLabel(item.state))}</span></span></div><span class="growth-task-next">${esc(action)} →</span></button>`;
  }

  function renderEmptyState() {
    setWorkspaceDetailMode(false);
    const node = document.getElementById('growthDetail');
    const copy=state.workBucket==='action_required'?['今天没有需要你处理的任务','AI 处理中和继续观察都不会占用你的待办。']:state.workBucket==='system_work'?['当前没有 AI 处理中的任务','提交订单后，素材审核、方案、安全演练、暂停态创建和回读会自动完成。']:state.workBucket==='observing'?['当前没有观察记录','实验启用后，D1 / D3 / D7 会自动记录在这里。']:state.workBucket==='exception'?['当前没有异常','只有无法安全判定的创建或回读异常才会出现在这里。']:['没有匹配的记录','可以修改搜索关键词，或返回“需要你处理”。'];
    node.innerHTML = `<div class="growth-empty"><div><b>${copy[0]}</b><span>${copy[1]}</span></div></div>`;
  }

  function setWorkspaceDetailMode(active) {
    document.querySelector('#growthWorkspacePanel .growth-queue-tabs')?.toggleAttribute('hidden',Boolean(active));
    document.querySelector('#growthWorkspacePanel .growth-workbar')?.toggleAttribute('hidden',Boolean(active));
    const panel=document.getElementById('growthWorkspacePanel');
    if(panel&&isEmbeddedWorkspace())panel.classList.toggle('is-detail-open',Boolean(active));
    if(!active)document.getElementById('growthDetail')?.classList.remove('has-autonomy-panel');
  }

  function experimentTitle(experiment) {
    const hypothesis=experiment.hypothesis_json||{},direction=hypothesis.creative_direction||{};
    const directionTitle=direction.title||hypothesis.creative_angle||'';
    if(String(experiment.source_report_id||'').startsWith('newacct_')&&directionTitle)return directionTitle;
    return experiment.experiment_name || experiment.title || [String(experiment.target_app||'').replace(/^./, char => char.toUpperCase()), experiment.country, experimentTypeLabel(experiment.experiment_type)].filter(Boolean).join(' · ') || experiment.experiment_code || '广告实验';
  }

  function experimentOrderSuffix(experiment) {
    const source=String(experiment.source_report_id||'');
    return source.startsWith('newacct_')?source.slice(-6).toUpperCase():'';
  }

  function experimentAccountName(experiment) {
    const accountId=String(experiment.account_id||'');
    const account=(launchState.accounts||[]).find(item=>String(item.account_id||'')===accountId);
    const persisted=String(experiment.workflow?.account_name||experiment.hypothesis_json?.account_name||'').trim();
    return String(persisted||account?.name||(accountId?`广告账户 ····${accountId.slice(-4)}`:'广告账户'));
  }

  function experimentOrderGroupTitle(experiment) {
    const order=experimentOrderSuffix(experiment)||'—';
    const campaign=experimentCampaignName(experiment)||'未命名广告系列';
    return `订单 ${order} · ${campaign} · ${experimentAccountName(experiment)}`;
  }

  function experimentDrawerTitle(experiment) {
    const campaign=experimentCampaignName(experiment),adset=experimentAdSetName(experiment);
    if(campaign)return `${campaign} · ${adset}`;
    const order=experimentOrderSuffix(experiment);
    return `${experimentTitle(experiment)}${order?` · 订单 ${order}`:''}`;
  }

  function experimentCampaignName(experiment) {
    const hypothesis=experiment.hypothesis_json||{},direction=hypothesis.creative_direction||{};
    const hypothesisNames=hypothesis.meta_names||{},directionNames=direction.meta_names||{},variant=experiment.variant_definition_json||{},variantNames=variant.meta_names||{};
    return hypothesisNames.campaign||directionNames.campaign||variantNames.campaign||'';
  }

  function experimentAdSetName(experiment) {
    const hypothesis=experiment.hypothesis_json||{},direction=hypothesis.creative_direction||{};
    const hypothesisNames=hypothesis.meta_names||{},directionNames=direction.meta_names||{},variant=experiment.variant_definition_json||{},variantNames=variant.meta_names||{};
    return hypothesisNames.adset||directionNames.adset||variantNames.adset||experiment.source_adset_id||experiment.experiment_code||'未命名广告组';
  }

  function experimentTypeLabel(value) {
    return ({NEW_AD_TEST:'新广告测试',WINNER_EXTENSION:'优胜扩展',CREATIVE_REPAIR:'素材修复',CREATIVE_REPLACEMENT:'素材替换',BUDGET_SCALE_UP:'预算上调',BUDGET_REDUCTION:'预算下调',PAUSE_TEST:'暂停测试',REACTIVATION_TEST:'重新启用'}[value] || value || '实验');
  }

  async function openAdExperiment(id) {
    state.activeExperiment = id;
    if(isEmbeddedWorkspace())setWorkspaceReturn({kind:'embeddedQueue'});
    const node = document.getElementById('growthDetail');
    const previous=state.detail?.experiment?.experiment_id===id?state.detail:null;
    if(!previous)node.innerHTML = '<div class="growth-empty"><div><b>正在读取广告任务</b></div></div>';
    try {
      state.detail = await api(`/api/ops/ad-data-dashboard/experiments/${encodeURIComponent(id)}`);
      const sourceReportId = String(state.detail?.experiment?.source_report_id || '');
      if(!state.workspaceReturn)setWorkspaceReturn(sourceReportId.startsWith('newacct_') ? {kind:'launch',launchId:sourceReportId} : null);
      renderExperimentDetail(state.detail);
    } catch (error) {
      if(previous){state.detail=previous;renderExperimentDetail(previous);const notice=document.createElement('div');notice.className='growth-error';notice.textContent='服务更新期间暂时无法刷新，当前内容已保留。稍后点击刷新即可。';node.prepend(notice);}
      else node.innerHTML = `<div class="growth-error">${esc(readableError(error))}</div>`;
    }
  }

  function stageFor(experiment, evaluations) {
    const finalStates = ['EFFECTIVE','INEFFECTIVE','INCONCLUSIVE','DATA_INCOMPLETE','MIXED_CHANGE','PAUSED'];
    if (experiment.state === 'ARCHIVED') return 6;
    if (finalStates.includes(experiment.state) || evaluations.some(item => item.checkpoint === 'D7')) return 5;
    if (['RUNNING','MATURING','EVALUATING_ADJUSTMENT'].includes(experiment.state) || evaluations.length) return 4;
    if (['CREATING_PAUSED_OBJECTS','META_REVIEW_PENDING','READY_FOR_ACTIVATION'].includes(experiment.state)) return 3;
    if (['WAITING_CREATE_APPROVAL','WAITING_ADJUSTMENT_APPROVAL'].includes(experiment.state)) return 2;
    return 1;
  }

  function phaseCopy(experiment, evaluations, stage) {
    const latest = evaluations[evaluations.length - 1] || {};
    if ((experiment.hypothesis_json||{}).mode === 'passive_observation' && !latest.checkpoint) return {title:'系统观察中',hint:'当前广告表现已纳入只读跟踪；达到 D1 / D3 / D7 后自动形成对比。'};
    if (latest.checkpoint === 'D3') return {title:'D3 已完成 · 等待 D7',hint:'当前无需处理，系统继续观察至 D7。'};
    if (latest.checkpoint === 'D1') return {title:'D1 已完成 · 等待 D3',hint:'当前无需处理，系统自动观察至 D3。'};
    if (latest.checkpoint === 'D7') return {title:statusLabel(experiment.state),hint:'D7 数据已经形成；只有证据成熟或出现异常时才进入你的待办。'};
    if (stage === 1) return {title:'方案草稿',hint:'确认目标、唯一变量、护栏指标和观察周期。'};
    if (stage === 2) return {title:'等待确认',hint:'复核变更摘要后先进行 dry-run，不会直接写入 Meta。'};
    if (stage === 3) return {title:'演练与审核',hint:'核对执行计划和 Meta 审核状态。'};
    if (stage === 4) return {title:'投放观察',hint:'等待 D1、D3、D7 自动回读。'};
    return {title:statusLabel(experiment.state),hint:'查看证据成熟度和下一轮建议。'};
  }

  function metricRows(evaluation) {
    const before = evaluation?.baseline_metrics_json || {};
    const current = evaluation?.post_metrics_json || {};
    const definitions = [
      ['安装数','installs','number',true],['安装单价（CPI）','cpi','money',false],
      ['CTR','ctr','pct',true],['真实入会','real_bind_count','number',true],['入会单价（CPA）','real_bind_cpa','money',false]
    ];
    return definitions.map(([label,key,type,higherBetter]) => {
      const beforeValue = numericMetric(before, key);
      const currentValue = numericMetric(current, key);
      const delta = beforeValue == null || currentValue == null ? null : currentValue - beforeValue;
      const rate = delta == null || !beforeValue ? null : delta / beforeValue;
      const good = delta == null ? false : (higherBetter ? delta >= 0 : delta <= 0);
      return {label,before:formatMetric(beforeValue,type),current:formatMetric(currentValue,type),delta:formatDelta(delta,type),rate:rate == null ? '-' : `${rate>=0?'+':''}${(rate*100).toFixed(2)}%`,good};
    });
  }

  function numericMetric(source, key) {
    const fallback = key === 'real_bind_count' ? source.conversions : (key === 'real_bind_cpa' ? source.cpa : undefined);
    const value = source[key] == null ? fallback : source[key];
    return value == null || value === '' || Number.isNaN(Number(value)) ? null : Number(value);
  }

  function formatMetric(value, type) {
    if (value == null) return '-';
    if (type === 'money') return value.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
    if (type === 'pct') return `${(value*100).toFixed(2)}%`;
    if (type === 'hours') return `${value.toLocaleString('zh-CN',{maximumFractionDigits:0})} 小时`;
    return value.toLocaleString('zh-CN',{maximumFractionDigits:0});
  }

  function formatDelta(value, type) {
    if (value == null) return '-';
    const arrow = value > 0 ? '↑' : (value < 0 ? '↓' : '→');
    const absolute = Math.abs(value);
    return `${arrow} ${type==='pct'?(absolute*100).toFixed(2)+'pp':formatMetric(absolute,type)}`;
  }

  function renderExperimentDetail(payload) {
    setWorkspaceDetailMode(true);
    const experiment = payload.experiment || {};
    const timeline = (payload.timeline || {}).items || [];
    const evaluations = (payload.performance || {}).items || [];
    const lineage = payload.growth_lineage || {};
    const workflow = payload.workflow || {};
    const immediateAssessment = workflow.immediate_assessment || payload.latest_closed_loop?.immediate_assessment || {};
    const latest = evaluations[evaluations.length - 1] || null;
    const passiveObservation = workflow.passive_observation === true;
    const completedCheckpoints = workflow.completed_checkpoints || [];
    const stage = passiveObservation
      ? (workflow.bucket==='action_required'?6:workflow.bucket==='exception'?5:completedCheckpoints.includes('D7')?4:completedCheckpoints.includes('D3')?3:completedCheckpoints.includes('D1')?2:1)
      : stageFor(experiment, evaluations);
    let phase = workflow.bucket==='system_work'
      ? {title:'AI 自动处理中',hint:workflow.current_action||'系统正在按订单参数继续创建'}
      : phaseCopy(experiment, evaluations, stage);
    if(immediateAssessment.status==='NO_INTERVENTION_SUPPORTED')phase={title:'当前建议：继续观察',hint:immediateAssessment.summary||'历史数据未达到新增干预阈值'};
    if(immediateAssessment.status==='INTERVENTION_REVIEW_SUPPORTED')phase={title:'发现需要复核的干预候选',hint:immediateAssessment.summary||'历史数据触发护栏，需先生成方案并确认'};
    const maturity = Math.max(0,Math.min(100,Number(workflow.maturity_pct||0)));
    const latestPlanEvent = [...timeline].reverse().find(item => item.event_type === 'PLAN_PROPOSED') || {};
    const planId = String((latestPlanEvent.evidence_json || {}).plan_id || workflow.plan_id || '');
    const approval = planApproval(payload, planId);
    const rows = metricRows(latest);
    const immediateReady=['NO_INTERVENTION_SUPPORTED','INTERVENTION_REVIEW_SUPPORTED'].includes(String(immediateAssessment.status||''));
    const preDelivery = !passiveObservation && !latest && !immediateReady;
    const needsAction=workflow.bucket==='action_required'||workflow.bucket==='exception';
    const nextCheckpoint=workflow.next_checkpoint||(!latest?'D1':'');
    const creationIncident=workflow.bucket==='exception'&&(String(experiment.state||'')==='CREATION_PARTIAL_FAILURE'||(String(experiment.state||'')==='DATA_INCOMPLETE'&&String(workflow.plan_action_type||'')==='CREATE_PAUSED_AD'));
    const deliveryIncident=workflow.bucket==='exception'&&String(workflow.plan_action_type||'')==='REACTIVATE_AD';
    const metaReview=workflow.meta_review||{},metaRejected=String(metaReview.effective_status||'').toUpperCase()==='DISAPPROVED';
    const feedbackGroups=Object.values(metaReview.review_feedback_json||{}).flatMap(item=>Object.keys(item||{}));
    const rejectionReason=feedbackGroups.join('、')||'Meta 广告政策拒审';
    const node = document.getElementById('growthDetail');
    node.classList.toggle('has-autonomy-panel',Boolean(experiment.account_id&&!creationIncident));
    const drawerTitle=document.getElementById('growthDrawerTitle');if(drawerTitle)drawerTitle.textContent=experimentDrawerTitle(experiment);
    const drawerContext=document.getElementById('growthDrawerContext');if(drawerContext)drawerContext.textContent=`${experimentTitle(experiment)} · ${String(experiment.target_app||'Tugao').replace(/^./,char=>char.toUpperCase())} · ${experiment.country||'-'} · ${statusLabel(experiment.state)}`;
    node.innerHTML = creationIncident?`
      <section class="growth-status-panel"><small>整单创建异常</small><h3>系统已停止旧任务，当前不会产生花费</h3><p>这不是单条广告的数据问题。当前订单中的所有异常项共用同一次创建结果，只需处理一次。</p><div class="growth-status-next">系统已保留确认成功的对象；处理时会先自动核对，再只让你确认无法代替的外部条件。</div><div class="growth-status-actions">${planId?'<button type="button" class="growth-primary" id="growthOpenOrderIncident">处理整单异常</button>':'<button type="button" id="growthReloadOrderIncident">重新读取订单</button>'}</div></section>
      <details class="growth-technical"><summary>技术信息与过程记录</summary><div class="growth-timeline">${timeline.length?timeline.map(item=>`<div><b>${esc(statusLabel(item.to_state)||item.event_type)}</b><br>${esc(item.created_at)} · ${esc(item.actor||'系统')}</div>`).join(''):'<div>暂无变更记录</div>'}</div></details>`:deliveryIncident?deliveryIncidentHtml(experiment,workflow,timeline):`
      ${metaRejected?`<section class="growth-status-panel growth-status-rejected"><small>Meta 审核结果</small><h3>素材已被拒，原广告不会继续配送</h3><p>${esc(rejectionReason)}</p><div class="growth-status-next">${esc(workflow.current_action||'AI 正在生成合规替代素材')}。原素材和拒审证据会保留；替代素材通过安全检查后才会生成送审 Plan。</div></section>`:''}
      ${preDelivery?preDeliveryStatusHtml(experiment,stage,latest,planId,approval,lineage,workflow,phase):`
        <section class="growth-phase"><small>${needsAction?'需要处理':'当前状态'}</small><h3>${esc(phase.title)}</h3><p>${esc(needsAction?(workflow.current_action||phase.hint):`无需处理${nextCheckpoint?` · 系统将在 ${nextCheckpoint} 自动回读`:''}`)}</p></section>
        ${immediateAssessmentHtml(immediateAssessment)}
        <section class="growth-section"><h3>${latest?'广告表现':'投放数据'}</h3>${latest?`<table class="growth-metric-table"><thead><tr><th>指标</th><th>投放前</th><th>${esc(latest.checkpoint)}</th><th>变化</th><th>变化率</th></tr></thead><tbody>${rows.map(row=>`<tr><td>${row.label}</td><td>${row.before}</td><td>${row.current}</td><td class="${row.good?'growth-good':''}">${row.delta}</td><td class="${row.good?'growth-good':''}">${row.rate}</td></tr>`).join('')}</tbody></table>`:observationSnapshotHtml(workflow)}</section>
        <section class="growth-section"><h3>观察进度</h3><div class="growth-progress"><i style="width:${maturity}%"></i></div><div class="growth-progress-copy"><span>${completedCheckpoints.length?`${esc(completedCheckpoints.join('、'))} 已回读`:'等待首轮数据'}</span><b>${maturity}%</b><span>${workflow.evidence_mature?'可以形成结论':`${esc(nextCheckpoint||'下一检查点')} 自动更新`}</span></div></section>
        <div class="growth-actions">${primaryActionHtml(experiment,stage,latest,planId,approval,lineage,workflow)}</div>
        <div id="growthActionArea">${planControlsHtml(experiment,planId,approval,lineage,workflow)}</div>`}
      ${experiment.account_id?'<section class="growth-section" id="growthAutonomyPanel"><h3>AI 下一步</h3><div class="growth-review-card"><b>正在读取账户策略</b><span>系统会把观察结论转换成明确经营动作。</span></div></section>':''}
      <details class="growth-technical"><summary>技术信息与过程记录</summary>${deliveryPathHtml(experiment,workflow)}<div class="growth-timeline">${timeline.length?timeline.map(item=>`<div><b>${esc(statusLabel(item.to_state)||item.event_type)}</b><br>${esc(item.created_at)} · ${esc(item.actor||'系统')}</div>`).join(''):'<div>暂无变更记录</div>'}</div></details>`;
    bindExperimentActions(experiment,planId,approval,payload);
    document.getElementById('growthOpenOrderIncident')?.addEventListener('click',()=>openLaunchBatchWorkflow(planId));
    document.getElementById('growthReloadOrderIncident')?.addEventListener('click',()=>openAdExperiment(experiment.experiment_id));
    document.getElementById('growthReconcileDeliveryIncident')?.addEventListener('click',event=>reconcileDeliveryIncident(experiment,event.currentTarget));
    if(experiment.account_id&&!creationIncident)loadAutonomyPanel(experiment,payload);
  }

  function immediateAssessmentHtml(assessment) {
    if(!assessment||!assessment.status||assessment.status==='DATA_NOT_READY')return '';
    const window=assessment.source_window||{},count=Number(window.observed_day_count||0),candidateCount=(assessment.action_candidates||[]).length;
    const title=assessment.status==='NO_INTERVENTION_SUPPORTED'?'现在不用再停广告':assessment.status==='INTERVENTION_REVIEW_SUPPORTED'?`发现 ${candidateCount} 条暂停候选`:'历史证据需要复核';
    const timing=count?`${esc(window.start||'-')} 至 ${esc(window.end||'-')} · ${count} 个共同完整日`:'暂停前历史窗口';
    return `<section class="growth-section growth-immediate-assessment"><h3>立即经营判断</h3><div class="growth-review-card"><b>${esc(title)}</b><span>${esc(assessment.summary||'')}</span><small>${timing}</small><small>这不是暂停后的效果评价；D1 / D3 / D7 仍会继续回读真实结果。</small></div></section>`;
  }

  function deliveryIncidentCause(workflow) {
    const step=String(workflow.execution_failed_step||'').toUpperCase();
    const raw=String(workflow.execution_error_message||workflow.execution_error_code||'').toLowerCase();
    const location={CAMPAIGN_STATUS_UPDATE:'开启广告系列',ADSET_STATUS_UPDATE:'开启广告组',AD_STATUS_UPDATE:'开启广告'}[step]||'开启投放';
    const reason=raw.includes('2500')?'Meta 连接在返回开关结果前发生异常，系统无法确认这一步是否成功。':'Meta 没有返回可确认的开关结果，系统已停止后续步骤。';
    return {location,reason};
  }

  function deliveryIncidentHtml(experiment,workflow,timeline) {
    const cause=deliveryIncidentCause(workflow);
    const recent=timeline.slice(-5).reverse();
    return `
      <section class="growth-status-panel growth-incident-panel" role="alert" aria-label="投放开启异常">
        <small>投放开启异常</small>
        <h3>${esc(cause.location)}时没有获得 Meta 的确认结果</h3>
        <p>${esc(cause.reason)}</p>
        <div class="growth-review-card"><b>异常发生在哪里</b><span>${esc(cause.location)}。广告组和广告没有继续开启，系统也没有自动重试。</span></div>
        <div class="growth-review-card"><b>你现在可以做什么</b><span>直接重新读取广告系列、广告组和广告的真实状态。若三层都为暂停，系统会关闭本次异常，并在原订单里恢复“开启广告”入口。</span></div>
        <div id="growthIncidentResult" class="growth-notice" hidden></div>
        <div class="growth-status-actions"><button type="button" class="growth-primary" id="growthReconcileDeliveryIncident">重新核对 Meta 状态</button></div>
        <div class="growth-safety">这一步只读取 Meta，不会重新提交旧操作，也不会产生广告花费。</div>
      </section>
      <details class="growth-technical"><summary>技术信息与过程记录</summary><div class="growth-timeline">${recent.map(item=>`<div><b>${esc(statusLabel(item.to_state)||'状态变化')}</b><br>${esc(item.created_at||'')} · ${esc(item.actor||'系统')}</div>`).join('')||'<div>暂无技术记录</div>'}</div></details>`;
  }

  async function reconcileDeliveryIncident(experiment,button) {
    const resultNode=document.getElementById('growthIncidentResult');
    try {
      button.disabled=true;button.textContent='正在读取 Meta…';
      if(resultNode){resultNode.hidden=false;resultNode.textContent='正在核对广告系列、广告组和广告的真实状态。';}
      const result=await api(`/api/ops/ad-data-dashboard/experiments/${encodeURIComponent(experiment.experiment_id)}/delivery-incident/reconcile`,{
        method:'POST',
        headers:postHeaders('delivery-incident-reconcile',{experiment_id:experiment.experiment_id,checked_at:new Date().toISOString()}),
        body:JSON.stringify({}),
      });
      if(result.safe_to_retry){
        if(resultNode)resultNode.textContent='已确认：广告系列、广告组和广告均为暂停。旧操作未重放，现在可以重新开启广告。';
        await openAdExperiment(experiment.experiment_id);return;
      }
      const objects=(result.objects||[]).map(item=>`${item.label}：${statusLabel(item.status||item.effective_status)}`).join('；');
      if(resultNode)resultNode.innerHTML=`<b>已读取真实状态，但暂不允许重试</b><br>${esc(objects||'对象状态不完整')}<br>系统不会覆盖现状；请按上面显示的具体层级继续处理。`;
      button.disabled=false;button.textContent='再次核对 Meta 状态';
    } catch(error) {
      if(resultNode){resultNode.hidden=false;resultNode.textContent=`核对失败：${readableError(error)}。旧操作没有重放，请稍后再次核对。`;}
      button.disabled=false;button.textContent='重新核对 Meta 状态';
    }
  }

  const AUTONOMY_ACTION_LABELS={OBSERVE:'继续观察',CHECK_DATA:'修复数据',CREATE_NEXT_TEST:'创建下一轮实验',ADD_PAUSED_ADSET:'增加暂停态广告组',COPY_SCALE:'复制扩量',REPLACE_CREATIVE:'更换素材',COPY_TEST:'文案实验',INCREASE_BUDGET:'增加预算',DECREASE_BUDGET:'降低预算',PAUSE_AD:'暂停广告',REACTIVATE_AD:'重新启用'};

  async function loadAutonomyPanel(experiment,payload) {
    const node=document.getElementById('growthAutonomyPanel');if(!node)return;
    try{
      const account=String(experiment.account_id||'').replace(/^act_/,''),launch=String(experiment.source_report_id||'');
      const [catalog,queue]=await Promise.all([
        api(`/api/ops/ad-data-dashboard/autonomy/${encodeURIComponent(account)}`),
        api(`/api/ops/ad-data-dashboard/next-actions?account_id=${encodeURIComponent(account)}&limit=100`),
      ]);
      const actions=(queue.items||[]).filter(item=>String(item.experiment_id||'')===String(experiment.experiment_id||'')||(launch&&String(item.launch_id||'')===launch));
      const immediate=payload.latest_closed_loop?.immediate_assessment||payload.workflow?.immediate_assessment||{};
      const level=String(catalog.policy?.level||'L0_OBSERVE'),levelText={L0_OBSERVE:'只观察',L1_RECOMMEND:'生成建议',L2_PAUSED_CREATE:'可创建暂停态对象',L3_BOUNDED_LIVE:'边界内执行'}[level]||level;
      const rows=actions.slice(0,3).map(item=>`<div class="growth-delivery-row"><span><b>${esc(AUTONOMY_ACTION_LABELS[item.action_type]||item.action_type)}</b><small>${esc(item.summary||'')}</small></span><strong>${esc(item.status==='BLOCKED'?(item.block_reason||'暂不可执行'):(item.status==='APPROVAL_REQUIRED'?'等待确认':'已就绪'))}</strong></div>`).join('');
      const immediateTitle=immediate.status==='NO_INTERVENTION_SUPPORTED'?'继续观察，暂不干预':immediate.status==='INTERVENTION_REVIEW_SUPPORTED'?'复核暂停候选':'等待下一检查点';
      const immediateCopy=immediate.summary?`<span>${esc(immediate.summary)}</span>`:'<span>系统将在 D1 / D3 / D7 自动读取数据并生成下一步。</span>';
      node.innerHTML=`<h3>AI 下一步</h3><div class="growth-review-card"><b>${actions.length?esc(AUTONOMY_ACTION_LABELS[actions[0].action_type]||actions[0].action_type):esc(immediateTitle)}</b><span>账户放权：${esc(levelText)}。只有可能产生花费或修改投放的动作才需要你确认。</span>${rows||immediateCopy}</div><div class="growth-autonomy-actions"><button type="button" id="growthCreateNextTest">创建下一轮</button><button type="button" id="growthShowCapabilities">增加组 / 扩量 / 换素材 / 文案实验</button></div>`;
      document.getElementById('growthCreateNextTest')?.addEventListener('click',()=>{closeWorkspace();openLaunchWorkspace({startCreate:true});});
      document.getElementById('growthShowCapabilities')?.addEventListener('click',()=>openCapabilityCatalog(catalog,experiment,payload));
    }catch(error){node.innerHTML=`<h3>AI 下一步</h3><div class="growth-error">${esc(readableError(error))}</div>`;}
  }

  function openCapabilityCatalog(catalog,experiment,payload){
    const groups=(catalog.groups||[]).map(group=>`<section class="growth-review-card"><b>${esc(group.label)}</b>${(group.actions||[]).map(item=>`<div class="growth-delivery-row"><span>${esc(AUTONOMY_ACTION_LABELS[item.action_type]||item.action_type)}</span><strong>${item.authorized?'可提交确认':'暂未放权'}</strong></div>`).join('')}</section>`).join('');
    showModal(`<section class="growth-modal growth-modal-compact"><header class="growth-modal-head"><div><b>经营动作</b><small>${esc(experimentDrawerTitle(experiment))}</small></div><button type="button" class="growth-icon-button" data-modal-close>×</button></header><div class="growth-modal-body"><p>系统会根据 D1 / D3 / D7 结果推荐动作；增加组、扩量、换素材和文案实验从这里统一进入。</p>${groups}<div class="growth-safety">当前页面不会自动写入 Meta。未达到安装、真实入会和 CPA 门槛时，强动作会被阻止。</div></div><footer class="growth-modal-foot"><button type="button" data-modal-close>关闭</button><button type="button" class="growth-primary" id="growthCapabilityNewTest">创建单变量实验</button></footer></section>`);
    document.getElementById('growthCapabilityNewTest')?.addEventListener('click',()=>{closeModal();closeWorkspace();openLaunchWorkspace({startCreate:true});});
  }

  function deliveryPathHtml(experiment,workflow) {
    if(!experiment.source_campaign_id&&!experiment.source_adset_id&&!experiment.source_ad_id)return '';
    const state=String(experiment.state||'').toUpperCase(),action=String((workflow||{}).plan_action_type||'').toUpperCase();
    const running=['RUNNING','MATURING','RECOMMENDATION_READY','EVALUATING_ADJUSTMENT','EFFECTIVE','INEFFECTIVE','INCONCLUSIVE'].includes(state);
    const pending=state==='WAITING_ADJUSTMENT_APPROVAL'||state==='ADJUSTING';
    const status=(kind)=>{
      if(pending)return action==='REACTIVATE_AD'?'待启用':action.startsWith('PAUSE')?(kind==='ad'?'待暂停':'保持投放'):'变更中';
      if(running)return '投放中';
      if(state==='PAUSED')return kind==='ad'?'已暂停':'保持当前状态';
      return '已暂停';
    };
    const rows=[['广告系列',experiment.source_campaign_id,status('campaign')],['广告组',experiment.source_adset_id,status('adset')],['广告',experiment.source_ad_id,status('ad')]];
    return `<section class="growth-section growth-delivery-path"><h3>投放开关</h3><div class="growth-review-card"><b>${running?'当前正在投放':state==='PAUSED'?'当前已停止这条广告':'当前不会产生花费'}</b><span>${running?'暂停只作用于这条广告，不影响同系列下的其他实验。':'启用时会同时开启广告系列、对应广告组和广告，缺一层都不会开始投放。'}</span>${rows.map(row=>`<div class="growth-delivery-row"><span>${esc(row[0])}<small>${esc(row[1]||'待回读')}</small></span><strong>${esc(row[2])}</strong></div>`).join('')}</div></section>`;
  }

  function preDeliveryStatusHtml(experiment,stage,latest,planId,approval,lineage,workflow,phase) {
    const queued=workflow.execution_task_id&&workflow.execution_status==='QUEUED'&&Number(workflow.receipt_count||0)===0;
    const unavailable=queued&&!workflow.live_execution_available;
    const title=unavailable?'广告尚未创建':(queued?'等待系统开始创建':phase.title);
    const summary=unavailable?'创建确认已记录，当前 Meta 写入 0。':(queued?'系统收到创建任务，尚未产生 Meta 写入。':(workflow.current_action||phase.hint));
    const next=unavailable?'下一步：完成真实创建通道配置后，系统按原确认内容创建并回读暂停态广告。':'投放数据会在广告创建并开始投放后显示。';
    return `<section class="growth-status-panel"><small>当前状态</small><h3>${esc(title)}</h3><p>${esc(summary)}</p><div class="growth-status-next">${esc(next)}</div><div class="growth-status-actions">${primaryActionHtml(experiment,stage,latest,planId,approval,lineage,workflow)}</div></section>`;
  }

  function observationSnapshotHtml(workflow) {
    const snapshot=workflow.observation_snapshot||{},metrics=snapshot.metrics||{};
    if(!workflow.passive_observation)return '<div class="growth-review-card"><b>指标将在投放后出现</b>当前只保存方案、确认记录与 dry-run 结果。</div>';
    const values=[['安装数',formatMetric(numericMetric(metrics,'installs'),'number')],['安装单价（CPI）',formatMetric(numericMetric(metrics,'cpi'),'money')],['CTR',formatMetric(numericMetric(metrics,'ctr'),'pct')],['真实入会',formatMetric(numericMetric(metrics,'real_bind_count'),'number')],['入会单价（CPA）',formatMetric(numericMetric(metrics,'real_bind_cpa'),'money')]];
    const labels={installs:'安装数',cpi:'安装单价（CPI）',ctr:'CTR',real_joins:'真实入会',real_join_cpa:'入会单价（CPA）'},states={unready:'未成熟',initial:'可初读',strong:'可强判',high_confidence:'高置信'};
    const maturity=Object.entries(snapshot.maturity||{}).filter(([key])=>labels[key]).map(([key,item])=>`<span><small>${esc(labels[key])}</small><strong>${esc(item.value??0)} / ${esc(item.strong_threshold??'-')}</strong><small>${esc(states[item.state]||item.state||'-')}</small></span>`);
    const score=snapshot.score==null?'-':Number(snapshot.score).toFixed(1);
    return `<div class="growth-review-card"><b>当前广告表现 · ${esc(snapshot.report_date||'最新报告')}</b><div class="growth-observation-metrics">${values.map(item=>`<span><small>${esc(item[0])}</small><strong>${esc(item[1])}</strong></span>`).join('')}</div><div class="growth-observation-score"><strong>${esc(score)}</strong><span>v4.2 评分 · ${esc(snapshot.band_zh||'数据不足')}<br>判断把握：${esc(snapshot.confidence||'low')}</span></div>${maturity.length?`<div class="growth-observation-maturity">${maturity.join('')}</div>`:''}<small class="growth-observation-source">基准 ${esc(snapshot.benchmark_version||'-')}<br>阈值来源 ${esc(snapshot.threshold_source||'-')}<br>事实快照不代表因果结论。</small></div>`;
  }

  function workflowSummaryHtml(workflow) {
    const blockers=(workflow.blockers||[]).map(item=>({missing_decision:'缺少系统建议来源',missing_account:'目标广告账户待确认',plan_not_ready:'执行方案尚未生成',data_quality_not_passed:'数据质量未通过',manual_review_required:'需要人工核对不确定结果',meta_live_execution_unavailable:'真实创建通道尚未开启'}[item]||item));
    return `<section class="growth-section"><h3>这一步需要什么</h3><div class="growth-review-card"><b>${esc(workflow.current_action||'查看当前任务')}</b>${blockers.length?`<span>开始前还需：${esc(blockers.join('、'))}</span>`:'<span>前置条件已齐备。</span>'}</div></section>`;
  }

  function receiptSummaryHtml(workflow) {
    if(!workflow.plan_id&&!workflow.execution_task_id)return '';
    const status=workflow.execution_status||workflow.approval_status||'计划已生成';
    const receiptCount=Number(workflow.receipt_count||0)+Number(workflow.dry_run_receipt_count||0);
    const queued=workflow.execution_task_id&&status==='QUEUED'&&Number(workflow.receipt_count||0)===0;
    const summary=queued?'你的创建确认已记录，但系统尚未开始创建，Meta 写入 0。':`已记录 ${esc(receiptCount)} 条检查记录`;
    return `<section class="growth-section"><h3>创建状态</h3><div class="growth-review-card"><b>${esc(workflow.dry_run_verified&&!workflow.execution_task_id?'安全检查已通过':statusLabel(status))}</b><span>${summary}</span><button type="button" id="growthOpenReceipts">${queued?'查看原因和下一步':'查看创建进度'}</button></div></section>`;
  }

  function planApproval(payload, planId) {
    if (!planId) return {};
    const actions = (((payload.growth_lineage||{}).episode_detail||{}).actions || []);
    const action = actions.find(item => item.operation_action_id === planId) || {};
    return action.approval || (action.payload_json||{}).approval || {};
  }

  function primaryActionHtml(experiment, stage, latest, planId, approval, lineage, workflow) {
    if ((workflow||{}).passive_observation && (workflow||{}).bucket==='observing') return `<span class="growth-no-action">当前无需处理 · ${workflow.next_checkpoint?`系统观察至 ${esc(workflow.next_checkpoint)}`:'系统继续积累证据'}</span>${latest?`<button type="button" id="growthReviewCheckpoint">查看 ${esc(latest.checkpoint)} 阶段记录</button>`:''}`;
    if ((workflow||{}).bucket === 'system_work') return `<span class="growth-no-action">无需操作 · ${esc(workflow.current_action||'AI 正在按订单参数自动继续')}</span>`;
    if ((workflow||{}).bucket === 'exception' && (workflow||{}).plan_action_type === 'CREATE_PAUSED_AD') return '<span class="growth-no-action">无需操作 · AI 已停止重复写入，系统正在处理创建异常</span>';
    if ((workflow||{}).bucket === 'exception') return '<button type="button" class="growth-primary" id="growthReviewException">查看异常原因和处理入口</button>';
    if (['META_REVIEW_PENDING','READY_FOR_ACTIVATION','PAUSED','WAITING_ADJUSTMENT_APPROVAL'].includes(experiment.state) && (!(workflow||{}).execution_task_id) && (!((workflow||{}).plan_action_type)||((workflow||{}).plan_action_type)==='REACTIVATE_AD')) return '<button type="button" class="growth-primary" id="growthActivateNow">开启广告</button>';
    if (['RUNNING','MATURING'].includes(experiment.state) && !['PAUSE_AD','PAUSE_ADSET'].includes((workflow||{}).plan_action_type)) return '<button type="button" class="growth-danger" id="growthPreparePause">暂停这条广告</button>';
    if (planId && approval.status === 'PROPOSED') return '<button type="button" class="growth-primary" id="growthConfirmPlan">复核并确认计划</button>';
    if ((workflow||{}).execution_task_id) return '<button type="button" class="growth-primary" id="growthOpenReceiptsPrimary">查看原因和下一步</button>';
    if (planId && (workflow||{}).dry_run_verified && (workflow||{}).plan_action_type === 'CREATE_PAUSED_AD') return '<button type="button" class="growth-primary" id="growthSubmitPausedCreation">下一步：创建暂停态广告</button>';
    if (planId && (workflow||{}).dry_run_verified && (workflow||{}).plan_action_type === 'REACTIVATE_AD') return '<button type="button" class="growth-primary" id="growthEnableDelivery">确认启用投放</button>';
    if (planId && (workflow||{}).dry_run_verified && ['PAUSE_AD','PAUSE_ADSET'].includes((workflow||{}).plan_action_type)) return '<button type="button" class="growth-danger" id="growthPauseDelivery">确认暂停投放</button>';
    if (planId) return '<button type="button" class="growth-primary" id="adExperimentDryRun">执行 dry-run 演练</button>';
    const decision=(((lineage||{}).episode_detail||{}).decision)||{};
    if ((stage === 1 || stage === 2) && decision.decision_id) return '<button type="button" class="growth-primary" id="growthPreparePlan">检查并准备方案</button>';
    if (stage === 1 || stage === 2) return '<button type="button" id="growthReturnToRecommendation">关联系统建议后继续</button>';
    if (latest) return `<button type="button" class="growth-primary" id="growthReviewCheckpoint">查看 ${esc(latest.checkpoint)} 复盘</button>`;
    return '<button type="button" disabled>等待实验数据</button>';
  }

  function planControlsHtml(experiment, planId, approval, lineage, workflow) {
    const growth = (lineage||{}).episode_detail || {}, decision = growth.decision || {};
    if ((workflow||{}).bucket === 'system_work') return '';
    if ((workflow||{}).passive_observation) return '';
    if (!decision.decision_id) return '<div class="growth-notice">尚未关联系统建议。</div>';
    if ((workflow||{}).execution_task_id) {
      if ((workflow||{}).execution_status==='QUEUED'&&Number((workflow||{}).receipt_count||0)===0&&!((workflow||{}).live_execution_available)) return '<div class="growth-notice">创建通道未开启，广告尚未创建。</div>';
      return '';
    }
    if (planId) return '<div id="adExperimentControlStatus" class="growth-notice" hidden></div>';
    return '';
  }

  function bindExperimentActions(experiment, planId, approval, payload) {
    const showTimeline = document.getElementById('growthShowTimeline');
    if (showTimeline) showTimeline.addEventListener('click', () => { const details=document.querySelector('.growth-technical'); details.open=!details.open; });
    const prepare = document.getElementById('growthPreparePlan');
    if (prepare) prepare.addEventListener('click', () => openPlanReadiness(experiment,payload));
    const returnToRecommendation = document.getElementById('growthReturnToRecommendation');
    if (returnToRecommendation) returnToRecommendation.addEventListener('click', closeWorkspace);
    const preview = document.getElementById('adExperimentPreviewPlan');
    if (preview) preview.addEventListener('click', () => previewPlan(experiment,payload,preview));
    const confirm = document.getElementById('growthConfirmPlan');
    if (confirm) confirm.addEventListener('click', () => openPlanConfirmation(experiment,planId));
    const dryRun = document.getElementById('adExperimentDryRun');
    if (dryRun) dryRun.addEventListener('click', () => executeDryRun(experiment,planId,dryRun));
    const review = document.getElementById('growthReviewCheckpoint');
    if (review) review.addEventListener('click', () => openCheckpointReview(experiment,payload));
    const receipts = document.getElementById('growthOpenReceipts');
    if (receipts) receipts.addEventListener('click', () => openPlanReceipts(experiment,planId));
    const receiptsPrimary = document.getElementById('growthOpenReceiptsPrimary');
    if (receiptsPrimary) receiptsPrimary.addEventListener('click', () => openPlanReceipts(experiment,planId));
    const submitPaused = document.getElementById('growthSubmitPausedCreation');
    if (submitPaused) submitPaused.addEventListener('click', () => openPausedCreationConfirmation(experiment,planId));
    const exception = document.getElementById('growthReviewException');
    if (exception) exception.addEventListener('click', () => openExceptionReview(experiment,payload,planId));
    const activation = document.getElementById('growthActivateNow');
    if (activation) activation.addEventListener('click', () => openActivationConfirmation(experiment,payload));
    const preparePause = document.getElementById('growthPreparePause');
    if (preparePause) preparePause.addEventListener('click', () => openPausePlan(experiment,payload));
    const enableDelivery = document.getElementById('growthEnableDelivery');
    if (enableDelivery) enableDelivery.addEventListener('click', () => openDeliveryExecutionConfirmation(experiment,planId,'ENABLE'));
    const pauseDelivery = document.getElementById('growthPauseDelivery');
    if (pauseDelivery) pauseDelivery.addEventListener('click', () => openDeliveryExecutionConfirmation(experiment,planId,'PAUSE'));
  }

  async function previewPlan(experiment,payload,button) {
    const growth=(payload.growth_lineage||{}).episode_detail||{},decision=growth.decision||{},episode=growth.episode||{};
    const status=document.getElementById('adExperimentControlStatus');
    let before,after,steps;
    try { before=JSON.parse(document.getElementById('adExperimentBefore').value);after=JSON.parse(document.getElementById('adExperimentAfter').value);steps=JSON.parse(document.getElementById('adExperimentSteps').value); }
    catch (_) { status.hidden=false;status.textContent='Before / After / Steps 必须是合法 JSON。';return; }
    const actionType=document.getElementById('adExperimentAction').value,target=document.getElementById('adExperimentTarget').value.trim();
    const body={decision_id:decision.decision_id,episode_id:episode.episode_id||'',action_type:actionType,target_account_id:experiment.account_id,target_object_type:actionType==='PAUSE_ADSET'?'ADSET':'AD',target_object_id:target,before_json:before,after_json:after,steps,creative:after.creative||{},max_write_requests:actionType==='CREATE_PAUSED_AD'?5:(actionType==='REPLACE_CREATIVE'?2:1),evaluation_window:{checkpoints:['D1','D3','D7']}};
    try { button.disabled=true;status.hidden=false;status.textContent='正在固化计划和内容哈希…';const kind=actionType==='CREATE_PAUSED_AD'?'create-plan':'adjustment-plan';await api(`/api/ops/ad-data-dashboard/experiments/${encodeURIComponent(experiment.experiment_id)}/${kind}/preview`,{method:'POST',headers:postHeaders('ad-plan-preview',{experiment_id:experiment.experiment_id,...body}),body:JSON.stringify(body)});await openAdExperiment(experiment.experiment_id); }
    catch(error){status.hidden=false;status.textContent=readableError(error);button.disabled=false;}
  }

  function decisionPlanAction(decision,experiment) {
    const selected=String(decision.selected_action||'').toUpperCase();
    if(selected==='PAUSE')return 'PAUSE_AD';
    if(selected==='SCALE_UP')return 'INCREASE_BUDGET';
    if(selected==='REDUCE_BUDGET')return 'DECREASE_BUDGET';
    if(selected==='CREATE_EXPERIMENT'||selected==='CREATE_PAUSED_AD')return 'CREATE_PAUSED_AD';
    return String(experiment.experiment_type||'')==='REACTIVATION_TEST'?'REACTIVATE_AD':'';
  }

  function openPlanReadiness(experiment,payload) {
    const growth=(payload.growth_lineage||{}).episode_detail||{},decision=growth.decision||{},episode=growth.episode||{};
    const actionType=decisionPlanAction(decision,experiment),isBudget=['INCREASE_BUDGET','DECREASE_BUDGET'].includes(actionType);
    const target=actionType.includes('BUDGET')?(experiment.source_adset_id||experiment.source_ad_id):experiment.source_ad_id;
    const hypothesis=experiment.hypothesis_json||{},variant=experiment.variant_definition_json||{},metaNames=hypothesis.meta_names||variant.meta_names||{};
    const initialCostCapUsd=Number(hypothesis.cpi_target||variant.cpi_target||0);
    const targeting=hypothesis.targeting||variant.targeting||hypothesis.audience||{},direction=hypothesis.creative_direction||variant.creative_direction||{};
    const approvedEvidence=payload.approved_creative||{},creativeReady=actionType==='CREATE_PAUSED_AD'&&Boolean(approvedEvidence.image_id);
    const blockers=[];if(!experiment.account_id)blockers.push('目标广告账户');if(actionType!=='CREATE_PAUSED_AD'&&!target)blockers.push('目标广告对象');if(!actionType)blockers.push('可执行动作');
    if(actionType==='CREATE_PAUSED_AD'&&!creativeReady)blockers.push('已审核通过的最终素材');
    const audience=[experiment.country,targeting.gender||'不限',`${targeting.age_min||18}-${targeting.age_max||65} 岁`,targeting.language||'-'].join(' · ');
    const defaultCopy={BR:['Descubra tarefas simples no Tugao e acompanhe seu progresso no app.','Comece no Tugao','Veja as atividades disponíveis no app.'],MX:['Descubre tareas simples en Tugao y sigue tu progreso en la app.','Empieza en Tugao','Consulta las actividades disponibles en la app.'],ID:['Temukan tugas sederhana di Tugao dan pantau progresmu di aplikasi.','Mulai di Tugao','Lihat aktivitas yang tersedia di aplikasi.']}[String(experiment.country||'BR')]||['Explore Tugao and track your progress in the app.','Get started with Tugao','See available activities in the app.'];
    const createForm=actionType==='CREATE_PAUSED_AD'?`<div class="growth-structure-head"><span>广告系列</span><span>广告组</span><span>广告</span></div><div class="growth-structure-layout"><section class="growth-structure-card is-campaign"><header><b>共用广告系列</b><span>暂停</span></header><div class="growth-structure-fields"><label>广告系列名称<input id="growthCampaignName" maxlength="80" value="${esc(metaNames.campaign||`TG_${experiment.country||'BR'}_INS_CS`)}"></label></div><div class="growth-structure-facts"><span>目标：App 安装</span><span>预算：广告组预算（ABO）</span><span>购买：竞价</span></div></section><div class="growth-structure-rows"><div class="growth-structure-row"><section class="growth-structure-card"><header><b>${esc(direction.title||hypothesis.creative_angle||'广告组')}</b><span>受众与预算</span></header><div class="growth-structure-fields"><label>广告组名称<input id="growthAdsetName" maxlength="80" value="${esc(metaNames.adset||`${experiment.country||'BR'}_CS`)}"></label><label>每日预算（美元）<input id="growthAdsetBudget" type="number" min="5" max="100" step="1" value="${esc(String(variant.initial_daily_budget||hypothesis.initial_daily_budget||20))}"></label><label>CPI 成本上限（美元 / 安装）<input id="growthCostCapUsd" type="number" min="0.01" step="0.01" value="${initialCostCapUsd>0?esc(initialCostCapUsd.toFixed(2)):''}" placeholder="请确认本轮止损上限"></label><label>版位<select id="growthPlacement"><option value="ADVANTAGE_PLUS">Advantage+ 自动版位</option><option value="FEEDS_ONLY">仅信息流</option></select></label></div><div class="growth-structure-facts"><span><b>受众</b> ${esc(audience)}</span><span>App 安装 · 成本上限 · 展示计费 · 点击 7 天 / 浏览 1 天</span></div></section><section class="growth-structure-card"><header><b>${esc(metaNames.ad||experiment.experiment_code||'广告')}</b><span>素材与文案</span></header><div class="growth-structure-fields"><label>广告名称<input id="growthAdName" maxlength="80" value="${esc(metaNames.ad||experiment.experiment_code||'Tugao ad')}"></label><label>主要文案<textarea id="growthPrimaryText" maxlength="500">${esc(defaultCopy[0])}</textarea></label><label>标题<input id="growthHeadline" maxlength="80" value="${esc(defaultCopy[1])}"></label><label>描述<input id="growthDescription" maxlength="120" value="${esc(defaultCopy[2])}"></label><label>行动号召<select id="growthCta"><option value="INSTALL_MOBILE_APP">立即安装</option></select></label></div><div class="growth-structure-facts"><span>公共主页：${esc(String(hypothesis.page_id||'-'))}</span><span>审核素材：${esc(String(approvedEvidence.image_id||'-').slice(-12))}</span></div></section></div></div></div>`:'';
    const form=isBudget?`<div class="growth-form"><label>当前预算<input id="growthBeforeBudget" type="number" min="0.01" step="0.01" placeholder="由当前广告配置确认"></label><label>计划预算<input id="growthAfterBudget" type="number" min="0.01" step="0.01" placeholder="系统建议值"></label></div>`:createForm;
    const creativeSummary=creativeReady?`<div class="growth-review-card"><b>已审核素材</b>${esc(metaNames.ad||experiment.experiment_code||'最终素材')} · 图片 ${esc(String(approvedEvidence.image_id).slice(-8))}<br>广告系列 ${esc(metaNames.campaign||'-')} · 广告组 ${esc(metaNames.adset||'-')}</div>`:'';
    showModal(`<section class="growth-modal ${actionType==='CREATE_PAUSED_AD'?'growth-batch-modal':'growth-plan-modal'}"><header class="growth-modal-head"><div><b>${actionType==='CREATE_PAUSED_AD'?'确认广告结构':'执行方案准备'}</b><small>${esc(experimentTitle(experiment))}</small></div><button type="button" class="growth-icon-button" data-modal-close>×</button></header><div class="growth-modal-body">${actionType==='CREATE_PAUSED_AD'?`<div class="growth-plan-context"><span><b>${esc(experiment.country||'-')} · ${esc(experiment.account_id||'-')}</b></span><span>确认后只保存方案，不会启用广告</span></div>`:`<h3>${esc(actionType||'动作待确认')}</h3><p>系统已关联建议、证据和实验；这里只补齐方案所需的业务要素。</p>${creativeSummary}`}${form}${blockers.length?`<div class="growth-safety">还需要：${esc(blockers.join('、'))}。${actionType==='CREATE_PAUSED_AD'?'请先完成素材审核；素材审核通过不等于广告已经创建。':''}</div>`:'<div class="growth-safety">确认后保存本次方案；名称、预算、受众、文案或素材变化时需要重新确认。</div>'}</div><footer class="growth-modal-foot"><button type="button" data-modal-close>返回</button><button type="button" class="growth-primary" id="growthCreateBusinessPlan" ${blockers.length?'disabled':''}>${actionType==='CREATE_PAUSED_AD'?'确认参数并生成 Plan':'生成不可变 Plan'}</button></footer></section>`);
    const button=document.getElementById('growthCreateBusinessPlan');
    if(button&&!blockers.length)button.addEventListener('click',async()=>{
      const before={status:'ACTIVE'},after={},steps={};
      if(actionType==='PAUSE_AD'){after.status='PAUSED';steps.STATUS_UPDATE={status:'PAUSED'};}
      if(actionType==='CREATE_PAUSED_AD'){
        const costCapUsd=Number(document.getElementById('growthCostCapUsd')?.value||0);
        const campaignName=String(document.getElementById('growthCampaignName')?.value||'').trim();
        const adsetName=String(document.getElementById('growthAdsetName')?.value||'').trim();
        const adName=String(document.getElementById('growthAdName')?.value||'').trim();
        const dailyBudget=Number(document.getElementById('growthAdsetBudget')?.value||0);
        const primaryText=String(document.getElementById('growthPrimaryText')?.value||'').trim();
        const headline=String(document.getElementById('growthHeadline')?.value||'').trim();
        const description=String(document.getElementById('growthDescription')?.value||'').trim();
        const bidStrategy='COST_CAP',bidAmount=Math.round(costCapUsd*100);
        const placement=String(document.getElementById('growthPlacement')?.value||'ADVANTAGE_PLUS');
        const cta=String(document.getElementById('growthCta')?.value||'INSTALL_MOBILE_APP');
        if(!campaignName||!adsetName||!adName||!primaryText||!headline||!(dailyBudget>=5&&dailyBudget<=100)||!(costCapUsd>0)){showModalError('请补齐名称、文案和 CPI 成本上限，并把广告组每日预算设置在 5–100 美元之间。');return;}
        before.objects=[];
        after.status='PAUSED';
        after.campaign={name:campaignName,objective:'OUTCOME_APP_PROMOTION',buying_type:'AUCTION',budget_mode:'ABO',status:'PAUSED',shared_launch_campaign:true};
        after.adset={name:adsetName,daily_budget_usd:dailyBudget,optimization_goal:'APP_INSTALLS',billing_event:'IMPRESSIONS',bid_strategy:bidStrategy,bid_amount:bidAmount,cost_cap_usd:costCapUsd,placement,targeting,attribution_spec:[{event_type:'CLICK_THROUGH',window_days:1},{event_type:'VIEW_THROUGH',window_days:1},{event_type:'ENGAGED_VIDEO_VIEW',window_days:1}],status:'PAUSED'};
        after.ad={name:adName,primary_text:primaryText,headline,description,call_to_action:cta,status:'PAUSED'};
        after.creative={image_id:String(approvedEvidence.image_id),review_status:'APPROVED',page_id:String(hypothesis.page_id||''),meta_names:{campaign:campaignName,adset:adsetName,ad:adName}};
        steps.CAMPAIGN_CREATE={name:campaignName,objective:'OUTCOME_APP_PROMOTION',buying_type:'AUCTION',special_ad_categories:[],status:'PAUSED'};
        steps.IMAGE_UPLOAD={image_id:String(approvedEvidence.image_id),source:'approved_growth_creative'};
        steps.CREATIVE_CREATE={name:`${adName}_CR`,page_id:String(hypothesis.page_id||''),primary_text:primaryText,headline,description,call_to_action:cta,image_id:String(approvedEvidence.image_id)};
        steps.ADSET_CREATE={name:adsetName,daily_budget_usd:dailyBudget,optimization_goal:'APP_INSTALLS',billing_event:'IMPRESSIONS',bid_strategy:bidStrategy,bid_amount:bidAmount,placement,targeting,attribution_spec:after.adset.attribution_spec,status:'PAUSED'};
        steps.AD_CREATE={name:adName,status:'PAUSED'};
      }
      if(isBudget){const beforeBudget=Number(document.getElementById('growthBeforeBudget').value),afterBudget=Number(document.getElementById('growthAfterBudget').value);if(!(beforeBudget>0&&afterBudget>0)){showModalError('请填写有效的当前预算和计划预算。');return;}before.budget=beforeBudget;after.budget=afterBudget;steps.BUDGET_UPDATE={budget:afterBudget};}
      const body={decision_id:decision.decision_id,episode_id:episode.episode_id||'',action_type:actionType,target_account_id:experiment.account_id,target_object_type:actionType.includes('BUDGET')?'ADSET':'AD',target_object_id:target,before_json:before,after_json:after,steps,creative:after.creative||{},asset_sha256:String(approvedEvidence.image_hash||''),max_write_requests:actionType==='CREATE_PAUSED_AD'?5:1,evaluation_window:{checkpoints:['D1','D3','D7']}};
      const planKind=actionType==='CREATE_PAUSED_AD'?'create-plan':'adjustment-plan';
      try{button.disabled=true;await api(`/api/ops/ad-data-dashboard/experiments/${encodeURIComponent(experiment.experiment_id)}/${planKind}/preview`,{method:'POST',headers:postHeaders('ad-business-plan',{experiment_id:experiment.experiment_id,...body}),body:JSON.stringify(body)});closeModal();await openAdExperiment(experiment.experiment_id);}catch(error){button.disabled=false;showModalError(readableError(error));}
    });
  }

  function openPlanConfirmation(experiment,planId) {
    const isRebuild=Boolean((experiment.hypothesis_json||{}).rebuild_source);
    showModal(`<section class="growth-modal growth-modal-compact"><header class="growth-modal-head"><div><b>${isRebuild?'审批重建方案':'确认方案'}</b><small>${esc(experimentTitle(experiment))}</small></div><button type="button" class="growth-icon-button" data-modal-close>×</button></header><div class="growth-modal-body"><p>${isRebuild?'系统已从原 Meta 广告回读并冻结账户、素材、文案、受众和预算；新广告组使用 CPI 成本上限。确认后会自动完成安全演练并创建全暂停对象，不会开始投放。':'确认预算、素材、受众和目标对象没有变化。确认后先做安全演练。'}</p><div id="growthApprovePlanStatus" class="growth-notice" hidden></div></div><footer class="growth-modal-foot"><button type="button" data-modal-close>返回</button><button type="button" class="growth-primary" id="growthApproveExactPlan">${isRebuild?'批准并开始重建':'确认方案'}</button></footer></section>`);
    document.getElementById('growthApproveExactPlan').addEventListener('click', async event => {
      const button=event.currentTarget,status=document.getElementById('growthApprovePlanStatus');
      try {
        button.disabled=true;if(status){status.hidden=false;status.textContent=isRebuild?'正在批准方案…':'正在确认方案…';}
        const body={confirmation:'APPROVE_EXACT_PLAN'};
        await api(`/api/ops/ad-data-dashboard/meta-plans/${encodeURIComponent(planId)}/approve`,{method:'POST',headers:postHeaders('ad-plan-approve',{plan_id:planId,...body}),body:JSON.stringify(body)});
        if(isRebuild){
          if(status)status.textContent='审批已记录，正在执行安全演练…';
          const dryBody={execution_mode:'dry_run'};
          await api(`/api/ops/ad-data-dashboard/meta-plans/${encodeURIComponent(planId)}/execute`,{method:'POST',headers:postHeaders('rebuild-plan-dry-run',{plan_id:planId,...dryBody}),body:JSON.stringify(dryBody)});
          if(status)status.textContent='安全演练通过，正在提交暂停态对象创建…';
          const liveBody={execution_mode:'live',confirmation:'CREATE_PAUSED_OBJECTS'};
          await api(`/api/ops/ad-data-dashboard/meta-plans/${encodeURIComponent(planId)}/execute`,{method:'POST',headers:postHeaders('rebuild-plan-live',{plan_id:planId,...liveBody}),body:JSON.stringify(liveBody)});
        }
        closeModal();await openAdExperiment(experiment.experiment_id);
      } catch(error){button.disabled=false;if(status){status.hidden=false;status.textContent=readableError(error);}else showModalError(readableError(error));}
    });
  }

  async function executeDryRun(experiment,planId,button) {
    const status=document.getElementById('adExperimentControlStatus');
    try { button.disabled=true;status.hidden=false;status.textContent='正在执行 dry-run 演练…';const body={execution_mode:'dry_run'};const result=await api(`/api/ops/ad-data-dashboard/meta-plans/${encodeURIComponent(planId)}/execute`,{method:'POST',headers:postHeaders('ad-plan-dry-run',{plan_id:planId,...body}),body:JSON.stringify(body)});status.textContent=`演练与核验完成：${(result.receipts||[]).length} 条回执，Meta 写入=${result.meta_writes_performed?'是':'否'}。`;button.disabled=false;await openAdExperiment(experiment.experiment_id);await openPlanReceipts(experiment,planId); }
    catch(error){status.hidden=false;status.textContent=readableError(error);button.disabled=false;}
  }

  function openPausedCreationConfirmation(experiment,planId) {
    showModal(`<section class="growth-modal growth-modal-compact"><header class="growth-modal-head"><div><b>创建暂停态广告</b><small>${esc(experimentTitle(experiment))}</small></div><button type="button" class="growth-icon-button" data-modal-close>×</button></header><div class="growth-modal-body"><p>将创建 Campaign、广告组、素材和广告，全部保持暂停，不会开始投放。</p><div id="growthPausedCreationStatus" class="growth-notice" hidden></div></div><footer class="growth-modal-foot"><button type="button" data-modal-close>返回</button><button type="button" class="growth-primary" id="growthConfirmPausedCreation">确认创建</button></footer></section>`);
    const button=document.getElementById('growthConfirmPausedCreation');
    button.addEventListener('click',async()=>{
      const status=document.getElementById('growthPausedCreationStatus');
      try {
        button.disabled=true;status.hidden=false;status.textContent='正在提交暂停态广告创建任务…';
        const body={execution_mode:'live',confirmation:'CREATE_PAUSED_OBJECTS'};
        await api(`/api/ops/ad-data-dashboard/meta-plans/${encodeURIComponent(planId)}/execute`,{method:'POST',headers:postHeaders('ad-plan-live-create',{plan_id:planId,...body}),body:JSON.stringify(body)});
        closeModal();await openAdExperiment(experiment.experiment_id);await openPlanReceipts(experiment,planId);
      } catch(error) { button.disabled=false;status.hidden=false;status.textContent=readableError(error); }
    });
  }

  function creationOperatorIssue(payload={}) {
    const task=payload.execution_task||{},message=String(task.error_message||''),step=String(task.current_step||'').toUpperCase();
    if(!message.includes('meta_graph_error:100:1815645'))return null;
    const planDetail=((payload.plan||{}).plan)||{},cells=Array.isArray(planDetail.cells)?planDetail.cells:[];
    const pageId=String(cells.map(cell=>((((cell||{}).steps||{}).CREATIVE_CREATE||{}).object_story_spec||{}).page_id||'').find(Boolean)||'');
    const accountId=String(planDetail.target_account_id||'');
    return {
      title:'公共主页不能用于当前广告账户',
      summary:'Meta 已拒绝广告创建。问题在投放身份配置，不在素材；已通过素材会保留。',
      location:'当前广告订单 → 查看目标 → Meta 身份 → 修改公共主页',
      parameter:`广告账户尾号 ${accountId.slice(-4)||'—'} · 公共主页尾号 ${pageId.slice(-4)||'—'}`,
      action:'点击“修改公共主页”，从该广告账户历史投放中已验证成功的主页里选择；广告账户保持不变。',
      after:'保存后系统会在当前订单内创建修复方案，保留 Campaign、广告组和已通过素材，只从未完成的广告继续。',
      step:RECEIPT_STEP_LABELS[step]||step||'广告创建',
    };
  }

  async function openPlanReceipts(experiment,planId) {
    if(!planId){showModalError('当前还没有可查看的 Plan。');return;}
    try {
      const payload=await api(`/api/ops/ad-data-dashboard/meta-plans/${encodeURIComponent(planId)}/receipt`);
      const dry=payload.dry_run_receipt||{},task=payload.execution_task||{},real=payload.receipts||[],items=real.length?real:(dry.receipts||[]);
      const rows=items.map(item=>{const step=String(item.step_name||'').toUpperCase(),status=String(item.step_status||'').toUpperCase(),verification=String(item.verification||((item.verification_result_json||{}).status)||'').toUpperCase();return `<tr><td>${esc(RECEIPT_STEP_LABELS[step]||step||'待检查')}</td><td>${esc(RECEIPT_STATUS_LABELS[status]||status||'待开始')}</td><td>${esc(RECEIPT_STATUS_LABELS[verification]||verification||'—')}</td></tr>`;}).join('');
      const nextStep=String(payload.next_step||''),canSubmit=nextStep==='SUBMIT_PAUSED_OBJECT_CREATION',operatorIssue=creationOperatorIssue(payload);
      const needsReplan=nextStep==='PLAN_EXPIRED_REPLAN';
      const issueCard=operatorIssue?`<section class="growth-operator-issue" role="alert" aria-label="需要人工处理的创建问题"><strong>${esc(operatorIssue.title)}</strong><dl><dt>出问题的位置</dt><dd>${esc(operatorIssue.step)}</dd><dt>去哪里修改</dt><dd>${esc(operatorIssue.location)}</dd><dt>需要检查的参数</dt><dd>${esc(operatorIssue.parameter)}</dd><dt>具体怎么改</dt><dd>${esc(operatorIssue.action)}</dd><dt>修好以后</dt><dd>${esc(operatorIssue.after)}</dd></dl><small>系统已停止继续提交，当前不会产生广告花费。</small></section>`:'';
      const nextCard=operatorIssue?'':(canSubmit?'<div class="growth-review-card"><b>下一步：确认创建暂停态广告</b>安全检查已经通过。确认后才会创建广告系列、广告组、广告素材和广告，且全部保持暂停。<br><span>创建并回读 → Meta 审核 → 单独确认启用 → D1 / D3 / D7 观察</span></div>':(needsReplan?'<div class="growth-review-card"><b>方案已过期</b>旧任务不会执行，也不会产生 Meta 写入。请基于当前账户、受众、素材和预算重新生成完整创建方案。</div>':(nextStep==='LIVE_EXECUTION_UNAVAILABLE'?'<div class="growth-review-card"><b>广告还没有创建</b>你已经确认创建，但真实创建通道尚未开启，所以系统没有开始执行。不要重复提交。<br><span>下一步：系统管理员完成一次性执行配置；配置就绪后再创建并自动回读。</span></div>':(nextStep==='WAIT_FOR_EXECUTOR'?'<div class="growth-review-card"><b>等待系统开始创建</b>你的确认已经记录，目前没有 Meta 写入。刷新状态即可，不需要重复提交。</div>':(nextStep==='WAIT_FOR_PAUSED_OBJECTS'?'<div class="growth-review-card"><b>正在创建并核对</b>系统正在逐项创建并回读真实状态；不会自动启用广告。</div>':(nextStep==='REVIEW_PAUSED_OBJECTS'?'<div class="growth-review-card"><b>下一步：核对已创建广告</b>确认广告系列、广告组和广告都处于暂停状态，再进入 Meta 审核。</div>':'<div class="growth-review-card"><b>需要人工核对</b>系统无法确认某一步的真实结果，已停止自动推进，不会重复写入。</div>'))))));
      const taskStarted=Boolean(task.execution_task_id),headline=taskStarted?statusLabel(task.status):'安全检查已通过';
      const intro=operatorIssue?operatorIssue.summary:(nextStep==='LIVE_EXECUTION_UNAVAILABLE'?'广告尚未创建，下面是已经完成的安全检查。':(taskStarted?'这里显示真实创建进度；只有取得 Meta 回读结果才算完成。':'检查通过不等于广告已经创建。'));
      showModal(`<section class="growth-modal"><header class="growth-modal-head"><div><b>创建状态</b><small>${esc(experimentTitle(experiment))}</small></div><button type="button" class="growth-icon-button" data-modal-close aria-label="关闭">×</button></header><div class="growth-modal-body"><h3>${esc(operatorIssue?.title||(needsReplan?'方案已过期':headline))}</h3><p>${esc(intro)}</p>${issueCard}<table class="growth-metric-table"><thead><tr><th>要做什么</th><th>当前结果</th><th>是否核对</th></tr></thead><tbody>${rows||'<tr><td colspan="3">系统尚未开始创建</td></tr>'}</tbody></table>${nextCard}<div class="growth-safety">Meta 写入：${payload.meta_writes_performed?'已发生':'未发生'}。广告未创建前不会进入投放。</div></div><footer class="growth-modal-foot"><button type="button" data-modal-close>返回</button>${taskStarted&&!needsReplan?'<button type="button" id="growthRefreshCreationStatus">重新检查状态</button>':''}${canSubmit?'<button type="button" class="growth-primary" id="growthReceiptNextStep">确认创建暂停态广告</button>':''}${needsReplan?'<button type="button" class="growth-primary" id="growthRebuildCreatePlan">重新生成创建方案</button>':''}</footer></section>`);
      const nextButton=document.getElementById('growthReceiptNextStep');
      if(nextButton)nextButton.addEventListener('click',()=>{closeModal();openPausedCreationConfirmation(experiment,planId);});
      const refreshButton=document.getElementById('growthRefreshCreationStatus');
      if(refreshButton)refreshButton.addEventListener('click',()=>openPlanReceipts(experiment,planId));
      const rebuildButton=document.getElementById('growthRebuildCreatePlan');
      if(rebuildButton)rebuildButton.addEventListener('click',async()=>{try{rebuildButton.disabled=true;await api(`/api/ops/ad-data-dashboard/meta-plans/${encodeURIComponent(planId)}/invalidate-expired`,{method:'POST',headers:postHeaders('ad-plan-expire',{plan_id:planId})});const fresh=await api(`/api/ops/ad-data-dashboard/experiments/${encodeURIComponent(experiment.experiment_id)}`);closeModal();openPlanReadiness(fresh.experiment||experiment,fresh);}catch(error){rebuildButton.disabled=false;showModalError(readableError(error));}});
    } catch(error){showModalError(readableError(error));}
  }

  function openExceptionReview(experiment,payload,planId) {
    const workflow=payload.workflow||{},timeline=(payload.timeline||{}).items||[];
    const recent=timeline.slice(-5).reverse();
    showModal(`<section class="growth-modal"><header class="growth-modal-head"><div><b>投放结果待核对</b><small>${esc(experimentDrawerTitle(experiment))}</small></div><button type="button" class="growth-icon-button" data-modal-close>×</button></header><div class="growth-modal-body"><h3>当前广告仍保持暂停</h3><p>系统没有确认本次开关操作成功，因此已停止继续提交。当前不会产生广告花费。</p><div class="growth-review-card"><b>需要你处理什么</b>${planId?'点击“核对 Meta 状态”，确认真实开关结果后，系统会给出下一步。':'当前缺少可核对的执行记录，请稍后刷新。'}</div><div class="growth-safety">系统不会自动重试结果不确定的 Meta 操作，也不会删除已经创建的广告。</div><details class="growth-technical"><summary>查看技术记录</summary><div class="growth-timeline">${recent.map(item=>`<div><b>${esc(statusLabel(item.to_state)||'状态变化')}</b><br>${esc(item.created_at||'')}</div>`).join('')||'<div>暂无技术记录</div>'}</div></details></div><footer class="growth-modal-foot"><button type="button" data-modal-close>关闭</button>${planId?'<button type="button" class="growth-primary" id="growthExceptionReceipts">核对 Meta 状态</button>':''}</footer></section>`);
    const button=document.getElementById('growthExceptionReceipts');
    if(button)button.addEventListener('click',()=>openPlanReceipts(experiment,planId));
  }

  async function refreshLaunchDeliveryOrder(launchId) {
    if(!launchId)return;
    const order=await api(`/api/ops/ad-data-dashboard/new-account-launches/${encodeURIComponent(launchId)}`);
    hydrateLaunchOrder(order);await Promise.all([refreshLaunchMetaRateLimit(),refreshLaunchDeliveryStatus()]);renderLaunch('cold');
  }

  function deliveryFailureGuidance(payload) {
    const task=payload?.execution_task||{},step=String(task.current_step||'').toUpperCase(),message=String(task.error_message||'');
    const stage={CAMPAIGN_STATUS_UPDATE:'开启广告系列',ADSET_STATUS_UPDATE:'开启广告组',AD_STATUS_UPDATE:'开启广告',VERIFY:'核对 Meta 状态',RECONCILE:'再次核对 Meta 状态'}[step]||'执行 Meta 操作';
    if(message.includes('meta_graph_endpoint_'))return {stage,reason:'系统的 Meta 连接配置未通过安全检查，本次没有提交开启请求',next:'系统修复配置并确认广告仍为暂停后，可重新开启整单投放'};
    if(message.includes('meta_graph_error:2500:'))return {stage,reason:'系统连接 Meta 的地址配置错误',next:'系统修复连接并确认对象仍为暂停后，可重新开启整单投放'};
    if(message.includes('meta_rate_limit'))return {stage,reason:'Meta API 当前限流，系统已停止继续请求',next:'等待限流窗口恢复并完成状态核对后再重试'};
    return {stage,reason:'系统没有取得足够的 Meta 回读证据，无法确认操作结果',next:'先核对广告系列、广告组和广告状态，再决定是否重试'};
  }

  function openDeliveryFailure(payload,experiment={}) {
    const guidance=deliveryFailureGuidance(payload);
    showModal(`<section class="growth-modal growth-modal-compact"><header class="growth-modal-head"><div><b>开启投放失败</b><small>${esc(experimentDrawerTitle(experiment)||launchCampaignName(launchState.launch||{}))}</small></div><button type="button" class="growth-icon-button" data-modal-close>×</button></header><div class="growth-modal-body"><div class="growth-review-card"><b>卡在：${esc(guidance.stage)}</b>${esc(guidance.reason)}</div><div class="growth-safety"><b>当前结果</b> 广告仍保持暂停，没有开始产生花费。</div><div class="growth-review-card"><b>下一步</b>${esc(guidance.next)}</div></div><footer class="growth-modal-foot"><button type="button" class="growth-primary" data-modal-close>知道了</button></footer></section>`);
  }

  function openLaunchActivationException() {
    const experiment=(launchState.experiments||[]).find(item=>String(item.state||'')==='DATA_INCOMPLETE')||{};
    const failure=experiment.workflow?.failure||{};
    const guidance={stage:failure.stage||'开启广告系列',reason:failure.reason||'系统未能完成本次开启操作',next:failure.next_step||'系统确认广告仍为暂停后，会重新开放整单开启'};
    showModal(`<section class="growth-modal growth-modal-compact"><header class="growth-modal-head"><div><b>开启投放失败</b><small>${esc(launchCampaignName(launchState.launch||{}))}</small></div><button type="button" class="growth-icon-button" data-modal-close>×</button></header><div class="growth-modal-body"><div class="growth-review-card"><b>卡在：${esc(guidance.stage)}</b>${esc(guidance.reason)}</div><div class="growth-safety"><b>当前结果</b> 整个订单仍保持暂停，没有产生新的广告花费。</div><div class="growth-review-card"><b>下一步</b>${esc(guidance.next)}</div></div><footer class="growth-modal-foot"><button type="button" class="growth-primary" data-modal-close>知道了</button></footer></section>`);
  }

  async function openLaunchActivationConfirmation(triggerButton=null) {
    if(guardLaunchMetaAction())return;
    const originalText=triggerButton?.textContent||'开启整单投放';
    let launchId=String((launchState.launch||{}).launch_id||''),experiments=launchState.experiments||[];
    try{
      if(triggerButton){triggerButton.disabled=true;triggerButton.textContent='正在核对投放对象…';}
      if(!launchId)throw new Error('当前订单标识未完整回读，请刷新订单后重试。');
      if(experiments.length<2||launchState.orderDataMismatch){
        showModal('<section class="growth-modal growth-modal-compact"><header class="growth-modal-head"><div><b>正在核对整单投放</b><small>读取当前订单的真实广告对象</small></div></header><div class="growth-modal-body"><p>系统正在补全广告系列、广告组和广告的回读结果，不会写入 Meta。</p></div></section>');
        const order=await api(`/api/ops/ad-data-dashboard/new-account-launches/${encodeURIComponent(launchId)}`);
        hydrateLaunchOrder(order);
        launchId=String((launchState.launch||{}).launch_id||launchId);
        experiments=launchState.experiments||[];
      }
      if(experiments.length<2||launchState.orderDataMismatch)throw new Error('订单投放对象尚未完整回读，系统已停止开启；请刷新订单后重试。');
      const campaign=launchCampaignName(launchState.launch||{});
      showModal(`<section class="growth-modal growth-modal-compact"><header class="growth-modal-head"><div><b>开启整单投放</b><small>${esc(campaign)}</small></div><button type="button" class="growth-icon-button" data-modal-close>×</button></header><div class="growth-modal-body"><p>一次开启共享广告系列，以及本订单的 ${experiments.length} 个广告组和 ${experiments.length} 条广告。</p><div class="growth-review-card"><b>订单 ${esc(String(launchId).slice(-6).toUpperCase())}</b>${esc(experiments.map(item=>item.experiment_code||item.ad_id).join(' · '))}</div><div class="growth-safety">确认后可能立即产生广告费用。系统会逐层回读；结果不确定时不会重复提交。</div></div><footer class="growth-modal-foot"><button type="button" data-modal-close>取消</button><button type="button" class="growth-primary" id="growthConfirmLaunchActivation">确认开启整单</button></footer></section>`);
      document.getElementById('growthConfirmLaunchActivation')?.addEventListener('click',async event=>{const button=event.currentTarget,body={confirmation:'ENABLE_LAUNCH_DELIVERY'};try{button.disabled=true;const result=await api(`/api/ops/ad-data-dashboard/new-account-launches/${encodeURIComponent(launchId)}/activate`,{method:'POST',headers:postHeaders('launch-enable-delivery',{launch_id:launchId,...body}),body:JSON.stringify(body)});closeModal();await waitForLaunchDeliveryReadback(String(result.plan_id||''),launchId);}catch(error){button.disabled=false;showModalError(readableError(error));}});
    }catch(error){
      showModal(`<section class="growth-modal growth-modal-compact"><header class="growth-modal-head"><div><b>暂时不能开启整单投放</b><small>${esc(launchCampaignName(launchState.launch||{}))}</small></div><button type="button" class="growth-icon-button" data-modal-close>×</button></header><div class="growth-modal-body"><div class="growth-operator-issue"><strong>系统未提交任何投放操作</strong><small>${esc(readableError(error))}</small></div></div><footer class="growth-modal-foot"><button type="button" class="growth-primary" data-modal-close>知道了</button></footer></section>`);
    }finally{
      if(triggerButton?.isConnected){triggerButton.disabled=false;triggerButton.textContent=originalText;}
    }
  }

  async function waitForLaunchDeliveryReadback(planId,launchId) {
    let terminal='',payload={};
    for(let attempt=0;attempt<40;attempt+=1){
      payload=await api(`/api/ops/ad-data-dashboard/meta-plans/${encodeURIComponent(planId)}/receipt`);
      terminal=String((payload.execution_task||{}).status||'').toUpperCase();
      if(['SUCCESS','MANUAL_REVIEW'].includes(terminal))break;
      await new Promise(resolve=>setTimeout(resolve,1500));
    }
    await refreshLaunchDeliveryOrder(launchId);
    if(terminal==='SUCCESS'){showLaunchToast('整单投放已开启');return;}
    if(terminal==='MANUAL_REVIEW'){openDeliveryFailure(payload,(launchState.experiments||[])[0]||{});return;}
    showLaunchToast('开启结果尚未确认，请稍后刷新订单');
  }

  async function waitForDeliveryReadback(experiment,planId,mode,launchId='') {
    const enabling=mode==='ENABLE',successMessage=enabling?'开启成功':'关闭成功';
    let terminal='';
    for(let attempt=0;attempt<40;attempt+=1){
      try{
        const payload=await api(`/api/ops/ad-data-dashboard/meta-plans/${encodeURIComponent(planId)}/receipt`);
        terminal=String((payload.execution_task||{}).status||'').toUpperCase();
        if(terminal==='SUCCESS'||terminal==='MANUAL_REVIEW')break;
      }catch(error){
        if(attempt===39){showLaunchToast(`结果读取失败：${readableError(error)}`);return;}
      }
      await new Promise(resolve=>setTimeout(resolve,1500));
    }
    if(launchId){try{await refreshLaunchDeliveryOrder(launchId);}catch(error){loadLaunchOrders({badgeOnly:true});}}
    else{try{await openAdExperiment(experiment.experiment_id);}catch(error){loadLaunchOrders({badgeOnly:true});}}
    if(terminal==='SUCCESS'){showLaunchToast(successMessage);return;}
    if(terminal==='MANUAL_REVIEW'){showLaunchToast(enabling?'开启失败，广告仍保持关闭':'关闭失败，请核对广告状态');return;}
    showLaunchToast('结果尚未确认，请稍后刷新订单');
  }

  function openActivationConfirmation(experiment,payload,options={}) {
    const growth=(payload.growth_lineage||{}).episode_detail||{},decision=growth.decision||{},episode=growth.episode||{},blockers=[];
    const activationReady=['META_REVIEW_PENDING','READY_FOR_ACTIVATION','PAUSED'].includes(String(experiment.state||''));
    if(!decision.decision_id)blockers.push('缺少决策记录');if(!experiment.account_id)blockers.push('缺少目标广告账户');if(!experiment.source_ad_id)blockers.push('广告尚未创建');if(!experiment.source_campaign_id)blockers.push('Campaign 尚未创建');if(!experiment.source_adset_id)blockers.push('广告组尚未创建');
    if(!activationReady||blockers.length){showModal(`<section class="growth-modal growth-modal-compact"><header class="growth-modal-head"><div><b>广告创建尚未完成</b><small>${esc(experimentDrawerTitle(experiment))}</small></div><button type="button" class="growth-icon-button" data-modal-close aria-label="关闭">×</button></header><div class="growth-modal-body"><p>系统会自动保存并核对已经创建的对象，不需要你提供 ID，也不会重复创建。</p><div class="growth-review-card"><b>当前不会进入启用</b>只有 Campaign、广告组和广告全部创建并回读成功后，才会出现“开启投放”。</div></div><footer class="growth-modal-foot"><button type="button" class="growth-primary" data-modal-close>知道了</button></footer></section>`);return;}
    showModal(`<section class="growth-modal growth-modal-compact"><header class="growth-modal-head"><div><b>确认开启广告</b><small>${esc(experimentDrawerTitle(experiment))}</small></div><button type="button" class="growth-icon-button" data-modal-close>×</button></header><div class="growth-modal-body"><p>确认后系统会自动完成安全检查，开启这条广告及其对应广告组；如果共享广告系列尚未开启，会一并开启。无需重新生成广告或重复确认。</p><div class="growth-review-card"><b>订单 ${esc(experimentOrderSuffix(experiment)||'—')}</b>Campaign ${esc(experiment.source_campaign_id||'待回读')}<br>Ad Set ${esc(experiment.source_adset_id||'待回读')}<br>Ad ${esc(experiment.source_ad_id||'待回读')}</div>${blockers.length?`<div class="growth-error">暂不能开启：${esc(blockers.join('、'))}</div>`:'<div class="growth-safety">确认后可能立即产生广告费用；已有启用方案会继续使用，不重新生成。系统会回读真实状态，不确定时不会重复提交。</div>'}</div><footer class="growth-modal-foot"><button type="button" data-modal-close>取消</button><button type="button" class="growth-primary" id="growthConfirmActivation" ${blockers.length?'disabled':''}>确认开启</button></footer></section>`);
    const button=document.getElementById('growthConfirmActivation');
    if(button&&!blockers.length)button.addEventListener('click',async()=>{const body={decision_id:decision.decision_id,episode_id:episode.episode_id||'',confirmation:'ENABLE_DELIVERY'};try{button.disabled=true;const result=await api(`/api/ops/ad-data-dashboard/experiments/${encodeURIComponent(experiment.experiment_id)}/activate`,{method:'POST',headers:postHeaders('ad-enable-now',{experiment_id:experiment.experiment_id,ad_id:experiment.source_ad_id}),body:JSON.stringify(body)});closeModal();await waitForDeliveryReadback(experiment,String(result.plan_id||''),'ENABLE',String(options.launchId||''));}catch(error){button.disabled=false;showModalError(readableError(error));}});
  }

  function openActivationPlan(experiment,payload) {
    const growth=(payload.growth_lineage||{}).episode_detail||{},decision=growth.decision||{},episode=growth.episode||{};
    const blockers=[];if(!decision.decision_id)blockers.push('缺少决策记录');if(!experiment.account_id)blockers.push('缺少目标广告账户');if(!experiment.source_ad_id)blockers.push('缺少已回读的 Ad ID');
    if(!experiment.source_campaign_id)blockers.push('缺少已回读的 Campaign ID');if(!experiment.source_adset_id)blockers.push('缺少已回读的 Ad Set ID');
    showModal(`<section class="growth-modal"><header class="growth-modal-head"><div><b>启用投放前检查</b><small>${esc(experimentTitle(experiment))}</small></div><button type="button" class="growth-icon-button" data-modal-close>×</button></header><div class="growth-modal-body"><h3>同时开启三层投放路径</h3><p>系统会依次启用广告系列、这条实验对应的广告组和广告，并逐层回读；任何一层不确定都会停止。</p><div class="growth-review-card"><b>本次影响范围</b>Campaign ${esc(experiment.source_campaign_id||'待回读')}<br>Ad Set ${esc(experiment.source_adset_id||'待回读')}<br>Ad ${esc(experiment.source_ad_id||'待回读')}<br>启用成功后进入 D1 / D3 / D7 自动观察。</div>${experiment.state==='META_REVIEW_PENDING'?'<div class="growth-notice">Meta 尚未返回可单独确认的“审核通过”状态。你确认开启后：已通过会开始配送；仍在审核会等待 Meta 审核完成后配送。</div>':''}${blockers.length?`<div class="growth-error">暂不能生成：${esc(blockers.join('、'))}</div>`:'<div class="growth-safety">先生成不可变 Plan、确认并完成 dry-run；最后一次确认才会真实启用投放。</div>'}</div><footer class="growth-modal-foot"><button type="button" data-modal-close>返回</button><button type="button" class="growth-primary" id="growthCreateActivationPlan" ${blockers.length?'disabled':''}>生成启用 Plan</button></footer></section>`);
    const button=document.getElementById('growthCreateActivationPlan');
    if(button&&!blockers.length)button.addEventListener('click',async()=>{const body={decision_id:decision.decision_id,episode_id:episode.episode_id||'',action_type:'REACTIVATE_AD',target_account_id:experiment.account_id,target_object_type:'DELIVERY_PATH',target_object_id:experiment.source_ad_id,before_json:{status:'PAUSED'},after_json:{status:'ACTIVE'},steps:{CAMPAIGN_STATUS_UPDATE:{target_id:experiment.source_campaign_id,status:'ACTIVE'},ADSET_STATUS_UPDATE:{target_id:experiment.source_adset_id,status:'ACTIVE'},AD_STATUS_UPDATE:{target_id:experiment.source_ad_id,status:'ACTIVE'}},max_write_requests:3,evaluation_window:{checkpoints:['D1','D3','D7']}};try{button.disabled=true;await api(`/api/ops/ad-data-dashboard/experiments/${encodeURIComponent(experiment.experiment_id)}/activation-plan/preview`,{method:'POST',headers:postHeaders('ad-activation-plan',{experiment_id:experiment.experiment_id,...body}),body:JSON.stringify(body)});closeModal();await openAdExperiment(experiment.experiment_id);}catch(error){button.disabled=false;showModalError(readableError(error));}});
  }

  function openPausePlan(experiment,payload,options={}) {
    const growth=(payload.growth_lineage||{}).episode_detail||{},decision=growth.decision||{},episode=growth.episode||{};
    const blockers=[];if(!decision.decision_id)blockers.push('缺少决策记录');if(!experiment.account_id)blockers.push('缺少目标广告账户');if(!experiment.source_ad_id)blockers.push('缺少已回读的 Ad ID');
    showModal(`<section class="growth-modal growth-modal-compact"><header class="growth-modal-head"><div><b>暂停这条广告</b><small>${esc(experimentTitle(experiment))}</small></div><button type="button" class="growth-icon-button" data-modal-close>×</button></header><div class="growth-modal-body"><p>暂停只作用于 Ad ${esc(experiment.source_ad_id||'待回读')}，不会暂停共享广告系列，也不会影响同系列下的其他实验。</p>${blockers.length?`<div class="growth-error">暂不能生成：${esc(blockers.join('、'))}</div>`:'<div class="growth-safety">系统仍会先生成 Plan、确认并 dry-run；最后一次确认后才会真实暂停。</div>'}</div><footer class="growth-modal-foot"><button type="button" data-modal-close>返回</button><button type="button" class="growth-danger" id="growthCreatePausePlan" ${blockers.length?'disabled':''}>生成暂停 Plan</button></footer></section>`);
    const button=document.getElementById('growthCreatePausePlan');
    if(button&&!blockers.length)button.addEventListener('click',async()=>{const body={decision_id:decision.decision_id,episode_id:episode.episode_id||'',action_type:'PAUSE_AD',target_account_id:experiment.account_id,target_object_type:'AD',target_object_id:experiment.source_ad_id,before_json:{status:'ACTIVE'},after_json:{status:'PAUSED'},steps:{STATUS_UPDATE:{target_id:experiment.source_ad_id,status:'PAUSED'}},max_write_requests:1,evaluation_window:{checkpoints:['D1','D3','D7']}};try{button.disabled=true;launchState.deliveryLaunchId=String(options.launchId||'');await api(`/api/ops/ad-data-dashboard/experiments/${encodeURIComponent(experiment.experiment_id)}/adjustment-plan/preview`,{method:'POST',headers:postHeaders('ad-pause-plan',{experiment_id:experiment.experiment_id,...body}),body:JSON.stringify(body)});closeModal();await openAdExperiment(experiment.experiment_id);}catch(error){button.disabled=false;showModalError(readableError(error));}});
  }

  function openDeliveryExecutionConfirmation(experiment,planId,mode) {
    const enabling=mode==='ENABLE',verb=enabling?'启用投放':'暂停投放';
    showModal(`<section class="growth-modal growth-modal-compact"><header class="growth-modal-head"><div><b>确认${verb}</b><small>${esc(experimentTitle(experiment))}</small></div><button type="button" class="growth-icon-button" data-modal-close>×</button></header><div class="growth-modal-body"><p>${enabling?'将真实启用广告系列、对应广告组和广告。成功后可能开始产生费用。':'将真实暂停这条广告；共享广告系列和其他实验保持不变。'}</p></div><footer class="growth-modal-foot"><button type="button" data-modal-close>返回</button><button type="button" class="${enabling?'growth-primary':'growth-danger'}" id="growthConfirmDeliveryExecution">确认${verb}</button></footer></section>`);
    const button=document.getElementById('growthConfirmDeliveryExecution');
    button.addEventListener('click',async()=>{try{button.disabled=true;const body={execution_mode:'live',confirmation:enabling?'ENABLE_DELIVERY':'PAUSE_DELIVERY'};await api(`/api/ops/ad-data-dashboard/meta-plans/${encodeURIComponent(planId)}/execute`,{method:'POST',headers:postHeaders(enabling?'ad-enable-delivery':'ad-pause-delivery',{plan_id:planId,...body}),body:JSON.stringify(body)});closeModal();await waitForDeliveryReadback(experiment,planId,mode,String(launchState.deliveryLaunchId||''));}catch(error){button.disabled=false;showModalError(readableError(error));}});
  }

  async function openCheckpointReview(experiment,payload) {
    const evaluations=(payload.performance||{}).items||[],latest=evaluations[evaluations.length-1]||{};
    let recommendation;
    try { recommendation=await api(`/api/ops/ad-data-dashboard/experiments/${encodeURIComponent(experiment.experiment_id)}/recommendation`); }
    catch (error) { showModalError(`下一轮建议读取失败：${readableError(error)}。系统没有把失败伪装成“继续观察”。`); return; }
    showModal(`<section class="growth-modal growth-modal-compact"><header class="growth-modal-head"><div><b>${esc(latest.checkpoint||'阶段')} 复盘</b><small>${esc(experimentTitle(experiment))}</small></div><button type="button" class="growth-icon-button" data-modal-close>×</button></header><div class="growth-modal-body"><h3>${esc(statusLabel(latest.evaluation_status||experiment.state))}</h3><div class="growth-review-card"><b>下一步</b>${esc(statusLabel(recommendation.recommended_action||'OBSERVE'))}</div><p>当前是阶段数据结论，后续实验会继续验证。</p></div><footer class="growth-modal-foot"><button type="button" class="growth-primary" data-modal-close>关闭</button></footer></section>`);
  }

  function openCreateFlow() { state.createStep=1;state.createSource='manual';state.draftRecommendation='';renderCreateFlow(); }

  function renderCreateFlow() {
    const step=state.createStep;
    showModal(`<section class="growth-modal"><header class="growth-modal-head"><div><b>新建广告实验</b><small>第 ${step} 步，共 3 步</small></div><button type="button" class="growth-icon-button" data-modal-close>×</button></header><div class="growth-modal-body"><div class="growth-mini-steps"><span class="active">来源</span><span class="${step>=2?'active':''}">实验方案</span><span class="${step>=3?'active':''}">风险与周期</span></div>${createStepHtml(step)}</div><footer class="growth-modal-foot"><button type="button" class="growth-secondary" id="growthCreateBack">${step===1?'取消':'上一步'}</button><button type="button" class="growth-primary" id="growthCreateNext">${step===3?'创建 dry-run 草稿':'下一步'}</button></footer></section>`);
    document.getElementById('growthCreateBack').addEventListener('click',()=>{if(step===1)closeModal();else{state.createStep-=1;renderCreateFlow();}});
    document.getElementById('growthCreateNext').addEventListener('click',()=>{if(step<3){captureCreateStep(step);state.createStep+=1;renderCreateFlow();}else createExperimentDraft();});
    document.querySelectorAll('[data-create-source]').forEach(button=>button.addEventListener('click',()=>{state.createSource=button.dataset.createSource;renderCreateFlow();}));
  }

  function createStepHtml(step) {
    if(step===1)return `<h3>手动实验（高级功能）</h3><p>正常情况下请从“近 7 天建议”确认系统方案，系统会自动建立闭环。</p><div class="growth-review-card"><b>仅在没有对应系统建议时使用</b>适合临时素材测试或你已经明确要验证的单一变量。这里不需要填写任何技术 ID。</div>`;
    if(step===2)return `<h3>确认实验方案</h3><p>只填写业务信息，技术映射会自动记录。</p><div class="growth-review-card"><b>推广应用：Tugao</b>系统使用固定应用配置，无需额外填写。</div><div class="growth-form"><label>国家/市场<input id="growthDraftCountry" value="${esc(state.draftCountry||'ID')}"></label><label>实验类型<select id="growthDraftType"><option value="NEW_AD_TEST">新广告测试</option><option value="CREATIVE_REPLACEMENT">素材替换</option><option value="BUDGET_SCALE_UP">预算上调</option><option value="BUDGET_REDUCTION">预算下调</option><option value="PAUSE_TEST">暂停测试</option></select></label><label>实验假设<textarea id="growthDraftHypothesis">${esc(state.draftHypothesis||'只改变一个变量，并以 CPA 与真实加入量验证效果。')}</textarea></label></div>`;
    return `<h3>风险与周期确认</h3><p>单人模式下，提交后只创建业务草稿，不会直接写入 Meta。</p><div class="growth-review-card"><b>默认观察周期</b>D1 / D3 / D7 自动回读<br>护栏：消耗、真实加入量、CPA</div><div class="growth-safety">创建后仍需生成不可变 Plan、再次确认并通过 dry-run。</div>`;
  }

  function captureCreateStep(step) {
    if(step===1) state.draftRecommendation='';
    if(step===2){state.draftApp='tugao';state.draftCountry=document.getElementById('growthDraftCountry').value.trim();state.draftType=document.getElementById('growthDraftType').value;state.draftHypothesis=document.getElementById('growthDraftHypothesis').value.trim();}
  }

  async function createExperimentDraft() {
    const button=document.getElementById('growthCreateNext');
    const body={target_app:'tugao',country:state.draftCountry||'',experiment_type:state.draftType||'NEW_AD_TEST',source_recommendation_id:state.draftRecommendation||'',hypothesis_json:{summary:state.draftHypothesis||''},primary_metric:'cpa',guardrail_metrics_json:['installs','cpi','ctr','real_bind_count','real_bind_cpa'],maturity_rule_json:{minimum_installs:100,minimum_real_joins:10}};
    try { button.disabled=true;const item=await api('/api/ops/ad-data-dashboard/experiments/draft',{method:'POST',headers:postHeaders('ad-experiment-draft',body),body:JSON.stringify(body)});closeModal();await loadList({select:item.experiment_id}); }
    catch(error){button.disabled=false;showModalError(readableError(error));}
  }

  function recommendationExperimentType(recommendation, selectedAction) {
    const raw=String(recommendation.primary_action||recommendation.action_type||'').toLowerCase();
    if(selectedAction==='PAUSE')return 'PAUSE_TEST';
    if(selectedAction==='SCALE_UP')return 'BUDGET_SCALE_UP';
    if(selectedAction==='REDUCE_BUDGET')return 'BUDGET_REDUCTION';
    if(raw==='generate_derivative_creative')return 'WINNER_EXTENSION';
    if(['generate_creative','generate_repair_creative'].includes(raw)||String(recommendation.diagnosis_type||'').includes('creative'))return 'CREATIVE_REPLACEMENT';
    return 'NEW_AD_TEST';
  }

  function recommendationDraft(recommendation, selectedAction) {
    const level=String(recommendation.object_level||'').toLowerCase(),objectId=String(recommendation.object_id||'');
    const canonicalAccountId=String(recommendation.account_id||recommendation.gle_scope_account_id||'').replace(/^act_/,'');
    const canonicalAdId=String(recommendation.source_ad_id||recommendation.ad_id||recommendation.gle_scope_ad_id||'');
    return {
      target_app:String(recommendation.target_app||recommendation.project||'linky').toLowerCase(),
      country:String(recommendation.country||''),platform:'meta',
      account_id:canonicalAccountId,source_report_id:String(recommendation.report_id||''),
      source_recommendation_id:String(recommendation.recommendation_id||''),
      source_campaign_id:level==='campaign'?objectId:String(recommendation.source_campaign_id||recommendation.campaign_id||''),
      source_adset_id:['adset','ad_set'].includes(level)?objectId:String(recommendation.source_adset_id||recommendation.adset_id||''),
      source_ad_id:canonicalAdId,
      source_creative_id:String(recommendation.source_creative_id||recommendation.creative_id||''),
      experiment_type:recommendationExperimentType(recommendation,selectedAction),
      hypothesis_json:{summary:String(recommendation.reason_zh||recommendation.diagnosis_type_zh||'验证系统优化建议'),recommended_action:selectedAction,evidence_only:true,cpi_target:Number(recommendation.cpi_target||recommendation.objective?.cpi_target||recommendation.evidence?.cpi_target||0)||null,initial_daily_budget:Number(recommendation.initial_daily_budget||recommendation.daily_budget||0)||null,targeting:recommendation.targeting||{}},
      primary_metric:'cpa',guardrail_metrics_json:['spend','real_bind_count','real_bind_cpa'],
      maturity_rule_json:{minimum_conversions:10,checkpoints:['D1','D3','D7']},
      stop_rule_json:{meta_writes_require_explicit_enablement:true}
    };
  }

  async function acceptRecommendation(recommendation, decision, options={}) {
    const action=String(decision.selected_action||'OBSERVE').toUpperCase();
    if(['OBSERVE','CHECK_DATA'].includes(action)) {
      return {message:action==='OBSERVE'?'已确认，系统会持续观察并在数据成熟后提醒你。':'已确认，系统已将数据复核放入待处理队列。'};
    }
    const body=recommendationDraft(recommendation,action);
    const requestHeaders=(phase,identity)=>options.batchId
      ?bulkRebuildHeaders(options.batchId,String(recommendation.recommendation_id||''),phase)
      :postHeaders(phase,identity);
    const experiment=await api('/api/ops/ad-data-dashboard/experiments/draft',{method:'POST',headers:requestHeaders('accepted-recommendation-draft',body),body:JSON.stringify(body)});
    try {
      await api(`/api/ops/growth/decisions/${encodeURIComponent(decision.decision_id)}/target`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({target_type:'EXPERIMENT',target_id:experiment.experiment_id})});
    } catch(error) {
      if(!(error.status===409&&String(error.message||'').includes('decision_not_bindable')))throw error;
    }
    let plan={};
    if(action==='CREATE_EXPERIMENT'){
      plan=await api(`/api/ops/ad-data-dashboard/experiments/${encodeURIComponent(experiment.experiment_id)}/rebuild-plan/prepare`,{method:'POST',headers:requestHeaders('accepted-recommendation-rebuild-plan',{experiment_id:experiment.experiment_id,recommendation_id:body.source_recommendation_id}),body:JSON.stringify({})});
    }
    if(options.open!==false)openWorkspace(experiment.experiment_id);
    return {
      experiment_id:experiment.experiment_id,
      plan_id:String(plan.plan_id||plan.operation_action_id||''),
      message:action==='CREATE_EXPERIMENT'?'已生成重建方案，请查看并审批。':'已确认，系统已自动建立跟踪闭环并带入建议与证据。'
    };
  }

  function bulkRebuildRecommendationSnapshot(recommendation) {
    const source=recommendation||{},snapshot={};
    [
      'recommendation_id','primary_action','action_type','object_level','object_id','account_id','gle_scope_account_id',
      'source_ad_id','ad_id','gle_scope_ad_id','target_app','project','country','report_id','source_campaign_id',
      'campaign_id','source_adset_id','adset_id','source_creative_id','creative_id','reason_zh','diagnosis_type_zh',
      'diagnosis_type','cpi_target','initial_daily_budget','daily_budget','targeting'
    ].forEach(key=>{if(source[key]!==undefined)snapshot[key]=source[key];});
    if(source.objective&&source.objective.cpi_target!==undefined)snapshot.objective={cpi_target:source.objective.cpi_target};
    if(source.evidence&&source.evidence.cpi_target!==undefined)snapshot.evidence={cpi_target:source.evidence.cpi_target};
    return snapshot;
  }

  function readBulkRebuildBatch() {
    for(const storageName of ['localStorage','sessionStorage']){
      try{const raw=window[storageName].getItem(BULK_REBUILD_STORAGE_KEY);if(raw)return JSON.parse(raw);}catch(_){}
    }
    return null;
  }

  function writeBulkRebuildBatch(batch) {
    batch.updated_at=new Date().toISOString();
    const serialized=JSON.stringify(batch);
    try{window.localStorage.setItem(BULK_REBUILD_STORAGE_KEY,serialized);try{window.sessionStorage.removeItem(BULK_REBUILD_STORAGE_KEY);}catch(_){}return true;}catch(_){}
    try{window.sessionStorage.setItem(BULK_REBUILD_STORAGE_KEY,serialized);return true;}catch(_){return false;}
  }

  function ensureBulkRebuildModal() {
    let node=document.getElementById('growthBulkRebuildModal');
    if(!node){node=document.createElement('section');node.id='growthBulkRebuildModal';node.className='growth-modal-layer growth-bulk-modal-layer';node.hidden=true;document.body.appendChild(node);}
    return node;
  }

  function closeBulkRebuildModal() {
    const node=document.getElementById('growthBulkRebuildModal');
    if(node){node.hidden=true;node.innerHTML='';}
  }

  function bulkRebuildItemStatus(item) {
    const labels={PENDING:'等待处理',RUNNING:'处理中',SUCCESS:'已创建暂停态对象',MANUAL_REVIEW:'结果待人工核对'};
    return labels[item.status]||item.status||'等待处理';
  }

  function renderBulkRebuildProgress(batch) {
    const node=document.querySelector('#growthBulkRebuildModal #growthBulkRebuildProgress');
    if(!node)return;
    const success=batch.items.filter(item=>item.status==='SUCCESS').length;
    const manual=batch.items.filter(item=>item.status==='MANUAL_REVIEW').length;
    const pending=batch.items.filter(item=>item.status==='PENDING').length;
    node.innerHTML=`<div class="growth-review-card"><b>批次 ${esc(batch.batch_id.slice(-8))}</b>成功 ${success} · 待核对 ${manual} · 未开始 ${pending}</div><div class="growth-timeline">${batch.items.map(item=>`<div><b>${esc(item.ad_name||item.ad_id)} · ${esc(bulkRebuildItemStatus(item))}</b><br>${esc(item.account_name||item.account_id||'')} · ${esc(item.ad_id||'')}${item.experiment_id?` · <button type="button" data-bulk-open-task="${esc(item.experiment_id)}">查看任务</button>`:''}${item.error?`<br>${esc(item.error)}`:''}</div>`).join('')}</div>`;
    node.querySelectorAll('[data-bulk-open-task]').forEach(button=>button.addEventListener('click',()=>{closeBulkRebuildModal();if(typeof window.setGleWorkspaceView==='function')window.setGleWorkspaceView('tasks',{focus:true});openWorkspace(button.getAttribute('data-bulk-open-task')||'');}));
  }

  async function executeBulkRebuildItem(batch,item) {
    const recommendation=item.recommendation,recommendationId=String(recommendation.recommendation_id||'');
    const decisionBody={
      recommendation_id:recommendationId,selected_action:'CREATE_EXPERIMENT',rejected_actions:[],
      decision_reason:{type:'OPERATOR_APPROVED',note:`批量重建审批 ${batch.batch_id}`},confidence:1
    };
    const decision=await api('/api/ops/growth/decisions',{method:'POST',headers:bulkRebuildHeaders(batch.batch_id,recommendationId,'decision'),body:JSON.stringify(decisionBody)});
    const accepted=await acceptRecommendation(recommendation,{...decision,selected_action:'CREATE_EXPERIMENT'},{batchId:batch.batch_id,open:false});
    item.experiment_id=String(accepted.experiment_id||'');item.plan_id=String(accepted.plan_id||'');
    if(!item.plan_id)throw new Error('重建方案标识未完整回读，系统已停止该条处理。');
    const approveBody={confirmation:'APPROVE_EXACT_PLAN'};
    await api(`/api/ops/ad-data-dashboard/meta-plans/${encodeURIComponent(item.plan_id)}/approve`,{method:'POST',headers:bulkRebuildHeaders(batch.batch_id,recommendationId,'approve'),body:JSON.stringify(approveBody)});
    const dryBody={execution_mode:'dry_run'};
    await api(`/api/ops/ad-data-dashboard/meta-plans/${encodeURIComponent(item.plan_id)}/execute`,{method:'POST',headers:bulkRebuildHeaders(batch.batch_id,recommendationId,'dry-run'),body:JSON.stringify(dryBody)});
    const liveBody={execution_mode:'live',confirmation:'CREATE_PAUSED_OBJECTS'};
    await api(`/api/ops/ad-data-dashboard/meta-plans/${encodeURIComponent(item.plan_id)}/execute`,{method:'POST',headers:bulkRebuildHeaders(batch.batch_id,recommendationId,'create-paused'),body:JSON.stringify(liveBody)});
  }

  async function runBulkRebuildBatch(batch,button) {
    button.disabled=true;button.textContent='正在逐条处理…';
    for(const item of batch.items){
      if(item.status!=='PENDING')continue;
      item.status='RUNNING';item.error='';
      if(!writeBulkRebuildBatch(batch)){item.status='MANUAL_REVIEW';item.error='浏览器无法保存处理进度，本条尚未发起，批次已安全停止。';renderBulkRebuildProgress(batch);break;}
      renderBulkRebuildProgress(batch);
      try{
        await executeBulkRebuildItem(batch,item);
        item.status='SUCCESS';
      }catch(error){
        item.status='MANUAL_REVIEW';item.error=readableError(error);
      }
      if(!writeBulkRebuildBatch(batch)){item.status='MANUAL_REVIEW';item.error='请求结果已返回，但浏览器无法保存结果；请先人工核对，本批次不会继续。';renderBulkRebuildProgress(batch);break;}
      renderBulkRebuildProgress(batch);
    }
    batch.status=batch.items.every(item=>item.status==='SUCCESS')?'SUCCESS':'MANUAL_REVIEW';
    writeBulkRebuildBatch(batch);renderBulkRebuildProgress(batch);
    button.textContent=batch.status==='SUCCESS'?'全部完成':'处理完成，存在待核对项';
    window.dispatchEvent(new CustomEvent('gle-bulk-rebuild-completed',{detail:{batch_id:batch.batch_id,status:batch.status}}));
    if(typeof window.refreshGleDecisionSurface==='function')await window.refreshGleDecisionSurface();
  }

  function showBulkRebuildModal(batch,{allowPending=true,persisted=true}={}) {
    const pending=batch.items.filter(item=>item.status==='PENDING').length;
    const node=ensureBulkRebuildModal();
    node.innerHTML=`<section class="growth-modal" role="dialog" aria-modal="true" aria-labelledby="growthBulkRebuildTitle"><header class="growth-modal-head"><div><b id="growthBulkRebuildTitle">批量审批重建投放 · ${batch.items.length}</b><small>只处理投放配置重建，不混入暂停、调预算、放量或素材动作</small></div><button type="button" class="growth-icon-button" data-bulk-modal-close aria-label="关闭">×</button></header><div class="growth-modal-body"><div class="growth-safety"><b>确认结果</b> 每条广告独立回读来源、审批和安全演练；只创建暂停态 Meta 对象，不会开启投放或产生新增花费。结果不确定的条目会立即停止且不会自动重试。</div>${persisted?'':'<div class="growth-error">浏览器无法保存批次进度，本次尚未执行任何广告操作。请清理站点存储空间或更换正常浏览器窗口后重试。</div>'}<div id="growthBulkRebuildProgress"></div></div><footer class="growth-modal-foot"><button type="button" data-bulk-modal-close>关闭</button>${persisted&&allowPending&&pending?`<button type="button" class="growth-primary" id="growthConfirmBulkRebuild">${pending===batch.items.length?'确认并创建':'继续处理'} ${pending} 组暂停态广告</button>`:''}</footer></section>`;
    node.hidden=false;
    node.querySelectorAll('[data-bulk-modal-close]').forEach(button=>button.addEventListener('click',closeBulkRebuildModal));
    renderBulkRebuildProgress(batch);
    node.querySelector('#growthConfirmBulkRebuild')?.addEventListener('click',event=>runBulkRebuildBatch(batch,event.currentTarget));
  }

  function openBulkRebuildApproval(candidates) {
    const eligible=(Array.isArray(candidates)?candidates:[]).filter(item=>
      item&&item.batch_action==='repair_delivery_config'&&item.recommendation&&item.recommendation.recommendation_id
    );
    if(!eligible.length){showModalError('当前没有可批量审批的重建投放方案。');return false;}
    const eligibleIds=eligible.map(item=>String(item.recommendation.recommendation_id||'')).sort();
    const existing=readBulkRebuildBatch();
    const existingIds=(existing&&Array.isArray(existing.items)?existing.items:[]).map(item=>String(item.recommendation&&item.recommendation.recommendation_id||'')).sort();
    if(existing&&stableJson(existingIds)===stableJson(eligibleIds)){
      existing.items.forEach(item=>{
        item.recommendation=bulkRebuildRecommendationSnapshot(item.recommendation);
        if(item.status==='RUNNING'){
          item.status='MANUAL_REVIEW';
          item.error='页面在请求处理中断开，真实结果需要先核对；系统不会自动重试该条。';
        }
      });
      const persisted=writeBulkRebuildBatch(existing);showBulkRebuildModal(existing,{allowPending:true,persisted});return persisted;
    }
    const batch={
      batch_id:`gle-bulk-${Date.now()}-${stableHash(eligible.map(item=>item.recommendation.recommendation_id))}`,
      status:'PENDING',created_at:new Date().toISOString(),items:eligible.map(item=>({
        status:'PENDING',error:'',experiment_id:'',plan_id:'',account_id:String(item.account_id||''),
        account_name:String(item.account_name||''),ad_id:String(item.ad_id||''),ad_name:String(item.ad_name||''),
        recommendation:bulkRebuildRecommendationSnapshot(item.recommendation)
      }))
    };
    const persisted=writeBulkRebuildBatch(batch);
    showBulkRebuildModal(batch,{allowPending:true,persisted});
    return persisted;
  }

  async function openTechnicalOverview() {
    showModal(`<section class="growth-modal"><header class="growth-modal-head"><div><b>技术与知识记录</b><small>默认折叠，不占用日常操作界面</small></div><button type="button" class="growth-icon-button" data-modal-close>×</button></header><div class="growth-modal-body"><h3>Context → Decision → Action → Outcome → Knowledge</h3><p>Episode 与 Knowledge 用于审计、复盘和相似证据，不建立第二套业务工作台。</p><div class="growth-review-card"><b>知识学习</b><button type="button" class="growth-secondary" id="growthMinePatterns">挖掘候选模式</button><div id="growthTechnicalStatus" class="growth-notice" hidden></div></div><div class="growth-review-card" style="margin-top:12px"><b>Learning / Adaptive</b><div class="growth-actions" style="min-height:58px;justify-content:flex-start;border:0"><button type="button" id="growthRecommendStrategy">生成策略建议</button><select id="growthSimulationAction" aria-label="模拟动作"><option>OBSERVE</option><option>CHECK_DATA</option><option>CREATE_EXPERIMENT</option><option>PAUSE</option></select><button type="button" id="growthRunSimulation">运行证据模拟</button></div><div id="growthAdaptiveStatus" class="growth-notice" hidden></div><div id="growthStrategyResult"></div><div id="growthSimulationResult"></div></div><div class="growth-safety">RAW 候选；仍需人工审核。只有 APPROVED 且为 OBSERVE/CHECK_DATA 的本地动作可以低风险执行；涉及 Meta 或其他写动作时不提供自动执行。Plan Preview / Approval 使用单人二次确认。</div></div><footer class="growth-modal-foot"><button type="button" class="growth-primary" data-modal-close>关闭</button></footer></section>`);
    document.getElementById('growthMinePatterns').addEventListener('click',minePatterns);
    document.getElementById('growthRecommendStrategy').addEventListener('click',generateStrategyRecommendation);
    document.getElementById('growthRunSimulation').addEventListener('click',runStrategySimulation);
  }

  async function minePatterns() {
    const button=document.getElementById('growthMinePatterns'),status=document.getElementById('growthTechnicalStatus');
    const body={minimum_support:2};
    try {button.disabled=true;status.hidden=false;status.textContent='正在从 Completed Episode 挖掘候选模式…';const payload=await api('/api/ops/growth/patterns/mine',{method:'POST',headers:postHeaders('patterns-mine',body),body:JSON.stringify(body)});status.textContent=`已生成 ${Number(payload.count||0)} 条 RAW 候选；仍需人工审核。重复点击会复用本次结果。`;}
    catch(error){status.hidden=false;status.textContent=readableError(error);button.disabled=false;}
  }

  function technicalContext() {
    const episodeDetail=((state.detail||{}).growth_lineage||{}).episode_detail||{};
    return {context:episodeDetail.context||{},decision:episodeDetail.decision||{}};
  }

  async function generateStrategyRecommendation() {
    const button=document.getElementById('growthRecommendStrategy'),status=document.getElementById('growthAdaptiveStatus');
    const current=technicalContext(),contextId=current.context.context_snapshot_id;
    if(!contextId){status.hidden=false;status.textContent='当前实验尚未绑定 Context，不能生成策略建议。';return;}
    const body={context_snapshot_id:contextId};
    try{button.disabled=true;status.hidden=false;status.textContent='正在匹配 ACTIVE Knowledge…';const item=await api('/api/ops/growth/strategy-recommendations',{method:'POST',headers:postHeaders('strategy-recommendation',body),body:JSON.stringify(body)});renderStrategyRecommendation(item,current);status.textContent='策略建议已生成；必须人工确认后才可能执行。重复点击会复用本次结果。';}
    catch(error){status.hidden=false;status.textContent=readableError(error);button.disabled=false;}
  }

  function renderStrategyRecommendation(item,current) {
    const node=document.getElementById('growthStrategyResult'),rationale=item.rationale_json||{};
    const lowRiskApproved=item.status === 'APPROVED' && LOW_RISK_AUTOMATIC_ACTIONS.includes(item.action_type);
    node.innerHTML=`<div class="growth-technical" style="padding:12px"><b>策略建议 · ${esc(item.action_type)}</b><p>状态 ${esc(item.status)} · support_count=${esc(rationale.support_count??'-')} · historical_success_rate=${esc(rationale.historical_success_rate??'-')} · causal_claim=false</p><div class="growth-actions" style="min-height:52px;border:0">${item.status==='PROPOSED'?'<button type="button" data-strategy-status="REJECTED">拒绝</button><button type="button" class="growth-primary" data-strategy-status="APPROVED">人工确认建议</button>':''}${lowRiskApproved?'<button type="button" class="growth-primary" data-execute-strategy>执行低风险本地动作</button>':''}</div></div>`;
    node.querySelectorAll('[data-strategy-status]').forEach(button=>button.addEventListener('click',async()=>{button.disabled=true;try{const updated=await api(`/api/ops/growth/strategy-recommendations/${encodeURIComponent(item.strategy_recommendation_id)}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:button.dataset.strategyStatus})});renderStrategyRecommendation(updated,current);}catch(error){document.getElementById('growthAdaptiveStatus').textContent=readableError(error);button.disabled=false;}}));
    const execute=node.querySelector('[data-execute-strategy]');
    if(execute)execute.addEventListener('click',async()=>{const body={decision_id:current.decision.decision_id};try{execute.disabled=true;const action=await api(`/api/ops/growth/strategy-recommendations/${encodeURIComponent(item.strategy_recommendation_id)}/execute`,{method:'POST',headers:postHeaders('strategy-execute',{strategy_recommendation_id:item.strategy_recommendation_id,...body}),body:JSON.stringify(body)});document.getElementById('growthAdaptiveStatus').textContent=`低风险本地动作已完成：${action.status}`;}catch(error){document.getElementById('growthAdaptiveStatus').textContent=readableError(error);execute.disabled=false;}});
  }

  async function runStrategySimulation() {
    const button=document.getElementById('growthRunSimulation'),status=document.getElementById('growthAdaptiveStatus'),current=technicalContext();
    const contextId=current.context.context_snapshot_id;
    if(!contextId){status.hidden=false;status.textContent='当前实验尚未绑定 Context，不能运行证据模拟。';return;}
    const body={context_snapshot_id:contextId,proposed_action:document.getElementById('growthSimulationAction').value};
    try{button.disabled=true;status.hidden=false;status.textContent='正在读取历史 Episode 频率…';const item=await api('/api/ops/growth/simulations',{method:'POST',headers:postHeaders('strategy-simulation',body),body:JSON.stringify(body)});document.getElementById('growthSimulationResult').innerHTML=`<div class="growth-technical" style="padding:12px"><b>证据模拟 · ${esc(item.proposed_action)}</b><p>sample_count=${esc(item.sample_count)} · expected_success_rate=${esc(item.expected_success_rate)} · risk_level=${esc(item.risk_level)} · causal_claim=false</p></div>`;status.textContent='模拟已完成；结果仅表示历史相关性。重复点击会复用本次结果。';}
    catch(error){status.hidden=false;status.textContent=readableError(error);button.disabled=false;}
  }

  function showModal(html) {
    const launchPanel=document.getElementById('growthLaunchPanel');
    let node=launchPanel&&!launchPanel.hidden?document.getElementById('growthLaunchModal'):document.getElementById('growthModal');
    if(!node&&launchPanel&&!launchPanel.hidden){node=document.createElement('section');node.id='growthLaunchModal';node.className='growth-modal-layer';launchPanel.appendChild(node);}
    if(!node){console.error('growth_modal_container_missing');return false;}
    const workspace=document.getElementById('growthWorkspacePanel');
    const inline=node.id==='growthModal'&&isEmbeddedWorkspace();
    node.innerHTML=html;node.hidden=false;
    if(inline){
      workspace?.classList.add('has-inline-action');
      node.classList.add('growth-inline-action');
      node.setAttribute('role','complementary');
      const dialog=node.querySelector('.growth-modal');
      const title=String(dialog?.querySelector('.growth-modal-head b')?.textContent||'处理当前任务').trim();
      dialog?.setAttribute('role','region');dialog?.removeAttribute('aria-modal');dialog?.setAttribute('aria-label',title);
      const context=document.createElement('div');
      context.className='growth-inline-context';
      context.innerHTML='<span>当前任务的下一步</span><small>在这里核对影响范围并确认；任务列表和当前上下文会保留。</small>';
      node.prepend(context);
      node.setAttribute('aria-label',`当前任务操作：${title}`);
      node.scrollIntoView({block:'nearest'});
    }
    node.querySelectorAll('[data-modal-close]').forEach(button=>button.addEventListener('click',closeModal));
    return true;
  }

  function showModalError(message) {
    const body=document.querySelector('#growthLaunchModal:not([hidden]) .growth-modal-body,#growthModal:not([hidden]) .growth-modal-body');if(!body)return;let node=body.querySelector('.growth-error');if(!node){node=document.createElement('div');node.className='growth-error';body.appendChild(node);}node.textContent=message;
  }

  function closeModal() { clearLaunchBatchWorkflowTimer();launchState.batchWorkflowPlanId='';document.getElementById('growthWorkspacePanel')?.classList.remove('has-inline-action');['growthModal','growthLaunchModal'].forEach(id=>{const node=document.getElementById(id);if(node){node.hidden=true;node.innerHTML='';node.classList.remove('growth-inline-action');node.removeAttribute('role');node.removeAttribute('aria-label');}}); }

  async function openEpisode(id) { openWorkspace(); const node=document.getElementById('growthDetail');node.innerHTML='<div class="growth-empty"><div><b>正在读取 Episode</b></div></div>';try{const detail=await api(`/api/ops/growth/episodes/${encodeURIComponent(id)}`);node.innerHTML=`<div class="growth-detail-head"><div><h2>Episode 复盘</h2><p>${esc(id)}</p></div><span class="growth-chip">${esc(statusLabel((detail.episode||{}).status))}</span></div><details class="growth-technical" open><summary>Context → Knowledge</summary><pre>${esc(pretty(detail))}</pre></details>${(detail.episode||{}).status==='COMPLETED'?'<div class="growth-notice">Completed Episode 已冻结；仍可审核或归档知识。</div>':''}`;}catch(error){node.innerHTML=`<div class="growth-error">${esc(readableError(error))}</div>`;}}
  async function openKnowledge(id) { openWorkspace(); const node=document.getElementById('growthDetail');node.innerHTML='<div class="growth-empty"><div><b>正在读取知识证据</b></div></div>';try{const detail=await api(`/api/ops/growth/knowledge/${encodeURIComponent(id)}`);node.innerHTML=`<div class="growth-detail-head"><div><h2>知识证据</h2><p>${esc(id)}</p></div><span class="growth-chip">${esc(statusLabel((detail.knowledge||{}).status))}</span></div><details class="growth-technical" open><summary>相似 Episode 与证据</summary><pre>${esc(pretty(detail))}</pre></details>`;}catch(error){node.innerHTML=`<div class="growth-error">${esc(readableError(error))}</div>`;}}
  async function openExperiment(id) { return openWorkspace(id); }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install); else install();
  window.GrowthWorkspace = {openEpisode,openKnowledge,openExperiment,openAdExperiment,acceptRecommendation,openBulkRebuildApproval,readBulkRebuildBatch,refresh:loadList,open:openWorkspace,openTasks:openLaunchWorkspace,setCoverageScope,showQueue:showEmbeddedQueue};
})();
