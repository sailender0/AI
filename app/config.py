from pydantic import ConfigDict
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = ConfigDict(env_file=".env", extra="ignore")
    APP_BASE_URL: str = "http://localhost:8000"
    WEBHOOK_BASE_URL: str = ""   # ngrok/public URL for webhook registration; falls back to APP_BASE_URL
    SECRET_KEY: str = ""
    # Comma-separated emails promoted to role=admin at every login (bootstrap +
    # lockout recovery). Promote-only — see app/auth/sso.py:auth_callback.
    ADMIN_EMAILS: str = ""

    # Entra ID / Azure AD
    AZURE_TENANT_ID: str = ""
    AZURE_CLIENT_ID: str = ""
    AZURE_CLIENT_SECRET: str = ""

    # Azure Key Vault
    AZURE_KEYVAULT_URL: str = ""

    # Azure OpenAI
    AZURE_OPENAI_ENDPOINT: str = ""
    AZURE_OPENAI_KEY: str = ""
    AZURE_OPENAI_DEPLOYMENT: str = "gpt-4o"
    # Per-1M-token prices for cost logging. UPDATE these together with the
    # deployment above when you change models. Defaults are gpt-4.1-mini.
    AZURE_OPENAI_PRICE_IN: float = 0.40
    AZURE_OPENAI_PRICE_OUT: float = 1.60

    # GitHub
    GITHUB_APP_ID: str = ""
    GITHUB_APP_SLUG: str = ""          # e.g. "my-activity-tracker"
    GITHUB_APP_PRIVATE_KEY: str = ""
    GITHUB_APP_INSTALLATION_ID: str = ""
    GITHUB_WEBHOOK_SECRET: str = ""
    GITHUB_ORG: str = ""
    GITHUB_CLIENT_ID: str = ""
    GITHUB_CLIENT_SECRET: str = ""

    # GitLab
    GITLAB_WEBHOOK_SECRET: str = ""
    GITLAB_CLIENT_ID: str = ""
    GITLAB_CLIENT_SECRET: str = ""

    # Jira
    JIRA_CLIENT_ID: str = ""
    JIRA_CLIENT_SECRET: str = ""
    JIRA_WEBHOOK_SECRET: str = ""

    # Teams
    BOT_SERVICE_PRINCIPAL_ID: str = ""

    # Storage
    POSTGRES_URL: str = "postgresql+asyncpg://user:password@localhost:5432/activity_tracker"
    MONGODB_URL: str = "mongodb://localhost:27017"
    MONGODB_DB: str = "activity_tracker"
    REDIS_URL: str = "redis://localhost:6379"



settings = Settings()

assert settings.SECRET_KEY, "SECRET_KEY must be set in .env"
