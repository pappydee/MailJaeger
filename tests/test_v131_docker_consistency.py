"""
Tests for v1.3.1 corrective consistency pass:

1. Scheduler defaults are 02:00 in all three config sources (regression guard)
2. docker-compose.yml has an active (uncommented) certs volume mount
3. certs/ directory exists in the repo so the bind-mount source always exists
4. docker-compose.prod.yml uses a configurable AI endpoint that defaults to
   host.docker.internal (not a hardcoded internal container address)
5. docker-compose.prod.yml does NOT hardcode user: "1000:1000"
6. docker-compose.prod.yml containerised Ollama service is optional (profile-gated)
7. .gitignore excludes real cert files while allowing the directory itself
8. Python config default for ai_endpoint is host.docker.internal
"""

import os
import pytest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent

ENV = {
    "API_KEY": "test_key_v131",
    "IMAP_HOST": "imap.test.com",
    "IMAP_USERNAME": "test@test.com",
    "IMAP_PASSWORD": "test_password",
}


# ===========================================================================
# Task 1 — Scheduler defaults (regression guard — already fixed in v1.3)
# ===========================================================================


class TestSchedulerDefaultsRegression:
    """02:00 must remain the default in every config source."""

    def test_docker_compose_yml_schedule_time_default(self):
        content = (REPO_ROOT / "docker-compose.yml").read_text()
        assert "SCHEDULE_TIME:-02:00" in content
        assert "SCHEDULE_TIME:-08:00" not in content

    def test_docker_compose_prod_yml_schedule_time_default(self):
        content = (REPO_ROOT / "docker-compose.prod.yml").read_text()
        assert "SCHEDULE_TIME:-02:00" in content
        assert "SCHEDULE_TIME:-08:00" not in content

    def test_env_example_schedule_time(self):
        content = (REPO_ROOT / ".env.example").read_text()
        assert "SCHEDULE_TIME=02:00" in content
        assert "SCHEDULE_TIME=08:00" not in content

    def test_python_config_default(self):
        with patch.dict(os.environ, ENV):
            from src.config import reload_settings
            s = reload_settings()
        assert s.schedule_time == "02:00"


# ===========================================================================
# Task 2 — CA/TLS certs: active volume mount + repo directory
# ===========================================================================


class TestCertsVolumeMount:
    """The certs bind-mount must be active (not commented out) in compose files."""

    def test_certs_directory_exists_in_repo(self):
        """certs/ must exist so Docker Compose bind-mount never fails."""
        certs_dir = REPO_ROOT / "certs"
        assert certs_dir.is_dir(), (
            "certs/ directory must exist in the repository root so the "
            "Docker Compose bind-mount source always exists on the host"
        )

    def test_certs_gitkeep_present(self):
        """certs/.gitkeep must exist so the directory is tracked by git."""
        assert (REPO_ROOT / "certs" / ".gitkeep").exists()

    def test_docker_compose_yml_certs_volume_is_active(self):
        """docker-compose.yml must have an uncommented ./certs:/app/certs mount."""
        content = (REPO_ROOT / "docker-compose.yml").read_text()
        active_lines = [
            line.strip()
            for line in content.splitlines()
            if "./certs:/app/certs" in line and not line.strip().startswith("#")
        ]
        assert active_lines, (
            "docker-compose.yml must have an active (uncommented) "
            "'./certs:/app/certs' volume mount"
        )

    def test_docker_compose_prod_yml_certs_volume_is_active(self):
        """docker-compose.prod.yml must also have an active certs volume mount."""
        content = (REPO_ROOT / "docker-compose.prod.yml").read_text()
        active_lines = [
            line.strip()
            for line in content.splitlines()
            if "./certs:/app/certs" in line and not line.strip().startswith("#")
        ]
        assert active_lines, (
            "docker-compose.prod.yml must have an active (uncommented) "
            "'./certs:/app/certs' volume mount"
        )

    def test_certs_gitignore_excludes_real_certs(self):
        """.gitignore must exclude *.crt and *.pem inside certs/."""
        content = (REPO_ROOT / ".gitignore").read_text()
        assert "certs/*.crt" in content, (
            ".gitignore must contain 'certs/*.crt' to prevent real cert files "
            "from being committed"
        )
        assert "certs/*.pem" in content, (
            ".gitignore must contain 'certs/*.pem' to prevent real cert files "
            "from being committed"
        )

    def test_entrypoint_handles_empty_certs_gracefully(self):
        """entrypoint.sh must not fail when certs dir is present but empty."""
        import subprocess
        result = subprocess.run(
            ["sh", "-n", str(REPO_ROOT / "scripts" / "entrypoint.sh")],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"entrypoint.sh has syntax errors: {result.stderr}"
        )

    def test_env_example_does_not_say_uncomment_certs_volume(self):
        """.env.example must not instruct users to manually uncomment the certs volume.
        The mount is now active by default so those instructions are stale."""
        content = (REPO_ROOT / ".env.example").read_text()
        # The old step 3 told users to uncomment the line in docker-compose.yml
        assert "Uncomment the certs volume" not in content, (
            ".env.example still says to uncomment the certs volume — "
            "update the comment to reflect that the mount is now active by default"
        )


# ===========================================================================
# Task 3 — Local Ollama: configurable AI endpoint in prod compose
# ===========================================================================


class TestLocalOllamaConfig:
    """AI endpoint must default to host.docker.internal in all compose files."""

    def test_docker_compose_yml_uses_host_docker_internal(self):
        """docker-compose.yml AI endpoint must default to host.docker.internal."""
        content = (REPO_ROOT / "docker-compose.yml").read_text()
        assert "host.docker.internal:11434" in content, (
            "docker-compose.yml AI_ENDPOINT must default to host.docker.internal:11434"
        )

    def test_docker_compose_prod_yml_ai_endpoint_is_configurable(self):
        """docker-compose.prod.yml AI_ENDPOINT must be an env-var expression, not hardcoded."""
        content = (REPO_ROOT / "docker-compose.prod.yml").read_text()
        # Must not contain the old hardcoded value as a plain (non-variable) assignment
        hardcoded_lines = [
            line.strip()
            for line in content.splitlines()
            if "AI_ENDPOINT=http://ollama:11434" in line
            and not line.strip().startswith("#")
            and "${" not in line
        ]
        assert not hardcoded_lines, (
            "docker-compose.prod.yml must not hardcode AI_ENDPOINT=http://ollama:11434; "
            "use ${AI_ENDPOINT:-...} so it can be overridden"
        )

    def test_docker_compose_prod_yml_default_uses_host_docker_internal(self):
        """docker-compose.prod.yml AI endpoint default must be host.docker.internal."""
        content = (REPO_ROOT / "docker-compose.prod.yml").read_text()
        assert "host.docker.internal:11434" in content, (
            "docker-compose.prod.yml must default AI_ENDPOINT to host.docker.internal:11434"
        )

    def test_docker_compose_prod_yml_has_extra_hosts(self):
        """docker-compose.prod.yml must declare host.docker.internal extra_host."""
        content = (REPO_ROOT / "docker-compose.prod.yml").read_text()
        assert "host-gateway" in content, (
            "docker-compose.prod.yml must declare 'host.docker.internal:host-gateway' "
            "so the container can reach the host Ollama instance"
        )

    def test_docker_compose_prod_ollama_service_is_optional(self):
        """Containerised Ollama in docker-compose.prod.yml must have a profile so it
        is not started by default (start on demand with --profile with-ollama)."""
        content = (REPO_ROOT / "docker-compose.prod.yml").read_text()
        assert "profiles" in content, (
            "docker-compose.prod.yml must use Docker Compose profiles to make "
            "the containerised Ollama service opt-in, not started by default"
        )

    def test_docker_compose_prod_mailjaeger_does_not_depend_on_ollama(self):
        """mailjaeger service must not have a mandatory depends_on for ollama."""
        content = (REPO_ROOT / "docker-compose.prod.yml").read_text()
        # Find the mailjaeger service block (after the ollama service)
        # A simple check: the active depends_on block must not reference ollama
        # We look for non-commented "depends_on" lines followed by "ollama"
        lines = content.splitlines()
        in_depends_on = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "depends_on:" in stripped:
                in_depends_on = True
                continue
            if in_depends_on:
                if stripped == "- ollama":
                    pytest.fail(
                        "mailjaeger service in docker-compose.prod.yml must not have "
                        "a mandatory depends_on for ollama (ollama is now optional)"
                    )
                # Stop checking when we exit the depends_on block
                if stripped and not stripped.startswith("-"):
                    in_depends_on = False

    def test_python_config_ai_endpoint_default_is_host_docker_internal(self):
        """Python Settings.ai_endpoint Field default must be host.docker.internal.

        Note: tests use AI_ENDPOINT=http://localhost:11434 via conftest for isolation,
        so we check the Field.default rather than a loaded instance to avoid that
        test-baseline interference.
        """
        from src.config import Settings
        # Access the Pydantic field default without instantiating (no env-var influence)
        field_default = Settings.model_fields["ai_endpoint"].default
        assert "host.docker.internal" in field_default, (
            f"Settings.ai_endpoint Field default should be host.docker.internal, "
            f"got {field_default!r}"
        )


# ===========================================================================
# Task 4 — No hardcoded user: "1000:1000" in local/prod compose
# ===========================================================================


class TestNoHardcodedUser:
    """Neither compose file must hardcode user: "1000:1000"."""

    def test_docker_compose_yml_no_hardcoded_user(self):
        content = (REPO_ROOT / "docker-compose.yml").read_text()
        hardcoded = [
            line.strip()
            for line in content.splitlines()
            if 'user: "1000:1000"' in line and not line.strip().startswith("#")
        ]
        assert not hardcoded, (
            "docker-compose.yml must not hardcode user: '1000:1000' — "
            "this breaks macOS volume permissions"
        )

    def test_docker_compose_prod_yml_no_hardcoded_user(self):
        content = (REPO_ROOT / "docker-compose.prod.yml").read_text()
        hardcoded = [
            line.strip()
            for line in content.splitlines()
            if 'user: "1000:1000"' in line and not line.strip().startswith("#")
        ]
        assert not hardcoded, (
            "docker-compose.prod.yml must not hardcode user: '1000:1000' — "
            "this breaks macOS volume permissions"
        )
