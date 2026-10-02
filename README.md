# Manuscript Review — 2단 첨삭 HTML

논문 원고를 **왼쪽 원고 / 오른쪽 첨삭 메모** 2단 HTML로 띄워 수정 요청을 적고, `.md`로 내보내면 에이전트(Claude Code)가 원고에 반영하고 다시 빌드하는 킷이다. 반영 후에는 **무엇이 바뀌었는지(diff)** 와 **코멘트별 반영 결과**가 같은 화면에 표시된다.

```
원고(.md/.tex/.txt) ──build_review.py──▶ review.html (왼쪽 원고 · 오른쪽 메모)
      ▲                                        │ 드래그 → 코멘트, 블록 코멘트, 전체 지시
      │                                        ▼
 에이전트가 원고 수정 ◀── revisions/rev_*.md (내보내기)
      │
      └─▶ revisions/rev_*.applied.json + 다시 빌드 → diff·반영 결과 표시 → 다음 라운드
```

필요한 것: Python 3.8+ (표준 라이브러리만 사용), Chrome/Edge 권장, 반영 단계에는 Claude Code.

---

## 1. 준비

1. 원고를 `manuscript/`에 넣는다 (다른 폴더에 있어도 된다).
2. `review.json` 수정:
   ```json
   {
     "id": "my-paper-2026",
     "title": "My Paper Title",
     "subtitle": "ICML 2026 submission draft",
     "authors": "홍길동 - ACAS Lab",
     "sources": ["manuscript/main.tex", "manuscript/sections/intro.tex"],
     "bridge_port": 8788
   }
   ```
   - `sources`: 이 폴더 기준 상대 경로. `.md` · `.tex` · `.txt`, 여러 파일 가능 (`../my-paper/main.tex`도 가능).
   - `id`: 브라우저에 저장되는 코멘트의 이름표. 원고마다 다르게.
3. 빌드:
   ```bash
   python build_review.py          # review.html 생성
   python build_review.py --open   # 생성 후 브라우저로 열기
   python build_review.py --watch  # 원고를 직접 고칠 때마다 자동 재빌드
   ```

지금 들어 있는 `manuscript/draft.md`는 예시 원고다. 바로 `review.html`을 열어 기능을 확인할 수 있다.

## 2. 첨삭하기 (review.html)

| 하고 싶은 것 | 방법 |
|---|---|
| 특정 구절 수정 요청 | 왼쪽 원고에서 **드래그** → 나타나는 **코멘트** 버튼 |
| 블록(문단·수식·표) 전체 요청 | 오른쪽 칸의 **+ 코멘트** |
| 문서 전체 지시 | 상단 **+ 전체 지시** (예: "수동태 줄이기") |
| 바꿀 문장을 직접 제시 | 카드의 **+ 제안 문구** — 에이전트가 이 문구를 우선 사용 |
| 유형 지정 | 수정 · 삭제 · 추가 · 표현 · 질문(원고는 안 고치고 답만) · 기타 |

- 메모는 **브라우저에 자동 저장**된다 (새로고침·재시작해도 남음). 원고를 다시 빌드해도 코멘트는 내용 해시로 블록을 따라간다. 블록이 수정됐으면 새 블록으로 자동 재연결되고, 못 찾으면 상단 **위치를 잃은 코멘트**에 모인다. 어느 경우에도 버려지지 않는다.
- 왼쪽 원고는 원문 문자를 그대로 보여 준다 (마크업은 흐리게 표시). 그래서 드래그한 구절이 원고 파일에 그대로 존재하고, 에이전트가 정확히 찾는다.

## 3. 내보내기 → 반영

**.md 내보내기**를 누르면 미리보기 창이 뜬다.

- **수정 범위**: 최소 수정(요청한 곳만) / 자유 재작성(해당 블록 안에서 문장 재구성 허용)
- 저장 방법 (택1):
  - **폴더에 저장** — 처음 한 번 이 프로젝트 폴더를 지정하면 이후 다이얼로그 없이 `revisions/`에 저장 (Chrome/Edge)
  - **다운로드** — 받은 파일을 `revisions/`로 옮긴다
  - **에이전트에 바로 반영** — 브릿지를 켜 둔 경우 (아래 4)

저장한 뒤 이 폴더에서 Claude Code를 열고 다음과 같이 요청한다:

```
revisions/rev_v001_260930-1520.md 반영해줘
```
(또는 "최근 첨삭 반영해줘"). 에이전트는 `prompts/apply_revisions.md` 절차대로 원고를 고치고, `revisions/<rev>.applied.json`을 남기고, `review.html`을 다시 빌드한다. **review.html을 새로고침**하면:

- **변경 표시** 보기가 자동으로 켜진다. 초록은 추가, 분홍 취소선은 삭제이고, 상단 ↑↓로 변경된 블록 사이를 이동한다.
- 바뀐 블록 옆 오른쪽 칸에 **반영 / 부분 반영 / 보류 / 답변** 카드와 에이전트 메모가 뜬다.
- 반영·답변된 코멘트는 자동 보관된다. 보류·부분 반영된 코멘트는 에이전트 메모와 함께 남으므로, 고쳐 쓰면 다음 내보내기에 "이전 에이전트 응답"과 함께 다시 실린다.

## 4. (선택) 버튼 한 번으로 반영 — 로컬 브릿지

```bash
python review_bridge.py                          # 127.0.0.1:8788 (review.json 의 bridge_port)
REVIEW_BRIDGE_MODEL=sonnet python review_bridge.py   # 헤드리스 모델 지정
```

켜 두면 헤더에 "브릿지 연결됨"이 표시되고, 내보내기 창의 **에이전트에 바로 반영**이 활성화된다. 누르면 `revisions/`에 저장 → 헤드리스 `claude -p`가 반영 → 다시 빌드까지 진행되고, 오른쪽 아래 진행 창에 로그가 흐른다. 끝나면 **새로고침해서 결과 보기**를 누르면 된다. 브릿지가 켜져 있으면 **폴더에 저장**도 폴더 지정 없이 바로 저장된다.

- 편집 권한 때문에 막히면: `REVIEW_BRIDGE_SKIP_PERMS=1 python review_bridge.py` (자기 원고 폴더에서만)
- 이 방식은 상위 킷의 `tools/qa_bridge.py`와 같은 구조다. 브라우저(file://)는 CLI를 직접 실행할 수 없어서 localhost 서버를 거친다.

## 폴더 구조

```
manuscript_review/
├── README.md               ← 이 문서
├── CLAUDE.md               ← 에이전트용 규칙 (이 폴더에서 Claude Code를 열면 자동 로드)
├── review.json             ← 설정 (원고 경로·제목·id)
├── build_review.py         ← 원고 → review.html (+ versions/ 스냅샷, diff)
├── review_bridge.py        ← (선택) 에이전트 바로 반영용 로컬 서버
├── prompts/
│   └── apply_revisions.md  ← 에이전트 반영 절차 정본 (.md 형식·유형별 처리·applied.json 스키마)
├── manuscript/draft.md     ← 예시 원고 (자기 원고로 교체)
├── revisions/              ← rev_*.md (요청) · rev_*.applied.json (결과) · CHANGELOG.md
├── versions/               ← 빌드 스냅샷 (diff 기준, 자동 생성)
├── assets/logo.png         ← 헤더 로고 (없으면 생략)
└── review.html             ← 빌드 산출물 (self-contained, 직접 수정 금지)
```

## 참고

- **다른 위치로 옮기기**: 폴더째 옮기면 된다. 상위 킷의 파일은 참조하지 않는다. 옮긴 뒤 `python build_review.py`를 한 번 실행한다.
- **여러 원고**: 킷 폴더 하나로 여러 프로젝트를 다루려면 프로젝트마다 `review.json`이 있는 폴더를 두고 `python build_review.py <프로젝트 폴더>`, `python review_bridge.py <프로젝트 폴더> --port 8789`처럼 실행한다.
- **블록 분할 규칙**: Markdown은 헤딩 / 빈 줄로 나뉜 문단 / 목록 / `$$` 수식 / 코드 펜스 / 표 단위로 나눈다. LaTeX은 `\section` 류 / 빈 줄 문단 / `\begin…\end` 환경(수식·figure·itemize 등 통째로) / preamble 단위로 나눈다. `.txt`는 빈 줄 문단 단위다.
- **git**: `.gitignore`가 `review.html`과 `versions/`를 제외한다. 요청·결과 기록(`revisions/`)은 남겨 두면 첨삭 이력이 된다.
