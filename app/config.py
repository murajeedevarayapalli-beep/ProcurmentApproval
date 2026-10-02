from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_env: str = "local"
    ai_mode: str = "demo"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    data_dir: str = "./data"
    chroma_collection: str = "procurement_evidence"
    log_level: str = "INFO"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

settings = Settings()
