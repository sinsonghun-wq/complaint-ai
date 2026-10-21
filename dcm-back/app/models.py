from typing import Any
from pydantic import BaseModel, Field

class LoginBody(BaseModel):
    username: str
    password: str

class SignupBody(LoginBody):
    display_name: str = ""

class ComplaintBody(BaseModel):
    title: str = ""
    content: str = ""

class BatchBody(BaseModel):
    complaints: list[dict[str, Any]] = Field(default_factory=list)

class PasswordBody(BaseModel):
    password: str

class StatusBody(BaseModel):
    status: str

class ResponseBody(BaseModel):
    content: str

class UpdateComplaintBody(BaseModel):
    title: str = ""
    content: str = ""

class TransferBody(BaseModel):
    department_id: int = Field(strict=True, ge=1, le=7)

class CancelBody(BaseModel):
    reason: str = Field(default="", max_length=2000)

class CsvMappingBody(BaseModel):
    profile_name: str = ""
    title_column: str = ""
    content_columns: list[str] = Field(default_factory=list)
    response_column: str = ""
    category_column: str = ""
    save_mapping: bool = True

class ChatResponseBody(BaseModel):
    code: int
    message: str = None
    result: list = []  

class ChatRequestBody(BaseModel):
    content: str
    agent_mode:int=0

class DepartmentDocumentBody(BaseModel):
    document_id: str | None = None
    department_id: int
    title: str
    original_name: str
    version: int | None = None
    storage_path: str
    content: str
    embedding: list[float] | None = None
