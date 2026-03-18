from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_contains_archive_folder_controls():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'id="archiveFolderSelect"' in html
    assert 'id="refreshFoldersBtn"' in html
    assert 'id="saveArchiveFolderBtn"' in html
    assert 'id="archiveFolderCurrent"' in html


def test_frontend_wires_folder_loading_and_saving():
    js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    assert "async function loadFolders()" in js
    assert "fetch(`${API_BASE}/api/folders`)" in js
    assert "async function saveArchiveFolderSelection()" in js
    assert "JSON.stringify({ archive_folder: selected })" in js
