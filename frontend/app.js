// MailJaeger Dashboard JavaScript

const API_BASE = window.location.origin;

// State
let allEmails = [];
let currentFilters = {
    category: '',
    priority: '',
    action_required: ''
};
let _statusPollInterval = null;
let _dashboardPollInterval = null;

// Initialize dashboard
document.addEventListener('DOMContentLoaded', async () => {
    // Check authentication via /api/auth/verify (uses session cookie automatically)
    const authenticated = await checkAuthenticated();

    if (!authenticated) {
        showLoginModal();
        return;
    }

    await initDashboard();
});

// Check if already authenticated (session cookie)
async function checkAuthenticated() {
    try {
        const response = await fetch(`${API_BASE}/api/auth/verify`);
        return response.ok;
    } catch (error) {
        console.error('Auth check failed:', error);
        return false;
    }
}

// Show login modal
function showLoginModal() {
    const modal = document.getElementById('loginModal');
    modal.style.display = 'flex';
    modal.style.alignItems = 'center';
    modal.style.justifyContent = 'center';
    modal.style.position = 'fixed';
    modal.style.inset = '0';
    modal.style.background = 'linear-gradient(135deg,#667eea 0%,#764ba2 100%)';
    modal.style.zIndex = '1000';

    document.getElementById('loginForm').addEventListener('submit', handleLogin);
    document.getElementById('apiKeyInput').focus();
}

// Handle login form submit
async function handleLogin(e) {
    e.preventDefault();
    const keyInput = document.getElementById('apiKeyInput');
    const errorDiv = document.getElementById('loginError');
    const key = keyInput.value.trim();

    errorDiv.style.display = 'none';

    if (!key) {
        errorDiv.textContent = 'Please enter an API key';
        errorDiv.style.display = 'block';
        return;
    }

    try {
        const response = await fetch(`${API_BASE}/api/auth/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ api_key: key }),
        });

        if (response.ok) {
            // Cookie is now set by the server; reload to enter authenticated state
            location.reload();
        } else {
            const data = await response.json().catch(() => ({}));
            errorDiv.textContent = data.detail || 'Invalid API key. Please try again.';
            errorDiv.style.display = 'block';
            keyInput.value = '';
            keyInput.focus();
        }
    } catch (error) {
        errorDiv.textContent = 'Connection error. Please try again.';
        errorDiv.style.display = 'block';
    }
}

// Handle authentication errors from API calls
function handleAuthError(response) {
    if (response.status === 401) {
        showError('Session expired. Bitte neu einloggen…');
        setTimeout(() => location.reload(), 2000);
        return true;
    }
    return false;
}

// Initialize the main dashboard after login
async function initDashboard() {
    await loadVersion();
    await loadDashboard();
    await loadEmails();
    setupEventListeners();
    startStatusPolling();

    // Refresh dashboard every 30 seconds
    _dashboardPollInterval = setInterval(loadDashboard, 30000);
}

// Setup event listeners
function setupEventListeners() {
    document.getElementById('triggerProcessing').addEventListener('click', triggerProcessing);
    document.getElementById('applyFilters').addEventListener('click', applyFilters);
    document.getElementById('closeModal').addEventListener('click', closeModal);
    document.getElementById('logoutBtn').addEventListener('click', handleLogout);

    // Version badge → open version history modal
    document.getElementById('versionBadge').addEventListener('click', openVersionModal);
    document.getElementById('closeVersionModal').addEventListener('click', closeVersionModal);

    // Close modals on background click
    document.getElementById('emailModal').addEventListener('click', (e) => {
        if (e.target.id === 'emailModal') closeModal();
    });
    document.getElementById('versionModal').addEventListener('click', (e) => {
        if (e.target.id === 'versionModal') closeVersionModal();
    });
}

// Logout
async function handleLogout() {
    try {
        await fetch(`${API_BASE}/api/auth/logout`, { method: 'POST' });
    } catch (_) { /* ignore */ }
    location.reload();
}

// Load version and show badge
async function loadVersion() {
    try {
        const response = await fetch(`${API_BASE}/api/version`);
        if (!response.ok) return;
        const data = await response.json();
        const badge = document.getElementById('versionBadge');
        if (badge) badge.textContent = `v${data.version}`;
        // Store changelog for modal
        window._changelog = data.changelog || [];
    } catch (error) {
        console.error('Version load failed:', error);
    }
}

// Open version history modal
function openVersionModal() {
    const modal = document.getElementById('versionModal');
    const body = document.getElementById('versionModalBody');
    modal.classList.add('active');

    const changelog = window._changelog || [];
    if (changelog.length === 0) {
        body.innerHTML = '<p style="color:#718096;">No version history available.</p>';
        return;
    }

    body.innerHTML = changelog.map(entry => `
        <div style="margin-bottom:20px;padding-bottom:16px;border-bottom:1px solid #e2e8f0;">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                <strong style="font-size:16px;color:#2d3748;">v${entry.version}</strong>
                <span style="font-size:13px;color:#718096;">${entry.date || ''}</span>
            </div>
            <ul style="margin:0;padding-left:20px;">
                ${(entry.changes || []).map(c => `<li style="font-size:14px;color:#4a5568;margin-bottom:4px;">${escapeHtml(c)}</li>`).join('')}
            </ul>
        </div>
    `).join('');
}

function closeVersionModal() {
    document.getElementById('versionModal').classList.remove('active');
}

// ─── Status / Progress polling ────────────────────────────────────────────────

function startStatusPolling() {
    updateStatus();
    _statusPollInterval = setInterval(updateStatus, 2000);
}

async function updateStatus() {
    try {
        const response = await fetch(`${API_BASE}/api/status`);
        if (!response.ok) return;
        const data = await response.json();
        renderStatus(data);
    } catch (error) {
        console.error('Status poll error:', error);
    }
}

function renderStatus(data) {
    const section = document.getElementById('progressSection');
    const label = document.getElementById('progressLabel');
    const percent = document.getElementById('progressPercent');
    const bar = document.getElementById('progressBar');

    if (data.status === 'running') {
        section.style.display = 'block';
        label.textContent = data.current_step || 'Verarbeitung läuft…';
        const pct = data.progress_percent || 0;
        percent.textContent = `${pct}%`;
        bar.style.width = `${pct}%`;
    } else {
        section.style.display = 'none';
    }
}

// ─── Dashboard ────────────────────────────────────────────────────────────────

async function loadDashboard() {
    try {
        const response = await fetch(`${API_BASE}/api/dashboard`);

        if (handleAuthError(response)) return;

        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        const data = await response.json();

        updateDashboardStats(data);
        updateLastRunInfo(data);
        updateHealthStatus(data.health_status);
    } catch (error) {
        console.error('Error loading dashboard:', error);
        showError('Dashboard konnte nicht geladen werden');
    }
}

function updateDashboardStats(data) {
    document.getElementById('totalEmails').textContent = data.total_emails || 0;
    document.getElementById('actionRequired').textContent = data.action_required_count || 0;
    document.getElementById('unresolvedCount').textContent = data.unresolved_count || 0;

    const spamCount = data.last_run?.emails_spam || 0;
    document.getElementById('spamFiltered').textContent = spamCount;
}

function updateLastRunInfo(data) {
    const lastRun = data.last_run;

    if (lastRun) {
        const startTime = new Date(lastRun.started_at);
        document.getElementById('lastRunTime').textContent = formatDateTime(startTime);

        const statusBadge = document.getElementById('lastRunStatus');
        statusBadge.textContent = lastRun.status;
        statusBadge.className = 'info-badge ' + getStatusClass(lastRun.status);

        document.getElementById('lastRunProcessed').textContent =
            `${lastRun.emails_processed} Emails (${lastRun.emails_failed} Fehler)`;
    } else {
        document.getElementById('lastRunTime').textContent = 'Noch keine Verarbeitung';
        document.getElementById('lastRunStatus').textContent = '-';
        document.getElementById('lastRunProcessed').textContent = '-';
    }

    if (data.next_scheduled_run) {
        const nextRun = new Date(data.next_scheduled_run);
        document.getElementById('nextRunTime').textContent = formatDateTime(nextRun);
    } else {
        document.getElementById('nextRunTime').textContent = 'Nicht geplant';
    }
}

function updateHealthStatus(healthStatus) {
    const statusDot = document.querySelector('.status-dot');
    const statusText = document.querySelector('.status-text');

    if (!healthStatus) {
        statusText.textContent = 'Status unbekannt';
        return;
    }

    const allHealthy =
        healthStatus.mail_server?.status === 'healthy' &&
        healthStatus.ai_service?.status === 'healthy';

    if (allHealthy) {
        statusDot.className = 'status-dot healthy';
        statusText.textContent = 'System läuft';
    } else {
        statusDot.className = 'status-dot unhealthy';
        statusText.textContent = 'Systemprobleme';
    }
}

// ─── Email list ───────────────────────────────────────────────────────────────

async function loadEmails() {
    const emailList = document.getElementById('emailList');
    emailList.innerHTML = '<div class="loading">Lade Emails...</div>';

    try {
        const response = await fetch(`${API_BASE}/api/emails/list`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                page: 1,
                page_size: 50,
                sort_by: 'date',
                sort_order: 'desc',
                ...currentFilters
            })
        });

        if (handleAuthError(response)) return;

        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        allEmails = await response.json();
        renderEmailList(allEmails);
    } catch (error) {
        console.error('Error loading emails:', error);
        emailList.innerHTML = '<div class="loading">Fehler beim Laden der Emails</div>';
    }
}

function renderEmailList(emails) {
    const emailList = document.getElementById('emailList');

    if (!Array.isArray(emails)) {
        console.error('Expected array but got:', typeof emails, emails);
        emailList.innerHTML = '<div class="loading">Fehler beim Laden der Emails</div>';
        return;
    }

    if (emails.length === 0) {
        emailList.innerHTML = '<div class="loading">Keine Emails gefunden</div>';
        return;
    }

    emailList.innerHTML = emails.map(email => `
        <div class="email-item" onclick="showEmailDetail(${email.id})">
            <div class="email-priority ${email.priority || 'LOW'}"></div>
            <div class="email-content">
                <div class="email-header">
                    <div>
                        <div class="email-subject">${escapeHtml(email.subject || 'Kein Betreff')}</div>
                        <div class="email-sender">${escapeHtml(email.sender || 'Unbekannt')}</div>
                    </div>
                    <div class="email-badges">
                        ${email.category ? `<span class="badge category">${email.category}</span>` : ''}
                        ${email.action_required ? '<span class="badge action">Aktion erforderlich</span>' : ''}
                    </div>
                </div>
                ${email.summary ? `<div class="email-summary">${escapeHtml(email.summary)}</div>` : ''}
                <div class="email-meta">
                    <span>📅 ${formatDate(email.date)}</span>
                    <span>⚡ ${email.priority || 'LOW'}</span>
                    ${email.tasks?.length > 0 ? `<span>✓ ${email.tasks.length} Aufgaben</span>` : ''}
                </div>
            </div>
        </div>
    `).join('');
}

async function showEmailDetail(emailId) {
    const modal = document.getElementById('emailModal');
    const modalBody = document.getElementById('modalBody');

    modal.classList.add('active');
    modalBody.innerHTML = '<div class="loading">Lade Details...</div>';

    try {
        const response = await fetch(`${API_BASE}/api/emails/${emailId}`);

        if (handleAuthError(response)) return;

        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        const email = await response.json();

        document.getElementById('modalSubject').textContent = email.subject || 'Kein Betreff';

        modalBody.innerHTML = `
            <div class="detail-section">
                <h3>Email-Informationen</h3>
                <div class="detail-grid">
                    <div class="detail-item">
                        <div class="detail-label">Von</div>
                        <div class="detail-value">${escapeHtml(email.sender || '-')}</div>
                    </div>
                    <div class="detail-item">
                        <div class="detail-label">Datum</div>
                        <div class="detail-value">${formatDateTime(email.date)}</div>
                    </div>
                    <div class="detail-item">
                        <div class="detail-label">Kategorie</div>
                        <div class="detail-value">${email.category || '-'}</div>
                    </div>
                    <div class="detail-item">
                        <div class="detail-label">Priorität</div>
                        <div class="detail-value">${email.priority || '-'}</div>
                    </div>
                    <div class="detail-item">
                        <div class="detail-label">Spam-Wahrscheinlichkeit</div>
                        <div class="detail-value">${email.spam_probability ? (email.spam_probability * 100).toFixed(1) + '%' : '-'}</div>
                    </div>
                    <div class="detail-item">
                        <div class="detail-label">Status</div>
                        <div class="detail-value">
                            ${email.is_resolved ? '✓ Bearbeitet' : '○ Unbearbeitet'}
                        </div>
                    </div>
                </div>
            </div>

            ${email.summary ? `
                <div class="detail-section">
                    <h3>Zusammenfassung</h3>
                    <div class="detail-value">${escapeHtml(email.summary)}</div>
                </div>
            ` : ''}

            ${email.reasoning ? `
                <div class="detail-section">
                    <h3>KI-Analyse</h3>
                    <div class="detail-value">${escapeHtml(email.reasoning)}</div>
                </div>
            ` : ''}

            ${email.tasks && email.tasks.length > 0 ? `
                <div class="detail-section">
                    <h3>Aufgaben (${email.tasks.length})</h3>
                    <div class="task-list">
                        ${email.tasks.map(task => `
                            <div class="task-item">
                                <div class="task-description">${escapeHtml(task.description)}</div>
                                <div class="task-meta">
                                    ${task.due_date ? `<span>📅 Fällig: ${formatDate(task.due_date)}</span>` : ''}
                                    ${task.confidence ? `<span>🎯 Sicherheit: ${(task.confidence * 100).toFixed(0)}%</span>` : ''}
                                </div>
                            </div>
                        `).join('')}
                    </div>
                </div>
            ` : ''}

            ${email.suggested_folder ? `
                <div class="detail-section">
                    <h3>Vorgeschlagener Ordner</h3>
                    <div class="detail-value">📁 ${escapeHtml(email.suggested_folder)}</div>
                </div>
            ` : ''}

            ${!email.is_resolved ? `
                <div class="detail-section">
                    <button class="btn btn-primary" onclick="markAsResolved(${email.id})">
                        Als bearbeitet markieren
                    </button>
                </div>
            ` : ''}
        `;
    } catch (error) {
        console.error('Error loading email detail:', error);
        modalBody.innerHTML = '<div class="loading">Fehler beim Laden der Details</div>';
    }
}

function closeModal() {
    document.getElementById('emailModal').classList.remove('active');
}

async function markAsResolved(emailId) {
    try {
        const response = await fetch(`${API_BASE}/api/emails/${emailId}/resolve`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email_id: emailId, resolved: true })
        });

        if (handleAuthError(response)) return;

        if (response.ok) {
            closeModal();
            await loadDashboard();
            await loadEmails();
            showSuccess('Email als bearbeitet markiert');
        }
    } catch (error) {
        console.error('Error marking as resolved:', error);
        showError('Fehler beim Markieren');
    }
}

// ─── Trigger processing ───────────────────────────────────────────────────────

async function triggerProcessing() {
    const button = document.getElementById('triggerProcessing');
    button.disabled = true;
    button.textContent = 'Verarbeitung läuft...';

    // Show progress bar immediately
    document.getElementById('progressSection').style.display = 'block';
    document.getElementById('progressLabel').textContent = 'Starte Verarbeitung…';
    document.getElementById('progressPercent').textContent = '0%';
    document.getElementById('progressBar').style.width = '0%';

    try {
        const response = await fetch(`${API_BASE}/api/processing/trigger`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ trigger_type: 'MANUAL' })
        });

        if (handleAuthError(response)) {
            button.disabled = false;
            button.textContent = 'Jetzt verarbeiten';
            return;
        }

        const result = await response.json();

        if (result.success) {
            showSuccess('Verarbeitung gestartet');
            // Dashboard refresh after processing (status polling handles progress)
            setTimeout(async () => {
                await loadDashboard();
                await loadEmails();
            }, 5000);
        } else {
            showError(result.message || 'Verarbeitung konnte nicht gestartet werden');
            document.getElementById('progressSection').style.display = 'none';
        }
    } catch (error) {
        console.error('Error triggering processing:', error);
        showError('Fehler beim Starten der Verarbeitung');
        document.getElementById('progressSection').style.display = 'none';
    } finally {
        button.disabled = false;
        button.innerHTML = `
            <svg width="16" height="16" fill="currentColor" viewBox="0 0 16 16">
                <path fill-rule="evenodd" d="M8 3a5 5 0 1 0 4.546 2.914.5.5 0 0 1 .908-.417A6 6 0 1 1 8 2v1z"/>
                <path d="M8 4.466V.534a.25.25 0 0 1 .41-.192l2.36 1.966c.12.1.12.284 0 .384L8.41 4.658A.25.25 0 0 1 8 4.466z"/>
            </svg>
            Jetzt verarbeiten
        `;
    }
}

// ─── Filters ──────────────────────────────────────────────────────────────────

function applyFilters() {
    currentFilters = {
        category: document.getElementById('filterCategory').value || null,
        priority: document.getElementById('filterPriority').value || null,
        action_required: document.getElementById('filterAction').value ?
            document.getElementById('filterAction').value === 'true' : null
    };

    // Remove null values
    Object.keys(currentFilters).forEach(key =>
        currentFilters[key] === null && delete currentFilters[key]
    );

    loadEmails();
}

// ─── Utilities ────────────────────────────────────────────────────────────────

function formatDate(dateString) {
    if (!dateString) return '-';
    const date = new Date(dateString);
    return date.toLocaleDateString('de-DE', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit'
    });
}

function formatDateTime(dateString) {
    if (!dateString) return '-';
    const date = new Date(dateString);
    return date.toLocaleString('de-DE', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit'
    });
}

function getStatusClass(status) {
    const statusMap = {
        'SUCCESS': 'success',
        'FAILURE': 'failure',
        'PARTIAL': 'partial'
    };
    return statusMap[status] || '';
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function showSuccess(message) {
    console.log('Success:', message);
}

function showError(message) {
    console.error('Error:', message);
    alert(message);
}
