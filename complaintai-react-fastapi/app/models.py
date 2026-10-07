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
    category: str

class CancelBody(BaseModel):
    reason: str = Field(default="", max_length=2000)

class CsvMappingBody(BaseModel):
    profile_name: str = ""
    title_column: str = ""
    content_columns: list[str] = Field(default_factory=list)
    response_column: str = ""
    category_column: str = ""
    save_mapping: bool = True