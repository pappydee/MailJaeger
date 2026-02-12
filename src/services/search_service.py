"""
Search service for email retrieval and semantic search
"""
import logging
import os
from typing import List, Dict, Any, Optional
from pathlib import Path
from sqlalchemy.orm import Session
from sqlalchemy import or_, and_

from whoosh import index
from whoosh.fields import Schema, TEXT, ID, DATETIME, KEYWORD
from whoosh.qparser import QueryParser, MultifieldParser
from whoosh.query import DateRange

from src.config import get_settings
from src.models.database import ProcessedEmail, EmailTask
from src.utils.logging import get_logger

logger = get_logger(__name__)


class SearchService:
    """Service for searching emails"""
    
    def __init__(self, db_session: Session):
        self.settings = get_settings()
        self.db = db_session
        self.index_dir = Path(self.settings.search_index_dir)
        self._init_index()
    
    def _init_index(self):
        """Initialize search index"""
        try:
            self.index_dir.mkdir(parents=True, exist_ok=True)
            
            # Define schema
            schema = Schema(
                id=ID(stored=True, unique=True),
                message_id=ID(stored=True),
                subject=TEXT(stored=True),
                sender=TEXT(stored=True),
                summary=TEXT(stored=True),
                body=TEXT,
                tasks=TEXT,
                category=KEYWORD(stored=True),
                priority=KEYWORD(stored=True),
                date=DATETIME(stored=True)
            )
            
            # Create or open index
            if index.exists_in(str(self.index_dir)):
                self.ix = index.open_dir(str(self.index_dir))
            else:
                self.ix = index.create_in(str(self.index_dir), schema)
                logger.info("Created new search index")
        
        except Exception as e:
            logger.error(f"Failed to initialize search index: {e}")
            self.ix = None
    
    def index_email(self, email: ProcessedEmail):
        """Add email to search index"""
        if not self.ix:
            return
        
        try:
            writer = self.ix.writer()
            
            # Combine task descriptions
            tasks_text = " ".join([task.description for task in email.tasks])
            
            writer.add_document(
                id=str(email.id),
                message_id=email.message_id,
                subject=email.subject or "",
                sender=email.sender or "",
                summary=email.summary or "",
                body=(email.body_plain or "") if email.body_plain else "",
                tasks=tasks_text,
                category=email.category or "",
                priority=email.priority or "",
                date=email.date
            )
            
            writer.commit()
            logger.debug(f"Indexed email: {email.message_id}")
        
        except Exception as e:
            logger.error(f"Failed to index email {email.message_id}: {e}")
    
    def search(
        self,
        query: str,
        category: Optional[str] = None,
        priority: Optional[str] = None,
        action_required: Optional[bool] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        page: int = 1,
        page_size: int = 50
    ) -> Dict[str, Any]:
        """
        Full-text search across emails
        """
        if not self.ix:
            return {"results": [], "total": 0}
        
        try:
            with self.ix.searcher() as searcher:
                # Parse query
                parser = MultifieldParser(
                    ["subject", "sender", "summary", "body", "tasks"],
                    schema=self.ix.schema
                )
                parsed_query = parser.parse(query)
                
                # Execute search
                results = searcher.search(parsed_query, limit=page * page_size)
                
                # Get email IDs
                email_ids = [int(hit['id']) for hit in results]
                
                # Fetch from database with filters
                db_query = self.db.query(ProcessedEmail).filter(
                    ProcessedEmail.id.in_(email_ids)
                )
                
                if category:
                    db_query = db_query.filter(ProcessedEmail.category == category)
                if priority:
                    db_query = db_query.filter(ProcessedEmail.priority == priority)
                if action_required is not None:
                    db_query = db_query.filter(ProcessedEmail.action_required == action_required)
                if date_from:
                    db_query = db_query.filter(ProcessedEmail.date >= date_from)
                if date_to:
                    db_query = db_query.filter(ProcessedEmail.date <= date_to)
                
                emails = db_query.all()
                
                return {
                    "results": emails,
                    "total": len(emails),
                    "page": page,
                    "page_size": page_size
                }
        
        except Exception as e:
            logger.error(f"Search failed: {e}")
            return {"results": [], "total": 0}
    
    def rebuild_index(self):
        """Rebuild search index from database"""
        if not self.ix:
            return
        
        try:
            logger.info("Rebuilding search index...")
            
            # Clear index
            writer = self.ix.writer()
            writer.commit(mergetype=index.CLEAR)
            
            # Index all emails
            emails = self.db.query(ProcessedEmail).all()
            
            writer = self.ix.writer()
            for email in emails:
                tasks_text = " ".join([task.description for task in email.tasks])
                
                writer.add_document(
                    id=str(email.id),
                    message_id=email.message_id,
                    subject=email.subject or "",
                    sender=email.sender or "",
                    summary=email.summary or "",
                    body=(email.body_plain or "") if email.body_plain else "",
                    tasks=tasks_text,
                    category=email.category or "",
                    priority=email.priority or "",
                    date=email.date
                )
            
            writer.commit()
            logger.info(f"Rebuilt search index with {len(emails)} emails")
        
        except Exception as e:
            logger.error(f"Failed to rebuild index: {e}")
