from fastapi import FastAPI

app = FastAPI(title="Rappi AI Purchasing Agent")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
