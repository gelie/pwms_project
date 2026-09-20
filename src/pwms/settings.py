"""
Django settings for pwms project.

For more information on this file, see
https://docs.djangoproject.com/en/6.1/topics/settings/

For the full list of settings and their values, see
https://docs.djangoproject.com/en/6.1/ref/settings/
"""

from pathlib import Path

import ldap
from decouple import config
from django_auth_ldap.config import LDAPSearch

# Repo root (manage.py, .env, logs/, docs/ live here; templates/ and static/
# live inside the app package at src/pwms/).
# src/pwms/settings.py -> parents[0]=src/pwms, [1]=src, [2]=repo root.
BASE_DIR = Path(__file__).resolve().parents[2]

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/6.1/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = config("SECRET_KEY")

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

ALLOWED_HOSTS = []

# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "auditlog",
    "background_task",
    "rest_framework",
    "drf_spectacular",
    "chartjs",
    "django_flatpickr",
    "lucide",
    "mptt",
    "django_htmx",
    # "django_extensions",
    "pwms",  # ← custom app config for auditlog wiring
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Login-required-by-default for the site. Public routes opt out with the
    # login_not_required decorator (see the home view); API/admin namespaces
    # manage their own authentication (see pwms.middleware).
    "pwms.middleware.SiteLoginRequiredMiddleware",
    "auditlog.middleware.AuditlogMiddleware",  # Add after AuthenticationMiddleware
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
]

# Django REST Framework
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.BasicAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    # OpenAPI schema generation (drf-spectacular)
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

# drf-spectacular (OpenAPI 3 + Swagger UI / ReDoc)
SPECTACULAR_SETTINGS = {
    "TITLE": "PWMS Workflow Audit API",
    "DESCRIPTION": (
        "API for parliamentary workflow instances: auditlog CRUD history and "
        "domain TransitionLog events."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
}

ROOT_URLCONF = "pwms.root_urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # App templates dir is also on the *filesystem* loader (checked before
        # app directories), so admin template overrides in
        # src/pwms/templates/admin/ win over django.contrib.admin's defaults.
        "DIRS": [BASE_DIR / "src" / "pwms" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                # Powers the dynamic "Create a workflow" nav menu (creatable types).
                "pwms.navigation.navigation",
                # Unread count + recent alerts for the bell in the site chrome.
                "pwms.notifications.alerts",
            ],
            "builtins": ["lucide.templatetags.lucide"],
        },
    },
]

WSGI_APPLICATION = "pwms.wsgi.application"

# Database
# https://docs.djangoproject.com/en/6.1/ref/settings/#databases

DATABASES = {
    # "default": {
    #     "ENGINE": "django.db.backends.sqlite3",
    #     "NAME": BASE_DIR / "db.sqlite3",
    # }
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": config("PG_DB"),
        "USER": config("PG_USERNAME"),
        "PASSWORD": config("PG_PASSWORD"),
        "HOST": config("PG_HOST"),
        "PORT": config("PG_PORT"),
        "DISABLE_SERVER_SIDE_CURSORS": True,
    }
}

# Password validation
# https://docs.djangoproject.com/en/6.1/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

# Internationalization
# https://docs.djangoproject.com/en/6.1/topics/i18n/

LANGUAGE_CODE = "en-za"

TIME_ZONE = "Africa/Johannesburg"

USE_I18N = True

USE_TZ = True

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.1/howto/static-files/

STATIC_URL = "static/"
# Static lives in the app package (src/pwms/static) and is auto-discovered.
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_ROOT = BASE_DIR / "media"
MEDIA_URL = "media/"

# Date/time pickers (django-flatpickr).
# https://django-flatpickr.readthedocs.io/
# The flatpickr build and the package's own glue script are vendored under
# static/vendor/flatpickr, so the pickers never depend on a CDN at runtime.
# Both keys name that one directory because the package builds every media URL
# by appending a fixed file name to them.
DJANGO_FLATPICKR = {
    "flatpickr_cdn_url": "vendor/flatpickr/",  # + flatpickr.min.{js,css}
    "app_static_url": "vendor/flatpickr/",  # + js/django-flatpickr.js
}

# Email
# https://docs.djangoproject.com/en/6.1/topics/email/#topic-email-configuration

# Development delivery: outbound alerts are printed to the console rather than
# sent (see pwms.notifications). Swapping in the SMTP backend is the only change
# needed to start delivering them for real.
#
# Alert email is *queued*, not sent inline, so `manage.py process_tasks` (the
# django-background-tasks worker configured below) has to be running for
# anything to go out. That worker is also what retries a transient failure.
MAILERS = {
    "default": {
        "BACKEND": "django.core.mail.backends.console.EmailBackend",
    },
}

# Sender for outbound alerts, and the base URL used to make the links in those
# alerts absolute (the in-app bell uses site-relative paths). Both default to
# values that suit a local runserver.
DEFAULT_FROM_EMAIL = config("DEFAULT_FROM_EMAIL", default="noreply@parliament.gov.za")
PWMS_BASE_URL = config("PWMS_BASE_URL", default="http://localhost:8000")

# For Production Server
# EMAIL_BACKEND = config(
#     "EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend"
# )
# EMAIL_HOST = config("EMAIL_HOST", default="localhost")
# DEFAULT_FROM_EMAIL = config("DEFAULT_FROM_EMAIL", default="noreply@parliament.gov.za")

# Authentication backends
# Active Directory first: credentials live in the directory. Order matters -
# Django tries each backend until one returns a user, so LDAP gets the first
# look at every password and the local backend catches whatever it cannot serve
# (identities with no AD account, and a break-glass superuser while the
# directory is unreachable). Keep both: LDAPBackend is not a ModelBackend, so
# `is_superuser` and staff permissions are only answered by the second entry.
AUTHENTICATION_BACKENDS = [
    "pwms.backends.GracefulLDAPBackend",  # Active Directory, stands aside if down
    "pwms.backends.FallbackModelBackend",  # local database fallback with logging
]

# Custom user model
AUTH_USER_MODEL = "pwms.User"

# Login URLs
LOGIN_URL = "/pwms/login/"
LOGIN_REDIRECT_URL = "/pwms/"
LOGOUT_REDIRECT_URL = "/pwms/login/"

# Identity-number encryption keys (see pwms.models.users.User.set_idno).
#
# An ID number is never stored in plain text: `idno_hmac` is a keyed SHA-256
# digest that gives the row a unique, searchable key, and `idno_encrypted` holds
# the recoverable value.
#
# Both are read from the environment and neither has a default. That is
# deliberate. The model reads them with `getattr(settings, ..., None)`, so an
# undeclared setting turns `set_idno` into a silent no-op: every row keeps an
# empty `idno_hmac`, nothing is encrypted, and `sync_users_oracle`'s deactivation
# pass is left matching against an empty candidate set. Failing loudly at
# startup is the lesser evil.
IDNO_HMAC_KEY = config("IDNO_HMAC_KEY")
IDNO_ENC_KEY = config("IDNO_ENC_KEY")

# Background tasks configuration
MAX_ATTEMPTS = 3
BACKGROUND_TASK_RUN_ASYNC = True

# Background tasks settings
BACKGROUND_TASK_ASYNC_THREADS = 4

# Background task priority
BACKGROUND_TASK_PRIORITY_ORDERING = "DESC"

# File upload settings
FILE_UPLOAD_MAX_MEMORY_SIZE = 50 * 1024 * 1024  # 50MB
DATA_UPLOAD_MAX_MEMORY_SIZE = 50 * 1024 * 1024  # 50MB

# Basic logging configuration
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,  # ← very important!
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
        },
        "file": {
            "class": "logging.FileHandler",
            # the log file will be created in the logs directory
            "filename": LOG_DIR / "pwms.log",
        },
    },
    "loggers": {
        "": {  # ← root logger = catch-all
            "handlers": ["file", "console"],
            "level": "INFO",  # or 'DEBUG' if you want even more
        },
        "django": {  # optional – keep Django quieter
            "level": "WARNING",
            "handlers": ["file", "console"],
            "propagate": False,
        },
    },
}

# SharePoint Graph API Configuration
SHAREPOINT_CLIENT_ID = config("CLIENT_ID", default="")
SHAREPOINT_CLIENT_SECRET = config("CLIENT_SECRET", default="")
SHAREPOINT_TENANT_ID = config("TENANT_ID", default="")
SHAREPOINT_TOKEN_URL = (
    f"https://login.microsoftonline.com/{SHAREPOINT_TENANT_ID}/oauth2/v2.0/token"
)
SHAREPOINT_SCOPE = "https://graph.microsoft.com/.default"

# Workflow diagram paths
WORKFLOW_DIAGRAM_OUTPUT_DIR = BASE_DIR / "src/pwms/diagrams"
WORKFLOW_DIAGRAM_DIRS = [
    WORKFLOW_DIAGRAM_OUTPUT_DIR,
    BASE_DIR / "diagrams",
]
# Mermaid Markdown exports land next to the written docs so they render inline
# in GitHub and VS Code. Override per run with `--mermaid-dir` / `--no-mermaid`.
WORKFLOW_DIAGRAM_DOCS_DIR = BASE_DIR / "src/pwms/docs"

# === LDAP / Active Directory authentication ===
#
# Active Directory holds the credentials; PWMS holds the identity record that
# sync_users_oracle maintains. Authentication only - nothing below grants
# authorisation, which stays with Role/GroupMembership (pwms.models.permissions).
#
# Every value is optional so the project still boots and the test suite still
# runs without a directory: pwms.backends.GracefulLDAPBackend returns None
# immediately unless a server URI *and* a user search are configured, and login
# then falls through to the local backend.
#
# Deliberately NOT configured:
#   * AUTH_LDAP_MIRROR_GROUPS / AUTH_LDAP_FIND_GROUP_PERMS - these mirror AD
#     groups into django.contrib.auth.Group and load permissions from them.
#     PWMS authorises through Role/GroupMembership instead, so mirroring would
#     fill auth_group with rows nothing reads and add a directory round trip to
#     permission checks that do not consult it.
#   * AUTH_LDAP_ALWAYS_UPDATE_USER - names, emails and memberships are owned by
#     the nightly ERP sync, so refreshing them from AD at every login would have
#     the two systems overwriting each other. AD populates a user only when it
#     creates the account.
#   * AUTH_LDAP_USER_ATTR_MAP["username"] - the ERP owns this field (the sync
#     writes the lower-cased Oracle username). Letting AD rewrite it on login
#     would rename an ERP-owned row, so the backend matches sAMAccountName to it
#     case-insensitively instead: pwms.backends.GracefulLDAPBackend.
ldap.set_option(
    ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_ALLOW
)  # matches the per-connection option

# LDAP Server Configuration
AUTH_LDAP_SERVER_URI = config("AUTH_LDAP_SERVER_URI", default="")
AUTH_LDAP_BIND_DN = config("AUTH_LDAP_BIND_DN", default="")
AUTH_LDAP_BIND_PASSWORD = config("AUTH_LDAP_BIND_PASSWORD", default="")
AUTH_LDAP_BASE_DN = config("AUTH_LDAP_BASE_DN", default="")

AUTH_LDAP_ALWAYS_UPDATE_USER = False
# Encrypt the connection with STARTTLS rather than an ldaps:// URI. Off by
# default: plain ldap:// is what the internal server is reached over.
AUTH_LDAP_START_TLS = config("AUTH_LDAP_START_TLS", default=False, cast=bool)
# Cache the username -> DN search, which is otherwise an extra round trip on
# every login. Only the lookup is cached; the password is always checked against
# the directory.
AUTH_LDAP_CACHE_TIMEOUT = 3600
# No LDAP-groups-only permission loading, for the reason given above.
AUTH_LDAP_AUTHORIZE_ALL_USERS = False
AUTH_LDAP_FIND_GROUP_PERMS = False

# === Essential options for Active Directory ===
AUTH_LDAP_CONNECTION_OPTIONS = {
    ldap.OPT_REFERRALS: 0,  # AD referrals break everything if not disabled
    # The sign-in form waits on this connection, so fail fast and let the local
    # backend take over instead of tying up a worker.
    ldap.OPT_NETWORK_TIMEOUT: 3,
    ldap.OPT_TIMEOUT: 3,  # Overall operation timeout
    # This tells OpenSSL/python-ldap to skip CA verification
    # Perfectly safe when you fully control both ends (internal AD)
    ldap.OPT_X_TLS_REQUIRE_CERT: ldap.OPT_X_TLS_ALLOW,
}

if AUTH_LDAP_SERVER_URI and AUTH_LDAP_BASE_DN:
    # Finding a user's DN requires a search base. The filter accepts the account
    # name, the user principal name and the mail address, because people sign in
    # with whichever of the three they know; all three resolve to the same
    # directory entry.
    AUTH_LDAP_USER_SEARCH = LDAPSearch(
        AUTH_LDAP_BASE_DN,
        ldap.SCOPE_SUBTREE,
        "(|(sAMAccountName=%(user)s)(userPrincipalName=%(user)s)(mail=%(user)s))",
    )

    # Attributes AD owns for a *newly created* account. AD does not populate
    # every attribute on every account, and django-auth-ldap logs a warning and
    # leaves the field alone when one is missing, so this cannot fail a login.
    AUTH_LDAP_USER_ATTR_MAP = {
        "first_name": "givenName",
        "last_name": "sn",
        "email": "mail",
    }
