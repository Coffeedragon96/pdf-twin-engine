from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Redis
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379

    # Anthropic Claude Vision API
    ANTHROPIC_API_KEY: str = ""

    # AWS S3 (for glTF artifact storage)
    AWS_ACCESS_KEY_ID:     str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_S3_BUCKET:         str = ""
    AWS_S3_REGION:         str = "us-east-1"

    # Platform webhook callback URL
    PLATFORM_CALLBACK_URL: str = ""

    # App
    APP_ENV: str = "development"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()