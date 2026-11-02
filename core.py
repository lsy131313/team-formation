"""팀 편성 앱의 핵심 로직: DB 연결, 규칙 검사, 지원/투표/마감, AI 배정.

화면(streamlit_app.py)과 나눠 두어서, 화면 없이도 시나리오를 시험할 수 있음.
비밀 값(키, DB 주소, 관리자 비밀번호)은 절대 화면이나 로그에 출력하지 않음.
"""
import csv
import io
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timedelta, timezone
from itertools import combinations
from pathlib import Path

import bcrypt
import psycopg2
import psycopg2.extras
import psycopg2.pool
import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

# ---------------------------------------------------------------- 상수
ROLES = ["기획자", "웹 프로그래머", "프론트엔드 디자이너", "테스터", "보고서 작성자", "발표자"]
PL, WP = "기획자", "웹 프로그래머"
REQUIRED = (PL, WP)
MIN_SIZE, MAX_SIZE = 2, 4
MODEL_TIMEOUT_SEC = 120          # 모델 하나당 전체 대기 시간(호출 ~ 응답 끝)
STUDENT_PASSWORD = "1234"
KST = timezone(timedelta(hours=9))
SECRET_NAMES = ("OPENROUTER_API_KEY", "DATABASE_URL", "ADMIN_PASSWORD")

# 1차, 2차, 3차 모델. Secrets 에 MODEL_1, MODEL_2, MODEL_3 을 넣으면 그 값을 씀.
# 무료 모델은 자주 바뀌므로 https://openrouter.ai/models?max_price=0 에서 지금 있는 :free 모델로 확인해 바꿀 것.
DEFAULT_MODELS = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "openai/gpt-oss-120b:free",
    "qwen/qwen3-coder:free",
]


class Blocked(Exception):
    """규칙 때문에 처리하지 않음. 화면에는 '막힘: 이유' 로 보임."""


# ---------------------------------------------------------------- 비밀 값
def get_secret(name, default=None):
    value = os.environ.get(name)
    if value:
        return value
    try:
        import streamlit as st
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return default


def safe_error(e) -> str:
    """오류 문장에서 비밀 값을 지우고 짧게 만듦(키나 DB 주소가 화면에 나오지 않게)."""
    msg = str(e)
    for name in SECRET_NAMES:
        v = get_secret(name)
        if v and v in msg:
            msg = msg.replace(v, "***")
    msg = re.sub(r"postgres(ql)?://\S+", "***", msg)
    msg = re.sub(r"sk-or-[\w-]+", "***", msg)
    return msg.strip().replace("\n", " ")[:200]


def get_models():
    return [get_secret(f"MODEL_{i}", DEFAULT_MODELS[i - 1]) for i in (1, 2, 3)]


def now_kst():
    return datetime.now(KST)


# ---------------------------------------------------------------- 비밀번호 해시
def hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=10)).decode()


def check_pw(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except Exception:
        return False


# ---------------------------------------------------------------- DB 연결(한 번 만들고 다시 씀)
_pool = None
_pool_lock = threading.Lock()


def get_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            url = get_secret("DATABASE_URL")
            if not url:
                raise RuntimeError("DATABASE_URL 값이 없음 (.env 나 Streamlit Secrets 를 확인)")
            _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, dsn=url, connect_timeout=10)
        return _pool


def _getconn(pool):
    deadline = time.monotonic() + 10
    while True:
        try:
            return pool.getconn()
        except psycopg2.pool.PoolError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)


def tx(fn):
    """연결 풀에서 연결을 빌려 한 트랜잭션으로 fn(cur) 을 실행함. 끊긴 연결이면 한 번 다시 시도함."""
    pool = get_pool()
    for attempt in range(2):
        conn = _getconn(pool)
        try:
            with conn:  # 성공하면 commit, 예외면 rollback
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    result = fn(cur)
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            pool.putconn(conn, close=True)
            if attempt == 1:
                raise
            continue
        except BaseException:
            pool.putconn(conn)
            raise
        pool.putconn(conn)
        return result


_init_lock = threading.Lock()
_init_done = False


def init_db():
    """schema.sql 실행 + 관리자 계정 만들기/비밀번호 갱신. 앱이 시작할 때 한 번."""
    global _init_done
    with _init_lock:
        if _init_done:
            return
        sql = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        tx(lambda cur: cur.execute(sql))
        ensure_admin()
        _init_done = True


def ensure_admin():
    pw = get_secret("ADMIN_PASSWORD")
    if not pw:
        raise RuntimeError("ADMIN_PASSWORD 값이 없음 (.env 나 Streamlit Secrets 를 확인)")

    def fn(cur):
        cur.execute("select id, password_hash from tf_students where student_no = 'admin'")
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "insert into tf_students (student_no, name, main_role, password_hash, is_admin) "
                "values ('admin', '관리자', null, %s, true)", (hash_pw(pw),))
        elif not check_pw(pw, row["password_hash"]):
            cur.execute("update tf_students set password_hash = %s, is_admin = true where id = %s",
                        (hash_pw(pw), row["id"]))
    tx(fn)


# ---------------------------------------------------------------- 작은 DB 도우미
def _get_setting(cur, key):
    cur.execute("select value from tf_settings where key = %s", (key,))
    row = cur.fetchone()
    return row["value"] if row else None


def _set_setting(cur, key, value):
    if value is None:
        cur.execute("delete from tf_settings where key = %s", (key,))
    else:
        cur.execute("insert into tf_settings (key, value) values (%s, %s) "
                    "on conflict (key) do update set value = excluded.value", (key, value))


def _is_closed(cur):
    return _get_setting(cur, "closed") == "1"


def _students(cur, ids=None):
    """학생 dict: id → {id, student_no, name, main_role, subs[]} (관리자 제외)"""
    if ids is None:
        cur.execute("select id, student_no, name, main_role from tf_students "
                    "where not is_admin order by student_no")
    else:
        cur.execute("select id, student_no, name, main_role from tf_students "
                    "where id = any(%s::bigint[])", (list(ids),))
    out = {r["id"]: {**r, "subs": []} for r in cur.fetchall()}
    if out:
        cur.execute("select student_id, role from tf_student_sub_roles "
                    "where student_id = any(%s::bigint[]) order by role", (list(out),))
        for r in cur.fetchall():
            out[r["student_id"]]["subs"].append(r["role"])
    return out


def _student_by_no(cur, student_no):
    cur.execute("select id from tf_students where student_no = %s and not is_admin", (student_no,))
    row = cur.fetchone()
    if not row:
        return None
    return _students(cur, [row["id"]])[row["id"]]


def _team_members(cur, team_id):
    cur.execute("select student_id from tf_members where team_id = %s order by slot_no", (team_id,))
    ids = [r["student_id"] for r in cur.fetchall()]
    studs = _students(cur, ids)
    return [studs[i] for i in ids]


def _free_slot(cur, team_id):
    cur.execute("select slot_no from tf_members where team_id = %s", (team_id,))
    used = {r["slot_no"] for r in cur.fetchall()}
    for s in range(1, MAX_SIZE + 1):
        if s not in used:
            return s
    return None


# 지원/투표는 공유 잠금, 마감/명단/확정은 배타 잠금 → 마감과 지원이 섞이지 않음
_LOCK_KEY = 20261102


def _lock_shared(cur):
    cur.execute("select pg_advisory_xact_lock_shared(%s)", (_LOCK_KEY,))


def _lock_exclusive(cur):
    cur.execute("select pg_advisory_xact_lock(%s)", (_LOCK_KEY,))


# ---------------------------------------------------------------- 규칙(순수 함수)
def can_take(s, role):
    return s["main_role"] == role or role in s["subs"]


def required_filled(group):
    """기획자와 웹 프로그래머를 '서로 다른 학생'이 맡을 때, 채울 수 있는 필수 역할 수(0~2)."""
    pls = [s for s in group if can_take(s, PL)]
    wps = [s for s in group if can_take(s, WP)]
    for a in pls:
        for b in wps:
            if a["id"] != b["id"]:
                return 2
    return 1 if (pls or wps) else 0


def join_problem(members, applicant):
    """applicant 가 members 팀에 들어갈 때 어기는 규칙(없으면 None). 지원할 때와 승인 직전에 씀."""
    if len(members) >= MAX_SIZE:
        return "팀이 꽉 참"
    if any(m["main_role"] == applicant["main_role"] for m in members):
        return "같은 주 역할의 팀원이 있음"
    after = members + [applicant]
    missing = len(REQUIRED) - required_filled(after)
    seats_left = MAX_SIZE - len(after)
    if seats_left < missing:
        return "필수 포함 역할(기획자, 웹 프로그래머)을 맡을 학생의 자리가 모자람"
    return None


def team_problems(name, group):
    """확정한 팀이 지켜야 할 규칙(인원, 주 역할 중복, 필수 포함 역할)을 검사함."""
    probs = []
    if not (MIN_SIZE <= len(group) <= MAX_SIZE):
        probs.append(f"{name}의 인원이 {len(group)}명임(2~4명이어야 함)")
    mains = [s["main_role"] for s in group]
    dups = sorted({m for m in mains if mains.count(m) > 1})
    if dups:
        probs.append(f"{name}에 주 역할이 겹침({', '.join(dups)})")
    if group and required_filled(group) < 2:
        lack = [r for r in REQUIRED if not any(can_take(s, r) for s in group)]
        what = ", ".join(lack) if lack else "기획자와 웹 프로그래머(서로 다른 두 학생)"
        probs.append(f"{name}에 {what}를 맡을 학생이 없음")
    return probs


def validate_assignment(team_ids, team_names, groups, pool, pairs):
    """배정안 검사 다섯 가지. 문제 목록을 돌려줌(빈 목록이면 통과).
    pairs: [(학생 id, 팀 id)], groups: 팀 id → 지금 팀원 목록, pool: 팀이 없는 학생 목록"""
    probs = []
    by_id = {s["id"]: s for s in pool}
    seen = {}
    for sid, tid in pairs:
        if sid not in by_id:
            probs.append(f"팀이 없는 학생이 아닌 학생이 들어 있음({sid})")
            continue
        if tid not in team_ids:
            probs.append(f"없는 팀 번호({tid})")
            continue
        if sid in seen:
            probs.append(f"{by_id[sid]['name']}이(가) 두 번 이상 배정됨")  # 2) 한 학생은 한 팀에만
            continue
        seen[sid] = tid
    missing = [s["name"] for s in pool if s["id"] not in seen]
    if missing:
        probs.append("배정되지 않은 학생: " + ", ".join(missing))  # 1) 빠짐없이
    new_groups = {t: list(groups.get(t, [])) for t in team_ids}
    for sid, tid in seen.items():
        new_groups[tid].append(by_id[sid])
    for t in team_ids:  # 3) 인원 4) 주 역할 5) 필수 역할
        probs += team_problems(team_names[t], new_groups[t])
    return probs


class _SearchLimit(Exception):
    pass


def diagnose(team_ids, team_names, groups, pool):
    """규칙을 지키는 배정이 '분명히' 없을 때 그 이유를 돌려줌. 판단이 안 서면 None."""
    total = sum(len(groups[t]) for t in team_ids) + len(pool)
    n = len(team_ids)
    if total < MIN_SIZE * n:
        return f"학생이 {total}명이라 팀 {n}개를 모두 2명 이상으로 만들 수 없음"
    if total > MAX_SIZE * n:
        return f"학생이 {total}명이라 팀 {n}개에 4명씩 넣어도 자리가 모자람"
    for t in team_ids:
        g = groups[t]
        mains = {s["main_role"] for s in g}
        dup = [m for m in mains if [s["main_role"] for s in g].count(m) > 1]
        if dup:
            return f"{team_names[t]}에 이미 주 역할이 겹친 팀원이 있음"
        if required_filled(g) == 2:
            continue
        seats = MAX_SIZE - len(g)
        cands = [s for s in pool if s["main_role"] not in mains]
        ok = False
        for k in range(1, min(seats, 2) + 1):
            for combo in combinations(cands, k):
                if len({s["main_role"] for s in combo}) == k and required_filled(g + list(combo)) == 2:
                    ok = True
                    break
            if ok:
                break
        if not ok:
            usable = g if seats == 0 else g + cands
            lack = [r for r in REQUIRED if not any(can_take(s, r) for s in usable)]
            what = ", ".join(lack) if lack else "기획자와 웹 프로그래머(서로 다른 두 학생)"
            if seats == 0:
                return f"{team_names[t]}이 꽉 찼는데 {what}를 맡을 팀원이 없음"
            return f"{team_names[t]}에 {what}를 맡을 학생이 없음"
    return None


def solve_assignment(team_ids, groups, pool, node_limit=3_000_000):
    """모든 경우를 따져(백트래킹) 규칙을 모두 지키는 배정을 찾음. 없으면 None.
    빈 팀끼리는 서로 같으므로 빈 팀 여러 개 중 하나에만 넣어 보는 방식으로 경우의 수를 줄임."""
    cur_groups = {t: list(groups[t]) for t in team_ids}
    # 필수 역할을 맡을 수 있는 학생부터 넣어 보면 답을 빨리 찾음
    order = sorted(pool, key=lambda s: -(can_take(s, PL) + can_take(s, WP)))
    result = {}
    nodes = 0

    def need_min():
        return sum(max(0, MIN_SIZE - len(cur_groups[t])) for t in team_ids)

    def rec(i):
        nonlocal nodes
        nodes += 1
        if nodes > node_limit:
            raise _SearchLimit()
        if i == len(order):
            return all(not team_problems("", cur_groups[t]) for t in team_ids)
        s = order[i]
        remaining = len(order) - i - 1
        tried_empty = False
        for t in sorted(team_ids, key=lambda t: (len(cur_groups[t]), t)):
            g = cur_groups[t]
            if len(g) >= MAX_SIZE or any(m["main_role"] == s["main_role"] for m in g):
                continue
            if not g:
                if tried_empty:
                    continue
                tried_empty = True
            g.append(s)
            ok = (len(REQUIRED) - required_filled(g) <= MAX_SIZE - len(g)) and need_min() <= remaining
            if ok:
                result[s["id"]] = t
                if rec(i + 1):
                    return True
                del result[s["id"]]
            g.pop()
        return False

    try:
        return dict(result) if rec(0) else None
    except _SearchLimit:
        return None


# ---------------------------------------------------------------- 로그인, 화면 데이터
def login(student_no, password):
    student_no = (student_no or "").strip()

    def fn(cur):
        cur.execute("select student_no, password_hash from tf_students where student_no = %s", (student_no,))
        return cur.fetchone()
    row = tx(fn)
    if row and check_pw(password or "", row["password_hash"]):
        return row["student_no"]
    return None


def load_view(user_no):
    """화면 한 번 그릴 때 필요한 것을 연결 하나로 모두 읽음."""
    def fn(cur):
        V = {}
        cur.execute("select id, student_no, name, main_role, is_admin from tf_students where student_no = %s",
                    (user_no,))
        me = cur.fetchone()
        V["me"] = dict(me) if me else None
        if not me:
            return V
        V["students"] = _students(cur)
        V["me"]["subs"] = V["students"].get(me["id"], {}).get("subs", [])
        cur.execute("select id, name from tf_teams order by id")
        V["teams"] = [dict(r) for r in cur.fetchall()]
        cur.execute("select team_id, student_id from tf_members order by team_id, slot_no")
        members, member_of = {}, {}
        for r in cur.fetchall():
            members.setdefault(r["team_id"], []).append(r["student_id"])
            member_of[r["student_id"]] = r["team_id"]
        V["members"], V["member_of"] = members, member_of
        cur.execute("select id, team_id, student_id from tf_applications where status = '대기' order by id")
        V["pending"] = [dict(r) for r in cur.fetchall()]
        cur.execute("select v.application_id, v.voter_id, v.vote from tf_votes v "
                    "join tf_applications a on a.id = v.application_id where a.status = '대기'")
        votes = {}
        for r in cur.fetchall():
            votes.setdefault(r["application_id"], {})[r["voter_id"]] = r["vote"]
        V["votes"] = votes
        cur.execute("select key, value from tf_settings")
        settings = {r["key"]: r["value"] for r in cur.fetchall()}
        V["closed"] = settings.get("closed") == "1"
        V["proposal"] = json.loads(settings["proposal"]) if settings.get("proposal") else None
        V["assign_note"] = settings.get("assign_note")
        cur.execute("select a.status, a.reason, t.name as team_name from tf_applications a "
                    "join tf_teams t on t.id = a.team_id where a.student_id = %s order by a.id desc limit 1",
                    (me["id"],))
        last = cur.fetchone()
        V["my_last_app"] = dict(last) if last else None
        return V
    return tx(fn)


# ---------------------------------------------------------------- 학생 동작: 지원, 투표
def apply_to_team(user_no, team_id):
    def fn(cur):
        _lock_shared(cur)
        cur.execute("select id, name from tf_teams where id = %s for update", (team_id,))
        team = cur.fetchone()
        if not team:
            raise Blocked("없는 팀")
        me = _student_by_no(cur, user_no)
        if not me:
            raise Blocked("학생 계정만 지원할 수 있음")
        if _is_closed(cur):
            raise Blocked("모집이 마감됨")
        cur.execute("select 1 from tf_members where student_id = %s", (me["id"],))
        if cur.fetchone():
            raise Blocked("이미 팀이 있음")
        cur.execute("select 1 from tf_applications where student_id = %s and status = '대기'", (me["id"],))
        if cur.fetchone():
            raise Blocked("결과를 기다리는 지원이 있음")
        members = _team_members(cur, team_id)
        prob = join_problem(members, me)
        if prob:
            raise Blocked(prob)
        if not members:  # 빈 팀: 투표할 팀원이 없으므로 바로 승인
            cur.execute("insert into tf_members (team_id, slot_no, student_id) values (%s, 1, %s)",
                        (team_id, me["id"]))
            cur.execute("insert into tf_applications (team_id, student_id, status, reason, decided_at) "
                        "values (%s, %s, '승인', '빈 팀에 지원', now())", (team_id, me["id"]))
            return f"승인: {team['name']}의 팀원이 됨 (빈 팀이라 바로 승인)"
        cur.execute("insert into tf_applications (team_id, student_id, status) values (%s, %s, '대기')",
                    (team_id, me["id"]))
        return f"대기: {team['name']} 팀원 {len(members)}명의 투표를 기다림"
    return _run_action(fn)


def _settle(cur, app_id, team, applicant):
    """모든 팀원이 찬성했는지 보고, 그렇다면 승인 직전 검사 후 승인/거절함."""
    members = _team_members(cur, team["id"])
    cur.execute("select voter_id from tf_votes where application_id = %s and vote = '찬성'", (app_id,))
    yes = {r["voter_id"] for r in cur.fetchall()}
    waiting = [m["name"] for m in members if m["id"] not in yes]
    if waiting:
        return (f"대기: {applicant['name']}의 지원 - 찬성 {len(members) - len(waiting)}/{len(members)}, "
                f"아직 투표하지 않은 팀원: {', '.join(waiting)}")
    prob = join_problem(members, applicant)  # 승인 직전 재검사
    if prob:
        cur.execute("update tf_applications set status = '거절', reason = %s, decided_at = now() where id = %s",
                    ("승인 직전 검사: " + prob, app_id))
        return f"거절: {applicant['name']}의 지원 - 승인 직전 검사에서 규칙을 어김({prob})"
    slot = _free_slot(cur, team["id"])
    cur.execute("insert into tf_members (team_id, slot_no, student_id) values (%s, %s, %s)",
                (team["id"], slot, applicant["id"]))
    cur.execute("update tf_applications set status = '승인', reason = '모든 팀원 찬성', decided_at = now() "
                "where id = %s", (app_id,))
    return f"승인: {applicant['name']}이(가) {team['name']}의 팀원이 됨"


def vote(voter_no, applicant_no, vote_value):
    assert vote_value in ("찬성", "거부")

    def fn(cur):
        _lock_shared(cur)
        voter = _student_by_no(cur, voter_no)
        applicant = _student_by_no(cur, applicant_no)
        if not voter or not applicant:
            raise Blocked("없는 학생")
        cur.execute("select id, team_id from tf_applications where student_id = %s and status = '대기'",
                    (applicant["id"],))
        app = cur.fetchone()
        if not app:
            cur.execute("select status, reason from tf_applications where student_id = %s "
                        "order by id desc limit 1", (applicant["id"],))
            last = cur.fetchone()
            if last:
                return f"{last['status']}: {applicant['name']}의 지원은 이미 처리됨 ({last['reason'] or ''})"
            raise Blocked("대기 중인 지원이 없음")
        # 팀 → 지원 순서로 잠가서 같은 팀의 처리가 한 번에 하나씩 되게 함(처리 순서 = 투표가 끝난 순서)
        cur.execute("select id, name from tf_teams where id = %s for update", (app["team_id"],))
        team = cur.fetchone()
        cur.execute("select status from tf_applications where id = %s for update", (app["id"],))
        if cur.fetchone()["status"] != "대기":
            raise Blocked("이미 처리된 지원")
        if _is_closed(cur):
            raise Blocked("모집이 마감됨")
        cur.execute("select 1 from tf_members where team_id = %s and student_id = %s", (team["id"], voter["id"]))
        if not cur.fetchone():
            raise Blocked("그 팀의 팀원만 투표할 수 있음")
        cur.execute("insert into tf_votes (application_id, voter_id, vote) values (%s, %s, %s) "
                    "on conflict do nothing", (app["id"], voter["id"], vote_value))
        if cur.rowcount == 0:
            raise Blocked("이미 투표함 (투표는 바꿀 수 없음)")
        if vote_value == "거부":
            cur.execute("update tf_applications set status = '거절', reason = %s, decided_at = now() "
                        "where id = %s", (f"{voter['name']}이(가) 거부", app["id"]))
            return f"거절: {voter['name']}이(가) 거부해서 {applicant['name']}의 지원이 거절됨"
        return _settle(cur, app["id"], team, applicant)
    return _run_action(fn)


def _run_action(fn):
    try:
        return tx(fn)
    except Blocked as e:
        return f"막힘: {e}"
    except psycopg2.IntegrityError:
        return "막힘: 다른 학생이 먼저 그 자리에 들어감 (DB 유일성 제약)"
    except Exception as e:
        return f"막힘: 처리 중 오류가 남 ({type(e).__name__}: {safe_error(e)})"


# ---------------------------------------------------------------- 관리자 동작
def parse_roster(data: bytes):
    try:
        text = data.decode("utf-8-sig")  # BOM 이 있어도 읽음
    except UnicodeDecodeError:
        raise Blocked("CSV 를 UTF-8 로 읽을 수 없음")
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows:
        raise Blocked("빈 CSV 파일")
    header = [c.strip() for c in rows[0]]
    if header != ["student_no", "name", "main_role", "sub_roles"]:
        raise Blocked("첫 줄이 student_no,name,main_role,sub_roles 이어야 함")
    out, seen = [], set()
    for ln, r in enumerate(rows[1:], start=2):
        if len(r) != 4:
            raise Blocked(f"{ln}번째 줄의 열이 4개가 아님")
        no, name, main, subs = [c.strip() for c in r]
        if not no or not name:
            raise Blocked(f"{ln}번째 줄에 학번이나 이름이 없음")
        if no == "admin":
            raise Blocked("학번 admin 은 관리자용임")
        if no in seen:
            raise Blocked(f"학번이 겹침({no})")
        seen.add(no)
        if main not in ROLES:
            raise Blocked(f"{ln}번째 줄의 주 역할이 6개 역할이 아님({main})")
        sub_list = []
        for x in subs.split(";"):
            x = x.strip()
            if not x:
                continue
            if x not in ROLES:
                raise Blocked(f"{ln}번째 줄의 보조 역할이 6개 역할이 아님({x})")
            if x != main and x not in sub_list:
                sub_list.append(x)
        if len(sub_list) > 2:
            raise Blocked(f"{ln}번째 줄의 보조 역할이 2개를 넘음")
        out.append({"student_no": no, "name": name, "main_role": main, "subs": sub_list})
    if not out:
        raise Blocked("학생이 없음")
    return out


def start_roster(data: bytes, team_count):
    """명단으로 처음 상태를 만듦: 지원, 투표, 팀원, 마감 표시를 지우고 학생과 빈 팀을 만듦. 관리자 계정은 남김."""
    try:
        if not data:
            raise Blocked("명단 CSV 파일을 먼저 올려야 함")
        team_count = int(team_count)
        if not 1 <= team_count <= 20:
            raise Blocked("팀 수는 1~20 이어야 함")
        rows = parse_roster(data)
    except Blocked as e:
        return f"막힘: {e}"
    hashes = [hash_pw(STUDENT_PASSWORD) for _ in rows]

    def fn(cur):
        _lock_exclusive(cur)
        cur.execute("delete from tf_votes")
        cur.execute("delete from tf_applications")
        cur.execute("delete from tf_members")
        cur.execute("delete from tf_teams")
        cur.execute("delete from tf_students where not is_admin")  # 보조 역할은 cascade 로 지워짐
        cur.execute("delete from tf_settings")
        for i in range(1, team_count + 1):
            cur.execute("insert into tf_teams (id, name) values (%s, %s)", (i, f"팀{i}"))
        for r, h in zip(rows, hashes):
            cur.execute("insert into tf_students (student_no, name, main_role, password_hash, is_admin) "
                        "values (%s, %s, %s, %s, false) returning id",
                        (r["student_no"], r["name"], r["main_role"], h))
            sid = cur.fetchone()["id"]
            for role in r["subs"]:
                cur.execute("insert into tf_student_sub_roles (student_id, role) values (%s, %s)", (sid, role))
        _set_setting(cur, "closed", "0")
        return f"승인: 학생 {len(rows)}명, 팀 {team_count}개로 처음 상태를 만듦"
    return _run_action(fn)


def close_recruitment():
    def fn(cur):
        _lock_exclusive(cur)
        if _is_closed(cur):
            raise Blocked("이미 모집을 마감함")
        cur.execute("update tf_applications set status = '거절', reason = '모집 마감', decided_at = now() "
                    "where status = '대기'")
        rejected = cur.rowcount
        cur.execute("delete from tf_members where team_id in "
                    "(select team_id from tf_members group by team_id having count(*) = 1)")
        removed = cur.rowcount
        _set_setting(cur, "closed", "1")
        _set_setting(cur, "proposal", None)
        _set_setting(cur, "assign_note", None)
        return f"승인: 모집을 마감함 (대기 지원 {rejected}건 거절, 1명뿐인 팀에서 {removed}명 빠짐)"
    return _run_action(fn)


def _assignment_input(cur):
    cur.execute("select id, name from tf_teams order by id")
    teams = cur.fetchall()
    team_ids = [t["id"] for t in teams]
    names = {t["id"]: t["name"] for t in teams}
    groups = {t: _team_members(cur, t) for t in team_ids}
    studs = _students(cur)
    cur.execute("select student_id from tf_members")
    in_team = {r["student_id"] for r in cur.fetchall()}
    pool = [s for s in studs.values() if s["id"] not in in_team]
    return team_ids, names, groups, pool


# ---------------------------------------------------------------- AI 배정
def build_prompt(team_ids, names, groups, pool):
    def info(s, with_no=False):
        d = {"name": s["name"], "main_role": s["main_role"], "sub_roles": s["subs"]}
        if with_no:
            d = {"student_no": s["student_no"], **d}
        return d
    data = {
        "teams": [{"team": t, "name": names[t], "members": [info(s) for s in groups[t]]} for t in team_ids],
        "unassigned_students": [info(s, True) for s in pool],
    }
    return (
        "팀 프로젝트의 팀 편성 문제임. 팀이 없는 학생(unassigned_students)을 모두 팀에 배정해야 함.\n"
        "이미 팀에 있는 학생은 옮기지 않음.\n\n"
        "규칙(모두 지켜야 함):\n"
        "1. 팀이 없는 학생은 빠짐없이, 한 학생은 한 팀에만 배정함.\n"
        "2. 배정한 뒤 모든 팀은 2명 이상 4명 이하임(빈 팀도 2명 이상으로 채워야 함).\n"
        "3. 한 팀 안에서 주 역할(main_role)이 같은 학생이 2명 이상이면 안 됨.\n"
        "4. 모든 팀에 '기획자'를 맡을 학생과 '웹 프로그래머'를 맡을 학생이 서로 다른 사람으로 1명씩 있어야 함. "
        "주 역할이나 보조 역할(sub_roles)로 맡을 수 있음. 한 학생이 두 역할을 동시에 맡을 수는 없음.\n\n"
        "데이터(JSON):\n" + json.dumps(data, ensure_ascii=False, indent=1) + "\n\n"
        "답은 다음 형식의 JSON 하나만 출력함. 설명은 쓰지 않음. team 은 팀 번호(정수)임.\n"
        '{"assignments": [{"student_no": "학번", "team": 1}]}'
    )


def call_openrouter(model, prompt, key):
    r = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "X-Title": "Team Formation App"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": "너는 규칙을 정확히 지키는 팀 배정 도우미임. JSON 만 출력함."},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 8000,
            "temperature": 0,
            "reasoning": {"effort": "low"},
        },
        timeout=(10, MODEL_TIMEOUT_SEC),
    )
    if r.status_code != 200:
        detail = ""
        try:
            detail = r.json().get("error", {}).get("message", "")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {r.status_code} {detail}".strip())
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"오류 응답: {data['error'].get('message', '')}")
    choices = data.get("choices") or []
    content = (choices[0].get("message", {}).get("content") if choices else None) or ""
    if not content.strip():
        raise RuntimeError("빈 응답")
    return content


def parse_ai_answer(text, pool):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    raw = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    obj = json.loads(raw)
    items = obj.get("assignments") if isinstance(obj, dict) else obj
    if not isinstance(items, list):
        raise ValueError("assignments 목록이 없음")
    by_no = {s["student_no"]: s for s in pool}
    pairs = []
    for it in items:
        no = str(it.get("student_no", "")).strip()
        tm = re.search(r"\d+", str(it.get("team", "")))
        if not tm:
            raise ValueError(f"팀 번호를 읽을 수 없음({it})")
        sid = by_no[no]["id"] if no in by_no else f"학번 {no}"
        pairs.append((sid, int(tm.group())))
    return pairs


def _log_ai(attempt, model, outcome, detail, elapsed):
    try:
        tx(lambda cur: cur.execute(
            "insert into tf_ai_logs (attempt, model, outcome, detail, elapsed_sec) values (%s,%s,%s,%s,%s)",
            (attempt, model, outcome, detail[:1000], round(elapsed, 1))))
    except Exception:
        pass


def ai_assign(team_ids, names, groups, pool, on_status):
    """1차 → 2차 → 3차 모델 순서로 부름. 모델마다 120초 전체 대기 시간을 재서 넘으면 끊음.
    통과한 배정안이 있으면 (pairs, 출처, 기록), 모두 실패하면 (None, None, 기록)."""
    key = get_secret("OPENROUTER_API_KEY")
    history = []
    if not key:
        history.append("✗ OPENROUTER_API_KEY 가 없어 AI 를 부르지 못함")
        return None, None, history
    prompt = build_prompt(team_ids, names, groups, pool)
    for i, model in enumerate(get_models(), start=1):
        t0 = time.monotonic()
        ex = ThreadPoolExecutor(max_workers=1)  # 모델마다 새 스레드: 끊은 호출이 다음 호출을 막지 않음
        fut = ex.submit(call_openrouter, model, prompt, key)
        outcome, content, detail = None, None, ""
        while True:
            elapsed = time.monotonic() - t0
            on_status(history, f"⏳ {i}차 모델({model})의 응답을 기다리는 중… {elapsed:.0f}초 / {MODEL_TIMEOUT_SEC}초")
            remain = MODEL_TIMEOUT_SEC - elapsed
            if remain <= 0:
                outcome, detail = "시간 초과", f"{MODEL_TIMEOUT_SEC}초 안에 응답하지 않아 끊음"
                break
            try:
                content = fut.result(timeout=min(1.0, remain))
                outcome = "응답"
                break
            except FutureTimeout:
                continue
            except Exception as e:
                outcome, detail = "오류", safe_error(e)
                break
        ex.shutdown(wait=False, cancel_futures=True)
        elapsed = time.monotonic() - t0
        if outcome == "응답":
            try:
                pairs = parse_ai_answer(content, pool)
                probs = validate_assignment(team_ids, names, groups, pool, pairs)
            except Exception as e:
                pairs, probs = None, [f"답을 JSON 으로 읽지 못함({safe_error(e)})"]
            if not probs:
                _log_ai(i, model, "통과", "검사 다섯 가지 통과", elapsed)
                history.append(f"✓ {i}차 모델({model}): 배정안이 검사를 통과함 ({elapsed:.0f}초)")
                return pairs, f"{i}차 모델({model})", history
            outcome, detail = "검사 불통과", "; ".join(probs[:3])
        _log_ai(i, model, outcome, detail, elapsed)
        history.append(f"✗ {i}차 모델({model}): {outcome} - {detail} ({elapsed:.0f}초)")
    return None, None, history


def run_assignment(on_status):
    """'AI 배정' 단추. 결과 안내 문장을 돌려줌."""
    try:
        closed, (team_ids, names, groups, pool) = tx(lambda cur: (_is_closed(cur), _assignment_input(cur)))
    except Exception as e:
        return f"막힘: DB 오류 ({safe_error(e)})"
    if not closed:
        return "막힘: 먼저 모집을 마감해야 함"

    reason = diagnose(team_ids, names, groups, pool)
    solution = None
    if reason is None:
        solution = solve_assignment(team_ids, groups, pool)
        if solution is None:
            reason = "모든 경우를 따져 보았지만, 기획자와 웹 프로그래머를 맡을 학생이 모자라 모든 팀을 동시에 채울 수 없음"
    if reason:  # 규칙을 지키는 배정이 없음 → AI 를 부르지 않음
        note = f"규칙을 지키는 배정이 없음: {reason}"
        tx(lambda cur: (_set_setting(cur, "proposal", None), _set_setting(cur, "assign_note", note)))
        on_status([], "❌ " + note)
        return f"막힘: {note}"

    if not pool:
        pairs, source, history = [], "팀이 없는 학생이 없음", []
    else:
        pairs, source, history = ai_assign(team_ids, names, groups, pool, on_status)
        if pairs is None:  # 3차 모델까지 실패 → 앱이 스스로 배정(모든 경우를 따진 결과)
            pairs = list(solution.items())
            source = "앱 자체 배정(3차 모델까지 실패해서, 모든 경우를 따져 찾음)"
            history.append("→ 3차 모델까지 실패해서 앱이 모든 경우를 따져 규칙을 지키는 배정을 찾음")
    probs = validate_assignment(team_ids, names, groups, pool, pairs)
    if probs:  # 안전장치(이론상 일어나지 않음)
        return "막힘: 배정안이 검사를 통과하지 못함 - " + "; ".join(probs)
    proposal = {"source": source, "pairs": [[int(s), int(t)] for s, t in pairs], "history": history,
                "made_at": now_kst().strftime("%H:%M:%S")}
    tx(lambda cur: (_set_setting(cur, "proposal", json.dumps(proposal, ensure_ascii=False)),
                    _set_setting(cur, "assign_note", None)))
    return f"승인: 배정안을 만듦 ({source}). 확인한 뒤 '배정안 확정'을 누름"


def confirm_assignment():
    def fn(cur):
        _lock_exclusive(cur)
        if not _is_closed(cur):
            raise Blocked("모집을 마감한 뒤에 확정할 수 있음")
        raw = _get_setting(cur, "proposal")
        if not raw:
            raise Blocked("확정할 배정안이 없음 (먼저 'AI 배정'을 누름)")
        prop = json.loads(raw)
        team_ids, names, groups, pool = _assignment_input(cur)
        pairs = [(s, t) for s, t in prop["pairs"]]
        probs = validate_assignment(team_ids, names, groups, pool, pairs)
        if probs:
            raise Blocked("배정안이 지금 상태와 맞지 않음 - " + probs[0])
        for sid, tid in pairs:
            cur.execute("insert into tf_members (team_id, slot_no, student_id) values (%s, %s, %s)",
                        (tid, _free_slot(cur, tid), sid))
        _set_setting(cur, "proposal", None)
        _set_setting(cur, "assign_note", f"배정을 확정함 ({prop['source']}, {len(pairs)}명)")
        return f"승인: 배정안을 확정함 ({len(pairs)}명 배정)"
    return _run_action(fn)
