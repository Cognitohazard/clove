from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse, JSONResponse

from app.core.exceptions import NoResponseError
from app.dependencies.auth import AuthDep
from app.models.claude import MessagesAPIRequest
from app.processors.claude_ai import ClaudeAIContext
from app.processors.claude_ai.pipeline import ClaudeAIPipeline

router = APIRouter()


@router.post("/messages", response_model=None)
async def create_message(
    request: Request, messages_request: MessagesAPIRequest, _: AuthDep
) -> StreamingResponse | JSONResponse:
    context = ClaudeAIContext(
        original_request=request,
        messages_api_request=messages_request,
    )

    context = await ClaudeAIPipeline().process(context)

    if not context.response:
        raise NoResponseError()

    return context.response
