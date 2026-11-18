"""
Django settings for the University of Ibadan Employee Payroll Management System.

All secrets/hosts come from environment variables (see .env.example).
"""
import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and value in (None, ""):
        raise ImproperlyConfigured(f"Missing required environment variable: {name}")
    return value


def env_bool(name, default=False):
    return str(os.environ.get(name, str(default))).strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------
DEBUG = env_bool("DJANGO_DEBUG", False)
# A hard failure in production if no secret key is supplied.
SECRET_KEY = env("DJANGO_SECRET_KEY", "insecure-dev-only-key" if DEBUG else None, required=True)
ALLOWED_HOSTS = [h.strip() for h in env("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",  # enables refresh-token revocation (logout)
    "drf_spectacular",
    "simple_history",  # change history (audit trail) for payroll/staff/user data
    # Local
    "accounts",
    "staff",
    "payroll",
    "audit",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",  # serves STATIC_ROOT (admin CSS/JS) under gunicorn
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Lets history rows record WHO made a change (admin session or API JWT user).
    "simple_history.middleware.HistoryRequestMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

AUTH_USER_MODEL = "accounts.User"

# --------------------------------------------------------------------------
# Database (PostgreSQL by default; sqlite is only supported for local tests)
# --------------------------------------------------------------------------
_db_engine = env("DB_ENGINE", "django.db.backends.postgresql")
if _db_engine.endswith("sqlite3"):
    DATABASES = {"default": {"ENGINE": _db_engine, "NAME": env("DB_NAME", str(BASE_DIR / "db.sqlite3"))}}
else:
    DATABASES = {
        "default": {
            "ENGINE": _db_engine,
            "NAME": env("DB_NAME", "uipayroll"),
            "USER": env("DB_USER", "uipayroll"),
            "PASSWORD": env("DB_PASSWORD", "", required=not DEBUG),
            "HOST": env("DB_HOST", "127.0.0.1"),
            "PORT": env("DB_PORT", "5432"),
            "CONN_MAX_AGE": 60,
            "ATOMIC_REQUESTS": False,  # transactions are explicit where needed
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --------------------------------------------------------------------------
# Internationalisation
# --------------------------------------------------------------------------
LANGUAGE_CODE = "en-gb"
TIME_ZONE = "Africa/Lagos"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"  # filled by `python manage.py collectstatic` on deploy
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}

# --------------------------------------------------------------------------
# Django REST Framework
# --------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ("rest_framework_simplejwt.authentication.JWTAuthentication",),
    # Deny by default: every endpoint requires a valid JWT unless it opts out.
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_THROTTLE_CLASSES": (
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ),
    "DEFAULT_THROTTLE_RATES": {
        "anon": "30/min",
        "user": "300/min",
        "login": "10/min",
        "payslip": "60/hour",
    },
    "DEFAULT_RENDERER_CLASSES": (
        ("rest_framework.renderers.JSONRenderer", "rest_framework.renderers.BrowsableAPIRenderer")
        if DEBUG
        else ("rest_framework.renderers.JSONRenderer",)
    ),
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=1),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    # last_login is updated by accounts.views.LoginView (without writing a
    # change-history row on every login; logins go to the activity log instead).
    "UPDATE_LAST_LOGIN": False,
    "ALGORITHM": "HS256",
    "SIGNING_KEY": SECRET_KEY,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

SPECTACULAR_SETTINGS = {
    "TITLE": "University of Ibadan - Employee Payroll Management API",
    "DESCRIPTION": (
        "Role-based payroll API: salary scales (CONUASS/CONTISS/CONPCASS), allowances, "
        "CPS pension, PAYE (Nigeria Tax Act 2025 bands), batch processing and PDF payslips."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SERVE_PERMISSIONS": (
        ["rest_framework.permissions.AllowAny"] if DEBUG else ["rest_framework.permissions.IsAdminUser"]
    ),
    "COMPONENT_SPLIT_REQUEST": True,
    # Stable, readable enum names in the OpenAPI schema.
    "ENUM_NAME_OVERRIDES": {
        "SalaryScaleEnum": "staff.models.SalaryScaleType",
        "EmploymentTypeEnum": "staff.models.EmploymentType",
        "AllowanceEmploymentTypeEnum": [("ALL", "All staff"), ("ACADEMIC", "Academic"), ("NON_ACADEMIC", "Non-Academic")],
    },
}

# --------------------------------------------------------------------------
# Celery + Redis
# --------------------------------------------------------------------------
CELERY_BROKER_URL = env("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", "redis://127.0.0.1:6379/1")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_ACKS_LATE = True  # re-deliver if a worker dies mid-task (task is idempotent)
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_SOFT_TIME_LIMIT = 60 * 30
CELERY_TASK_TIME_LIMIT = 60 * 35
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
# Dev convenience: run tasks inline (no Redis/worker). NEVER enable in production.
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_EAGER", False)
CELERY_TASK_EAGER_PROPAGATES = CELERY_TASK_ALWAYS_EAGER
# Only used if a beat process runs (`celery -A config beat`); cron + the
# `fail_stale_batches` management command is an equivalent alternative.
CELERY_BEAT_SCHEDULE = {
    "fail-stale-payroll-batches": {"task": "payroll.fail_stale_batches", "schedule": 15 * 60},
}

# --------------------------------------------------------------------------
# Email (payroll report dispatch)
# --------------------------------------------------------------------------
EMAIL_BACKEND = (
    "django.core.mail.backends.console.EmailBackend"
    if DEBUG
    else "django.core.mail.backends.smtp.EmailBackend"
)
EMAIL_HOST = env("EMAIL_HOST", "localhost")
EMAIL_PORT = int(env("EMAIL_PORT", "587"))
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "payroll@localhost")

# --------------------------------------------------------------------------
# Payroll business rules (statutory rates live here, NOT scattered in code)
# --------------------------------------------------------------------------
PAYROLL = {
    "INSTITUTION_NAME": "University of Ibadan",
    "CURRENCY": "NGN",
    # Pension Reform Act 2014: employee >= 8%, employer >= 10% of monthly
    # emoluments (basic + housing + transport).
    "PENSION_EMPLOYEE_RATE": Decimal("0.08"),
    "PENSION_EMPLOYER_RATE": Decimal("0.10"),
    # Nigeria Tax Act 2025 (effective 1 Jan 2026): rent relief replaces CRA.
    "RENT_RELIEF_RATE": Decimal("0.20"),
    "RENT_RELIEF_CAP": Decimal("500000.00"),
    "BATCH_CHUNK_SIZE": 500,
    "NOTIFY_STAFF_BY_EMAIL": env_bool("PAYROLL_NOTIFY_STAFF", True),
}

# --------------------------------------------------------------------------
# Audit trail
# --------------------------------------------------------------------------
# History is read-only evidence: no one-click "revert" in the admin (a revert would
# bypass the API's business-rule validation). Make corrections as normal edits.
SIMPLE_HISTORY_REVERT_DISABLED = True

# --------------------------------------------------------------------------
# Production hardening
# --------------------------------------------------------------------------
if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"std": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "std"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
