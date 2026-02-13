"""
IMAP email retrieval service
"""
import logging
from typing import List, Dict, Any, Optional
from email import message_from_bytes
from email.header import decode_header
from email.utils import parsedate_to_datetime
import hashlib
from datetime import datetime

import imapclient
from imapclient import IMAPClient

from src.config import get_settings
from src.utils.logging import get_logger

logger = get_logger(__name__)


class IMAPService:
    """Service for IMAP email operations"""
    
    def __init__(self):
        self.settings = get_settings()
        self.client: Optional[IMAPClient] = None
    
    def connect(self) -> bool:
        """Connect to IMAP server"""
        try:
            self.client = IMAPClient(
                self.settings.imap_host,
                port=self.settings.imap_port,
                ssl=self.settings.imap_use_ssl
            )
            self.client.login(
                self.settings.imap_username,
                self.settings.imap_password
            )
            logger.info(f"Connected to IMAP server: {self.settings.imap_host}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to IMAP server: {e}")
            return False
    
    def disconnect(self):
        """Disconnect from IMAP server"""
        if self.client:
            try:
                self.client.logout()
                logger.info("Disconnected from IMAP server")
            except Exception as e:
                logger.warning(f"Error during IMAP disconnect: {e}")
            finally:
                self.client = None
    
    def __enter__(self):
        """Context manager entry"""
        self.connect()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.disconnect()
    
    def get_unread_emails(self, max_count: Optional[int] = None) -> List[Dict[str, Any]]:
        """Retrieve unread emails from inbox"""
        if not self.client:
            logger.error("IMAP client not connected")
            return []
        
        try:
            # Select inbox
            self.client.select_folder(self.settings.inbox_folder)
            
            # Search for unread messages
            messages = self.client.search(['UNSEEN'])
            
            if not messages:
                logger.info("No unread emails found")
                return []
            
            # Limit number of emails
            if max_count and len(messages) > max_count:
                messages = messages[:max_count]
                logger.info(f"Limited to {max_count} emails")
            
            logger.info(f"Found {len(messages)} unread emails")
            
            # Fetch email data
            emails = []
            for uid, message_data in self.client.fetch(messages, ['RFC822', 'FLAGS']).items():
                try:
                    email_data = self._parse_email(uid, message_data)
                    if email_data:
                        emails.append(email_data)
                except Exception as e:
                    logger.error(f"Failed to parse email UID {uid}: {e}")
                    continue
            
            return emails
            
        except Exception as e:
            logger.error(f"Failed to retrieve unread emails: {e}")
            return []
    
    def _parse_email(self, uid: int, message_data: Dict) -> Optional[Dict[str, Any]]:
        """Parse email message"""
        try:
            raw_email = message_data[b'RFC822']
            msg = message_from_bytes(raw_email)
            
            # Extract headers
            subject = self._decode_header(msg.get('Subject', ''))
            sender = self._decode_header(msg.get('From', ''))
            recipients = self._decode_header(msg.get('To', ''))
            date_str = msg.get('Date')
            message_id = msg.get('Message-ID', f"<generated-{uid}@mailjaeger>")
            
            # Parse date
            email_date = None
            if date_str:
                try:
                    email_date = parsedate_to_datetime(date_str)
                except (ValueError, TypeError) as e:
                    logger.warning(f"Failed to parse date '{date_str}': {e}")
                    email_date = datetime.utcnow()
            else:
                email_date = datetime.utcnow()
            
            # Extract body
            body_plain = ""
            body_html = ""
            
            if msg.is_multipart():
                for part in msg.walk():
                    content_type = part.get_content_type()
                    if content_type == "text/plain":
                        try:
                            body_plain += part.get_payload(decode=True).decode('utf-8', errors='ignore')
                        except (UnicodeDecodeError, AttributeError) as e:
                            logger.warning(f"Failed to decode plain text part: {e}")
                    elif content_type == "text/html":
                        try:
                            body_html += part.get_payload(decode=True).decode('utf-8', errors='ignore')
                        except (UnicodeDecodeError, AttributeError) as e:
                            logger.warning(f"Failed to decode HTML part: {e}")
            else:
                content_type = msg.get_content_type()
                try:
                    payload = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
                    if content_type == "text/plain":
                        body_plain = payload
                    elif content_type == "text/html":
                        body_html = payload
                except (UnicodeDecodeError, AttributeError) as e:
                    logger.warning(f"Failed to decode email body: {e}")
            
            # Calculate integrity hash
            integrity_hash = hashlib.sha256(raw_email).hexdigest()
            
            return {
                'uid': str(uid),
                'message_id': message_id,
                'subject': subject,
                'sender': sender,
                'recipients': recipients,
                'date': email_date,
                'body_plain': body_plain,
                'body_html': body_html,
                'integrity_hash': integrity_hash,
                'raw_email': raw_email
            }
            
        except Exception as e:
            logger.error(f"Failed to parse email: {e}")
            return None
    
    def _decode_header(self, header: str) -> str:
        """Decode email header"""
        if not header:
            return ""
        
        decoded_parts = []
        for part, encoding in decode_header(header):
            if isinstance(part, bytes):
                try:
                    decoded_parts.append(part.decode(encoding or 'utf-8', errors='ignore'))
                except:
                    decoded_parts.append(part.decode('utf-8', errors='ignore'))
            else:
                decoded_parts.append(str(part))
        
        return ''.join(decoded_parts)
    
    def mark_as_read(self, uid: int) -> bool:
        """Mark email as read"""
        if not self.client:
            return False
        
        try:
            self.client.add_flags([uid], [imapclient.SEEN])
            return True
        except Exception as e:
            logger.error(f"Failed to mark email {uid} as read: {e}")
            return False
    
    def move_to_folder(self, uid: int, folder: str) -> bool:
        """Move email to folder"""
        if not self.client:
            return False
        
        try:
            # Ensure folder exists
            self._ensure_folder_exists(folder)
            
            # Move message
            self.client.move([uid], folder)
            logger.debug(f"Moved email {uid} to {folder}")
            return True
        except Exception as e:
            logger.error(f"Failed to move email {uid} to {folder}: {e}")
            return False
    
    def add_flag(self, uid: int) -> bool:
        """Add flag to email"""
        if not self.client:
            return False
        
        try:
            self.client.add_flags([uid], [imapclient.FLAGGED])
            return True
        except Exception as e:
            logger.error(f"Failed to flag email {uid}: {e}")
            return False
    
    def _ensure_folder_exists(self, folder: str):
        """Ensure folder exists, create if needed"""
        try:
            folders = [f[2] for f in self.client.list_folders()]
            if folder not in folders:
                self.client.create_folder(folder)
                logger.info(f"Created folder: {folder}")
        except Exception as e:
            logger.warning(f"Could not ensure folder exists: {e}")
    
    def check_health(self) -> Dict[str, Any]:
        """Check IMAP connection health"""
        try:
            if not self.client:
                self.connect()
            
            if self.client:
                # Try to select inbox
                self.client.select_folder(self.settings.inbox_folder)
                return {
                    "status": "healthy",
                    "connected": True,
                    "message": "IMAP connection is working"
                }
            else:
                return {
                    "status": "unhealthy",
                    "connected": False,
                    "message": "Failed to connect to IMAP server"
                }
        except Exception as e:
            return {
                "status": "unhealthy",
                "connected": False,
                "message": f"IMAP error: {str(e)}"
            }
