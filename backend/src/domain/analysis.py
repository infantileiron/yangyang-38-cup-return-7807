from datetime import date, timedelta
from math import sqrt
import numpy as np
from scipy.stats import t


COLUMNS = (
    ("week", "生产周／估计项"), ("returns", "有效退杯"), ("actual", "周实测平均（分钟）"),
    ("fitted", "分段拟合（分钟）"), ("counterfactual", "延续前期趋势（分钟）"),
    ("estimate", "变化估计"), ("lower", "95% 下界"), ("upper", "95% 上界"), ("unit", "估计单位"),
)


def validate(row):
    if date.fromisoformat(row["week"]).weekday() != 0:
        raise ValueError("按周一归档退杯记录")


def analyze(rows, parameters):
    baseline = date.fromisoformat(parameters.get("start","2026-09-07"))
    lag = parameters.get("lag",2)
    if baseline.weekday() != 0:
        raise ValueError("上线日期使用上线周周一")
    seen = set()
    for row in rows:
        validate(row)
        if row["week"] in seen:
            raise ValueError(f"重复的退杯交接周 {row['week']}")
        seen.add(row["week"])
    ordered = sorted(rows, key=lambda row:row["week"])
    weeks = [(date.fromisoformat(row["week"])-baseline).days//7 for row in ordered]
    if sum(week < 0 for week in weeks) < 6 or sum(week >= 0 for week in weeks) < 6:
        raise ValueError("拟合要求上线前后各至少 6 个完整周")
    if any(after-before != 1 for before,after in zip(weeks,weeks[1:])):
        raise ValueError("周序列存在缺周，请从服务台补齐口径")
    if not isinstance(lag,int) or not 0 <= lag < len(weeks)-4:
        raise ValueError("HAC 滞后阶超出可估计范围")
    x = np.asarray([(1., week, float(week >= 0), max(week,0)) for week in weeks])
    y = np.asarray([row["delay"] for row in ordered])
    weights, *_ = np.linalg.lstsq(x,y,rcond=None)
    fitted = x @ weights
    residual = y - fitted
    scores = x * residual[:,None]
    xe_inv = np.linalg.inv(x.T @ x)
    covariance_sum = scores.T @ scores
    for offset in range(1, lag+1):
        crossed = scores[offset:].T @ scores[:-offset]
        covariance_sum += (1-offset/(lag+1))*(crossed + crossed.T)
    degrees = len(weeks)-x.shape[1]
    covariance = (xe_inv @ covariance_sum @ xe_inv) * (len(weeks)/degrees)
    critical = float(t.ppf(0.975,degrees))
    names = ("上线周延展截距","上线前趋势","即时变化","上线后趋势变化")
    units = ("分钟","分钟/周","分钟","分钟/周")
    table = []
    for index, (name, unit) in enumerate(zip(names,units)):
        center = float(weights[index])
        radius = critical * sqrt(max(0,float(covariance[index,index])))
        table.append(dict(week=name,estimate=round(center,4),lower=round(center-radius,4),upper=round(center+radius,4),unit=unit))
    chart = []
    for index,(row,week) in enumerate(zip(ordered,weeks)):
        reference = float(weights[0]+weights[1]*week)
        table.append(dict(week=row["week"],returns=row["returns"],actual=row["delay"],
                          fitted=round(float(fitted[index]),3),counterfactual=round(reference,3)))
        if week >= 0:
            chart.append((row["week"][5:],round(row["delay"]-reference,3)))
    title = "上线周观察到平均退杯时延下移" if table[2]["upper"] < 0 else "已拆解票据上线的时点变化"
    return report(title,
                  f"{baseline.isoformat()} 划分前后，每周以一个观测进入拟合。移动客流等同时期变化仍需单独核查。负差值表示比延续前期趋势更快。",
                  [("交接周",len(weeks),"周"),("即时差",table[2]["estimate"],"分钟"),
                   ("斜率改变量",table[3]["estimate"],"分钟/周")],
                  COLUMNS,table,"上线后实测与前期趋势差",chart[:18],"分钟")


def seed():
    boundary = date(2026,9,7)
    return [dict(week=(boundary+timedelta(weeks=index)).isoformat(),returns=64 + (index*13)%58,
                 delay=round(12 + index*0.22 - (3.4 + index*0.38 if index >= 0 else 0) + ((index*9)%13-6)*0.075,2))
            for index in range(-12,11)]


def report(headline, summary, metrics, columns, rows, chart_label, chart_values, unit=""):
    return {
        "headline": headline, "summary": summary,
        "metrics": [{"label": label, "value": value, "unit": suffix} for label, value, suffix in metrics],
        "columns": [{"key": key, "label": label} for key, label in columns],
        "rows": [{key: row.get(key) for key, _ in columns} for row in rows],
        "chart": {"label": chart_label, "unit": unit,
                  "points": [{"label": label, "value": value} for label, value in chart_values]},
    }
