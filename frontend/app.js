// MailJaeger Dashboard — v1.1.0

const API_BASE = window.location.origin;

// ── State ──────────────────────────────────────────────────────────────
let _statusPollTimer = null;
let _isProcessing = false;
let _dashboardTimer = null;
let currentFilters = { category: null, priority: null, action_required: null };

// ── Bootstrap ──────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
    const ok = await checkAuthenticated();
    if (!ok) {
        openLoginModal();
    } else {
        await boot();
    }
});

async function checkAuthenticated() {
    try {
        const r = await fetch(`${API_BASE}/api/auth/verify`);
        return r.ok;
    } catch { return false; }
}

async function boot() {
    await loadVersion();
    await loadDashboard();
    await loadEmails();
    wireListeners();
    startStatusPolling();
    // Refresh dashboard every 30 s when idle
    _dashboardTimer = setInterval(loadDashboard, 30_000);
}

// ── Toast / Error banner ───────────────────────────────────────────────
let _toastTimer = null;
function showToast(msg, type = 'error', ms = 5000) {
    const el = document.getElementById('toastBanner');
    if (!el) return;
    el.textContent = msg;
    el.className = type;
    el.style.display = 'block';
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => { el.style.display = 'none'; }, ms);
}

// ── Login ──────────────────────────────────────────────────────────────
function openLoginModal() {
    const m = document.getElementById('loginModal');
    m.classList.add('active');
    document.getElementById('apiKeyInput').focus();
    document.getElementById('loginForm').addEventListener('submit', handleLogin);
}

async function handleLogin(e) {
    e.preventDefault();
    const key = document.getElementById('apiKeyInput').value.trim();
    const err = document.getElementById('loginError');
    const btn = document.getElementById('loginBtn');
    err.style.display = 'none';
    if (!key) { err.textContent = 'Bitte API-Schlüssel eingeben.'; err.style.display = 'block'; return; }

    btn.disabled = true;
    btn.textContent = 'Prüfe…';
    try {
        const r = await fetch(`${API_BASE}/api/auth/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ api_key: key }),
        });
        if (r.ok) { location.reload(); return; }
        const d = await r.json().catch(() => ({}));
        err.textContent = d.detail || 'Ungültiger API-Schlüssel.';
        err.style.display = 'block';
        document.getElementById('apiKeyInput').value = '';
        document.getElementById('apiKeyInput').focus();
    } catch { err.textContent = 'Verbindungsfehler.'; err.style.display = 'block'; }
    finally { btn.disabled = false; btn.textContent = 'Sign In'; }
}

async function handleLogout() {
    try { await fetch(`${API_BASE}/api/auth/logout`, { method: 'POST' }); } catch {}
    location.reload();
}

// ── Version ────────────────────────────────────────────────────────────
async function loadVersion() {
    try {
        const r = await fetch(`${API_BASE}/api/version`);
        if (!r.ok) return;
        const d = await r.json();
        const badge = document.getElementById('versionBadge');
        if (badge) badge.textContent = `v${d.version}`;
        window._changelog = d.changelog || [];
    } catch {}
}

function openVersionModal() {
    const modal = document.getElementById('versionModal');
    const body  = document.getElementById('versionModalBody');
    modal.classList.add('active');
    const cl = window._changelog || [];
    if (!cl.length) { body.innerHTML = '<p style="color:#718096;">Kein Verlauf verfügbar.</p>'; return; }
    body.innerHTML = cl.map(e => `
        <div style="margin-bottom:18px;padding-bottom:14px;border-bottom:1px solid #e2e8f0;">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
                <strong style="font-size:15px;color:#2d3748;">v${e.version}</strong>
                <span style="font-size:12px;color:#718096;">${e.date||''}</span>
            </div>
            <ul style="margin:0;padding-left:18px;">
                ${(e.changes||[]).map(c=>`<li style="font-size:13px;color:#4a5568;margin-bottom:3px;">${escHtml(c)}</li>`).join('')}
            </ul>
        </div>`).join('');
}

// ── Status polling ─────────────────────────────────────────────────────
function startStatusPolling() {
    if (_statusPollTimer) return;
    pollStatus();
    _statusPollTimer = setInterval(pollStatus, 2000);
}

function stopStatusPolling() {
    clearInterval(_statusPollTimer);
    _statusPollTimer = null;
}

async function pollStatus() {
    try {
        const r = await fetch(`${API_BASE}/api/status`);
        if (r.status === 401) { stopStatusPolling(); return; }
        if (!r.ok) return;
        const d = await r.json();
        renderStatus(d);
    } catch {}
}

function renderStatus(d) {
    const section   = document.getElementById('progressSection');
    const label     = document.getElementById('progressLabel');
    const pctEl     = document.getElementById('progressPercent');
    const bar       = document.getElementById('progressBar');
    const counts    = document.getElementById('progressCounts');
    const trigBtn   = document.getElementById('triggerProcessing');
    const trigLbl   = document.getElementById('triggerLabel');
    const trigIco   = document.getElementById('triggerIcon');
    const cancelBtn = document.getElementById('cancelRunBtn');

    const isActive = isActiveRun(d.status);

    if (isActive) {
        _isProcessing = true;
        section.style.display = 'block';

        // Phase badge: show ingestion vs analysis phase label
        const phaseLabel = d.phase === 'ingestion' ? '📥 Einlesen' : d.phase === 'analysis' ? '🔍 Analyse' : '';

        if (d.status === 'cancelling') {
            label.textContent = 'Wird abgebrochen…';
            bar.style.background = 'var(--warning)';
        } else {
            const stepText = d.current_step || 'Verarbeitung läuft…';
            label.textContent = phaseLabel ? `${phaseLabel} · ${stepText}` : stepText;
            bar.style.background = d.phase === 'ingestion' ? 'var(--secondary)' : '';
        }

        pctEl.textContent = `${d.progress_percent || 0} %`;
        bar.style.width   = `${d.progress_percent || 0}%`;

        // Count detail — all from same run_status object
        const parts = [];
        if (d.total > 0) parts.push(`${d.processed}/${d.total} verarbeitet`);
        if (d.spam > 0)  parts.push(`${d.spam} Spam`);
        if (d.action_required > 0) parts.push(`${d.action_required} Aktion`);
        if (d.failed > 0) parts.push(`${d.failed} Fehler`);
        counts.innerHTML = parts.map(p => `<span>${escHtml(p)}</span>`).join('');

        // Cancel button: visible during active run, disabled while already cancelling
        if (cancelBtn) {
            cancelBtn.style.display = 'inline-flex';
            cancelBtn.disabled = d.status === 'cancelling';
            cancelBtn.textContent = d.status === 'cancelling' ? 'Wird abgebrochen…' : 'Abbrechen';
        }

        // Trigger button: disabled while running
        if (trigBtn) {
            trigBtn.disabled = true;
            if (trigIco) trigIco.outerHTML = '<span class="spinner" id="triggerIcon"></span>';
            if (trigLbl) trigLbl.textContent = 'Läuft…';
        }

        // "Letzte Verarbeitung" section: show LIVE data from run_status (not stale DB)
        renderLastRunFromStatus(d);

    } else {
        // Not active: idle / success / failed / cancelled
        const wasActive = _isProcessing;
        _isProcessing = false;

        section.style.display = 'none';
        if (cancelBtn) cancelBtn.style.display = 'none';

        // Restore trigger button
        if (trigBtn) {
            trigBtn.disabled = false;
            const ico = document.getElementById('triggerIcon');
            if (ico && ico.classList.contains('spinner')) {
                ico.outerHTML = `<svg id="triggerIcon" width="14" height="14" fill="currentColor" viewBox="0 0 16 16">
                    <path fill-rule="evenodd" d="M8 3a5 5 0 1 0 4.546 2.914.5.5 0 0 1 .908-.417A6 6 0 1 1 8 2v1z"/>
                    <path d="M8 4.466V.534a.25.25 0 0 1 .41-.192l2.36 1.966c.12.1.12.284 0 .384L8.41 4.658A.25.25 0 0 1 8 4.466z"/>
                </svg>`;
            }
            if (trigLbl) trigLbl.textContent = 'Jetzt verarbeiten';
        }

        // If just finished: reload dashboard/list and show toast
        if (wasActive) {
            loadDashboard();
            loadEmails();
            if (d.status === 'success')   showToast('Verarbeitung abgeschlossen ✓', 'success');
            else if (d.status === 'failed')    showToast('Verarbeitung mit Fehler beendet', 'error');
            else if (d.status === 'cancelled') showToast('Verarbeitung abgebrochen', 'info');
        }
    }
}

/**
 * Update the "Letzte Verarbeitung" info section with LIVE data from run_status.
 * Called every 2 s while a run is active so the section never shows stale DB values.
 */
function renderLastRunFromStatus(d) {
    const time      = document.getElementById('lastRunTime');
    const processed = document.getElementById('lastRunProcessed');
    const badge     = document.getElementById('lastRunStatus');

    if (time && d.started_at) time.textContent = fmtDt(d.started_at);
    if (processed) {
        const p = d.processed || 0;
        const t = d.total || 0;
        const f = d.failed || 0;
        processed.textContent = t > 0 ? `${p}/${t} (${f} Fehler)` : `${p} (${f} Fehler)`;
    }
    if (badge) {
        badge.textContent = statusLabel(d.status);
        badge.className   = 'info-badge ' + statusCls(d.status);
    }
}

// ── Dashboard ──────────────────────────────────────────────────────────
async function loadDashboard() {
    try {
        const r = await fetch(`${API_BASE}/api/dashboard`);
        if (r.status === 401) { showToast('Sitzung abgelaufen – bitte neu anmelden', 'error'); setTimeout(() => location.reload(), 2000); return; }
        if (!r.ok) { showToast(`Dashboard-Fehler: ${r.status}`, 'error'); return; }
        const d = await r.json();

        document.getElementById('totalEmails').textContent    = d.total_emails ?? '–';
        document.getElementById('actionRequired').textContent = d.action_required_count ?? '–';
        document.getElementById('unresolvedCount').textContent= d.unresolved_count ?? '–';
        document.getElementById('spamFiltered').textContent   = d.last_run?.emails_spam ?? '–';

        // ── "Letzte Verarbeitung" ────────────────────────────────────────
        // Rule: if a run is currently active (running / cancelling), the
        // renderStatus() loop updates this section every 2 s from run_status.
        // We only update it here (from the DB last_run record) when the
        // backend is idle / completed, so we never overwrite live counters
        // with stale DB data.
        const liveStatus = d.run_status?.status;
        const isLiveActive = isActiveRun(liveStatus);

        if (!isLiveActive) {
            const lr = d.last_run;
            if (lr) {
                document.getElementById('lastRunTime').textContent      = fmtDt(lr.started_at);
                document.getElementById('lastRunProcessed').textContent = `${lr.emails_processed} (${lr.emails_failed} Fehler)`;
                const badge = document.getElementById('lastRunStatus');
                badge.textContent = statusLabel(lr.status);
                badge.className   = 'info-badge ' + statusCls(lr.status);
            } else {
                document.getElementById('lastRunTime').textContent = 'Noch keine Verarbeitung';
            }
        }
        document.getElementById('nextRunTime').textContent = d.next_scheduled_run ? fmtDt(d.next_scheduled_run) : 'Nicht geplant';

        // ── Health badge ─────────────────────────────────────────────────
        // Prefer the backend-computed overall_status; fall back to per-service
        // check for backward compatibility with older backend versions.
        const hs  = d.health_status;
        const dot = document.getElementById('healthDot');
        const txt = document.getElementById('healthText');
        const overall = hs?.overall_status;
        if (overall === 'OK') {
            if (dot) dot.className = 'status-dot healthy';
            if (txt) txt.textContent = 'System OK';
        } else if (overall === 'DEGRADED') {
            if (dot) dot.className = 'status-dot degraded';
            if (txt) txt.textContent = 'Eingeschränkt';
        } else if (overall === 'ERROR') {
            if (dot) dot.className = 'status-dot unhealthy';
            if (txt) txt.textContent = 'Fehler';
        } else {
            // Fallback: check individual services
            const ok = hs?.mail_server?.status === 'healthy' && hs?.ai_service?.status === 'healthy';
            if (dot) dot.className = 'status-dot ' + (ok ? 'healthy' : 'unhealthy');
            if (txt) txt.textContent = ok ? 'System OK' : 'Problem';
        }

        // Safe mode badge
        const smb = document.getElementById('safeModeBadge');
        if (smb) smb.style.display = d.safe_mode ? 'inline-block' : 'none';

        // Daily report button
        const reportBtn = document.getElementById('viewDailyReport');
        if (reportBtn) reportBtn.style.display = d.daily_report_available ? 'inline-flex' : 'none';

    } catch (ex) {
        showToast('Dashboard konnte nicht geladen werden', 'error');
    }
}

// ── Email list ─────────────────────────────────────────────────────────
async function loadEmails() {
    const list = document.getElementById('emailList');
    list.innerHTML = '<div class="loading">Lade Emails…</div>';

    const body = { page: 1, page_size: 50, sort_by: 'date', sort_order: 'desc' };
    if (currentFilters.category)       body.category        = currentFilters.category;
    if (currentFilters.priority)       body.priority        = currentFilters.priority;
    if (currentFilters.action_required != null) body.action_required = currentFilters.action_required;

    try {
        const r = await fetch(`${API_BASE}/api/emails/list`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (r.status === 401) { list.innerHTML = '<div class="loading">Nicht autorisiert</div>'; return; }
        if (!r.ok) { list.innerHTML = '<div class="loading">Fehler beim Laden</div>'; showToast(`Email-Liste: HTTP ${r.status}`, 'error'); return; }
        const emails = await r.json();
        renderEmails(Array.isArray(emails) ? emails : []);
    } catch {
        list.innerHTML = '<div class="loading">Fehler beim Laden</div>';
        showToast('E-Mail-Liste konnte nicht geladen werden', 'error');
    }
}

function renderEmails(emails) {
    const list = document.getElementById('emailList');
    if (!emails.length) { list.innerHTML = '<div class="loading">Keine Emails gefunden</div>'; return; }
    list.innerHTML = emails.map(e => `
        <div class="email-item" onclick="openEmail(${e.id})">
            <div class="email-priority ${escHtml(e.priority||'LOW')}"></div>
            <div class="email-content">
                <div class="email-header">
                    <div>
                        <div class="email-subject">${escHtml(e.subject||'Kein Betreff')}</div>
                        <div class="email-sender">${escHtml(e.sender||'')}</div>
                    </div>
                    <div class="email-badges">
                        ${e.category ? `<span class="badge category">${escHtml(e.category)}</span>` : ''}
                        ${e.action_required ? '<span class="badge action">Aktion</span>' : ''}
                    </div>
                </div>
                ${e.summary ? `<div class="email-summary">${escHtml(e.summary)}</div>` : ''}
                <div class="email-meta">
                    <span>📅 ${fmtDate(e.date)}</span>
                    <span>⚡ ${escHtml(e.priority||'LOW')}</span>
                    ${e.tasks?.length ? `<span>✓ ${e.tasks.length}</span>` : ''}
                </div>
            </div>
        </div>`).join('');
}

async function openEmail(id) {
    const modal = document.getElementById('emailModal');
    const body  = document.getElementById('modalBody');
    modal.classList.add('active');
    body.innerHTML = '<div class="loading">Lade…</div>';
    try {
        const r = await fetch(`${API_BASE}/api/emails/${id}`);
        if (!r.ok) { body.innerHTML = '<div class="loading">Fehler</div>'; return; }
        const e = await r.json();
        document.getElementById('modalSubject').textContent = e.subject || '–';
        body.innerHTML = `
            <div class="detail-section">
                <h3>Details</h3>
                <div class="detail-grid">
                    <div class="detail-item"><div class="detail-label">Von</div><div class="detail-value">${escHtml(e.sender||'–')}</div></div>
                    <div class="detail-item"><div class="detail-label">Datum</div><div class="detail-value">${fmtDt(e.date)}</div></div>
                    <div class="detail-item"><div class="detail-label">Kategorie</div><div class="detail-value">${escHtml(e.category||'–')}</div></div>
                    <div class="detail-item"><div class="detail-label">Priorität</div><div class="detail-value">${escHtml(e.priority||'–')}</div></div>
                    <div class="detail-item"><div class="detail-label">Spam</div><div class="detail-value">${e.spam_probability != null ? (e.spam_probability*100).toFixed(1)+'%' : '–'}</div></div>
                    <div class="detail-item"><div class="detail-label">Status</div><div class="detail-value">${e.is_resolved ? '✓ Bearbeitet' : '○ Offen'}</div></div>
                </div>
            </div>
            ${e.summary ? `<div class="detail-section"><h3>Zusammenfassung</h3><div class="detail-value">${escHtml(e.summary)}</div></div>` : ''}
            ${e.reasoning ? `<div class="detail-section"><h3>KI-Analyse</h3><div class="detail-value">${escHtml(e.reasoning)}</div></div>` : ''}
            ${e.tasks?.length ? `<div class="detail-section"><h3>Aufgaben (${e.tasks.length})</h3><div class="task-list">${e.tasks.map(t => `
                <div class="task-item">
                    <div class="task-description">${escHtml(t.description)}</div>
                    <div class="task-meta">
                        ${t.due_date ? `<span>📅 ${fmtDate(t.due_date)}</span>` : ''}
                        ${t.confidence != null ? `<span>🎯 ${(t.confidence*100).toFixed(0)}%</span>` : ''}
                    </div>
                </div>`).join('')}</div></div>` : ''}
            ${!e.is_resolved ? `<div class="detail-section"><button class="btn btn-primary" onclick="markResolved(${e.id})">Als bearbeitet markieren</button></div>` : ''}`;
    } catch { body.innerHTML = '<div class="loading">Fehler beim Laden</div>'; }
}

async function markResolved(id) {
    try {
        const r = await fetch(`${API_BASE}/api/emails/${id}/resolve`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email_id: id, resolved: true }),
        });
        if (!r.ok) { showToast('Fehler beim Markieren', 'error'); return; }
        document.getElementById('emailModal').classList.remove('active');
        showToast('Als bearbeitet markiert ✓', 'success');
        await loadDashboard();
        await loadEmails();
    } catch { showToast('Fehler beim Markieren', 'error'); }
}

// ── Cancel processing ──────────────────────────────────────────────────
async function cancelProcessing() {
    const btn = document.getElementById('cancelRunBtn');
    if (btn) { btn.disabled = true; btn.textContent = 'Wird abgebrochen…'; }
    try {
        const r = await fetch(`${API_BASE}/api/processing/cancel`, { method: 'POST' });
        if (!r.ok) {
            showToast('Abbruch fehlgeschlagen', 'error');
            // Re-enable button so user can retry
            if (btn) { btn.disabled = false; btn.textContent = 'Abbrechen'; }
        }
    } catch {
        showToast('Verbindungsfehler beim Abbrechen', 'error');
        if (btn) { btn.disabled = false; btn.textContent = 'Abbrechen'; }
    }
}

// ── Trigger processing ─────────────────────────────────────────────────
async function triggerProcessing() {
    if (_isProcessing) return;
    const btn = document.getElementById('triggerProcessing');
    const lbl = document.getElementById('triggerLabel');
    btn.disabled = true;

    // Immediate optimistic UI: show progress section
    const section = document.getElementById('progressSection');
    section.style.display = 'block';
    document.getElementById('progressLabel').textContent  = 'Starte…';
    document.getElementById('progressPercent').textContent= '0 %';
    document.getElementById('progressBar').style.width   = '0%';
    document.getElementById('progressCounts').innerHTML  = '';

    // Show spinner
    const ico = document.getElementById('triggerIcon');
    if (ico) ico.outerHTML = '<span class="spinner" id="triggerIcon"></span>';
    if (lbl) lbl.textContent = 'Läuft…';

    try {
        const r = await fetch(`${API_BASE}/api/processing/trigger`, {
            method: 'POST',
        });
        if (r.status === 401) { showToast('Nicht autorisiert', 'error'); btn.disabled = false; section.style.display = 'none'; return; }
        const d = await r.json();
        if (d.success) {
            _isProcessing = true;
            showToast('Verarbeitung gestartet', 'info', 3000);
        } else {
            showToast(d.message || 'Konnte nicht starten', 'info', 3000);
            _isProcessing = (d.message || '').includes('in progress');
            if (!_isProcessing) { section.style.display = 'none'; btn.disabled = false; }
        }
    } catch {
        showToast('Verbindungsfehler', 'error');
        section.style.display = 'none';
        btn.disabled = false;
        const ico2 = document.getElementById('triggerIcon');
        if (ico2 && ico2.classList.contains('spinner')) {
            ico2.outerHTML = `<svg id="triggerIcon" width="14" height="14" fill="currentColor" viewBox="0 0 16 16">
                <path fill-rule="evenodd" d="M8 3a5 5 0 1 0 4.546 2.914.5.5 0 0 1 .908-.417A6 6 0 1 1 8 2v1z"/>
                <path d="M8 4.466V.534a.25.25 0 0 1 .41-.192l2.36 1.966c.12.1.12.284 0 .384L8.41 4.658A.25.25 0 0 1 8 4.466z"/>
            </svg>`;
        }
        if (lbl) lbl.textContent = 'Jetzt verarbeiten';
    }
}

// ── Daily report ───────────────────────────────────────────────────────
async function openDailyReport() {
    const modal = document.getElementById('reportModal');
    const body  = document.getElementById('reportModalBody');
    if (!modal || !body) return;
    modal.classList.add('active');
    body.innerHTML = '<div class="loading">Bericht wird erstellt…</div>';
    try {
        const r = await fetch(`${API_BASE}/api/reports/daily`);
        if (!r.ok) { body.innerHTML = '<div class="loading">Fehler beim Laden des Berichts</div>'; return; }
        const d = await r.json();
        body.innerHTML = `
            <div class="report-meta">
                <span>Erstellt: ${fmtDt(d.generated_at)}</span>
                <span>Zeitraum: letzte ${d.period_hours}h</span>
            </div>
            <div class="report-stats">
                <span>📧 ${d.total_processed} verarbeitet</span>
                <span>⚡ ${d.action_required} Aktion erforderlich</span>
                <span>🚫 ${d.spam_detected} Spam</span>
                <span>⏳ ${d.unresolved} ungelöst</span>
            </div>
            <pre class="report-text">${escHtml(d.report_text)}</pre>`;
    } catch {
        body.innerHTML = '<div class="loading">Fehler beim Laden des Berichts</div>';
    }
}

// ── Filters ────────────────────────────────────────────────────────────
function applyFilters() {
    const cat = document.getElementById('filterCategory').value;
    const pri = document.getElementById('filterPriority').value;
    const act = document.getElementById('filterAction').value;
    currentFilters = {
        category:        cat || null,
        priority:        pri || null,
        action_required: act === '' ? null : act === 'true',
    };
    loadEmails();
}

// ── Event wiring ────────────────────────────────────────────────────────
function wireListeners() {
    document.getElementById('triggerProcessing')?.addEventListener('click', triggerProcessing);
    document.getElementById('cancelRunBtn')?.addEventListener('click', cancelProcessing);
    document.getElementById('viewDailyReport')?.addEventListener('click', openDailyReport);
    document.getElementById('closeReportModal')?.addEventListener('click', () => document.getElementById('reportModal').classList.remove('active'));
    document.getElementById('applyFilters')?.addEventListener('click', applyFilters);
    document.getElementById('logoutBtn')?.addEventListener('click', handleLogout);
    document.getElementById('versionBadge')?.addEventListener('click', openVersionModal);
    document.getElementById('closeVersionModal')?.addEventListener('click', () => document.getElementById('versionModal').classList.remove('active'));
    document.getElementById('closeModal')?.addEventListener('click', () => document.getElementById('emailModal').classList.remove('active'));

    // Close modals on backdrop click
    ['versionModal', 'emailModal', 'reportModal'].forEach(id => {
        document.getElementById(id)?.addEventListener('click', e => {
            if (e.target.id === id) document.getElementById(id).classList.remove('active');
        });
    });
}

// ── Utilities ───────────────────────────────────────────────────────────
function escHtml(s) {
    if (s == null) return '';
    const d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
}

/** True when a processing run is currently in-flight (progress bar visible). */
function isActiveRun(status) {
    return status === 'running' || status === 'cancelling';
}

function fmtDate(s) {
    if (!s) return '–';
    return new Date(s).toLocaleDateString('de-DE', { year:'numeric', month:'2-digit', day:'2-digit' });
}

function fmtDt(s) {
    if (!s) return '–';
    return new Date(s).toLocaleString('de-DE', { year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit' });
}

function statusCls(s) {
    return ({
        // DB / backend uppercase
        SUCCESS: 'success', FAILURE: 'failure', PARTIAL: 'partial',
        CANCELLED: 'cancelled', IN_PROGRESS: 'running',
        // run_status lowercase
        success: 'success', failed: 'failure',
        running: 'running', cancelling: 'cancelling', cancelled: 'cancelled',
        idle: '',
    })[s] || '';
}

function statusLabel(s) {
    return ({
        // DB / backend uppercase
        SUCCESS: 'Erfolgreich', FAILURE: 'Fehler', PARTIAL: 'Teilweise',
        CANCELLED: 'Abgebrochen', IN_PROGRESS: 'Läuft',
        // run_status lowercase
        idle: 'Bereit', running: 'Läuft', cancelling: 'Wird abgebrochen…',
        cancelled: 'Abgebrochen', success: 'Erfolgreich', failed: 'Fehler',
    })[s] || s || '–';
}
