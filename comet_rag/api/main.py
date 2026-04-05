import uvicorn
from fastapi import Depends, FastAPI

from comet_rag.api.lifespan import lifespan
from comet_rag.api.middleware import TraceMiddleware, get_trace_id
from comet_rag.api.routes import admin, search
from comet_rag.config.settings import get_config

config = get_config()

app = FastAPI(
    title=config.server_config.app_name,
    lifespan=lifespan,
    dependencies=[Depends(get_trace_id)],
)


app.include_router(search.router)
app.include_router(admin.router)
app.add_middleware(TraceMiddleware)


@app.get("/")
async def root():
    return {"message": "Comet-RAG API"}


if __name__ == "__main__":
    uvicorn.run(app, host=config.server_config.host, port=config.server_config.port)
