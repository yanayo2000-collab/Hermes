(function () {
  'use strict';

  const ACTION_MAP = {
    generate_derivative_creative: 'CREATE_EXPERIMENT',
    generate_creative: 'CREATE_EXPERIMENT',
    pause: 'PAUSE',
    scale_up: 'SCALE_UP',
    reduce_budget: 'REDUCE_BUDGET',
    observe: 'OBSERVE',
    inspect_data_quality: 'CHECK_DATA',
    manual_review: 'CHECK_DATA'
  };
  const REASONS = [
    ['CREATIVE_FATIGUE', '素材疲劳'],
    ['COST_INCREASE', '成本上升'],
    ['FUNNEL_DEGRADATION', '漏斗转化下降'],
    ['STOP_LOSS', '止损'],
    ['OTHER', '其他']
  ];
  const ACTION_EFFECTS = {
    CREATE_EXPERIMENT: '确认后创建实验草稿并关联这条建议；还需要检查方案、审批和 dry-run，不会直接修改 Meta 广告。',
    PAUSE: '确认后系统建立暂停实验草稿并完成安全检查；真实 Meta 写入开启前不会改动广告。',
    SCALE_UP: '确认后系统建立扩量实验草稿并完成预算护栏；真实 Meta 写入开启前不会改动预算。',
    REDUCE_BUDGET: '确认后系统建立降预算实验草稿并完成止损检查；真实 Meta 写入开启前不会改动预算。',
    OBSERVE: '确认后系统持续观察数据，到期后再把结论交给你，不修改广告。',
    CHECK_DATA: '确认后将这条表现偏弱广告加入经营数据复核队列；不会暂停、降预算、放量或修改 Meta。'
  };
  let active = null;
  let activeUiKey = '';
  let activeIdempotencyKey = '';
  let previousFocus = null;

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
  }

  function ensureModal() {
    if (document.getElementById('growthDecisionModal')) return;
    const style = document.createElement('style');
    style.textContent = `
      .growth-decision-backdrop{position:fixed;inset:0;z-index:10020;background:rgba(15,23,42,.48);display:none;align-items:center;justify-content:center;padding:12px}
      .growth-decision-backdrop.is-open{display:flex}.growth-decision-modal{width:min(860px,100%);max-height:calc(100dvh - 24px);display:grid;grid-template-rows:auto minmax(0,1fr) auto;overflow:hidden;background:#fff;border:1px solid rgba(15,23,42,.08);border-radius:16px;box-shadow:0 28px 80px rgba(15,23,42,.28)}
      .growth-decision-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;padding:15px 18px 13px;border-bottom:1px solid #e5e7eb}.growth-decision-head h2{font-size:18px;line-height:1.25;margin:0;color:#101828}.growth-decision-head p{margin:4px 0 0;color:#667085;font-size:12px}.growth-decision-close{display:grid!important;place-items:center!important;flex:0 0 auto!important;width:32px!important;height:32px!important;min-width:32px!important;min-height:32px!important;margin:0!important;padding:0!important;border:1px solid #e2e8f0!important;border-radius:9px!important;background:#f8fafc!important;color:#475569!important;font:800 18px/1 sans-serif!important;box-shadow:none!important;cursor:pointer}
      .growth-decision-body{min-height:0;overflow-y:auto;padding:14px 18px;display:grid;gap:12px}.growth-decision-impact{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;padding:10px 12px;border:1px solid #c7d7fe;border-radius:10px;background:#f7f9ff;color:#344054}.growth-decision-impact strong{grid-row:1/3;color:#1d4ed8;font-size:12px;white-space:nowrap}.growth-decision-impact span{font-size:12px;font-weight:780}.growth-decision-impact small{font-size:11px;color:#667085;line-height:1.35}
      .growth-decision-grid{display:grid;grid-template-columns:minmax(0,.92fr) minmax(0,1.08fr);gap:12px;align-items:start}.growth-decision-step{display:grid;gap:9px;min-width:0;padding:12px;border:1px solid #e2e8f0;border-radius:11px;background:#fff}.growth-decision-step h3{font-size:13px;margin:0;color:#344054}.growth-decision-fields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.growth-decision-field{display:grid;gap:4px;min-width:0}.growth-decision-field.is-wide{grid-column:1/-1}.growth-decision-field>span{font-size:11px;color:#667085;font-weight:760}.growth-decision-plan{display:grid;gap:7px}.growth-decision-plan div{padding:8px 10px;border-radius:8px;background:#f8fafc}.growth-decision-plan span{display:block;color:#667085;font-size:10px}.growth-decision-plan b{display:block;margin-top:2px;color:#101828;font-size:12px}.growth-decision-adjust summary{color:#667085;font-size:11px;font-weight:760;cursor:pointer}.growth-decision-adjust .growth-decision-fields{margin-top:8px}
      .growth-decision-step select,.growth-decision-step textarea{width:100%!important;box-sizing:border-box!important;margin:0!important;border:1px solid #cbd5e1!important;border-radius:8px!important;background:#fff!important;color:#101828!important;padding:7px 9px!important;font:600 12px/1.3 inherit!important;box-shadow:none!important}.growth-decision-step select{height:36px!important;min-height:36px!important}.growth-decision-step textarea{height:54px!important;min-height:54px!important;resize:none!important}
      .growth-decision-effect{display:grid;gap:2px;padding:8px 10px;border-radius:8px;background:#f8fafc}.growth-decision-effect span{font-size:10px;color:#667085;font-weight:760}.growth-decision-effect strong{font-size:11px;line-height:1.4;color:#344054}
      .growth-decision-evidence{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}.growth-decision-evidence div{min-width:0;background:#f8fafc;border:1px solid #edf1f5;border-radius:8px;padding:7px 8px}.growth-decision-evidence span{display:block;color:#64748b;font-size:10px}.growth-decision-evidence b{display:block;margin-top:2px;color:#0f172a;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
      .growth-decision-similar-details{border:1px solid #e2e8f0;border-radius:9px;background:#fff}.growth-decision-similar-details summary{padding:8px 10px;color:#475569;font-size:11px;font-weight:780;cursor:pointer}.growth-decision-similar{display:grid;gap:6px;padding:0 10px 9px}.growth-decision-similar article{border:1px solid #e2e8f0;border-radius:8px;padding:8px 9px;background:#f8fafc;font-size:11px;color:#475467}.growth-decision-similar strong{color:#101828}
      .growth-decision-foot{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:11px 18px;border-top:1px solid #e5e7eb;background:#fff}.growth-decision-status{font-size:11px;line-height:1.35;color:#64748b}.growth-decision-foot-actions{display:flex;align-items:center;gap:8px}.growth-decision-later,.growth-decision-submit{min-height:36px!important;margin:0!important;border-radius:9px!important;padding:8px 14px!important;font:800 12px/1.2 inherit!important;box-shadow:none!important;white-space:nowrap!important;cursor:pointer}.growth-decision-later{border:1px solid #d0d5dd!important;background:#fff!important;color:#475467!important}.growth-decision-submit{border:0!important;background:#111827!important;color:#fff!important}.growth-decision-submit:disabled{opacity:.55!important;cursor:not-allowed!important}
      .growth-bulk-modal{width:min(680px,100%)}.growth-bulk-summary{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.growth-bulk-summary div{padding:10px 12px;border:1px solid #e2e8f0;border-radius:10px;background:#f8fafc}.growth-bulk-summary span{display:block;color:#667085;font-size:11px}.growth-bulk-summary strong{display:block;margin-top:3px;color:#101828;font-size:18px}.growth-bulk-list{display:grid;gap:0;border:1px solid #e2e8f0;border-radius:10px;overflow:hidden}.growth-bulk-list div{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:9px 11px;border-bottom:1px solid #edf2f7;font-size:12px;color:#344054}.growth-bulk-list div:last-child{border-bottom:0}.growth-bulk-list b{color:#101828}.growth-bulk-safe-note{padding:9px 11px;border:1px solid #abefc6;border-radius:9px;background:#ecfdf3;color:#027a48;font-size:11px;line-height:1.45}.growth-bulk-result{padding:9px 11px;border-radius:9px;background:#f8fafc;color:#475467;font-size:12px;line-height:1.45}
      [data-growth-decision]{margin:0!important;background:#111827!important;color:#fff!important;border-color:#111827!important}
      @media(max-width:720px){.growth-decision-grid{grid-template-columns:1fr}.growth-decision-impact{grid-template-columns:1fr;gap:4px}.growth-decision-impact strong{grid-row:auto}.growth-decision-modal{max-height:calc(100dvh - 12px)}.growth-decision-body{padding:12px}.growth-decision-foot{padding:10px 12px}}
    `;
    document.head.appendChild(style);
    const modal = document.createElement('div');
    modal.id = 'growthDecisionModal';
    modal.className = 'growth-decision-backdrop';
    modal.setAttribute('aria-hidden', 'true');
    modal.innerHTML = `
      <section class="growth-decision-modal" role="dialog" aria-modal="true" aria-labelledby="growthDecisionTitle">
        <header class="growth-decision-head"><div><h2 id="growthDecisionTitle">系统优化方案待确认</h2><p id="growthDecisionSubtitle"></p></div><button class="growth-decision-close" type="button" aria-label="关闭">×</button></header>
        <div class="growth-decision-body">
          <section class="growth-decision-impact" aria-label="操作说明"><strong>系统已完成分析</strong><span>你只需确认是否接受这个方案。</span><small>确认后系统会自动建立跟踪闭环并带入建议与证据；不需要填写技术 ID，也不会直接修改 Meta 广告。</small></section>
          <div class="growth-decision-grid">
            <section class="growth-decision-step"><h3>系统给出的方案</h3><div class="growth-decision-plan" id="growthDecisionPlan"></div><div class="growth-decision-effect"><span>你确认后</span><strong id="growthDecisionEffect"></strong></div><details class="growth-decision-adjust"><summary>需要时调整处理方式</summary><div class="growth-decision-fields"><label class="growth-decision-field" for="growthDecisionAction"><span>处理方式</span><select id="growthDecisionAction"><option value="CREATE_EXPERIMENT">创建实验</option><option value="PAUSE">暂停止损</option><option value="SCALE_UP">扩大预算</option><option value="REDUCE_BUDGET">降低预算</option><option value="OBSERVE">继续观察</option><option value="CHECK_DATA">检查数据</option></select></label><label class="growth-decision-field" for="growthDecisionNote"><span>补充说明（可选）</span><textarea id="growthDecisionNote" placeholder="例如：先观察到 D3"></textarea></label></div></details><select id="growthDecisionReason" hidden>${REASONS.map(item=>`<option value="${item[0]}">${item[1]}</option>`).join('')}</select><select id="growthDecisionConfidence" hidden><option value="0.8">高</option><option value="0.6" selected>中</option><option value="0.3">低</option></select></section>
            <section class="growth-decision-step"><h3>系统判断依据</h3><div class="growth-decision-evidence" id="growthDecisionEvidence"></div></section>
          </div>
          <details class="growth-decision-similar-details"><summary>参考过往结果（可选）</summary><div class="growth-decision-similar" id="growthDecisionSimilar"><div class="growth-decision-status">正在匹配历史结果…</div></div></details>
        </div>
        <footer class="growth-decision-foot"><span class="growth-decision-status" id="growthDecisionStatus" aria-live="polite">等待你确认；真实 Meta 写入当前关闭。</span><div class="growth-decision-foot-actions"><button class="growth-decision-later" type="button">暂不处理</button><button class="growth-decision-submit" type="button">确认并交给系统</button></div></footer>
      </section>`;
    document.body.appendChild(modal);
    modal.querySelector('.growth-decision-close').addEventListener('click', close);
    modal.querySelector('.growth-decision-later').addEventListener('click', close);
    modal.addEventListener('click', event => { if (event.target === modal) close(); });
    modal.querySelector('#growthDecisionAction').addEventListener('change', updateActionEffect);
    modal.querySelector('.growth-decision-submit').addEventListener('click', submit);
    const bulkModal = document.createElement('div');
    bulkModal.id = 'growthBulkDecisionModal';
    bulkModal.className = 'growth-decision-backdrop';
    bulkModal.setAttribute('aria-hidden', 'true');
    bulkModal.innerHTML = `<section class="growth-decision-modal growth-bulk-modal" role="dialog" aria-modal="true" aria-labelledby="growthBulkDecisionTitle"><header class="growth-decision-head"><div><h2 id="growthBulkDecisionTitle">确认加入数据复核队列</h2><p>一次确认，不需要逐条打开</p></div><button class="growth-decision-close growth-bulk-close" type="button" aria-label="关闭">×</button></header><div class="growth-decision-body"><div class="growth-bulk-summary" id="growthBulkSummary"></div><div class="growth-bulk-safe-note">本次只会把表现偏弱广告加入经营数据复核队列。Meta 写入 0：不会暂停、降预算、放量或修改广告。</div><div class="growth-bulk-list" id="growthBulkList"></div><div class="growth-bulk-result" id="growthBulkResult" aria-live="polite">等待确认。</div></div><footer class="growth-decision-foot"><span class="growth-decision-status">提交后可在“已交给系统”中一次查看。</span><div class="growth-decision-foot-actions"><button class="growth-decision-later growth-bulk-close" type="button">取消</button><button class="growth-decision-submit growth-bulk-submit" type="button">确认加入复核队列</button></div></footer></section>`;
    document.body.appendChild(bulkModal);
    bulkModal.querySelectorAll('.growth-bulk-close').forEach(button => button.addEventListener('click', closeBulk));
    bulkModal.addEventListener('click', event => { if (event.target === bulkModal) closeBulk(); });
    bulkModal.querySelector('.growth-bulk-submit').addEventListener('click', submitBulk);
  }

  function recommendationById(id) {
    try { return dailyRecommendationById[id] || null; } catch (_) { return null; }
  }

  function open(id) {
    ensureModal();
    active = recommendationById(id);
    if (!active) return;
    if (String(active.data_origin || 'LEGACY').toUpperCase() === 'LEGACY') return;
    activeUiKey = id;
    previousFocus = document.activeElement;
    activeIdempotencyKey = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : `growth-${Date.now()}-${Math.random()}`;
    const modal = document.getElementById('growthDecisionModal');
    document.getElementById('growthDecisionSubtitle').textContent = active.object_name || active.object_id || active.recommendation_id;
    document.getElementById('growthDecisionReason').value = reasonFromRecommendation(active);
    document.getElementById('growthDecisionAction').value = actionFromRecommendation(active);
    document.getElementById('growthDecisionConfidence').value = confidenceValue(active.confidence);
    document.getElementById('growthDecisionNote').value = '';
    renderPendingState();
    document.getElementById('growthDecisionPlan').innerHTML = planHtml(active);
    document.getElementById('growthDecisionEvidence').innerHTML = evidenceHtml(active);
    document.getElementById('growthDecisionSimilar').innerHTML = '<div class="growth-decision-status">正在匹配历史结果…</div>';
    const submitButton = modal.querySelector('.growth-decision-submit');
    submitButton.textContent = '确认并交给系统';
    submitButton.disabled = true;
    updateActionEffect();
    modal.classList.add('is-open');
    modal.setAttribute('aria-hidden', 'false');
    window.setTimeout(() => document.getElementById('growthDecisionReason').focus(), 0);
    if (active.decision_state && active.decision_state.decision_id) {
      renderAcceptedState(active.decision_state);
    }
    loadDecisionPreview(active.recommendation_id);
  }

  function renderPendingState() {
    document.getElementById('growthDecisionTitle').textContent = '系统优化方案待确认';
    document.querySelector('.growth-decision-impact').innerHTML = '<strong>系统已完成分析</strong><span>你只需确认是否接受这个方案。</span><small>确认后系统会自动建立跟踪闭环并带入建议与证据；不需要填写技术 ID，也不会直接修改 Meta 广告。</small>';
    document.getElementById('growthDecisionStatus').textContent = '正在读取当前处理状态…';
    document.querySelector('.growth-decision-adjust').hidden = false;
    document.querySelector('.growth-decision-later').textContent = '暂不处理';
    const button = document.querySelector('#growthDecisionModal .growth-decision-submit');
    button.dataset.acceptedNavigation = '0';
  }

  function acceptedMessage(decision) {
    const action = String(decision.selected_action || '').toUpperCase();
    if (action === 'OBSERVE') return '已交给系统，系统会持续观察并在数据成熟后提醒你。';
    if (action === 'CHECK_DATA') return '已交给系统，数据复核已进入待处理队列。';
    if (decision.target_type === 'EXPERIMENT' && decision.target_id) return '已交给系统，跟踪实验已经建立，可在广告任务中查看进度。';
    return '已交给系统，后续处理状态会由系统持续更新。';
  }

  function markAccepted(decision) {
    if (active) active.decision_state = decision;
    if (active && typeof window.applyDailyRecommendationDecisionState === 'function') {
      window.applyDailyRecommendationDecisionState(active.recommendation_id || activeUiKey, decision);
    }
    document.querySelectorAll('[data-growth-decision]').forEach(entry => {
      if (entry.getAttribute('data-growth-decision') !== activeUiKey) return;
      entry.disabled = false;
      entry.textContent = '已交给系统';
      entry.title = '点击查看当前处理状态';
    });
  }

  async function refreshRecommendationPanel() {
    if (typeof window.refreshDailyRecommendationPanel !== 'function') return;
    try { await window.refreshDailyRecommendationPanel(); } catch (_) {}
  }

  function renderAcceptedState(decision) {
    markAccepted(decision);
    document.getElementById('growthDecisionTitle').textContent = '方案已交给系统';
    document.querySelector('.growth-decision-impact').innerHTML = '<strong>无需再次确认</strong><span>系统已接手这条方案。</span><small>当前状态和下一步会保留在这里，不会因为关闭页面而消失。</small>';
    document.getElementById('growthDecisionStatus').textContent = `${acceptedMessage(decision)} 已从“待确认”移到“已交给系统”。`;
    document.querySelector('.growth-decision-adjust').hidden = true;
    document.querySelector('.growth-decision-later').textContent = '关闭';
    const button = document.querySelector('#growthDecisionModal .growth-decision-submit');
    button.textContent = '查看已交给系统';
    button.disabled = false;
    button.dataset.acceptedNavigation = '1';
  }

  function close() {
    const modal = document.getElementById('growthDecisionModal');
    if (!modal) return;
    modal.classList.remove('is-open');
    modal.setAttribute('aria-hidden', 'true');
    if (previousFocus && typeof previousFocus.focus === 'function') previousFocus.focus();
  }

  function closeBulk() {
    const modal = document.getElementById('growthBulkDecisionModal');
    if (!modal) return;
    modal.classList.remove('is-open');
    modal.setAttribute('aria-hidden', 'true');
  }

  function safeBulkCandidates() {
    const rows = typeof window.dailyRecommendationRowsForBulk === 'function' ? window.dailyRecommendationRowsForBulk() : [];
    return (rows || []).filter(row => {
      const systemManaged = row.system_managed === true || String(row.management_state && row.management_state.mode || '') === 'SYSTEM_MANAGED';
      return String(row.data_origin || 'LEGACY').toUpperCase() !== 'LEGACY' && !systemManaged && !(row.decision_state && row.decision_state.decision_id) && actionFromRecommendation(row) === 'CHECK_DATA';
    });
  }

  function openBulk() {
    ensureModal();
    const candidates = safeBulkCandidates();
    if (!candidates.length) return;
    const modal = document.getElementById('growthBulkDecisionModal');
    const checkCount = candidates.length;
    document.getElementById('growthBulkSummary').innerHTML = `<div><span>加入数据复核</span><strong>${checkCount}</strong></div><div><span>Meta 写入</span><strong>0</strong></div>`;
    const visible = candidates.slice(0, 8);
    document.getElementById('growthBulkList').innerHTML = visible.map(row => `<div><b>${esc(row.object_name || row.object_id || row.recommendation_id)}</b><span>经营数据复核</span></div>`).join('') + (candidates.length > visible.length ? `<div><b>其余 ${candidates.length - visible.length} 条</b><span>同一复核范围</span></div>` : '');
    document.getElementById('growthBulkResult').textContent = `共 ${candidates.length} 条，等待一次确认。`;
    const button = modal.querySelector('.growth-bulk-submit');
    button.textContent = '确认加入复核队列';
    button.disabled = false;
    button.dataset.complete = '0';
    modal.classList.add('is-open');
    modal.setAttribute('aria-hidden', 'false');
  }

  async function submitBulk() {
    const candidates = safeBulkCandidates();
    const modal = document.getElementById('growthBulkDecisionModal');
    const button = modal.querySelector('.growth-bulk-submit');
    if (button.dataset.complete === '1') { closeBulk(); return; }
    const result = document.getElementById('growthBulkResult');
    button.disabled = true;
    let succeeded = 0;
    const failed = [];
    for (const row of candidates) {
      result.textContent = `正在处理 ${succeeded + failed.length + 1}/${candidates.length}…`;
      const key = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : `growth-bulk-${Date.now()}-${succeeded + failed.length}`;
      const body = {recommendation_id:row.recommendation_id,selected_action:actionFromRecommendation(row),rejected_actions:[],decision_reason:{type:reasonFromRecommendation(row),note:'批量确认安全方案'},confidence:Number(confidenceValue(row.confidence))};
      try {
        const response = await fetch('/api/ops/growth/decisions', {method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':key,'X-Request-ID':key},body:JSON.stringify(body)});
        const payload = await response.json().catch(() => ({}));
        let decision = payload;
        if (response.status === 409) {
          const previewResponse = await fetch(`/api/ops/growth/recommendations/${encodeURIComponent(row.recommendation_id)}/decision-preview`);
          const preview = await previewResponse.json().catch(() => ({}));
          if (!previewResponse.ok || !preview.existing_decision || !preview.existing_decision.decision_id) throw decisionError(response.status, payload);
          decision = preview.existing_decision;
        } else if (!response.ok) throw decisionError(response.status, payload);
        const accepted = {...decision, selected_action:decision.selected_action || body.selected_action};
        row.decision_state = accepted;
        if (typeof window.applyDailyRecommendationDecisionState === 'function') window.applyDailyRecommendationDecisionState(row.recommendation_id, accepted);
        succeeded += 1;
      } catch (error) {
        failed.push(row.object_name || row.object_id || row.recommendation_id);
      }
    }
    if (!failed.length) {
      await refreshRecommendationPanel();
      result.textContent = `已交给系统 ${succeeded} 条。可关闭后在“已交给系统”中统一查看。`;
      button.textContent = '完成';
      button.disabled = false;
      button.dataset.complete = '1';
      return;
    }
    result.textContent = `已成功 ${succeeded} 条，失败 ${failed.length} 条；失败项仍保留待确认，系统没有自动重试。`;
    button.textContent = '关闭';
    button.disabled = false;
    button.dataset.complete = '1';
  }

  function updateActionEffect() {
    const action = document.getElementById('growthDecisionAction');
    const effect = document.getElementById('growthDecisionEffect');
    if (action && effect) effect.textContent = ACTION_EFFECTS[action.value] || ACTION_EFFECTS.OBSERVE;
  }

  function reasonFromRecommendation(row) {
    const diagnosis = String(row.diagnosis_type || '').toLowerCase();
    if (diagnosis.includes('fatigue')) return 'CREATIVE_FATIGUE';
    if (diagnosis.includes('cost') || diagnosis.includes('cpa')) return 'COST_INCREASE';
    if (diagnosis.includes('funnel') || diagnosis.includes('im_')) return 'FUNNEL_DEGRADATION';
    if (String(row.primary_action || '').toLowerCase() === 'pause') return 'STOP_LOSS';
    return 'OTHER';
  }

  function actionFromRecommendation(row) {
    const canonical = typeof window.dailyRecommendationDecisionAction === 'function' ? String(window.dailyRecommendationDecisionAction(row) || '').toLowerCase() : '';
    const scorecard = row && row.evidence && row.evidence.scorecard || {};
    const fallback = String(row.primary_action || row.action_type || 'OBSERVE').toLowerCase();
    const raw = canonical || (fallback === 'observe' && String(scorecard.band || '').toLowerCase() === 'poor' ? 'manual_review' : fallback);
    return ACTION_MAP[raw] || (row.allow_generate_creative === true ? 'CREATE_EXPERIMENT' : 'OBSERVE');
  }

  function confidenceValue(value) {
    const normalized = String(value || '').toLowerCase();
    return normalized === 'high' ? '0.8' : (normalized === 'low' ? '0.3' : '0.6');
  }

  function evidenceHtml(row) {
    const ev = row.evidence || {};
    const scorecard = ev.scorecard || {};
    const entries = [
      ['安装数', ev.installs == null ? '-' : ev.installs],
      ['安装单价（CPI）', ev.cpi == null ? '-' : `$${Number(ev.cpi).toFixed(2)}`],
      ['CTR', ev.ctr == null ? '-' : `${(Number(ev.ctr) * 100).toFixed(2)}%`],
      ['真实入会', ev.real_bind_count == null ? '-' : ev.real_bind_count],
      ['入会单价（CPA）', ev.real_bind_cpa == null ? '-' : `$${Number(ev.real_bind_cpa).toFixed(2)}`]
    ];
    return entries.map(item => `<div><span>${esc(item[0])}</span><b title="${esc(item[1])}">${esc(item[1])}</b></div>`).join('');
  }

  function planHtml(row) {
    const action = actionFromRecommendation(row);
    const labels = {CREATE_EXPERIMENT:'创建广告实验',PAUSE:'暂停止损',SCALE_UP:'扩大预算',REDUCE_BUDGET:'降低预算',OBSERVE:'继续观察',CHECK_DATA:'检查数据'};
    return `<div><span>建议动作</span><b>${esc(labels[action] || action)}</b></div><div><span>作用对象</span><b>${esc(row.object_name || row.object_id || '-')}</b></div><div><span>系统判断</span><b>${esc(row.reason_zh || row.diagnosis_type_zh || row.status_tag || '-')}</b></div><div><span>证据把握</span><b>${esc(row.confidence_zh || row.confidence || '中')}</b></div>`;
  }

  async function loadDecisionPreview(recommendationId) {
    const node = document.getElementById('growthDecisionSimilar');
    try {
      const response = await fetch(`/api/ops/growth/recommendations/${encodeURIComponent(recommendationId)}/decision-preview`);
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error((payload.detail && (payload.detail.message || payload.detail.code)) || `HTTP ${response.status}`);
      const items = Array.isArray(payload.similar_episodes) ? payload.similar_episodes : [];
      if (payload.existing_decision && payload.existing_decision.decision_id) {
        renderAcceptedState(payload.existing_decision);
      } else {
        const button = document.querySelector('#growthDecisionModal .growth-decision-submit');
        button.textContent = '确认并交给系统';
        button.disabled = false;
        document.getElementById('growthDecisionStatus').textContent = '等待你确认；确认后由系统持续处理。';
      }
      if (!items.length) {
        node.innerHTML = '<div class="growth-decision-status">暂无可参考的已完成记录，本次判断不会借用不存在的历史依据。</div>';
        return;
      }
      node.innerHTML = items.map(item => `<article><strong>相似度 ${(Number(item.score || 0) * 100).toFixed(0)}%</strong> · ${esc(item.episode_id)}<br>结果：${esc(JSON.stringify(item.outcome || {}))}<br>经验：${esc(JSON.stringify(item.lesson || {}))}</article>`).join('');
    } catch (error) {
      node.innerHTML = `<div class="growth-decision-status">过往结果暂时不可用：${esc(error.message || error)}</div>`;
      document.getElementById('growthDecisionStatus').textContent = active && active.decision_state && active.decision_state.decision_id
        ? acceptedMessage(active.decision_state)
        : '当前处理状态读取失败，请稍后重试。';
    }
  }

  async function submit() {
    if (!active) return;
    const button = document.querySelector('#growthDecisionModal .growth-decision-submit');
    if (button.dataset.acceptedNavigation === '1') {
      close();
      if (typeof window.showDailyRecommendationHandedOff === 'function') {
        window.showDailyRecommendationHandedOff();
      }
      return;
    }
    const status = document.getElementById('growthDecisionStatus');
    button.disabled = true;
    status.textContent = '正在创建决策…';
    const body = {
      recommendation_id: active.recommendation_id,
      selected_action: document.getElementById('growthDecisionAction').value,
      rejected_actions: [],
      decision_reason: {
        type: document.getElementById('growthDecisionReason').value,
        note: document.getElementById('growthDecisionNote').value.trim()
      },
      confidence: Number(document.getElementById('growthDecisionConfidence').value)
    };
    try {
      const response = await fetch('/api/ops/growth/decisions', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Idempotency-Key': activeIdempotencyKey,
          'X-Request-ID': activeIdempotencyKey
        },
        body: JSON.stringify(body)
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw decisionError(response.status, payload);
      status.textContent = '已确认，正在建立后续跟踪闭环…';
      button.textContent = '正在交给系统';
      const acceptedDecision = {...payload, selected_action:body.selected_action};
      markAccepted(acceptedDecision);
      if (window.GrowthWorkspace && typeof window.GrowthWorkspace.acceptRecommendation === 'function') {
        const result = await window.GrowthWorkspace.acceptRecommendation(active, acceptedDecision);
        status.textContent = result && result.message ? result.message : '已确认，系统已开始后续处理。';
        if (result && result.experiment_id) close();
      } else {
        status.textContent = '已确认，系统已开始跟踪后续效果。';
      }
      renderAcceptedState({...acceptedDecision, target_type:acceptedDecision.target_type || (active.decision_state || {}).target_type, target_id:acceptedDecision.target_id || (active.decision_state || {}).target_id});
      await refreshRecommendationPanel();
    } catch (error) {
      if (error && error.alreadyDecided) {
        await loadDecisionPreview(active.recommendation_id);
        return;
      }
      const retryable = !error || !error.userMessage || error.retryable === true;
      status.textContent = error && error.userMessage ? error.userMessage : '网络连接中断。可以重试，系统会复用同一幂等键。';
      button.textContent = retryable ? '重试确认' : '确认并交给系统';
      button.disabled = !retryable;
    }
  }

  function decisionError(httpStatus, payload) {
    const detail = payload && payload.detail || {};
    const raw = String(detail.message || detail.code || '').toLowerCase();
    const error = new Error(raw || `HTTP ${httpStatus}`);
    error.retryable = false;
    if (httpStatus === 403) error.userMessage = '你没有创建增长决策的权限。请联系管理员授权。';
    else if (httpStatus === 404 || raw.includes('expired') || raw.includes('recommendation_not_found')) error.userMessage = '这条建议已过期或不再存在。请刷新近 7 天建议后重新选择。';
    else if (httpStatus === 409 && (raw.includes('already_decided') || raw.includes('already_executed'))) {
      error.userMessage = '这条方案已经交给系统，正在读取当前状态。';
      error.alreadyDecided = true;
    }
    else if (httpStatus === 409 || raw.includes('duplicate')) error.userMessage = '检测到重复提交。系统没有创建第二条决策，请刷新查看已有结果。';
    else if (httpStatus >= 500) {
      error.userMessage = '服务暂时不可用。可以安全重试，系统会复用同一幂等键。';
      error.retryable = true;
    } else error.userMessage = `无法创建决策：${detail.message || detail.code || `HTTP ${httpStatus}`}。`;
    return error;
  }

  document.addEventListener('click', event => {
    const bulkButton = event.target.closest('[data-growth-bulk-confirm]');
    if (bulkButton) { openBulk(); return; }
    const button = event.target.closest('[data-growth-decision]');
    if (button) open(button.getAttribute('data-growth-decision') || '');
  });
  document.addEventListener('keydown', event => { if (event.key === 'Escape') { close(); closeBulk(); } });
})();
