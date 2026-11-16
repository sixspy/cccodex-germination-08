import os
import time

import psycopg
from psycopg.rows import dict_row

from metric import CURRENT, LEGACY, measure

SCHEMA = """
CREATE TABLE IF NOT EXISTS trials (
    id BIGSERIAL PRIMARY KEY,
    lot_code TEXT NOT NULL UNIQUE,
    crop_name TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS beds (
    id BIGSERIAL PRIMARY KEY,
    trial_id BIGINT NOT NULL REFERENCES trials(id),
    bed_no INTEGER NOT NULL,
    sown INTEGER NOT NULL,
    sprouted INTEGER NOT NULL,
    moldy INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS publications (
    id BIGSERIAL PRIMARY KEY,
    trial_id BIGINT NOT NULL REFERENCES trials(id),
    protocol_version TEXT NOT NULL,
    tested INTEGER NOT NULL,
    excluded_mold INTEGER NOT NULL,
    sprouted INTEGER NOT NULL,
    rate_bp INTEGER NOT NULL,
    note TEXT NOT NULL,
    published_by TEXT NOT NULL,
    published_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 一个批次在同一口径下只允许有一条台账记录，从数据库层禁止双轨/重复入册。
CREATE UNIQUE INDEX IF NOT EXISTS publications_trial_protocol_uniq
    ON publications (trial_id, protocol_version);
"""

# 出数被并发争用打断时，建议后手等待后重试。
RETRY_LIMIT = 3
RETRY_DELAY_SECONDS = 0.2


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def init_db():
    last_error = None
    for _ in range(30):
        try:
            with connect() as conn:
                conn.execute(SCHEMA)
                count = conn.execute("SELECT COUNT(*) AS n FROM trials").fetchone()["n"]
                if count == 0:
                    _seed(conn)
                _resync_statuses(conn)
                conn.commit()
            return
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(1)
    raise last_error


def _add_trial(conn, lot_code, crop_name, status, beds):
    trial = conn.execute(
        "INSERT INTO trials (lot_code, crop_name, status) VALUES (%s, %s, %s) RETURNING id",
        (lot_code, crop_name, status),
    ).fetchone()
    for bed_no, sown, sprouted, moldy in beds:
        conn.execute(
            """
            INSERT INTO beds (trial_id, bed_no, sown, sprouted, moldy)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (trial["id"], bed_no, sown, sprouted, moldy),
        )
    return trial["id"]


def _lock_trial(conn, trial_id: int):
    """锁定批次行。并发归档同一批次时在此排队，后手不会静默覆盖。"""
    trial = conn.execute(
        "SELECT id, lot_code, status FROM trials WHERE id = %s FOR UPDATE",
        (trial_id,),
    ).fetchone()
    if not trial:
        raise LookupError("没有这个批次")
    return trial


def _resync_statuses(conn):
    """让批次列表状态与台账逐批对齐：有现行口径记录才算已出数，否则待重算。"""
    conn.execute(
        """
        UPDATE trials
        SET status = '已按现行口径出数'
        WHERE id IN (
            SELECT trial_id FROM publications WHERE protocol_version = %s
        )
        """,
        (CURRENT,),
    )
    conn.execute(
        """
        UPDATE trials
        SET status = '待按现行口径重算'
        WHERE id NOT IN (
            SELECT trial_id FROM publications WHERE protocol_version = %s
        )
        """,
        (CURRENT,),
    )


def publish(conn, trial_id: int, username: str, protocol: str):
    """整批归档：口径校验、台账写入、状态更新在同一事务里要么全成要么全不写。

    返回 (publication_id, skipped)。同一批次同一口径重复归档时跳过，
    skipped 记 1，不产生半条台账。
    """
    _lock_trial(conn, trial_id)
    existing = conn.execute(
        """
        SELECT id FROM publications
        WHERE trial_id = %s AND protocol_version = %s
        """,
        (trial_id, protocol),
    ).fetchone()
    if existing:
        return existing["id"], 1

    beds = conn.execute(
        "SELECT sown, sprouted, moldy FROM beds WHERE trial_id = %s ORDER BY bed_no",
        (trial_id,),
    ).fetchall()
    if not beds:
        raise ValueError("这个批次还没有床次")
    # measure 抛错时本事务没有任何写入，整笔归档失败，杜绝半截入册。
    result = measure(beds, protocol)
    row = conn.execute(
        """
        INSERT INTO publications (
            trial_id, protocol_version, tested, excluded_mold, sprouted, rate_bp, note, published_by
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            trial_id,
            result["protocol_version"],
            result["tested"],
            result["excluded_mold"],
            result["sprouted"],
            result["rate_bp"],
            result["note"],
            username,
        ),
    ).fetchone()
    if protocol != LEGACY:
        conn.execute(
            "UPDATE trials SET status = %s WHERE id = %s",
            ("已按现行口径出数", trial_id),
        )
    return row["id"], 0


def rollback_publication(conn, trial_id: int, keep_valid: bool):
    """撤回归档。

    keep_valid=False：整笔撤回，删掉该批次全部台账记录，批次回到待重算。
    keep_valid=True ：只保留口径校验合格的记录；校验不过的记录整笔剔除，
                      没有“留一半”的中间态。台账始终只有 publications 这一条轨。
    """
    trial = _lock_trial(conn, trial_id)
    beds = conn.execute(
        "SELECT sown, sprouted, moldy FROM beds WHERE trial_id = %s ORDER BY bed_no",
        (trial_id,),
    ).fetchall()
    publications = conn.execute(
        "SELECT id, protocol_version FROM publications WHERE trial_id = %s ORDER BY id",
        (trial_id,),
    ).fetchall()
    if not publications:
        raise ValueError("这个批次还没有归档记录，无法撤回")

    removed = 0
    if keep_valid:
        # 用当前床次数据重新裁定每条台账：验得过就留，验不过整条撤。
        for publication in publications:
            try:
                measure(beds, publication["protocol_version"])
            except ValueError:
                conn.execute(
                    "DELETE FROM publications WHERE id = %s", (publication["id"],)
                )
                removed += 1
        if removed == 0:
            raise ValueError("现有归档记录全部校验合格，没有可撤回的条目")
    else:
        removed = conn.execute(
            "DELETE FROM publications WHERE trial_id = %s", (trial_id,)
        ).rowcount

    # 回滚后立刻按台账重算状态，切日列表与日册对齐，不靠人工刷新。
    _resync_statuses(conn)
    remaining = conn.execute(
        "SELECT COUNT(*) AS n FROM publications WHERE trial_id = %s",
        (trial_id,),
    ).fetchone()["n"]
    return {"lot_code": trial["lot_code"], "removed": removed, "remaining": remaining}


def _seed(conn):
    wheat = _add_trial(
        conn,
        "WH-2408",
        "小麦",
        "已按现行口径出数",
        [(1, 100, 92, 3), (2, 100, 88, 5)],
    )
    publish(conn, wheat, "admin", "v2")
    rice = _add_trial(
        conn,
        "RC-2411",
        "水稻",
        "待按现行口径重算",
        [(1, 80, 70, 6), (2, 80, 64, 4)],
    )
    publish(conn, rice, "admin", LEGACY)
    conn.execute("UPDATE trials SET status = %s WHERE id = %s", ("待按现行口径重算", rice))
