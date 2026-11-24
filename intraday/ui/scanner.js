/* Intraday Scanner tab. Self-contained: the app calls window.IntradayScanner.render(mainElement) for the Scanner tab.
   Polls /scanner/state (cheap when nothing changed), keeps cards keyed by symbol so typing and charts survive refreshes,
   and raises toasts / sounds / browser notifications for new alerts even while another tab is open.
   Educational tool: signals only, it never places an order. */
(function () {
  'use strict';
  const BASE = window.SCAN_SERVED || null;
  const POLL_MS = 3000;
  const PREF_KEY = 'intraday.prefs.v1';
  const $ = (s, el) => (el || document).querySelector(s);
  const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const px = n => n == null ? '—' : Number(n).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const rs = n => n == null ? '—' : '₹' + Number(n).toLocaleString('en-IN', { maximumFractionDigits: 0 });
  const pct = n => n == null ? '—' : (n > 0 ? '+' : '') + Number(n).toFixed(2) + '%';
  const ist = iso => iso ? new Date(iso).toLocaleTimeString('en-GB', { timeZone: 'Asia/Kolkata', hour12: false }) : '—';
  const cssv = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

  // state -> [tone, label, icon]; tone 'side' = green for long, red for short
  const STATE = {
    WATCHING: ['grey', 'Watching', 'eye'], BREAKOUT_CONFIRMED: ['side', 'Breakout confirmed', 'bolt'],
    SETUP_FORMING: ['side', 'Armed · setup formed', 'target'], APPROACHING_ENTRY: ['amber', 'Approaching entry', 'near'],
    ENTRY_TRIGGERED: ['blue', 'Entry triggered', 'play'], IN_TRADE: ['blue', 'In trade', 'pulse'],
    APPROACHING_EXIT: ['purple', 'Approaching exit', 'alert'], EXIT_SIGNAL: ['purple', 'Exit signal', 'exit'],
    CLOSED: ['grey', 'Closed', 'check'], INVALIDATED: ['grey', 'Invalidated', 'x']
  };
  const URGENCY = { APPROACHING_EXIT: 0, APPROACHING_ENTRY: 1, EXIT_SIGNAL: 2, ENTRY_TRIGGERED: 3, IN_TRADE: 4, SETUP_FORMING: 5, BREAKOUT_CONFIRMED: 6, CLOSED: 7, INVALIDATED: 8, WATCHING: 9 };
  const STAGES = { active: ['BREAKOUT_CONFIRMED', 'SETUP_FORMING', 'APPROACHING_ENTRY', 'ENTRY_TRIGGERED', 'IN_TRADE', 'APPROACHING_EXIT', 'EXIT_SIGNAL'],
    approaching: ['APPROACHING_ENTRY', 'APPROACHING_EXIT'], armed: ['SETUP_FORMING', 'APPROACHING_ENTRY'],
    trade: ['ENTRY_TRIGGERED', 'IN_TRADE', 'APPROACHING_EXIT', 'EXIT_SIGNAL'], done: ['CLOSED', 'INVALIDATED'],
    pre: ['BREAKOUT_CONFIRMED', 'SETUP_FORMING', 'APPROACHING_ENTRY'], alerting: ['APPROACHING_ENTRY', 'ENTRY_TRIGGERED', 'APPROACHING_EXIT', 'EXIT_SIGNAL'] };
  // outcome filters that look past the current state (a finished setup's result, or a stock watching again after one)
  const lastR = c => c.result && c.result.r != null ? c.result.r : (c.last && c.last.r != null ? c.last.r : null);
  const OUTCOME = { win: c => lastR(c) != null && lastR(c) > 0, loss: c => lastR(c) != null && lastR(c) <= 0,
    t2: c => lastR(c) != null && lastR(c) >= 2, sqoff: c => !!c.square_off, again: c => c.state === 'WATCHING' && !!c.last,
    nosetup: c => c.state === 'WATCHING' && !c.last };
  function stageOk(c, v) {
    if (v === 'all') return true;
    if (v.startsWith('s:')) return c.state === v.slice(2);
    if (v.startsWith('o:')) return !!(OUTCOME[v.slice(2)] || (() => true))(c);
    return (STAGES[v] || []).includes(c.state);
  }
  const TOAST_TO = ['SETUP_FORMING', 'APPROACHING_ENTRY', 'ENTRY_TRIGGERED', 'APPROACHING_EXIT', 'EXIT_SIGNAL', 'CLOSED'];
  const IC = {
    eye: '<path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12z"/><circle cx="12" cy="12" r="2.5"/>', bolt: '<path d="M13 3L5 14h6l-1 7 8-11h-6z"/>',
    target: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3.5"/>', near: '<path d="M4 12h12M12 7l5 5-5 5M20 5v14"/>',
    play: '<path d="M7 5l12 7-12 7z"/>', pulse: '<path d="M3 12h4l2.5-6 5 12 2.5-6h4"/>', alert: '<path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17.5v.01"/>',
    exit: '<path d="M10 4H5v16h5M14 8l5 4-5 4M19 12H9"/>', check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>', x: '<path d="M7 7l10 10M17 7L7 17"/>',
    up: '<path d="M12 19V5M6 11l6-6 6 6"/>', down: '<path d="M12 5v14M6 13l6 6 6-6"/>', flat: '<path d="M5 12h14"/>'
  };
  const ico = n => '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + IC[n] + '</svg>';
  const toneOf = c => { const t = STATE[c.state][0]; return t === 'side' ? (c.side === 'short' ? 'short' : 'long') : t; };
  const TONE_CSS = { amber: '--is-amber', blue: '--is-blue', purple: '--is-purple', long: '--up', short: '--down', grey: '--muted' };

  let D = null, ver = null, lastSeq = null, root = null, err = null, audio = null, busy = false;
  const flashes = {};
  const F0 = { side: 'all', stage: 'all', sector: 'all', secdir: 'all', bias: 'all', budget: 'all', wl: 'all', price: 'all', near: 'all', taken: 'all', q: '', sort: 'urgency' };
  const prefs = (() => { try { return JSON.parse(localStorage.getItem(PREF_KEY) || '{}'); } catch (e) { return {}; } })();
  const savePrefs = () => { try { localStorage.setItem(PREF_KEY, JSON.stringify(prefs)); } catch (e) { } };
  const F = Object.assign({}, F0, prefs.filters || {});           // filter choices are remembered in this browser
  const saveFilters = () => { const o = {}; Object.keys(F0).forEach(k => { o[k] = F[k]; }); prefs.filters = o; savePrefs(); };

  // ------------------------------------------------------------ data
  async function poll(force) {
    if (!BASE || busy) return;
    busy = true;
    try {
      const r = await fetch(BASE + '/scanner/state' + (ver && !force ? '?since=' + ver : ''), { cache: 'no-store' });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || 'HTTP ' + r.status);
      err = null;
      if (d.unchanged) return;
      if (d.cards) { alerts(d); ver = d.version; }
      D = d;
    } catch (e) { err = 'Cannot reach the scanner: ' + (e.message || e); }
    finally { busy = false; badge(); if (active()) update(); }
  }

  async function act(a) {
    try {
      const r = await fetch(BASE + '/scanner/action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(a) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || 'HTTP ' + r.status);
      ver = null; await poll(true);
      return true;
    } catch (e) { toast({ symbol: 'Not saved', msg: e.message || String(e) }, '--down'); return false; }
  }

  // ------------------------------------------------------------ alerts: toast, sound, browser notification, flash
  function alerts(d) {
    if (lastSeq === null) { lastSeq = d.seq; return; }                 // history from before this page opened: log only
    const fresh = d.alerts.filter(a => a.seq > lastSeq);
    lastSeq = d.seq;
    const secs = (d.config && d.config.ui && d.config.ui.flash_seconds) || 8;
    fresh.forEach(a => {
      if (!a.symbol) return;
      const card = d.cards.find(c => c.symbol === a.symbol);
      const t = card ? TONE_CSS[toneOf(card)] : '--is-amber';
      flashes[a.symbol] = { until: Date.now() + secs * 1000, color: 'var(' + t + ')' };
      if (TOAST_TO.includes(a.to) || a.type === 'square_off') {
        toast(a, t);
        if (pref('sound')) beep(a.to === 'ENTRY_TRIGGERED' || a.to === 'EXIT_SIGNAL' || a.type === 'square_off' ? 2 : 1);
        if (pref('notify') && 'Notification' in window && Notification.permission === 'granted') {
          try { new Notification(a.symbol + ' · ' + (STATE[a.to] ? STATE[a.to][1] : 'Alert'), { body: a.msg, tag: a.symbol + a.seq }); } catch (e) { }
        }
      }
    });
  }
  function pref(k) { return prefs[k] != null ? prefs[k] : !!(D && D.config && D.config.ui && D.config.ui[k === 'notify' ? 'browser_notifications' : k]); }
  function toast(a, colorVar) {
    let box = $('.is-toasts');
    if (!box) { box = document.createElement('div'); box.className = 'is-toasts'; box.setAttribute('role', 'status'); box.setAttribute('aria-live', 'polite'); document.body.appendChild(box); }
    const el = document.createElement('div');
    el.className = 'is-toast'; el.style.setProperty('--fc', 'var(' + colorVar + ')');
    el.innerHTML = '<b>' + esc(a.symbol) + (a.to && STATE[a.to] ? ' · ' + esc(STATE[a.to][1]) : '') + '</b>' + esc(a.msg);
    el.onclick = () => el.remove();
    box.appendChild(el);
    while (box.children.length > 4) box.firstChild.remove();
    setTimeout(() => el.remove(), 9000);
  }
  function beep(n) {
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      for (let i = 0; i < n; i++) {
        const o = audio.createOscillator(), g = audio.createGain(), t = audio.currentTime + i * 0.22;
        o.frequency.value = n > 1 ? 880 : 660; g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.2, t + 0.02); g.gain.exponentialRampToValueAtTime(0.0001, t + 0.18);
        o.connect(g); g.connect(audio.destination); o.start(t); o.stop(t + 0.2);
      }
    } catch (e) { }
  }
  function badge() {
    const b = document.getElementById('scanCount'); if (!b) return;
    const n = D && D.cards ? D.cards.filter(c => ['APPROACHING_ENTRY', 'ENTRY_TRIGGERED', 'APPROACHING_EXIT', 'EXIT_SIGNAL'].includes(c.state)).length : 0;
    b.textContent = n; b.hidden = !n;
  }

  // ------------------------------------------------------------ view
  const active = () => root && document.body.contains(root);

  function render(main) {
    if (!main) return;
    if (!active() || root.parentNode !== main) {
      main.innerHTML = '';
      root = document.createElement('div'); root.className = 'iscan';
      root.id = 'iscanRoot';                 // a unique id: the app's DOM patcher replaces this node instead of reusing it for another tab
      main.appendChild(root);
      root.innerHTML =
        '<div class="is-disc">' + ico('alert').replace('<svg', '<svg width="14" height="14"') + '<span><b>Educational tool, not financial advice.</b> Signals and alerts only: nothing here places an order.</span></div>' +
        '<div id="isStop"></div><div class="is-top" id="isTop"></div>' +
        '<div class="is-top">' +
        '<section class="is-box"><h4>Market bias <span>pre-open advances vs declines</span></h4><div class="is-row" id="isBiasV"></div>' +
        '<div class="is-row" style="margin-top:8px"><label class="is-lbl">Advances<input class="is-in" id="isAdv" type="number" min="0"></label><label class="is-lbl">Declines<input class="is-in" id="isDec" type="number" min="0"></label><button class="btn sm" data-a="bias" style="align-self:flex-end">Set</button></div>' +
        '<div class="is-sub">Enter the NSE pre-open numbers (Yahoo has no pre-open feed). Leave empty for no bias.</div></section>' +
        '<section class="is-box"><h4>Position size <span id="isBudget"></span></h4><div class="is-row">' +
        '<label class="is-lbl">Capital ₹<input class="is-in" id="isCap" type="number" min="1" step="1000"></label>' +
        '<label class="is-lbl">Daily risk %<input class="is-in" id="isRisk" type="number" min="0.1" step="0.1"></label>' +
        '<label class="is-lbl">Trades / day<input class="is-in" id="isTpd" type="number" min="1" step="1"></label>' +
        '<button class="btn sm" data-a="risk" style="align-self:flex-end">Save</button></div>' +
        '<div class="is-sub">Quantity = risk per trade ÷ (entry − stop), rounded down to whole F&amp;O lots. Each card shows its size.</div></section>' +
        '<section class="is-box"><h4>Alerts <span class="is-row"><label><input type="checkbox" id="isSound"> sound</label><label><input type="checkbox" id="isNotify"> browser</label><button class="btn sm" data-a="test">Test</button></span></h4><div class="is-log" id="isLog"></div></section>' +
        '</div>' +
        '<div class="is-filters" id="isFilters"></div><div class="is-board" id="isBoard" aria-live="off"></div>' +
        '<details class="is-box is-watch" id="isWatchD"><summary id="isWatchS">Watching</summary><div id="isWatch"></div></details>' +
        '<div class="is-sub" id="isFoot"></div>';
      root.addEventListener('click', onClick); root.addEventListener('change', onChange);
      root.addEventListener('input', e => { if (e.target.id === 'isFq') { F.q = e.target.value; saveFilters(); resetBoardHeight(); cards($('#isBoard', root)); filtersBar(); } });
      root.addEventListener('toggle', e => { if (e.target.id === 'isWatchD') F.watchOpen = e.target.open; }, true);
    }
    update();
  }

  function setVal(id, v) { const el = document.getElementById(id); if (el && document.activeElement !== el) el.value = v == null ? '' : v; }

  function update() {
    if (!active()) return;
    const sc = root.parentNode, top0 = sc.scrollTop;
    try { paint(); } finally { if (sc.scrollTop !== top0) sc.scrollTop = top0; }   // a refresh never moves your place
  }

  function paint() {
    const board = $('#isBoard', root), top = $('#isTop', root);
    if (!BASE) { top.innerHTML = '<section class="is-box"><b>The Strategy-PD-H/L tab needs the data server.</b><div class="is-sub">Start <span class="num">python server.py</span> and open http://localhost:8787/.</div></section>'; board.innerHTML = ''; return; }
    if (!D) { top.innerHTML = '<section class="is-box"><div class="spin"></div>' + (err ? '<div class="is-err">' + esc(err) + '</div>' : 'Loading the scanner…') + '</section>'; return; }
    if (D.off || D.loading) {
      top.innerHTML = '<section class="is-box">' + (D.off ? '<b>Scanner is off</b>' : '<div class="spin"></div><b>Starting the scanner</b><div class="is-sub">Loading F&amp;O lot sizes and the first Yahoo download (about a minute).</div>') +
        (D.error ? '<div class="is-err">' + esc(D.error) + '</div>' : '') + '</section>';
      return;
    }
    const r = D.risk, now = D.now;
    $('#isStop', root).innerHTML = r.stop_trading ? '<div class="is-stop" role="alert">' + ico('alert').replace('<svg', '<svg width="20" height="20"') + '<div>STOP TRADING<br><small>Daily loss limit reached: ' + rs(r.loss_used) + ' of ' + rs(r.loss_limit) + '.</small></div></div>' : '';
    const mk = D.market, info = D.feed;
    top.innerHTML =
      '<section class="is-box"><h4>Market <span>' + esc(info.name) + '</span></h4><div class="is-row"><span class="is-big">' + ist(now) + '</span><span class="is-status ' + mk + '">' + (mk === 'open' ? '● Open' : mk === 'pre-open' ? '◐ Pre-open' : '○ Closed') + '</span>' +
      (info.kind === 'replay' ? '<span class="is-tag">REPLAY</span>' : '') + '</div>' +
      '<div class="is-sub">Session ' + esc(D.session || '—') + ' · data ' + ist(info.data_time) + '. ' + esc(info.note || '') + '</div>' +
      ((D.error || err) ? '<div class="is-err">' + esc(err || D.error) + ' (showing the last good data)</div>' : '') + '</section>' +
      '<section class="is-box"><h4>Daily risk <span>' + r.trades_taken + ' / ' + r.trades_per_day + ' trades</span></h4>' +
      '<div class="is-row"><span class="is-big">' + rs(r.loss_used) + '</span><span class="is-sub">of ' + rs(r.loss_limit) + ' loss limit (' + r.daily_risk_pct + '% of ' + rs(r.capital) + ')</span></div>' +
      '<div class="is-meter' + (r.stop_trading ? ' full' : '') + '" role="meter" aria-valuenow="' + r.used_pct + '" aria-valuemin="0" aria-valuemax="100" aria-label="Daily loss used"><i style="width:' + Math.min(100, r.used_pct) + '%"></i></div>' +
      '<div class="is-sub">Realised P&amp;L <b class="' + (r.realised_pnl >= 0 ? 'up' : 'down') + '">' + rs(r.realised_pnl) + '</b> · open risk ' + rs(r.open_risk) + '. Mark trades on the cards to track this.</div></section>';
    const b = D.bias;
    $('#isBiasV', root).innerHTML = '<span class="is-bias ' + (b.value || 'unset') + '">' + (b.value === 'long' ? ico('up').replace('<svg', '<svg width="14" height="14"') + 'Long bias' : b.value === 'short' ? ico('down').replace('<svg', '<svg width="14" height="14"') + 'Short bias' : b.value === 'neutral' ? 'Neutral' : 'No bias set') + '</span>' +
      (b.advances != null ? '<span class="is-sub">' + b.advances + ' advances · ' + b.declines + ' declines</span>' : '');
    setVal('isAdv', b.advances); setVal('isDec', b.declines);
    setVal('isCap', r.capital); setVal('isRisk', r.daily_risk_pct); setVal('isTpd', r.trades_per_day);
    $('#isBudget', root).textContent = 'risk per trade ' + rs(r.capital * r.daily_risk_pct / 100 / r.trades_per_day);
    $('#isSound', root).checked = pref('sound'); $('#isNotify', root).checked = pref('notify');
    $('#isLog', root).innerHTML = D.alerts.slice().reverse().map(a => '<div><time>' + ist(a.at) + '</time><b>' + esc(a.symbol || '—') + '</b><span>' + esc(a.msg) + '</span></div>').join('') || '<div class="is-sub">No alerts yet.</div>';
    filtersBar();
    cards(board);
    watching();
    $('#isFoot', root).textContent = [D.oi_note, D.lots_note].filter(Boolean).join(' · ');
  }

  function stageSel() {
    const o = (v, t) => '<option value="' + v + '"' + (F.stage === v ? ' selected' : '') + '>' + esc(t) + '</option>';
    const g = (label, opts) => '<optgroup label="' + label + '">' + opts.map(x => o(x[0], x[1])).join('') + '</optgroup>';
    return '<select id="isFt" data-f="stage" aria-label="State">' + o('all', 'All states') +
      g('Groups', [['active', 'All active'], ['pre', 'Before entry'], ['armed', 'Armed (entry levels set)'], ['approaching', 'Approaching entry / exit'],
        ['trade', 'In trade (any)'], ['alerting', 'Needs attention now'], ['done', 'Closed / invalidated']]) +
      g('Single state', ['BREAKOUT_CONFIRMED', 'SETUP_FORMING', 'APPROACHING_ENTRY', 'ENTRY_TRIGGERED', 'IN_TRADE', 'APPROACHING_EXIT', 'EXIT_SIGNAL', 'CLOSED', 'INVALIDATED', 'WATCHING']
        .map(k => ['s:' + k, STATE[k][1]])) +
      g('Outcome', [['o:win', 'Winners (closed above 0R)'], ['o:t2', 'Reached 2R or more'], ['o:loss', 'Losers / scratch (0R or less)'],
        ['o:sqoff', 'Square-off warning'], ['o:again', 'Watching again after a setup'], ['o:nosetup', 'Watching, no setup yet']]) + '</select>';
  }

  function filtersBar() {
    const secs = [...new Set(D.cards.map(c => c.sector).filter(Boolean))].sort();
    const sel = (id, k, opts) => '<select id="' + id + '" data-f="' + k + '" aria-label="' + k + '">' + opts.map(o => '<option value="' + o[0] + '"' + (F[k] === o[0] ? ' selected' : '') + '>' + esc(o[1]) + '</option>').join('') + '</select>';
    const active = Object.keys(F0).some(k => k !== 'sort' && F[k] !== F0[k]), shown = visible().length;
    const html = '<span class="pl">Show</span>' +
      sel('isFs', 'side', [['all', 'Long and short'], ['long', 'Long only'], ['short', 'Short only']]) +
      stageSel() +
      sel('isFc', 'sector', [['all', 'All sectors']].concat(secs.map(s => [s, s]))) +
      sel('isFd', 'secdir', [['all', 'Any sector move'], ['agree', 'Sector agrees'], ['against', 'Sector against']]) +
      sel('isFb', 'bias', [['all', 'Any bias'], ['match', 'Matches bias'], ['against', 'Against bias']]) +
      sel('isFn', 'near', [['all', 'Any distance'], ['0.25', 'Entry within 0.25%'], ['0.5', 'Entry within 0.5%'], ['1', 'Entry within 1%']]) +
      sel('isFu', 'budget', [['all', 'Any size'], ['fits', 'Fits risk budget (1+ lot)'], ['over', 'Over budget']]) +
      sel('isFp', 'price', [['all', 'Any price'], ['0-500', 'Under ₹500'], ['500-2000', '₹500 – 2,000'], ['2000-5000', '₹2,000 – 5,000'], ['5000-', 'Over ₹5,000']]) +
      sel('isFw', 'wl', [['all', 'Watchlist: any'], ['manual', 'My watchlist'], ['candidate', 'Range-bound candidates']]) +
      sel('isFk', 'taken', [['all', 'Taken: any'], ['taken', 'Marked taken'], ['open', 'Open position']]) +
      '<input class="is-in is-q" id="isFq" data-f="q" type="search" placeholder="Symbol…" aria-label="Search symbol" value="' + esc(F.q) + '">' +
      sel('isFo', 'sort', [['urgency', 'Sort: urgency'], ['near', 'Sort: closest to entry'], ['chg', 'Sort: % change'], ['risk', 'Sort: tightest stop'], ['symbol', 'Sort: symbol']]) +
      (active ? '<button class="btn sm" data-a="freset">Reset filters</button>' : '') +
      '<span class="is-sub" style="margin:0">' + shown + ' of ' + D.cards.length + ' cards</span>';
    const box = $('#isFilters', root);
    if (box._h !== html && !box.contains(document.activeElement)) { box.innerHTML = html; box._h = html; }
  }

  function visible() {
    const q = (F.q || '').trim().toUpperCase(), [pLo, pHi] = F.price === 'all' ? [0, 0] : F.price.split('-').map(Number);
    const entryPct = c => c.prox && c.prox.entry ? Math.abs(c.prox.entry.pct) : null;
    const lv = c => c.trade || c.setup || {};
    const riskPct = c => { const l = lv(c); return l.entry && l.stop ? Math.abs(l.entry - l.stop) / l.entry * 100 : null; };
    const fl = c => c.flags || {};
    let list = D.cards.filter(c => (F.side === 'all' || c.side === F.side || (!c.side && F.side === 'all')) &&
      stageOk(c, F.stage) && (F.sector === 'all' || c.sector === F.sector) &&
      (F.secdir === 'all' || fl(c).sector_same_dir === (F.secdir === 'agree')) &&
      (F.bias === 'all' || fl(c).bias_match === (F.bias === 'match')) &&
      (F.near === 'all' || (entryPct(c) != null && entryPct(c) <= Number(F.near))) &&
      (F.budget === 'all' || (c.size && (F.budget === 'fits' ? c.size.lots > 0 : c.size.lots === 0))) &&
      (F.price === 'all' || (c.ltp >= pLo && (!pHi || c.ltp < pHi))) &&
      (F.wl === 'all' || (F.wl === 'manual' ? c.manual_watch : !!(c.watchlist && c.watchlist.candidate))) &&
      (F.taken === 'all' || ((c.taken || []).length && (F.taken === 'taken' || c.taken.some(j => j.exit == null)))) &&
      (!q || c.symbol.includes(q)));
    const nil = (v, big) => v == null ? big : v;
    const by = { urgency: (a, b) => URGENCY[a.state] - URGENCY[b.state] || a.symbol.localeCompare(b.symbol),
      near: (a, b) => nil(entryPct(a), 1e9) - nil(entryPct(b), 1e9) || URGENCY[a.state] - URGENCY[b.state],
      risk: (a, b) => nil(riskPct(a), 1e9) - nil(riskPct(b), 1e9) || a.symbol.localeCompare(b.symbol),
      chg: (a, b) => Math.abs(b.chg || 0) - Math.abs(a.chg || 0), symbol: (a, b) => a.symbol.localeCompare(b.symbol) };
    return list.sort(by[F.sort] || by.urgency);
  }

  function cards(board) {
    const list = visible(), keep = new Set();
    if (!list.length) {
      board.innerHTML = '<div class="empty" style="grid-column:1/-1"><b>' + (D.cards.length ? 'No card matches these filters.' : 'No setups yet.') + '</b><p>A stock gets a card once a 5-minute candle closes beyond its previous day\'s high or low after 09:30. Manual watchlist symbols also get one.</p></div>';
      board._empty = true; return;
    }
    if (board._empty) { board.innerHTML = ''; board._empty = false; }
    const now = Date.now();
    let order = 0;
    list.forEach(c => {
      keep.add(c.symbol);
      let el = board.querySelector('[data-sym="' + CSS.escape(c.symbol) + '"]');
      if (!el) { el = document.createElement('article'); el.dataset.sym = c.symbol; }
      const html = cardHtml(c);
      if (el._h !== html && !el.contains(document.activeElement)) { el.innerHTML = html; el._h = html; el._c = null; }
      const tone = toneOf(c);
      el.className = 'is-card s-' + tone;
      const f = flashes[c.symbol];
      el.classList.toggle('flash', !!(f && f.until > now));
      if (f) el.style.setProperty('--fc', f.color);
      // move a card only when it is out of place: re-inserting every card each second disturbs scroll anchoring
      const at = board.children[order];
      if (at !== el) board.insertBefore(el, at || null);
      order++;
      const key = JSON.stringify([c.candles, c.pdh, c.pdl, c.setup, c.trade, c.exit, cssv('--bg')]);
      const cv = el.querySelector('canvas');
      if (cv && (el._c !== key || cv._w !== cv.clientWidth)) { mini(cv, c); el._c = key; cv._w = cv.clientWidth; }
    });
    [...board.children].forEach(el => { if (!keep.has(el.dataset.sym)) el.remove(); });
    const h = board.offsetHeight;                                     // never let the board get shorter while you read it
    if (h > (board._minH || 0)) { board._minH = h; board.style.minHeight = h + 'px'; }
  }
  function resetBoardHeight() { const b = root && $('#isBoard', root); if (b) { b._minH = 0; b.style.minHeight = ''; } }

  function cardHtml(c) {
    const st = STATE[c.state], tone = toneOf(c), lv = c.trade || c.setup || {};
    const dir = c.chg > 0 ? 'up' : c.chg < 0 ? 'down' : 'flat';
    const sideTxt = c.side ? (c.side === 'long' ? 'LONG' : 'SHORT') : '';
    const pr = (label, d, color, scalePct) => {
      if (!d) return '<div class="is-pr off"><em>' + label + '</em><span class="bar"></span><span>—</span></div>';   // keeps the card height steady
      const fill = Math.max(0.03, Math.min(1, 1 - Math.abs(d.pct) / (scalePct * 4)));
      return '<div class="is-pr"><em>' + label + '</em><span class="bar" role="meter" aria-label="Distance to ' + label + '" aria-valuenow="' + Math.abs(d.pct) + '"><i style="width:' + (fill * 100).toFixed(0) + '%;background:var(' + color + ')"></i></span>' +
        '<span>' + (d.pct > 0 ? '+' : '') + d.pct.toFixed(2) + '% · ₹' + Math.abs(d.rs).toFixed(2) + ' · ' + d.ticks + 't</span></div>';
    };
    const cfg = D.config;
    const prox = pr('Entry', c.prox.entry, '--is-amber', cfg.approach_entry_pct) + pr('Stop', c.prox.stop, '--down', cfg.approach_exit_pct) + pr('10 EMA', c.prox.ema, '--is-blue', cfg.approach_exit_pct);
    const lvl = (k, v) => v == null ? '' : '<span><b>' + k + '</b>' + px(v) + '</span>';
    const tg = (lv.targets || []);
    const levels = lvl('PDH', c.pdh) + lvl('PDL', c.pdl) + lvl('Entry', lv.entry) + lvl('Stop', lv.stop) + tg.map(t => lvl('1:' + t.r, t.price)).join('') +
      lvl('10 EMA', c.ema) + (c.exit ? lvl('Exit below', c.side === 'long' ? c.exit.level : null) + lvl('Exit above', c.side === 'short' ? c.exit.level : null) : '');
    const size = c.size ? '<span><b>Size</b>' + (c.size.qty ? c.size.qty + (c.lot ? ' (' + c.size.lots + ' × ' + c.lot + ')' : ' sh') + ' · risk ' + rs(c.size.risk) : esc(c.size.note)) + '</span>' : '';
    const flag = (ok, yes, no, unknown) => '<span class="is-flag ' + (ok === true ? 'yes' : ok === false ? 'no' : '') + '">' + (ok === true ? '✓ ' + yes : ok === false ? '✗ ' + no : '– ' + unknown) + '</span>';
    const fl = c.flags;
    const flags = c.side ? flag(fl.sector_same_dir, 'Sector agrees' + (fl.sector_move_pct != null ? ' ' + pct(fl.sector_move_pct) : ''), 'Sector against' + (fl.sector_move_pct != null ? ' ' + pct(fl.sector_move_pct) : ''), 'Sector n/a') +
      flag(fl.bias_match, 'Matches bias', 'Against bias', 'No bias set') + flag(null, '', '', 'OI n/a') : (c.watch_reason ? '<span class="is-flag">Watchlist: ' + esc(c.watch_reason) + '</span>' : '');
    const open = (c.taken || []).filter(j => j.exit == null), shut = (c.taken || []).filter(j => j.exit != null);
    let acts = '';
    if (open.length) acts = '<span>Taken ' + open[0].qty + ' @ ' + px(open[0].entry) + '</span><input class="is-in" data-exit="' + esc(c.symbol) + '" type="number" step="0.05" value="' + (c.result ? c.result.exit : c.ltp) + '" aria-label="Exit price"><button class="btn sm" data-a="exit" data-s="' + esc(c.symbol) + '">Mark exited</button>';
    else if (lv.entry && !['CLOSED', 'INVALIDATED'].includes(c.state)) acts = '<input class="is-in" data-qty="' + esc(c.symbol) + '" type="number" min="1" step="' + (c.lot || 1) + '" value="' + ((c.size && c.size.qty) || c.lot || 1) + '" aria-label="Quantity"><button class="btn sm" data-a="take" data-s="' + esc(c.symbol) + '">Mark taken</button><span>for the risk tracker</span>';
    acts += shut.map(j => '<span>P&amp;L <b class="' + (j.pnl >= 0 ? 'up' : 'down') + '">' + rs(j.pnl) + '</b></span>').join('');
    const res = c.result ? ' · ' + (c.result.r != null ? (c.result.r >= 0 ? '+' : '') + c.result.r + 'R' : '') : '';
    return '<div class="is-ch"><div><div class="is-sym">' + esc(c.symbol) + '</div><div class="is-meta">' + esc(c.sector || '—') + (c.lot ? '<span class="is-tag">Lot ' + c.lot + '</span>' : '') + '<span class="is-tag" title="Open interest is not available from Yahoo">OI n/a</span></div></div>' +
      '<div class="is-px"><div class="is-ltp">' + px(c.ltp) + '</div><div class="is-chg ' + (dir === 'flat' ? 'muted' : dir) + '">' + ico(dir).replace('<svg', '<svg width="12" height="12" style="vertical-align:-1px"') + ' ' + pct(c.chg) + '</div></div></div>' +
      '<span class="is-badge b-' + tone + '">' + ico(st[2]) + (sideTxt ? sideTxt + ' · ' : '') + st[1] + res + '</span>' +
      '<div class="is-note">' + (c.last ? 'Watching for a new setup. Last: ' + esc(STATE[c.last.state][1]) + (c.last.r != null ? ' ' + (c.last.r >= 0 ? '+' : '') + c.last.r + 'R' : '') +
        ' at ' + ist(c.last.at).slice(0, 5) + ' — ' + esc(c.last.note) : esc(c.note)) + (c.square_off ? ' <b class="down">· Square off by ' + esc(D.config.square_off_warning) + '</b>' : '') + '</div>' +
      '<canvas aria-label="5-minute chart of ' + esc(c.symbol) + ' with PDH, PDL, entry, stop and 10 EMA"></canvas>' +
      '<div class="is-prox">' + prox + '</div>' + '<div class="is-lv">' + levels + size + '</div>' + '<div class="is-flags">' + (flags || '<span class="is-flag">– No setup yet</span>') + '</div>' +
      '<div class="is-act">' + (acts || '<span>' + (['CLOSED', 'INVALIDATED'].includes(c.state) ? 'Setup finished' : 'No entry levels yet') + '</span>') + '</div>';
  }

  function mini(cv, c) {
    const dpr = window.devicePixelRatio || 1, W = cv.clientWidth, H = cv.clientHeight; if (!W) return;
    cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
    const g = cv.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, W, H);
    const rows = c.candles; if (!rows.length) return;
    const up = cssv('--up'), dn = cssv('--down'), mu = cssv('--muted'), amber = cssv('--is-amber'), blue = cssv('--is-blue'), purple = cssv('--is-purple');
    let lo = Infinity, hi = -Infinity; rows.forEach(r => { lo = Math.min(lo, r[3]); hi = Math.max(hi, r[2]); });
    const lv = c.trade || c.setup || {};
    const lines = [[c.pdh, up, [4, 3], 'PDH'], [c.pdl, dn, [4, 3], 'PDL'], [lv.entry, amber, [], 'Entry'], [lv.stop, dn, [], 'Stop'], [c.exit && c.exit.level, purple, [2, 2], 'Exit']];
    const span = hi - lo || hi * 0.01;
    lines.forEach(l => { if (l[0] != null && l[0] > lo - span && l[0] < hi + span) { lo = Math.min(lo, l[0]); hi = Math.max(hi, l[0]); } });
    rows.forEach(r => { if (r[6] != null) { lo = Math.min(lo, r[6]); hi = Math.max(hi, r[6]); } });
    const pad = (hi - lo) * 0.08 || 1; lo -= pad; hi += pad;
    const L = 4, R = 40, slots = Math.max(30, 75), step = (W - L - R) / slots, bw = Math.max(1.2, Math.min(step * 0.7, 8));
    const Y = v => 4 + (hi - v) / (hi - lo) * (H - 8);
    g.font = '10px "JetBrains Mono",monospace'; g.textBaseline = 'middle';
    lines.forEach(l => {
      if (l[0] == null || l[0] < lo || l[0] > hi) return;
      const y = Math.round(Y(l[0])) + .5; g.strokeStyle = l[1]; g.setLineDash(l[2]); g.lineWidth = 1;
      g.beginPath(); g.moveTo(L, y); g.lineTo(W - R, y); g.stroke(); g.setLineDash([]); g.fillStyle = l[1]; g.fillText(l[3], W - R + 3, y);
    });
    rows.forEach((r, i) => {
      const x = L + step * (i + .5), col = r[4] >= r[1] ? up : dn; g.strokeStyle = col; g.fillStyle = col; g.globalAlpha = r[7] ? 1 : 0.55;
      g.beginPath(); g.moveTo(Math.round(x) + .5, Y(r[2])); g.lineTo(Math.round(x) + .5, Y(r[3])); g.stroke();
      const yt = Y(Math.max(r[1], r[4])), yb = Y(Math.min(r[1], r[4])); g.fillRect(x - bw / 2, yt, bw, Math.max(1, yb - yt));
    });
    g.globalAlpha = 1; g.strokeStyle = blue; g.lineWidth = 1.5; g.beginPath(); let on = false;
    rows.forEach((r, i) => { if (r[6] == null) { on = false; return; } const x = L + step * (i + .5); on ? g.lineTo(x, Y(r[6])) : g.moveTo(x, Y(r[6])); on = true; });
    g.stroke();
    if (c.ltp != null) { g.fillStyle = mu; g.fillText(px(c.ltp), W - R + 3, Math.max(8, Math.min(H - 8, Y(c.ltp)))); }
  }

  function watching() {
    const w = D.watching || [], box = $('#isWatch', root);
    $('#isWatchS', root).textContent = 'Watching ' + w.length + ' stocks (no breakout yet) · closest to PDH/PDL first';
    if (!$('#isWatchD', root).open) return;
    const cell = d => d ? '<span class="' + (Math.abs(d.pct) < 0.5 ? 'up' : '') + '">' + (d.pct > 0 ? '+' : '') + d.pct.toFixed(2) + '%</span>' : '—';
    box.innerHTML = '<table><thead><tr><th>Symbol</th><th>LTP</th><th>Change</th><th>To PDH</th><th>To PDL</th><th>Watchlist</th></tr></thead><tbody>' +
      w.slice(0, 60).map(c => '<tr><td>' + esc(c.symbol) + '</td><td>' + px(c.ltp) + '</td><td class="' + (c.chg > 0 ? 'up' : c.chg < 0 ? 'down' : '') + '">' + pct(c.chg) + '</td><td>' + cell(c.to_pdh) + '</td><td>' + cell(c.to_pdl) + '</td><td>' + (c.candidate ? 'range-bound, not extended' : '') + '</td></tr>').join('') +
      '</tbody></table><div class="is-sub">Day-before watchlist rule: yesterday\'s range small versus the last 10 days and price near its 20-day average. OI change is not available from Yahoo.</div>';
  }

  // ------------------------------------------------------------ events
  function onClick(e) {
    const b = e.target.closest('[data-a]'); if (!b) return;
    const a = b.dataset.a, num = id => { const v = $('#' + id, root).value; return v === '' ? null : Number(v); };
    if (a === 'bias') act({ type: 'bias', advances: num('isAdv'), declines: num('isDec') });
    else if (a === 'risk') act({ type: 'risk', capital: num('isCap'), daily_risk_pct: num('isRisk'), trades_per_day: num('isTpd') });
    else if (a === 'take') { const q = root.querySelector('[data-qty="' + CSS.escape(b.dataset.s) + '"]'); act({ type: 'take', symbol: b.dataset.s, qty: Number(q.value) }); }
    else if (a === 'exit') { const p = root.querySelector('[data-exit="' + CSS.escape(b.dataset.s) + '"]'); act({ type: 'exit', symbol: b.dataset.s, price: Number(p.value) }); }
    else if (a === 'freset') { Object.assign(F, F0, { sort: F.sort }); saveFilters(); resetBoardHeight(); update(); }
    else if (a === 'test') { beep(2); toast({ symbol: 'Test alert', msg: 'This is how an alert looks and sounds.' }, '--is-amber'); }
  }
  function onChange(e) {
    const t = e.target;
    if (t.dataset.f && t.dataset.f !== 'q') { F[t.dataset.f] = t.value; saveFilters(); resetBoardHeight(); update(); return; }
    if (t.id === 'isSound') { prefs.sound = t.checked; savePrefs(); if (t.checked) beep(1); }
    if (t.id === 'isNotify') {
      prefs.notify = t.checked; savePrefs();
      if (t.checked && 'Notification' in window && Notification.permission === 'default') Notification.requestPermission();
    }
    if (t.id === 'isWatchD') watching();
  }
  document.addEventListener('toggle', e => { if (e.target.id === 'isWatchD') watching(); }, true);
  setInterval(() => { if (active() && D && D.cards) cards($('#isBoard', root)); }, 1000);   // end flashes on time
  window.addEventListener('resize', () => { if (active() && D && D.cards) { resetBoardHeight(); cards($('#isBoard', root)); } });

  window.IntradayScanner = { render };
  if (BASE) { poll(); setInterval(poll, POLL_MS); }
})();
