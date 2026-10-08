from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel, Field

from .pipeline import AdaptiveHierarchicalPipeline


class QuestionRequest(BaseModel):
    question: str = Field(min_length=3, max_length=4000)


def create_app(pipeline: AdaptiveHierarchicalPipeline) -> FastAPI:
    app = FastAPI(title="EDAHR Scientific QA", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ready"}

    @app.post("/answer")
    def answer(request: QuestionRequest) -> dict:
        return pipeline.answer(request.question).to_dict()

    return app
