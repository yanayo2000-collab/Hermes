STREAMER_ANALYTICS_PAGE_HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>主播数据分析</title>
  <style>
    .analytics-hero{display:grid;gap:var(--ops-space-4);margin:0!important}
    .analytics-titlebar{display:flex;justify-content:space-between;gap:20px;align-items:flex-start}.analytics-titlebar h1{margin:0}.analytics-title-meta{display:flex;align-items:center;gap:10px;margin-top:8px;color:var(--ops-text-2);font-size:13px}
    .analytics-tabs{display:flex;gap:6px;padding:4px;border:1px solid var(--ops-border-strong);border-radius:var(--ops-r-lg);background:var(--ops-surface-2);box-shadow:var(--ops-shadow-soft)}
    #appTabs button{border:1px solid var(--ops-border)!important;background:var(--ops-panel)!important;color:var(--ops-text-2)!important;border-radius:var(--ops-r-md)!important;padding:8px 16px!important;font-weight:720!important;cursor:pointer;box-shadow:none!important}
    #appTabs button:hover:not(.active){background:var(--ops-blue-soft)!important;color:var(--ops-blue-hover)!important;border-color:#d7e5ff!important}
    #appTabs button:focus-visible{outline:2px solid var(--ops-blue);outline-offset:2px}
    #appTabs button.active{border-color:var(--ops-blue)!important;background:var(--ops-blue)!important;color:#fff!important;box-shadow:0 8px 18px rgba(47,107,255,.18)!important}
    .analytics-filters{display:grid;grid-template-columns:minmax(150px,.8fr) minmax(150px,.8fr) minmax(150px,.8fr) minmax(210px,1.15fr) max-content;gap:var(--ops-space-3);align-items:end}.analytics-filters label{display:grid;gap:6px;min-width:0;color:var(--ops-text-2)!important;font-size:12px!important;font-weight:700!important}
    .analytics-filters input,.analytics-filters select{min-width:0!important;margin:0!important}.analytics-filters button{min-height:40px!important;padding-inline:18px!important}.analytics-filters button:disabled,.analytics-filters select:disabled{opacity:.62!important;cursor:wait!important}
    .analytics-content{display:grid;gap:var(--ops-card-gap)}
    .analytics-overview{margin:0!important;padding:18px!important}.overview-heading{display:flex;justify-content:space-between;align-items:center;gap:14px;margin-bottom:14px}.overview-heading h2{margin:0}.overview-range{color:var(--ops-text-2);font-size:13px;font-weight:650}
    .metric-grid{display:grid;grid-template-columns:1.45fr repeat(3,minmax(0,1fr));gap:1px;overflow:hidden;border:1px solid var(--ops-border);border-radius:var(--ops-r-lg);background:var(--ops-border)}.metric{min-width:0;padding:15px 16px;background:var(--ops-panel)}.metric.featured{background:linear-gradient(145deg,var(--ops-blue-soft),var(--ops-panel))}
    .metric small{color:var(--ops-text-2);font-weight:650}.metric strong{display:block;margin-top:9px;color:var(--ops-text)!important;font-size:23px;line-height:1.1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-variant-numeric:tabular-nums}.metric.featured strong{font-size:29px;color:var(--ops-blue-hover)!important}
    .analytics-tooltip-target{cursor:help}.analytics-tooltip-target:focus-visible{outline:2px solid var(--ops-blue);outline-offset:3px;border-radius:4px}.analytics-tooltip{position:fixed;z-index:9999;display:grid;gap:5px;min-width:132px;max-width:min(320px,calc(100vw - 24px));padding:10px 12px;border:1px solid rgba(15,23,42,.16);border-radius:10px;background:rgba(17,24,39,.96);box-shadow:0 16px 40px rgba(15,23,42,.22);color:#fff;font-size:12px;line-height:1.35}.analytics-tooltip[hidden]{display:none}.analytics-tooltip strong{display:block;margin:0;color:#fff!important;font-size:12px}.analytics-tooltip span{color:#fff;font-size:13px;font-weight:800;font-variant-numeric:tabular-nums}.analytics-tooltip::after{content:"";position:absolute;left:var(--tooltip-arrow-left,50%);width:10px;height:10px;background:rgba(17,24,39,.96);transform:translateX(-50%) rotate(45deg)}.analytics-tooltip[data-placement="top"]::after{bottom:-5px}.analytics-tooltip[data-placement="bottom"]::after{top:-5px}
    .quality-panel{display:grid;grid-template-columns:150px minmax(0,1fr);gap:14px;align-items:center;margin-top:14px;padding-top:14px;border-top:1px solid var(--ops-border)}.quality-label strong{display:block;font-size:14px}.quality-label span{display:block;margin-top:4px;color:var(--ops-text-2);font-size:12px;white-space:nowrap}
    .cohort-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1px;overflow:hidden;border:1px solid var(--ops-border);border-radius:var(--ops-r-md);background:var(--ops-border)}.cohort-group{display:grid;grid-template-rows:auto 1fr;min-width:0;background:var(--ops-surface)}.cohort-group-head{display:flex;align-items:baseline;justify-content:space-between;gap:8px;padding:8px 11px 6px}.cohort-group-head strong{flex:none;color:var(--ops-text);font-size:15px}.cohort-group-head span{min-width:0;color:var(--ops-text-2);font-size:10px;font-weight:600;line-height:1.3;text-align:right;white-space:normal;font-variant-numeric:tabular-nums}.cohort-pair{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))}.cohort-stat{min-width:0;padding:4px 11px 9px}.cohort-stat+.cohort-stat{border-left:1px solid var(--ops-border)}.cohort-stat small{display:block;color:var(--ops-text-2);font-size:11px;white-space:nowrap}.cohort-stat strong{display:block;margin-top:5px;color:var(--ops-text)!important;font-size:17px;line-height:1.15;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-variant-numeric:tabular-nums}.cohort-stat span{display:block;margin-top:3px;color:var(--ops-text-2);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .analytics-content .analytics-section{margin:0!important;padding:0!important;overflow:hidden}.analytics-section-head{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:11px 16px;border-bottom:1px solid var(--ops-border)}.analytics-section-head h3{margin:0!important}.analytics-section-head span{color:var(--ops-text-2);font-size:12px}
    .analytics-scroll{overflow:auto;max-height:430px}.analytics-section .analytics-table{margin:0!important;border-radius:0!important;box-shadow:none!important}.analytics-table-heading{position:sticky;top:0;z-index:1}.numeric-col{text-align:right!important;font-variant-numeric:tabular-nums}.analytics-empty{padding:26px!important;text-align:center;color:var(--ops-text-2)!important}.income-cell{font-weight:800;color:var(--ops-text)!important}.muted{color:var(--ops-text-2)}#guildDetailTable{width:100%!important;min-width:840px!important;table-layout:fixed!important}#guildDetailTable col{width:16.6667%!important}
    .trend-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1px;background:var(--ops-border)}.trend-metric{min-width:0;padding:15px 17px 12px;background:var(--ops-panel)}.trend-metric-head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}.trend-metric-label{color:var(--ops-text-2);font-size:12px;font-weight:700}.trend-metric-value{display:block;margin-top:5px;color:var(--ops-text);font-size:21px;line-height:1.1;font-variant-numeric:tabular-nums}.trend-change{flex:none;margin-top:1px;padding:3px 7px;border-radius:999px;background:var(--ops-surface-2);color:var(--ops-text-2);font-size:11px;font-weight:750;font-variant-numeric:tabular-nums}.trend-change.alert{background:#fff7ed;color:#c2410c}.trend-change.drop{background:#fef2f2;color:#b91c1c}.trend-svg{display:block;width:100%;height:76px;margin-top:10px;overflow:visible}.trend-baseline{stroke:var(--ops-border);stroke-width:1}.trend-line{fill:none;stroke:var(--ops-blue);stroke-width:2.25;stroke-linecap:round;stroke-linejoin:round}.trend-area{fill:rgba(47,107,255,.08)}.trend-point{fill:var(--ops-panel);stroke:var(--ops-blue);stroke-width:2}.trend-dates{display:flex;justify-content:space-between;gap:10px;margin-top:3px;color:var(--ops-text-2);font-size:10px;font-variant-numeric:tabular-nums}.analytics-section[hidden]{display:none!important}
    .cohort-matrix-section[hidden]{display:none!important}.cohort-matrix-head{align-items:center}.cohort-matrix-tools{display:flex;align-items:center;justify-content:flex-end;gap:10px;min-width:0}.cohort-matrix-meta{text-align:right;white-space:nowrap}.cohort-matrix-meta strong,.cohort-matrix-meta span{display:block}.cohort-matrix-meta span{margin-top:3px}.cohort-country-quick{display:flex;gap:4px;padding:3px;border:1px solid var(--ops-border-strong);border-radius:var(--ops-r-md);background:var(--ops-surface-2)}.cohort-country-quick button{min-height:30px!important;padding:5px 10px!important;border:1px solid transparent!important;border-radius:calc(var(--ops-r-md) - 3px)!important;background:transparent!important;color:var(--ops-text-2)!important;font-size:12px!important;font-weight:720!important;line-height:1!important;white-space:nowrap;box-shadow:none!important}.cohort-country-quick button:hover:not(.active):not(:disabled){border-color:var(--ops-border)!important;background:var(--ops-panel)!important;color:var(--ops-text)!important}.cohort-country-quick button:focus-visible{outline:2px solid var(--ops-blue);outline-offset:2px}.cohort-country-quick button.active{border-color:var(--ops-blue)!important;background:var(--ops-blue)!important;color:#fff!important}.cohort-country-quick button:disabled{cursor:wait;opacity:.6}.cohort-guild-filter{display:flex;align-items:center;gap:6px;color:var(--ops-text-2);font-size:12px;font-weight:700;white-space:nowrap}.cohort-guild-filter[hidden]{display:none!important}.cohort-guild-filter select{min-width:150px!important;max-width:220px!important;min-height:36px!important;margin:0!important;padding-block:5px!important}.cohort-matrix-scroll{position:relative;overflow:auto;max-height:560px;isolation:isolate}.cohort-matrix{display:table!important;width:max-content!important;min-width:1480px!important;margin:0!important;overflow:visible!important;table-layout:fixed!important;border-collapse:separate!important;border-spacing:0!important;border-radius:0!important;box-shadow:none!important}.cohort-matrix :is(thead,tbody)>tr>*{padding:9px 10px!important;border-right:1px solid var(--ops-border);border-bottom:1px solid var(--ops-border);white-space:nowrap}.cohort-matrix thead>tr>*{position:sticky;background:var(--ops-surface-2)!important;color:var(--ops-text-2)!important;font-size:11px!important;line-height:1.25;text-align:center!important;z-index:4}.cohort-matrix thead>tr:first-child>*{top:0;height:38px;color:var(--ops-text)!important;font-weight:800!important}.cohort-matrix thead>tr:nth-child(2)>*{top:38px;height:36px}.cohort-matrix tbody>tr>*{background:var(--ops-panel);font-variant-numeric:tabular-nums}.cohort-matrix tbody>tr:hover>*{background:var(--ops-blue-soft)}.cohort-matrix .sticky-week{position:sticky!important;left:0!important;z-index:5;width:124px;min-width:124px;max-width:124px;box-shadow:1px 0 0 var(--ops-border)}.cohort-matrix tbody .sticky-week{background:var(--ops-panel)!important}.cohort-matrix tbody>tr:hover .sticky-week{background:var(--ops-blue-soft)!important}.cohort-matrix thead .sticky-week{z-index:8;background:var(--ops-surface-2)!important}.cohort-matrix .pending-cell{color:var(--ops-text-2)!important;font-weight:600}.cohort-matrix .settlement-cell{font-weight:800;color:#166534!important}
    .roi-card-stack{display:grid;gap:var(--ops-card-gap)}.roi-head-actions{display:flex;align-items:center;gap:12px}.roi-head-meta{text-align:right}.roi-head-meta strong,.roi-head-meta span{display:block}.roi-head-meta span{margin-top:2px}.roi-head-actions button{min-height:34px!important;padding:6px 13px!important}.roi-status{display:inline-flex;padding:3px 8px;border-radius:999px;background:var(--ops-surface-2);color:var(--ops-text-2);font-size:11px;font-weight:750}.roi-status.complete,.roi-status.published{background:#ecfdf3;color:#166534}.roi-status.pending_settlement{background:#fff7ed;color:#c2410c}.roi-status.policy_missing{background:#fef2f2;color:#b91c1c}.roi-table-scroll{position:relative;overflow:auto;max-height:560px;overscroll-behavior:contain;isolation:isolate}.roi-table-scroll:focus-visible{outline:2px solid var(--ops-blue);outline-offset:-2px}.roi-table{display:table!important;width:max-content!important;min-width:1480px!important;margin:0!important;overflow:visible!important;table-layout:fixed!important;border-collapse:separate!important;border-spacing:0!important;border-radius:0!important;box-shadow:none!important}.roi-progress-table{min-width:2920px!important}.roi-table :is(thead,tbody)>tr>*{padding:9px 10px!important;border-right:1px solid var(--ops-border);border-bottom:1px solid var(--ops-border);white-space:nowrap!important;word-break:keep-all!important}.roi-table thead>tr>*{position:sticky!important;top:0;z-index:4;background:var(--ops-surface-2)!important;color:var(--ops-text-2)!important;line-height:1.35}.roi-summary-table thead>tr>*{height:42px}.roi-progress-table thead>tr:first-child>*{top:0;height:42px}.roi-progress-table thead>tr:nth-child(2)>*{top:42px;height:42px}.roi-table tbody>tr>*{background:var(--ops-panel)}.roi-table tbody>tr:hover>*{background:var(--ops-blue-soft)}.roi-table .roi-sticky{position:sticky!important;left:0!important;z-index:5;width:180px;min-width:180px;max-width:180px;box-shadow:1px 0 0 var(--ops-border),8px 0 14px rgba(15,23,42,.035)}.roi-table tbody .roi-sticky{background:var(--ops-panel)!important}.roi-table tbody>tr:hover .roi-sticky{background:var(--ops-blue-soft)!important}.roi-table thead .roi-sticky{z-index:8;background:var(--ops-surface-2)!important}.guild-country-tip{display:inline-flex;max-width:100%;overflow:hidden;text-overflow:ellipsis}.roi-positive{color:#166534!important;font-weight:800}.roi-negative{color:#b91c1c!important;font-weight:800}
    .growth-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:1px;background:var(--ops-border)}.growth-metric{min-width:0;padding:14px 16px;background:var(--ops-panel)}.growth-metric small{display:block;color:var(--ops-text-2);font-size:11px;font-weight:700}.growth-metric strong{display:block;margin-top:7px;color:var(--ops-text)!important;font-size:20px;font-variant-numeric:tabular-nums;white-space:nowrap}.growth-metric span{display:block;margin-top:4px;color:var(--ops-text-2);font-size:11px}.scorecard-grid{display:grid;grid-template-columns:repeat(8,minmax(0,1fr));gap:1px;border-top:1px solid var(--ops-border);background:var(--ops-border)}.scorecard-item{min-width:0;padding:11px 12px;background:var(--ops-surface)}.scorecard-item small{display:block;color:var(--ops-text-2);font-size:10px;font-weight:700;white-space:nowrap}.scorecard-item strong{display:block;margin-top:5px;color:var(--ops-text)!important;font-size:16px;font-variant-numeric:tabular-nums;white-space:nowrap}.scale-state{display:inline-flex!important;width:max-content;padding:4px 8px;border-radius:999px;font-size:12px!important}.scale-state.scale{background:#ecfdf3;color:#166534!important}.scale-state.validate{background:#eff6ff;color:#1d4ed8!important}.scale-state.hold{background:#fef2f2;color:#b91c1c!important}.scale-state.insufficient{background:var(--ops-surface-2);color:var(--ops-text-2)!important}
    .roi-decision-body{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(230px,.7fr);gap:0;border-top:0}.roi-kpis{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:1px;background:var(--ops-border);border-bottom:1px solid var(--ops-border)}.roi-kpi{min-width:0;padding:12px 14px;background:var(--ops-panel)}.roi-kpi small{display:block;color:var(--ops-text-2);font-size:11px;font-weight:700}.roi-kpi strong{display:block;margin-top:6px;color:var(--ops-text)!important;font-size:19px;line-height:1.15;font-variant-numeric:tabular-nums;white-space:nowrap}.roi-kpi strong.good{color:#166534!important}.roi-kpi strong.bad{color:#b91c1c!important}.roi-chart-wrap{min-width:0;padding:14px 16px 12px}.roi-chart-legend{display:flex;align-items:center;gap:14px;color:var(--ops-text-2);font-size:11px}.roi-chart-legend span{display:inline-flex;align-items:center;gap:6px}.roi-chart-legend i{display:block;width:18px;height:3px;border-radius:999px;background:var(--ops-blue)}.roi-chart-legend .scenario i{height:0;border-top:2px dashed #d97706;background:transparent}.roi-chart{display:block;width:100%;height:250px;margin-top:4px}.roi-conclusion{display:grid;align-content:center;gap:8px;padding:18px;border-left:1px solid var(--ops-border);background:var(--ops-surface)}.roi-conclusion small{color:var(--ops-text-2);font-size:11px;font-weight:750}.roi-conclusion strong{font-size:18px;line-height:1.35;text-wrap:balance}.roi-conclusion span{color:var(--ops-text-2);font-size:12px}.roi-scenario>summary{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:13px 16px;cursor:pointer;font-weight:800;list-style:none}.roi-scenario>summary::-webkit-details-marker{display:none}.roi-scenario>summary::after{content:'展开';color:var(--ops-text-2);font-size:11px;font-weight:700}.roi-scenario[open]>summary::after{content:'收起'}.scenario-mode{display:flex;gap:4px;padding:4px;border-radius:var(--ops-r-md);background:var(--ops-surface-2)}.scenario-mode button{flex:1;min-height:30px!important;padding:4px 8px!important;background:transparent!important;color:var(--ops-text-2)!important;box-shadow:none!important}.scenario-mode button.active{background:var(--ops-panel)!important;color:var(--ops-blue-hover)!important}.roi-scenario-content{display:grid;grid-template-columns:minmax(280px,.85fr) minmax(0,1.4fr);border-top:1px solid var(--ops-border)}.roi-scenario-controls{display:grid;gap:12px;padding:16px;border-right:1px solid var(--ops-border)}.relative-scenario{display:grid;gap:12px}.roi-scenario-control{display:grid;grid-template-columns:minmax(0,1fr) 72px;gap:4px 10px;align-items:center}.roi-scenario-control span{font-size:12px;font-weight:700}.roi-scenario-control output{text-align:right;color:var(--ops-blue-hover);font-weight:800;font-variant-numeric:tabular-nums}.roi-scenario-control input{grid-column:1/-1;width:100%;accent-color:var(--ops-blue)}.absolute-scenario{display:grid;grid-template-columns:1fr 1fr;gap:10px}.absolute-scenario[hidden],.relative-scenario[hidden]{display:none!important}.absolute-scenario label{display:grid;gap:5px;color:var(--ops-text-2);font-size:11px;font-weight:700}.absolute-scenario input{width:100%!important;margin:0!important;text-align:right}.scenario-baseline{grid-column:1/-1;margin:0;color:var(--ops-text-2);font-size:11px}.roi-scenario-actions{display:flex;align-items:center;justify-content:space-between;gap:10px;padding-top:4px}.roi-scenario-actions button{min-height:32px!important;padding:5px 10px!important}.roi-scenario-result{font-size:12px;color:var(--ops-text-2)}.roi-scenario-result strong{display:block;margin-top:2px;color:var(--ops-text)!important;font-size:21px}.roi-sensitivity{display:grid;align-content:start;gap:12px;padding:16px}.roi-sensitivity-head{display:flex;justify-content:space-between;gap:10px}.roi-sensitivity-head strong{font-size:13px}.roi-sensitivity-head span{color:var(--ops-text-2);font-size:11px}.roi-sensitivity-item{display:grid;grid-template-columns:minmax(120px,1fr) minmax(100px,1.2fr) 62px;gap:10px;align-items:center;font-size:12px}.roi-sensitivity-track{height:7px;overflow:hidden;border-radius:999px;background:var(--ops-surface-2)}.roi-sensitivity-bar{height:100%;border-radius:inherit;background:var(--ops-blue)}.roi-sensitivity-value{text-align:right;font-weight:800;font-variant-numeric:tabular-nums}.roi-scenario.is-unavailable .roi-scenario-content{opacity:.55}
    .cohort-matrix thead .week-band-odd,.roi-table thead .week-band-odd{background:#eaf2ff!important}.cohort-matrix thead .week-band-even,.roi-table thead .week-band-even{background:#edf8f5!important}.week-band-head{text-align:center!important;font-weight:800!important}.week-band-start.week-band-odd{border-left:2px solid #bfd4ff!important}.week-band-start.week-band-even{border-left:2px solid #c8e4da!important}
    .roi-dialog{width:min(1180px,calc(100vw - 40px));max-height:calc(100vh - 48px);padding:0;border:1px solid var(--ops-border-strong);border-radius:var(--ops-r-xl);background:var(--ops-panel);color:var(--ops-text);box-shadow:0 28px 70px rgba(15,23,42,.28)}.roi-dialog::backdrop{background:rgba(15,23,42,.38);backdrop-filter:blur(2px)}.roi-dialog-form{display:grid;grid-template-rows:auto minmax(0,1fr) auto;max-height:calc(100vh - 48px)}.roi-dialog-head{display:flex;justify-content:space-between;align-items:flex-start;gap:18px;padding:18px 20px;border-bottom:1px solid var(--ops-border)}.roi-dialog-head h3{margin:0}.roi-dialog-head p{margin:5px 0 0;color:var(--ops-text-2);font-size:12px}.roi-dialog-head button{min-width:36px!important;min-height:36px!important;padding:0!important}.roi-entry-scroll{overflow:auto}.roi-entry-table{width:max-content!important;min-width:100%!important;margin:0!important;border-radius:0!important;box-shadow:none!important}.roi-entry-table th{position:sticky;top:0;z-index:2;background:var(--ops-surface-2)!important;white-space:nowrap}.roi-entry-table .entry-guild{position:sticky;left:0;z-index:3;min-width:150px;background:var(--ops-panel)!important;box-shadow:1px 0 0 var(--ops-border)}.roi-entry-table thead .entry-guild{z-index:4;background:var(--ops-surface-2)!important}.roi-entry-table input[type=number]{width:112px!important;min-width:112px!important;margin:0!important;text-align:right;font-variant-numeric:tabular-nums}.roi-entry-table input.invalid{border-color:#dc2626!important;background:#fef2f2!important}.roi-entry-total{font-weight:800}.roi-dialog-foot{display:grid;gap:12px;padding:14px 20px;border-top:1px solid var(--ops-border);background:var(--ops-surface)}.roi-correction{display:grid;grid-template-columns:110px minmax(0,1fr);align-items:center;gap:10px}.roi-correction label{font-size:12px;font-weight:750;color:var(--ops-text-2)}.roi-correction input{margin:0!important}.roi-dialog-actions{display:flex;justify-content:space-between;align-items:center;gap:12px}.roi-dialog-actions>div{display:flex;gap:8px}.roi-save-message{font-size:12px;color:var(--ops-text-2)}
    .policy-dialog{width:min(1080px,calc(100vw - 40px))}.policy-body{overflow:auto;padding:18px 20px}.policy-scope-grid{display:grid;grid-template-columns:1.2fr .9fr .8fr .8fr;gap:12px}.policy-field{display:grid;gap:6px;color:var(--ops-text-2);font-size:12px;font-weight:700}.policy-field input,.policy-field select,.policy-field textarea{margin:0!important}.policy-mode-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin-top:14px}.policy-tier-section{margin-top:16px;border:1px solid var(--ops-border);border-radius:var(--ops-r-md);overflow:hidden}.policy-tier-head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:9px 11px;background:var(--ops-surface-2);border-bottom:1px solid var(--ops-border)}.policy-tier-head strong{font-size:13px}.policy-tier-head button{min-height:28px!important;padding:4px 9px!important}.policy-tier-scroll{overflow:auto;max-height:260px}.policy-tier-table{width:100%!important;min-width:640px!important;margin:0!important;border-radius:0!important;box-shadow:none!important}.policy-tier-table th{position:sticky;top:0;z-index:2;background:var(--ops-surface-2)!important;white-space:nowrap}.policy-tier-table td{padding:7px 8px!important}.policy-tier-table input{width:100%!important;min-width:100px!important;margin:0!important;text-align:right}.policy-tier-table input.invalid{border-color:#dc2626!important;background:#fef2f2!important}.policy-tier-table .tier-level{width:62px!important;min-width:62px!important}.policy-tier-table .tier-remove{min-width:30px!important;min-height:30px!important;padding:0!important}.policy-flat-fields[hidden],.policy-tier-fields[hidden]{display:none!important}.policy-meta-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:14px}.policy-history{margin-top:14px;padding:10px 12px;border:1px solid var(--ops-border);border-radius:var(--ops-r-md);background:var(--ops-surface)}.policy-history strong{display:block;font-size:12px}.policy-history-list{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}.policy-version{padding:4px 8px;border:1px solid var(--ops-border);border-radius:999px;background:var(--ops-panel);color:var(--ops-text-2);font-size:11px}.policy-version.current{border-color:#bfd4ff;background:var(--ops-blue-soft);color:var(--ops-blue-hover)}
    @media(max-width:1100px){.metric-grid{grid-template-columns:repeat(2,1fr)}.metric.featured{grid-column:1/-1}.quality-panel{grid-template-columns:1fr}.cohort-grid{grid-template-columns:repeat(3,1fr)}.growth-grid{grid-template-columns:repeat(2,1fr)}.scorecard-grid{grid-template-columns:repeat(4,1fr)}.roi-decision-body{grid-template-columns:1fr}.roi-conclusion{border-left:0;border-top:1px solid var(--ops-border)}.roi-kpis{grid-template-columns:repeat(3,1fr)}}
    @media(max-width:900px){.analytics-filters{grid-template-columns:repeat(2,minmax(0,1fr))}.analytics-filters button{width:100%}.trend-grid{grid-template-columns:1fr}.cohort-matrix-head{align-items:flex-start}.cohort-matrix-tools{width:100%;justify-content:space-between;flex-wrap:wrap}.cohort-matrix-meta{text-align:left}.roi-head-actions{width:100%;justify-content:space-between}.roi-head-meta{text-align:left}.roi-dialog{width:calc(100vw - 20px)}.policy-scope-grid,.policy-mode-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
    @media(max-width:760px){.analytics-titlebar{display:block}.analytics-tabs{margin-top:14px;width:max-content}.metric-grid{grid-template-columns:repeat(2,1fr)}.metric.featured{grid-column:1/-1}.cohort-grid{grid-template-columns:1fr}.growth-grid,.scorecard-grid{grid-template-columns:repeat(2,1fr)}.roi-kpis{grid-template-columns:repeat(2,1fr)}.roi-scenario-content{grid-template-columns:1fr}.roi-scenario-controls{border-right:0;border-bottom:1px solid var(--ops-border)}}
    @media(max-width:520px){.analytics-tabs{width:100%}#appTabs button{flex:1 1 0;padding-inline:10px!important}.overview-heading{align-items:flex-start;flex-direction:column;gap:4px}}
  </style>
</head>
<body>
<div class="page-shell" id="analyticsPage">
  <div class="shell-nav"></div>
  <section class="hero analytics-hero" aria-labelledby="analyticsTitle">
    <div class="analytics-titlebar">
      <div>
        <h1 id="analyticsTitle">主播数据分析</h1>
        <div class="analytics-title-meta" role="status" aria-live="polite" aria-atomic="true"><span id="dataAsOf">—</span></div>
      </div>
      <div class="analytics-tabs" id="appTabs" role="tablist" aria-label="平台切换">
        <button id="appTabLinky" data-app="linky" role="tab" aria-selected="false" aria-controls="analyticsContent" tabindex="-1">Linky</button><button class="active" id="appTabTimo" data-app="timo" role="tab" aria-selected="true" aria-controls="analyticsContent" tabindex="0">Timo</button><button id="appTabSugo" data-app="sugo" role="tab" aria-selected="false" aria-controls="analyticsContent" tabindex="-1">Sugo</button>
      </div>
    </div>

    <div class="analytics-filters" role="group" aria-label="分析筛选条件">
      <label for="dateFrom">开始日期<input id="dateFrom" type="date" /></label>
      <label for="dateTo">结束日期<input id="dateTo" type="date" /></label>
      <label for="countryFilter">国家<select id="countryFilter"><option value="">全部国家</option></select></label>
      <label for="guildFilter">公会<select id="guildFilter"><option value="">全部公会</option></select></label>
      <button id="refreshBtn" type="button">更新数据</button>
    </div>
  </section>

  <div class="analytics-content" id="analyticsContent" role="tabpanel" aria-labelledby="appTabTimo" tabindex="0" aria-busy="true">
  <section class="card analytics-overview" aria-labelledby="overviewTitle">
    <div class="overview-heading"><h2 id="overviewTitle">经营概览</h2><span id="overviewRange" class="overview-range">—</span></div>
    <div id="metrics" class="metric-grid"></div>
    <div class="quality-panel">
      <div class="quality-label"><strong>新人质量</strong><span id="newcomerScope">—</span></div>
      <div id="cohorts" class="cohort-grid"></div>
    </div>
  </section>

  <section id="timoCohortPanel" class="card analytics-section cohort-matrix-section" aria-labelledby="timoCohortTitle" hidden>
    <div class="analytics-section-head cohort-matrix-head">
      <div><h3 id="timoCohortTitle">Timo 新主播周 Cohort</h3></div>
      <div class="cohort-matrix-tools"><div id="cohortCountryQuick" class="cohort-country-quick" role="group" aria-label="快捷切换国家"></div><label id="cohortGuildControl" class="cohort-guild-filter" for="cohortGuildFilter" hidden>公会<select id="cohortGuildFilter"><option value="">全部公会</option></select></label><div class="cohort-matrix-meta"><strong id="cohortScope">—</strong><span id="cohortDataAsOf">—</span></div></div>
    </div>
    <div class="cohort-matrix-scroll">
      <table id="timoCohortTable" class="analytics-table cohort-matrix">
        <thead id="timoCohortHead"></thead><tbody id="timoCohortRows"></tbody>
      </table>
    </div>
  </section>

  <section id="weeklyRoiPanel" class="roi-card-stack" aria-label="上周新增主播 ROI">
    <section id="weeklyRoiSummaryPanel" class="card analytics-section" aria-labelledby="weeklyRoiTitle">
      <div class="analytics-section-head">
        <div><h3 id="weeklyRoiTitle">项目整体 ROI</h3></div>
        <div class="roi-head-actions"><div class="roi-head-meta"><strong id="weeklyRoiScope">—</strong><span id="weeklyRoiStatus">—</span></div><button id="openPolicyBtn" type="button" class="secondary">配置政策</button><button id="openRoiEntryBtn" type="button">填写上周成本</button></div>
      </div>
      <div class="roi-table-scroll" tabindex="0" aria-label="项目整体 ROI 表格，可横向和纵向滚动"><table class="analytics-table roi-table roi-summary-table"><thead><tr><th class="roi-sticky">公会</th><th>状态</th><th class="numeric-col">新增主播</th><th class="numeric-col">注册周全成本/$</th><th class="numeric-col">平台结算/$</th><th class="numeric-col">结算规则</th><th class="numeric-col">全体收益/$</th><th class="numeric-col">注册周收入/$</th><th class="numeric-col">注册周ROI</th><th class="numeric-col">注册周利润/$</th><th class="numeric-col">新增主播全成本/$</th></tr></thead><tbody id="weeklyRoiSummaryRows"></tbody></table></div>
    </section>
    <section id="weeklyRoiProgressPanel" class="card analytics-section" aria-labelledby="weeklyRoiProgressTitle">
      <div class="analytics-section-head"><h3 id="weeklyRoiProgressTitle">新增主播回本进度</h3><span>W1=A周</span></div>
      <div class="roi-table-scroll" tabindex="0" aria-label="新增主播回本进度表格，可横向和纵向滚动"><table class="analytics-table roi-table roi-progress-table"><thead id="weeklyRoiCohortHead"></thead><tbody id="weeklyRoiCohortRows"></tbody></table></div>
    </section>
  </section>

  <section id="growthDecisionPanel" class="card analytics-section" aria-labelledby="growthDecisionTitle" hidden>
    <div class="analytics-section-head"><h3 id="growthDecisionTitle">利润增长与经营评分卡</h3><span id="growthDecisionScope">全部可追踪 Cohort</span></div>
    <div id="growthMetrics" class="growth-grid"></div>
    <div id="growthScorecard" class="scorecard-grid"></div>
  </section>

  <section class="card analytics-section">
    <div class="analytics-section-head"><h3>每日趋势</h3><span id="trendRangeLabel">最近 14 天</span></div>
    <div id="trendChart" class="trend-grid"></div>
  </section>
  <section id="guildDetailPanel" class="card analytics-section">
    <div class="analytics-section-head"><h3>公会经营明细</h3><span id="guildCountLabel">—</span></div>
    <div class="analytics-scroll"><table id="guildDetailTable" class="analytics-table"><colgroup><col><col><col><col><col><col></colgroup><thead><tr><th class="analytics-table-heading">公会</th><th class="analytics-table-heading numeric-col">主播数</th><th class="analytics-table-heading numeric-col">新增主播</th><th class="analytics-table-heading numeric-col">收益活跃主播</th><th class="analytics-table-heading numeric-col">平台总收益</th><th class="analytics-table-heading numeric-col">折合美元</th></tr></thead><tbody id="guildRows"></tbody></table></div>
  </section>
  <section id="roiLifecyclePanel" class="roi-card-stack" aria-label="生命周期 ROI 分析">
    <section id="roiDecisionPanel" class="card analytics-section" aria-labelledby="roiDecisionTitle">
      <div class="analytics-section-head"><h3 id="roiDecisionTitle">生命周期 ROI 回收</h3><span id="roiDecisionScope">—</span></div>
      <div id="roiKpis" class="roi-kpis"></div>
      <div class="roi-decision-body"><div class="roi-chart-wrap"><div class="roi-chart-legend"><span><i></i>实际 ROI</span><span id="roiScenarioLegend" class="scenario" hidden><i></i>演算 ROI</span><span>100% 回本线</span></div><svg id="roiLifecycleChart" class="roi-chart" role="img" aria-label="生命周期 ROI 回收曲线"></svg></div><aside class="roi-conclusion"><small>关键结论</small><strong id="roiConclusion">—</strong><span id="roiConclusionMeta">—</span></aside></div>
    </section>
    <details id="roiScenarioPanel" class="card roi-scenario">
      <summary><span>ROI 演算与敏感度（预测）</span></summary>
      <div class="roi-scenario-content"><div class="roi-scenario-controls">
        <div class="scenario-mode"><button id="relativeScenarioBtn" type="button" class="active">相对实值</button><button id="absoluteScenarioBtn" type="button">绝对参数</button></div>
        <div id="relativeScenarioControls" class="relative-scenario">
          <label class="roi-scenario-control"><span>收益变化</span><output id="roiRevenueValue">0%</output><input id="roiRevenueInput" type="range" min="-30" max="30" step="1" value="0" /></label>
          <label class="roi-scenario-control"><span>W2+ 留存变化</span><output id="roiRetentionValue">0%</output><input id="roiRetentionInput" type="range" min="-30" max="30" step="1" value="0" /></label>
          <label class="roi-scenario-control"><span>获客成本变化</span><output id="roiAcquisitionValue">0%</output><input id="roiAcquisitionInput" type="range" min="-30" max="30" step="1" value="0" /></label>
          <label class="roi-scenario-control"><span>共享成本变化</span><output id="roiSharedValue">0%</output><input id="roiSharedInput" type="range" min="-30" max="30" step="1" value="0" /></label>
        </div>
        <div id="absoluteScenarioControls" class="absolute-scenario" hidden>
          <label>主播单价/$<input id="absoluteUnitPrice" type="number" min="0" step="0.01" /></label>
          <label>首周 ARPU/$<input id="absoluteW1Arpu" type="number" min="0" step="0.01" /></label>
          <label>W2 留存/%<input id="absoluteRetentionW2" type="number" min="0" max="100" step="0.1" /></label>
          <label>单活跃成本/$<input id="absoluteActiveCost" type="number" min="0" step="0.01" /></label>
          <p id="absoluteBaselineNote" class="scenario-baseline">—</p>
        </div>
        <div class="roi-scenario-actions"><button id="resetRoiScenarioBtn" type="button" class="secondary">恢复实值</button><div class="roi-scenario-result">当前演算 ROI<strong id="roiScenarioResult">—</strong></div></div>
      </div><div class="roi-sensitivity"><div class="roi-sensitivity-head"><strong>单因素敏感度</strong><span>对最新成熟周 ROI 的影响</span></div><div id="roiSensitivityList"></div></div></div>
    </details>
  </section>
  </div>
</div>
<dialog id="roiEntryDialog" class="roi-dialog" aria-labelledby="roiEntryTitle">
  <form id="roiEntryForm" class="roi-dialog-form" method="dialog">
    <div class="roi-dialog-head"><div><h3 id="roiEntryTitle">填写上周成本</h3><p id="roiEntryScope">—</p></div><button id="closeRoiEntryBtn" type="button" aria-label="关闭">×</button></div>
    <div class="roi-entry-scroll"><table class="analytics-table roi-entry-table"><thead><tr><th class="entry-guild">公会</th><th>投放成本/$</th><th>Admin成本/$</th><th>客服成本/$</th><th>投手成本/$</th><th>活动成本/$</th><th>全成本/$</th><th>当前状态</th></tr></thead><tbody id="roiEntryRows"></tbody></table></div>
    <div class="roi-dialog-foot"><div id="roiCorrectionWrap" class="roi-correction" hidden><label for="roiCorrectionReason">更正原因</label><input id="roiCorrectionReason" type="text" maxlength="200" placeholder="修改已发布数据时必填" /></div><div class="roi-dialog-actions"><span id="roiSaveMessage" class="roi-save-message"></span><div><button id="copyPreviousRoiBtn" type="button" class="secondary">复制上周</button><button id="saveRoiDraftBtn" type="button" class="secondary">保存草稿</button><button id="publishRoiBtn" type="button">保存并发布</button></div></div></div>
  </form>
</dialog>
<dialog id="policyDialog" class="roi-dialog policy-dialog" aria-labelledby="policyDialogTitle">
  <form class="roi-dialog-form" method="dialog">
    <div class="roi-dialog-head"><div><h3 id="policyDialogTitle">配置结算政策</h3><p id="policyDialogScope">按生效周保存版本</p></div><button id="closePolicyBtn" type="button" aria-label="关闭">×</button></div>
    <div class="policy-body">
      <div class="policy-scope-grid">
        <label class="policy-field">公会<select id="policyGuild"></select></label>
        <label class="policy-field">生效周<input id="policyEffectiveFrom" type="date" /></label>
        <label class="policy-field">计算方式<select id="policyMode"><option value="flat">固定 CPA / CPS</option><option value="timo_tiered_1v1">Timo 1v1 梯度</option></select></label>
        <label class="policy-field">钻石 / $<input id="policyUnitsPerUsd" type="number" min="0.01" step="0.01" inputmode="decimal" /></label>
      </div>
      <div class="policy-mode-grid">
        <label class="policy-field policy-flat-fields">CPS结算比例 %<input id="policyCpsRate" type="number" min="0" step="0.01" inputmode="decimal" /></label>
        <label class="policy-field policy-flat-fields">新增主播 CPA / $<input id="policyNewcomerCpa" type="number" min="0" step="0.01" /></label>
        <label class="policy-field policy-flat-fields">非认证 CPA / $<input id="policyNonCertifiedCpa" type="number" min="0" step="0.01" /></label>
        <label class="policy-field policy-flat-fields">认证 CPA / $<input id="policyCertifiedCpa" type="number" min="0" step="0.01" /></label>
        <label class="policy-field policy-flat-fields">7日 / 10日奖励 $<span style="display:flex;gap:6px"><input id="policyBonus7" type="number" min="0" step="0.01" /><input id="policyBonus10" type="number" min="0" step="0.01" /></span></label>
        <label class="policy-field policy-tier-fields" hidden>公会有效主播门槛<input id="policyEligibleMin" type="number" min="0" step="1" /></label>
      </div>
      <div class="policy-tier-fields" hidden>
        <section class="policy-tier-section"><div class="policy-tier-head"><strong>主播梯度</strong><button id="addStreamerTierBtn" type="button" class="secondary">新增档位</button></div><div class="policy-tier-scroll"><table class="analytics-table policy-tier-table"><thead><tr><th>等级</th><th>1v1收益</th><th>累计奖励</th><th>本档奖励</th><th></th></tr></thead><tbody id="streamerTierRows"></tbody></table></div></section>
        <section class="policy-tier-section"><div class="policy-tier-head"><strong>公会梯度</strong><button id="addGuildTierBtn" type="button" class="secondary">新增档位</button></div><div class="policy-tier-scroll"><table class="analytics-table policy-tier-table"><thead><tr><th>等级</th><th>有效1v1收益</th><th>累计奖励</th><th>本档奖励</th><th></th></tr></thead><tbody id="guildTierRows"></tbody></table></div></section>
      </div>
      <div class="policy-meta-grid"><label class="policy-field">政策备注<textarea id="policyNote" rows="2" maxlength="500"></textarea></label><label class="policy-field">变更原因<textarea id="policyChangeReason" rows="2" maxlength="200" placeholder="保存新版本或更正时必填"></textarea></label></div>
      <div class="policy-history"><strong>历史版本</strong><div id="policyHistoryList" class="policy-history-list"></div></div>
    </div>
    <div class="roi-dialog-foot"><div class="roi-dialog-actions"><span id="policySaveMessage" class="roi-save-message" role="status" aria-live="polite"></span><div><button id="newPolicyVersionBtn" type="button" class="secondary">新建版本</button><button id="savePolicyBtn" type="button">保存政策</button></div></div></div>
  </form>
</dialog>
<script>
const state={app:'timo',payload:null,guildOptions:[],autoLatestCompleteRange:true,latestDataAsOf:{},metadataReady:false,weeklyRoi:null,roiDirty:false,policyData:null,policyDirty:false,roiScenarioMode:'relative'};
let latestRequestId=0;
let activeLoadController=null;
const appTabsControl=document.getElementById('appTabs');
const appTabButtons=[...appTabsControl.querySelectorAll('button[data-app]')];
const analyticsPanel=document.getElementById('analyticsContent');
const esc=value=>String(value??'').replace(/[&<>'"]/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
const fmt=n=>n===null||n===undefined?'—':Number(n).toLocaleString('zh-CN',{maximumFractionDigits:2});
const money=n=>n===null||n===undefined?'—':fmt(n);
const usd=n=>n===null||n===undefined?'—':'$'+Number(n).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
const incomeUsd=(income,unitsPerUsd)=>income===null||income===undefined||!Number(unitsPerUsd)?'—':usd(Number(income)/Number(unitsPerUsd));
const analyticsCountryLabel=value=>String(value||'').trim();
function defaultDates(latestComplete=''){const end=latestComplete?new Date(`${latestComplete}T00:00:00Z`):new Date();if(!latestComplete)end.setUTCDate(end.getUTCDate()-1);const start=new Date(end);start.setUTCDate(start.getUTCDate()-29);dateTo.value=end.toISOString().slice(0,10);dateFrom.value=start.toISOString().slice(0,10)}
function tooltipTarget(value,label,tooltipValue,className='',element='strong'){const tag=element==='span'?'span':'strong';return `<${tag} class="analytics-tooltip-target ${esc(className)}" tabindex="0" data-tooltip-label="${esc(label)}" data-tooltip-value="${esc(tooltipValue)}" aria-label="${esc(value+'，'+label+'：'+tooltipValue)}">${esc(value)}</${tag}>`}
function metric(label,value,featured=false,tooltipValue=''){const rendered=tooltipValue&&tooltipValue!=='—'?tooltipTarget(value,'换算美元',tooltipValue):`<strong>${esc(value)}</strong>`;return `<div class="metric${featured?' featured':''}"><small>${esc(label)}</small>${rendered}</div>`}
function incomeWithUsdTooltip(income,unitsPerUsd,element='span',className=''){const value=money(income),converted=incomeUsd(income,unitsPerUsd);return converted==='—'?`<${element} class="${esc(className)}">${esc(value)}</${element}>`:tooltipTarget(value,'换算美元',converted,className,element)}
function cohortStat(label,value,meta,tooltipValue=''){const rendered=tooltipValue&&tooltipValue!=='—'?tooltipTarget(value,'换算美元',tooltipValue):`<strong>${esc(value)}</strong>`;return `<div class="cohort-stat"><small>${esc(label)}</small>${rendered}<span>${esc(meta)}</span></div>`}
function newcomerQualityGroup(day,revenue={},retention={},ranges={},unitsPerUsd){
  const incomeRange=ranges['income_d'+day]||{},retentionRange=ranges['retention_d'+day]||{};
  const incomeEnd=incomeRange.date_to||'—',retentionEnd=retentionRange.date_to||'—';
  const deadline=`收益截至 ${incomeEnd} · 留存截至 ${retentionEnd}`;
  const unavailable=retention.rate===null||retention.rate===undefined;
  const rate=unavailable?'—':(retention.rate*100).toFixed(1)+'%';
  const retainedCount=unavailable?`—/${fmt(retention.eligible??0)} 人`:`${fmt(retention.retained)}/${fmt(retention.eligible)} 人`;
  return `<section class="cohort-group" aria-label="首${esc(day)}日新人质量"><div class="cohort-group-head"><strong>首${esc(day)}日</strong><span>${esc(deadline)}</span></div><div class="cohort-pair">${cohortStat('人均收益',money(revenue.avg_income),`${fmt(revenue.cohort_count??0)} 人`,incomeUsd(revenue.avg_income,unitsPerUsd))}${cohortStat('收益留存',rate,retainedCount)}</div></section>`;
}
function renderTrendChart(rows){
  const trend=(rows||[]).slice(-14);
  if(!trend.length){trendChart.innerHTML='<div class="analytics-empty">暂无趋势数据</div>';trendRangeLabel.textContent='暂无数据';return}
  trendRangeLabel.textContent=`${trend[0].date} 至 ${trend[trend.length-1].date}`;
  const metrics=[
    {key:'total_income',label:'平台总收益',format:money},
    {key:'active_streamers',label:'收益活跃主播',format:fmt},
    {key:'new_streamers',label:'新增主播',format:fmt},
  ];
  trendChart.innerHTML=metrics.map(metricItem=>{
    const values=trend.map(item=>Number(item[metricItem.key]||0));
    const latest=values[values.length-1];
    const previous=values.length>1?values[values.length-2]:null;
    let change='暂无对比',changeClass='';
    if(previous!==null){
      if(previous===0){change=latest===0?'较上期持平':'较上期新增';changeClass=latest===0?'':'alert'}
      else{const delta=(latest-previous)/Math.abs(previous)*100;change=`较上期 ${delta>=0?'+':''}${delta.toFixed(1)}%`;if(Math.abs(delta)>=30)changeClass=delta<0?'drop':'alert'}
    }
    const min=Math.min(...values),max=Math.max(...values),range=max-min||1,width=520,height=76,pad=6;
    const points=values.map((value,index)=>{const x=values.length===1?width/2:pad+index*(width-pad*2)/(values.length-1);const y=pad+(max-value)*(height-pad*2)/range;return [x,y]});
    const line=points.map(point=>point.map(value=>value.toFixed(1)).join(',')).join(' ');
    const area=`${pad},${height-pad} ${line} ${width-pad},${height-pad}`;
    const lastPoint=points[points.length-1];
    const title=`${metricItem.label}：${metricItem.format(latest)}；${trend[0].date} 至 ${trend[trend.length-1].date}`;
    const renderedLatest=metricItem.key==='total_income'?incomeWithUsdTooltip(latest,state.payload?.income_units_per_usd,'strong','trend-metric-value'):`<strong class="trend-metric-value">${esc(metricItem.format(latest))}</strong>`;
    return `<div class="trend-metric"><div class="trend-metric-head"><div><span class="trend-metric-label">${esc(metricItem.label)}</span>${renderedLatest}</div><span class="trend-change ${changeClass}">${esc(change)}</span></div><svg class="trend-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="${esc(title)}"><title>${esc(title)}</title><line class="trend-baseline" x1="${pad}" y1="${height-pad}" x2="${width-pad}" y2="${height-pad}"></line><polygon class="trend-area" points="${area}"></polygon><polyline class="trend-line" points="${line}"></polyline><circle class="trend-point" cx="${lastPoint[0].toFixed(1)}" cy="${lastPoint[1].toFixed(1)}" r="3.5"></circle></svg><div class="trend-dates"><span>${esc(trend[0].date)}</span><span>${esc(trend[trend.length-1].date)}</span></div></div>`;
  }).join('');
}
function cohortValue(value,status,formatter=fmt,label='未结算',extraClass='',tooltipLabel='',tooltipValue=''){
  if(!['complete','partial'].includes(status)||value===null||value===undefined)return `<td class="numeric-col pending-cell" title="${esc(label)}">—</td>`;
  const formatted=formatter(value),rendered=tooltipValue&&tooltipValue!=='—'?tooltipTarget(formatted,tooltipLabel,tooltipValue,'','span'):esc(formatted);
  return `<td class="numeric-col ${extraClass}">${rendered}</td>`;
}
const weekBandClass=week=>Number(week)%2===1?'week-band-odd':'week-band-even';
function renderWeeklyCohorts(data){
  const weekly=data.weekly_cohorts||{};
  const visible=['timo','linky'].includes(data.app)&&weekly.available;
  timoCohortPanel.hidden=!visible;
  if(!visible)return;
  const isTimo=data.app==='timo';
  const diamondsPerUsd=Number(weekly.diamonds_per_usd||data.income_units_per_usd||0);
  const rows=weekly.rows||[];
  timoCohortTitle.textContent=`${data.app_label} 新主播周 Cohort`;
  cohortGuildControl.hidden=data.app!=='linky';
  renderCohortGuildOptions(state.guildOptions,guildFilter.value,countryFilter.value);
  cohortScope.textContent=weekly.cohort_date_from&&weekly.cohort_date_to?`完整周 ${weekly.cohort_date_from} 至 ${weekly.cohort_date_to}`:'暂无完整周';
  cohortDataAsOf.textContent=weekly.data_as_of?`数据截至 ${weekly.data_as_of} · 只使用完整收益日`:'暂无完整收益';
  const maxWeek=rows.reduce((max,row)=>Math.max(max,...(row.periods||[]).map(period=>Number(period.week)||0)),0);
  const periodWeeks=Array.from({length:maxWeek},(_,index)=>index+1);
  const periodHead=periodWeeks.map(week=>`<th class="week-band-head week-band-start ${weekBandClass(week)}" colspan="3">W${week}</th>`).join('');
  const cohortIncomeLabel=isTimo?'新粉钻石':'新主播钻石';
  const periodSubhead=periodWeeks.map(week=>`<th class="week-band-start ${weekBandClass(week)}">活跃数</th><th class="${weekBandClass(week)}">${cohortIncomeLabel}</th><th class="${weekBandClass(week)}">${week===1?'ARPU/$':'ARPPU/$'}</th>`).join('');
  timoCohortHead.innerHTML=isTimo?`<tr>
    <th class="sticky-week" rowspan="2">注册周</th><th rowspan="2">地区</th>
    <th colspan="2">注册 Cohort</th><th colspan="4">平台结算</th><th rowspan="2">全体收益</th>
    ${periodHead}
  </tr><tr>
    <th>新增数</th><th>认证数</th>
    <th>基础/$</th><th>7日CPA/$</th><th>10日CPA/$</th><th>合计/$</th>
    ${periodSubhead}
  </tr>`:`<tr>
    <th class="sticky-week" rowspan="2">注册周</th><th rowspan="2">地区</th>
    <th colspan="2">注册 Cohort</th><th rowspan="2">全体收益</th>${periodHead}
  </tr><tr><th>新增数</th><th>认证数</th>${periodSubhead}</tr>`;
  const columnCount=(isTimo?9:5)+periodWeeks.length*3;
  if(!rows.length){timoCohortRows.innerHTML=`<tr><td colspan="${columnCount}" class="analytics-empty">当前筛选范围暂无 ${esc(data.app_label)} 新增主播</td></tr>`;return}
  timoCohortRows.innerHTML=rows.map(row=>{
    const periods=new Map((row.periods||[]).map(period=>[Number(period.week),period]));
    const first=periods.get(1)||{};
    const periodCells=periodWeeks.map(week=>{const period=periods.get(week)||{},denominator=week===1?Number(row.new_streamers||0):Number(period.active_streamers||0),perUserDiamonds=denominator&&period.income_diamonds!==null&&period.income_diamonds!==undefined?`${fmt(Number(period.income_diamonds)/denominator)} 钻石`:'';return `${cohortValue(period.active_streamers,period.status)}${cohortValue(period.income_diamonds,period.status,fmt,'未结算','','换算美元',incomeUsd(period.income_diamonds,diamondsPerUsd))}${cohortValue(period.per_user_usd,period.status,usd,'未结算','','对应钻石',perUserDiamonds)}`}).join('');
    if(!isTimo)return `<tr>
      <td class="sticky-week"><strong>${esc(row.week_start)}</strong><br><span class="muted">至 ${esc(row.week_end)}</span></td>
      <td><strong>${esc(analyticsCountryLabel(row.country))}</strong></td>
      <td class="numeric-col">${esc(fmt(row.new_streamers))}</td>
      <td class="numeric-col" title="未认证 ${esc(fmt(row.non_certified_streamers))} 人">${esc(fmt(row.certified_streamers))}</td>
      ${cohortValue(row.platform_week_income_diamonds,first.status,fmt,'未结算','','换算美元',incomeUsd(row.platform_week_income_diamonds,diamondsPerUsd))}${periodCells}
    </tr>`;
    const settlement=row.settlement||{};
    const bonus7=settlement.bonus_7d||{};
    const bonus10=settlement.bonus_10d||{};
    return `<tr>
      <td class="sticky-week"><strong>${esc(row.week_start)}</strong><br><span class="muted">至 ${esc(row.week_end)}</span></td>
      <td><strong>${esc(row.country)}</strong></td>
      <td class="numeric-col">${esc(fmt(row.new_streamers))}</td>
      <td class="numeric-col" title="未认证 ${esc(fmt(row.non_certified_streamers))} 人">${esc(fmt(row.certified_streamers))}</td>
      <td class="numeric-col settlement-cell">${esc(usd(settlement.base_usd))}</td>
      ${cohortValue(bonus7.amount_usd,bonus7.status,usd,'7日窗口未完整','settlement-cell')}
      ${cohortValue(bonus10.amount_usd,bonus10.status,usd,'10日窗口未完整','settlement-cell')}
      ${cohortValue(settlement.total_usd,settlement.status,usd,'结算观察窗口未完整','settlement-cell')}
      ${cohortValue(row.platform_week_income_diamonds,first.status,fmt,'未结算','','换算美元',incomeUsd(row.platform_week_income_diamonds,diamondsPerUsd))}
      ${periodCells}
    </tr>`;
  }).join('');
}
const roiStateLabels={not_open:'待首次填报',missing:'待填写',draft:'草稿',published:'已发布',pending_settlement:'数据待成熟',complete:'已完成',policy_missing:'政策未配置'};
const roiInputFields=['ad_cost_usd','admin_cost_usd','customer_service_cost_usd','media_buyer_cost_usd','activity_cost_usd'];
const roiInputLabels={ad_cost_usd:'投放成本',admin_cost_usd:'Admin成本',customer_service_cost_usd:'客服成本',media_buyer_cost_usd:'投手成本',activity_cost_usd:'活动成本'};
const timoGuildNames={'Agency MX somente':'Royal Latam','TIMO001':'Royal ID','agency of BR somente':'Royal BR'};
const analyticsGuildDisplayName=value=>{const row=value&&typeof value==='object'?value:{};const raw=String(row.guild_name??value??'').trim();return String(row.guild_display_name||timoGuildNames[raw]||raw)};
const guildCountryTip=row=>{const country=String(row.country||'未标注');return tooltipTarget(analyticsGuildDisplayName(row),'国家',country,'guild-country-tip')};
function installAnalyticsTooltips(){
  const tip=document.createElement('div');tip.className='analytics-tooltip';tip.hidden=true;tip.setAttribute('role','tooltip');tip.innerHTML='<strong></strong><span></span>';document.body.appendChild(tip);
  const hide=()=>{tip.hidden=true};
  const show=target=>{
    tip.querySelector('strong').textContent=target.dataset.tooltipLabel||'';tip.querySelector('span').textContent=target.dataset.tooltipValue||'';tip.hidden=false;
    const rect=target.getBoundingClientRect(),margin=12,width=tip.offsetWidth,height=tip.offsetHeight;
    const left=Math.min(Math.max(rect.left+(rect.width-width)/2,margin),window.innerWidth-width-margin);
    const above=rect.top-height-10>=margin,top=above?rect.top-height-10:rect.bottom+10;
    tip.dataset.placement=above?'top':'bottom';tip.style.left=`${Math.round(left)}px`;tip.style.top=`${Math.round(top)}px`;
    tip.style.setProperty('--tooltip-arrow-left',`${Math.min(Math.max(rect.left+rect.width/2-left,12),width-12)}px`);
  };
  analyticsPanel.addEventListener('mouseover',event=>{const target=event.target.closest('.analytics-tooltip-target');if(target)show(target)});
  analyticsPanel.addEventListener('mouseout',event=>{const target=event.target.closest('.analytics-tooltip-target');if(target&&!target.contains(event.relatedTarget))hide()});
  analyticsPanel.addEventListener('focusin',event=>{const target=event.target.closest('.analytics-tooltip-target');if(target)show(target)});
  analyticsPanel.addEventListener('focusout',event=>{if(event.target.closest('.analytics-tooltip-target'))hide()});
  window.addEventListener('scroll',hide,{passive:true});window.addEventListener('resize',hide);
}
const pct=value=>value===null||value===undefined?'—':(Number(value)*100).toLocaleString('zh-CN',{maximumFractionDigits:2})+'%';
const policyGuildKey=(country,guild)=>`${country}\u001f${guild}`;
const splitPolicyGuildKey=value=>{const [country='',guild_name='']=String(value||'').split('\u001f');return {country,guild_name}};
function policyVersions(){
  const scope=splitPolicyGuildKey(policyGuild.value);
  return (state.policyData?.policies||[]).filter(item=>item.country===scope.country&&item.guild_name===scope.guild_name).sort((a,b)=>String(b.effective_from).localeCompare(String(a.effective_from)));
}
function renderPolicyTierRows(target,tiers=[]){
  target.innerHTML=(tiers||[]).map((tier,index)=>`<tr data-tier-index="${index}"><td><input class="tier-level" type="number" value="${index+1}" readonly /></td><td><input data-tier-field="threshold_income_units" type="number" min="0" step="1" value="${tier.threshold_income_units??''}" /></td><td><input data-tier-field="cumulative_reward_units" type="number" min="0" step="1" value="${tier.cumulative_reward_units??''}" /></td><td><input data-tier-field="incremental_reward_units" type="number" value="${tier.incremental_reward_units??''}" readonly /></td><td><button type="button" class="secondary tier-remove" aria-label="删除第 ${index+1} 档">×</button></td></tr>`).join('');
}
function recalcPolicyTierRewards(target){
  let previous=0;
  [...target.querySelectorAll('tr')].forEach((row,index)=>{row.querySelector('.tier-level').value=index+1;const cumulative=Number(row.querySelector('[data-tier-field="cumulative_reward_units"]').value||0);row.querySelector('[data-tier-field="incremental_reward_units"]').value=Math.max(cumulative-previous,0);previous=cumulative});
}
function appendPolicyTier(target){
  const rows=collectPolicyTiers(target,false);const last=rows[rows.length-1]||{};rows.push({tier_level:rows.length+1,threshold_income_units:'',cumulative_reward_units:'',incremental_reward_units:last.cumulative_reward_units||''});renderPolicyTierRows(target,rows);target.querySelector('tr:last-child [data-tier-field="threshold_income_units"]')?.focus();state.policyDirty=true;
}
function collectPolicyTiers(target,validate=true){
  let previousThreshold=-1,previousCumulative=0,valid=true;
  const rows=[...target.querySelectorAll('tr')].map((row,index)=>{const thresholdInput=row.querySelector('[data-tier-field="threshold_income_units"]'),cumulativeInput=row.querySelector('[data-tier-field="cumulative_reward_units"]');const threshold=Number(thresholdInput.value),cumulative=Number(cumulativeInput.value);const rowValid=threshold>previousThreshold&&threshold>0&&cumulative>=previousCumulative;thresholdInput.classList.toggle('invalid',validate&&!rowValid);cumulativeInput.classList.toggle('invalid',validate&&!rowValid);if(!rowValid)valid=false;const item={tier_level:index+1,threshold_income_units:threshold,cumulative_reward_units:cumulative,incremental_reward_units:cumulative-previousCumulative};previousThreshold=threshold;previousCumulative=cumulative;return item});
  return validate?{rows,valid}:rows;
}
function setPolicyMode(mode){
  const tiered=mode==='timo_tiered_1v1';document.querySelectorAll('.policy-flat-fields').forEach(item=>item.hidden=tiered);document.querySelectorAll('.policy-tier-fields').forEach(item=>item.hidden=!tiered);
}
function renderPolicyHistory(current=''){
  const versions=policyVersions();policyHistoryList.innerHTML=versions.length?versions.map(item=>`<button type="button" class="policy-version ${item.effective_from===current?'current':''}" data-effective-from="${esc(item.effective_from)}">${esc(item.effective_from)} · ${item.calculation_mode==='timo_tiered_1v1'?'1v1梯度':'固定政策'}</button>`).join(''):'<span class="muted">暂无版本</span>';
}
function fillPolicyForm(policy){
  const item=policy||{};policyEffectiveFrom.value=item.effective_from||state.weeklyRoi?.week_start||'';policyMode.value=item.calculation_mode||'flat';policyUnitsPerUsd.value=item.income_units_per_usd??(state.app==='timo'?20000:10000);policyCpsRate.value=item.cps_rate===null||item.cps_rate===undefined?'':Number(item.cps_rate)*100;policyNewcomerCpa.value=item.newcomer_cpa_usd??0;policyNonCertifiedCpa.value=item.non_certified_cpa_usd??0;policyCertifiedCpa.value=item.certified_cpa_usd??0;policyBonus7.value=item.bonus_7d_usd??0;policyBonus10.value=item.bonus_10d_usd??0;policyEligibleMin.value=item.guild_eligible_host_min_units??0;policyNote.value=item.policy_note||'';policyChangeReason.value='';renderPolicyTierRows(streamerTierRows,item.tiers?.streamer||[]);renderPolicyTierRows(guildTierRows,item.tiers?.guild||[]);setPolicyMode(policyMode.value);renderPolicyHistory(item.effective_from||'');state.policyDirty=false;policySaveMessage.textContent='';
}
function selectPolicyGuild(){fillPolicyForm(policyVersions()[0]||null)}
async function openPolicyDialog(){
  policySaveMessage.textContent='正在读取…';policyDialog.showModal();
  try{const response=await fetch('/api/ops/streamer-analytics/roi-policies?'+new URLSearchParams({app:state.app}));if(!response.ok)throw new Error('政策读取失败');state.policyData=await response.json();policyGuild.innerHTML=(state.policyData.guilds||[]).map(item=>`<option value="${esc(policyGuildKey(item.country,item.guild_name))}">${esc(analyticsGuildDisplayName(item))} · ${esc(item.country)}</option>`).join('');const missing=(state.weeklyRoi?.rows||[]).find(row=>!row.policy?.configured);if(missing)policyGuild.value=policyGuildKey(missing.country,missing.guild_name);selectPolicyGuild()}catch(error){policySaveMessage.textContent=error.message}
}
function closePolicyDialog(){if(state.policyDirty&&!window.confirm('当前政策尚未保存，确定关闭吗？'))return;state.policyDirty=false;policyDialog.close()}
function newPolicyVersion(){const latest=policyVersions()[0];fillPolicyForm(latest||null);const base=new Date(`${latest?.effective_from||state.weeklyRoi?.week_start}T00:00:00Z`);base.setUTCDate(base.getUTCDate()+7);policyEffectiveFrom.value=base.toISOString().slice(0,10);policyChangeReason.value='';renderPolicyHistory('');state.policyDirty=true;policyEffectiveFrom.focus()}
async function savePolicy(){
  const scope=splitPolicyGuildKey(policyGuild.value),tiered=policyMode.value==='timo_tiered_1v1',streamer=collectPolicyTiers(streamerTierRows,tiered),guild=collectPolicyTiers(guildTierRows,tiered);if(!policyEffectiveFrom.value||!Number(policyUnitsPerUsd.value)||!policyChangeReason.value.trim()||(tiered&&(!streamer.valid||!guild.valid||!guild.rows.length))){policySaveMessage.textContent='请补齐生效周、换算值、梯度和变更原因';return}
  const payload={app:state.app,...scope,effective_from:policyEffectiveFrom.value,calculation_mode:policyMode.value,income_units_per_usd:Number(policyUnitsPerUsd.value),cps_rate:tiered?0:Number(policyCpsRate.value||0)/100,newcomer_cpa_usd:Number(policyNewcomerCpa.value||0),non_certified_cpa_usd:Number(policyNonCertifiedCpa.value||0),certified_cpa_usd:Number(policyCertifiedCpa.value||0),bonus_7d_usd:Number(policyBonus7.value||0),bonus_10d_usd:Number(policyBonus10.value||0),guild_eligible_host_min_units:Number(policyEligibleMin.value||0),streamer_tiers:tiered?streamer.rows:[],guild_tiers:tiered?guild.rows:[],policy_note:policyNote.value.trim(),change_reason:policyChangeReason.value.trim()};
  savePolicyBtn.disabled=true;policySaveMessage.textContent='正在保存…';try{const response=await fetch('/api/ops/streamer-analytics/roi-policies',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const result=await response.json().catch(()=>({detail:'保存失败'}));if(!response.ok)throw new Error(result.detail||'保存失败');state.policyData={...(state.policyData||{}),policies:[...(state.policyData?.policies||[]).filter(item=>!(item.country===scope.country&&item.guild_name===scope.guild_name)),...(result.policies||[])]};state.policyDirty=false;fillPolicyForm((result.policies||[]).find(item=>item.effective_from===payload.effective_from));policySaveMessage.textContent='政策已保存，历史版本和审计已记录';const roiResponse=await fetch('/api/ops/streamer-analytics/weekly-roi?'+new URLSearchParams({app:state.app}));if(roiResponse.ok)renderWeeklyRoi(await roiResponse.json())}catch(error){policySaveMessage.textContent=error.message}finally{savePolicyBtn.disabled=false}
}
function installRoiDataScrollIsolation(){
  document.querySelectorAll('#weeklyRoiPanel .roi-table-scroll').forEach(scroller=>{
    scroller.addEventListener('wheel',event=>{
      const cell=event.target instanceof Element?event.target.closest('tbody td'):null;
      if(!cell||cell.classList.contains('roi-sticky'))return;
      const scale=event.deltaMode===1?16:(event.deltaMode===2?scroller.clientHeight:1);
      const shiftHorizontal=event.shiftKey&&!event.deltaX;
      event.preventDefault();
      scroller.scrollLeft+=(shiftHorizontal?event.deltaY:event.deltaX)*scale;
      scroller.scrollTop+=(shiftHorizontal?0:event.deltaY)*scale;
    },{passive:false});
  });
}
function roiScenarioAdjustments(){
  return {
    revenue:Number(roiRevenueInput.value||0)/100,
    retention:Number(roiRetentionInput.value||0)/100,
    acquisition:Number(roiAcquisitionInput.value||0)/100,
    shared:Number(roiSharedInput.value||0)/100,
  };
}
function calculateRoiScenario(portfolio,adjustments={}){
  let cumulativeIncome=0,cumulativeShared=0,breakEvenWeek=null;
  const periods=(portfolio?.periods||[]).filter(item=>['complete','partial'].includes(item.lifecycle_status)).map(item=>{
    const retentionFactor=item.week===1?1:Math.max(0,1+Number(adjustments.retention||0));
    const incrementalIncome=Number(item.incremental_income_usd||0)*Math.max(0,1+Number(adjustments.revenue||0))*retentionFactor;
    const weeklyShared=Number(item.allocated_shared_cost_usd||0)*Math.max(0,1+Number(adjustments.shared||0))*retentionFactor;
    const acquisition=Number(item.acquisition_cost_usd||0)*Math.max(0,1+Number(adjustments.acquisition||0));
    cumulativeIncome+=incrementalIncome;cumulativeShared+=weeklyShared;
    const lifecycleCost=acquisition+cumulativeShared;
    const roi=lifecycleCost?cumulativeIncome/lifecycleCost:null;
    if(breakEvenWeek===null&&roi!==null&&roi>=1)breakEvenWeek=item.week;
    return {...item,incremental_income_usd:incrementalIncome,cumulative_income_usd:cumulativeIncome,allocated_shared_cost_usd:weeklyShared,cumulative_shared_cost_usd:cumulativeShared,lifecycle_cost_usd:lifecycleCost,roi,profit_usd:cumulativeIncome-lifecycleCost,break_even_gap_usd:Math.max(lifecycleCost-cumulativeIncome,0)};
  });
  return {periods,break_even_week:breakEvenWeek,latest:periods.at(-1)||null};
}
function roiChartPath(values,x,y){return values.map((value,index)=>`${index?'L':'M'}${x(index)} ${y(value)}`).join(' ')}
function renderRoiLifecycleChart(portfolio,scenario=null){
  const actual=(portfolio?.periods||[]).filter(item=>['complete','partial'].includes(item.lifecycle_status)&&item.roi!==null&&item.roi!==undefined);
  if(!actual.length){roiLifecycleChart.setAttribute('viewBox','0 0 900 250');roiLifecycleChart.innerHTML='<text x="450" y="128" text-anchor="middle" fill="#718095" font-size="13">待发布完整周成本</text>';roiScenarioLegend.hidden=true;return}
  const scenarioRows=scenario?.periods||[];
  const width=900,height=250,margin={top:18,right:26,bottom:34,left:52},plotW=width-margin.left-margin.right,plotH=height-margin.top-margin.bottom;
  const values=[...actual.map(item=>Number(item.roi||0)),...scenarioRows.map(item=>Number(item.roi||0)),1];
  const yMax=Math.max(1.1,Math.ceil(Math.max(...values)*4)/4);
  const x=index=>margin.left+plotW*index/Math.max(actual.length-1,1),y=value=>margin.top+plotH-(Number(value||0)/yMax)*plotH;
  const ticks=Array.from({length:5},(_,index)=>yMax*index/4);
  const grid=ticks.map(value=>`<g><line x1="${margin.left}" y1="${y(value)}" x2="${width-margin.right}" y2="${y(value)}" stroke="#e2e8f0"/><text x="${margin.left-8}" y="${y(value)+4}" text-anchor="end" fill="#718095" font-size="10">${Math.round(value*100)}%</text></g>`).join('');
  const labels=actual.map((item,index)=>`<text x="${x(index)}" y="${height-10}" text-anchor="middle" fill="#718095" font-size="10">W${item.week}</text>`).join('');
  const actualLine=`<path d="${roiChartPath(actual.map(item=>item.roi),x,y)}" fill="none" stroke="#2f6bff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>${actual.map((item,index)=>`<circle cx="${x(index)}" cy="${y(item.roi)}" r="3.5" fill="#fff" stroke="#2f6bff" stroke-width="2"><title>W${item.week} ${pct(item.roi)}</title></circle>`).join('')}`;
  const scenarioLine=scenarioRows.length?`<path d="${roiChartPath(scenarioRows.map(item=>item.roi),x,y)}" fill="none" stroke="#d97706" stroke-width="2.5" stroke-dasharray="7 5" stroke-linecap="round" stroke-linejoin="round"/>`:'';
  const breakEven=`<line x1="${margin.left}" y1="${y(1)}" x2="${width-margin.right}" y2="${y(1)}" stroke="#b7791f" stroke-width="1.5" stroke-dasharray="5 4"/>`;
  roiLifecycleChart.setAttribute('viewBox',`0 0 ${width} ${height}`);roiLifecycleChart.innerHTML=grid+breakEven+labels+actualLine+scenarioLine;roiScenarioLegend.hidden=!scenarioRows.length;
}
function renderRoiSensitivity(portfolio){
  const baseline=calculateRoiScenario(portfolio,{}),base=baseline.latest?.roi;
  if(base===null||base===undefined){roiSensitivityList.innerHTML='<div class="analytics-empty">暂无可演算周期</div>';return}
  const cases=[
    ['收益提升 10%',{revenue:.1}],
    ['W2+ 留存提升 10%',{retention:.1}],
    ['获客成本降低 10%',{acquisition:-.1}],
    ['共享成本降低 10%',{shared:-.1}],
  ].map(([label,adjustments])=>({label,delta:Number(calculateRoiScenario(portfolio,adjustments).latest?.roi||0)-base}));
  const max=Math.max(...cases.map(item=>Math.abs(item.delta)),.0001);
  roiSensitivityList.innerHTML=cases.map(item=>`<div class="roi-sensitivity-item"><span>${esc(item.label)}</span><div class="roi-sensitivity-track"><div class="roi-sensitivity-bar" style="width:${Math.max(3,Math.abs(item.delta)/max*100)}%"></div></div><span class="roi-sensitivity-value ${item.delta>=0?'roi-positive':'roi-negative'}">${item.delta>=0?'+':''}${(item.delta*100).toFixed(1)}pp</span></div>`).join('');
}
function absoluteScenarioParameters(){return {unitPrice:Number(absoluteUnitPrice.value||0),w1Arpu:Number(absoluteW1Arpu.value||0),retentionW2:Number(absoluteRetentionW2.value||0)/100,activeCost:Number(absoluteActiveCost.value||0)}}
function calculateAbsoluteRoiScenario(portfolio,growth,params){
  const baseline=growth?.forecast?.actual_baseline||{},newStreamers=Number(baseline.new_streamers||0);
  const revenueScale=Number(baseline.w1_arpu_usd)>0?Math.max(0,params.w1Arpu/Number(baseline.w1_arpu_usd)):1;
  const retentionScale=Number(baseline.retention_w2)>0?Math.max(0,params.retentionW2/Number(baseline.retention_w2)):1;
  let cumulativeIncome=0,cumulativeShared=0,breakEvenWeek=null;
  const periods=(portfolio?.periods||[]).filter(item=>['complete','partial'].includes(item.lifecycle_status)).map(item=>{
    const week=Number(item.week||0),weekRetentionScale=week===1?1:retentionScale;
    const incrementalIncome=Number(item.incremental_income_usd||0)*revenueScale*weekRetentionScale;
    const scenarioActive=Number(item.active_streamers||0)*weekRetentionScale;
    const weeklyShared=scenarioActive*Math.max(0,params.activeCost);
    const acquisition=newStreamers*Math.max(0,params.unitPrice);
    cumulativeIncome+=incrementalIncome;cumulativeShared+=weeklyShared;
    const lifecycleCost=acquisition+cumulativeShared,roi=lifecycleCost?cumulativeIncome/lifecycleCost:null;
    if(breakEvenWeek===null&&roi!==null&&roi>=1)breakEvenWeek=week;
    return {...item,incremental_income_usd:incrementalIncome,cumulative_income_usd:cumulativeIncome,allocated_shared_cost_usd:weeklyShared,cumulative_shared_cost_usd:cumulativeShared,lifecycle_cost_usd:lifecycleCost,roi,profit_usd:cumulativeIncome-lifecycleCost,break_even_gap_usd:Math.max(lifecycleCost-cumulativeIncome,0)};
  });
  return {periods,break_even_week:breakEvenWeek,latest:periods.at(-1)||null};
}
function fillAbsoluteScenario(useStandard=false){
  const forecast=state.weeklyRoi?.growth?.forecast||{},actual=forecast.actual_baseline||{},standard=forecast.standard_baseline||{},source=useStandard&&forecast.standard_baseline?standard:actual;
  absoluteUnitPrice.value=source.unit_price_usd??'';absoluteW1Arpu.value=source.w1_arpu_usd??'';absoluteRetentionW2.value=source.retention_w2===null||source.retention_w2===undefined?'':(Number(source.retention_w2)*100).toFixed(1);absoluteActiveCost.value=source.active_cost_per_streamer_usd??'';
  absoluteBaselineNote.textContent=forecast.standard_baseline?`实值：${usd(actual.unit_price_usd)} / ${usd(actual.w1_arpu_usd)} / ${pct(actual.retention_w2)} / ${usd(actual.active_cost_per_streamer_usd)}；Timo 标准：${usd(standard.unit_price_usd)} / ${usd(standard.w1_arpu_usd)} / ${pct(standard.retention_w2)} / ${usd(standard.active_cost_per_streamer_usd)}`:'当前平台暂无外部标准，默认使用实值基准';
}
function setRoiScenarioMode(mode){
  state.roiScenarioMode=mode==='absolute'?'absolute':'relative';relativeScenarioBtn.classList.toggle('active',state.roiScenarioMode==='relative');absoluteScenarioBtn.classList.toggle('active',state.roiScenarioMode==='absolute');relativeScenarioControls.hidden=state.roiScenarioMode!=='relative';absoluteScenarioControls.hidden=state.roiScenarioMode!=='absolute';renderRoiScenario();
}
function renderRoiScenario(){
  const portfolio=state.weeklyRoi?.portfolio||{};
  const absolute=state.roiScenarioMode==='absolute';
  const scenario=absolute?calculateAbsoluteRoiScenario(portfolio,state.weeklyRoi?.growth||{},absoluteScenarioParameters()):calculateRoiScenario(portfolio,roiScenarioAdjustments());
  const hasChanges=absolute||[roiRevenueInput,roiRetentionInput,roiAcquisitionInput,roiSharedInput].some(input=>Number(input.value)!==0);
  [roiRevenueValue,roiRetentionValue,roiAcquisitionValue,roiSharedValue].forEach((output,index)=>{const input=[roiRevenueInput,roiRetentionInput,roiAcquisitionInput,roiSharedInput][index],value=Number(input.value||0);output.textContent=`${value>0?'+':''}${value}%`});
  roiScenarioResult.textContent=scenario.latest?pct(scenario.latest.roi):'—';
  renderRoiLifecycleChart(portfolio,hasChanges?scenario:null);
}
function renderGrowthDecision(growth){
  const available=Boolean(growth?.scorecard)||Boolean(growth?.periods?.length);growthDecisionPanel.hidden=!available;if(!available)return;
  const score=growth.scorecard||{},weeks=(growth.rolling_4w_weeks||[]).map(week=>String(week).slice(5)).join(' / ')||'—';
  growthDecisionScope.textContent='利润：全部公会 · 评分卡：当前筛选';
  growthMetrics.innerHTML=[
    ['滚动4周净利润',usd(growth.rolling_4w_profit_usd),weeks],
    ['W5+ 长尾利润池',usd(growth.w5_plus_profit_usd),growth.w5_plus_latest_week?`${String(growth.w5_plus_latest_week).slice(5)} 自然周`:'暂无完整自然周'],
    ['W5+ 长尾活跃',fmt(growth.w5_plus_active_streamers),growth.w5_plus_latest_week?`${String(growth.w5_plus_latest_week).slice(5)} 收益活跃`:'暂无完整自然周'],
    ['放量状态',({scale:'可放量',validate:'继续验证',hold:'暂缓',insufficient:'数据不足'})[score.scale_status]||'—',score.scale_reason||'—'],
  ].map(([label,value,meta],index)=>`<div class="growth-metric"><small>${esc(label)}</small><strong class="${index===3?'scale-state '+esc(score.scale_status||'insufficient'):''}">${esc(value)}</strong><span>${esc(meta)}</span></div>`).join('');
  const reference=score.reference||{};
  const items=[
    ['主播单价',usd(score.unit_price_usd),reference.unit_price_max_usd?`参考 ≤ ${usd(reference.unit_price_max_usd)}`:''],
    ['认证率',pct(score.certification_rate),reference.certification_rate_min?`参考 ≥ ${pct(reference.certification_rate_min)}`:''],
    ['收益率',pct(score.income_rate),reference.income_rate_min?`参考 ≥ ${pct(reference.income_rate_min)}`:''],
    ['首周 ARPU',usd(score.w1_arpu_usd),reference.w1_arpu_min_usd?`参考 ≥ ${usd(reference.w1_arpu_min_usd)}`:''],
    ['W2 留存',pct(score.retention_w2),''],['W4 留存',pct(score.retention_w4),''],['W8 留存',pct(score.retention_w8),''],
    ['单活跃成本',usd(score.active_cost_per_streamer_usd),reference.active_cost_target_usd?`参考 ≤ ${usd(reference.active_cost_target_usd)}`:''],
  ];
  growthScorecard.innerHTML=items.map(([label,value,meta])=>`<div class="scorecard-item" title="${esc(meta)}"><small>${esc(label)}</small><strong>${esc(value)}</strong></div>`).join('');
}
function renderRoiDecision(portfolio){
  const latest=portfolio?.latest||null,ready=portfolio?.status==='ready'&&latest;
  const roiClass=ready?(Number(latest.roi)>=1?'good':'bad'):'';
  roiKpis.innerHTML=[
    ['当前成熟周 ROI',ready?pct(latest.roi):'—',roiClass],
    ['回本周',ready?(portfolio.break_even_week?`W${portfolio.break_even_week}`:'未回本'):'—',portfolio.break_even_week?'good':(ready?'bad':'')],
    ['累计收入',ready?usd(latest.cumulative_income_usd):'—',''],
    ['生命周期全成本',ready?usd(latest.lifecycle_cost_usd):'—',''],
    ['距离打正',ready?usd(latest.break_even_gap_usd):'—',Number(latest?.break_even_gap_usd)>0?'bad':'good'],
  ].map(([label,value,className])=>`<div class="roi-kpi"><small>${esc(label)}</small><strong class="${className}">${esc(value)}</strong></div>`).join('');
  const covered=Number(portfolio?.covered_row_count||0),total=Number(portfolio?.row_count||0);
  roiDecisionScope.textContent=ready?`累计至 W${latest.week} · 成本 ${covered}/${total}`:'成本未完整';
  roiConclusion.textContent=portfolio?.conclusion||'—';
  roiConclusionMeta.textContent=ready?`成本 ${covered}/${total} · ${usd(latest.cumulative_income_usd)} / ${usd(latest.lifecycle_cost_usd)}`:'发布各观察周成本后自动计算';
  roiScenarioPanel.classList.toggle('is-unavailable',!ready);
  roiScenarioPanel.querySelectorAll('input,button').forEach(control=>{control.disabled=!ready});
  [roiRevenueInput,roiRetentionInput,roiAcquisitionInput,roiSharedInput].forEach(input=>{input.value='0'});
  fillAbsoluteScenario();
  const absoluteBaseline=state.weeklyRoi?.growth?.forecast?.actual_baseline||{};
  absoluteScenarioBtn.disabled=!ready||!Number(absoluteBaseline.new_streamers)||![absoluteBaseline.unit_price_usd,absoluteBaseline.w1_arpu_usd,absoluteBaseline.retention_w2,absoluteBaseline.active_cost_per_streamer_usd].every(value=>value!==null&&value!==undefined);
  setRoiScenarioMode('relative');renderRoiLifecycleChart(portfolio);renderRoiSensitivity(portfolio);renderRoiScenario();
}
function renderWeeklyRoi(payload){
  state.weeklyRoi=payload||{available:false,rows:[]};
  const rows=state.weeklyRoi.rows||[];
  weeklyRoiScope.textContent=state.weeklyRoi.week_start?`${state.weeklyRoi.week_start} 至 ${state.weeklyRoi.week_end}`:'—';
  const missing=rows.filter(row=>['missing','not_open'].includes(row.input_status||row.state)).length;
  const draft=rows.filter(row=>row.input_status==='draft').length;
  const policyMissing=rows.filter(row=>!row.policy?.configured).length;
  if(!state.weeklyRoi.available){
    weeklyRoiStatus.textContent='当前平台暂未配置';
  }else if(!state.weeklyRoi.editable){
    weeklyRoiStatus.textContent=`${state.weeklyRoi.available_from} 开放填报`;
  }else{
    weeklyRoiStatus.textContent=policyMissing?`${policyMissing} 个公会政策未配置`:`${rows.length-missing-draft}/${rows.length} 个公会已发布`;
  }
  openRoiEntryBtn.disabled=!state.weeklyRoi.editable||!rows.length;
  openPolicyBtn.disabled=!rows.length;
  openRoiEntryBtn.textContent=rows.some(row=>row.input_status==='published')?'更正成本':'填写上周成本';
  renderGrowthDecision(state.weeklyRoi.growth||{});
  renderRoiDecision(state.weeklyRoi.portfolio||{});
  weeklyRoiSummaryRows.innerHTML=rows.length?rows.map(row=>{
    const settlement=row.platform_settlement||{};
    const stateLabel=roiStateLabels[row.state]||row.state||'—';
    const roiClass=row.overall_roi===null||row.overall_roi===undefined?'':(row.overall_roi>=1?'roi-positive':'roi-negative');
    const profitClass=row.overall_profit_usd===null||row.overall_profit_usd===undefined?'':(row.overall_profit_usd>=0?'roi-positive':'roi-negative');
    const settlementText=settlement.total_usd===null||settlement.total_usd===undefined?'—':usd(settlement.total_usd)+(settlement.status==='complete'?'':' 暂估');
    const tiered=row.policy?.settlement_basis==='weekly_tier';
    const settlementRule=tiered?'每周梯度':pct(row.policy?.cps_rate);
    const settlementRuleTitle=tiered?'按当周公会有效1v1收益命中最高档，取累计政策奖励':'固定比例，包含平台补贴，允许超过100%';
    return `<tr><td class="roi-sticky">${guildCountryTip(row)}</td><td><span class="roi-status ${esc(row.state)}">${esc(stateLabel)}</span></td><td class="numeric-col">${fmt(row.new_streamers)}</td><td class="numeric-col">${usd(row.total_cost_usd)}</td><td class="numeric-col settlement-cell">${esc(settlementText)}</td><td class="numeric-col" title="${esc(settlementRuleTitle)}">${esc(settlementRule)}</td><td class="numeric-col">${usd(row.whole_week_income_usd)}</td><td class="numeric-col">${usd(row.overall_income_usd)}</td><td class="numeric-col ${roiClass}">${pct(row.overall_roi)}</td><td class="numeric-col ${profitClass}">${usd(row.overall_profit_usd)}</td><td class="numeric-col">${usd(row.cost_per_new_streamer_usd)}</td></tr>`;
  }).join(''):'<tr><td colspan="11" class="analytics-empty">当前筛选范围暂无公会</td></tr>';
  const maxWeek=rows.reduce((max,row)=>Math.max(max,...(row.periods||[]).map(item=>Number(item.week)||0)),0);
  const weeks=Array.from({length:maxWeek},(_,index)=>index+1);
  weeklyRoiCohortHead.innerHTML=`<tr><th class="roi-sticky" rowspan="2">公会</th><th rowspan="2">获客成本/$</th><th rowspan="2">平台结算/$</th>${weeks.map(week=>`<th class="week-band-head week-band-start ${weekBandClass(week)}" colspan="3">W${week}</th>`).join('')}</tr><tr>${weeks.map(week=>`<th class="week-band-start ${weekBandClass(week)}">累计收入/$</th><th class="${weekBandClass(week)}">生命周期ROI</th><th class="${weekBandClass(week)}">距离打正/$</th>`).join('')}</tr>`;
  const columns=3+weeks.length*3;
  weeklyRoiCohortRows.innerHTML=rows.length?rows.map(row=>{
    const periods=new Map((row.periods||[]).map(item=>[Number(item.week),item]));
    return `<tr><td class="roi-sticky">${guildCountryTip(row)}</td><td class="numeric-col">${usd(row.lifecycle?.acquisition_cost_usd)}</td><td class="numeric-col settlement-cell">${usd(row.platform_settlement?.total_usd)}</td>${weeks.map(week=>{const item=periods.get(week)||{};if(item.status!=='complete'||item.lifecycle_status!=='complete')return '<td class="pending-cell numeric-col">—</td><td class="pending-cell numeric-col">—</td><td class="pending-cell numeric-col">—</td>';const roiClass=item.lifecycle_roi===null||item.lifecycle_roi===undefined?'':(item.lifecycle_roi>=1?'roi-positive':'roi-negative');const gapClass=Number(item.break_even_gap_usd||0)>0?'roi-negative':'roi-positive';return `<td class="numeric-col">${usd(item.cumulative_income_usd)}</td><td class="numeric-col ${roiClass}">${pct(item.lifecycle_roi)}</td><td class="numeric-col ${gapClass}">${usd(item.break_even_gap_usd)}</td>`}).join('')}</tr>`;
  }).join(''):`<tr><td colspan="${columns}" class="analytics-empty">暂无回本数据</td></tr>`;
}
function entryStateLabel(row){return roiStateLabels[row.input_status]||roiStateLabels[row.state]||'待填写'}
function entryTotal(rowElement){
  const inputs=[...rowElement.querySelectorAll('input[type=number]')];
  const complete=inputs.every(input=>input.value!=='');
  const total=complete?inputs.reduce((sum,input)=>sum+Number(input.value||0),0):null;
  rowElement.querySelector('.roi-entry-total').textContent=total===null?'—':usd(total);
}
function renderRoiEntryRows(){
  const rows=state.weeklyRoi?.rows||[];
  roiEntryRows.innerHTML=rows.map((row,rowIndex)=>`<tr data-row-index="${rowIndex}" data-country="${esc(row.country)}" data-guild="${esc(row.guild_name)}"><td class="entry-guild"><strong>${esc(analyticsGuildDisplayName(row))}</strong><br><span class="muted">${esc(row.country)}</span></td>${roiInputFields.map(field=>`<td><input type="number" min="0" step="0.01" inputmode="decimal" data-field="${field}" aria-label="${esc(analyticsGuildDisplayName(row)+' '+roiInputLabels[field])}" value="${row.input?.[field]??''}" /></td>`).join('')}<td class="numeric-col roi-entry-total">${usd(row.total_cost_usd)}</td><td><span class="roi-status ${esc(row.input_status)}">${esc(entryStateLabel(row))}</span></td></tr>`).join('');
  roiCorrectionWrap.hidden=!rows.some(row=>row.input_status==='published');
  roiCorrectionReason.value='';
  roiEntryRows.querySelectorAll('tr').forEach(entryTotal);
}
function openRoiEntry(){
  if(!state.weeklyRoi?.editable)return;
  state.roiDirty=false;roiSaveMessage.textContent='';
  roiEntryScope.textContent=`${state.app.toUpperCase()} · ${state.weeklyRoi.week_start} 至 ${state.weeklyRoi.week_end}`;
  renderRoiEntryRows();roiEntryDialog.showModal();
}
function closeRoiEntry(){
  if(state.roiDirty&&!window.confirm('当前有未保存的成本数据，确定关闭吗？'))return;
  state.roiDirty=false;roiEntryDialog.close();
}
function collectRoiEntryRows(publishing=false){
  let valid=true;
  const rows=[...roiEntryRows.querySelectorAll('tr')].map(rowElement=>{
    const result={country:rowElement.dataset.country,guild_name:rowElement.dataset.guild,correction_reason:roiCorrectionReason.value.trim()};
    rowElement.querySelectorAll('input[type=number]').forEach(input=>{
      const missing=input.value==='';
      input.classList.toggle('invalid',publishing&&missing);
      if(publishing&&missing)valid=false;
      result[input.dataset.field]=missing?null:Number(input.value);
    });
    return result;
  });
  if(publishing&&!roiCorrectionWrap.hidden&&state.weeklyRoi.rows.some(row=>row.input_status==='published')&&!roiCorrectionReason.value.trim()){
    roiCorrectionReason.focus();roiSaveMessage.textContent='更正已发布数据时请填写原因';valid=false;
  }
  return {rows,valid};
}
async function saveRoiInputs(status){
  const publishing=status==='published';
  const collected=collectRoiEntryRows(publishing);
  if(!collected.valid)return;
  const buttons=[saveRoiDraftBtn,publishRoiBtn,copyPreviousRoiBtn];buttons.forEach(button=>button.disabled=true);
  roiSaveMessage.textContent=publishing?'正在发布…':'正在保存草稿…';
  try{
    const response=await fetch('/api/ops/streamer-analytics/weekly-roi',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({app:state.app,week_start:state.weeklyRoi.week_start,status,rows:collected.rows})});
    if(!response.ok){const detail=await response.json().catch(()=>({detail:'保存失败'}));throw new Error(detail.detail||'保存失败')}
    const payload=await response.json();state.roiDirty=false;renderWeeklyRoi(payload);roiEntryDialog.close();
  }catch(error){roiSaveMessage.textContent=error.message}
  finally{buttons.forEach(button=>button.disabled=false)}
}
async function copyPreviousRoi(){
  const previous=new Date(`${state.weeklyRoi.week_start}T00:00:00Z`);previous.setUTCDate(previous.getUTCDate()-7);
  roiSaveMessage.textContent='正在读取上周…';copyPreviousRoiBtn.disabled=true;
  try{
    const query=new URLSearchParams({app:state.app,week_start:previous.toISOString().slice(0,10)});
    const response=await fetch('/api/ops/streamer-analytics/weekly-roi?'+query);
    if(!response.ok)throw new Error('没有可复制的上周数据');
    const payload=await response.json();
    const prior=new Map((payload.rows||[]).filter(row=>row.input_status!=='missing').map(row=>[`${row.country}\u001f${row.guild_name}`,row.input||{}]));
    let copied=0;
    roiEntryRows.querySelectorAll('tr').forEach(rowElement=>{const values=prior.get(`${rowElement.dataset.country}\u001f${rowElement.dataset.guild}`);if(!values)return;rowElement.querySelectorAll('input[type=number]').forEach(input=>{input.value=values[input.dataset.field]??''});entryTotal(rowElement);copied++});
    if(!copied)throw new Error('没有可复制的上周数据');
    state.roiDirty=true;roiSaveMessage.textContent=`已复制 ${copied} 个公会，请核对后保存`;
  }catch(error){roiSaveMessage.textContent=error.message}
  finally{copyPreviousRoiBtn.disabled=false}
}
function renderGuildOptions(guilds,selected='',country=''){
  const options=[...new Map((guilds||[]).filter(item=>!country||String(item.country||'').trim()===country).map(item=>[String(item.guild_name||'').trim(),item]).filter(([name])=>Boolean(name))).entries()];
  guildFilter.innerHTML='<option value="">全部公会</option>'+options.map(([name,item])=>`<option value="${esc(name)}">${esc(analyticsGuildDisplayName(item))}</option>`).join('');
  guildFilter.value=options.some(([name])=>name===selected)?selected:'';
}
function renderCohortGuildOptions(guilds,selected='',country=''){
  const options=[...new Map((guilds||[]).filter(item=>!country||String(item.country||'').trim()===country).map(item=>[String(item.guild_name||'').trim(),item]).filter(([name])=>Boolean(name))).entries()];
  cohortGuildFilter.innerHTML='<option value="">全部公会</option>'+options.map(([name,item])=>`<option value="${esc(name)}">${esc(analyticsGuildDisplayName(item))}</option>`).join('');
  cohortGuildFilter.value=options.some(([name])=>name===selected)?selected:'';
}
function renderCountryOptions(countries,selected=''){
  const names=(countries||[]).map(item=>String(item||'').trim()).filter(Boolean);
  countryFilter.innerHTML='<option value="">全部国家</option>'+names.map(name=>`<option value="${esc(name)}">${esc(analyticsCountryLabel(name))}</option>`).join('');
  countryFilter.value=names.includes(selected)?selected:'';
}
function renderCohortCountryQuick(countries,selected=''){
  const items=[{value:'',label:'全部'},...(countries||[]).map(value=>({value:String(value||'').trim(),label:analyticsCountryLabel(value)})).filter(item=>item.value)];
  cohortCountryQuick.innerHTML=items.map(item=>`<button type="button" data-country="${esc(item.value)}" class="${item.value===selected?'active':''}" aria-pressed="${item.value===selected?'true':'false'}">${esc(item.label)}</button>`).join('');
}
function setCohortCountryQuickDisabled(disabled){cohortCountryQuick.querySelectorAll('button').forEach(button=>{button.disabled=disabled})}
const transientAnalyticsStatuses=new Set([502,503,504]);
const analyticsRetryDelays=[800,1600,3200,6400];
const wait=milliseconds=>new Promise(resolve=>setTimeout(resolve,milliseconds));
async function fetchAnalytics(url,requestId,signal){
  for(let attempt=0;;attempt+=1){
    let response;
    try{response=await fetch(url,{signal})}
    catch(error){if(error?.name==='AbortError'||attempt>=analyticsRetryDelays.length)throw error}
    if(response&&(response.ok||!transientAnalyticsStatuses.has(response.status)))return response;
    if(attempt>=analyticsRetryDelays.length)return response;
    await wait(analyticsRetryDelays[attempt]);
    if(signal?.aborted)throw new DOMException('Aborted','AbortError');
  }
}
async function loadAnalyticsMetadata(){
  try{
    const response=await fetch('/api/ops/streamer-analytics/metadata',{cache:'no-store'});
    if(!response.ok)return;
    const metadata=await response.json();
    state.latestDataAsOf=Object.fromEntries(Object.entries(metadata.apps||{}).map(([app,item])=>[app,String(item?.data_as_of||'')]).filter(([,value])=>value));
    state.metadataReady=true;
  }catch(_){state.metadataReady=false}
}
function alignDatesToLatest(){
  if(!state.autoLatestCompleteRange)return;
  const latest=state.latestDataAsOf[state.app]||'';
  if(latest)defaultDates(latest);
}
function render(data){
  state.payload=data;
  state.guildOptions=data.guild_options||data.guilds||[];
  dataAsOf.textContent=data.data_as_of?`收益截至 ${data.data_as_of}`:'收益未就绪';
  overviewRange.textContent=`${data.app_label} · ${data.date_from} 至 ${data.date_to}${data.country?' · '+analyticsCountryLabel(data.country):' · 全部国家'}${data.guild_name?' · '+analyticsGuildDisplayName(data):' · 全部公会'}`;
  newcomerScope.textContent=data.newcomer_cohort_date_from?`样本起点 ${data.newcomer_cohort_date_from}`:'样本起点 —';
  metrics.innerHTML=[
    metric('平台总收益',money(data.summary.total_income),true,incomeUsd(data.summary.total_income,data.income_units_per_usd)),
    metric('收益活跃主播',fmt(data.summary.active_streamers)),
    metric('区间新增主播',fmt(data.summary.new_streamers)),
    metric('主播总数',fmt(data.summary.streamer_count)),
  ].join('');
  if(data.capabilities.newcomer_revenue){
    const revenueByDay=new Map(data.newcomer_revenue.map(item=>[Number(item.days),item]));
    const retentionByDay=new Map(data.retention.map(item=>[Number(item.day),item]));
    const cells=[];
    [1,7,30].forEach(day=>cells.push(newcomerQualityGroup(day,revenueByDay.get(day)||{},retentionByDay.get(day)||{},data.newcomer_metric_ranges||{},data.income_units_per_usd)));
    cohorts.innerHTML=cells.join('');
  }else{
    cohorts.innerHTML='<div class="analytics-empty">当前平台暂无收益留存数据</div>';
  }
  renderWeeklyCohorts(data);
  renderTrendChart(data.trend||[]);
  guildDetailPanel.hidden=Boolean(data.guild_name);
  guildRows.innerHTML=data.guilds.length?data.guilds.map(item=>`<tr><td><strong>${esc(analyticsGuildDisplayName(item))}</strong></td><td class="numeric-col">${fmt(item.streamer_count)}</td><td class="numeric-col">${fmt(item.new_streamers)}</td><td class="numeric-col">${fmt(item.active_streamers)}</td><td class="income-cell numeric-col">${incomeWithUsdTooltip(item.total_income,data.income_units_per_usd)}</td><td class="income-cell numeric-col">${incomeUsd(item.total_income,data.income_units_per_usd)}</td></tr>`).join(''):'<tr><td colspan="6" class="analytics-empty">暂无公会数据</td></tr>';
  guildCountLabel.textContent=`统计周期 ${data.date_from} 至 ${data.date_to} · ${data.guilds.length} 个公会`;
  renderCountryOptions(data.countries,countryFilter.value);
  renderCohortCountryQuick(data.countries,countryFilter.value);
  renderGuildOptions(state.guildOptions,guildFilter.value,countryFilter.value);
}
async function load(){
  const requestId=++latestRequestId;
  activeLoadController?.abort();
  const controller=new AbortController();activeLoadController=controller;
  analyticsPanel.setAttribute('aria-busy','true');
  refreshBtn.disabled=true;countryFilter.disabled=true;guildFilter.disabled=true;cohortGuildFilter.disabled=true;setCohortCountryQuickDisabled(true);
  try{
    const query=new URLSearchParams({app:state.app,date_from:dateFrom.value,date_to:dateTo.value,limit:'30'});
    if(countryFilter.value)query.set('country',countryFilter.value);
    if(guildFilter.value)query.set('guild_name',guildFilter.value);
    const roiQuery=new URLSearchParams({app:state.app});
    if(countryFilter.value)roiQuery.set('country',countryFilter.value);
    if(guildFilter.value)roiQuery.set('guild_name',guildFilter.value);
    const responsePromise=fetchAnalytics('/api/ops/streamer-analytics/summary?'+query,requestId,controller.signal);
    const roiResponsePromise=fetchAnalytics('/api/ops/streamer-analytics/weekly-roi?'+roiQuery,requestId,controller.signal)
      .then(response=>({response,error:null}),error=>({response:null,error}));
    const response=await responsePromise;
    if(!response.ok)throw new Error(await response.text());
    const data=await response.json();
    if(requestId===latestRequestId&&!state.metadataReady&&state.autoLatestCompleteRange&&data.data_as_of&&dateTo.value!==data.data_as_of){defaultDates(data.data_as_of);load();return}
    if(requestId===latestRequestId){render(data);analyticsPanel.setAttribute('aria-busy','false');refreshBtn.disabled=false;countryFilter.disabled=false;guildFilter.disabled=false;cohortGuildFilter.disabled=false;setCohortCountryQuickDisabled(false);refreshBtn.textContent='更新数据'}
    const roiResult=await roiResponsePromise;
    if(roiResult.error){if(roiResult.error.name==='AbortError')return;if(requestId===latestRequestId)renderWeeklyRoi({available:false,rows:[],error:roiResult.error.message||'ROI 数据加载失败'});return}
    const roiResponse=roiResult.response;
    const roiData=roiResponse.ok?await roiResponse.json():{available:false,rows:[],error:await roiResponse.text()};
    if(requestId===latestRequestId)renderWeeklyRoi(roiData);
  }catch(error){if(error?.name!=='AbortError'&&requestId===latestRequestId&&!state.payload){dataAsOf.textContent='暂无可用数据';dataAsOf.title=error.message}}
  finally{if(requestId===latestRequestId){analyticsPanel.setAttribute('aria-busy','false');refreshBtn.disabled=false;countryFilter.disabled=false;guildFilter.disabled=false;cohortGuildFilter.disabled=false;setCohortCountryQuickDisabled(false);refreshBtn.textContent='更新数据'}}
}
function activateApp(button,{moveFocus=false}={}){
  if(!button)return;
  const changed=button.dataset.app!==state.app;
  state.app=button.dataset.app;
  appTabButtons.forEach(item=>{const selected=item===button;item.classList.toggle('active',selected);item.setAttribute('aria-selected',String(selected));item.setAttribute('tabindex',selected?'0':'-1')});
  analyticsPanel.setAttribute('aria-labelledby',button.id);
  if(moveFocus)button.focus();
  if(changed){state.guildOptions=[];countryFilter.value='';guildFilter.value='';cohortGuildFilter.value='';renderCountryOptions([]);renderCohortCountryQuick([]);renderGuildOptions([]);renderCohortGuildOptions([]);alignDatesToLatest();load()}
}
appTabsControl.addEventListener('click',event=>activateApp(event.target.closest('button[data-app]')));
appTabsControl.addEventListener('keydown',event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;const current=appTabButtons.indexOf(document.activeElement);if(current<0)return;let next=current;if(event.key==='ArrowLeft')next=(current-1+appTabButtons.length)%appTabButtons.length;if(event.key==='ArrowRight')next=(current+1)%appTabButtons.length;if(event.key==='Home')next=0;if(event.key==='End')next=appTabButtons.length-1;event.preventDefault();activateApp(appTabButtons[next],{moveFocus:true})});
cohortCountryQuick.addEventListener('click',event=>{const button=event.target.closest('button[data-country]');if(!button||button.disabled)return;const country=button.dataset.country||'';if(country===countryFilter.value)return;countryFilter.value=country;guildFilter.value='';renderGuildOptions(state.guildOptions,'',country);renderCohortGuildOptions(state.guildOptions,'',country);renderCohortCountryQuick(state.payload?.countries||[],country);load()});
cohortGuildFilter.addEventListener('change',()=>{guildFilter.value=cohortGuildFilter.value;load()});
countryFilter.addEventListener('change',()=>{guildFilter.value='';renderGuildOptions(state.guildOptions,'',countryFilter.value);renderCohortGuildOptions(state.guildOptions,'',countryFilter.value);renderCohortCountryQuick(state.payload?.countries||[],countryFilter.value)});
guildFilter.addEventListener('change',()=>{cohortGuildFilter.value=guildFilter.value});
dateFrom.addEventListener('change',()=>{state.autoLatestCompleteRange=false});dateTo.addEventListener('change',()=>{state.autoLatestCompleteRange=false});
openRoiEntryBtn.addEventListener('click',openRoiEntry);
openPolicyBtn.addEventListener('click',openPolicyDialog);
closePolicyBtn.addEventListener('click',closePolicyDialog);
policyGuild.addEventListener('change',selectPolicyGuild);
policyMode.addEventListener('change',()=>{setPolicyMode(policyMode.value);state.policyDirty=true});
newPolicyVersionBtn.addEventListener('click',newPolicyVersion);
savePolicyBtn.addEventListener('click',savePolicy);
addStreamerTierBtn.addEventListener('click',()=>appendPolicyTier(streamerTierRows));
addGuildTierBtn.addEventListener('click',()=>appendPolicyTier(guildTierRows));
[streamerTierRows,guildTierRows].forEach(target=>{target.addEventListener('click',event=>{const button=event.target.closest('.tier-remove');if(!button)return;button.closest('tr').remove();recalcPolicyTierRewards(target);state.policyDirty=true});target.addEventListener('input',()=>{recalcPolicyTierRewards(target);state.policyDirty=true})});
policyDialog.querySelectorAll('input,select,textarea').forEach(input=>input.addEventListener('input',()=>{state.policyDirty=true}));
policyHistoryList.addEventListener('click',event=>{const button=event.target.closest('[data-effective-from]');if(!button)return;fillPolicyForm(policyVersions().find(item=>item.effective_from===button.dataset.effectiveFrom)||null)});
policyDialog.addEventListener('cancel',event=>{if(state.policyDirty){event.preventDefault();closePolicyDialog()}});
closeRoiEntryBtn.addEventListener('click',closeRoiEntry);
saveRoiDraftBtn.addEventListener('click',()=>saveRoiInputs('draft'));
publishRoiBtn.addEventListener('click',()=>saveRoiInputs('published'));
copyPreviousRoiBtn.addEventListener('click',copyPreviousRoi);
roiEntryRows.addEventListener('input',event=>{const input=event.target.closest('input[type=number]');if(!input)return;input.classList.toggle('invalid',input.value!==''&&Number(input.value)<0);entryTotal(input.closest('tr'));state.roiDirty=true;roiSaveMessage.textContent='尚未保存'});
roiEntryRows.addEventListener('paste',event=>{
  const target=event.target.closest('input[type=number]');const text=event.clipboardData?.getData('text/plain')||'';
  if(!target||(!text.includes('\t')&&!text.includes('\n')))return;
  event.preventDefault();
  const grid=[...roiEntryRows.querySelectorAll('tr')].map(row=>[...row.querySelectorAll('input[type=number]')]);
  const startRow=Number(target.closest('tr').dataset.rowIndex||0),startCol=grid[startRow].indexOf(target);
  text.trim().split(/\r?\n/).forEach((line,rowOffset)=>line.split('\t').forEach((value,colOffset)=>{const input=grid[startRow+rowOffset]?.[startCol+colOffset];if(input)input.value=value.trim()}));
  grid.forEach(row=>{if(row[0])entryTotal(row[0].closest('tr'))});state.roiDirty=true;roiSaveMessage.textContent='已粘贴，请核对后保存';
});
roiEntryDialog.addEventListener('cancel',event=>{if(state.roiDirty){event.preventDefault();closeRoiEntry()}});
window.addEventListener('beforeunload',event=>{if(!state.roiDirty&&!state.policyDirty)return;event.preventDefault();event.returnValue=''});
[roiRevenueInput,roiRetentionInput,roiAcquisitionInput,roiSharedInput].forEach(input=>input.addEventListener('input',renderRoiScenario));
relativeScenarioBtn.addEventListener('click',()=>setRoiScenarioMode('relative'));
absoluteScenarioBtn.addEventListener('click',()=>{fillAbsoluteScenario(true);setRoiScenarioMode('absolute')});
[absoluteUnitPrice,absoluteW1Arpu,absoluteRetentionW2,absoluteActiveCost].forEach(input=>input.addEventListener('input',renderRoiScenario));
resetRoiScenarioBtn.addEventListener('click',()=>{[roiRevenueInput,roiRetentionInput,roiAcquisitionInput,roiSharedInput].forEach(input=>{input.value='0'});fillAbsoluteScenario();renderRoiScenario()});
async function initializeAnalytics(){defaultDates();await loadAnalyticsMetadata();alignDatesToLatest();load()}
refreshBtn.addEventListener('click',load);installRoiDataScrollIsolation();installAnalyticsTooltips();initializeAnalytics();
</script>
</body></html>'''
