"""
Test fail-closed authentication and security hardening
"""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
import os

# Test with NO API key configured to verify fail-closed behavior
os.environ["API_KEY"] = ""
os.environ["IMAP_HOST"] = "imap.test.com"
os.environ["IMAP_USERNAME"] = "test@test.com"
os.environ["IMAP_PASSWORD"] = "test_password"
os.environ["AI_ENDPOINT"] = "http://localhost:11434"

from src.main import app
from src.config import reload_settings

@pytest.fixture
def client():
    """Create test client with NO API key configured"""
    reload_settings()
    return TestClient(app)


class TestFailClosedAuthentication:
    """Test that authentication is fail-closed when no API keys configured"""
    
    def test_health_endpoint_accessible_without_api_key(self, client):
        """Health endpoint should be accessible without API key"""
        with patch('src.main.IMAPService') as mock_imap, \
             patch('src.main.AIService') as mock_ai, \
             patch('src.main.get_scheduler') as mock_scheduler:
            mock_imap.return_value.check_health.return_value = {"status": "healthy"}
            mock_ai.return_value.check_health.return_value = {"status": "healthy"}
            mock_scheduler.return_value.get_status.return_value = {"status": "running"}
            
            response = client.get("/api/health")
            assert response.status_code == 200, "Health endpoint should be accessible without API key"
    
    def test_root_requires_auth_when_no_api_key(self, client):
        """Root endpoint should require auth even when no API key configured (fail-closed)"""
        response = client.get("/")
        assert response.status_code == 401, "Root should return 401 when no API key configured"
        assert "authentication required" in response.json()["detail"].lower()
    
    def test_dashboard_requires_auth_when_no_api_key(self, client):
        """Dashboard should require auth even when no API key configured (fail-closed)"""
        response = client.get("/api/dashboard")
        assert response.status_code == 401, "Dashboard should return 401 when no API key configured"
    
    def test_settings_requires_auth_when_no_api_key(self, client):
        """Settings endpoint should require auth even when no API key configured"""
        response = client.get("/api/settings")
        assert response.status_code == 401, "Settings should return 401 when no API key configured"
    
    def test_email_list_requires_auth_when_no_api_key(self, client):
        """Email list should require auth even when no API key configured"""
        response = client.post("/api/emails/list", json={"page": 1, "page_size": 10})
        assert response.status_code == 401, "Email list should return 401 when no API key configured"


class TestSettingsEndpointSecurity:
    """Test that settings endpoint doesn't expose sensitive information"""
    
    def test_settings_does_not_expose_imap_username(self):
        """Settings endpoint should not return IMAP username"""
        # Use environment with API key for this test
        with patch.dict(os.environ, {
            "API_KEY": "test_key_12345",
            "IMAP_HOST": "imap.test.com",
            "IMAP_USERNAME": "secret_user@test.com",
            "IMAP_PASSWORD": "secret_password",
            "AI_ENDPOINT": "http://localhost:11434",
        }):
            reload_settings()
            test_client = TestClient(app)
            
            with patch('src.main.get_db') as mock_db:
                mock_session = MagicMock()
                mock_db.return_value = mock_session
                
                headers = {"Authorization": "Bearer test_key_12345"}
                response = test_client.get("/api/settings", headers=headers)
                
                assert response.status_code == 200
                settings_data = response.json()
                
                # Verify IMAP username is NOT in response
                assert "imap_username" not in settings_data, "IMAP username should not be exposed"
                
                # Verify IMAP host and port are still available (non-sensitive)
                assert "imap_host" in settings_data
                assert "imap_port" in settings_data
                
                # Verify other sensitive fields are not exposed
                assert "imap_password" not in settings_data
                assert "api_key" not in settings_data
                assert "api_key_file" not in settings_data
                assert "imap_password_file" not in settings_data


class TestDockerfileHostDefault:
    """Test that Dockerfile uses safe host default"""
    
    def test_dockerfile_has_safe_host_default(self):
        """Dockerfile should use 127.0.0.1 as default host"""
        with open("/home/runner/work/MailJaeger/MailJaeger/Dockerfile", "r") as f:
            dockerfile_content = f.read()
        
        # Check for the safe default pattern
        assert "${SERVER_HOST:-127.0.0.1}" in dockerfile_content, \
            "Dockerfile should use ${SERVER_HOST:-127.0.0.1} for safe default"
        
        # Ensure it doesn't hardcode 0.0.0.0
        assert "--host 0.0.0.0" not in dockerfile_content, \
            "Dockerfile should not hardcode 0.0.0.0 as host"


class TestProductionComposeSecur ity:
    """Test that production compose is secure by default"""
    
    def test_prod_compose_does_not_expose_ollama_port(self):
        """Production compose should not publish Ollama port"""
        with open("/home/runner/work/MailJaeger/MailJaeger/docker-compose.prod.yml", "r") as f:
            prod_compose = f.read()
        
        # Check that ollama service doesn't have ports exposed
        assert "11434:11434" not in prod_compose or \
               "# DO NOT expose ports" in prod_compose, \
            "Production compose should not expose Ollama port 11434"
    
    def test_prod_compose_does_not_publish_mailjaeger_port(self):
        """Production compose should not publish mailjaeger port by default"""
        with open("/home/runner/work/MailJaeger/MailJaeger/docker-compose.prod.yml", "r") as f:
            prod_compose = f.read()
        
        # Look for the mailjaeger service
        lines = prod_compose.split('\n')
        in_mailjaeger_service = False
        has_public_port = False
        
        for line in lines:
            if 'mailjaeger:' in line:
                in_mailjaeger_service = True
            elif in_mailjaeger_service and line.strip().startswith('ports:'):
                # If there's an uncommented ports section, it's a problem
                if not line.strip().startswith('#'):
                    has_public_port = True
            elif in_mailjaeger_service and ('networks:' in line or 'secrets:' in line):
                # We've moved past the ports section
                in_mailjaeger_service = False
        
        assert not has_public_port, \
            "Production compose should not have uncommented ports for mailjaeger"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
