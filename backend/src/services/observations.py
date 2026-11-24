import csv
import hashlib
import io
import json

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from src.config.project import SPEC
from src.domain import analysis
from src.models.workflow import AuditEvent, IngestBatch, Observation
from src.services.errors import BusinessError
from src.services.validation import validated


MAX_OBSERVATIONS = 5000


def clean_observation(raw):
    row = validated(SPEC['fields'], raw)
    try:
        if hasattr(analysis, 'validate'):
            analysis.validate(row)
    except ValueError as exc:
        raise BusinessError(str(exc)) from exc
    return row


def content_key(row):
    return 'data:' + hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def batch_key(rows):
    body = json.dumps(rows, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return 'batch:' + hashlib.sha256(body.encode()).hexdigest()


def parse_csv(raw):
    if len(raw) > 512 * 1024:
        raise BusinessError('CSV 文件上限 512 KB', 413)
    try:
        reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig')), strict=True)
        header = reader.fieldnames or []
        known = {item['key'] for item in SPEC['fields']}
        required = {item['key'] for item in SPEC['fields'] if item.get('required', True)}
        if len(set(header)) != len(header) or not required.issubset(header) or not set(header).issubset(known):
            raise BusinessError('CSV 表头与导入模板不匹配')
        rows = []
        for row in reader:
            try:
                if None in row or any(value is None for value in row.values()):
                    raise BusinessError('列数与表头不匹配')
                rows.append(clean_observation(row))
            except BusinessError as exc:
                raise BusinessError(f'CSV 第 {reader.line_num} 行，{exc}') from exc
            if len(rows) > 500:
                raise BusinessError('单次导入上限 500 条')
        if not rows:
            raise BusinessError('CSV 里没有观测记录')
        return rows
    except (UnicodeDecodeError, csv.Error) as exc:
        raise BusinessError('请上传 UTF-8 编码的有效 CSV') from exc


def _reject(message):
    raise BusinessError(f'整批拒收：{message}（未写入任何记录）')


def add_observations(db, rows, actor, action):
    """按 (周, 版本) 入库一个批次。

    - 同一周同一版本报出不同数值（批内或与库内冲突）：整批拒收，事务回滚。
    - 同一周同一版本数值相同：视为重跑，跳过且不新增版本。
    - 晚到的旧版本只新增自身版本行，不覆盖已入库的新版本。
    - 批次内容键相同：整批幂等，直接返回首次受理结果。
    """
    key = batch_key(rows)
    previous = db.scalar(select(IngestBatch).where(IngestBatch.batch_key == key))
    if previous is not None:
        db.add(AuditEvent(actor=actor, action=action + '.rerun',
                          detail=f'批次 #{previous.id} 重跑，沿用首次受理结果，未新增版本'))
        return {'items': [], 'added': 0, 'skipped': len(rows), 'batch_id': previous.id, 'rerun': True}

    # 批内去重；同一 (周, 版本) 出现不同内容即整批拒收。
    deduped = {}
    for row in rows:
        slot = (row['week'], row['version'])
        prior = deduped.get(slot)
        if prior is not None and prior != row:
            _reject(f'同一周 {row["week"]} 同一版本 {row["version"]} 在本批报出不同数值')
        deduped[slot] = row
    unique_rows = list(deduped.values())

    slots = list(deduped)
    stored = list(db.scalars(select(Observation).where(
        Observation.week.in_({slot[0] for slot in slots}),
        Observation.version.in_({slot[1] for slot in slots}))))
    stored_map = {(item.week, item.version): item for item in stored}
    for slot, row in deduped.items():
        conflict = stored_map.get(slot)
        if conflict is not None and conflict.payload != row:
            _reject(f'同一周 {row["week"]} 同一版本 {row["version"]} 已入库数值与本批不同')

    if db.scalar(select(func.count(Observation.id))) + len(unique_rows) > MAX_OBSERVATIONS:
        raise BusinessError('当前项目最多保留 5000 条观测记录', 409)

    items = []
    for row in unique_rows:
        slot = (row['week'], row['version'])
        if slot in stored_map:
            continue  # 同周同版同值：重跑跳过，不新增版本。
        items.append(Observation(week=row['week'], version=row['version'],
                                 source_key=content_key(row), payload=row))
    db.add_all(items)
    try:
        db.flush()
    except IntegrityError as exc:
        raise BusinessError('有相同周与版本的记录刚刚写入，请刷新后重试', 409) from exc

    batch = IngestBatch(batch_key=key, row_count=len(rows), accepted_count=len(items), actor=actor)
    db.add(batch)
    db.flush()
    skipped = len(rows) - len(items)
    db.add(AuditEvent(actor=actor, action=action,
                      detail=f'批次 #{batch.id} 受理 {len(rows)} 条，新增 {len(items)} 条版本记录，跳过 {skipped} 条'))
    return {'items': items, 'added': len(items), 'skipped': skipped, 'batch_id': batch.id, 'rerun': False}


def latest_weekly_observations(db):
    """每个交接周取当前已入库的最高来源版本（受理时点快照）。"""
    latest_version = (
        select(Observation.week, func.max(Observation.version).label('version'))
        .group_by(Observation.week)
        .subquery()
    )
    return list(db.scalars(
        select(Observation)
        .join(latest_version,
              (Observation.week == latest_version.c.week)
              & (Observation.version == latest_version.c.version))
        .order_by(Observation.week, Observation.id)
    ))
