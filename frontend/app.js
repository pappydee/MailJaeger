// MailJaeger Dashboard — v1.1.0

const API_BASE = window.location.origin;

// ── State ──────────────────────────────────────────────────────────────
let _statusPollTimer = null;
let _isProcessing = false;
let _dashboardTimer = null;
let _actionQueueCache = [];
let _safeModeEnabled = null;
let _archiveFolder = '';
let _foldersCache = [];
const _reportQueueingKeys = new Set();
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
    await loadFolderSettings();
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

        _safeModeEnabled = Boolean(d.safe_mode);
        renderSafeModeState(_safeModeEnabled);
        if (typeof d.archive_folder === 'string' && d.archive_folder.trim()) {
            _archiveFolder = d.archive_folder.trim();
            renderArchiveFolderCurrent();
        }

        // Daily report button
        const reportBtn = document.getElementById('viewDailyReport');
        if (reportBtn) reportBtn.style.display = d.daily_report_available ? 'inline-flex' : 'none';
        await loadActionQueue();

    } catch (ex) {
        showToast('Dashboard konnte nicht geladen werden', 'error');
    }
}

async function loadFolderSettings() {
    await Promise.all([loadSettingsSnapshot(), loadFolders()]);
}

async function loadSettingsSnapshot() {
    try {
        const r = await fetch(`${API_BASE}/api/settings`);
        if (!r.ok) return;
        const body = await r.json();
        if (typeof body.archive_folder === 'string' && body.archive_folder.trim()) {
            _archiveFolder = body.archive_folder.trim();
        }
        renderArchiveFolderCurrent();
    } catch {}
}

function renderArchiveFolderCurrent() {
    const current = document.getElementById('archiveFolderCurrent');
    if (current) current.textContent = `Archivordner: ${_archiveFolder || '–'}`;
}

async function loadFolders() {
    const select = document.getElementById('archiveFolderSelect');
    if (select) {
        select.innerHTML = '<option value="">Ordner laden…</option>';
    }
    try {
        const r = await fetch(`${API_BASE}/api/folders`);
        if (!r.ok) {
            const body = await r.json().catch(() => ({}));
            throw new Error(body.detail || 'Ordner konnten nicht geladen werden');
        }
        const body = await r.json();
        _foldersCache = Array.isArray(body.folders) ? body.folders : [];
        if (typeof body.current_archive_folder === 'string' && body.current_archive_folder.trim()) {
            _archiveFolder = body.current_archive_folder.trim();
        }
        renderArchiveFolderCurrent();
        if (select) {
            const options = _foldersCache.map(folder => {
                const name = folder?.name || '';
                return `<option value="${escHtml(name)}">${escHtml(name)}</option>`;
            }).join('');
            select.innerHTML = `<option value="">Bitte wählen</option>${options}`;
            if (_archiveFolder) select.value = _archiveFolder;
        }
    } catch (error) {
        showToast(error?.message || 'Ordner konnten nicht geladen werden', 'error');
    }
}

async function saveArchiveFolderSelection() {
    const select = document.getElementById('archiveFolderSelect');
    if (!select) return;
    const selected = (select.value || '').trim();
    if (!selected) {
        showToast('Bitte zuerst einen Archivordner auswählen', 'info');
        return;
    }
    try {
        const r = await fetch(`${API_BASE}/api/settings`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ archive_folder: selected }),
        });
        const body = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(body.detail || 'Archivordner konnte nicht gespeichert werden');
        _archiveFolder = body.archive_folder || selected;
        renderArchiveFolderCurrent();
        showToast(`Archivordner gespeichert: ${_archiveFolder}`, 'success', 2800);
        await loadActionQueue();
    } catch (error) {
        showToast(error?.message || 'Archivordner konnte nicht gespeichert werden', 'error');
    }
}

function renderSafeModeState(isEnabled) {
    const smb = document.getElementById('safeModeBadge');
    if (smb) smb.style.display = isEnabled ? 'inline-block' : 'none';

    const safeModeToggle = document.getElementById('safeModeToggle');
    if (safeModeToggle) {
        safeModeToggle.textContent = isEnabled ? 'Safe Mode: AN' : 'Safe Mode: AUS';
        safeModeToggle.classList.toggle('btn-primary', isEnabled);
        safeModeToggle.classList.toggle('btn-secondary', !isEnabled);
        safeModeToggle.setAttribute('aria-pressed', isEnabled ? 'true' : 'false');
        safeModeToggle.title = isEnabled
            ? 'Safe Mode aktiv: automatische Ausführung deaktiviert'
            : 'Safe Mode aus: freigegebene Aktionen können automatisch ausgeführt werden';
    }

    const safeModeHint = document.getElementById('actionQueueSafeMode');
    if (safeModeHint) safeModeHint.style.display = isEnabled ? 'block' : 'none';
}

async function toggleSafeMode() {
    if (_safeModeEnabled === null) return;

    const btn = document.getElementById('safeModeToggle');
    if (btn) btn.disabled = true;

    const target = !_safeModeEnabled;
    try {
        const r = await fetch(`${API_BASE}/api/settings`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ safe_mode: target }),
        });
        const body = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(body.detail || 'Safe Mode konnte nicht aktualisiert werden');

        _safeModeEnabled = typeof body.safe_mode === 'boolean' ? body.safe_mode : target;
        renderSafeModeState(_safeModeEnabled);
        showToast(
            _safeModeEnabled
                ? 'Safe Mode aktiv: keine automatische Ausführung'
                : 'Safe Mode deaktiviert: automatische Ausführung freigegebener Aktionen aktiv',
            'info',
            3200
        );
        await loadActionQueue();
    } catch (e) {
        showToast(e?.message || 'Safe Mode konnte nicht umgeschaltet werden', 'error');
    } finally {
        if (btn) btn.disabled = false;
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
    body.innerHTML = `
        <div class="loading">
            Bericht wird erstellt…
            <div style="margin-top:10px;">
                <span class="spinner" style="border-color:rgba(79,70,229,.25);border-top-color:#4F46E5;"></span>
            </div>
        </div>`;
    try {
        const r = await fetch(`${API_BASE}/api/reports/daily`);
        if (!r.ok) {
            const detail = await r.json().catch(() => ({}));
            body.innerHTML = `
                <div class="report-empty-state report-error">
                    <strong>Bericht konnte nicht geladen werden</strong>
                    <p>${escHtml(detail.detail || 'Die Berichtserstellung ist fehlgeschlagen.')}</p>
                    <button class="btn btn-secondary" id="retryDailyReportBtn">Erneut versuchen</button>
                </div>`;
            document.getElementById('retryDailyReportBtn')?.addEventListener('click', openDailyReport);
            return;
        }
        const payload = await r.json();
        if (payload.status && payload.status !== 'ready') {
            const statusText = payload.status === 'running'
                ? 'Bericht wird aktuell im Hintergrund erstellt.'
                : 'Bericht wurde zur Erstellung eingeplant.';
            body.innerHTML = `
                <div class="report-empty-state">
                    <strong>${escHtml(statusText)}</strong>
                    <p>Bitte in wenigen Sekunden erneut versuchen.</p>
                    <button class="btn btn-secondary" id="retryDailyReportBtn">Aktualisieren</button>
                </div>`;
            document.getElementById('retryDailyReportBtn')?.addEventListener('click', openDailyReport);
            return;
        }
        const d = payload.report || payload;
        const suggestions = Array.isArray(d.suggested_actions) ? d.suggested_actions : [];
        const totals = d.totals || {};
        const totalProcessed = totals.total_processed ?? d.total_processed ?? 0;
        const actionRequired = totals.action_required ?? d.action_required ?? 0;
        const spamDetected = totals.spam_detected ?? d.spam_detected ?? 0;
        const unresolved = totals.unresolved ?? d.unresolved ?? 0;
        const safeModeActive = suggestions.some(a => a.safe_mode);
        if (!totalProcessed && !suggestions.length) {
            body.innerHTML = `
                <div class="report-empty-state">
                    <strong>Keine Report-Daten für die letzten ${escHtml(d.period_hours || 24)} Stunden.</strong>
                    <p>Sobald neue E-Mails verarbeitet wurden, erscheint hier ein strukturierter Tagesbericht.</p>
                </div>`;
            return;
        }
        body.innerHTML = `
            <div class="report-meta">
                <span>Erstellt: ${fmtDt(d.generated_at)}</span>
                <span>Zeitraum: letzte ${d.period_hours}h</span>
            </div>
            <div class="report-stats">
                <span>📧 ${totalProcessed} verarbeitet</span>
                <span>⚡ ${actionRequired} Aktion erforderlich</span>
                <span>🚫 ${spamDetected} Spam</span>
                <span>⏳ ${unresolved} ungelöst</span>
            </div>
            ${safeModeActive ? `
                <div class="report-safe-hint">
                    <strong>SAFE MODE aktiv</strong>
                    Vorschläge werden nicht automatisch ausgeführt. Aktionen landen in der Warteschlange und benötigen explizite Freigabe/Ausführung.
                    <div class="report-safe-hint-sub">Destruktive Aktionen wie Löschen sind deutlich markiert.</div>
                </div>` : ''}
            ${renderReportSection('Wichtige E-Mails', d.important_items, 'Keine wichtigen E-Mails im Zeitraum.')}
            ${renderReportSection('Aktion erforderlich', d.action_items, 'Keine offenen Aktionspunkte erkannt.')}
            ${renderReportSection('Ungelöste Elemente', d.unresolved_items, 'Keine ungelösten Elemente.')}
            ${renderReportSection('Spam / Bulk', d.spam_items, 'Keine Spam-/Bulk-Meldungen im Zeitraum.')}
            ${renderThreadGroups(Array.isArray(d.threads) ? d.threads : [])}
            ${renderSuggestedActions(suggestions)}
            <section class="report-section">
                <h3>Zusammenfassung</h3>
                <pre class="report-text">${escHtml(d.report_text || 'Keine KI-Zusammenfassung verfügbar.')}</pre>
            </section>`;
        body.querySelectorAll('.report-action-btn').forEach(btn => {
            btn.addEventListener('click', async () => {
                try {
                    const suggestion = JSON.parse(decodeURIComponent(btn.dataset.suggestion || '%7B%7D'));
                    await queueSuggestedAction(suggestion, btn);
                } catch {
                    showToast('Vorschlagsdaten sind ungültig', 'error');
                }
            });
        });
        body.querySelectorAll('.report-open-email-btn').forEach(btn => {
            btn.addEventListener('click', async () => {
                const emailId = Number(btn.dataset.emailId);
                const threadId = btn.dataset.threadId || null;
                if (!Number.isFinite(emailId) || emailId <= 0) return;
                await recordReportEvent({
                    event_type: 'open_related_email_from_report',
                    email_id: emailId,
                    thread_id: threadId,
                    source: 'report_suggestion',
                });
                document.getElementById('reportModal')?.classList.remove('active');
                openEmail(emailId);
            });
        });
        body.querySelectorAll('.report-preview-draft-btn').forEach(btn => {
            btn.addEventListener('click', async () => {
                try {
                    const suggestion = JSON.parse(decodeURIComponent(btn.dataset.suggestion || '%7B%7D'));
                    await previewReplyDraft(suggestion);
                } catch {
                    showToast('Entwurf konnte nicht geöffnet werden', 'error');
                }
            });
        });
    } catch {
        body.innerHTML = `
            <div class="report-empty-state report-error">
                <strong>Fehler beim Laden des Berichts</strong>
                <p>Bitte prüfen Sie die Verbindung und versuchen Sie es erneut.</p>
                <button class="btn btn-secondary" id="retryDailyReportBtn">Erneut versuchen</button>
            </div>`;
        document.getElementById('retryDailyReportBtn')?.addEventListener('click', openDailyReport);
    }
}

function renderReportSection(title, items = [], emptyMessage = 'Keine Einträge') {
    const list = (items || []).slice(0, 8);
    if (!list.length) {
        return `
            <section class="report-section">
                <h3>${escHtml(title)}</h3>
                <div class="report-empty-state">${escHtml(emptyMessage)}</div>
            </section>`;
    }
    return `
        <section class="report-section">
            <h3>${escHtml(title)}</h3>
            <div class="report-item-list">
                ${list.map(i => `
                    <article class="report-item-card">
                        <div class="report-item-head">
                            <strong>${escHtml(i.subject || '(kein Betreff)')}</strong>
                            <div class="report-item-badges">
                                ${i.priority ? `<span class="badge report-badge-priority">${escHtml(i.priority)}</span>` : ''}
                                ${i.category ? `<span class="badge report-badge-category">${escHtml(i.category)}</span>` : ''}
                                ${i.thread_id ? `<span class="badge report-badge-thread">Thread ${escHtml(i.thread_id)}</span>` : ''}
                            </div>
                        </div>
                        <div class="report-item-meta">
                            <span>Von: ${escHtml(i.sender || 'Unbekannt')}</span>
                            <span>E-Mail #${escHtml(i.email_id)}</span>
                        </div>
                        ${i.summary ? `<div class="report-item-summary">${escHtml(i.summary)}</div>` : ''}
                        <div class="report-item-actions">
                            <button class="btn btn-secondary report-open-email-btn" data-email-id="${escHtml(i.email_id)}" data-thread-id="${escHtml(i.thread_id || '')}">E-Mail öffnen</button>
                        </div>
                    </article>
                `).join('')}
            </div>
        </section>`;
}

function renderSuggestedActions(actions = []) {
    if (!actions.length) {
        return `
            <section class="report-section">
                <h3>Vorgeschlagene Aktionen</h3>
                <div class="report-empty-state">Keine Vorschläge für diesen Zeitraum.</div>
            </section>`;
    }
    return `
        <section class="report-section">
            <h3>Vorgeschlagene Aktionen</h3>
            <div class="report-suggested-actions">
                ${actions.map(action => `
                    <div class="report-action-card ${isDestructiveAction(action.action_type) ? 'report-action-card-danger' : ''}">
                        <div>
                            <strong>${escHtml(action.description || action.action_type)}</strong>
                            <div class="report-action-context">
                                <span>E-Mail #${escHtml(action.email_id)}</span>
                                ${action.thread_id ? `<span>· Thread ${escHtml(action.thread_id)}</span>` : ''}
                                ${action.thread_suggestion_count > 1 ? `<span>· ${escHtml(action.thread_suggestion_count)} Vorschläge im Thread</span>` : ''}
                            </div>
                            ${renderSuggestedActionPayloadPreview(action)}
                            ${action.queue_status ? `<div class="report-queue-state">Status: <span class="queue-status-badge ${escHtml(action.queue_status)}">${escHtml(statusLabel(action.queue_status))}</span>${action.queue_action_id ? ` #${escHtml(action.queue_action_id)}` : ''}${action.queue_error ? ` · ${escHtml(action.queue_error)}` : ''}</div>` : ''}
                            ${isDestructiveAction(action.action_type) ? '<div class="report-action-warning">Destruktive Aktion</div>' : ''}
                        </div>
                        <div class="report-action-controls">
                            ${action.action_type === 'reply_draft' ? `<button class="btn btn-secondary report-preview-draft-btn" data-suggestion='${encodeURIComponent(JSON.stringify(action))}'>Entwurf ansehen</button>` : ''}
                            <button class="btn btn-secondary report-action-btn" ${action.queue_status && action.queue_status !== 'failed' && action.queue_status !== 'rejected' ? 'disabled' : ''} data-suggestion='${encodeURIComponent(JSON.stringify(action))}'>
                                ${action.queue_status && action.queue_status !== 'failed' && action.queue_status !== 'rejected' ? `Bereits ${escHtml(statusLabel(action.queue_status))}` : 'In Warteschlange'}
                            </button>
                        </div>
                    </div>
                `).join('')}
            </div>
        </section>`;
}

function renderThreadGroups(threads = []) {
    if (!threads.length) return '';
    return `
        <section class="report-section">
            <h3>Threads</h3>
            <div class="report-thread-groups">
                ${threads.map(group => `
                    <details class="report-thread-group" ${group.priority === 'urgent' || group.priority === 'high' ? 'open' : ''}>
                        <summary>
                            <span><strong>${escHtml(group.key_topic || group.summary || group.thread_id)}</strong></span>
                            <span class="report-thread-badges">
                                <span class="badge report-thread-priority priority-${escHtml(group.priority || 'normal')}">${escHtml((group.priority || 'normal').toUpperCase())}</span>
                                <span class="thread-state-badge ${escHtml(normalizeThreadState(group.thread_state))}">${escHtml(threadStateLabel(group.thread_state))}</span>
                                <span class="badge report-thread-score">${escHtml(Math.round(group.importance_score || 0))}</span>
                            </span>
                        </summary>
                        ${group.summary ? `<div class="report-thread-summary">${escHtml(group.summary)}</div>` : ''}
                        <div class="report-item-list">
                            ${(group.emails || []).slice(0, 8).map(i => `
                                <article class="report-item-card">
                                    <div class="report-item-head">
                                        <strong>${escHtml(i.subject || '(kein Betreff)')}</strong>
                                        <div class="report-item-badges">
                                            ${i.priority ? `<span class="badge report-badge-priority">${escHtml(i.priority)}</span>` : ''}
                                            ${i.thread_priority ? `<span class="badge report-thread-priority priority-${escHtml(i.thread_priority)}">${escHtml((i.thread_priority || 'normal').toUpperCase())}</span>` : ''}
                                        </div>
                                    </div>
                                    <div class="report-item-meta">
                                        <span>Von: ${escHtml(i.sender || 'Unbekannt')}</span>
                                        <span>E-Mail #${escHtml(i.email_id)}</span>
                                    </div>
                                    ${i.summary ? `<div class="report-item-summary">${escHtml(i.summary)}</div>` : ''}
                                    <div class="report-item-actions">
                                        <button class="btn btn-secondary report-open-email-btn" data-email-id="${escHtml(i.email_id)}" data-thread-id="${escHtml(i.thread_id || '')}">E-Mail öffnen</button>
                                    </div>
                                </article>
                            `).join('')}
                        </div>
                    </details>
                `).join('')}
            </div>
        </section>`;
}

async function queueSuggestedAction(action, button) {
    if (!action || !action.email_id || !action.action_type) {
        showToast('Ungültiger Aktionsvorschlag', 'error');
        return;
    }
    const requestKey = `${action.email_id}:${action.thread_id || ''}:${action.action_type}:${stableStringify(action.payload || {})}`;
    if (_reportQueueingKeys.has(requestKey)) {
        showToast('Aktion wird bereits eingereiht…', 'info', 2500);
        return;
    }
    const originalLabel = button.textContent;
    button.disabled = true;
    button.textContent = 'Wird eingestellt…';
    _reportQueueingKeys.add(requestKey);
    try {
        const r = await fetch(`${API_BASE}/api/reports/daily/suggested-actions`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                email_id: action.email_id,
                thread_id: action.thread_id || null,
                action_type: action.action_type,
                payload: action.payload || null,
                safe_mode: !!action.safe_mode,
                description: action.description || null,
                source: 'report_suggestion',
            }),
        });
        if (!r.ok) {
            const d = await r.json().catch(() => ({}));
            if (r.status === 409) {
                button.textContent = 'Bereits in Warteschlange';
                showToast(d.detail || 'Aktion wurde bereits eingereiht', 'info', 3500);
                await loadActionQueue();
                return;
            }
            throw new Error(d.detail || 'Queue-Fehler');
        }
        const queued = await r.json();
        button.textContent = `In Warteschlange (#${queued.id})`;
        showToast('Vorschlag wurde zur Freigabe eingereiht', 'success', 3000);
        await loadActionQueue();
        await loadDashboard();
    } catch (e) {
        button.disabled = false;
        button.textContent = originalLabel;
        showToast(e?.message || 'Aktion konnte nicht eingereiht werden', 'error');
    } finally {
        _reportQueueingKeys.delete(requestKey);
    }
}

function renderSuggestedActionPayloadPreview(action) {
    const payload = action?.payload || {};
    if (action.action_type === 'reply_draft') {
        const summary = payload.draft_summary || 'Antwortentwurf';
        const preview = (payload.draft_text || '').slice(0, 140);
        return `
            <div class="report-draft-preview">
                <div><strong>${escHtml(summary)}</strong></div>
                ${preview ? `<div class="report-draft-snippet">${escHtml(preview)}${payload.draft_text && payload.draft_text.length > 140 ? '…' : ''}</div>` : ''}
            </div>`;
    }
    if (payload.target_folder) {
        return `<div class="report-action-context">Zielordner: ${escHtml(payload.target_folder)}</div>`;
    }
    return '';
}

async function previewReplyDraft(action) {
    const payload = action?.payload || {};
    const modal = document.getElementById('replyDraftModal');
    const body = document.getElementById('replyDraftModalBody');
    if (!modal || !body) return;
    body.innerHTML = `
        <div class="detail-section">
            <h3>${escHtml(payload.draft_summary || 'Antwortentwurf')}</h3>
            <div class="detail-value">
                <div class="report-action-context">E-Mail #${escHtml(action.email_id)}${action.thread_id ? ` · Thread ${escHtml(action.thread_id)}` : ''}</div>
                <pre class="reply-draft-full-text">${escHtml(payload.draft_text || '(Kein Entwurfstext vorhanden)')}</pre>
                <div class="report-safe-hint">Entwurf wird nur vorgeschlagen. Es erfolgt kein automatischer Versand.</div>
            </div>
        </div>`;
    modal.classList.add('active');
    await recordReportEvent({
        event_type: 'preview_reply_draft',
        email_id: action.email_id,
        thread_id: action.thread_id || null,
        source: 'report_suggestion',
    });
}

// ── Action queue ────────────────────────────────────────────────────────
async function loadActionQueue() {
    const list = document.getElementById('actionQueueList');
    if (!list) return;
    list.innerHTML = '<div class="loading">Lade Aktionen…</div>';
    try {
        const r = await fetch(`${API_BASE}/api/actions`);
        if (!r.ok) {
            const message = r.status === 401 ? 'Nicht autorisiert' : 'Fehler beim Laden';
            list.innerHTML = `<div class="report-empty-state report-error">${escHtml(message)}</div>`;
            return;
        }
        const actions = await r.json();
        _actionQueueCache = Array.isArray(actions) ? actions : [];
        renderActionQueue(_actionQueueCache);
    } catch {
        list.innerHTML = '<div class="report-empty-state report-error">Aktionen konnten nicht geladen werden</div>';
    }
}

function renderActionQueue(actions = []) {
    const list = document.getElementById('actionQueueList');
    const summary = document.getElementById('actionQueueSummary');
    if (!list) return;
    if (!actions.length) {
        if (summary) summary.textContent = 'Keine wartenden Aktionen';
        list.innerHTML = '<div class="report-empty-state">Keine Aktionen in der Warteschlange.</div>';
        return;
    }
    const counts = actions.reduce((acc, action) => {
        const key = normalizeQueueStatus(action.status);
        acc[key] = (acc[key] || 0) + 1;
        return acc;
    }, {});
    if (summary) {
        summary.textContent = `Proposed: ${counts.proposed || 0} · Approved: ${counts.approved || 0} · Executed: ${counts.executed || 0} · Failed: ${counts.failed || 0} · Rejected: ${counts.rejected || 0}`;
    }
    list.innerHTML = actions.slice(0, 20).map(action => {
        const status = normalizeQueueStatus(action.status);
        const payload = action.payload || {};
        const threadState = normalizeThreadState(action.thread_state);
        const threadPriority = normalizeThreadPriority(action.thread_priority);
        const threadScore = Math.round(action.thread_importance_score || 0);
        const threadSummary = action.thread_summary || {};
        const threadSummaryLine = buildThreadSummaryLine(threadSummary);
        const waitingLabel = threadState === 'waiting_for_me'
            ? 'You need to act'
            : threadState === 'waiting_for_other'
                ? 'Waiting for reply'
                : null;
        return `
            <article class="action-queue-card ${threadState === 'waiting_for_me' ? 'action-queue-card-waiting' : ''}">
                <div class="action-queue-head">
                    <div>
                        <strong>${escHtml(action.action_type)}</strong>
                        <div class="report-action-context">
                            E-Mail #${escHtml(action.email_id)}${action.thread_id ? ` · Thread ${escHtml(action.thread_id)}` : ''}
                        </div>
                        ${threadSummaryLine ? `<div class="action-thread-summary">${escHtml(threadSummaryLine)}</div>` : ''}
                        ${waitingLabel ? `<div class="action-thread-decision">${escHtml(waitingLabel)}</div>` : ''}
                    </div>
                    <div class="action-queue-badges">
                        <span class="queue-status-badge ${escHtml(status)}">${escHtml(statusLabel(status))}</span>
                        <span class="thread-state-badge ${escHtml(threadState)}">${escHtml(threadStateLabel(threadState))}</span>
                        <span class="badge report-thread-priority priority-${escHtml(threadPriority)}">${escHtml(threadPriority.toUpperCase())}</span>
                        <span class="badge report-thread-score">${escHtml(threadScore)}</span>
                    </div>
                </div>
                <div class="action-queue-meta">
                    <span>Erstellt: ${fmtDt(action.created_at)}</span>
                    ${action.thread_last_activity_at ? `<span>Letzte Aktivität: ${fmtDt(action.thread_last_activity_at)}</span>` : ''}
                    <span>Quelle: ${escHtml(action.source || payload.source_context || payload.source || 'unbekannt')}</span>
                </div>
                ${action.action_type === 'move' && payload.target_folder ? `<div class="report-action-context"><strong>Zielordner:</strong> ${escHtml(payload.target_folder)}</div>` : ''}
                ${payload.description ? `<div class="action-queue-description">${escHtml(payload.description)}</div>` : ''}
                ${action.action_type === 'reply_draft' ? `<div class="report-draft-snippet">${escHtml((payload.draft_text || '').slice(0, 180))}${payload.draft_text && payload.draft_text.length > 180 ? '…' : ''}</div>` : ''}
                ${action.error_message ? `<div class="action-queue-error">${escHtml(action.error_message)}</div>` : ''}
                <div class="action-queue-controls">
                    <button class="btn btn-secondary queue-approve-btn" data-id="${escHtml(action.id)}" ${status !== 'proposed' ? 'disabled' : ''}>Freigeben</button>
                    <button class="btn btn-secondary queue-reject-btn" data-id="${escHtml(action.id)}" ${status === 'executed' || status === 'rejected' ? 'disabled' : ''}>Ablehnen</button>
                    <button class="btn btn-primary queue-execute-btn" data-id="${escHtml(action.id)}" ${status !== 'approved' ? 'disabled' : ''}>Ausführen</button>
                    ${action.action_type === 'reply_draft' ? `<button class="btn btn-secondary queue-preview-btn" data-id="${escHtml(action.id)}">Entwurf</button>` : ''}
                </div>
            </article>`;
    }).join('');

    list.querySelectorAll('.queue-approve-btn').forEach(btn => btn.addEventListener('click', () => mutateQueueAction(btn.dataset.id, 'approve')));
    list.querySelectorAll('.queue-reject-btn').forEach(btn => btn.addEventListener('click', () => mutateQueueAction(btn.dataset.id, 'reject')));
    list.querySelectorAll('.queue-execute-btn').forEach(btn => btn.addEventListener('click', () => mutateQueueAction(btn.dataset.id, 'execute')));
    list.querySelectorAll('.queue-preview-btn').forEach(btn => btn.addEventListener('click', () => {
        const action = _actionQueueCache.find(i => String(i.id) === String(btn.dataset.id));
        if (action) previewReplyDraft(action);
    }));
}

async function mutateQueueAction(actionId, transition) {
    if (!actionId || !transition) return;
    const endpoint = `${API_BASE}/api/actions/${actionId}/${transition}?source=queue_ui`;
    try {
        const r = await fetch(endpoint, { method: 'POST' });
        const body = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(body.detail || `Aktion ${transition} fehlgeschlagen`);
        showToast(`Aktion ${transition} erfolgreich`, 'success', 2200);
        await loadActionQueue();
    } catch (e) {
        showToast(e?.message || 'Queue-Übergang fehlgeschlagen', 'error');
        await loadActionQueue();
    }
}

async function recordReportEvent(payload) {
    if (!payload || !payload.email_id || !payload.event_type) return;
    try {
        await fetch(`${API_BASE}/api/reports/daily/events`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
    } catch (error) {
        console.debug('report event telemetry failed', error);
        // non-blocking telemetry hook
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
    document.getElementById('safeModeToggle')?.addEventListener('click', toggleSafeMode);
    document.getElementById('triggerProcessing')?.addEventListener('click', triggerProcessing);
    document.getElementById('cancelRunBtn')?.addEventListener('click', cancelProcessing);
    document.getElementById('viewDailyReport')?.addEventListener('click', openDailyReport);
    document.getElementById('closeReportModal')?.addEventListener('click', () => document.getElementById('reportModal').classList.remove('active'));
    document.getElementById('refreshActionQueue')?.addEventListener('click', loadActionQueue);
    document.getElementById('refreshFoldersBtn')?.addEventListener('click', loadFolders);
    document.getElementById('saveArchiveFolderBtn')?.addEventListener('click', saveArchiveFolderSelection);
    document.getElementById('applyFilters')?.addEventListener('click', applyFilters);
    document.getElementById('logoutBtn')?.addEventListener('click', handleLogout);
    document.getElementById('versionBadge')?.addEventListener('click', openVersionModal);
    document.getElementById('closeVersionModal')?.addEventListener('click', () => document.getElementById('versionModal').classList.remove('active'));
    document.getElementById('closeModal')?.addEventListener('click', () => document.getElementById('emailModal').classList.remove('active'));
    document.getElementById('closeReplyDraftModal')?.addEventListener('click', () => document.getElementById('replyDraftModal').classList.remove('active'));

    // Close modals on backdrop click
    ['versionModal', 'emailModal', 'reportModal', 'replyDraftModal'].forEach(id => {
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
        proposed: 'partial', approved: 'running', executed: 'success', rejected: 'failure',
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
        proposed: 'Vorgeschlagen', approved: 'Freigegeben', executed: 'Ausgeführt', rejected: 'Abgelehnt',
    })[s] || s || '–';
}

const VALID_THREAD_STATES = ['open', 'waiting_for_me', 'waiting_for_other', 'in_conversation', 'resolved', 'informational', 'auto_generated'];
const VALID_THREAD_PRIORITIES = ['urgent', 'high', 'normal', 'low'];

function normalizeQueueStatus(status) {
    const normalized = (status || '').toLowerCase();
    if (normalized === 'proposed_action') return 'proposed';
    if (normalized === 'approved_action') return 'approved';
    if (normalized === 'executed_action') return 'executed';
    if (normalized === 'failed_action') return 'failed';
    if (normalized === 'rejected_action') return 'rejected';
    return normalized || 'proposed';
}

function normalizeThreadState(state) {
    const normalized = (state || '').toLowerCase();
    if (VALID_THREAD_STATES.includes(normalized)) {
        return normalized;
    }
    return 'informational';
}

function threadStateLabel(state) {
    return ({
        open: 'Offen',
        waiting_for_me: 'Warte auf mich',
        waiting_for_other: 'Warte auf andere',
        in_conversation: 'Im Gespräch',
        resolved: 'Erledigt',
        informational: 'Info',
        auto_generated: 'Automatisch',
    })[normalizeThreadState(state)] || 'Info';
}

function normalizeThreadPriority(priority) {
    const normalized = (priority || '').toLowerCase();
    return VALID_THREAD_PRIORITIES.includes(normalized) ? normalized : 'normal';
}

function buildThreadSummaryLine(summary) {
    if (!summary || typeof summary !== 'object') return '';
    const subject = (summary.latest_subject || '').trim();
    const sender = (summary.last_sender || '').trim();
    const text = (summary.summary || '').trim();
    if (text) return text;
    if (subject && sender) return `${subject} · ${sender}`;
    return subject || sender || '';
}

function isDestructiveAction(actionType) {
    return actionType === 'delete' || actionType === 'mark_spam';
}

function stableStringify(value) {
    if (Array.isArray(value)) return `[${value.map(stableStringify).join(',')}]`;
    if (value !== null && typeof value === 'object') {
        return `{${Object.keys(value).sort().map(k => `${JSON.stringify(k)}:${stableStringify(value[k])}`).join(',')}}`;
    }
    return JSON.stringify(value);
}
