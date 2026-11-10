import hmac
import json
import logging

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

from assistant.models import parse_kuma
from assistant.pipeline import Deps, run_event

log = logging.getLogger(__name__)


def create_app(deps: Deps) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    secret = deps.settings.webhook_secret.encode()

    @app.post("/webhook/kuma")
    async def kuma_webhook(request: Request, background_tasks: BackgroundTasks):
        given = request.headers.get("X-Webhook-Secret", "").encode()
        if not hmac.compare_digest(given, secret):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        try:
            payload = json.loads(await request.body())
        except ValueError:
            payload = None
        event = parse_kuma(payload) if isinstance(payload, dict) else None
        if event is None:
            log.info("ignored")
            return JSONResponse({"status": "ignored"})
        background_tasks.add_task(run_event, event, deps)
        return JSONResponse({"status": "accepted"}, status_code=202)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app
