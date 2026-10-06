from sqlalchemy import create_engine, text

engine = create_engine("postgresql+psycopg://localhost/shop")


def orders():
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(text("select id, price from orders"))]
