# 팀 프로젝트 팀 편성 웹 앱 (중간 과제)

작성자: (본인 이름)

> ⚠️ 제출 전에 `TODO`라고 적힌 곳을 모두 본인이 직접 겪은 내용과 화면으로 채울 것.
> 5~7항은 꾸며 내면 시나리오 밖 점수가 0점임(공고 6.3절 20번).

## 1. 배포 주소
TODO: https://(본인 앱 주소).streamlit.app

## 2. PRD와 구현 계획의 위치
- PRD: `docs/PRD.md`
- 구현 계획: `docs/구현 계획.md`
- DB 테이블: `schema.sql`

## 3. 고른 해결 방법
| 기능 | 고른 해결 | 방법 |
|---|---|---|
| 다른 사람의 변경 보기 | 어려운 해결 | `st.fragment(run_every=3)`로 3초마다 바뀐 부분만 다시 그림 |
| 필수 포함 역할을 지킬 수 없는 팀 | 어려운 해결 | 지원할 때 "남는 자리 수 < 아직 못 채운 필수 역할 수"이면 막음 (`core.join_problem`) |
| 같은 팀에 대기 중인 지원이 여럿 | 어려운 해결 | 모든 팀원이 찬성한 순간 규칙을 다시 검사함. 팀 행 잠금으로 투표가 끝난 순서대로 처리 |
| 마지막 자리에 두 명이 동시에 들어감 | 어려운 해결 | `tf_members`의 기본 키 `(team_id, slot_no)`와 `student_id unique` (`schema.sql`) |
| AI 응답이 늦을 때 | 어려운 해결 | 모델마다 호출부터 응답까지 120초 전체 대기 시간을 재서 끊고 다음 모델로 넘어감 (`core.ai_assign`) |
| 3차 모델까지 실패 | 어려운 해결 | 백트래킹으로 모든 경우를 따져 검사 5가지를 통과하는 배정을 찾음 (`core.solve_assignment`) |
| AI 대기 중 화면 | 어려운 해결 | "N차 모델(이름)의 응답을 기다리는 중… 37초 / 120초"를 1초마다 갱신 |
| DB 일시정지 막기 | 어려운 해결 | GitHub Actions 예약 실행으로 매일 DB 질의 (`.github/workflows/keepalive.yml`) |

## 4. 쓴 모델 3개
TODO: 실제로 Secrets에 넣은 모델 이름으로 고칠 것
- 1차: `nvidia/nemotron-3-super-120b-a12b:free`
- 2차: `openai/gpt-oss-120b:free`
- 3차: `qwen/qwen3-coder:free`

## 5. 오픈라우터 AI 모델 호출 실패 사례
TODO: 관리자 화면 "AI 배정" 결과(✗ 줄)나 Supabase의 `tf_ai_logs` 테이블에서 본 실제 실패를 적고 화면을 `img/`에 넣을 것.
예시 형식: "1차 모델(…)이 HTTP 429로 실패해서 2차 모델로 넘어감 (화면 `img/ai-fail.png`)"

## 6. 클로드가 잘못된 추론이나 답변을 한 사례
TODO: 본인이 과제를 하며 실제로 겪은 것을 적을 것(화면이나 커밋 해시를 근거로).

## 7. 제출한 앱에서 발견한 문제점이나 개선 필요 사항
TODO: 연습용 시나리오를 직접 해 보며 발견한 것을 단계 번호와 화면으로 적을 것.
("문제 없음"은 인정되지 않음)

## 실행 방법(로컬)
```bash
pip install -r requirements.txt
# .env.example 을 복사해 .env 로 만들고 값을 채움
streamlit run streamlit_app.py
```
