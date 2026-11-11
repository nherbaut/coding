import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY", "development-only-change-before-deployment"
)
DEBUG = os.environ.get("DEBUG", "1") == "1"
if not DEBUG and SECRET_KEY == "development-only-change-before-deployment":
    raise RuntimeError("Set DJANGO_SECRET_KEY for deployment")
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(
    ","
)
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corpus",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "corpus.admin_tools.PrivateResponseMiddleware",
    "corpus.proxy_auth.AutheliaAdminMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
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
            ]
        },
    }
]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("SQLITE_PATH", BASE_DIR / "development.sqlite3"),
    }
}
if os.environ.get("POSTGRES_HOST"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "HOST": os.environ["POSTGRES_HOST"],
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
            "NAME": os.environ.get("POSTGRES_DB", "coding"),
            "USER": os.environ.get("POSTGRES_USER", "coding"),
            "PASSWORD": os.environ["POSTGRES_PASSWORD"],
        }
    }
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"
LOGIN_URL = "/admin/login/"
LOGIN_REDIRECT_URL = "/"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
CSRF_TRUSTED_ORIGINS = [
    x for x in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if x
]
SECURE_REFERRER_POLICY = "same-origin"
DATA_UPLOAD_MAX_MEMORY_SIZE = 50 * 1024 * 1024

# Administrator identity is checked for every request when deployed behind Authelia.
AUTHELIA_AUTH_ENABLED = os.environ.get("AUTHELIA_AUTH_ENABLED", "0") == "1"
AUTHELIA_ADMIN_GROUP = os.environ.get("AUTHELIA_ADMIN_GROUP", "coding-admins")
AUTHELIA_PROXY_SECRET = os.environ.get("AUTHELIA_PROXY_SECRET", "")
AUTHELIA_TRUSTED_PROXIES = [
    value.strip()
    for value in os.environ.get("AUTHELIA_TRUSTED_PROXIES", "").split(",")
    if value.strip()
]
if not DEBUG and not AUTHELIA_AUTH_ENABLED:
    raise RuntimeError("Deployed admin access requires Authelia")
if AUTHELIA_AUTH_ENABLED:
    if len(AUTHELIA_PROXY_SECRET) < 32 or not AUTHELIA_TRUSTED_PROXIES:
        raise RuntimeError(
            "Authelia requires a strong proxy secret and trusted proxy addresses"
        )
    AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.RemoteUserBackend"]


# Transactional submission notifications. Credentials are provided through the environment.
EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend"
)
EMAIL_HOST = os.environ.get("SMTP_HOST", "")
EMAIL_PORT = int(os.environ.get("SMTP_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("SMTP_USERNAME", "")
EMAIL_HOST_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
EMAIL_USE_TLS = os.environ.get("SMTP_STARTTLS", "1") == "1"
EMAIL_USE_SSL = os.environ.get("SMTP_TLS", "0") == "1"
EMAIL_TIMEOUT = 15
DEFAULT_FROM_EMAIL = os.environ.get("MAIL_FROM", "no-reply@localhost")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
CASE_EMAIL_ENABLED = os.environ.get("CASE_EMAIL_ENABLED", "0") == "1"
if EMAIL_USE_TLS and EMAIL_USE_SSL:
    raise RuntimeError("Choose SMTP_STARTTLS or SMTP_TLS, not both")
if CASE_EMAIL_ENABLED:
    from django.core.validators import validate_email
    from email.utils import parseaddr
    from urllib.parse import urlsplit

    if not EMAIL_HOST:
        raise RuntimeError("Transactional email requires SMTP_HOST")
    validate_email(parseaddr(DEFAULT_FROM_EMAIL)[1])
    if (
        urlsplit(PUBLIC_BASE_URL).scheme not in ("http", "https")
        or not urlsplit(PUBLIC_BASE_URL).hostname
    ):
        raise RuntimeError("Set PUBLIC_BASE_URL to the website address")

# GitHub App credentials belong to this server, not to study exports.
GITHUB_APP_ID = os.environ.get("GITHUB_APP_ID", "")
GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
GITHUB_PRIVATE_KEY_FILE = os.environ.get("GITHUB_PRIVATE_KEY_FILE", "")
GITHUB_APP_SLUG = os.environ.get("GITHUB_APP_SLUG", "")
GITHUB_IMPORT_MAX_BYTES = 100 * 1024 * 1024
GITHUB_SYNC_LEASE_SECONDS = 900
