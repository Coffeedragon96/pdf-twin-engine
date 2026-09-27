from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Postgres (see x2-backend/worker/CONTRACT.md — the shared job/artifact
    # contract this engine implements as an external processor)
    DB_HOST: str = "localhost"
    DB_PORT: str = "5432"
    DB_USER: str = "x2"
    DB_PASSWORD: str = ""
    DB_NAME: str = "x2"

    # S3-compatible object storage (MinIO locally, AWS S3 in production)
    S3_ENDPOINT: str = ""
    S3_REGION: str = "us-east-1"
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""
    S3_BUCKET_UPLOADS: str = "x2-uploads"
    S3_BUCKET_ARTIFACTS: str = "x2-artifacts"

    # SQS-compatible queue (ElasticMQ locally, AWS SQS in production)
    SQS_ENDPOINT: str = ""
    SQS_REGION: str = "us-east-1"
    SQS_QUEUE_PDF_TWIN: str = "x2-jobs-pdf_twin"

    # Anthropic Claude Vision API — provisioned with the same secret value
    # X2's internal/ai module uses; no code sharing, just the same key.
    ANTHROPIC_API_KEY: str = ""

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
