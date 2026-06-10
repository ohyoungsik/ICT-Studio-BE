from fastapi import FastAPI

app = FastAPI()

@app.post("/api/auth/signup")
def signup():
    return {"message": "Hello, World!"}


@app.post("/api/auth/login")
def login():
    return {"message": "Hello, World!"}

@app.post("/api/auth/logout")
def logout():
    return {"message": "Hello, World!"}