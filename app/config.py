from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_BASE_URL: str = "https://yourapp.azure.com"
    SECRET_KEY: str = "change-me"

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

    # GitHub
    GITHUB_APP_ID: str = ""
    GITHUB_APP_PRIVATE_KEY: str = ""
    GITHUB_APP_INSTALLATION_ID: str = ""
    GITHUB_WEBHOOK_SECRET: str = ""
    GITHUB_ORG: str = ""
    GITHUB_CLIENT_ID: str = ""
    GITHUB_CLIENT_SECRET: str = ""

    # GitLab
    GITLAB_WEBHOOK_SECRET: str = ""
    GITLAB_PROJECT_ID: str = ""
    GITLAB_CLIENT_ID: str = ""
    GITLAB_CLIENT_SECRET: str = ""

    # Jira
    JIRA_CLIENT_ID: str = ""
    JIRA_CLIENT_SECRET: str = ""
    JIRA_BASE_URL: str = ""
    JIRA_WEBHOOK_SECRET: str = ""

    # Teams
    BOT_SERVICE_PRINCIPAL_ID: str = ""

    # Storage
    POSTGRES_URL: str = "postgresql+asyncpg://user:password@localhost:5432/activity_tracker"
    MONGODB_URL: str = "mongodb://localhost:27017"
    MONGODB_DB: str = "activity_tracker"
    REDIS_URL: str = "redis://localhost:6379"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
