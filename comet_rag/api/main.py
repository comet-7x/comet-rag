import os

import dotenv
from fastapi import Depends, FastAPI

from comet_rag.api.lifespan import lifespan
from comet_rag.api.middleware import TraceMiddleware, get_trace_id
from comet_rag.api.routes import admin, search

app = FastAPI(
    title="Comet-RAG", lifespan=lifespan, dependencies=[Depends(get_trace_id)]
)


app.include_router(search.router)
app.include_router(admin.router)
app.add_middleware(TraceMiddleware)


@app.get("/")
async def root():
    return {"message": "Comet-RAG API"}


if __name__ == "__main__":
    import uvicorn

    dotenv.load_dotenv()
    uvicorn.run(
        app, host=os.environ["COMET_RAG_HOST"], port=int(os.environ["COMET_RAG_PORT"])
    )
