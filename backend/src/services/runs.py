import json
import logging

from sqlalchemy import select

from src.config.project import SPEC
from src.constants.enums import Decision, Role, RunStatus
from src.domain.analysis import analyze
from src.models.base import utc_now
from src.models.workflow import AnalysisRun, AuditEvent, Observation, Review
from src.services.errors import BusinessError
from src.services.observations import latest_weekly_observations
from src.services.validation import validated

logger = logging.getLogger(__name__)


def create_run(db, request, actor):
    parameters = validated(SPEC['parameters'], request.parameters, defaults=True)
    # 受理时点：每周只取已完整入库的最高来源版本，并在此刻冻结样本。
    sample = latest_weekly_observations(db)
    if not sample:
        raise BusinessError('请先导入观测记录')
    sample_ids = [item.id for item in sample]
    sample_versions = {item.week: item.version for item in sample}
    item = AnalysisRun(title=request.title, owner=actor, parameters=parameters,
                       observation_ids=sample_ids, sample_versions=sample_versions,
                       last_observation_id=max(sample_ids), sample_size=len(sample_ids))
    db.add(item)
    db.flush()
    db.add(AuditEvent(actor=actor, action='run.created',
                      detail=f'分析 #{item.id}，固化 {item.sample_size} 个交接周的最高版本周报'))
    return item


def visible_runs(db, user, run_id=None):
    query = select(AnalysisRun, Review).outerjoin(Review, Review.run_id == AnalysisRun.id)
    if user['role'] == Role.VIEWER:
        query = query.where(Review.decision == Decision.APPROVED)
    if run_id is not None:
        pair = db.execute(query.where(AnalysisRun.id == run_id)).first()
        if pair is None:
            raise BusinessError('没有可查看的分析结果', 404)
        return pair
    return db.execute(query.order_by(AnalysisRun.id.desc()).limit(100)).all()


def submit_review(db, run_id, request, actor):
    row = db.get(AnalysisRun, run_id, with_for_update=True)
    if row is None:
        raise BusinessError('分析不存在', 404)
    if row.status != RunStatus.SUCCEEDED:
        raise BusinessError('分析算完后才能复核', 409)
    if db.scalar(select(Review).where(Review.run_id == run_id)):
        raise BusinessError('这份分析已完成复核。请发起新分析并重新复核。', 409)
    if row.owner == actor:
        raise BusinessError('分析须由另一位负责人复核', 409)
    review = Review(run_id=run_id, decision=request.decision, note=request.note, reviewer=actor)
    db.add(review)
    db.add(AuditEvent(actor=actor, action=f'review.{request.decision}', detail=f'分析 #{run_id}'))
    db.flush()
    return review


def run_pending(db):
    item = db.scalar(select(AnalysisRun).where(AnalysisRun.status == RunStatus.QUEUED)
                     .order_by(AnalysisRun.id).limit(1).with_for_update(skip_locked=True))
    if item is None:
        return False
    try:
        # 只读取受理时冻结的观测 ID；排队期间新入库或补发的周报不换样本。
        sample = list(db.scalars(select(Observation.payload).where(Observation.id.in_(item.observation_ids))
                                 .order_by(Observation.week)))
        result = analyze(sample, item.parameters)
        if not {'headline', 'summary', 'metrics', 'columns', 'rows'} <= set(result):
            raise RuntimeError('invalid result schema')
        json.dumps(result, allow_nan=False, ensure_ascii=False)
        item.result = result
        item.status = RunStatus.SUCCEEDED
    except ValueError as exc:
        item.error = str(exc)[:600] or '本次观测数据不足以完成计算'
        item.status = RunStatus.FAILED
    except Exception:
        logger.exception('Analysis %s raised unexpectedly', item.id)
        item.error = '分析计算失败，请检查记录和参数后重试'
        item.status = RunStatus.FAILED
    item.finished_at = utc_now()
    return True
