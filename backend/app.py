from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from auth import authenticate, issue_token, read_user
from db import _insert_batch, _policy_for, connect, init_db
from parser import (
    CLEANING_METHODS,
    FLAGS,
    POLICY_LABELS,
    cleaned_flag,
    method_allowed,
    method_label,
)


class LoginBody(BaseModel):
    username: str
    password: str


class BatchBody(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    raw_text: str = Field(min_length=1)


class CleanBody(BaseModel):
    method: str
    claim_token: str = Field(min_length=8, max_length=64)


class SettingsBody(BaseModel):
    policy: str
    detail_note: str | None = None


def _stamp(value):
    return value.isoformat(sep=" ", timespec="minutes") if value else None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(lifespan=lifespan)


def _user_or_401(authorization: str | None):
    user = read_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def _writer_or_403(authorization: str | None):
    user = _user_or_401(authorization)
    if user["role"] != "writer":
        raise HTTPException(status_code=403, detail="档案员只能查看")
    return user


def _log(conn, staged_id, event, actor, detail):
    conn.execute(
        "INSERT INTO cleaning_log (staged_id, event, actor, detail) VALUES (%s, %s, %s, %s)",
        (staged_id, event, actor, detail),
    )


def _staged_view(row):
    item = dict(row)
    item["observed_at"] = _stamp(item["observed_at"])
    item["cleaned_at"] = _stamp(item.get("cleaned_at"))
    item["original_label"] = FLAGS.get(item["original_flag"], item["original_flag"])
    if item.get("cleaned_flag"):
        item["cleaned_label"] = FLAGS.get(item["cleaned_flag"], item["cleaned_flag"])
    else:
        item["cleaned_label"] = None
    if item.get("cleaning_method"):
        item["method_label"] = method_label(item["cleaning_method"], item["original_flag"])
    else:
        item["method_label"] = "待清洗"
    return item


def _settings_view(policy_row):
    return {
        "policy": policy_row["policy"],
        "policy_label": POLICY_LABELS.get(policy_row["policy"], policy_row["policy"]),
        "detail_note": policy_row["detail_note"],
        "policy_date": str(policy_row["policy_date"]) if policy_row["policy_date"] else None,
        "changed_by": policy_row["changed_by"],
        "changed_at": _stamp(policy_row["changed_at"]),
        "methods": CLEANING_METHODS,
    }


def _settings_state(conn):
    """策略单当前状态：今日是否已写定、队列还有多少行。"""
    policy_row = _policy_for(conn)
    view = _settings_view(policy_row)
    view["pending_count"] = conn.execute(
        "SELECT COUNT(*) AS n FROM staged_rows WHERE status IN ('待清洗', '已放行')"
    ).fetchone()["n"]
    view["locked"] = policy_row["policy_date"] is not None
    return view


@app.get("/api/health")
def health():
    with connect() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok", "service": "tide-telegram-intake"}


@app.post("/api/login")
def login(body: LoginBody):
    user = authenticate(body.username.strip(), body.password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {
        "access_token": issue_token(user),
        "username": user["username"],
        "role": user["role"],
        "title": user["title"],
    }


@app.get("/api/batches")
def list_batches(authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, title, status, staged_count, rejected_count, landed_count,
                   created_by, created_at
            FROM intake_batches
            ORDER BY id DESC
            """
        ).fetchall()
    for row in rows:
        row["created_at"] = _stamp(row["created_at"])
    return rows


@app.get("/api/batches/{batch_id}")
def batch_detail(batch_id: int, authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    with connect() as conn:
        batch = conn.execute(
            """
            SELECT id, title, raw_text, status, staged_count, rejected_count, landed_count,
                   created_by, created_at
            FROM intake_batches WHERE id = %s
            """,
            (batch_id,),
        ).fetchone()
        if not batch:
            raise HTTPException(status_code=404, detail="没有这一批报文")
        staged = conn.execute(
            """
            SELECT id, line_no, station_code, observed_at, tide_cm, original_flag,
                   status, cleaning_method, cleaned_flag, cleaned_by, cleaned_at,
                   claim_token
            FROM staged_rows WHERE batch_id = %s ORDER BY line_no
            """,
            (batch_id,),
        ).fetchall()
        landed = conn.execute(
            """
            SELECT line_no, station_code, observed_at, tide_cm, original_flag,
                   quality_flag, cleaning_method, cleaned_by, landed_by, landed_at
            FROM landed_rows WHERE batch_id = %s ORDER BY line_no
            """,
            (batch_id,),
        ).fetchall()
        rejected = conn.execute(
            """
            SELECT line_no, raw_line, reason
            FROM reject_rows WHERE batch_id = %s ORDER BY line_no
            """,
            (batch_id,),
        ).fetchall()
        events = conn.execute(
            """
            SELECT l.staged_id, l.event, l.actor, l.detail, l.created_at
            FROM cleaning_log l
            JOIN staged_rows s ON s.id = l.staged_id
            WHERE s.batch_id = %s ORDER BY l.id
            """,
            (batch_id,),
        ).fetchall()
        settings = _policy_for(conn)
    batch["created_at"] = _stamp(batch["created_at"])
    for row in staged:
        row.update(_staged_view(row))
        row.pop("claim_token", None)
    for row in landed:
        row["observed_at"] = _stamp(row["observed_at"])
        row["landed_at"] = _stamp(row["landed_at"])
        row["original_label"] = FLAGS.get(row["original_flag"], row["original_flag"])
        row["flag_label"] = FLAGS.get(row["quality_flag"], row["quality_flag"])
        row["method_label"] = method_label(row["cleaning_method"], row["original_flag"])
    for row in events:
        row["created_at"] = _stamp(row["created_at"])
    return {
        "batch": batch,
        "staged": staged,
        "landed": landed,
        "rejected": rejected,
        "events": events,
        "settings": _settings_view(settings),
    }


@app.post("/api/batches")
def create_batch(body: BatchBody, authorization: str | None = Header(default=None)):
    user = _writer_or_403(authorization)
    title = body.title.strip()
    raw_text = body.raw_text.strip()
    if not title or not raw_text:
        raise HTTPException(status_code=400, detail="标题和报文都不能空")
    with connect() as conn:
        batch_id = _insert_batch(conn, title, raw_text, user["username"])
        conn.commit()
    return batch_detail(batch_id, authorization)


@app.get("/api/pending")
def pending_queue(authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.batch_id, b.title AS batch_title, s.line_no, s.station_code,
                   s.observed_at, s.tide_cm, s.original_flag, s.status, s.cleaning_method,
                   s.cleaned_flag, s.cleaned_by, s.cleaned_at
            FROM staged_rows s
            JOIN intake_batches b ON b.id = s.batch_id
            WHERE s.status IN ('待清洗', '已放行')
            ORDER BY s.batch_id, s.line_no
            """
        ).fetchall()
        settings = _settings_state(conn)
    return {
        "rows": [_staged_view(row) for row in rows],
        "settings": settings,
    }


@app.get("/api/landed")
def landed_list(authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT l.id, l.batch_id, b.title AS batch_title, l.line_no, l.station_code,
                   l.observed_at, l.tide_cm, l.original_flag, l.quality_flag,
                   l.cleaning_method, l.cleaned_by, l.landed_by, l.landed_at
            FROM landed_rows l
            JOIN intake_batches b ON b.id = l.batch_id
            ORDER BY l.id DESC
            """
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["observed_at"] = _stamp(item["observed_at"])
        item["landed_at"] = _stamp(item["landed_at"])
        item["original_label"] = FLAGS.get(item["original_flag"], item["original_flag"])
        item["flag_label"] = FLAGS.get(item["quality_flag"], item["quality_flag"])
        item["method_label"] = method_label(item["cleaning_method"], item["original_flag"])
        result.append(item)
    return result


@app.get("/api/settings")
def get_settings(authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    with connect() as conn:
        return _settings_state(conn)


@app.post("/api/settings")
def update_settings(body: SettingsBody, authorization: str | None = Header(default=None)):
    user = _writer_or_403(authorization)
    if body.policy not in POLICY_LABELS:
        raise HTTPException(status_code=400, detail="策略单只能选严格清洗或留旗入库")
    note = (body.detail_note or "").strip() or None
    with connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(4211)")
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM staged_rows WHERE status IN ('待清洗', '已放行')"
        ).fetchone()["n"]
        if pending:
            raise HTTPException(
                status_code=409,
                detail=f"待落队列里还有 {pending} 行，清完之前不能改清洗设置",
            )
        current = _policy_for(conn)
        if current["policy_date"] is not None:
            # 本日策略单写定后不再换清洗方式，说明也一并锁住。
            raise HTTPException(
                status_code=409,
                detail="今日策略单已写定，本日不得更换清洗方式",
            )
        conn.execute(
            """
            INSERT INTO cleaning_settings (policy, detail_note, changed_by)
            VALUES (%s, %s, %s)
            """,
            (body.policy, note, user["username"]),
        )
        conn.commit()
        return _settings_state(conn)


@app.post("/api/staged/{staged_id}/clean")
def clean_row(
    staged_id: int,
    body: CleanBody,
    authorization: str | None = Header(default=None),
):
    user = _writer_or_403(authorization)
    if body.method not in CLEANING_METHODS:
        raise HTTPException(status_code=400, detail="清洗方式不在原旗放行、改记实测、改记插补之内")
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM staged_rows WHERE id = %s FOR UPDATE", (staged_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="没有这一行待落")
        if row["status"] == "已入库":
            raise HTTPException(status_code=409, detail="这行已经入库，不能再清洗")
        if row["status"] == "已放行":
            # 一行只留先认领的结果，后来者一律按争用处理。
            if row["claim_token"] == body.claim_token:
                return {
                    "id": staged_id,
                    "status": "已放行",
                    "method_label": method_label(row["cleaning_method"], row["original_flag"]),
                }
            _log(conn, staged_id, "清洗争用", user["username"], row["cleaned_by"] or "他人")
            conn.commit()
            raise HTTPException(
                status_code=409,
                detail=f"清洗争用：这行已被 {row['cleaned_by'] or '他人'} 先认领",
            )
        settings = _policy_for(conn)
        if not method_allowed(row["original_flag"], body.method, settings["policy"]):
            _log(
                conn,
                staged_id,
                "清洗方式不符",
                user["username"],
                f"原旗 {row['original_flag']}，用 {CLEANING_METHODS[body.method]}，"
                f"策略单为 {POLICY_LABELS[settings['policy']]}",
            )
            conn.commit()
            raise HTTPException(
                status_code=409,
                detail="清洗方式不符：和当日策略单的清洗设置偏离",
            )
        flag = cleaned_flag(row["original_flag"], body.method)
        label = method_label(body.method, row["original_flag"])
        conn.execute(
            """
            UPDATE staged_rows
            SET status = '已放行', cleaning_method = %s, cleaned_flag = %s,
                cleaned_by = %s, cleaned_at = NOW(), claim_token = %s
            WHERE id = %s
            """,
            (body.method, flag, user["username"], body.claim_token, staged_id),
        )
        _log(
            conn,
            staged_id,
            "已清洗",
            user["username"],
            f"{row['original_flag']}→{flag}，{label}",
        )
        conn.commit()
    return {"id": staged_id, "status": "已放行", "method_label": label}


@app.post("/api/batches/{batch_id}/land")
def land_batch(batch_id: int, authorization: str | None = Header(default=None)):
    user = _writer_or_403(authorization)
    with connect() as conn:
        batch = conn.execute(
            "SELECT id, title FROM intake_batches WHERE id = %s FOR UPDATE", (batch_id,)
        ).fetchone()
        if not batch:
            raise HTTPException(status_code=404, detail="没有这一批报文")
        rows = conn.execute(
            """
            SELECT id, line_no, station_code, observed_at, tide_cm, original_flag,
                   status, cleaning_method, cleaned_flag, cleaned_by
            FROM staged_rows WHERE batch_id = %s ORDER BY line_no FOR UPDATE
            """,
            (batch_id,),
        ).fetchall()
        cleaned = [row for row in rows if row["status"] == "已放行"]
        pending = [row for row in rows if row["status"] == "待清洗"]
        if pending:
            raise HTTPException(
                status_code=409,
                detail=f"还有 {len(pending)} 行未经清洗，不得入库",
            )
        if not cleaned:
            raise HTTPException(status_code=409, detail="这一批的行都已入库，没有可落的行")
        for row in cleaned:
            conn.execute(
                """
                INSERT INTO landed_rows
                    (batch_id, staged_id, line_no, station_code, observed_at, tide_cm,
                     original_flag, quality_flag, cleaning_method, cleaned_by, landed_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    batch_id,
                    row["id"],
                    row["line_no"],
                    row["station_code"],
                    row["observed_at"],
                    row["tide_cm"],
                    row["original_flag"],
                    row["cleaned_flag"],
                    row["cleaning_method"],
                    row["cleaned_by"],
                    user["username"],
                ),
            )
            conn.execute(
                "UPDATE staged_rows SET status = '已入库' WHERE id = %s", (row["id"],)
            )
            _log(conn, row["id"], "已入库", user["username"], row["cleaning_method"])
        staged_total = conn.execute(
            "SELECT COUNT(*) AS n FROM staged_rows WHERE batch_id = %s", (batch_id,)
        ).fetchone()["n"]
        landed_total = conn.execute(
            "SELECT COUNT(*) AS n FROM landed_rows WHERE batch_id = %s", (batch_id,)
        ).fetchone()["n"]
        status = "已入库" if landed_total >= staged_total else "部分入库"
        conn.execute(
            "UPDATE intake_batches SET status = %s, landed_count = %s WHERE id = %s",
            (status, landed_total, batch_id),
        )
        conn.commit()
    return {"batch_id": batch_id, "landed": len(cleaned), "status": status}


if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))

