import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from sqlalchemy import text
from scripts.seed_demo import seed
from src.config.database import Base, engine
from src.config.settings import SEED_DEMO
from src.domain.spec import SPEC
from src.middlewares import errors, headers, rate
from src.routes import distribution, identity, registry, runs
from src.utils.wakeup import broker

log = logging.getLogger(__name__)

# create_all 只补缺失的表，既有表的新增列在这里显式补齐。
MIGRATIONS = [
    'ALTER TABLE runs ADD COLUMN IF NOT EXISTS progress JSON',
    'ALTER TABLE runs ADD COLUMN IF NOT EXISTS dataset_checksum VARCHAR(64)',
]


@asynccontextmanager
async def lifespan(_app):
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        for statement in MIGRATIONS:
            connection.execute(text(statement))
    if SEED_DEMO:
        try:
            log.info('Seeded %s', seed())
        except Exception:
            log.exception('Demo seed failed')
    yield
    engine.dispose()


app = FastAPI(title=SPEC['title'], lifespan=lifespan)
errors.install(app)
headers.install(app)
rate.install(app)
for module in (identity, registry, runs):
    app.include_router(module.router)
if SPEC.get('scheduled'):
    app.include_router(distribution.router)


@app.get('/healthz', include_in_schema=False)
def health():
    with engine.connect() as connection:
        connection.execute(text('SELECT 1'))
    broker.ping()
    return {'status': 'ok'}
