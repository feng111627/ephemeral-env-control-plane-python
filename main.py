import uvicorn
from fastapi import FastAPI

from app.api import build_service, create_router, install_exception_handlers

app = FastAPI(title="ephemeral-env-control-plane")
install_exception_handlers(app)
app.include_router(create_router(build_service()), prefix="/api")


@app.get("/")
def read_root():
    return {"Hello": "World"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
