"""监听指标的 SQLite 存储；连接、锁和事务由调用方管理。

只保存计数、数值样本和采样时间，不保存推文内容或请求信息。
延迟样本由调用方统一换算为毫秒，每类保留最近写入的 1000 个样本。
"""
import math
from numbers import Real


SAMPLE_LIMIT_PER_KIND = 1000


def ensure_schema(db):
    """创建指标表和索引，不提交调用方的事务。"""
    # executescript 会隐式提交已有事务，因此逐条执行建表语句。
    db.execute('''CREATE TABLE IF NOT EXISTS metrics_counters (
        key TEXT PRIMARY KEY,
        value INTEGER NOT NULL
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS metrics_samples (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        value REAL NOT NULL,
        observed_at REAL NOT NULL
    )''')
    db.execute('''CREATE INDEX IF NOT EXISTS metrics_samples_kind_id
        ON metrics_samples(kind, id)''')


def _name(value):
    if not isinstance(value, str) or not value:
        raise ValueError('指标名称必须为非空字符串')
    return value


def _number(value):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError('指标样本和时间必须为有限非负数')
    try:
        value = float(value)
    except (ValueError, OverflowError):
        raise ValueError('指标样本和时间必须为有限非负数') from None
    if not math.isfinite(value) or value < 0:
        raise ValueError('指标样本和时间必须为有限非负数')
    return value


def increment(db, key, n=1):
    """累加非负整数计数，不提交事务。"""
    key = _name(key)
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        raise ValueError('指标计数增量必须为非负整数')
    db.execute('''INSERT INTO metrics_counters(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = value + excluded.value''', (key, n))


def record(db, kind, value, now):
    """记录数值样本并裁剪当前类别的旧样本，不提交事务。"""
    kind, value, now = _name(kind), _number(value), _number(now)
    db.execute('INSERT INTO metrics_samples(kind, value, observed_at) VALUES (?, ?, ?)',
               (kind, value, now))
    # 按写入顺序裁剪，系统时间回拨也不会挤掉刚采集的样本。
    db.execute('''DELETE FROM metrics_samples WHERE kind = ? AND id <= (
        SELECT id FROM metrics_samples WHERE kind = ?
        ORDER BY id DESC LIMIT 1 OFFSET ?
    )''', (kind, kind, SAMPLE_LIMIT_PER_KIND))


def _percentile(values, fraction):
    """用相邻排序样本线性插值；单个样本的分位数就是自身。"""
    position = (len(values) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def snapshot(db):
    """返回持久计数和保留样本的统计值；last_at 为最后写入样本的时间。"""
    counters = dict(db.execute('SELECT key, value FROM metrics_counters ORDER BY key'))
    by_kind = {}
    for kind, value, observed_at in db.execute('''
            SELECT kind, value, observed_at FROM metrics_samples ORDER BY kind, id'''):
        samples = by_kind.setdefault(kind, {'values': [], 'last_at': observed_at})
        samples['values'].append(value)
        samples['last_at'] = observed_at
    summaries = {}
    for kind, samples in by_kind.items():
        values = sorted(samples['values'])
        summaries[kind] = {
            'count': len(values),
            'p50': _percentile(values, 0.5),
            'p95': _percentile(values, 0.95),
            'max': values[-1],
            'last_at': samples['last_at'],
        }
    return {
        'counters': counters,
        'samples': summaries,
        'sample_limit_per_kind': SAMPLE_LIMIT_PER_KIND,
    }
