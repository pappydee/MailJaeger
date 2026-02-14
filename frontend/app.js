// MailJaeger Dashboard JavaScript

const API_BASE = window.location.origin;

// State
let allEmails = [];
let currentFilters = {
    category: '',
    priority: '',
    action_required: ''
};
let apiKey = '';  // Keep in memory only

// Initialize dashboard
document.addEventListener('DOMContentLoaded', async () => {
    // Check for API key in sessionStorage (session-only, not persistent)
    apiKey = sessionStorage.getItem('mailjaeger_api_key') || '';
    
    // Check if authentication is required
    const authRequired = await checkAuthRequired();
    
    if (authRequired && !apiKey) {
        showLoginUI();
        return;
    }
    
    await loadDashboard();
    await loadEmails();
    setupEventListeners();
    
    // Refresh dashboard every 30 seconds
    setInterval(loadDashboard, 30000);
});

// Check if authentication is required
async function checkAuthRequired() {
    try {
        // Health endpoint is unauthenticated for monitoring
        const response = await fetch(`${API_BASE}/api/health`);
        
        // Try to access root - if 401, auth is required
        const rootResponse = await fetch(`${API_BASE}/`);
        return rootResponse.status === 401;
    } catch (error) {
        console.error('Error checking auth:', error);
        return true; // Assume auth required on error
    }
}

// Show login UI
function showLoginUI() {
    document.body.innerHTML = `
        <div style="display: flex; justify-content: center; align-items: center; min-height: 100vh; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);">
            <div style="background: white; padding: 40px; border-radius: 12px; box-shadow: 0 20px 60px rgba(0,0,0,0.3); max-width: 400px; width: 90%;">
                <div style="text-align: center; margin-bottom: 30px;">
                    <svg width="60" height="60" viewBox="0 0 40 40" fill="none" xmlns="http://www.w3.org/2000/svg" style="margin: 0 auto;">
                        <rect width="40" height="40" rx="8" fill="#4F46E5"/>
                        <path d="M12 14L20 22L28 14" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                        <path d="M12 26H28" stroke="white" stroke-width="2" stroke-linecap="round"/>
                    </svg>
                    <h1 style="margin: 20px 0 10px; color: #1a202c; font-size: 28px;">MailJaeger</h1>
                    <p style="color: #718096; margin: 0;">Secure AI Email Processing</p>
                </div>
                <form id="loginForm" style="margin-top: 20px;">
                    <div style="margin-bottom: 20px;">
                        <label style="display: block; margin-bottom: 8px; color: #2d3748; font-weight: 500;">API Key</label>
                        <input type="password" id="apiKeyInput" 
                            placeholder="Enter your API key" 
                            style="width: 100%; padding: 12px; border: 2px solid #e2e8f0; border-radius: 8px; font-size: 14px; box-sizing: border-box;"
                            required
                            autocomplete="off"
                        />
                        <p style="margin-top: 8px; font-size: 12px; color: #718096;">
                            🔒 Stored for this session only
                        </p>
                    </div>
                    <button type="submit" 
                        style="width: 100%; padding: 12px; background: #4F46E5; color: white; border: none; border-radius: 8px; font-size: 16px; font-weight: 600; cursor: pointer; transition: background 0.2s;"
                        onmouseover="this.style.background='#4338ca'"
                        onmouseout="this.style.background='#4F46E5'"
                    >
                        Sign In
                    </button>
                </form>
                <div id="loginError" style="margin-top: 15px; padding: 12px; background: #fed7d7; color: #c53030; border-radius: 8px; display: none; font-size: 14px;"></div>
            </div>
        </div>
    `;
    
    document.getElementById('loginForm').addEventListener('submit', handleLogin);
}

// Handle login
async function handleLogin(e) {
    e.preventDefault();
    const keyInput = document.getElementById('apiKeyInput');
    const errorDiv = document.getElementById('loginError');
    const key = keyInput.value.trim();
    
    if (!key) {
        errorDiv.textContent = 'Please enter an API key';
        errorDiv.style.display = 'block';
        return;
    }
    
    // Test the key by trying to access dashboard
    apiKey = key;
    try {
        const response = await fetch(`${API_BASE}/api/dashboard`, {
            headers: getAuthHeaders()
        });
        
        if (response.ok) {
            // Store in sessionStorage (cleared when tab/browser closes)
            sessionStorage.setItem('mailjaeger_api_key', key);
            location.reload();
        } else {
            apiKey = '';
            errorDiv.textContent = 'Invalid API key. Please try again.';
            errorDiv.style.display = 'block';
            keyInput.value = '';
            keyInput.focus();
        }
    } catch (error) {
        apiKey = '';
        errorDiv.textContent = 'Connection error. Please try again.';
        errorDiv.style.display = 'block';
    }
}

// Get headers with authentication
function getAuthHeaders() {
    const headers = {
        'Content-Type': 'application/json'
    };
    
    if (apiKey) {
        headers['Authorization'] = `Bearer ${apiKey}`;
    }
    
    return headers;
}

// Handle authentication errors
function handleAuthError(response) {
    if (response.status === 401) {
        sessionStorage.removeItem('mailjaeger_api_key');
        apiKey = '';
        showError('Session expired. Reloading...');
        setTimeout(() => location.reload(), 2000);
        return true;
    }
    return false;
}

// Setup event listeners
function setupEventListeners() {
    document.getElementById('triggerProcessing').addEventListener('click', triggerProcessing);
    document.getElementById('applyFilters').addEventListener('click', applyFilters);
    document.getElementById('closeModal').addEventListener('click', closeModal);
    
    // Close modal on background click
    document.getElementById('emailModal').addEventListener('click', (e) => {
        if (e.target.id === 'emailModal') {
            closeModal();
        }
    });
}

// Load dashboard data
async function loadDashboard() {
    try {
        const response = await fetch(`${API_BASE}/api/dashboard`, {
            headers: getAuthHeaders()
        });
        
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

// Update dashboard statistics
function updateDashboardStats(data) {
    document.getElementById('totalEmails').textContent = data.total_emails || 0;
    document.getElementById('actionRequired').textContent = data.action_required_count || 0;
    document.getElementById('unresolvedCount').textContent = data.unresolved_count || 0;
    
    // Calculate spam from last run if available
    const spamCount = data.last_run?.emails_spam || 0;
    document.getElementById('spamFiltered').textContent = spamCount;
}

// Update last run information
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
    
    // Next scheduled run
    if (data.next_scheduled_run) {
        const nextRun = new Date(data.next_scheduled_run);
        document.getElementById('nextRunTime').textContent = formatDateTime(nextRun);
    } else {
        document.getElementById('nextRunTime').textContent = 'Nicht geplant';
    }
}

// Update health status
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

// Load emails
async function loadEmails() {
    const emailList = document.getElementById('emailList');
    emailList.innerHTML = '<div class="loading">Lade Emails...</div>';
    
    try {
        const response = await fetch(`${API_BASE}/api/emails/list`, {
            method: 'POST',
            headers: getAuthHeaders(),
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

// Render email list
function renderEmailList(emails) {
    const emailList = document.getElementById('emailList');
    
    // Ensure emails is an array
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

// Show email detail
async function showEmailDetail(emailId) {
    const modal = document.getElementById('emailModal');
    const modalBody = document.getElementById('modalBody');
    
    modal.classList.add('active');
    modalBody.innerHTML = '<div class="loading">Lade Details...</div>';
    
    try {
        const response = await fetch(`${API_BASE}/api/emails/${emailId}`, {
            headers: getAuthHeaders()
        });
        
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
            
            <div class="detail-section proposed-actions" id="proposedActionsSection">
                <h3>Proposed Mailbox Actions</h3>
                <div id="proposedActionsList">
                    <div class="loading" style="font-size: 13px;">Lade Actions...</div>
                </div>
            </div>
            
            ${!email.is_resolved ? `
                <div class="detail-section">
                    <button class="btn btn-primary" onclick="markAsResolved(${email.id})">
                        Als bearbeitet markieren
                    </button>
                </div>
            ` : ''}
        `;
        
        // Load pending actions for this email
        loadEmailPendingActions(email.id);
    } catch (error) {
        console.error('Error loading email detail:', error);
        modalBody.innerHTML = '<div class="loading">Fehler beim Laden der Details</div>';
    }
}

// Close modal
function closeModal() {
    document.getElementById('emailModal').classList.remove('active');
}

// Mark email as resolved
async function markAsResolved(emailId) {
    try {
        const response = await fetch(`${API_BASE}/api/emails/${emailId}/resolve`, {
            method: 'POST',
            headers: getAuthHeaders(),
            body: JSON.stringify({
                email_id: emailId,
                resolved: true
            })
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

// Trigger manual processing
async function triggerProcessing() {
    const button = document.getElementById('triggerProcessing');
    button.disabled = true;
    button.textContent = 'Verarbeitung läuft...';
    
    try {
        const response = await fetch(`${API_BASE}/api/processing/trigger`, {
            method: 'POST',
            headers: getAuthHeaders(),
            body: JSON.stringify({
                trigger_type: 'MANUAL'
            })
        });
        
        if (handleAuthError(response)) {
            button.disabled = false;
            button.textContent = 'Jetzt verarbeiten';
            return;
        }
        
        const result = await response.json();
        
        if (result.success) {
            showSuccess('Verarbeitung gestartet');
            // Refresh dashboard after a delay
            setTimeout(async () => {
                await loadDashboard();
                await loadEmails();
            }, 3000);
        } else {
            showError(result.message || 'Verarbeitung konnte nicht gestartet werden');
        }
    } catch (error) {
        console.error('Error triggering processing:', error);
        showError('Fehler beim Starten der Verarbeitung');
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

// Apply filters
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

// Utility functions
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

function showToast(message, type = 'success') {
    // Remove existing toasts
    const existingToast = document.querySelector('.toast');
    if (existingToast) {
        existingToast.remove();
    }
    
    // Create new toast
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    document.body.appendChild(toast);
    
    // Remove after 3 seconds
    setTimeout(() => {
        toast.remove();
    }, 3000);
}

function showSuccess(message) {
    showToast(message, 'success');
}

function showError(message) {
    showToast(message, 'error');
}

// ============================================================================
// Pending Actions Tab
// ============================================================================

let currentActionsPage = 1;
let currentActionsFilters = {
    status: '',
    action_type: ''
};
let allActions = [];

// Switch between tabs
function switchTab(tabName) {
    // Update tab buttons
    document.querySelectorAll('.tab-button').forEach(btn => {
        btn.classList.remove('active');
    });
    
    if (tabName === 'emails') {
        document.getElementById('tabEmails').classList.add('active');
        document.getElementById('emailList').style.display = 'flex';
        document.querySelector('.filters').style.display = 'flex';
        document.getElementById('pendingActionsContainer').style.display = 'none';
    } else if (tabName === 'pendingActions') {
        document.getElementById('tabPendingActions').classList.add('active');
        document.getElementById('emailList').style.display = 'none';
        document.querySelector('.filters').style.display = 'none';
        document.getElementById('pendingActionsContainer').style.display = 'block';
        
        // Load pending actions when tab is opened
        loadPendingActions();
    }
}

// Load pending actions from API
async function loadPendingActions() {
    const actionsList = document.getElementById('actionsList');
    actionsList.innerHTML = '<div class="loading">Lade Pending Actions...</div>';
    
    try {
        const params = new URLSearchParams({
            page: currentActionsPage,
            page_size: 50,
            ...currentActionsFilters
        });
        
        // Remove empty values
        for (const [key, value] of [...params.entries()]) {
            if (!value) params.delete(key);
        }
        
        const response = await fetch(`${API_BASE}/api/pending-actions?${params}`, {
            headers: getAuthHeaders()
        });
        
        if (handleAuthError(response)) return;
        
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        
        allActions = await response.json();
        renderPendingActions(allActions);
        
        // Update pagination
        updateActionsPagination();
    } catch (error) {
        console.error('Error loading pending actions:', error);
        actionsList.innerHTML = '<div class="loading">Fehler beim Laden der Pending Actions</div>';
    }
}

// Render pending actions list
function renderPendingActions(actions) {
    const actionsList = document.getElementById('actionsList');
    
    if (!Array.isArray(actions)) {
        console.error('Expected array but got:', typeof actions, actions);
        actionsList.innerHTML = '<div class="loading">Fehler beim Laden der Pending Actions</div>';
        return;
    }
    
    if (actions.length === 0) {
        actionsList.innerHTML = '<div class="loading">Keine Pending Actions gefunden</div>';
        return;
    }
    
    actionsList.innerHTML = actions.map(action => {
        const isApplyEnabled = action.status === 'APPROVED';
        const showActionButtons = action.status === 'PENDING';
        
        return `
            <div class="action-item">
                <div class="action-header">
                    <div class="action-info">
                        <div class="action-type">${escapeHtml(action.action_type)}</div>
                        <div class="action-email-ref">Email ID: ${action.email_id}</div>
                        <span class="action-status ${action.status}">${action.status}</span>
                        ${action.target_folder ? `<div style="margin-top: 8px; font-size: 13px;">📁 Target: ${escapeHtml(action.target_folder)}</div>` : ''}
                        ${action.reason ? `<div class="action-reason">${escapeHtml(action.reason)}</div>` : ''}
                        <div class="action-meta">
                            <span>📅 ${formatDateTime(action.created_at)}</span>
                            <span>👤 Proposed by: ${escapeHtml(action.proposed_by)}</span>
                            ${action.approved_by ? `<span>✓ Approved by: ${escapeHtml(action.approved_by)}</span>` : ''}
                            ${action.applied_at ? `<span>✓ Applied: ${formatDateTime(action.applied_at)}</span>` : ''}
                            ${action.error_message ? `<span style="color: var(--danger);">❌ Error: ${escapeHtml(action.error_message)}</span>` : ''}
                        </div>
                    </div>
                    <div class="action-buttons">
                        ${showActionButtons ? `
                            <button class="btn btn-success" onclick="approveAction(${action.id})">✓ Approve</button>
                            <button class="btn btn-danger" onclick="rejectAction(${action.id})">✗ Reject</button>
                        ` : ''}
                        ${isApplyEnabled ? `
                            <button class="btn btn-primary" onclick="applyAction(${action.id})">▶ Apply</button>
                        ` : ''}
                    </div>
                </div>
            </div>
        `;
    }).join('');
}

// Update pagination controls
function updateActionsPagination() {
    const pagination = document.getElementById('actionsPagination');
    const pageInfo = document.getElementById('actionsPageInfo');
    const prevBtn = document.getElementById('actionsPrevPage');
    const nextBtn = document.getElementById('actionsNextPage');
    
    if (allActions.length === 0) {
        pagination.style.display = 'none';
        return;
    }
    
    pagination.style.display = 'flex';
    pageInfo.textContent = `Seite ${currentActionsPage}`;
    
    prevBtn.disabled = currentActionsPage === 1;
    nextBtn.disabled = allActions.length < 50;
}

// Change actions page
function changeActionsPage(delta) {
    currentActionsPage = Math.max(1, currentActionsPage + delta);
    loadPendingActions();
}

// Apply action filters
document.addEventListener('DOMContentLoaded', () => {
    const applyActionFiltersBtn = document.getElementById('applyActionFilters');
    if (applyActionFiltersBtn) {
        applyActionFiltersBtn.addEventListener('click', () => {
            currentActionsFilters = {
                status: document.getElementById('filterActionStatus').value || null,
                action_type: document.getElementById('filterActionType').value || null
            };
            
            // Remove null values
            Object.keys(currentActionsFilters).forEach(key => 
                currentActionsFilters[key] === null && delete currentActionsFilters[key]
            );
            
            currentActionsPage = 1;
            loadPendingActions();
        });
    }
});

// Approve single action
async function approveAction(actionId) {
    try {
        const response = await fetch(`${API_BASE}/api/pending-actions/approve`, {
            method: 'POST',
            headers: getAuthHeaders(),
            body: JSON.stringify({
                action_ids: [actionId],
                approved_by: 'admin'
            })
        });
        
        if (handleAuthError(response)) return;
        
        const result = await response.json();
        
        if (response.ok) {
            showSuccess(result.message || 'Action approved');
            loadPendingActions();
        } else {
            showError(result.detail || 'Failed to approve action');
        }
    } catch (error) {
        console.error('Error approving action:', error);
        showError('Fehler beim Genehmigen');
    }
}

// Reject single action
async function rejectAction(actionId) {
    try {
        const response = await fetch(`${API_BASE}/api/pending-actions/reject`, {
            method: 'POST',
            headers: getAuthHeaders(),
            body: JSON.stringify({
                action_ids: [actionId],
                approved_by: 'admin'
            })
        });
        
        if (handleAuthError(response)) return;
        
        const result = await response.json();
        
        if (response.ok) {
            showSuccess(result.message || 'Action rejected');
            loadPendingActions();
        } else {
            showError(result.detail || 'Failed to reject action');
        }
    } catch (error) {
        console.error('Error rejecting action:', error);
        showError('Fehler beim Ablehnen');
    }
}

// Apply single action
async function applyAction(actionId) {
    try {
        const response = await fetch(`${API_BASE}/api/pending-actions/apply`, {
            method: 'POST',
            headers: getAuthHeaders(),
            body: JSON.stringify({
                action_ids: [actionId]
            })
        });
        
        if (handleAuthError(response)) return;
        
        const result = await response.json();
        
        if (response.ok) {
            showSuccess(result.message || 'Action applied');
            loadPendingActions();
        } else {
            showError(result.detail || 'Failed to apply action');
        }
    } catch (error) {
        console.error('Error applying action:', error);
        showError('Fehler beim Anwenden');
    }
}

// Batch approve all on page
async function batchApproveAll() {
    const pendingActions = allActions.filter(a => a.status === 'PENDING');
    
    if (pendingActions.length === 0) {
        showError('Keine pending actions auf dieser Seite');
        return;
    }
    
    try {
        const response = await fetch(`${API_BASE}/api/pending-actions/approve`, {
            method: 'POST',
            headers: getAuthHeaders(),
            body: JSON.stringify({
                action_ids: pendingActions.map(a => a.id),
                approved_by: 'admin'
            })
        });
        
        if (handleAuthError(response)) return;
        
        const result = await response.json();
        
        if (response.ok) {
            showSuccess(result.message || `${pendingActions.length} action(s) approved`);
            loadPendingActions();
        } else {
            showError(result.detail || 'Failed to approve actions');
        }
    } catch (error) {
        console.error('Error batch approving:', error);
        showError('Fehler beim Batch-Genehmigen');
    }
}

// Batch reject all on page
async function batchRejectAll() {
    const pendingActions = allActions.filter(a => a.status === 'PENDING');
    
    if (pendingActions.length === 0) {
        showError('Keine pending actions auf dieser Seite');
        return;
    }
    
    try {
        const response = await fetch(`${API_BASE}/api/pending-actions/reject`, {
            method: 'POST',
            headers: getAuthHeaders(),
            body: JSON.stringify({
                action_ids: pendingActions.map(a => a.id),
                approved_by: 'admin'
            })
        });
        
        if (handleAuthError(response)) return;
        
        const result = await response.json();
        
        if (response.ok) {
            showSuccess(result.message || `${pendingActions.length} action(s) rejected`);
            loadPendingActions();
        } else {
            showError(result.detail || 'Failed to reject actions');
        }
    } catch (error) {
        console.error('Error batch rejecting:', error);
        showError('Fehler beim Batch-Ablehnen');
    }
}

// Batch apply approved actions
async function batchApplyApproved() {
    try {
        const response = await fetch(`${API_BASE}/api/pending-actions/apply`, {
            method: 'POST',
            headers: getAuthHeaders(),
            body: JSON.stringify({
                max_count: 100  // Apply up to 100 approved actions
            })
        });
        
        if (handleAuthError(response)) return;
        
        const result = await response.json();
        
        if (response.ok) {
            showSuccess(result.message || 'Approved actions applied');
            loadPendingActions();
        } else {
            showError(result.detail || 'Failed to apply actions');
        }
    } catch (error) {
        console.error('Error batch applying:', error);
        showError('Fehler beim Batch-Anwenden');
    }
}

// Load pending actions for specific email
async function loadEmailPendingActions(emailId) {
    const proposedActionsList = document.getElementById('proposedActionsList');
    
    try {
        const response = await fetch(`${API_BASE}/api/pending-actions?email_id=${emailId}`, {
            headers: getAuthHeaders()
        });
        
        if (handleAuthError(response)) return;
        
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        
        const actions = await response.json();
        
        if (!Array.isArray(actions) || actions.length === 0) {
            proposedActionsList.innerHTML = '<div style="font-size: 13px; color: var(--text-light);">Keine Pending Actions für diese Email</div>';
            return;
        }
        
        proposedActionsList.innerHTML = `
            <div class="proposed-actions-list">
                ${actions.map(action => `
                    <div class="proposed-action-item">
                        <div class="action-type">${escapeHtml(action.action_type)}</div>
                        <span class="action-status ${action.status}">${action.status}</span>
                        ${action.target_folder ? `<div style="margin-top: 4px;">📁 ${escapeHtml(action.target_folder)}</div>` : ''}
                        ${action.reason ? `<div style="margin-top: 4px; font-size: 12px; color: var(--text-light);">${escapeHtml(action.reason)}</div>` : ''}
                        <div style="margin-top: 4px; font-size: 11px; color: var(--text-light);">
                            Created: ${formatDateTime(action.created_at)}
                        </div>
                    </div>
                `).join('')}
            </div>
        `;
    } catch (error) {
        console.error('Error loading email pending actions:', error);
        proposedActionsList.innerHTML = '<div style="font-size: 13px; color: var(--danger);">Fehler beim Laden</div>';
    }
}
