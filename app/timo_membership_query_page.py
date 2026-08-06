from __future__ import annotations


TIMO_MEMBERSHIP_QUERY_PAGE_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Timo 入会查询</title>
  <style>
    .timo-query-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; }
    .timo-query-card { padding:18px; border:1px solid var(--ops-border,#e5ebf3); border-radius:var(--ops-r-xl,22px); background:var(--ops-panel,#fff); box-shadow:var(--ops-shadow-card,0 1px 0 rgba(15,23,42,.02)); }
    .timo-query-card-head { margin-bottom:14px; }
    .timo-query-card h2 { margin:0 0 4px; font-size:18px; }
    .timo-query-country { color:var(--ops-muted,#718095); font-size:13px; }
    .timo-query-form { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:10px; align-items:end; }
    .timo-query-field input { margin:0!important; }
    .timo-query-form button { min-width:92px; margin:0; }
    .timo-query-result { display:none; margin-top:14px; padding:14px 15px; border:1px solid #d7e5ff; border-radius:16px; background:#f8fbff; }
    .timo-query-result.is-visible { display:block; }
    .timo-query-result.is-joined { border-color:#bbf7d0; background:#f0fdf4; }
    .timo-query-result.is-not-joined { border-color:#dbe3ee; background:#f8fafc; }
    .timo-query-result.is-error { border-color:#fed7aa; background:#fff7ed; }
    .timo-query-result-title { margin-bottom:7px; color:#172033; font-size:15px; font-weight:760; }
    .timo-query-result-meta { display:grid; grid-template-columns:72px minmax(0,1fr); gap:5px 10px; color:#475569; font-size:13px; }
    .timo-query-result-meta span:nth-child(odd) { color:#94a3b8; }
    .timo-query-empty { grid-column:1/-1; padding:44px 24px; border:1px dashed #cbd5e1; border-radius:22px; background:rgba(255,255,255,.72); color:#64748b; text-align:center; }
    .timo-query-loading { min-height:190px; background:linear-gradient(100deg,#fff 30%,#f6f9ff 50%,#fff 70%); background-size:220% 100%; animation:timo-query-shimmer 1.35s ease-in-out infinite; }
    @keyframes timo-query-shimmer { to { background-position:-220% 0; } }
    @media (prefers-reduced-motion:reduce) { .timo-query-loading { animation:none; } }
    @media (max-width:900px) { .timo-query-grid { grid-template-columns:1fr; } }
    @media (max-width:620px) {
      .timo-query-card { padding:17px; }
      .timo-query-form { grid-template-columns:1fr; }
      .timo-query-form button { width:100%; }
    }
  </style>
</head>
<body>
  <div class="page-shell">
    <div class="shell-nav"><a href="/ops/intake-submit">绑定中心</a><a href="/ops/timo-membership-query">Timo 入会查询</a><a href="/ops/production-ops">群审批控制台</a><a href="/ops/group-atmosphere">群聊天助手</a><a href="/ops/accounts">账号设置</a></div>
    <section class="hero"><h1>Timo 入会查询</h1></section>
    <main id="timoQueryGrid" class="timo-query-grid" aria-live="polite">
      <div class="timo-query-card timo-query-loading"></div>
      <div class="timo-query-card timo-query-loading"></div>
    </main>
  </div>
  <script>
    const grid=document.getElementById('timoQueryGrid');
    const esc=value=>String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    const inputId=index=>`timo_membership_sid_${index}`;
    const resultId=index=>`timo_membership_result_${index}`;
    function revealActiveNav(){requestAnimationFrame(()=>document.querySelector('.shell-nav a.is-active')?.scrollIntoView({block:'nearest',inline:'center'}));}
    if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',revealActiveNav);}else{revealActiveNav();}
    async function requestJson(url,options={}){
      const response=await fetch(url,{cache:'no-store',credentials:'same-origin',...options});
      let data={};
      try{data=await response.json()}catch(_){data={};}
      if(!response.ok){const detail=data.detail;const message=typeof detail==='string'?detail:(detail?.message||data.message||'查询失败，请稍后重试');throw new Error(message);}
      return data;
    }
    function renderGuilds(rows){
      if(!rows.length){grid.innerHTML='<div class="timo-query-empty"><strong>暂无可查询公会</strong></div>';return;}
      grid.innerHTML=rows.map((row,index)=>`<section class="timo-query-card" data-guild="${esc(row.guild_name)}"><div class="timo-query-card-head"><h2>${esc(row.guild_display_name||row.guild_name)}</h2><div class="timo-query-country">${esc(row.country||'')}</div></div><form class="timo-query-form" onsubmit="queryMembership(event,${index})"><div class="timo-query-field"><input id="${inputId(index)}" aria-label="Timo SID" inputmode="numeric" autocomplete="off" maxlength="20" placeholder="输入 Timo SID" aria-describedby="${resultId(index)}" /></div><button type="submit">查询</button></form><div id="${resultId(index)}" class="timo-query-result" role="status"></div></section>`).join('');
    }
    function showResult(index,type,title,meta=[]){
      const box=document.getElementById(resultId(index));
      if(!box)return;
      box.className=`timo-query-result is-visible is-${type}`;
      box.innerHTML=`<div class="timo-query-result-title">${esc(title)}</div>${meta.length?`<div class="timo-query-result-meta">${meta.map(([label,value])=>`<span>${esc(label)}</span><span>${esc(value)}</span>`).join('')}</div>`:''}`;
    }
    async function queryMembership(event,index){
      event.preventDefault();
      const card=event.currentTarget.closest('[data-guild]');
      const guildName=String(card?.dataset.guild||'').trim();
      const input=document.getElementById(inputId(index));
      const sid=String(input?.value||'').replace(/\s+/g,'');
      const button=event.currentTarget.querySelector('button[type="submit"]');
      if(!/^\d{6,20}$/.test(sid)){showResult(index,'error','请输入正确的 Timo SID');input?.focus();return;}
      const original=button.textContent;button.disabled=true;button.textContent='查询中…';
      showResult(index,'not-joined','查询中…');
      try{
        const data=await requestJson('/api/ops/timo-membership-query/query',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({guild_name:guildName,timo_id:sid})});
        if(data.status==='joined'){
          showResult(index,'joined','已加入', [['昵称',data.nickname||'-'],['加入日期',data.join_date||'-']]);
        }else{
          showResult(index,'not-joined','未加入');
        }
      }catch(error){showResult(index,'error',error.message||'暂时无法确认，请稍后重试');}
      finally{button.disabled=false;button.textContent=original;}
    }
    requestJson('/api/ops/timo-membership-query/guilds').then(data=>renderGuilds(Array.isArray(data.rows)?data.rows:[])).catch(error=>{grid.innerHTML=`<div class="timo-query-empty"><strong>公会列表加载失败</strong><div>${esc(error.message||'请刷新页面重试')}</div></div>`;});
  </script>
</body>
</html>
"""
