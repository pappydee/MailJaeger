from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_contains_safe_mode_toggle_control():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'id="safeModeToggle"' in html
    assert "SAFE MODE aktiv: Keine Aktionen werden automatisch ausgeführt." in html
    assert "Manuelle Ausführung per „Ausführen“ ist weiterhin möglich." in html


def test_frontend_wires_safe_mode_toggle_to_settings_endpoint():
    js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    assert "async function toggleSafeMode()" in js
    assert "document.getElementById('safeModeToggle')?.addEventListener('click', toggleSafeMode);" in js
    assert "fetch(`${API_BASE}/api/settings`" in js
    assert "JSON.stringify({ safe_mode: target })" in js
