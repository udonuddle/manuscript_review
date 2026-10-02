# Manuscript Review — 원고 첨삭 워크스페이스

사용자가 **자기 논문 원고**를 쓰면서 2단 HTML(`review.html` — 왼쪽 원고 / 오른쪽 첨삭 메모)에 수정 요청을 남기고, 그것을 `.md`로 내보내면 에이전트(나)가 원고에 반영하는 워크스페이스다.

> 이 폴더가 `paper-review-kit`(논문 학습 HTML 킷) 안에 있더라도 **그 킷의 규칙(8탭·번역·v4 빌드 절차·감성 온도 정책 등)은 여기에 적용되지 않는다.** 여기서의 기준은 사용자의 원고와 이 문서, `prompts/apply_revisions.md`뿐이다. 디자인 토큰만 그 킷의 v4 팔레트를 빌려 왔다.

## 구성

| 경로 | 역할 |
|---|---|
| `review.json` | 설정 — `sources`(원고 파일, .md/.tex/.txt, 여러 개 가능), `title`, `id`, `bridge_port` |
| `manuscript/` | 원고 기본 위치 (다른 곳에 있어도 `sources`에 상대 경로로 적으면 된다) |
| `build_review.py` | 원고 → `review.html`. 원고가 바뀐 빌드마다 `versions/vNNN/` 스냅샷을 만들고 직전 버전과의 diff를 HTML에 넣는다 |
| `revisions/rev_*.md` | 사용자가 내보낸 첨삭 요청 (**읽기 전용** — 수정하지 않는다) |
| `revisions/rev_*.applied.json` | 내가 쓰는 반영 결과 (코멘트별 status·note) → 다음 빌드에서 HTML에 표시 |
| `revisions/CHANGELOG.md` | 라운드별 한 줄 기록 |
| `prompts/apply_revisions.md` | **반영 절차 정본** |
| `review_bridge.py` | (선택) HTML 버튼 → 헤드리스 `claude -p`로 반영을 부르는 로컬 서버 |
| `versions/`, `review.html` | 빌드 산출물 — 직접 수정 금지 |

## 요청을 받았을 때

- "revisions/rev_….md 반영해줘", "최근 첨삭 반영해줘", "첨삭 반영" → **`prompts/apply_revisions.md`를 읽고 그 절차대로** 진행한다. 끝나면 `python build_review.py`로 다시 빌드한다.
- "원고 바꿨으니 다시 빌드해줘" → `python build_review.py`.
- 새 원고로 시작 → `review.json`의 `sources`·`title`·`id`를 바꾸고 빌드. `id`를 바꾸면 브라우저에 남은 이전 원고의 코멘트와 섞이지 않는다.

## 핵심 원칙 (정본은 prompts/apply_revisions.md)

1. 요청한 곳만 고친다 — 다른 문장·공백·줄바꿈을 건드리면 다음 라운드의 diff가 흐려진다.
2. 질문 유형은 답만 하고 원고는 고치지 않는다.
3. 인용·참조·수식·수치는 요청 없이는 그대로. 근거 없는 인용·결과를 지어내지 않는다(자리표시 + `partial`).
4. 모든 cid에 대해 `applied.json`을 남긴다 — 사용자는 그 note를 해당 블록 옆에서 읽는다.
