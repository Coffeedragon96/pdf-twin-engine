from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router

# Initialize the FastAPI application for the PDF to Twin microservice
app = FastAPI(
    title="PDF to Twin Conversion Engine",
    description="Microservice for converting 2D floor plan PDFs into 3D Digital Twin glTF models.",
    version="1.0.0"
)

# Configure CORS for external platform communication
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include job processing routes
app.include_router(router, prefix="/api/v1")

@app.get("/health", tags=["System"])
async def health_check():
    """
    Verify the health status of the microservice.
    Returns the current operational status.
    """
    return {"status": "healthy", "service": "pdf-twin-engine"}