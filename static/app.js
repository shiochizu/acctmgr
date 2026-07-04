// ============================================================
// IKEA Manager — Frontend SPA
// ============================================================

// State
let ws = null;
let currentScheduleLabel = null;
let accounts = {};  // label -> account object
let _loadAccountsTimer = null;
let _loadAccountsInFlight = false;
const browserLoginRunning = new Set();  // labels with in-progress browser login

// ============================================================
// Layout — sections + card ordering (persisted in localStorage)
// ============================================================

const LAYOUT_KEY = 'ikea_manager_layout_v1';
let _drag = null;  // { type: 'card'|'section', label?, id? }

function _loadLayout() {
  try {
    const raw = localStorage.getItem(LAYOUT_KEY);
    if (!raw) return { sections: [], ungrouped: [] };
    const p = JSON.parse(raw);
    if (!p.sections) p.sections = [];
    if (!p.ungrouped) p.ungrouped = [];
    return p;
  } catch { return { sections: [], ungrouped: [] }; }
}

function _saveLayout(layout) {
  try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout)); } catch {}
}

function _normalizeLayout(allLabels) {
  const layout = _loadLayout();
  const known = new Set(allLabels);
  layout.sections = layout.sections.map(s => ({ ...s, labels: (s.labels || []).filter(l => known.has(l)) }));
  layout.ungrouped = layout.ungrouped.filter(l => known.has(l));
  const placed = new Set([...layout.sections.flatMap(s => s.labels), ...layout.ungrouped]);
  for (const label of allLabels) {
    if (!placed.has(label)) layout.ungrouped.push(label);
  }
  _saveLayout(layout);
  return layout;
}

function _moveCardInLayout(label, toSectionId, beforeLabel) {
  const layout = _loadLayout();
  for (const s of layout.sections) s.labels = s.labels.filter(l => l !== label);
  layout.ungrouped = layout.ungrouped.filter(l => l !== label);
  let targetList = toSectionId ? (layout.sections.find(s => s.id === toSectionId) || {}).labels : null;
  if (!targetList) targetList = layout.ungrouped;
  if (beforeLabel) {
    const idx = targetList.indexOf(beforeLabel);
    idx >= 0 ? targetList.splice(idx, 0, label) : targetList.push(label);
  } else {
    targetList.push(label);
  }
  _saveLayout(layout);
}

function _moveSectionInLayout(sectionId, beforeSectionId) {
  const layout = _loadLayout();
  const idx = layout.sections.findIndex(s => s.id === sectionId);
  if (idx < 0) return;
  const [sec] = layout.sections.splice(idx, 1);
  const ti = layout.sections.findIndex(s => s.id === beforeSectionId);
  ti >= 0 ? layout.sections.splice(ti, 0, sec) : layout.sections.push(sec);
  _saveLayout(layout);
}

function addSection() {
  const name = prompt('Section name:');
  if (!name || !name.trim()) return;
  const layout = _loadLayout();
  layout.sections.push({ id: 'sec_' + Date.now(), name: name.trim(), labels: [] });
  _saveLayout(layout);
  renderAllCards();
}

function deleteSection(sectionId) {
  if (!confirm('Delete this section? Cards will move to the main area.')) return;
  const layout = _loadLayout();
  const sec = layout.sections.find(s => s.id === sectionId);
  if (sec) {
    layout.ungrouped = [...(sec.labels || []), ...layout.ungrouped];
    layout.sections = layout.sections.filter(s => s.id !== sectionId);
  }
  _saveLayout(layout);
  renderAllCards();
}

function renameSection(sectionId) {
  const layout = _loadLayout();
  const sec = layout.sections.find(s => s.id === sectionId);
  if (!sec) return;
  const name = prompt('Section name:', sec.name);
  if (name == null || !name.trim()) return;
  sec.name = name.trim();
  _saveLayout(layout);
  const el = document.querySelector(`.card-section[data-section-id="${sectionId}"] .section-name`);
  if (el) el.textContent = sec.name;
}

// ============================================================
// Init
// ============================================================

document.addEventListener('DOMContentLoaded', () => {
  buildScheduleSelects();
  loadAccounts();
  connectWebSocket();
  setInterval(scheduleLoadAccounts, 30000);  // fallback refresh every 30s
});

// ============================================================
// DOM helpers
// ============================================================

/** Find an account card by label — uses data-label so no CSS escaping needed. */
function findCard(label) {
  const grid = document.getElementById('accounts-grid');
  if (!grid) return null;
  return [...grid.querySelectorAll('.account-card')].find(c => c.dataset.label === label) ?? null;
}

// ============================================================
// Accounts
// ============================================================

function scheduleLoadAccounts() {
  clearTimeout(_loadAccountsTimer);
  _loadAccountsTimer = setTimeout(loadAccounts, 150);
}

async function loadAccounts() {
  if (_loadAccountsInFlight) {
    // Another fetch is already in progress — schedule a follow-up instead of stacking
    scheduleLoadAccounts();
    return;
  }
  _loadAccountsInFlight = true;
  try {
    const resp = await fetch('/api/accounts');
    if (!resp.ok) throw new Error('Failed to load accounts');
    const list = await resp.json();

    // Merge into state, preserve log lines for running accounts
    const updated = {};
    for (const acc of list) {
      const prev = accounts[acc.label] || {};
      updated[acc.label] = { ...acc, _logs: prev._logs || [] };
    }
    accounts = updated;
    renderAllCards();
  } catch (err) {
    console.error('loadAccounts error:', err);
  } finally {
    _loadAccountsInFlight = false;
  }
}

function renderAllCards() {
  const grid = document.getElementById('accounts-grid');
  const empty = document.getElementById('empty-state');
  const labels = Object.keys(accounts);

  if (labels.length === 0) {
    grid.innerHTML = '';
    empty.classList.remove('hidden');
    return;
  }
  empty.classList.add('hidden');

  const layout = _normalizeLayout(labels);

  // Collect or create card elements
  const cardMap = {};
  grid.querySelectorAll('.account-card').forEach(card => { cardMap[card.dataset.label] = card; });

  for (const label of labels) {
    if (!cardMap[label]) {
      const div = document.createElement('div');
      div.innerHTML = renderCard(accounts[label]);
      const card = div.firstElementChild;
      cardMap[label] = card;
      if (accounts[label]._logs && accounts[label]._logs.length > 0) {
        const logsEl = card.querySelector('.log-panel');
        if (logsEl) logsEl.classList.remove('hidden');
        const linesEl = card.querySelector('.log-lines');
        if (linesEl) linesEl.innerHTML = accounts[label]._logs.map(l => renderLogLine(l)).join('');
      }
    } else {
      updateCardDOM(label);
    }
  }

  // Detach all cards; remove stale
  Object.entries(cardMap).forEach(([lbl, card]) => {
    if (card.parentNode) card.remove();
    if (!accounts[lbl]) delete cardMap[lbl];
  });

  // Remove old structure
  grid.querySelectorAll('.card-section, .ungrouped-body').forEach(el => el.remove());

  // Helper: create a droppable body (section or ungrouped)
  function makeBody(sectionId) {
    const body = document.createElement('div');
    body.className = sectionId ? 'section-body' : 'ungrouped-body';
    body.dataset.sectionId = sectionId;
    body.addEventListener('dragover', e => {
      if (!_drag || _drag.type !== 'card') return;
      if (e.target.closest && e.target.closest('.account-card')) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      document.querySelectorAll('.drag-over-zone').forEach(el => el.classList.remove('drag-over-zone'));
      body.classList.add('drag-over-zone');
    });
    body.addEventListener('dragleave', e => {
      if (!e.relatedTarget || !body.contains(e.relatedTarget)) body.classList.remove('drag-over-zone');
    });
    body.addEventListener('drop', e => {
      if (!_drag || _drag.type !== 'card') return;
      if (e.target.closest && e.target.closest('.account-card')) return;
      e.preventDefault();
      body.classList.remove('drag-over-zone');
      _moveCardInLayout(_drag.label, sectionId, null);
      _drag = null;
      renderAllCards();
    });
    return body;
  }

  // Helper: attach drag events to a card element
  function attachCard(card, label) {
    card.setAttribute('draggable', 'true');
    card.ondragstart = e => {
      if (e.target.tagName === 'BUTTON' || (e.target.closest && e.target.closest('button'))) { e.preventDefault(); return; }
      _drag = { type: 'card', label };
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', label);
      requestAnimationFrame(() => card.classList.add('is-dragging'));
    };
    card.ondragend = () => {
      card.classList.remove('is-dragging');
      document.querySelectorAll('.drag-over-card, .drag-over-zone').forEach(el => el.classList.remove('drag-over-card', 'drag-over-zone'));
      _drag = null;
    };
    card.ondragover = e => {
      if (!_drag || _drag.type !== 'card' || _drag.label === label) return;
      e.preventDefault();
      e.stopPropagation();
      e.dataTransfer.dropEffect = 'move';
      document.querySelectorAll('.drag-over-card').forEach(el => el.classList.remove('drag-over-card'));
      card.classList.add('drag-over-card');
    };
    card.ondrop = e => {
      if (!_drag || _drag.type !== 'card' || _drag.label === label) return;
      e.preventDefault();
      e.stopPropagation();
      card.classList.remove('drag-over-card');
      const body = card.closest('[data-section-id]');
      _moveCardInLayout(_drag.label, body ? body.dataset.sectionId : '', label);
      _drag = null;
      renderAllCards();
    };
  }

  // Render named sections
  for (const section of layout.sections) {
    const sectionEl = document.createElement('div');
    sectionEl.className = 'card-section';
    sectionEl.dataset.sectionId = section.id;

    const header = document.createElement('div');
    header.className = 'section-header';
    header.setAttribute('draggable', 'true');
    const safeId = section.id.replace(/'/g, "\\'");
    header.innerHTML = `
      <span class="drag-handle">&#8942;</span>
      <span class="section-name">${escapeHtml(section.name)}</span>
      <span class="section-count">${section.labels.filter(l => accounts[l]).length}</span>
      <button class="btn-section-action btn-section-rename" onclick="renameSection('${safeId}')" title="Rename">&#9998;</button>
      <button class="btn-section-action btn-section-delete" onclick="deleteSection('${safeId}')" title="Delete">&#10005;</button>`;

    header.ondragstart = e => {
      if (e.target.tagName === 'BUTTON' || (e.target.closest && e.target.closest('button'))) { e.preventDefault(); return; }
      _drag = { type: 'section', id: section.id };
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', section.id);
      requestAnimationFrame(() => sectionEl.classList.add('is-dragging-section'));
    };
    header.ondragend = () => {
      sectionEl.classList.remove('is-dragging-section');
      document.querySelectorAll('.drag-over-section').forEach(el => el.classList.remove('drag-over-section'));
      _drag = null;
    };

    sectionEl.addEventListener('dragover', e => {
      if (!_drag || _drag.type !== 'section' || _drag.id === section.id) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      document.querySelectorAll('.drag-over-section').forEach(el => el.classList.remove('drag-over-section'));
      sectionEl.classList.add('drag-over-section');
    });
    sectionEl.addEventListener('drop', e => {
      if (!_drag || _drag.type !== 'section' || _drag.id === section.id) return;
      e.preventDefault();
      sectionEl.classList.remove('drag-over-section');
      _moveSectionInLayout(_drag.id, section.id);
      _drag = null;
      renderAllCards();
    });

    const body = makeBody(section.id);
    for (const label of section.labels) {
      if (cardMap[label]) { attachCard(cardMap[label], label); body.appendChild(cardMap[label]); }
    }
    sectionEl.appendChild(header);
    sectionEl.appendChild(body);
    grid.appendChild(sectionEl);
  }

  // Ungrouped area (always present)
  const ungroupedBody = makeBody('');
  for (const label of layout.ungrouped) {
    if (cardMap[label]) { attachCard(cardMap[label], label); ungroupedBody.appendChild(cardMap[label]); }
  }
  grid.appendChild(ungroupedBody);
}

function renderCard(account) {
  const label = account.label;
  const safeLabel = escapeHtml(label);
  const safeAttr = label.replace(/'/g, "\\'");
  const email = account.email || '';
  const status = account.running ? 'running' : (account.last_run_status || 'idle');
  const points = account.loyalty_points != null ? account.loyalty_points : '&mdash;';
  const scheduleTxt = formatSchedule(account.schedule);
  const lastRunTxt = account.last_run_at ? formatRelativeTime(account.last_run_at) : 'Never run';
  const { expiryTxt, expiryClass } = formatExpiry(account.session_expires_at, account.session_refresh_failed);
  const errors = account.last_run_errors || [];
  const isRunning = account.running;
  const refreshFailed = account.session_refresh_failed;

  const errorsHtml = errors.length > 0 ? `
    <div class="error-accordion">
      <button class="error-toggle" onclick="toggleErrors('${safeAttr}')">
        &#9654; ${errors.length} error(s)
      </button>
      <div class="error-list hidden" id="errors-${safeLabel}">
        ${errors.map(e => `
          <div class="error-item">
            <div class="error-item-task">${escapeHtml(e.task || '')}</div>
            ${escapeHtml(e.error || '')}
          </div>`).join('')}
      </div>
    </div>` : '';

  const logPanelHtml = `
    <div class="log-panel ${isRunning ? '' : 'hidden'}" id="logs-${safeLabel}">
      <div class="log-panel-header">&#9654; Live Output</div>
      <div class="log-lines" id="log-lines-${safeLabel}"></div>
    </div>`;

  return `
    <div class="account-card ${isRunning ? 'is-running' : ''}" id="card-${safeLabel}" data-label="${safeLabel}">
      <div class="card-header">
        <div>
          <div class="card-label">
            <span class="label-text">${safeLabel}</span>
            <button class="btn-edit-label" onclick="startEditLabel('${safeAttr}')" title="Rename">&#9998;</button>
          </div>
          ${email ? `<div class="card-email">${escapeHtml(email)}</div>` : ''}
        </div>
        <span class="status-badge status-${status}">${formatStatus(status)}</span>
      </div>

      <div class="points-display">
        <span class="points-number">${points}</span>
        ${getVisibleDelta(account) > 0 ? `<span class="points-delta">+${getVisibleDelta(account)}</span>` : ''}
        <span class="points-label">IKEA Family Points</span>
      </div>

      <div class="card-meta">
        <div class="meta-row"><span class="meta-icon">&#128197;</span> ${escapeHtml(scheduleTxt)}</div>
        <div class="meta-row"><span class="meta-icon">&#128336;</span> ${escapeHtml(lastRunTxt)}</div>
        <div class="meta-row ${expiryClass}"><span class="meta-icon">&#128274;</span> ${escapeHtml(expiryTxt)}</div>
      </div>

      ${errorsHtml}
      ${logPanelHtml}

      ${refreshFailed ? `
      <div class="reimport-banner">
        <span>&#9888; Auto-refresh failed &mdash; re-import cookies to keep this account active.</span>
        <button class="btn-reimport" onclick="openReimportModal('${safeAttr}')">Re-import</button>
      </div>` : ''}

      ${browserLoginRunning.has(label) ? `
      <div class="browser-login-banner">
        <span>&#9203; Waiting for browser login&hellip; Complete the email code flow in the browser window.</span>
      </div>` : ''}

      ${getActiveVoucherCount(account) > 0 ? `<div class="voucher-badge" onclick="openVouchersModal('${safeAttr}')">&#127881; ${getActiveVoucherCount(account)} active voucher${getActiveVoucherCount(account) > 1 ? 's' : ''}</div>` : ''}

      <div class="card-footer">
        <button class="btn-run" onclick="openRunModal('${safeAttr}')" ${isRunning ? 'disabled' : ''}>
          &#9654; Run
        </button>
        <button class="btn-icon" onclick="openScheduleModal('${safeAttr}')" title="Schedule">&#128197;</button>
        <button class="btn-icon" onclick="openHistoryModal('${safeAttr}')" title="History">&#128336;</button>
        <button class="btn-icon" onclick="openVouchersModal('${safeAttr}')" title="Discount codes">&#127881;</button>
        <button class="btn-icon" onclick="refreshPoints('${safeAttr}')" title="Refresh points">&#8635;</button>
        <button class="btn-icon" onclick="refreshSession('${safeAttr}')" title="Refresh session cookies">&#128274;</button>
        <button class="btn-icon" onclick="triggerBrowserLogin('${safeAttr}')" title="Login via browser">&#127760;</button>
        <button class="btn-icon btn-delete" onclick="deleteAccount('${safeAttr}')" title="Delete">&#128465;</button>
      </div>
    </div>`;
}

function updateCardDOM(label) {
  const card = findCard(label);
  if (!card) return;

  const account = accounts[label];
  if (!account) return;

  const status = account.running ? 'running' : (account.last_run_status || 'idle');
  const isRunning = account.running;
  const points = account.loyalty_points != null ? account.loyalty_points : '&mdash;';
  const scheduleTxt = formatSchedule(account.schedule);
  const lastRunTxt = account.last_run_at ? formatRelativeTime(account.last_run_at) : 'Never run';
  const errors = account.last_run_errors || [];

  // Toggle running class
  card.classList.toggle('is-running', isRunning);

  // Status badge
  const badge = card.querySelector('.status-badge');
  if (badge) {
    badge.className = `status-badge status-${status}`;
    badge.textContent = formatStatus(status);
  }

  // Points + delta badge
  const pointsEl = card.querySelector('.points-number');
  if (pointsEl) pointsEl.innerHTML = points;
  let deltaEl = card.querySelector('.points-delta');
  const visibleDelta = getVisibleDelta(account);
  if (visibleDelta > 0) {
    if (!deltaEl) {
      deltaEl = document.createElement('span');
      deltaEl.className = 'points-delta';
      pointsEl && pointsEl.after(deltaEl);
    }
    deltaEl.textContent = `+${visibleDelta}`;
  } else if (deltaEl) {
    deltaEl.remove();
  }

  // Meta rows
  const metaRows = card.querySelectorAll('.meta-row');
  if (metaRows[0]) metaRows[0].innerHTML = `<span class="meta-icon">&#128197;</span> ${escapeHtml(scheduleTxt)}`;
  if (metaRows[1]) metaRows[1].innerHTML = `<span class="meta-icon">&#128336;</span> ${escapeHtml(lastRunTxt)}`;

  // Error accordion
  let errorAccordion = card.querySelector('.error-accordion');
  if (errors.length > 0) {
    if (!errorAccordion) {
      errorAccordion = document.createElement('div');
      card.querySelector('.card-footer').before(errorAccordion);
    }
    errorAccordion.className = 'error-accordion';
    const safeAttr = label.replace(/'/g, "\\'");
    const safeLabel = escapeHtml(label);
    errorAccordion.innerHTML = `
      <button class="error-toggle" onclick="toggleErrors('${safeAttr}')">
        &#9654; ${errors.length} error(s)
      </button>
      <div class="error-list hidden" id="errors-${safeLabel}">
        ${errors.map(e => `
          <div class="error-item">
            <div class="error-item-task">${escapeHtml(e.task || '')}</div>
            ${escapeHtml(e.error || '')}
          </div>`).join('')}
      </div>`;
  } else if (errorAccordion) {
    errorAccordion.remove();
  }

  // Log panel visibility
  const logPanel = card.querySelector('.log-panel');
  if (logPanel) {
    if (isRunning) logPanel.classList.remove('hidden');
    else if (!isRunning && (account._logs || []).length === 0) logPanel.classList.add('hidden');
  }

  // Browser login banner
  let blBanner = card.querySelector('.browser-login-banner');
  if (browserLoginRunning.has(label)) {
    if (!blBanner) {
      blBanner = document.createElement('div');
      blBanner.className = 'browser-login-banner';
      blBanner.innerHTML = '<span>&#9203; Waiting for browser login&hellip; Complete the email code flow in the browser window.</span>';
      card.querySelector('.card-footer').before(blBanner);
    }
  } else if (blBanner) {
    blBanner.remove();
  }

  // Run button
  const runBtn = card.querySelector('.btn-run');
  if (runBtn) runBtn.disabled = isRunning;

  // Voucher badge
  const safeAttr = label.replace(/'/g, "\\'");
  let voucherBadge = card.querySelector('.voucher-badge');
  const activeCount = getActiveVoucherCount(account);
  if (activeCount > 0) {
    if (!voucherBadge) {
      voucherBadge = document.createElement('div');
      voucherBadge.className = 'voucher-badge';
      card.querySelector('.card-footer').before(voucherBadge);
    }
    voucherBadge.setAttribute('onclick', `openVouchersModal('${safeAttr}')`);
    voucherBadge.innerHTML = `&#127881; ${activeCount} active voucher${activeCount > 1 ? 's' : ''}`;
  } else if (voucherBadge) {
    voucherBadge.remove();
  }
}

// ============================================================
// WebSocket
// ============================================================

function connectWebSocket() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === 'log') appendLog(msg.label, msg.level, msg.message, msg.ts);
      if (msg.type === 'status') handleStatusUpdate(msg);
      if (msg.type === 'accounts_update') scheduleLoadAccounts();
      if (msg.type === 'browser_login_started') {
        browserLoginRunning.add(msg.label);
        updateCardDOM(msg.label);
        if (msg.vnc) openVncModal(msg.label);
      }
      if (msg.type === 'vouchers_update') {
        if (accounts[msg.label]) {
          accounts[msg.label].vouchers = msg.vouchers || [];
          accounts[msg.label].vouchers_updated_at = new Date().toISOString();
          accounts[msg.label].vouchers_error = msg.vouchers_error || null;
          updateCardDOM(msg.label);
          // If vouchers modal is open for this label, refresh its content
          if (_vouchersModalLabel === msg.label && !document.getElementById('modal-vouchers').classList.contains('hidden')) {
            renderVouchersContent(msg.vouchers || [], msg.label);
          }
        }
      }
      if (msg.type === 'browser_login_done') {
        closeVncModal();
        browserLoginRunning.delete(msg.label);
        if (!msg.success) {
          const card = findCard(msg.label);
          if (card) {
            const existing = card.querySelector('.browser-login-banner');
            if (existing) {
              existing.classList.add('browser-login-banner--failed');
              existing.innerHTML = '<span>&#9888; Browser login failed &mdash; try again or re-import cookies.</span>';
              setTimeout(() => existing.remove(), 6000);
            }
          }
        }
        updateCardDOM(msg.label);
      }
    } catch (err) {
      console.error('WS message parse error:', err);
    }
  };

  ws.onclose = () => {
    setTimeout(connectWebSocket, 3000);
  };

  ws.onerror = (err) => {
    console.warn('WS error:', err);
  };
}

function handleStatusUpdate(msg) {
  const label = msg.label;
  if (!accounts[label]) return;

  const isRunning = msg.status === 'running';
  accounts[label].running = isRunning;
  accounts[label].last_run_status = isRunning ? accounts[label].last_run_status : msg.status;
  if (msg.errors) accounts[label].last_run_errors = msg.errors;
  if (msg.points != null) accounts[label].loyalty_points = msg.points;

  if (!isRunning) {
    // Clear logs after a short delay so user can read final line
    setTimeout(() => {
      if (accounts[label]) {
        accounts[label]._logs = [];
        const card = findCard(label);
        const logPanel = card && card.querySelector('.log-panel');
        if (logPanel) logPanel.classList.add('hidden');
        const linesEl = card && card.querySelector('.log-lines');
        if (linesEl) linesEl.innerHTML = '';
      }
    }, 8000);
  }

  updateCardDOM(label);
}

// ============================================================
// Log panel
// ============================================================

function appendLog(label, level, message, ts) {
  if (!accounts[label]) return;

  const logEntry = { level, message, ts };
  if (!accounts[label]._logs) accounts[label]._logs = [];
  accounts[label]._logs.push(logEntry);

  // Keep last 100 lines
  if (accounts[label]._logs.length > 100) {
    accounts[label]._logs = accounts[label]._logs.slice(-100);
  }

  // Show log panel
  const card = findCard(label);
  const logPanel = card && card.querySelector('.log-panel');
  if (logPanel) logPanel.classList.remove('hidden');

  const linesEl = card && card.querySelector('.log-lines');
  if (!linesEl) return;

  const line = document.createElement('div');
  line.innerHTML = renderLogLine(logEntry);
  linesEl.appendChild(line.firstElementChild || line);

  // Keep DOM in sync — remove excess
  while (linesEl.children.length > 100) {
    linesEl.removeChild(linesEl.firstChild);
  }

  // Auto-scroll
  linesEl.scrollTop = linesEl.scrollHeight;
}

function renderLogLine({ level, message, ts }) {
  const timeStr = ts ? new Date(ts).toLocaleTimeString() : '';
  const msgClass = `log-msg-${level || 'info'}`;
  return `<div class="log-line"><span class="log-ts">${escapeHtml(timeStr)}</span><span class="${msgClass}">${escapeHtml(message)}</span></div>`;
}

// ============================================================
// Account actions
// ============================================================

let _runModalLabel = null;

function openRunModal(label) {
  _runModalLabel = label;
  document.getElementById('run-label-title').textContent = label;
  // Reset all checkboxes to checked
  document.querySelectorAll('#modal-run input[type="checkbox"]').forEach(cb => { cb.checked = true; });
  openModal('modal-run');
}

async function submitRun() {
  const label = _runModalLabel;
  if (!label) return;

  const tasks = [...document.querySelectorAll('#modal-run input[type="checkbox"]:checked')]
    .map(cb => cb.value);
  if (tasks.length === 0) { alert('Select at least one task.'); return; }

  closeModal('modal-run');

  const card = findCard(label);
  if (card) {
    const btn = card.querySelector('.btn-run');
    if (btn) btn.disabled = true;
  }
  try {
    const resp = await fetch(`/api/accounts/${encodeURIComponent(label)}/run`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tasks }),
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      alert(`Error: ${data.detail || resp.statusText}`);
      if (card) {
        const btn = card.querySelector('.btn-run');
        if (btn) btn.disabled = false;
      }
    } else {
      if (accounts[label]) {
        accounts[label].running = true;
        accounts[label]._logs = [];
      }
      updateCardDOM(label);
    }
  } catch (err) {
    alert(`Run failed: ${err.message}`);
  }
}

async function deleteAccount(label) {
  if (!confirm(`Delete account "${label}"? This cannot be undone.`)) return;
  try {
    const resp = await fetch(`/api/accounts/${encodeURIComponent(label)}`, { method: 'DELETE' });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      alert(`Error: ${data.detail || resp.statusText}`);
    } else {
      delete accounts[label];
      renderAllCards();
    }
  } catch (err) {
    alert(`Delete failed: ${err.message}`);
  }
}

async function refreshSession(label) {
  const card = findCard(label);
  const btn = card && [...card.querySelectorAll('.btn-icon')].find(b => b.title === 'Refresh session cookies');
  if (btn) { btn.disabled = true; btn.textContent = '⏳'; }
  try {
    await fetch(`/api/accounts/${encodeURIComponent(label)}/refresh-session`, { method: 'POST' });
    // accounts_update WS message will reload the card when done
  } catch (err) {
    alert(`Session refresh failed: ${err.message}`);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '🔒'; }
  }
}

function startEditLabel(label) {
  const card = findCard(label);
  if (!card) return;
  const labelDiv = card.querySelector('.card-label');
  const current = card.querySelector('.label-text').textContent;

  labelDiv.innerHTML = `
    <input class="label-edit-input" value="${escapeHtml(current)}" maxlength="80" />
    <button class="btn-edit-confirm" onclick="submitRenameLabel('${label.replace(/'/g, "\\'")}')">&#10003;</button>
    <button class="btn-edit-cancel" onclick="cancelEditLabel('${label.replace(/'/g, "\\'")}')">&#10005;</button>`;

  const input = labelDiv.querySelector('.label-edit-input');
  input.focus();
  input.select();
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') submitRenameLabel(label);
    if (e.key === 'Escape') cancelEditLabel(label);
  });
}

function cancelEditLabel(label) {
  const card = findCard(label);
  if (!card) return;
  const safeLabel = escapeHtml(label);
  const safeAttr = label.replace(/'/g, "\\'");
  card.querySelector('.card-label').innerHTML = `
    <span class="label-text">${safeLabel}</span>
    <button class="btn-edit-label" onclick="startEditLabel('${safeAttr}')" title="Rename">&#9998;</button>`;
}

async function submitRenameLabel(label) {
  const card = findCard(label);
  if (!card) return;
  const input = card.querySelector('.label-edit-input');
  const newLabel = input ? input.value.trim() : '';
  if (!newLabel) { input && input.focus(); return; }
  if (newLabel === label) { cancelEditLabel(label); return; }

  try {
    const resp = await fetch(`/api/accounts/${encodeURIComponent(label)}/rename`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ new_label: newLabel }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      alert(data.detail || 'Rename failed');
      cancelEditLabel(label);
    }
    // accounts_update WS event will re-render the grid
  } catch (err) {
    alert(`Rename failed: ${err.message}`);
    cancelEditLabel(label);
  }
}

async function triggerBrowserLogin(label) {
  try {
    const resp = await fetch(`/api/accounts/${encodeURIComponent(label)}/browser-login`, { method: 'POST' });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      alert(`Error: ${data.detail || resp.statusText}`);
    }
    // browser_login_started WS event will update the card
  } catch (err) {
    alert(`Browser login failed: ${err.message}`);
  }
}

let _reimportLabel = null;

function openReimportModal(label) {
  _reimportLabel = label;
  document.getElementById('reimport-label-title').textContent = label;
  document.getElementById('reimport-cookies').value = '';
  const errEl = document.getElementById('reimport-error');
  errEl.textContent = '';
  errEl.classList.add('hidden');
  openModal('modal-reimport');
}

async function submitReimport() {
  const label = _reimportLabel;
  if (!label) return;
  const cookiesJson = document.getElementById('reimport-cookies').value.trim();
  const errEl = document.getElementById('reimport-error');
  const btn = document.getElementById('btn-submit-reimport');

  errEl.classList.add('hidden');
  btn.disabled = true;
  btn.textContent = 'Updating…';

  try {
    const resp = await fetch(`/api/accounts/${encodeURIComponent(label)}/cookies`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label, cookies_json: cookiesJson }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      errEl.textContent = data.detail || resp.statusText;
      errEl.classList.remove('hidden');
    } else {
      closeModal('modal-reimport');
    }
  } catch (err) {
    errEl.textContent = `Error: ${err.message}`;
    errEl.classList.remove('hidden');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Update Cookies';
  }
}

async function refreshPoints(label) {
  try {
    const resp = await fetch(`/api/accounts/${encodeURIComponent(label)}/refresh`, { method: 'POST' });
    if (resp.ok) {
      const data = await resp.json();
      if (accounts[label]) accounts[label].loyalty_points = data.loyalty_points;
      updateCardDOM(label);
    }
  } catch (err) {
    console.error('refreshPoints error:', err);
  }
}

// ============================================================
// Add Account modal
// ============================================================

async function submitAddViaBrowser() {
  const label = document.getElementById('add-label').value.trim();
  const errEl = document.getElementById('add-error');
  const btn = document.getElementById('btn-submit-browser');

  errEl.classList.add('hidden');
  errEl.textContent = '';
  if (!label) { errEl.textContent = 'Label is required'; errEl.classList.remove('hidden'); return; }

  btn.disabled = true;
  btn.textContent = 'Opening browser…';

  try {
    const resp = await fetch('/api/accounts/add-via-browser', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      errEl.textContent = data.detail || 'Failed to start browser login';
      errEl.classList.remove('hidden');
    } else {
      closeModal('modal-add');
      document.getElementById('add-label').value = '';
    }
  } catch (err) {
    errEl.textContent = `Request failed: ${err.message}`;
    errEl.classList.remove('hidden');
  } finally {
    btn.disabled = false;
    btn.textContent = '\u{1F310} Login via Browser';
  }
}

async function submitAddAccount() {
  const label = document.getElementById('add-label').value.trim();
  const cookieJson = document.getElementById('add-cookies').value.trim();
  const errEl = document.getElementById('add-error');
  const btn = document.getElementById('btn-submit-add');

  errEl.classList.add('hidden');
  errEl.textContent = '';

  if (!label) { showAddError('Label is required'); return; }
  if (!cookieJson) { showAddError('Cookie JSON is required'); return; }

  btn.disabled = true;
  btn.textContent = 'Adding…';

  try {
    const resp = await fetch('/api/accounts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label, cookies_json: cookieJson }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      showAddError(data.detail || 'Failed to add account');
      return;
    }
    // Success — WS accounts_update will refresh the grid
    closeModal('modal-add');
    document.getElementById('add-label').value = '';
    document.getElementById('add-cookies').value = '';
  } catch (err) {
    showAddError(`Request failed: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Add Account';
  }
}

function showAddError(msg) {
  const errEl = document.getElementById('add-error');
  errEl.textContent = msg;
  errEl.classList.remove('hidden');
}

// ============================================================
// Schedule modal
// ============================================================

function buildScheduleSelects() {
  const hourSel = document.getElementById('sched-hour');
  for (let h = 0; h < 24; h++) {
    const opt = document.createElement('option');
    opt.value = h;
    opt.textContent = String(h).padStart(2, '0');
    hourSel.appendChild(opt);
  }

  const minSel = document.getElementById('sched-minute');
  for (const m of [0, 15, 30, 45]) {
    const opt = document.createElement('option');
    opt.value = m;
    opt.textContent = String(m).padStart(2, '0');
    minSel.appendChild(opt);
  }
}

function openScheduleModal(label) {
  currentScheduleLabel = label;
  document.getElementById('schedule-label-title').textContent = label;

  const account = accounts[label];
  const schedule = account ? account.schedule : null;

  // Reset checkboxes
  document.querySelectorAll('.day-grid input[type="checkbox"]').forEach(cb => {
    cb.checked = schedule && schedule.days ? schedule.days.includes(parseInt(cb.value)) : false;
  });

  // Set time
  const hour = schedule ? schedule.hour : 8;
  const minute = schedule ? schedule.minute : 0;
  document.getElementById('sched-hour').value = hour;

  // Find nearest valid minute option
  const validMinutes = [0, 15, 30, 45];
  const nearestMinute = validMinutes.reduce((a, b) => Math.abs(b - minute) < Math.abs(a - minute) ? b : a);
  document.getElementById('sched-minute').value = nearestMinute;

  openModal('modal-schedule');
}

async function saveSchedule() {
  const label = currentScheduleLabel;
  if (!label) return;

  const days = [];
  document.querySelectorAll('.day-grid input[type="checkbox"]:checked').forEach(cb => {
    days.push(parseInt(cb.value));
  });

  const hour = parseInt(document.getElementById('sched-hour').value);
  const minute = parseInt(document.getElementById('sched-minute').value);

  try {
    const resp = await fetch(`/api/accounts/${encodeURIComponent(label)}/schedule`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ days, hour, minute }),
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      alert(`Error: ${data.detail || resp.statusText}`);
      return;
    }
    closeModal('modal-schedule');
    await loadAccounts();
  } catch (err) {
    alert(`Save failed: ${err.message}`);
  }
}

async function clearSchedule() {
  const label = currentScheduleLabel;
  if (!label) return;

  try {
    await fetch(`/api/accounts/${encodeURIComponent(label)}/schedule`, { method: 'DELETE' });
    closeModal('modal-schedule');
    await loadAccounts();
  } catch (err) {
    alert(`Clear failed: ${err.message}`);
  }
}

// ============================================================
// History modal
// ============================================================

async function openHistoryModal(label) {
  document.getElementById('history-label').textContent = label;
  document.getElementById('history-table').innerHTML = '<p style="color:var(--text-muted);padding:8px 0">Loading…</p>';
  openModal('modal-history');

  try {
    const [logsResp, rewardResp] = await Promise.all([
      fetch(`/api/accounts/${encodeURIComponent(label)}/logs`),
      fetch(`/api/accounts/${encodeURIComponent(label)}/reward-history`),
    ]);
    const logs   = await logsResp.json();
    const reward = await rewardResp.json();
    renderHistoryTable(logs, reward);
  } catch (err) {
    document.getElementById('history-table').innerHTML = `<p style="color:var(--error)">Failed to load: ${escapeHtml(err.message)}</p>`;
  }
}

function renderHistoryTable(logs, rewardItems) {
  const container = document.getElementById('history-table');

  // ── Reward history from IKEA API ─────────────────────────────────────────
  let rewardHtml = '';
  if (rewardItems && rewardItems.length > 0) {
    // Group by calendar date
    const byDate = {};
    for (const item of rewardItems) {
      const date = item.datetime ? item.datetime.slice(0, 10) : '?';
      if (!byDate[date]) byDate[date] = [];
      byDate[date].push(item);
    }

    // 24h delta banner
    const cutoff = Date.now() - 24 * 3600 * 1000;
    const delta24h = rewardItems
      .filter(i => i.type === 'TokenAdded' && new Date(i.datetime).getTime() >= cutoff)
      .reduce((s, i) => s + i.value, 0);
    const bannerHtml = delta24h > 0
      ? `<div class="reward-delta-banner">&#128197; <strong>+${delta24h} pts</strong> earned in the last 24 h</div>`
      : '';

    const dateRows = Object.entries(byDate).map(([date, items]) => {
      const label = new Date(date).toLocaleDateString(undefined, { weekday: 'short', year: 'numeric', month: 'short', day: 'numeric' });
      const itemRows = items.map(item => {
        const isAdd = item.type === 'TokenAdded';
        const sign  = isAdd ? '+' : '−';
        const cls   = isAdd ? 'reward-pts-add' : 'reward-pts-remove';
        return `<tr>
          <td class="reward-time">${new Date(item.datetime).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})}</td>
          <td class="reward-desc">${escapeHtml(item.description || item.type)}</td>
          <td class="reward-pts ${cls}">${sign}${item.value}</td>
        </tr>`;
      }).join('');
      return `<tr class="reward-date-header"><td colspan="3">${escapeHtml(label)}</td></tr>${itemRows}`;
    }).join('');

    rewardHtml = `
      <h3 class="history-section-title">Reward Points History</h3>
      ${bannerHtml}
      <table class="history-tbl reward-history-tbl" style="margin-bottom:24px">
        <thead><tr><th>Time</th><th>Description</th><th>Points</th></tr></thead>
        <tbody>${dateRows}</tbody>
      </table>`;
  }

  // ── Run logs ──────────────────────────────────────────────────────────────
  let runHtml = '';
  if (logs && logs.length > 0) {
    const rows = logs.map(log => {
      const status   = log.status || 'unknown';
      const started  = log.started_at ? new Date(log.started_at).toLocaleString() : '—';
      const duration = (log.started_at && log.finished_at)
        ? formatDuration(log.started_at, log.finished_at) : '—';
      const errors   = log.errors || [];
      const errHtml  = errors.length > 0
        ? `<div class="history-errors">${errors.map(e =>
            `<div class="history-error-item">[${escapeHtml(e.task||'')}] ${escapeHtml(e.error||'')}</div>`
          ).join('')}</div>`
        : '<span style="color:var(--success)">—</span>';
      return `<tr>
        <td>${escapeHtml(started)}</td>
        <td>${escapeHtml(duration)}</td>
        <td><span class="status-badge status-${status}">${formatStatus(status)}</span></td>
        <td>${errHtml}</td>
      </tr>`;
    }).join('');
    runHtml = `
      <h3 class="history-section-title">Run History</h3>
      <table class="history-tbl">
        <thead><tr><th>Started</th><th>Duration</th><th>Status</th><th>Errors</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  if (!rewardHtml && !runHtml) {
    container.innerHTML = '<p style="color:var(--text-muted);padding:8px 0">No history yet. Run an account or refresh points to populate.</p>';
    return;
  }
  container.innerHTML = rewardHtml + runHtml;
}

// ============================================================
// Vouchers modal
// ============================================================

let _vouchersModalLabel = null;

function getActiveVoucherCount(account) {
  const vouchers = account.vouchers || [];
  return vouchers.filter(v => v.active).length;
}

async function openVouchersModal(label) {
  _vouchersModalLabel = label;
  document.getElementById('vouchers-label').textContent = label;
  const account = accounts[label];
  const vouchers = (account && account.vouchers) || [];
  renderVouchersContent(vouchers, label);
  openModal('modal-vouchers');
}

function renderVouchersContent(vouchers, label) {
  const container = document.getElementById('vouchers-content');
  const account = accounts[label] || {};
  const updatedAt = account.vouchers_updated_at
    ? `<p class="modal-sub">Last checked: ${new Date(account.vouchers_updated_at).toLocaleString()}</p>`
    : `<p class="modal-sub" style="color:var(--text-muted)">Not yet fetched — click Refresh to check.</p>`;

  const vouchersError = account.vouchers_error;
  if (vouchersError === 'session_expired') {
    container.innerHTML = updatedAt +
      `<div class="vouchers-error-banner">
        ⚠️ ikeafamily.eu session expired — click <strong>Login via Browser</strong> on the account card to re-authenticate.
      </div>`;
    return;
  }

  if (vouchers.length === 0) {
    container.innerHTML = updatedAt + `<p style="color:var(--text-muted);padding:12px 0">No vouchers found for this account.</p>`;
    return;
  }

  const rows = vouchers.map(v => {
    const activeClass = v.active ? 'voucher-active' : 'voucher-expired';
    const activeLbl = v.active
      ? `<span class="voucher-status-active">Active</span>`
      : `<span class="voucher-status-expired">Expired</span>`;
    const codeCell = v.code
      ? `<span class="voucher-code-value">${escapeHtml(v.code)}</span>`
      : `<span style="color:var(--text-muted)">${v.active ? 'Not revealed' : '—'}</span>`;
    return `<tr class="${activeClass}">
      <td>${escapeHtml(v.issued)}</td>
      <td>${escapeHtml(v.expires)}</td>
      <td><strong>${escapeHtml(v.amount)}</strong></td>
      <td>${escapeHtml(v.location)}</td>
      <td>${activeLbl}</td>
      <td>${codeCell}</td>
    </tr>`;
  }).join('');

  container.innerHTML = updatedAt + `
    <table class="history-tbl vouchers-tbl">
      <thead>
        <tr>
          <th>Issued</th>
          <th>Expires</th>
          <th>Amount</th>
          <th>Where</th>
          <th>Status</th>
          <th>Code</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
}

async function triggerVoucherRefresh() {
  const label = _vouchersModalLabel;
  if (!label) return;
  const btn = document.getElementById('btn-refresh-vouchers');
  btn.disabled = true;
  btn.textContent = '⏳ Fetching…';
  document.getElementById('vouchers-content').innerHTML =
    '<p style="color:var(--text-muted);padding:12px 0">Fetching vouchers from ikeafamily.eu… this may take a minute.</p>';
  try {
    const resp = await fetch(`/api/accounts/${encodeURIComponent(label)}/vouchers/refresh`, { method: 'POST' });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      document.getElementById('vouchers-content').innerHTML =
        `<p style="color:var(--error)">Error: ${escapeHtml(data.detail || resp.statusText)}</p>`;
    }
    // Result comes back via vouchers_update WS message
  } catch (err) {
    document.getElementById('vouchers-content').innerHTML =
      `<p style="color:var(--error)">Error: ${escapeHtml(err.message)}</p>`;
  } finally {
    btn.disabled = false;
    btn.textContent = '↻ Refresh';
  }
}

// ============================================================
// Error accordion toggle
// ============================================================

function toggleErrors(label) {
  const card = findCard(label);
  const list = card && card.querySelector('.error-list');
  const btn = list ? list.previousElementSibling : null;
  if (!list) return;
  const isHidden = list.classList.toggle('hidden');
  if (btn) btn.innerHTML = `${isHidden ? '&#9654;' : '&#9660;'} ${list.children.length} error(s)`;
}

// ============================================================
// Modal helpers
// ============================================================

function openModal(id) {
  document.getElementById(id).classList.remove('hidden');
  document.body.style.overflow = 'hidden';
}

function closeModal(id) {
  document.getElementById(id).classList.add('hidden');
  document.body.style.overflow = '';
}

// Close modal on Escape key
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    document.querySelectorAll('.modal:not(.hidden)').forEach(m => {
      m.classList.add('hidden');
    });
    document.body.style.overflow = '';
  }
});

// ============================================================
// Formatting utilities
// ============================================================

function getVisibleDelta(account) {
  if (!account.points_delta || !account.points_delta_at) return 0;
  const age = Date.now() - new Date(account.points_delta_at).getTime();
  return age < 24 * 3600 * 1000 ? account.points_delta : 0;
}

function formatExpiry(isoString, refreshFailed) {
  if (!isoString) return { expiryTxt: 'Session expiry unknown', expiryClass: 'meta-warn' };
  const diff = Math.floor((new Date(isoString) - Date.now()) / 1000);
  if (diff < 0) return { expiryTxt: 'Session expired — re-import cookies', expiryClass: 'meta-error' };
  const hours = Math.floor(diff / 3600);
  const mins = Math.floor((diff % 3600) / 60);
  const txt = hours > 0 ? `Session expires in ${hours}h ${mins}m` : `Session expires in ${mins}m`;
  const cls = refreshFailed || diff < 7200 ? 'meta-error' : diff < 28800 ? 'meta-warn' : '';
  return { expiryTxt: txt, expiryClass: cls };
}

function formatRelativeTime(isoString) {
  if (!isoString) return 'Never';
  const date = new Date(isoString);
  const diff = Math.floor((Date.now() - date.getTime()) / 1000);
  if (diff < 60) return 'just now';
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function formatDuration(start, end) {
  const ms = new Date(end) - new Date(start);
  if (ms < 0) return '—';
  const secs = Math.floor(ms / 1000);
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  const rem = secs % 60;
  return `${mins}m ${rem}s`;
}

const DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

function formatSchedule(schedule) {
  if (!schedule || !schedule.days || schedule.days.length === 0) return 'No schedule';
  const days = [...schedule.days].sort().map(d => DAY_NAMES[d] || '?').join(', ');
  const h = String(schedule.hour || 0).padStart(2, '0');
  const m = String(schedule.minute || 0).padStart(2, '0');
  return `${days} @ ${h}:${m}`;
}

function formatStatus(status) {
  const map = {
    ok: 'OK',
    partial: 'Partial',
    failed: 'Failed',
    running: 'Running',
    idle: 'Idle',
  };
  return map[status] || status || 'Idle';
}

// ============================================================
// VNC modal
// ============================================================

function openVncModal(label) {
  document.getElementById('vnc-label-title').textContent = label;
  const iframe = document.getElementById('vnc-iframe');
  iframe.src = `/novnc/vnc.html?path=/ws/vnc&autoconnect=1&resize=scale&reconnect=1&reconnect_delay=2000`;
  openModal('modal-vnc');
}

function closeVncModal() {
  if (document.getElementById('modal-vnc').classList.contains('hidden')) return;
  const iframe = document.getElementById('vnc-iframe');
  iframe.src = 'about:blank';
  closeModal('modal-vnc');
}

function escapeHtml(str) {
  if (str == null) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}
