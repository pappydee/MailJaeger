"""Thread-level summary generation with cache-aware regeneration."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from src.models.database import AppSetting, ProcessedEmail
from src.services.ai_service import AIService


class ThreadSummaryService:
    def __init__(self, ai_service: Optional[AIService] = None):
        self.ai_service = ai_service or AIService()

    @staticmethod
    def _cache_key(thread_id: str) -> str:
        return f"thread_summary::{thread_id}"

    @classmethod
    def _signature(cls, emails: List[ProcessedEmail]) -> str:
        latest = emails[0] if emails else None
        payload = {
            "count": len(emails),
            "latest_id": latest.id if latest else None,
            "latest_message_id": latest.message_id if latest else None,
            "latest_date": (
                cls._normalize_datetime(
                    latest.date or latest.processed_at or latest.created_at
                ).isoformat()
                if latest
                and cls._normalize_datetime(
                    latest.date or latest.processed_at or latest.created_at
                )
                else None
            ),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    def _load_cached(self, db: Session, *, thread_id: str) -> Optional[Dict]:
        key = self._cache_key(thread_id)
        row = db.query(AppSetting).filter(AppSetting.key == key).first()
        if row and isinstance(row.value, dict):
            return row.value
        return None

    def _store_cached(self, db: Session, *, thread_id: str, payload: Dict) -> Dict:
        key = self._cache_key(thread_id)
        row = db.query(AppSetting).filter(AppSetting.key == key).first()
        if row:
            row.value = payload
            row.updated_at = datetime.now(timezone.utc)
        else:
            db.add(AppSetting(key=key, value=payload))
        db.flush()
        return payload

    def _fallback_summary(self, emails: List[ProcessedEmail], thread_state: str) -> Dict:
        latest = emails[0] if emails else None
        topic = (latest.subject or "Allgemeine Konversation") if latest else "Unbekannt"
        summary_candidates: List[str] = []
        for email in emails[:4]:
            snippet = (email.summary or email.snippet or email.subject or "").strip()
            if snippet:
                summary_candidates.append(snippet[:140])
        compact = " | ".join(summary_candidates[:3]).strip()
        if compact:
            compact = compact[:320]
        else:
            compact = "Keine Thread-Zusammenfassung verfügbar."
        status_text = (
            "You need to act"
            if thread_state == "waiting_for_me"
            else "Waiting for reply"
            if thread_state == "waiting_for_other"
            else thread_state.replace("_", " ")
        )
        return {
            "summary": compact,
            "key_topic": topic[:120],
            "status": status_text,
        }

    def _generate_summary_with_llm(
        self, *, emails: List[ProcessedEmail], thread_state: str
    ) -> Optional[Dict]:
        lines: List[str] = []
        for email in emails[:8]:
            sender = email.sender or "?"
            subject = email.subject or "(kein Betreff)"
            short_summary = (email.summary or email.snippet or "").strip()
            if short_summary:
                short_summary = short_summary[:180]
                lines.append(f"- {sender}: {subject} ({short_summary})")
            else:
                lines.append(f"- {sender}: {subject}")

        prompt = (
            "Fasse den folgenden E-Mail-Thread kurz zusammen. "
            "Gib exakt drei Zeilen zurück:\n"
            "Summary: <2-3 Sätze>\n"
            "Topic: <kurzes Thema>\n"
            "Status: <wer muss handeln>\n\n"
            f"Aktueller Thread-Status: {thread_state}\n"
            "Thread-Nachrichten:\n"
            + "\n".join(lines or ["- Keine Nachrichten"])
        )
        raw = self.ai_service.generate_report(prompt)
        if not raw or not raw.strip():
            return None

        parsed = {"summary": "", "key_topic": "", "status": ""}
        for line in raw.splitlines():
            line = line.strip()
            if line.lower().startswith("summary:"):
                parsed["summary"] = line.split(":", 1)[1].strip()
            elif line.lower().startswith("topic:"):
                parsed["key_topic"] = line.split(":", 1)[1].strip()
            elif line.lower().startswith("status:"):
                parsed["status"] = line.split(":", 1)[1].strip()

        if not parsed["summary"]:
            parsed["summary"] = raw.strip()[:320]
        if not parsed["key_topic"]:
            parsed["key_topic"] = (emails[0].subject or "Unbekannt") if emails else "Unbekannt"
        if not parsed["status"]:
            parsed["status"] = thread_state.replace("_", " ")
        return parsed

    def get_or_generate_summary(
        self,
        db: Session,
        *,
        thread_id: Optional[str],
        emails: Iterable[ProcessedEmail],
        thread_state: str,
        allow_generate: bool,
    ) -> Optional[Dict]:
        if not thread_id:
            return None
        ordered = list(emails)
        ordered.sort(key=lambda email: (self._datetime_sort_value(email), email.id or 0), reverse=True)
        if not ordered:
            return None

        signature = self._signature(ordered)
        cached = self._load_cached(db, thread_id=thread_id)
        if cached and cached.get("signature") == signature:
            return cached
        if not allow_generate and cached:
            return cached

        payload = None
        if allow_generate:
            payload = self._generate_summary_with_llm(
                emails=ordered,
                thread_state=thread_state,
            )
        if payload is None:
            payload = self._fallback_summary(ordered, thread_state)

        payload.update(
            {
                "thread_id": thread_id,
                "signature": signature,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return self._store_cached(db, thread_id=thread_id, payload=payload)
    @staticmethod
    def _normalize_datetime(value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @classmethod
    def _datetime_sort_value(cls, email: ProcessedEmail) -> float:
        dt = cls._normalize_datetime(email.date or email.processed_at or email.created_at)
        return dt.timestamp() if dt else 0.0
