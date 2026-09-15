"""
Custom authentication backends for graceful LDAP fallback.
"""
import logging
from django.contrib.auth.backends import ModelBackend
from django_auth_ldap.backend import LDAPBackend

logger = logging.getLogger(__name__)


class GracefulLDAPBackend(LDAPBackend):
    """
    LDAP backend that fails gracefully when LDAP server is unavailable.
    This allows Django to fall back to ModelBackend instead of raising exceptions.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        """
        Authenticate against LDAP, but catch connection errors and return None
        to allow fallback to the next authentication backend.
        """
        try:
            return super().authenticate(request, username, password, **kwargs)
        except Exception as e:
            # Log the LDAP error but don't raise it
            logger.warning(
                f"LDAP authentication failed for user '{username}': {type(e).__name__}: {str(e)}"
            )
            logger.info(
                f"Falling back to Django ModelBackend for user '{username}'"
            )
            # Return None to allow Django to try the next backend
            return None


class FallbackModelBackend(ModelBackend):
    """
    Standard Django ModelBackend with logging for debugging.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        """
        Authenticate against Django's local database.
        """
        logger.debug(f"Attempting Django ModelBackend authentication for user '{username}'")
        user = super().authenticate(request, username, password, **kwargs)
        
        if user:
            logger.info(f"Django ModelBackend authentication successful for user '{username}'")
        else:
            logger.debug(f"Django ModelBackend authentication failed for user '{username}'")
        
        return user
