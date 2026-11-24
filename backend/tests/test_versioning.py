"""周报版本化入库与复盘快照的端到端测试。

运行：python -m pytest backend/tests -q
使用 SQLite 内存库直测服务层，不依赖 FastAPI、Redis 或外部数据库。
"""
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from src.domain import analysis
from src.models.base import Base
from src.models.workflow import AnalysisRun, IngestBatch, Observation
from src.services.errors import BusinessError
from src.services.observations import add_observations, latest_weekly_observations
from src.services.runs import create_run, run_pending


@pytest.fixture()
def db_session():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    with Session() as db:
        yield db


def row(week, version, returns=80, delay=7.0):
    return {'week': week, 'version': version, 'returns': returns, 'delay': delay}


W1, W2, W3 = '2026-09-07', '2026-09-14', '2026-09-21'


def seed_twelve_weeks(db, version=1):
    # 构造上线前后各 6 个连续周，满足算法前提。
    from datetime import date, timedelta
    base = date(2026, 9, 7)
    rows = []
    for offset in range(-6, 6):
        week = (base + timedelta(weeks=offset)).isoformat()
        rows.append(row(week, version, returns=70 + offset, delay=10 - offset * 0.2))
    add_observations(db, rows, 'analyst', 'observation.imported')
    return rows


# 1. 按来源版本入库：同一周可以存在多个版本行。
def test_multiple_versions_of_same_week_coexist(db_session):
    add_observations(db_session, [row(W1, 1, delay=7.0)], 'a', 'observation.imported')
    add_observations(db_session, [row(W1, 2, delay=6.4)], 'a', 'observation.imported')
    versions = list(db_session.scalars(select(Observation.version).where(Observation.week == W1)))
    assert sorted(versions) == [1, 2]


# 2. 晚到的旧记录不得覆盖新版：v2 先到、v1 晚到时，v2 数值原样保留。
def test_late_old_version_does_not_overwrite_new(db_session):
    add_observations(db_session, [row(W1, 2, delay=6.4)], 'a', 'observation.imported')
    add_observations(db_session, [row(W1, 1, delay=7.0)], 'a', 'observation.imported')
    latest = latest_weekly_observations(db_session)
    assert len(latest) == 1
    assert latest[0].version == 2
    assert latest[0].payload['delay'] == 6.4


# 3a. 同周同版报不同数值（与库内冲突）：整批拒收，本批一条都不入库。
def test_conflict_with_stored_value_rejects_whole_batch(db_session):
    add_observations(db_session, [row(W1, 1, delay=7.0), row(W2, 1, delay=8.0)],
                     'a', 'observation.imported')
    db_session.commit()
    before = db_session.scalar(select(func.count(Observation.id)))
    with pytest.raises(BusinessError, match='整批拒收'):
        add_observations(db_session, [row(W1, 1, delay=9.9), row(W3, 1, delay=5.0)],
                         'a', 'observation.imported')
    db_session.rollback()
    assert db_session.scalar(select(func.count(Observation.id))) == before
    assert db_session.scalar(select(func.count(Observation.id)).where(Observation.week == W3)) == 0


# 3b. 同周同版在同一批内报不同数值：整批拒收。
def test_conflict_inside_batch_rejects_whole_batch(db_session):
    with pytest.raises(BusinessError, match='整批拒收'):
        add_observations(db_session, [row(W1, 1, delay=7.0), row(W1, 1, delay=7.8)],
                         'a', 'observation.imported')
    db_session.rollback()
    assert db_session.scalar(select(func.count(Observation.id))) == 0


# 4. 重跑同一批：幂等，不新增版本、不新增批次。
def test_rerun_same_batch_is_idempotent(db_session):
    payload = [row(W1, 1, delay=7.0), row(W2, 1, delay=8.0)]
    first = add_observations(db_session, payload, 'a', 'observation.imported')
    second = add_observations(db_session, payload, 'a', 'observation.imported')
    assert first['added'] == 2 and second['added'] == 0 and second['rerun'] is True
    assert first['batch_id'] == second['batch_id']
    assert db_session.scalar(select(func.count(Observation.id))) == 2
    assert db_session.scalar(select(func.count(IngestBatch.id))) == 1


# 4b. 同周同版同值单独重报：跳过，不新增版本。
def test_same_value_same_version_is_skipped(db_session):
    add_observations(db_session, [row(W1, 1, delay=7.0)], 'a', 'observation.imported')
    outcome = add_observations(db_session, [row(W1, 1, delay=7.0)], 'a', 'observation.imported')
    assert outcome['added'] == 0
    assert db_session.scalar(select(func.count(Observation.id)).where(Observation.week == W1)) == 1


# 5. 收补报照常受理，复盘只用受理时已入库的最高版本。
def test_analysis_uses_highest_version_at_acceptance(db_session):
    seed_twelve_weeks(db_session)
    run = create_run(db_session, _request('第一次复盘'), 'analyst')
    assert run.sample_versions[W1] == 1

    # 上线当周补发 v2，新复盘采用 v2，旧复盘不受影响。
    add_observations(db_session, [row(W1, 2, returns=90, delay=6.1)], 'a', 'observation.imported')
    rerun = create_run(db_session, _request('补报后复盘'), 'analyst')
    assert rerun.sample_versions[W1] == 2
    assert run.sample_versions[W1] == 1  # 历史快照不被改写。
    assert rerun.observation_ids != run.observation_ids


# 6. 排队等算时不换样本：创建后补发的周报不影响已入队任务。
def test_queued_run_keeps_frozen_sample(db_session):
    seed_twelve_weeks(db_session)
    queued = create_run(db_session, _request('排队中的复盘'), 'analyst')
    frozen_ids = list(queued.observation_ids)

    # 入队之后才到达的补报。
    add_observations(db_session, [row(W1, 2, returns=90, delay=6.1)], 'a', 'observation.imported')
    assert run_pending(db_session) is True

    done = db_session.get(AnalysisRun, queued.id)
    assert list(done.observation_ids) == frozen_ids
    assert done.sample_versions[W1] == 1
    # 计算结果明细中的版本与冻结快照一致。
    weekly = [item for item in done.result['rows'] if item.get('week') == W1]
    assert weekly and weekly[0]['version'] == 1


# 7. 复盘结果可查到当时每周用了哪个版本（含导出 CSV 列）。
def test_result_and_export_carry_version_trace(db_session):
    seed_twelve_weeks(db_session)
    add_observations(db_session, [row(W1, 2, returns=90, delay=6.1)], 'a', 'observation.imported')
    run = create_run(db_session, _request('可追溯复盘'), 'analyst')
    run_pending(db_session)
    done = db_session.get(AnalysisRun, run.id)

    assert set(done.sample_versions.values()) <= {1, 2}
    assert done.sample_versions[W1] == 2
    version_column = next(col for col in done.result['columns'] if col['key'] == 'version')
    assert version_column['label'] == '来源版本'
    traced = {item['week']: item['version']
              for item in done.result['rows'] if item.get('returns') is not None}
    assert traced == done.sample_versions


def test_analyze_still_runs_with_versioned_rows(db_session):
    rows = seed_twelve_weeks(db_session)
    result = analysis.analyze(rows, {'start': '2026-09-07', 'lag': 2})
    assert result['headline']
    assert any(row.get('version') == 1 for row in result['rows'])


class _Request:
    def __init__(self, title):
        self.title = title
        self.parameters = {}


def _request(title):
    return _Request(title)
