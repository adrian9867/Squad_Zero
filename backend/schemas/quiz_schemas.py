import os
from typing import List, Optional
from pydantic import BaseModel, Field
from fastapi import HTTPException, UploadFile


# Request/ Response schemas

class QuizGenerateRequest(BaseModel):
    """Validated input for quiz generation"""
    num_questions: int = Field(ge=1, le=25, description="Number of questions (1-25)")
    difficulty: str = Field(pattern="^(easy|medium|hard)$", description="Difficulty level")
    time_limit: int = Field(ge=1, le=180, description="Time limit in minutes (1-180)")
    question_type: str = Field(pattern="^(mcq|short_answer|mixed)$", description="Question type")
    content_focus: str = Field(
        default="both",
        pattern="^(theoretical|practical|both)$",
        description="Content focus"
    )
    note_id: Optional[int] = None
    source_content: Optional[str] = None


class QuizResponse(BaseModel):
    """Quiz generation response — source_content intentionally excluded (large payload)"""
    quiz_id: int
    title: str
    description: str
    total_questions: int
    time_limit: int
    difficulty: str
    questions: List[dict]


class QuizSubmitRequest(BaseModel):
    """Validated input for quiz submission"""
    answers: dict
    time_taken: int = Field(ge=0, description="Time taken in seconds")


class AttemptDetailResponse(BaseModel):
    attempt_id: int
    quiz_title: str
    quiz_description: str
    difficulty: str
    score_percentage: float
    correct_answers: int
    total_questions: int
    time_taken: int
    attempt_date: str
    detailed_results: List[dict]


# File upload validator

class FileUploadValidator:
    """
    Validate uploaded files before extraction.
    Raise HTTPException so FastAPI returns proper 400 responses.
    """

    MAX_FILES: int = 20
    MAX_FILE_SIZE: int = 100 * 1024 * 1024  # 100 MB (matches the quiz UI)
    ALLOWED_EXTENSIONS: set = {
        "pdf", "doc", "docx", "txt", "rtf",
        "xlsx", "xls",
        "ppt", "pptx",
        "jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "md", 
        "epub",
    }

    @classmethod
    def validate(cls, files: List[UploadFile]) -> None:
        """
        Validate file count, size, and extensions.

        Args:
            files: List of uploaded files from the request

        Raises:
            HTTPException 400: if any validation rule is violated
        """
        if len(files) > cls.MAX_FILES:
            raise HTTPException(
                status_code=400,
                detail=f"Too many files. Maximum allowed is {cls.MAX_FILES}.",
            )
        
        # Extension check
        for file in files:
            _, raw_ext = os.path.splitext(file.filename or "")
            ext = raw_ext.lstrip(".").strip().lower()  # strip leading dot + any whitespace
            if ext not in cls.ALLOWED_EXTENSIONS:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"'{file.filename}' has unsupported extension '.{ext}'. "
                        f"Allowed types: {', '.join(sorted(cls.ALLOWED_EXTENSIONS))}"
                    ),
                )

            # Size check
            if hasattr(file, "size") and file.size and file.size > cls.MAX_FILE_SIZE:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"'{file.filename}' exceeds the 25 MB size limit "
                        f"({file.size / (1024 * 1024):.1f} MB)."
                    ),
                )
