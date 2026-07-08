from pydantic import BaseModel, Field
from typing import Optional
from enum import Enum


class JobStatus(str, Enum):
    PENDING   = "pending"
    PROCESSING = "processing"
    COMPLETED  = "completed"
    FAILED     = "failed"


class BuildingMetadata(BaseModel):
    building_name: str = Field(..., description="Name of the building")
    address:       Optional[str] = Field(None, description="Physical address")
    floors:        Optional[int] = Field(None, description="Expected number of floors")
    scale_meters:  Optional[float] = Field(None, description="Known scale in meters per unit")


class JobSubmitResponse(BaseModel):
    job_id:  str
    status:  JobStatus
    message: str


class JobStatusResponse(BaseModel):
    job_id:      str
    status:      JobStatus
    progress:    Optional[int]   = Field(None, description="Progress 0-100")
    result_url:  Optional[str]   = Field(None, description="glTF/GLB download URL")
    metadata:    Optional[dict]  = Field(None, description="Extracted floor metadata")
    error:       Optional[str]   = Field(None, description="Error message if failed")
    created_at:  Optional[str]   = None
    updated_at:  Optional[str]   = None