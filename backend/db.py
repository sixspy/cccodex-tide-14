import os
import time

import psycopg
from psycopg.rows import dict_row

SCHEMA = """
CREATE TABLE IF NOT EXISTS intake_batches (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    status TEXT NOT NULL,
    staged_count INTEGER NOT NULL,
    rejected_count INTEGER NOT NULL,
    landed_count INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS staged_rows (
    id BIGSERIAL PRIMARY KEY,
    batch_id BIGINT NOT NULL REFERENCES intake_batches(id),
    line_no INTEGER NOT NULL,
    station_code TEXT NOT NULL,
    observed_at TIMESTAMP NOT NULL,
    tide_cm INTEGER NOT NULL,
    original_flag TEXT NOT NULL,
    status TEXT NOT NULL,
    cleaning_method TEXT,
    cleaned_flag TEXT,
    cleaned_by TEXT,
    cleaned_at TIMESTAMPTZ,
    claim_token TEXT,
    UNIQUE (batch_id, line_no),
    UNIQUE (station_code, observed_at)
);

CREATE TABLE IF NOT EXISTS reject_rows (
    id BIGSERIAL PRIMARY KEY,
    batch_id BIGINT NOT NULL REFERENCES intake_batches(id),
    line_no INTEGER NOT NULL,
    raw_line TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS landed_rows (
    id BIGSERIAL PRIMARY KEY,
    batch_id BIGINT NOT NULL REFERENCES intake_batches(id),
    staged_id BIGINT NOT NULL UNIQUE REFERENCES staged_rows(id),
    line_no INTEGER NOT NULL,
    station_code TEXT NOT NULL,
    observed_at TIMESTAMP NOT NULL,
    original_flag TEXT NOT NULL,
    quality_flag TEXT NOT NULL,
    tide_cm INTEGER NOT NULL,
    cleaning_method TEXT NOT NULL,
    cleaned_by TEXT NOT NULL,
    landed_by TEXT NOT NULL,
    landed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (station_code, observed_at)
);

CREATE TABLE IF NOT EXISTS cleaning_settings (
    id BIGSERIAL PRIMARY KEY,
    policy TEXT NOT NULL,
    detail_note TEXT,
    policy_date DATE NOT NULL DEFAULT ((NOW() AT TIME ZONE 'Asia/Shanghai')::date),
    changed_by TEXT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS cleaning_log (
    id BIGSERIAL PRIMARY KEY,
    staged_id BIGINT NOT NULL REFERENCES staged_rows(id),
    event TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

SEED_CLEAN = """吴淞口 11-12 早班
WG01|202611120830|186|A
WG01|202611120900|191|A
BG02|202611120830|142|M
"""

SEED_BROKEN = """外高桥 11-12 错报
LG04|202611121000|205|A
WG01|202611120830|186|A
WG0X|202611121000|10|A
"""


def connect():
    # 时刻按上海时区回，和报文里的本地时刻、策略单日期对得上。
    return psycopg.connect(
        os.environ["DATABASE_URL"],
        row_factory=dict_row,
        options="-c timezone=Asia/Shanghai",
    )


def init_db():
    last_error = None
    for _ in range(30):
        try:
            with connect() as conn:
                _migrate(conn)
                count = conn.execute("SELECT COUNT(*) AS n FROM intake_batches").fetchone()["n"]
                if count == 0:
                    _seed(conn)
                conn.commit()
            return
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(1)
    raise last_error


def _migrate(conn):
    conn.execute(SCHEMA)
    # 早先的库只有入库口径，没有待落口径。补上三列再回填。
    for statement in (
        "ALTER TABLE intake_batches ADD COLUMN IF NOT EXISTS staged_count INTEGER",
        "ALTER TABLE intake_batches ADD COLUMN IF NOT EXISTS landed_count INTEGER",
        "ALTER TABLE intake_batches ADD COLUMN IF NOT EXISTS accepted_count INTEGER",
        "ALTER TABLE landed_rows ADD COLUMN IF NOT EXISTS staged_id BIGINT",
        "ALTER TABLE landed_rows ADD COLUMN IF NOT EXISTS original_flag TEXT",
        "ALTER TABLE landed_rows ADD COLUMN IF NOT EXISTS cleaning_method TEXT",
        "ALTER TABLE landed_rows ADD COLUMN IF NOT EXISTS cleaned_by TEXT",
        "ALTER TABLE landed_rows ADD COLUMN IF NOT EXISTS landed_by TEXT",
        "ALTER TABLE landed_rows ADD COLUMN IF NOT EXISTS landed_at TIMESTAMPTZ DEFAULT NOW()",
        "ALTER TABLE cleaning_settings ADD COLUMN IF NOT EXISTS policy_date DATE DEFAULT ((NOW() AT TIME ZONE 'Asia/Shanghai')::date)",
        # 旧的 accepted_count 是必填，新口径只写 staged_count / landed_count。
        "ALTER TABLE intake_batches ALTER COLUMN accepted_count DROP NOT NULL",
    ):
        conn.execute(statement)
    conn.execute(
        "UPDATE intake_batches SET staged_count = COALESCE(staged_count, accepted_count, 0)"
    )
    conn.execute("UPDATE intake_batches SET landed_count = COALESCE(landed_count, accepted_count, 0)")
    conn.execute(
        "UPDATE landed_rows SET original_flag = COALESCE(original_flag, quality_flag)"
    )
    conn.execute(
        "UPDATE landed_rows SET cleaning_method = COALESCE(cleaning_method, 'keep')"
    )
    conn.execute("UPDATE landed_rows SET cleaned_by = COALESCE(cleaned_by, '历史数据')")
    conn.execute("UPDATE landed_rows SET landed_by = COALESCE(landed_by, '历史数据')")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS cleaning_settings_day
        ON cleaning_settings (policy_date)
        """
    )


def _policy_for(conn, day=None):
    row = conn.execute(
        """
        SELECT policy, detail_note, policy_date, changed_by, changed_at
        FROM cleaning_settings
        WHERE policy_date = COALESCE(%s::date, (NOW() AT TIME ZONE 'Asia/Shanghai')::date)
        ORDER BY id DESC LIMIT 1
        """,
        (day,),
    ).fetchone()
    if row:
        return row
    return {
        "policy": "strict",
        "detail_note": None,
        "policy_date": None,
        "changed_by": None,
        "changed_at": None,
    }


def _insert_batch(conn, title: str, raw_text: str, created_by: str):
    from parser import parse_telegram_line

    batch = conn.execute(
        """
        INSERT INTO intake_batches
            (title, raw_text, status, staged_count, rejected_count, landed_count, created_by)
        VALUES (%s, %s, '待落', 0, 0, 0, %s)
        RETURNING id
        """,
        (title, raw_text, created_by),
    ).fetchone()
    staged = 0
    rejected = 0
    seen = set()
    for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
        if not raw_line.strip():
            continue
        parsed, reason = parse_telegram_line(raw_line)
        if reason:
            conn.execute(
                "INSERT INTO reject_rows (batch_id, line_no, raw_line, reason) VALUES (%s, %s, %s, %s)",
                (batch["id"], line_no, raw_line.strip(), reason),
            )
            rejected += 1
            continue
        key = (parsed["station_code"], parsed["observed_at"])
        if key in seen:
            reason = "同一批里观测时刻重复"
        elif conn.execute(
            "SELECT 1 FROM landed_rows WHERE station_code = %s AND observed_at = %s", key
        ).fetchone():
            reason = "这个站这个时刻已经落过库"
        elif conn.execute(
            "SELECT 1 FROM staged_rows WHERE station_code = %s AND observed_at = %s", key
        ).fetchone():
            reason = "这个站这个时刻已经在待落队列里"
        else:
            reason = None
        if reason:
            conn.execute(
                "INSERT INTO reject_rows (batch_id, line_no, raw_line, reason) VALUES (%s, %s, %s, %s)",
                (batch["id"], line_no, raw_line.strip(), reason),
            )
            rejected += 1
            continue
        seen.add(key)
        try:
            # 并发的两批可能同时看到空档，唯一约束兜底；用保存点让本行的失败不拖垮整批。
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO staged_rows
                        (batch_id, line_no, station_code, observed_at, tide_cm, original_flag, status)
                    VALUES (%s, %s, %s, %s, %s, %s, '待清洗')
                    """,
                    (
                        batch["id"],
                        line_no,
                        parsed["station_code"],
                        parsed["observed_at"],
                        parsed["tide_cm"],
                        parsed["quality_flag"],
                    ),
                )
        except psycopg.errors.UniqueViolation:
            conn.execute(
                "INSERT INTO reject_rows (batch_id, line_no, raw_line, reason) VALUES (%s, %s, %s, %s)",
                (batch["id"], line_no, raw_line.strip(), "这个站这个时刻已经在待落队列里"),
            )
            rejected += 1
            continue
        staged += 1
    conn.execute(
        """
        UPDATE intake_batches
        SET staged_count = %s, rejected_count = %s, status = %s
        WHERE id = %s
        """,
        (staged, rejected, "待落" if staged else "未接纳", batch["id"]),
    )
    return batch["id"]


def _seed(conn):
    title, _, body = SEED_CLEAN.partition("\n")
    _insert_batch(conn, title, body.strip(), "admin")
    title, _, body = SEED_BROKEN.partition("\n")
    _insert_batch(conn, title, body.strip(), "admin")
