from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import ValidationError

from app.core.exceptions import MalformedRequestBodyError, NoResponseError
from app.dependencies.auth import AuthDep
from app.handlers.messages_handler import MessagesHandler
from app.processors.claude_ai import ClaudeAIContext
from app.views.messages_view import MessagesRequestView

router = APIRouter()

_handler = MessagesHandler()


@router.post("/messages", response_model=None)
async def create_message(
    request: Request, _: AuthDep
) -> StreamingResponse | JSONResponse:
    """Messages API entry point.

    Owns body parsing and validation explicitly so the OAuth transparent-proxy
    path is in charge of what (if anything) it inspects, rather than letting
    FastAPI validate at the route boundary and 422 upstream-valid shapes.
    """
    raw_body = await request.body()
    view = MessagesRequestView(raw_body)

    try:
        view.parsed  # eager parse so 422-class issues become 400 here, not later
    except ValidationError as exc:
        raise MalformedRequestBodyError(detail=str(exc)) from exc

    context = ClaudeAIContext(
        original_request=request,
        view=view,
    )

    context = await _handler.handle(context)

    if not context.response:
        raise NoResponseError()

    return context.response
