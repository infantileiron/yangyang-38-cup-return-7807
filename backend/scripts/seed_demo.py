import json

from sqlalchemy import func, select

from src.domain.analysis import seed
from src.models.base import Base, Session, engine
from src.models.workflow import AnalysisRun, IngestBatch, Observation, Review, Schedule
from src.services.observations import add_observations, clean_observation


def seed_data():
    Base.metadata.create_all(engine)
    rows = [clean_observation(row) for row in seed()]
    with Session.begin() as db:
        if not db.scalar(select(Observation.id)):
            add_observations(db, rows, 'seed', 'observation.imported')
        db.flush()
        return {model.__tablename__: db.scalar(select(func.count(model.id)))
                for model in (Observation, IngestBatch, AnalysisRun, Review, Schedule)}


if __name__ == '__main__':
    print(json.dumps(seed_data(), sort_keys=True))
