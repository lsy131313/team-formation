"""팀 프로젝트 팀 편성 웹 앱 (Streamlit).

화면 요소의 key 는 공고 2.7절 표와 똑같이 맞춤. 로직은 core.py 에 있음.
"""
import streamlit as st

import core

st.set_page_config(page_title="팀 편성", page_icon="👥", layout="wide")
REFRESH_SEC = 3  # 다른 사람의 변경을 3초마다 다시 표시(st.fragment)
ss = st.session_state


@st.cache_resource(show_spinner="DB 준비 중…")
def boot():
    core.init_db()  # schema.sql 실행 + 관리자 계정 만들기/비밀번호 갱신
    return True


try:
    boot()
except Exception as e:  # 비밀 값은 safe_error 로 지운 뒤에만 보여 줌
    st.error("DB 나 비밀 값 설정에 문제가 있음: " + core.safe_error(e))
    st.stop()


# ---------------------------------------------------------------- 공통
def set_msg(msg):
    ss["msg"] = f"{msg}  ·  {core.now_kst():%H:%M:%S}"


def show_msg():
    msg = ss.get("msg", "")
    if not msg:
        st.caption("단추를 누르면 결과가 여기에 보임")
    elif msg.startswith("승인"):
        st.success(msg)
    elif msg.startswith("대기"):
        st.info(msg)
    else:
        st.warning(msg)


def role_text(s):
    t = f"주: {s['main_role']}"
    if s.get("subs"):
        t += f" / 보조: {', '.join(s['subs'])}"
    return t


def logout():
    for k in ("user_no", "msg"):
        ss.pop(k, None)


def whoami(me):
    with st.container(key="whoami"):
        c1, c2 = st.columns([5, 1])
        who = "관리자" if me["is_admin"] else role_text(me)
        c1.markdown(f"**{me['name']}** ({me['student_no']}) · {who}")
        if c2.button("로그아웃", key="logout"):
            logout()
            st.rerun()


def render_team(V, team, my_team_id=None):
    member_ids = V["members"].get(team["id"], [])
    group = [V["students"][i] for i in member_ids]
    mine = "  🟢 **내 팀**" if team["id"] == my_team_id else ""
    st.markdown(f"#### {team['name']} ({len(group)}/4명){mine}")
    pl = any(core.can_take(s, core.PL) for s in group)
    wp = any(core.can_take(s, core.WP) for s in group)
    st.caption(f"기획자 {'✓' if pl else '✗'} · 웹 프로그래머 {'✓' if wp else '✗'}")
    if not group:
        st.caption("(빈 팀)")
    for s in group:
        st.markdown(f"- **{s['name']}** — {role_text(s)}")


# ---------------------------------------------------------------- 로그인
def cb_login():
    no = ss.get("login_student_no", "")
    pw = ss.get("login_password", "")
    try:
        user = core.login(no, pw)
    except Exception as e:
        set_msg(f"막힘: 로그인 중 오류 ({core.safe_error(e)})")
        return
    if user:
        ss["user_no"] = user
        ss["msg"] = ""
    else:
        set_msg("막힘: 학번 또는 비밀번호가 틀림")


def login_page():
    st.title("👥 팀 프로젝트 팀 편성")
    with st.form("login_form"):
        st.text_input("학번", key="login_student_no")
        st.text_input("비밀번호", type="password", key="login_password")
        st.form_submit_button("로그인", key="login_submit", on_click=cb_login)
    with st.container(key="result"):
        show_msg()


# ---------------------------------------------------------------- 학생 화면
def cb_apply(team_id):
    set_msg(core.apply_to_team(ss["user_no"], team_id))


def cb_vote(applicant_no, value):
    set_msg(core.vote(ss["user_no"], applicant_no, value))


@st.fragment(run_every=REFRESH_SEC)
def student_view():
    if "user_no" not in ss:
        st.rerun()
    V = core.load_view(ss["user_no"])
    me = V["me"]
    if me is None:  # 명단이 바뀌어 계정이 없어짐
        logout()
        st.rerun()
    whoami(me)
    with st.container(key="result"):
        show_msg()

    my_team = V["member_of"].get(me["id"])
    my_pending = next((a for a in V["pending"] if a["student_id"] == me["id"]), None)
    team_name = {t["id"]: t["name"] for t in V["teams"]}

    if V["closed"]:
        st.error("모집이 마감됨")
    if my_team:
        st.success(f"내 팀: {team_name[my_team]}")
    else:
        st.info("아직 팀이 없음")
    if my_pending:
        n_members = len(V["members"].get(my_pending["team_id"], []))
        n_yes = sum(1 for v in V["votes"].get(my_pending["id"], {}).values() if v == "찬성")
        st.write(f"내 지원: {team_name[my_pending['team_id']]} — 대기 (찬성 {n_yes}/{n_members})")
    elif V["my_last_app"] and not my_team:
        la = V["my_last_app"]
        st.write(f"최근 지원: {la['team_name']} — {la['status']} ({la['reason'] or ''})")

    can_apply = not my_team and not my_pending and not V["closed"]
    st.subheader("팀 목록")
    cols = st.columns(2)
    for i, t in enumerate(V["teams"]):
        with cols[i % 2]:
            with st.container(key=f"team_{t['id']}", border=True):
                render_team(V, t, my_team)
                st.button("이 팀에 지원", key=f"apply_{t['id']}", disabled=not can_apply,
                          on_click=cb_apply, args=(t["id"],))

    if my_team:
        st.subheader("내 팀에 들어온 지원")
        apps = [a for a in V["pending"] if a["team_id"] == my_team]
        if not apps:
            st.caption("대기 중인 지원이 없음")
        for a in apps:
            s = V["students"][a["student_id"]]
            with st.container(border=True):
                st.markdown(f"**{s['name']}** ({s['student_no']}) — {role_text(s)}")
                mine = V["votes"].get(a["id"], {}).get(me["id"])
                if mine:
                    st.write(f"내 투표: {mine}")
                else:
                    c1, c2, _ = st.columns([1, 1, 4])
                    c1.button("찬성", key=f"vote_yes_{s['student_no']}", on_click=cb_vote,
                              args=(s["student_no"], "찬성"))
                    c2.button("거부", key=f"vote_no_{s['student_no']}", on_click=cb_vote,
                              args=(s["student_no"], "거부"))


# ---------------------------------------------------------------- 관리자 화면
def cb_start():
    f = ss.get("admin_roster_file")
    set_msg(core.start_roster(f.getvalue() if f else b"", ss.get("admin_team_count", 3)))


def cb_close():
    set_msg(core.close_recruitment())


def cb_confirm():
    set_msg(core.confirm_assignment())


def render_proposal(V):
    p = V["proposal"]
    for h in p.get("history", []):
        st.write(h)
    st.success(f"배정안 ({p['source']}, {p.get('made_at', '')}) — 검사 다섯 가지를 통과함")
    team_name = {t["id"]: t["name"] for t in V["teams"]}
    rows = []
    for sid, tid in p["pairs"]:
        s = V["students"].get(sid)
        if s:
            rows.append({"이름": s["name"], "학번": s["student_no"], "주 역할": s["main_role"],
                         "보조 역할": ", ".join(s["subs"]), "배정된 팀": team_name.get(tid, tid)})
    if rows:
        st.dataframe(rows, hide_index=True)
    else:
        st.caption("배정할 학생이 없음")


@st.fragment(run_every=REFRESH_SEC)
def admin_live():
    V = core.load_view(ss["user_no"])
    st.subheader("팀 현황" + (" (모집 마감됨)" if V["closed"] else " (모집 중)"))
    cols = st.columns(2)
    for i, t in enumerate(V["teams"]):
        with cols[i % 2]:
            with st.container(key=f"team_{t['id']}", border=True):
                render_team(V, t)
    st.subheader("팀이 없는 학생")
    with st.container(key="unassigned", border=True):
        rest = [s for s in V["students"].values() if s["id"] not in V["member_of"]]
        if rest:
            for s in rest:
                st.markdown(f"- **{s['name']}** — {role_text(s)}")
        else:
            st.caption("없음")
    if V["pending"]:
        st.subheader("대기 중인 지원")
        tn = {t["id"]: t["name"] for t in V["teams"]}
        for a in V["pending"]:
            st.write(f"- {V['students'][a['student_id']]['name']} → {tn[a['team_id']]}")


def admin_page(me):
    whoami(me)
    res_ph = st.container(key="result").empty()

    st.subheader("1. 명단 올리기")
    c1, c2 = st.columns([3, 1])
    c1.file_uploader("명단 CSV", type=["csv"], key="admin_roster_file")
    c2.number_input("팀 수", min_value=1, max_value=20, value=3, step=1, key="admin_team_count")
    st.button("명단으로 처음 상태 만들기", key="admin_start", on_click=cb_start)

    st.subheader("2. 모집 마감")
    st.button("모집 마감", key="admin_close", on_click=cb_close)

    st.subheader("3. AI 배정")
    clicked = st.button("AI 배정", key="admin_assign")
    status = st.container(key="assign_status", border=True)
    if clicked:
        res_ph.empty()
        with status:
            ph = st.empty()

        def on_status(history, current):
            with ph.container():
                for h in history:
                    st.write(h)
                st.info(current)

        set_msg(core.run_assignment(on_status))
        ph.empty()
    V = core.load_view(ss["user_no"])
    with status:
        if V["proposal"]:
            render_proposal(V)
        elif V["assign_note"]:
            if V["assign_note"].startswith("규칙을 지키는 배정이 없음"):
                st.error(V["assign_note"])
            else:
                st.write(V["assign_note"])
        else:
            st.caption("모집을 마감한 뒤 'AI 배정'을 누름")
    if V["proposal"]:
        st.button("배정안 확정", key="admin_confirm", on_click=cb_confirm, type="primary")

    st.divider()
    admin_live()
    with res_ph.container():
        show_msg()


# ---------------------------------------------------------------- 시작
if "user_no" not in ss:
    login_page()
else:
    try:
        is_admin = core.load_view(ss["user_no"])["me"]
    except Exception as e:
        st.error("DB 오류: " + core.safe_error(e))
        st.stop()
    if is_admin is None:
        logout()
        st.rerun()
    elif is_admin["is_admin"]:
        admin_page(is_admin)
    else:
        student_view()
