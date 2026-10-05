import uvicorn
from fastapi import FastAPI

from app.api import router as env_router

app = FastAPI()
app.include_router(env_router)


@app.get("/")
def read_root():
    return {"Hello": "World"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
