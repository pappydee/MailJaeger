"""
Error handling utilities for MailJaeger
"""


def sanitize_error(e: Exception, debug: bool = False) -> str:
    """
    Sanitize error messages to prevent credential leakage.
    
    In production (debug=False), only returns the exception type name.
    In debug mode, returns the full error message.
    
    Args:
        e: The exception to sanitize
        debug: Whether to return full error details (default: False)
    
    Returns:
        Sanitized error message safe for storage/API responses
    """
    if debug:
        return str(e)
    else:
        # Return only the exception type, no details
        error_type = type(e).__name__
        return error_type if error_type else "UnknownError"
