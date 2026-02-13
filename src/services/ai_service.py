"""
AI analysis service for email processing
"""
import logging
import json
from typing import Dict, Any, Optional, List
import httpx
from bs4 import BeautifulSoup

from src.config import get_settings
from src.utils.logging import get_logger

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
            logger.error(f"AI analysis failed: {e}")
            return self._fallback_classification(email_data)
    
    def _prepare_content(self, email_data: Dict[str, Any]) -> str:
        """Prepare email content for analysis"""
        subject = email_data.get('subject', '')
        sender = email_data.get('sender', '')
        body_plain = email_data.get('body_plain', '')
        body_html = email_data.get('body_html', '')
        
        # Use plain text if available, otherwise extract from HTML
        if body_plain:
            content = body_plain
        elif body_html:
            content = self._extract_text_from_html(body_html)
        else:
            content = ""
        
        # Limit content length
        max_length = 4000
        if len(content) > max_length:
            content = content[:max_length] + "..."
        
        return f"Betreff: {subject}\nAbsender: {sender}\n\nInhalt:\n{content}"
    
    def _extract_text_from_html(self, html: str) -> str:
        """Extract plain text from HTML"""
        try:
            soup = BeautifulSoup(html, 'lxml')
            # Remove script and style elements
            for script in soup(["script", "style"]):
                script.decompose()
            text = soup.get_text(separator='\n')
            # Clean up whitespace
            lines = (line.strip() for line in text.splitlines())
            return '\n'.join(line for line in lines if line)
        except Exception as e:
            logger.warning(f"Failed to extract text from HTML: {e}")
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
                "options": {
                    "temperature": 0.3,
                    "top_p": 0.9
                }
            }
            
            with httpx.Client(timeout=self.settings.ai_timeout) as client:
                response = client.post(url, json=payload)
                response.raise_for_status()
                
                result = response.json()
                return result.get('response', '')
                
        except httpx.TimeoutException:
            logger.error("AI service timeout")
            return None
        except httpx.HTTPError as e:
            logger.error(f"AI service HTTP error: {e}")
            return None
        except Exception as e:
            logger.error(f"AI service error: {e}")
            return None
    
    def _parse_ai_response(self, response: str) -> Dict[str, Any]:
        """Parse AI response and validate structure"""
        try:
            # Extract JSON from response
            json_start = response.find('{')
            json_end = response.rfind('}') + 1
            
            if json_start == -1 or json_end == 0:
                raise ValueError("No JSON found in response")
            
            json_str = response[json_start:json_end]
            data = json.loads(json_str)
            
            # Validate and normalize
            analysis = {
                'summary': data.get('summary', ''),
                'category': self._validate_category(data.get('category', 'Unklar')),
                'spam_probability': float(data.get('spam_probability', 0.0)),
                'action_required': bool(data.get('action_required', False)),
                'priority': self._validate_priority(data.get('priority', 'LOW')),
                'tasks': self._validate_tasks(data.get('tasks', [])),
                'suggested_folder': data.get('suggested_folder', ''),
                'reasoning': data.get('reasoning', '')
            }
            
            return analysis
            
        except Exception as e:
            logger.error(f"Failed to parse AI response: {e}")
            raise
    
    def _validate_category(self, category: str) -> str:
        """Validate category"""
        valid_categories = ['Klinik', 'Forschung', 'Privat', 'Verwaltung', 'Unklar']
        return category if category in valid_categories else 'Unklar'
    
    def _validate_priority(self, priority: str) -> str:
        """Validate priority"""
        valid_priorities = ['LOW', 'MEDIUM', 'HIGH']
        return priority if priority in valid_priorities else 'LOW'
    
    def _validate_tasks(self, tasks: List[Dict]) -> List[Dict]:
        """Validate and normalize tasks"""
        validated = []
        for task in tasks:
            if isinstance(task, dict) and task.get('description'):
                validated.append({
                    'description': task.get('description', ''),
                    'due_date': task.get('due_date'),
                    'context': task.get('context', ''),
                    'confidence': float(task.get('confidence', 0.5))
                })
        return validated
    
    def _fallback_classification(self, email_data: Dict[str, Any]) -> Dict[str, Any]:
        """Fallback classification when AI fails"""
        logger.warning("Using fallback classification")
        
        subject = email_data.get('subject', '').lower()
        body = email_data.get('body_plain', '').lower()
        sender = email_data.get('sender', '').lower()
        
        # Simple heuristics
        is_spam = self._is_spam_heuristic(subject, body, sender)
        action_required = self._requires_action_heuristic(subject, body)
        
        return {
            'summary': f"E-Mail von {email_data.get('sender', 'Unbekannt')}: {email_data.get('subject', 'Kein Betreff')}",
            'category': 'Unklar',
            'spam_probability': 0.8 if is_spam else 0.2,
            'action_required': action_required,
            'priority': 'MEDIUM' if action_required else 'LOW',
            'tasks': [],
            'suggested_folder': 'Archive',
            'reasoning': 'Automatische Fallback-Klassifizierung (AI nicht verfügbar)'
        }
    
    def _is_spam_heuristic(self, subject: str, body: str, sender: str) -> bool:
        """Simple spam detection heuristics"""
        spam_indicators = [
            'unsubscribe', 'abmelden', 'newsletter',
            'click here', 'klicken sie hier',
            'congratulations', 'gewonnen',
            'free', 'kostenlos', 'gratis'
        ]
        
        content = f"{subject} {body} {sender}"
        return any(indicator in content for indicator in spam_indicators)
    
    def _requires_action_heuristic(self, subject: str, body: str) -> bool:
        """Simple action detection heuristics"""
        action_indicators = [
            'please', 'bitte',
            'urgent', 'dringend',
            'deadline', 'frist',
            'respond', 'antworten',
            'confirm', 'bestätigen'
        ]
        
        content = f"{subject} {body}"
        return any(indicator in content for indicator in action_indicators)
    
    def check_health(self) -> Dict[str, Any]:
        """Check AI service health"""
        try:
            url = f"{self.settings.ai_endpoint}/api/tags"
            
            with httpx.Client(timeout=5) as client:
                response = client.get(url)
                response.raise_for_status()
                
                return {
                    "status": "healthy",
                    "available": True,
                    "message": "AI service is available"
                }
        except Exception as e:
            return {
                "status": "unhealthy",
                "available": False,
                "message": f"AI service error: {str(e)}"
            }
