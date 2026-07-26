from fastapi import FastAPI

from app.api.routes.dashboard import router as dashboard_router
from app.api.routes.github_webhooks import router as github_webhooks_router
from app.api.routes.health import router as health_router
from app.api.routes.repositories import router as repositories_router
from app.api.routes.tasks import router as tasks_router
from app.core.logging import configure_logging
from app.db.session import init_db
from codecairn import __version__


configure_logging()

app = FastAPI(title="CodeCairn", version=__version__)


@app.on_event("startup")
def on_startup() -> None:
    init_db()


app.include_router(health_router)
app.include_router(dashboard_router)
app.include_router(github_webhooks_router)
app.include_router(tasks_router)
app.include_router(repositories_router)
