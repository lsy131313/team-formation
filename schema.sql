-- 팀 편성 앱의 테이블 (모든 이름은 tf_ 로 시작)
-- 앱이 시작할 때 이 파일을 그대로 실행함(create ... if not exists 라서 여러 번 실행해도 안전함).
-- Supabase SQL 편집기에 붙여 넣어 직접 실행해도 됨.

-- 학생과 관리자. 비밀번호는 bcrypt 해시로만 저장함(password_hash)
create table if not exists tf_students (
    id            bigserial primary key,
    student_no    text not null unique,
    name          text not null,
    main_role     text check (main_role in ('기획자', '웹 프로그래머', '프론트엔드 디자이너', '테스터', '보고서 작성자', '발표자')),
    password_hash text not null,
    is_admin      boolean not null default false
);

-- 보조 역할. 학생 1명에 0~2행
create table if not exists tf_student_sub_roles (
    student_id bigint not null references tf_students(id) on delete cascade,
    role       text not null check (role in ('기획자', '웹 프로그래머', '프론트엔드 디자이너', '테스터', '보고서 작성자', '발표자')),
    primary key (student_id, role)
);

-- 팀. id 가 곧 팀 번호(팀1 → id 1)
create table if not exists tf_teams (
    id   integer primary key,
    name text not null unique
);

-- 팀원.
--  (team_id, slot_no) 기본 키: 같은 자리에 두 명이 들어갈 수 없음 → 마지막 자리에 동시에 들어오면 먼저 도착한 학생만 기록됨
--  student_id unique: 한 학생은 한 팀에만 소속
--  slot_no 1~4: 다섯 번째 자리를 막음
create table if not exists tf_members (
    team_id    integer not null references tf_teams(id) on delete cascade,
    slot_no    integer not null check (slot_no between 1 and 4),
    student_id bigint  not null unique references tf_students(id) on delete cascade,
    joined_at  timestamptz not null default now(),
    primary key (team_id, slot_no)
);

-- 지원
create table if not exists tf_applications (
    id         bigserial primary key,
    team_id    integer not null references tf_teams(id) on delete cascade,
    student_id bigint  not null references tf_students(id) on delete cascade,
    status     text not null check (status in ('대기', '승인', '거절')),
    reason     text,
    created_at timestamptz not null default now(),
    decided_at timestamptz
);
-- 한 학생은 결과를 기다리는 지원을 하나만 가질 수 있음
create unique index if not exists tf_applications_one_pending
    on tf_applications (student_id) where status = '대기';

-- 투표. 한 사람은 한 지원에 한 번만 투표함
create table if not exists tf_votes (
    application_id bigint not null references tf_applications(id) on delete cascade,
    voter_id       bigint not null references tf_students(id) on delete cascade,
    vote           text not null check (vote in ('찬성', '거부')),
    created_at     timestamptz not null default now(),
    primary key (application_id, voter_id)
);

-- 설정(마감 여부, 배정안 등)
create table if not exists tf_settings (
    key   text primary key,
    value text
);

-- AI 호출 기록(README 의 실패 사례 근거로 씀)
create table if not exists tf_ai_logs (
    id          bigserial primary key,
    created_at  timestamptz not null default now(),
    attempt     integer,
    model       text,
    outcome     text,
    detail      text,
    elapsed_sec numeric
);

-- Supabase 의 공개 REST API(anon 키)로는 읽고 쓸 수 없게 RLS 를 켬.
-- 앱은 DATABASE_URL(postgres 사용자)로 직접 접속하므로 영향이 없음.
alter table tf_students          enable row level security;
alter table tf_student_sub_roles enable row level security;
alter table tf_teams             enable row level security;
alter table tf_members           enable row level security;
alter table tf_applications      enable row level security;
alter table tf_votes             enable row level security;
alter table tf_settings          enable row level security;
alter table tf_ai_logs           enable row level security;
