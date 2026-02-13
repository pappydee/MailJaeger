// MailJaeger Dashboard JavaScript

const API_BASE = window.location.origin;

// State
let allEmails = [];
let currentFilters = {
    category: '',
    priority: '',
    action_required: ''
};

// Initialize dashboard
document.addEventListener('DOMContentLoaded', async () => {
    await loadDashboard();
    await loadEmails();
    setupEventListeners();
    
    // Refresh dashboard every 30 seconds
    setInterval(loadDashboard, 30000);
});

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
        const response = await fetch(`${API_BASE}/api/dashboard`);
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
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                page: 1,
                page_size: 50,
                sort_by: 'date',
                sort_order: 'desc',
                ...currentFilters
            })
        });
        
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
        const response = await fetch(`${API_BASE}/api/emails/${emailId}`);
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

// Close modal
function closeModal() {
    document.getElementById('emailModal').classList.remove('active');
}

// Mark email as resolved
async function markAsResolved(emailId) {
    try {
        const response = await fetch(`${API_BASE}/api/emails/${emailId}/resolve`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                email_id: emailId,
                resolved: true
            })
        });
        
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
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                trigger_type: 'MANUAL'
            })
        });
        
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

function showSuccess(message) {
    // Simple alert for now - could be replaced with toast notification
    console.log('Success:', message);
}

function showError(message) {
    // Simple alert for now - could be replaced with toast notification
    console.error('Error:', message);
    alert(message);
}
