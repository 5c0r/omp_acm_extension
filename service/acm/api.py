from fastapi import FastAPI

app = FastAPI(title="ACM service")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
