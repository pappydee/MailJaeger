"""
AI analysis service for email processing
"""

import logging
import json
import re
from typing import Dict, Any, Optional, List
import httpx
from bs4 import BeautifulSoup

from src.config import get_settings
from src.utils.logging import get_logger
from src.utils.error_handling import sanitize_error

logger = get_logger(__name__)


class AIService:
    """Service for AI-powered email analysis"""

    def __init__(self):
        self.settings = get_settings()

    def analyze_email(self, email_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Analyze email using local AI

        Returns structured analysis including:
        - summary (German)
        - category
        - spam_probability
        - action_required
        - priority
        - tasks
        - suggested_folder
        - reasoning
        """
        try:
            # Prepare email content
            content = self._prepare_content(email_data)

            # Create analysis prompt
            prompt = self._create_analysis_prompt(content)

            # Call AI service
            response = self._call_ai_service(prompt)

            if response:
                # Parse and validate response
                analysis = self._parse_ai_response(response)
                return analysis
            else:
                # Fallback classification
                return self._fallback_classification(email_data)

        except Exception as e:
            sanitized_error = sanitize_error(e, debug=self.settings.debug)
            logger.error(f"AI analysis failed: {sanitized_error}")
            return self._fallback_classification(email_data)

    def _prepare_content(self, email_data: Dict[str, Any]) -> str:
        """Prepare email content for analysis"""
        subject = email_data.get("subject", "")
        sender = email_data.get("sender", "")
        body_plain = email_data.get("body_plain", "")
        body_html = email_data.get("body_html", "")

        # Always prefer plain text; always strip HTML — never pass raw markup to AI
        if body_plain:
            content = body_plain
        elif body_html:
            content = self._extract_text_from_html(body_html)
        else:
            content = ""

        # Strip any residual HTML even in "plain" body (some clients mix)
        if "<" in content and ">" in content:
            content = self._extract_text_from_html(content)

        # Cap content length to keep prompts short and avoid timeout
        max_length = 1500
        if len(content) > max_length:
            content = content[:max_length] + "…"

        return f"Betreff: {subject}\nAbsender: {sender}\n\nInhalt:\n{content}"

    def _extract_text_from_html(self, html: str) -> str:
        """Extract plain text from HTML"""
        try:
            soup = BeautifulSoup(html, "lxml")
            # Remove script and style elements
            for script in soup(["script", "style"]):
                script.decompose()
            text = soup.get_text(separator="\n")
            # Clean up whitespace
            lines = (line.strip() for line in text.splitlines())
            return "\n".join(line for line in lines if line)
        except Exception as e:
            sanitized_error = sanitize_error(e, debug=self.settings.debug)
            logger.warning(f"Failed to extract text from HTML: {sanitized_error}")
            return html

    def _create_analysis_prompt(self, content: str) -> str:
        """Create analysis prompt for AI"""
        prompt = f"""Analysiere die folgende E-Mail und gib eine strukturierte Antwort im JSON-Format zurück.

E-Mail:
{content}

Antworte mit einem JSON-Objekt mit folgender Struktur:
{{
  "summary": "Kurze Zusammenfassung auf Deutsch (2-3 Sätze)",
  "category": "Eine von: Klinik, Forschung, Privat, Verwaltung, Unklar",
  "spam_probability": 0.0-1.0,
  "action_required": true/false,
  "priority": "Eine von: LOW, MEDIUM, HIGH",
  "tasks": [
    {{
      "description": "Aufgabenbeschreibung",
      "due_date": "YYYY-MM-DD oder null",
      "context": "Kontext",
      "confidence": 0.0-1.0
    }}
  ],
  "suggested_folder": "Vorgeschlagener Ordner",
  "reasoning": "Kurze Begründung der Klassifizierung"
}}

Kriterien:
- HIGH Priorität: Dringend, Frist < 7 Tage, klinisch/administrativ kritisch
- MEDIUM Priorität: Handlungsbedarf ohne Dringlichkeit
- LOW Priorität: Nur informativ
- Spam: Werbung, Newsletter, verdächtige Struktur
- Action required: Explizite Anfrage, administrative/klinische Verantwortung, Aufgabe mit Frist

Antworte NUR mit dem JSON-Objekt, keine zusätzlichen Erklärungen."""

        return prompt

    def _call_ai_service(self, prompt: str) -> Optional[str]:
        """Call local AI service (Ollama)"""
        try:
            url = f"{self.settings.ai_endpoint}/api/generate"

            payload = {
                "model": self.settings.ai_model,
                "prompt": prompt,
                "stream": False,
                "keep_alive": self.settings.ai_keep_alive,
                "options": {
                    "temperature": self.settings.ai_temperature,
                    "top_p": self.settings.ai_top_p,
                    "num_ctx": self.settings.ai_num_ctx,
                    "num_predict": self.settings.ai_num_predict,
                },
            }

            with httpx.Client(timeout=self.settings.ai_timeout) as client:
                response = client.post(url, json=payload)
                response.raise_for_status()

                result = response.json()
                return result.get("response", "")

        except httpx.TimeoutException:
            logger.error(
                f"AI service timeout after {self.settings.ai_timeout}s - using fallback"
            )
            return None
        except httpx.HTTPError as e:
            sanitized_error = sanitize_error(e, debug=self.settings.debug)
            logger.error(f"AI service HTTP error: {sanitized_error}")
            return None
        except Exception as e:
            sanitized_error = sanitize_error(e, debug=self.settings.debug)
            logger.error(f"AI service error: {sanitized_error}")
            return None

    def _parse_ai_response(self, response: str) -> Dict[str, Any]:
        """Parse AI response and validate structure.

        Handles:
        - Bare JSON
        - JSON wrapped in ```json ... ``` code fences
        - Leading/trailing prose text
        - Missing or invalid field values (clamped/defaulted)
        """
        try:
            json_str = self._extract_json_string(response)
            data = json.loads(json_str)

            # Strict validation and normalization
            analysis = {
                "summary": self._validate_string(
                    data.get("summary", ""), max_length=500
                ),
                "category": self._validate_category(data.get("category", "Unklar")),
                "spam_probability": self._validate_probability(
                    data.get("spam_probability", 0.0)
                ),
                "action_required": bool(data.get("action_required", False)),
                "priority": self._validate_priority(data.get("priority", "LOW")),
                "tasks": self._validate_tasks(data.get("tasks", [])),
                "suggested_folder": self._validate_folder(
                    data.get("suggested_folder", "Archive")
                ),
                "reasoning": self._validate_string(
                    data.get("reasoning", ""), max_length=500
                ),
            }

            # Additional safety check: ensure all required fields present
            required_fields = [
                "summary",
                "category",
                "spam_probability",
                "action_required",
                "priority",
            ]
            for field in required_fields:
                if field not in analysis or analysis[field] is None:
                    logger.warning(f"Missing required field '{field}' in AI response")
                    raise ValueError(f"Missing required field: {field}")

            return analysis

        except json.JSONDecodeError as e:
            sanitized_error = sanitize_error(e, debug=self.settings.debug)
            logger.error(f"Failed to parse JSON from AI response: {sanitized_error}")
            raise
        except Exception as e:
            sanitized_error = sanitize_error(e, debug=self.settings.debug)
            logger.error(f"Failed to validate AI response: {sanitized_error}")
            raise

    def _extract_json_string(self, text: str) -> str:
        """Extract the JSON object string from an AI response.

        Tries in order:
        1. Strip ```json ... ``` or ``` ... ``` code fences
        2. Find the first '{' to last '}' span
        """
        # 1. Strip code fences (```json or ```)
        stripped = text.strip()
        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", stripped)
        if fence_match:
            candidate = fence_match.group(1).strip()
            # Validate it looks like an object
            if candidate.startswith("{"):
                return candidate

        # 2. Find first { … last }
        json_start = stripped.find("{")
        json_end = stripped.rfind("}") + 1
        if json_start != -1 and json_end > json_start:
            return stripped[json_start:json_end]

        raise ValueError("No JSON object found in AI response")

    def _validate_string(self, value: Any, max_length: int = 1000) -> str:
        """Validate and sanitize string values"""
        if not value:
            return ""

        # Convert to string and truncate if needed
        str_value = str(value)[:max_length]

        # Strip control characters but preserve Unicode (including German umlauts: ä ö ü ß)
        # Allow printable ASCII, Unicode letters/digits/punctuation, newlines and tabs
        sanitized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str_value)

        return sanitized.strip()

    def _validate_probability(self, value: Any) -> float:
        """Validate probability value (0.0 to 1.0)"""
        try:
            prob = float(value)
            # Clamp to valid range
            return max(0.0, min(1.0, prob))
        except (ValueError, TypeError):
            logger.warning(f"Invalid probability value: {value}, using 0.0")
            return 0.0

    def _validate_category(self, category: str) -> str:
        """Validate category"""
        valid_categories = ["Klinik", "Forschung", "Privat", "Verwaltung", "Unklar"]
        return category if category in valid_categories else "Unklar"

    def _validate_priority(self, priority: str) -> str:
        """Validate priority"""
        valid_priorities = ["LOW", "MEDIUM", "HIGH"]
        return priority if priority in valid_priorities else "LOW"

    def _validate_tasks(self, tasks: List[Dict]) -> List[Dict]:
        """Validate and normalize tasks"""
        validated = []

        # Limit number of tasks to prevent abuse
        max_tasks = 10
        tasks = tasks[:max_tasks] if isinstance(tasks, list) else []

        for task in tasks:
            if not isinstance(task, dict):
                continue

            description = self._validate_string(
                task.get("description", ""), max_length=500
            )
            if not description:
                continue

            # Validate due_date if present
            due_date = task.get("due_date")
            if due_date and not isinstance(due_date, str):
                due_date = None

            validated.append(
                {
                    "description": description,
                    "due_date": due_date,
                    "context": self._validate_string(
                        task.get("context", ""), max_length=200
                    ),
                    "confidence": self._validate_probability(
                        task.get("confidence", 0.5)
                    ),
                }
            )

        return validated

    def _validate_folder(self, folder: str) -> str:
        """Validate suggested folder against allowlist"""
        # Allowlist of safe folder names - AI can only suggest these
        # This prevents prompt injection attacks where AI suggests malicious folder operations
        allowed_folders = [
            "Archive",
            "Klinik",
            "Forschung",
            "Privat",
            "Verwaltung",
            "Important",
            "Later",
            self.settings.archive_folder,
            self.settings.inbox_folder,
            # Note: Spam/Quarantine folders are NOT in allowlist -
            # spam handling is done by system logic, not AI suggestions
        ]

        # Normalize and check
        folder_clean = self._validate_string(folder, max_length=50)
        if folder_clean in allowed_folders:
            return folder_clean

        # Default to Archive if not in allowlist
        logger.warning(
            f"AI suggested non-allowed folder '{folder}', defaulting to Archive"
        )
        return "Archive"

    def _fallback_classification(self, email_data: Dict[str, Any]) -> Dict[str, Any]:
        """Fallback classification when AI fails"""
        return self.fallback_classification(email_data)

    def fallback_classification(self, email_data: Dict[str, Any]) -> Dict[str, Any]:
        """Fallback classification when AI fails (public API)."""
        logger.warning("Using fallback classification")

        subject = email_data.get("subject", "").lower()
        body = email_data.get("body_plain", "").lower()
        sender = email_data.get("sender", "").lower()

        # Simple heuristics
        is_spam = self._is_spam_heuristic(subject, body, sender)
        action_required = self._requires_action_heuristic(subject, body)

        return {
            "summary": f"E-Mail von {email_data.get('sender', 'Unbekannt')}: {email_data.get('subject', 'Kein Betreff')}",
            "category": "Unklar",
            "spam_probability": 0.8 if is_spam else 0.2,
            "action_required": action_required,
            "priority": "MEDIUM" if action_required else "LOW",
            "tasks": [],
            "suggested_folder": "Archive",
            "reasoning": "Automatische Fallback-Klassifizierung (AI nicht verfügbar)",
        }

    def _is_spam_heuristic(self, subject: str, body: str, sender: str) -> bool:
        """Simple spam detection heuristics"""
        spam_indicators = [
            "unsubscribe",
            "abmelden",
            "newsletter",
            "click here",
            "klicken sie hier",
            "congratulations",
            "gewonnen",
            "free",
            "kostenlos",
            "gratis",
        ]

        content = f"{subject} {body} {sender}"
        return any(indicator in content for indicator in spam_indicators)

    def _requires_action_heuristic(self, subject: str, body: str) -> bool:
        """Simple action detection heuristics"""
        action_indicators = [
            "please",
            "bitte",
            "urgent",
            "dringend",
            "deadline",
            "frist",
            "respond",
            "antworten",
            "confirm",
            "bestätigen",
        ]

        content = f"{subject} {body}"
        return any(indicator in content for indicator in action_indicators)

    def analyze_emails_batch(
        self, emails: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Analyse a batch of emails in a single LLM request.

        Each item in ``emails`` must have: id, subject, sender,
        body_plain (optional), body_html (optional).

        Returns a list of analysis dicts in the same order as ``emails``.
        On any failure the corresponding entry falls back to
        ``_fallback_classification``.
        """
        if not emails:
            return []

        email_ids = [e.get("id") for e in emails]
        logger.info(
            f"[batch] Starting batch LLM analysis: {len(emails)} email(s), "
            f"ids={email_ids}"
        )

        # Build a combined prompt that asks the model to return a JSON array
        entries = []
        for i, email_data in enumerate(emails):
            content = self._prepare_content(email_data)
            entries.append(f"### Email {i + 1} (id={email_data.get('id', i)})\n{content}")

        combined = "\n\n".join(entries)

        prompt = f"""Analysiere die folgenden {len(emails)} E-Mails und gib eine strukturierte Antwort als JSON-Array zurück.

{combined}

Antworte mit einem JSON-Array mit {len(emails)} Objekten in der gleichen Reihenfolge:
[
  {{
    "email_id": <id aus dem Header>,
    "summary": "Kurze Zusammenfassung auf Deutsch",
    "category": "Klinik|Forschung|Privat|Verwaltung|Unklar",
    "spam_probability": 0.0-1.0,
    "action_required": true/false,
    "priority": "LOW|MEDIUM|HIGH",
    "tasks": [],
    "suggested_folder": "Archive|Klinik|Forschung|Privat|Verwaltung|Important|Later",
    "reasoning": "Kurze Begründung"
  }},
  ...
]

Antworte NUR mit dem JSON-Array, keine zusätzlichen Erklärungen."""

        try:
            raw = self._call_ai_service(prompt)
            if not raw:
                raise ValueError("Empty AI response")

            batch_results = self._parse_batch_response(raw, emails)
            logger.info(
                f"[batch] Completed batch LLM analysis: {len(batch_results)} results "
                f"for {len(emails)} email(s)"
            )
            return batch_results

        except Exception as e:
            sanitized = sanitize_error(e, debug=self.settings.debug)
            logger.error(f"Batch AI analysis failed ({len(emails)} emails): {sanitized}")
            return [self._fallback_classification(e) for e in emails]

    def _parse_batch_response(
        self, response: str, emails: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Parse a JSON-array response for batch analysis.

        Safeguards:
        - Detects and warns about duplicate email_ids in the AI response.
        - Falls back per-item when a single entry is malformed/invalid.
        - Handles truncated arrays by falling back on the missing tail.
        - Validates category and folder values per-item.
        """
        # Extract the JSON array from the response text
        stripped = response.strip()

        # Try code fence first
        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", stripped)
        if fence_match:
            candidate = fence_match.group(1).strip()
            if candidate.startswith("["):
                stripped = candidate

        # Find first [ ... last ]
        arr_start = stripped.find("[")
        arr_end = stripped.rfind("]") + 1
        if arr_start == -1 or arr_end <= arr_start:
            raise ValueError("No JSON array found in batch AI response")

        json_str = stripped[arr_start:arr_end]
        raw_list = json.loads(json_str)

        if not isinstance(raw_list, list):
            raise ValueError("Batch AI response is not a JSON array")

        # Warn about duplicate email_ids returned by the model
        seen_ids = set()
        for item in raw_list:
            if isinstance(item, dict) and "email_id" in item:
                eid = item["email_id"]
                if eid in seen_ids:
                    logger.warning(
                        f"[batch] Duplicate email_id={eid} in AI batch response — "
                        f"only the first occurrence will be used"
                    )
                else:
                    seen_ids.add(eid)

        # Build a lookup by email_id (first occurrence wins)
        id_to_result: Dict[Any, Dict] = {}
        for item in raw_list:
            if isinstance(item, dict) and "email_id" in item:
                eid = item["email_id"]
                if eid not in id_to_result:
                    id_to_result[eid] = item

        results = []
        for i, email_data in enumerate(emails):
            email_id = email_data.get("id", i)
            raw_item = id_to_result.get(email_id) or (raw_list[i] if i < len(raw_list) else None)
            if raw_item:
                try:
                    analysis = self._parse_ai_response(json.dumps(raw_item))
                    results.append(analysis)
                except Exception as e:
                    sanitized = sanitize_error(e, debug=self.settings.debug)
                    logger.warning(
                        f"[batch] Failed to parse batch result for email {email_id}: "
                        f"{sanitized} — using fallback"
                    )
                    results.append(self._fallback_classification(email_data))
            else:
                logger.warning(
                    f"[batch] No AI result for email {email_id} (index {i}) — using fallback"
                )
                results.append(self._fallback_classification(email_data))

        return results

    def generate_report(self, prompt: str) -> Optional[str]:
        """
        Generate a free-form text report using the configured AI model.

        Unlike ``analyze_email`` / ``analyze_emails_batch`` this method
        returns the raw AI response string rather than parsing it into a
        structured dict.  Callers are responsible for handling a ``None``
        response (AI unavailable) gracefully.
        """
        try:
            return self._call_ai_service(prompt)
        except Exception as e:
            sanitized = sanitize_error(e, debug=self.settings.debug)
            logger.error(f"Report generation failed: {sanitized}")
            return None

    def check_health(self) -> Dict[str, Any]:
        """Check AI service health"""
        try:
            url = f"{self.settings.ai_endpoint}/api/tags"

            with httpx.Client(timeout=self.settings.ai_timeout) as client:
                response = client.get(url)
                response.raise_for_status()

                return {
                    "status": "healthy",
                    "available": True,
                    "message": "AI service is available",
                }
        except Exception as e:
            return {
                "status": "unhealthy",
                "available": False,
                "message": f"AI service error: {str(e)}",
            }
