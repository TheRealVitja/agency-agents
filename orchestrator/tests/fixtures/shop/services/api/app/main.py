from fastapi import FastAPI
from shop_core import pricing

from app.db import orders

app = FastAPI()


@app.get("/orders")
def list_orders():
    rows = orders()
    return {"orders": rows, "total": pricing.total([r["price"] for r in rows])}
