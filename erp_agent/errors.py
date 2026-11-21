class TransientERPError(Exception):
    """The ERP rejected the request before applying it (for example HTTP 429 or 503). Safe to retry."""
