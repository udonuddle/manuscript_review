#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_review.py - 원고(.md / .tex / .txt) → 2단 첨삭 HTML(review.html).

왼쪽 = 원고(블록 단위, 원문 문자 1:1 보존), 오른쪽 = 첨삭 메모.
브라우저에서 메모를 적고 "내보내기"로 revisions/rev_*.md 를 만들면, 에이전트(Claude Code)가
prompts/apply_revisions.md 절차대로 원고를 고치고 이 스크립트를 다시 돌린다.

핵심 설계
---------
- **블록 = 수정 단위**: 헤딩 / 문단 / 목록 / 수식 / 코드 / 표 / LaTeX 환경을 한 블록으로 자른다.
  블록 텍스트는 원고 문자 그대로(마크업 포함)라서, 브라우저에서 드래그한 구절이 원고에
  그대로 존재한다 → 에이전트가 구절 검색으로 위치를 정확히 찾는다.
- **블록 키 = 내용 해시**: 메모는 위치가 아니라 내용 해시에 붙는다. 원고가 바뀌어 블록이
  사라지면 그 메모는 "위치를 잃은 코멘트"로 따로 보이고, 버려지지 않는다.
- **버전 스냅샷**: 원고 내용이 바뀐 빌드마다 versions/vNNN/ 에 원고를 복사해 둔다.
  HTML 은 직전 버전과의 블록 정렬 + 단어 diff 를 품고 있어 "변경 표시"로 에이전트가
  무엇을 고쳤는지 바로 보인다.
- **반영 결과**: 에이전트가 쓴 revisions/<rev>.applied.json 을 읽어, 코멘트별 반영/보류/답변을
  해당 블록 옆(오른쪽 칸)과 상단 패널에 보여준다. 반영·답변된 코멘트는 브라우저가 자동 보관.
- **self-contained**: 로고까지 base64 인라인. 메모는 브라우저 localStorage 에 자동 저장.

사용법
------
    python build_review.py                 # 이 폴더의 review.json 기준
    python build_review.py path/to/project # 다른 프로젝트 폴더(review.json 이 있는 곳)
    python build_review.py --watch         # 원고가 바뀔 때마다 자동 재빌드
    python build_review.py --open          # 빌드 후 브라우저로 열기
"""
import argparse
import base64
import difflib
import hashlib
import json
import re
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERSION = "1.0"

DEFAULT_CFG = {
    "id": None,
    "title": None,
    "subtitle": "",
    "authors": "",
    "sources": [],
    "output": "review.html",
    "bridge_port": 8788,
}


# ============================================================ config
def die(msg):
    print(f"[error] {msg}", file=sys.stderr)
    sys.exit(1)


def load_config(project):
    p = project / "review.json"
    if not p.is_file():
        die(f"{p} 가 없습니다. review.json 에 sources(원고 파일 경로)를 적어 주세요.")
    try:
        user = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        die(f"review.json 을 읽을 수 없습니다: {e}")
    cfg = dict(DEFAULT_CFG)
    cfg.update({k: v for k, v in user.items() if not k.startswith("_")})
    if isinstance(cfg["sources"], str):
        cfg["sources"] = [cfg["sources"]]
    if not cfg["sources"]:
        die("review.json 의 sources 가 비어 있습니다.")
    if not cfg["title"]:
        cfg["title"] = Path(cfg["sources"][0]).stem
    return cfg


def doc_id(cfg):
    """localStorage 네임스페이스. 폴더를 옮겨도 유지되도록 경로가 아니라 id/제목+원고명에서 만든다."""
    if cfg.get("id"):
        return re.sub(r"[^A-Za-z0-9_.-]", "_", str(cfg["id"]))[:48]
    seed = cfg["title"] + "|" + "|".join(cfg["sources"])
    return "doc-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8]


def kind_of(path):
    s = Path(path).suffix.lower()
    if s in (".md", ".markdown", ".mdx", ".qmd", ".rmd"):
        return "md"
    if s in (".tex", ".ltx"):
        return "tex"
    return "txt"


def split_lines(text):
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


# ============================================================ parsers
# 각 파서는 lines(list[str]) → [{t, s, e, lv?, title?, env?}] (s,e = 0-based, 양끝 포함)

FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
MD_HEAD_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
MD_LIST_RE = re.compile(r"^\s*([-*+]|\d+[.)])\s+")


def parse_md(lines):
    out, n, i = [], len(lines), 0
    if n and lines[0].strip() == "---":
        for k in range(1, n):
            if lines[k].strip() in ("---", "..."):
                out.append(dict(t="meta", s=0, e=k))
                i = k + 1
                break
    while i < n:
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        m = FENCE_RE.match(lines[i])
        if m:
            fence = m.group(1)
            close = re.compile(r"^\s{0,3}" + re.escape(fence[0]) + "{%d,}\\s*$" % len(fence))
            j = i + 1
            while j < n and not close.match(lines[j]):
                j += 1
            j = min(j, n - 1)
            out.append(dict(t="code", s=i, e=j))
            i = j + 1
            continue
        if s.startswith("$$"):
            j = i
            if not (len(s) >= 4 and s.endswith("$$")):
                j = i + 1
                while j < n and not lines[j].strip().endswith("$$"):
                    j += 1
                j = min(j, n - 1)
            out.append(dict(t="math", s=i, e=j))
            i = j + 1
            continue
        hm = MD_HEAD_RE.match(lines[i])
        if hm:
            out.append(dict(t="heading", s=i, e=i, lv=len(hm.group(1)), title=hm.group(2)))
            i += 1
            continue
        if s.startswith("<!--"):
            j = i
            while j < n and "-->" not in lines[j]:
                j += 1
            j = min(j, n - 1)
            out.append(dict(t="comment", s=i, e=j))
            i = j + 1
            continue
        if s.startswith("|"):
            j = i
            while j + 1 < n and lines[j + 1].strip().startswith("|"):
                j += 1
            out.append(dict(t="table", s=i, e=j))
            i = j + 1
            continue
        j = i
        while j + 1 < n:
            nx = lines[j + 1]
            if not nx.strip() or FENCE_RE.match(nx) or MD_HEAD_RE.match(nx) or nx.strip().startswith("$$"):
                break
            j += 1
        t = "list" if MD_LIST_RE.match(lines[i]) else ("quote" if s.startswith(">") else "para")
        out.append(dict(t=t, s=i, e=j))
        i = j + 1
    return out


TEX_SEC_RE = re.compile(
    r"^\s*\\(part|chapter|section|subsection|subsubsection|paragraph|subparagraph)\*?\s*(?:\[[^\]]*\])?\s*\{"
)
TEX_LV = {"part": 1, "chapter": 1, "section": 1, "subsection": 2, "subsubsection": 3, "paragraph": 4, "subparagraph": 5}
BEGIN_RE = re.compile(r"\\begin\s*\{([^}]+)\}")
END_RE = re.compile(r"\\end\s*\{([^}]+)\}")
MATH_ENVS = {
    "equation", "equation*", "align", "align*", "gather", "gather*", "multline", "multline*",
    "eqnarray", "eqnarray*", "displaymath", "math", "flalign", "flalign*",
}
FLOAT_ENVS = {"figure", "figure*", "table", "table*", "algorithm", "algorithm*", "wrapfigure", "wraptable", "subfigure"}


def tex_code(line):
    """% 주석을 뗀 코드 부분 (\\% 는 보존)."""
    return re.sub(r"(?<!\\)%.*", "", line)


def brace_arg(text, start):
    """text[start] == '{' 인 인자의 내용."""
    depth = 0
    for k in range(start, len(text)):
        c = text[k]
        if c == "{" and (k == 0 or text[k - 1] != "\\"):
            depth += 1
        elif c == "}" and text[k - 1] != "\\":
            depth -= 1
            if depth == 0:
                return text[start + 1:k]
    return text[start + 1:]


def parse_tex(lines):
    out, n = [], len(lines)
    bd = next((k for k, l in enumerate(lines) if "\\begin{document}" in tex_code(l)), None)
    ed = next((k for k, l in enumerate(lines) if "\\end{document}" in tex_code(l)), None)
    i, stop = 0, n
    if bd is not None:
        out.append(dict(t="meta", s=0, e=bd))
        i = bd + 1
    if ed is not None and ed >= i:
        stop = ed
    while i < stop:
        raw = lines[i]
        if not raw.strip():
            i += 1
            continue
        code = tex_code(raw).strip()
        if not code:  # 주석 전용 줄 묶음
            j = i
            while j + 1 < stop and lines[j + 1].strip() and not tex_code(lines[j + 1]).strip():
                j += 1
            out.append(dict(t="comment", s=i, e=j))
            i = j + 1
            continue
        m = TEX_SEC_RE.match(raw)
        if m:
            # 제목 중괄호가 줄을 넘기면 균형이 맞을 때까지 확장
            j, joined = i, tex_code(raw)
            while joined.count("{") > joined.count("}") and j + 1 < stop:
                j += 1
                joined += "\n" + tex_code(lines[j])
            title = brace_arg(joined, joined.find("{", m.end() - 1)).strip()
            out.append(dict(t="heading", s=i, e=j, lv=TEX_LV[m.group(1)], title=re.sub(r"\s+", " ", title)))
            i = j + 1
            continue
        if code.startswith("\\begin"):
            bm = BEGIN_RE.match(code)
            env = bm.group(1).strip() if bm else ""
            depth, j = 0, i
            while j < stop:
                c = tex_code(lines[j])
                depth += len(BEGIN_RE.findall(c)) - len(END_RE.findall(c))
                if depth <= 0:
                    break
                j += 1
            j = min(j, stop - 1)
            t = "math" if env in MATH_ENVS else "float" if env in FLOAT_ENVS else "abstract" if env == "abstract" else "env"
            out.append(dict(t=t, s=i, e=j, env=env))
            i = j + 1
            continue
        if code.startswith("\\["):
            j = i
            while j < stop and "\\]" not in tex_code(lines[j]):
                j += 1
            j = min(j, stop - 1)
            out.append(dict(t="math", s=i, e=j))
            i = j + 1
            continue
        j = i
        while j + 1 < stop:
            nx = lines[j + 1]
            nc = tex_code(nx).strip()
            if not nx.strip() or TEX_SEC_RE.match(nx) or nc.startswith("\\begin") or nc.startswith("\\["):
                break
            j += 1
        out.append(dict(t="para", s=i, e=j))
        i = j + 1
    if ed is not None:
        out.append(dict(t="meta", s=ed, e=n - 1))
    return out


def parse_txt(lines):
    out, n, i = [], len(lines), 0
    while i < n:
        if not lines[i].strip():
            i += 1
            continue
        j = i
        while j + 1 < n and lines[j + 1].strip():
            j += 1
        out.append(dict(t="para", s=i, e=j))
        i = j + 1
    return out


PARSERS = {"md": parse_md, "tex": parse_tex, "txt": parse_txt}


def sha10(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def make_blocks(text, kind, rel):
    """원고 텍스트 → 블록 리스트. s/e 는 1-based 줄 번호."""
    lines = split_lines(text)
    heads, res = {}, []
    spans = PARSERS[kind](lines)
    # Markdown 의 유일한 H1 은 문서 제목 → 섹션 경로에서 뺀다 (모든 블록 앞에 반복되지 않게)
    skip_lv = 1 if kind == "md" and sum(1 for sp in spans if sp["t"] == "heading" and sp.get("lv") == 1) == 1 else None
    for sp in spans:
        body = "\n".join(l.rstrip() for l in lines[sp["s"]:sp["e"] + 1]).strip("\n")
        if not body.strip():
            continue
        if sp["t"] == "heading":
            lv = sp.get("lv", 1)
            heads = {k: v for k, v in heads.items() if k < lv}
            heads[lv] = sp.get("title", "")
        res.append(dict(
            file=rel, s=sp["s"] + 1, e=sp["e"] + 1, t=sp["t"], lv=sp.get("lv"), env=sp.get("env"),
            sec=" › ".join(heads[k] for k in sorted(heads) if heads[k] and k != skip_lv),
            text=body, h=sha10(body),
        ))
    return res


# ============================================================ sources & versions
def load_sources(project, cfg):
    files = []
    for rel in cfg["sources"]:
        p = (project / rel).resolve()
        if not p.is_file():
            die(f"원고 파일을 찾을 수 없습니다: {rel} (기준 폴더 {project})")
        raw = p.read_bytes()
        text = raw.decode("utf-8-sig", errors="replace")
        files.append(dict(
            rel=rel, path=p, text=text, kind=kind_of(p),
            sha1=hashlib.sha1(raw).hexdigest()[:10], lines=len(split_lines(text)),
        ))
    return files


def _vnum(d):
    return int(d.name[1:])


def list_versions(project):
    vroot = project / "versions"
    if not vroot.is_dir():
        return []
    return sorted((d for d in vroot.iterdir() if d.is_dir() and re.fullmatch(r"v\d{3,}", d.name)), key=_vnum)


def snapshot(project, files):
    """원고 내용이 직전 스냅샷과 다르면 versions/vNNN/ 을 만든다. 현재 버전 번호를 반환."""
    combo = hashlib.sha1("\n".join(f["rel"] + "\0" + f["text"] for f in files).encode("utf-8")).hexdigest()
    vers = list_versions(project)
    if vers:
        try:
            meta = json.loads((vers[-1] / "meta.json").read_text(encoding="utf-8"))
            if meta.get("hash") == combo:
                return _vnum(vers[-1]), False
        except Exception:
            pass
    num = _vnum(vers[-1]) + 1 if vers else 1
    vdir = project / "versions" / f"v{num:03d}"
    (vdir / "files").mkdir(parents=True, exist_ok=True)
    entries = []
    for idx, f in enumerate(files):
        snap = f"{idx:02d}__{Path(f['rel']).name}"
        (vdir / "files" / snap).write_text(f["text"], encoding="utf-8")
        entries.append(dict(rel=f["rel"], snap=snap, kind=f["kind"], sha1=f["sha1"]))
    meta = dict(version=num, hash=combo, created=datetime.now().isoformat(timespec="seconds"), files=entries)
    (vdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return num, True


def load_snapshot_blocks(project, num):
    """버전 num 의 블록을 {rel: [blocks]} 로. 없으면 None."""
    vdir = project / "versions" / f"v{num:03d}"
    try:
        meta = json.loads((vdir / "meta.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    out = {}
    for e in meta.get("files", []):
        p = vdir / "files" / e["snap"]
        if p.is_file():
            out[e["rel"]] = make_blocks(p.read_text(encoding="utf-8"), e.get("kind") or kind_of(e["rel"]), e["rel"])
    return out


# ============================================================ diff
TOK_RE = re.compile(r"\s+|\w+|[^\w\s]", re.U)


def similarity(a, b):
    ta, tb = TOK_RE.findall(a), TOK_RE.findall(b)
    sm = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    if sm.real_quick_ratio() < 0.3 or sm.quick_ratio() < 0.3:
        return 0.0
    return sm.ratio()


def align(prev, curr):
    """같은 파일의 이전/현재 블록 정렬.
    반환: status[j] ('same'|'mod'|'new'), pair[j] = 이전 블록 인덱스|None, ghosts = [(j, 이전 블록)]
    (ghost = 삭제된 이전 블록, 현재 j 번째 블록 앞에 표시)"""
    a = [p["text"] for p in prev]
    b = [c["text"] for c in curr]
    status, pair, ghosts = ["same"] * len(curr), [None] * len(curr), []
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for d in range(i2 - i1):
                pair[j1 + d] = i1 + d
        elif tag == "insert":
            for j in range(j1, j2):
                status[j] = "new"
        elif tag == "delete":
            ghosts += [(j1, prev[k]) for k in range(i1, i2)]
        else:
            k0 = i1
            for j in range(j1, j2):
                best, best_r = None, 0.0
                for k in range(k0, i2):
                    r = similarity(a[k], b[j])
                    if r > best_r:
                        best, best_r = k, r
                if best is not None and best_r >= 0.3:
                    ghosts += [(j, prev[k]) for k in range(k0, best)]
                    pair[j], status[j], k0 = best, "mod", best + 1
                else:
                    status[j] = "new"
            ghosts += [(j2, prev[k]) for k in range(k0, i2)]
    return status, pair, ghosts


def word_diff(old, new):
    """단어 단위 diff → [[op, text], ...]  op: '=' | '-' | '+'.
    변경 사이에 낀 공백만의 '같음' 구간은 변경에 흡수해 조각나지 않게 한다."""
    a, b = TOK_RE.findall(old), TOK_RE.findall(new)
    ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    merged, i = [], 0
    while i < len(ops):
        tag, i1, i2, j1, j2 = ops[i]
        if tag != "equal":
            while (i + 2 < len(ops) and ops[i + 1][0] == "equal"
                   and not "".join(a[ops[i + 1][1]:ops[i + 1][2]]).strip() and ops[i + 2][0] != "equal"):
                i2, j2 = ops[i + 2][2], ops[i + 2][4]
                i += 2
        merged.append((tag, i1, i2, j1, j2))
        i += 1
    segs = []

    def push(op, txt):
        if not txt:
            return
        if segs and segs[-1][0] == op:
            segs[-1][1] += txt
        else:
            segs.append([op, txt])

    for tag, i1, i2, j1, j2 in merged:
        if tag == "equal":
            push("=", "".join(b[j1:j2]))
        else:
            push("-", "".join(a[i1:i2]))
            push("+", "".join(b[j1:j2]))
    return segs


# ============================================================ revisions (agent results)
META_RE = re.compile(r"<!--\s*(cid:.*?)-->", re.S)


def parse_frontmatter(text):
    fm = {}
    m = re.match(r"^---\n(.*?)\n---", text.replace("\r\n", "\n"), re.S)
    if m:
        for line in m.group(1).split("\n"):
            mm = re.match(r"^([A-Za-z_]+):\s*(.*?)\s*(#.*)?$", line)
            if mm and mm.group(2):
                fm[mm.group(1)] = mm.group(2).strip('"')
    return fm


def _section(chunk, label):
    m = re.search(r"\*\*" + re.escape(label) + r"\*\*[^\n]*\n(.*?)(?=\n\*\*[^*\n]+\*\*[^\n]*\n|\n<details>|\Z)", chunk, re.S)
    if not m:
        return ""
    body = m.group(1).strip("\n")
    if all(l.startswith(">") or not l.strip() for l in body.split("\n")):
        body = "\n".join(re.sub(r"^> ?", "", l) for l in body.split("\n"))
    return body.strip()


def parse_rev_md(text):
    """review.html 이 내보낸 첨삭 요청 .md → (frontmatter, {cid: item})"""
    fm = parse_frontmatter(text)
    items = {}
    for chunk in re.split(r"(?m)^### ", text.replace("\r\n", "\n"))[1:]:
        m = META_RE.search(chunk)
        if not m:
            continue
        meta = {}
        for kv in m.group(1).split("|"):
            if ":" in kv:
                k, v = kv.split(":", 1)
                meta[k.strip()] = v.strip()
        cid = meta.get("cid")
        if not cid:
            continue
        items[cid] = dict(
            cid=cid, block=meta.get("block", ""), hash=meta.get("hash", ""), file=meta.get("file", ""),
            lines=meta.get("lines", ""), type=meta.get("type", ""),
            quote=_section(chunk, "대상 구절"), text=_section(chunk, "요청"), sug=_section(chunk, "제안 문구"),
        )
    return fm, items


def load_results(project, blocks, snap_cache):
    """revisions/*.applied.json → (applied{cid:{status,note,rev}}, latest{...}|None, res_by_block{bid:[...]})"""
    revdir = project / "revisions"
    applied, latest, latest_key = {}, None, None
    if not revdir.is_dir():
        return applied, None, {}
    for jf in sorted(revdir.glob("*.applied.json")):
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[warn] {jf.name} 를 읽지 못했습니다: {e}")
            continue
        rev = data.get("rev") or jf.name[: -len(".applied.json")]
        for it in data.get("items", []):
            if it.get("cid"):
                applied[it["cid"]] = dict(status=it.get("status", ""), note=it.get("note", ""), rev=rev)
        key = (str(data.get("applied_at", "")), jf.stat().st_mtime)
        if latest_key is None or key > latest_key:
            latest_key, latest = key, dict(data, rev=rev)
    if not latest:
        return applied, None, {}

    rev = latest["rev"]
    md_path = revdir / f"{rev}.md"
    fm, req = ({}, {})
    if md_path.is_file():
        fm, req = parse_rev_md(md_path.read_text(encoding="utf-8"))
    base = int(fm["base_version"]) if str(fm.get("base_version", "")).isdigit() else None

    # 이전 블록 해시 → 현재 블록 id (base 스냅샷과 현재를 정렬해서 추적)
    cur_by_file = {}
    for b in blocks:
        cur_by_file.setdefault(b["file"], []).append(b)
    hash_to_cur = {}
    if base is not None:
        if base not in snap_cache:
            snap_cache[base] = load_snapshot_blocks(project, base)
        snap = snap_cache[base] or {}
        for rel, prev in snap.items():
            curr = cur_by_file.get(rel, [])
            _, pair, _ = align(prev, curr)
            for j, k in enumerate(pair):
                if k is not None:
                    hash_to_cur.setdefault(prev[k]["h"], curr[j]["id"])
    cur_hash = {}
    for b in blocks:
        cur_hash.setdefault(b["h"], b["id"])

    items, res_by_block = [], {}
    for it in latest.get("items", []):
        cid = it.get("cid", "")
        r = req.get(cid, {})
        target = None
        if r.get("hash") and r.get("block") not in ("ALL", "ORPHAN"):
            target = hash_to_cur.get(r["hash"]) or cur_hash.get(r["hash"])
        row = dict(
            cid=cid, status=it.get("status", ""), note=it.get("note", ""), rev=rev,
            type=r.get("type", ""), quote=r.get("quote", ""), text=r.get("text", ""),
            block=r.get("block", ""), loc=(f"{r.get('file', '')}:{r.get('lines', '')}" if r.get("file") else ""),
            target=target,
        )
        items.append(row)
        if target:
            res_by_block.setdefault(target, []).append(row)
    out = dict(rev=rev, applied_at=latest.get("applied_at", ""), summary=latest.get("summary", ""),
               base_version=base, items=items)
    return applied, out, res_by_block


# ============================================================ build
def build(project, open_after=False, quiet=False):
    cfg = load_config(project)
    files = load_sources(project, cfg)
    version, created = snapshot(project, files)

    blocks = []
    for fi, f in enumerate(files):
        for b in make_blocks(f["text"], f["kind"], f["rel"]):
            b["fi"] = fi
            b["k"] = f["kind"]
            blocks.append(b)
    seen = {}
    for i, b in enumerate(blocks):
        b["id"] = f"B{i + 1:03d}"
        n = seen.get(b["h"], 0)
        seen[b["h"]] = n + 1
        b["key"] = b["h"] if n == 0 else f"{b['h']}.{n}"

    # 직전 버전과 diff
    snap_cache = {}
    vers = [_vnum(d) for d in list_versions(project)]
    prev_ver = max((v for v in vers if v < version), default=None)
    ghosts, n_mod, n_new = [], 0, 0
    if prev_ver is not None:
        snap_cache[prev_ver] = load_snapshot_blocks(project, prev_ver)
        prev_all = snap_cache[prev_ver] or {}
        offset = 0
        for f in files:
            curr = [b for b in blocks if b["file"] == f["rel"]]
            prev = prev_all.get(f["rel"])
            if prev is None:
                for b in curr:
                    b["ch"] = "new"
                n_new += len(curr)
            else:
                status, pair, gh = align(prev, curr)
                for j, b in enumerate(curr):
                    b["ch"] = status[j]
                    if status[j] == "mod":
                        b["diff"] = word_diff(prev[pair[j]]["text"], b["text"])
                        b["ph"] = prev[pair[j]]["h"]  # 이전 버전 블록 해시 → 코멘트 재연결용
                        n_mod += 1
                    elif status[j] == "new":
                        n_new += 1
                for j, pb in gh:
                    ghosts.append(dict(before=offset + j, file=f["rel"], text=pb["text"], t=pb["t"], k=f["kind"], lv=pb.get("lv")))
            offset += len(curr)

    applied, latest, res_by_block = load_results(project, blocks, snap_cache)

    logo = ""
    for cand in (project / "assets" / "logo.png", HERE / "assets" / "logo.png"):
        if cand.is_file():
            logo = "data:image/png;base64," + base64.b64encode(cand.read_bytes()).decode("ascii")
            break

    data = dict(
        doc=dict(
            id=doc_id(cfg), title=cfg["title"], subtitle=cfg.get("subtitle", ""), authors=cfg.get("authors", ""),
            version=version, prev_version=prev_ver, built=datetime.now().strftime("%Y-%m-%d %H:%M"),
            bridge_port=int(cfg.get("bridge_port") or 8788), tool=VERSION,
            sources=[dict(path=f["rel"], sha1=f["sha1"], lines=f["lines"], kind=f["kind"]) for f in files],
            changes=dict(mod=n_mod, new=n_new, deleted=len(ghosts)),
        ),
        blocks=[{k: v for k, v in b.items() if v is not None} for b in blocks],
        ghosts=ghosts, applied=applied, latest=latest, res_by_block=res_by_block,
    )
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    title_esc = (cfg["title"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    html_out = (TEMPLATE.replace("__TITLE__", title_esc)
                .replace("__LOGO__", logo)
                .replace("/*__DATA__*/", payload))
    out = project / cfg.get("output", "review.html")
    out.write_text(html_out, encoding="utf-8")
    if not quiet:
        ch = f" · 변경 {n_mod} 수정 / {n_new} 추가 / {len(ghosts)} 삭제 (vs v{prev_ver:03d})" if prev_ver else ""
        print(f"OK {out.name} · v{version:03d}{' (new snapshot)' if created else ''} · 블록 {len(blocks)}{ch}"
              + (f" · 반영 결과 {latest['rev']}" if latest else ""))
    if open_after:
        webbrowser.open(out.resolve().as_uri())
    return out


def watch(project):
    cfg = load_config(project)
    paths = [(project / s).resolve() for s in cfg["sources"]] + [project / "review.json"]
    revdir = project / "revisions"

    def stamp():
        st = [p.stat().st_mtime if p.exists() else 0 for p in paths]
        if revdir.is_dir():
            st += sorted(p.stat().st_mtime for p in revdir.glob("*.applied.json"))
        return st

    last = None
    print("watch: 원고·review.json·반영 결과가 바뀌면 다시 빌드합니다. Ctrl+C 로 종료.")
    try:
        while True:
            s = stamp()
            if s != last:
                last = s
                try:
                    build(project)
                except SystemExit:
                    pass
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n종료합니다.")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="원고 → 2단 첨삭 HTML")
    ap.add_argument("project", nargs="?", default=str(HERE), help="review.json 이 있는 폴더 (기본: 이 스크립트 폴더)")
    ap.add_argument("--watch", action="store_true", help="원고가 바뀔 때마다 자동 재빌드")
    ap.add_argument("--open", action="store_true", help="빌드 후 브라우저로 열기")
    args = ap.parse_args()
    project = Path(args.project).resolve()
    if args.watch:
        watch(project)
    else:
        build(project, open_after=args.open)


# ============================================================ HTML template
TEMPLATE = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ · 첨삭</title>
<style>
:root{
  --bg:#fafbfc; --paper:#ffffff; --ink:#32363f; --ink-soft:#5e6470; --muted:#9aa0ac; --line:#ecedf2;
  --accent:#5e6488; --accent-mid:#9398b9; --accent-soft:#f1f2f8; --accent-pale:#d9dce9;
  --azure:#7191ab; --azure-soft:#eaf1f7; --azure-pale:#d5e2ec;
  --rose:#ab8290; --rose-soft:#f6edf0; --mint:#74ad97; --mint-soft:#edf4f1;
  --amber:#ab8a55; --amber-soft:#f6f1e6;
  --radius:14px; --shadow:0 1px 2px rgba(48,53,66,.03),0 1px 6px rgba(48,53,66,.03);
  --ins:#e2f0e9; --ins-ink:#3f6a58; --del:#f6e5ea; --del-ink:#8a5363;
  --code-bg:#f1f2f7; --tb:120px;
}
*{box-sizing:border-box}
html{scroll-padding-top:calc(var(--tb) + 48px)}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.66 -apple-system,BlinkMacSystemFont,"Segoe UI","Pretendard Variable","Noto Sans KR","Apple SD Gothic Neo","Malgun Gothic",sans-serif;
  word-break:keep-all}
button{font:inherit;color:inherit}
a{color:var(--azure)}

/* ---------- topbar (v4 대시보드) ---------- */
.topbar{position:sticky;top:0;z-index:60;background:var(--bg);border-bottom:1px solid var(--line);box-shadow:0 1px 6px rgba(95,106,166,.03)}
.hdr{display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:13px 30px 8px}
.brand{display:flex;align-items:center;gap:12px;margin-right:6px;min-width:0}
.brand img{height:40px;width:auto;display:block;flex-shrink:0}
.brand .bt{font-weight:800;font-size:15px;color:var(--accent);line-height:1.25}
.brand .bs{font-size:12.5px;color:var(--ink-soft);font-weight:700;margin-top:1px;line-height:1.35}
.brand .bs2{font-size:11px;color:var(--muted);font-weight:600;margin-top:2px;line-height:1.35}
.brand-meta{margin-left:auto;font-size:12px;color:var(--muted);text-align:right;line-height:1.6;flex-shrink:0}
.brand-meta code{font-size:11px;background:var(--accent-soft);padding:0 5px;border-radius:5px;color:var(--ink-soft)}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--muted);margin-right:5px;vertical-align:1px}
.dot.on{background:var(--mint)}
.toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:6px 30px 10px}
.tb-sp{flex:1}
.btn{border:1px solid var(--line);background:var(--paper);border-radius:9px;padding:5px 12px;font-size:13px;font-weight:600;color:var(--ink-soft);cursor:pointer;line-height:1.5;white-space:nowrap}
.btn:hover{border-color:var(--accent-pale);color:var(--accent)}
.btn:disabled{opacity:.45;cursor:not-allowed}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.primary:hover{background:#4f5577;color:#fff}
.btn.ghost{background:none;border-color:transparent}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:9px;overflow:hidden;background:var(--paper)}
.seg button{border:0;background:none;padding:5px 12px;font-size:13px;font-weight:600;color:var(--ink-soft);cursor:pointer}
.seg button.on{background:var(--accent-soft);color:var(--accent);font-weight:800}
.seg button:disabled{opacity:.45;cursor:not-allowed}
.chk{display:inline-flex;align-items:center;gap:5px;font-size:13px;color:var(--ink-soft);cursor:pointer;user-select:none}
.counts{font-size:12.5px;color:var(--muted);font-weight:600}
.counts b{color:var(--accent)}
.hint{font-size:12.5px;color:var(--muted);padding:0 30px 10px}

/* ---------- panels ---------- */
.wrap{max-width:1560px;margin:0 auto;padding:18px 30px 120px}
.panel{background:var(--paper);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);margin-bottom:14px;padding:14px 18px}
.p-hd{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.p-hd b{color:var(--accent);font-size:14px}
.p-hd .sub{font-size:12px;color:var(--muted)}
.p-hd .btn{margin-left:auto}
.p-body{margin-top:10px}
.p-sum{margin:8px 0 0;font-size:14px;color:var(--ink-soft)}
.res-list{list-style:none;margin:10px 0 0;padding:0;display:grid;gap:8px}
.res-list li{border:1px solid var(--line);border-radius:10px;padding:8px 12px;font-size:13.5px;background:#fcfcfe}
.res-list .loc{font-size:11.5px;color:var(--muted);margin-left:6px}
.res-list .req{display:block;margin-top:3px;color:var(--ink-soft);white-space:pre-wrap}
.res-list .note{display:block;margin-top:4px;color:var(--ink);white-space:pre-wrap}
.res-list .jump{font-size:12px;margin-left:6px;cursor:pointer;color:var(--azure);text-decoration:underline}
.help ol{margin:8px 0 0;padding-left:20px;font-size:13.5px;color:var(--ink-soft)}
.help code{background:var(--code-bg);padding:1px 5px;border-radius:5px;font-size:12.5px}
.orphan-ctx{font-size:12px;color:var(--muted);margin-top:4px}
.orphan-ctx summary{cursor:pointer}
.orphan-ctx pre{white-space:pre-wrap;background:var(--code-bg);padding:8px 10px;border-radius:8px;font-size:12px;margin:6px 0 0}

/* ---------- sheet (2단) ---------- */
.sheet{background:var(--paper);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow)}
.colhead{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(330px,1fr);position:sticky;top:var(--tb);z-index:20;background:var(--paper);border-bottom:1px solid var(--line);border-radius:var(--radius) var(--radius) 0 0}
.colhead div{padding:9px 22px;font-size:12px;font-weight:800;color:var(--muted);letter-spacing:.02em}
.colhead .ch-r{border-left:1px solid var(--line);background:#fcfcfe;border-top-right-radius:var(--radius)}
.row{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(330px,1fr);border-bottom:1px solid var(--line)}
.row:last-child{border-bottom:0}
.row .L{display:grid;grid-template-columns:62px minmax(0,1fr);gap:12px;padding:14px 22px 14px 10px;min-width:0}
.row .R{border-left:1px solid var(--line);background:#fcfcfe;padding:10px 16px;display:flex;flex-direction:column;gap:8px;min-width:0}
.row:hover .L{background:#fcfcfd}
.row.flash .L{animation:flash 1.4s ease}
@keyframes flash{0%{background:var(--amber-soft)}100%{background:transparent}}
.gut{font-size:10.5px;color:var(--muted);text-align:right;padding-top:4px;line-height:1.45;font-variant-numeric:tabular-nums;user-select:none}
.gut b{display:block;font-weight:700;color:var(--accent-mid)}
.gut .chg{display:inline-block;margin-top:4px;font-style:normal;font-weight:700;font-size:10px;padding:0 5px;border-radius:5px}
.row.ch-mod .gut .chg{background:var(--amber-soft);color:var(--amber)}
.row.ch-new .gut .chg{background:var(--mint-soft);color:var(--mint)}
.src{white-space:pre-wrap;overflow-wrap:anywhere;min-width:0}
.row.t-heading .src{font-weight:800;color:var(--accent)}
.row.t-heading.lv1 .src{font-size:20px}
.row.t-heading.lv2 .src{font-size:17.5px}
.row.t-heading.lv3 .src{font-size:16px}
.row.t-code .src,.row.t-math .src,.row.t-table .src,.row.t-float .src,.row.t-env .src,.row.t-meta .src{
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"D2Coding",monospace;font-size:13px;line-height:1.6;background:var(--code-bg);padding:10px 12px;border-radius:10px}
.row.t-comment .src{color:var(--muted);font-style:italic}
.row.t-abstract .src{border-left:3px solid var(--accent-pale);padding-left:12px}
.row.t-meta details summary{cursor:pointer;font-size:12.5px;color:var(--muted);font-weight:600}
.row.t-meta details .src{margin-top:8px}
.filediv{padding:8px 22px;background:var(--accent-soft);font-size:12px;font-weight:800;color:var(--accent);border-bottom:1px solid var(--line)}
.row.ghost{display:none}
body.show-diff .row.ghost{display:grid}
.row.ghost .src{background:var(--del);color:var(--del-ink);text-decoration:line-through;border-radius:8px;padding:8px 10px;font-family:inherit;font-size:14px}
.row.ghost .R{font-size:12px;color:var(--del-ink);justify-content:center}
body.only-c .row:not(.has-c):not(.t-heading):not(.ghost){display:none}

/* 토큰 색 (원문 문자는 전부 보존, 색만 입힘) */
.tk-mk{color:#b9bdc8}
.tk-cmd{color:var(--azure)}
.tk-ref{color:var(--azure);background:var(--azure-soft);border-radius:4px}
.tk-math{color:#6f6892;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.92em}
.tk-code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.92em;background:var(--code-bg);border-radius:4px}
.tk-cmt{color:var(--muted);font-style:italic}
mark.q{background:var(--amber-soft);color:inherit;border-bottom:2px solid #dcc9a1;border-radius:2px;cursor:pointer}
mark.q.on{background:#eedfbd}
ins{background:var(--ins);color:var(--ins-ink);text-decoration:none;border-radius:3px}
del{background:var(--del);color:var(--del-ink);border-radius:3px}
.row.ch-new.diffon .src{box-shadow:inset 3px 0 0 var(--mint);padding-left:10px;background:linear-gradient(90deg,var(--mint-soft),transparent 60%)}

/* ---------- 오른쪽 칸: 코멘트 카드 ---------- */
.add{align-self:flex-start;border:1px dashed var(--accent-pale);background:none;border-radius:8px;padding:3px 10px;font-size:12.5px;color:var(--accent-mid);cursor:pointer;opacity:.4;transition:opacity .12s}
.row:hover .add,.add:focus{opacity:1}
.cm{background:var(--paper);border:1px solid var(--line);border-left:3px solid var(--accent-pale);border-radius:10px;padding:8px 10px;display:flex;flex-direction:column;gap:6px}
.cm.on{border-color:#dcc9a1;box-shadow:0 0 0 3px var(--amber-soft)}
.cm[data-type=rewrite]{border-left-color:var(--accent-mid)}
.cm[data-type=delete]{border-left-color:var(--rose)}
.cm[data-type=insert]{border-left-color:var(--mint)}
.cm[data-type=style]{border-left-color:var(--azure)}
.cm[data-type=question]{border-left-color:var(--amber)}
.cm-hd{display:flex;align-items:center;gap:6px}
.cm-type{border:1px solid var(--line);background:var(--accent-soft);color:var(--accent);font-weight:700;font-size:12px;border-radius:7px;padding:2px 4px;cursor:pointer}
.cm-state{font-size:11px;font-weight:700;color:var(--muted);padding:1px 6px;border-radius:6px;background:var(--bg);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:190px}
.cm-state.sent{color:var(--azure);background:var(--azure-soft)}
.cm-state.returned{color:var(--rose);background:var(--rose-soft)}
.cm-x{margin-left:auto;border:0;background:none;color:var(--muted);cursor:pointer;font-size:16px;line-height:1;padding:0 2px}
.cm-x:hover{color:var(--rose)}
.cm-q{margin:0;font-size:12.5px;color:var(--ink-soft);background:var(--amber-soft);border-radius:6px;padding:3px 8px;white-space:pre-wrap;overflow-wrap:anywhere;cursor:pointer;max-height:6.5em;overflow:auto}
.cm textarea{width:100%;border:1px solid var(--line);border-radius:8px;padding:6px 8px;font:inherit;font-size:13.5px;line-height:1.55;resize:vertical;min-height:52px;background:#fff;color:var(--ink)}
.cm textarea:focus{outline:none;border-color:var(--accent-pale);box-shadow:0 0 0 3px var(--accent-soft)}
.cm textarea.cm-s{min-height:40px;background:var(--mint-soft);border-color:#dbe9e2}
.cm-sug-btn{align-self:flex-start;border:0;background:none;color:var(--mint);font-size:12px;font-weight:700;cursor:pointer;padding:0}
.cm-note{font-size:12.5px;background:var(--rose-soft);color:#7a5560;border-radius:7px;padding:5px 8px;white-space:pre-wrap}
.cm-note b{font-weight:800}
.rs{border:1px solid #dbe9e2;background:var(--mint-soft);border-radius:10px;padding:7px 10px;font-size:12.5px}
.rs.st-skipped,.rs.st-partial{background:var(--rose-soft);border-color:#ecdbe0}
.rs.st-answered{background:var(--azure-soft);border-color:var(--azure-pale)}
.rs-hd{display:flex;align-items:center;gap:6px}
.rs-hd .rs-t{color:var(--ink-soft);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;flex:1}
.rs-x{border:0;background:none;color:var(--muted);cursor:pointer;font-size:11.5px;text-decoration:underline;flex-shrink:0}
.rs-req{color:var(--ink-soft);margin-top:3px;white-space:pre-wrap}
.rs-note{color:var(--ink);margin-top:3px;white-space:pre-wrap}
.st{display:inline-block;font-size:11px;font-weight:800;border-radius:6px;padding:0 6px;flex-shrink:0}
.st-applied .st,.st.st-applied{background:#fff;color:var(--mint)}
.st-partial .st,.st.st-partial{background:#fff;color:var(--amber)}
.st-skipped .st,.st.st-skipped{background:#fff;color:var(--rose)}
.st-answered .st,.st.st-answered{background:#fff;color:var(--azure)}
.res-list .st{border:1px solid var(--line)}

/* ---------- 선택 → 코멘트 버튼 ---------- */
#selfab{position:absolute;z-index:80;display:none;background:var(--accent);color:#fff;border:0;border-radius:9px;padding:5px 11px;font-size:12.5px;font-weight:700;cursor:pointer;box-shadow:0 4px 14px rgba(48,53,66,.18)}
#selfab.show{display:block}

/* ---------- 내보내기 모달 ---------- */
.ov{position:fixed;inset:0;background:rgba(48,53,66,.28);z-index:100;display:none;align-items:center;justify-content:center;padding:20px}
.ov.show{display:flex}
.modal{background:var(--paper);border-radius:var(--radius);box-shadow:0 12px 40px rgba(48,53,66,.18);width:min(920px,100%);max-height:calc(100vh - 40px);display:flex;flex-direction:column}
.m-hd{display:flex;align-items:center;gap:10px;padding:14px 18px;border-bottom:1px solid var(--line)}
.m-hd b{color:var(--accent);font-size:15px}
.m-hd .sub{font-size:12px;color:var(--muted)}
.m-hd .cm-x{font-size:20px}
.m-body{padding:12px 18px;overflow:auto;display:flex;flex-direction:column;gap:10px}
.m-opts{display:flex;gap:16px;flex-wrap:wrap;align-items:center;font-size:13px;color:var(--ink-soft)}
.m-opts label{display:inline-flex;align-items:center;gap:5px;cursor:pointer}
.m-body textarea{width:100%;min-height:42vh;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px;line-height:1.55;border:1px solid var(--line);border-radius:10px;padding:10px 12px;background:#fcfcfe;color:var(--ink);resize:vertical}
.m-warn{font-size:12.5px;color:var(--rose)}
.m-cmd{font-size:12.5px;color:var(--ink-soft);background:var(--accent-soft);border-radius:8px;padding:7px 10px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.m-cmd code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:var(--accent);font-weight:700}
.m-ft{display:flex;gap:8px;flex-wrap:wrap;align-items:center;padding:12px 18px;border-top:1px solid var(--line)}
.m-ft .sp{flex:1}

/* ---------- 토스트 / job ---------- */
#toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:var(--ink);color:#fff;border-radius:10px;padding:8px 14px;font-size:13px;z-index:120;display:none;align-items:center;gap:10px;box-shadow:0 6px 20px rgba(48,53,66,.2)}
#toast.show{display:flex}
#toast button{background:none;border:0;color:#d9dce9;text-decoration:underline;cursor:pointer;font-size:13px}
#job{position:fixed;right:20px;bottom:20px;width:min(420px,calc(100vw - 40px));background:var(--paper);border:1px solid var(--line);border-radius:var(--radius);box-shadow:0 10px 30px rgba(48,53,66,.14);z-index:110;padding:12px 14px;display:none}
#job.show{display:block}
#job .j-hd{display:flex;align-items:center;gap:8px;font-size:13.5px;font-weight:700;color:var(--accent)}
#job .j-hd .cm-x{font-size:16px}
#job pre{margin:8px 0 0;max-height:130px;overflow:auto;background:var(--code-bg);border-radius:8px;padding:6px 8px;font-size:11px;white-space:pre-wrap;color:var(--ink-soft)}
#job .j-ft{display:flex;gap:8px;margin-top:8px}
.spin{width:12px;height:12px;border:2px solid var(--accent-pale);border-top-color:var(--accent);border-radius:50%;animation:sp 0.9s linear infinite;display:inline-block}
@keyframes sp{to{transform:rotate(360deg)}}

@media (max-width:900px){
  .hdr,.toolbar,.hint{padding-left:16px;padding-right:16px}
  .wrap{padding:14px 16px 100px}
  .brand-meta{margin-left:0;text-align:left}
  .colhead{display:none}
  .row{grid-template-columns:1fr}
  .row .L{grid-template-columns:44px minmax(0,1fr);padding:12px 12px 8px 6px}
  .row .R{border-left:0;border-top:1px dashed var(--line)}
  .add{opacity:1}
}
@media print{.topbar,.row .R,.colhead,#selfab,#job,#toast,.panel{display:none!important}.row{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="topbar" id="topbar">
  <div class="hdr">
    <div class="brand"><img id="logo" src="__LOGO__" alt="">
      <div><div class="bt" id="bt"></div><div class="bs" id="bs"></div><div class="bs2" id="bs2"></div></div>
    </div>
    <div class="brand-meta" id="meta"></div>
  </div>
  <div class="toolbar">
    <div class="seg" id="viewseg"><button data-v="clean">정리본</button><button data-v="diff">변경 표시</button></div>
    <span class="counts" id="chnav"></span>
    <label class="chk"><input type="checkbox" id="onlyc"> 코멘트 있는 블록만</label>
    <span class="tb-sp"></span>
    <span class="counts" id="counts"></span>
    <button class="btn" id="btn-global">+ 전체 지시</button>
    <button class="btn ghost" id="btn-help">사용법</button>
    <button class="btn primary" id="btn-export">.md 내보내기</button>
  </div>
  <div class="hint" id="hint"></div>
</div>

<div class="wrap">
  <section class="panel help" id="help-panel" hidden>
    <div class="p-hd"><b>사용법</b><span class="sub">한 라운드 = 코멘트 → .md 내보내기 → 에이전트 반영 → 다시 빌드</span></div>
    <ol>
      <li>왼쪽 원고에서 <b>문장·구절을 드래그</b>하면 나타나는 <b>코멘트</b> 버튼 → 그 구절에 대한 수정 요청. 블록 전체에 대한 요청은 오른쪽 <b>+ 코멘트</b>.</li>
      <li>유형(수정·삭제·추가·표현·질문·기타)을 고르고 요청을 적습니다. 바꿀 문장을 직접 정해 두었다면 <b>+ 제안 문구</b>에 적습니다. 문서 전체에 적용할 지시는 상단 <b>+ 전체 지시</b>.</li>
      <li><b>.md 내보내기</b> → <code>revisions/rev_*.md</code> 로 저장 (폴더 저장 · 다운로드 · 브릿지 중 택1). 메모는 브라우저에 자동 저장되므로 새로고침해도 남습니다.</li>
      <li>Claude Code 에서 <code>revisions/rev_….md 반영해줘</code> — 또는 브릿지(<code>python review_bridge.py</code>)를 켜 두고 <b>에이전트에 바로 반영</b>.</li>
      <li>에이전트가 원고를 고치고 HTML 을 다시 빌드합니다. 새로고침하면 <b>변경 표시</b>로 바뀐 곳이, 오른쪽 칸에 코멘트별 반영 결과가 보입니다.</li>
    </ol>
  </section>
  <section class="panel" id="res-panel" hidden></section>
  <section class="panel" id="orphan-panel" hidden></section>
  <section class="panel" id="global-panel" hidden></section>
  <div class="sheet" id="sheet">
    <div class="colhead"><div id="ch-l">원고</div><div class="ch-r">첨삭 메모</div></div>
  </div>
</div>

<button id="selfab" type="button">코멘트</button>

<div class="ov" id="ov">
  <div class="modal" role="dialog" aria-modal="true">
    <div class="m-hd"><b>첨삭 요청 내보내기</b><span class="sub" id="m-name"></span><button class="cm-x" id="m-close" title="닫기 (Esc)">×</button></div>
    <div class="m-body">
      <div class="m-opts">
        <span>수정 범위</span>
        <label><input type="radio" name="scope" value="minimal"> 최소 수정 (요청한 곳만)</label>
        <label><input type="radio" name="scope" value="free"> 자유 재작성 (블록 안 재구성 허용)</label>
        <label><input type="checkbox" id="m-incsent"> 이미 전송·보류된 코멘트도 포함</label>
      </div>
      <div class="m-warn" id="m-warn"></div>
      <textarea id="m-text" readonly spellcheck="false"></textarea>
      <div class="m-cmd" id="m-cmd"></div>
    </div>
    <div class="m-ft">
      <button class="btn" id="m-save">폴더에 저장</button>
      <button class="btn" id="m-dl">다운로드</button>
      <button class="btn" id="m-copy">복사</button>
      <button class="btn ghost" id="m-setdir" title="저장 폴더 다시 지정">폴더 변경</button>
      <span class="sp"></span>
      <button class="btn primary" id="m-apply">에이전트에 바로 반영</button>
    </div>
  </div>
</div>

<div id="job"></div>
<div id="toast"></div>

<script type="application/json" id="mrev-data">/*__DATA__*/</script>
<script>
(function(){
'use strict';
var D = JSON.parse(document.getElementById('mrev-data').textContent);
var DOC = D.doc, BLOCKS = D.blocks;
var NS = 'mrev:' + DOC.id + ':';
var BRIDGE = 'http://127.0.0.1:' + DOC.bridge_port;
var TYPES = [['rewrite','수정'],['delete','삭제'],['insert','추가'],['style','표현'],['question','질문'],['other','기타']];
var TYPE_LABEL = {}; TYPES.forEach(function(t){ TYPE_LABEL[t[0]] = t[1]; });
var STATUS_LABEL = {applied:'반영', partial:'부분 반영', skipped:'보류', answered:'답변'};

function $(id){ return document.getElementById(id); }
function esc(s){ return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function lsGet(k, d){ try { var v = localStorage.getItem(NS + k); return v == null ? d : JSON.parse(v); } catch(e){ return d; } }
function lsSet(k, v){ try { localStorage.setItem(NS + k, JSON.stringify(v)); } catch(e){ toast('브라우저 저장 실패: ' + e.message); } }
function lsDel(k){ try { localStorage.removeItem(NS + k); } catch(e){} }
function pad(n, w){ n = String(n); while (n.length < w) n = '0' + n; return n; }

var byKey = {}, byId = {};
BLOCKS.forEach(function(b, i){ b.i = i; byKey[b.key] = b; byId[b.id] = b; });

var comments = lsGet('comments', []);
var archive = lsGet('archive', []);
var ui = lsGet('ui', {});
if (!ui.scope) ui.scope = 'minimal';
var dismissed = lsGet('dismissed', {});
var hasPrev = DOC.prev_version != null;
if (ui.seenVer !== DOC.version){ ui.diff = hasPrev && (DOC.changes.mod + DOC.changes.new + DOC.changes.deleted) > 0; ui.seenVer = DOC.version; }
if (!hasPrev) ui.diff = false;

function saveComments(){ lsSet('comments', comments); }
var saveTimer = null;
function saveSoon(){ clearTimeout(saveTimer); saveTimer = setTimeout(saveComments, 250); }
window.addEventListener('beforeunload', saveComments);
document.addEventListener('visibilitychange', function(){ if (document.hidden) saveComments(); });

// ---- 에이전트 반영 결과와 대조: 반영·답변 → 보관, 부분·보류 → 카드에 에이전트 메모 ----
(function reconcile(){
  var keep = [], moved = 0;
  comments.forEach(function(c){
    var a = D.applied[c.cid];
    if (a && a.rev !== c.seenRev && (c.state !== 'sent' || a.rev === c.sentRev)){
      c.seenRev = a.rev; c.result = a;
      if (a.status === 'applied' || a.status === 'answered'){ c.archived = Date.now(); archive.push(c); moved++; return; }
      c.state = 'returned';
    }
    keep.push(c);
  });
  comments = keep;
  // 직전 버전에서 수정된 블록의 코멘트 → 대응하는 새 블록에 다시 연결 (못 찾으면 '위치를 잃은 코멘트')
  var prevMap = {}, relinked = 0;
  BLOCKS.forEach(function(b){ if (b.ph && !prevMap[b.ph]) prevMap[b.ph] = b; });
  comments.forEach(function(c){
    if (c.key === 'ALL' || byKey[c.key]) return;
    var nb = prevMap[c.h];
    if (!nb) return;
    c.key = nb.key; c.h = nb.h; c.bid = nb.id; c.file = nb.file; c.s = nb.s; c.e = nb.e; c.relinked = DOC.version;
    if (c.quote && nb.text.indexOf(c.quote) < 0) c.qoff = -1;
    relinked++;
  });
  saveComments();
  if (moved){ lsSet('archive', archive.slice(-800)); }
  if (relinked) setTimeout(function(){ toast('원고가 수정된 블록의 코멘트 ' + relinked + '건을 새 블록에 다시 연결했습니다.'); }, 300);
})();

// ---- 헤더 ----
$('bt').textContent = DOC.title;
$('bs').textContent = (DOC.subtitle ? DOC.subtitle + ' · ' : '') + '원고 첨삭 · v' + pad(DOC.version, 3) + ' · 블록 ' + BLOCKS.length + '개';
$('bs2').textContent = DOC.authors || '';
if (!$('logo').getAttribute('src')) $('logo').style.display = 'none';
$('meta').innerHTML = DOC.sources.map(function(s){ return esc(s.path) + ' <code>' + esc(s.sha1) + '</code>'; }).join('<br>')
  + '<br>빌드 ' + esc(DOC.built) + (hasPrev ? ' · 비교 기준 v' + pad(DOC.prev_version, 3) : '')
  + '<br><span id="bridge-st"><span class="dot"></span>브릿지 확인 중</span>';
$('ch-l').textContent = '원고 · v' + pad(DOC.version, 3) + (DOC.sources.length === 1 ? ' · ' + DOC.sources[0].path : '');

// ---- 원문 렌더 (문자 1:1 보존 + 마크업만 색) ----
var RX_MD = /(<!--[\s\S]*?-->)|(`[^`\n]+`)|(\$\$[^$]+\$\$|\$[^$\n]+\$)|(\*\*|__)|(\[)([^\]\n]*)(\]\([^)\n]*\))|(^[ \t]*#{1,6}[ \t])|(^[ \t]*(?:[-*+]|\d+[.)])[ \t])|(^[ \t]*>[ \t]?)/gm;
var RX_TEX = /((?:^|[^\\])%.*$)|(\\(?:cite[a-zA-Z]*|ref|eqref|autoref|cref|Cref|label|pageref|footnote|url)\*?(?:\[[^\]\n]*\])*\{[^}\n]*\})|(\$\$[^$]+\$\$|\$[^$\n]+\$|\\\([^\n]*?\\\))|(\\[a-zA-Z@]+\*?|\\[\\{}%$&_#,;!])|([{}])/gm;
function span(cls, s){ return '<span class="' + cls + '">' + esc(s) + '</span>'; }
function tokenize(text, rx, fn){
  var out = '', last = 0, m;
  rx.lastIndex = 0;
  while ((m = rx.exec(text))){
    if (m[0] === ''){ rx.lastIndex++; continue; }
    out += esc(text.slice(last, m.index)) + fn(m);
    last = m.index + m[0].length;
  }
  return out + esc(text.slice(last));
}
function mdTok(m){
  if (m[1]) return span('tk-cmt', m[1]);
  if (m[2]) return span('tk-code', m[2]);
  if (m[3]) return span('tk-math', m[3]);
  if (m[4]) return span('tk-mk', m[4]);
  if (m[5]) return span('tk-mk', m[5]) + esc(m[6]) + span('tk-mk', m[7]);
  return span('tk-mk', m[0]);
}
function texTok(m){
  if (m[1]){ var s = m[1]; if (s[0] !== '%') return esc(s[0]) + span('tk-cmt', s.slice(1)); return span('tk-cmt', s); }
  if (m[2]) return span('tk-ref', m[2]);
  if (m[3]) return span('tk-math', m[3]);
  if (m[4]) return span('tk-cmd', m[4]);
  return span('tk-mk', m[0]);
}
function srcHtml(b){
  var t = b.text;
  if (b.k === 'md'){
    if (b.t === 'code' || b.t === 'table' || b.t === 'meta' || b.t === 'math') return esc(t);
    if (b.t === 'comment') return span('tk-cmt', t);
    return tokenize(t, RX_MD, mdTok);
  }
  if (b.k === 'tex') return tokenize(t, RX_TEX, texTok);
  return esc(t);
}
function diffHtml(segs){
  return segs.map(function(s){
    if (s[0] === '+') return '<ins>' + esc(s[1]) + '</ins>';
    if (s[0] === '-') return '<del>' + esc(s[1]) + '</del>';
    return esc(s[1]);
  }).join('');
}

// ---- 구절 하이라이트 (텍스트 노드를 쪼개 mark 로 감쌈) ----
function quoteRanges(b){
  var out = [];
  comments.forEach(function(c){
    if (c.key !== b.key || !c.quote) return;
    var st = (c.qoff >= 0 && b.text.substr(c.qoff, c.quote.length) === c.quote) ? c.qoff : b.text.indexOf(c.quote);
    if (st >= 0) out.push({cid: c.cid, start: st, end: st + c.quote.length});
  });
  return out;
}
function applyMarks(el, ranges){
  if (!ranges.length) return;
  var walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT), nodes = [], pos = 0, nd;
  while ((nd = walker.nextNode())){ nodes.push({node: nd, s: pos, e: pos + nd.nodeValue.length}); pos += nd.nodeValue.length; }
  nodes.forEach(function(x){
    var pts = [x.s, x.e];
    ranges.forEach(function(r){ if (r.end > x.s && r.start < x.e){ pts.push(Math.max(r.start, x.s), Math.min(r.end, x.e)); } });
    if (pts.length === 2) return;
    pts = pts.filter(function(v, i, a){ return a.indexOf(v) === i; }).sort(function(p, q){ return p - q; });
    var frag = document.createDocumentFragment(), txt = x.node.nodeValue;
    for (var k = 0; k < pts.length - 1; k++){
      var a = pts[k], z = pts[k + 1], piece = txt.slice(a - x.s, z - x.s);
      if (!piece) continue;
      var cids = ranges.filter(function(r){ return r.start <= a && r.end >= z; }).map(function(r){ return r.cid; });
      if (cids.length){ var mk = document.createElement('mark'); mk.className = 'q'; mk.setAttribute('data-cids', cids.join(' ')); mk.textContent = piece; frag.appendChild(mk); }
      else frag.appendChild(document.createTextNode(piece));
    }
    x.node.parentNode.replaceChild(frag, x.node);
  });
}

// ---- 행 만들기 ----
var sheet = $('sheet'), rowEls = {};
function renderSrc(b){
  var el = rowEls[b.key] && rowEls[b.key].querySelector('.src');
  if (!el) return;
  var diffOn = ui.diff && b.diff;
  el.innerHTML = diffOn ? diffHtml(b.diff) : srcHtml(b);
  rowEls[b.key].classList.toggle('diffon', !!ui.diff);
  if (!diffOn) applyMarks(el, quoteRanges(b));
}
function blockRow(b){
  var row = document.createElement('div');
  row.className = 'row t-' + b.t + (b.lv ? ' lv' + b.lv : '') + (b.ch && b.ch !== 'same' ? ' ch-' + b.ch : '');
  row.id = b.id;
  row.setAttribute('data-key', b.key);
  var chg = b.ch === 'mod' ? '<em class="chg">수정됨</em>' : (b.ch === 'new' ? '<em class="chg">새 블록</em>' : '');
  var lines = b.s === b.e ? 'L' + b.s : 'L' + b.s + '–' + b.e;
  var inner = b.t === 'meta'
    ? '<details><summary>' + (b.k === 'tex' ? (b.s === 1 ? 'LaTeX preamble' : 'document 끝') : 'front matter') + ' · ' + (b.e - b.s + 1) + '줄</summary><div class="src"></div></details>'
    : '<div class="src"></div>';
  row.innerHTML = '<div class="L"><div class="gut"><b>' + b.id + '</b>' + lines + chg + '</div><div>' + inner + '</div></div>'
    + '<div class="R"><div class="rs-wrap"></div><div class="cms"></div><button class="add" type="button">+ 코멘트</button></div>';
  row.querySelector('.add').addEventListener('click', function(){ addComment(b.key, {}); });
  rowEls[b.key] = row;
  return row;
}
function ghostRow(g){
  var row = document.createElement('div');
  row.className = 'row ghost';
  row.innerHTML = '<div class="L"><div class="gut"><b>삭제</b></div><div><div class="src"></div></div></div><div class="R">이전 버전(v' + pad(DOC.prev_version, 3) + ')에서 삭제된 블록</div>';
  row.querySelector('.src').textContent = g.text;
  return row;
}
(function buildRows(){
  var frag = document.createDocumentFragment(), ghostsAt = {}, lastFile = null;
  D.ghosts.forEach(function(g){ (ghostsAt[g.before] = ghostsAt[g.before] || []).push(g); });
  BLOCKS.forEach(function(b, i){
    if (DOC.sources.length > 1 && b.file !== lastFile){
      var fd = document.createElement('div'); fd.className = 'filediv'; fd.textContent = b.file; frag.appendChild(fd); lastFile = b.file;
    }
    (ghostsAt[i] || []).forEach(function(g){ frag.appendChild(ghostRow(g)); });
    frag.appendChild(blockRow(b));
  });
  (ghostsAt[BLOCKS.length] || []).forEach(function(g){ frag.appendChild(ghostRow(g)); });
  sheet.appendChild(frag);
  BLOCKS.forEach(function(b){ renderSrc(b); renderRight(b); });
})();

// ---- 코멘트 카드 ----
function newCid(){
  var used = {};
  comments.concat(archive).forEach(function(c){ used[c.cid] = 1; });
  var id;
  do { id = 'c-' + Math.random().toString(36).slice(2, 6); } while (used[id]);
  return id;
}
function stateText(c){
  if (c.state === 'sent') return {cls: 'sent', t: '전송됨 · ' + (c.sentRev || '')};
  if (c.state === 'returned') return {cls: 'returned', t: (STATUS_LABEL[c.result && c.result.status] || '보류') + ' · 다시 보내려면 수정'};
  return {cls: '', t: c.result ? '재요청 초안' : '초안'};
}
function cardEl(c){
  var el = document.createElement('div');
  el.className = 'cm';
  el.setAttribute('data-cid', c.cid);
  el.setAttribute('data-type', c.type);
  var opts = TYPES.map(function(t){ return '<option value="' + t[0] + '"' + (t[0] === c.type ? ' selected' : '') + '>' + t[1] + '</option>'; }).join('');
  var st = stateText(c);
  el.innerHTML = '<div class="cm-hd"><select class="cm-type" title="유형">' + opts + '</select><span class="cm-state ' + st.cls + '">' + esc(st.t) + '</span><button class="cm-x" type="button" title="코멘트 삭제">×</button></div>'
    + (c.quote ? '<blockquote class="cm-q" title="원고에서 위치 보기"></blockquote>' : '')
    + '<textarea class="cm-t" rows="2" placeholder="' + (c.key === 'ALL' ? '문서 전체에 적용할 지시' : '수정 요청') + '"></textarea>'
    + '<button class="cm-sug-btn" type="button"' + (c.sug ? ' hidden' : '') + '>+ 제안 문구</button>'
    + '<textarea class="cm-s" rows="2" placeholder="이렇게 바꿔 주세요 (선택 - 에이전트가 이 문구를 우선 사용)"' + (c.sug ? '' : ' hidden') + '></textarea>'
    + (c.result && c.result.note ? '<div class="cm-note"><b>에이전트 · ' + esc(STATUS_LABEL[c.result.status] || c.result.status) + '</b> — ' + esc(c.result.note) + '</div>' : '');
  if (c.quote) el.querySelector('.cm-q').textContent = c.quote;
  var ta = el.querySelector('.cm-t'), ts = el.querySelector('.cm-s');
  ta.value = c.text || ''; ts.value = c.sug || '';
  function touched(){
    c.updated = Date.now();
    if (c.state !== 'draft'){ c.state = 'draft'; var s2 = stateText(c), sp = el.querySelector('.cm-state'); sp.className = 'cm-state'; sp.textContent = s2.t; }
    saveSoon(); updateCounts();
  }
  ta.addEventListener('input', function(){ c.text = ta.value; touched(); });
  ts.addEventListener('input', function(){ c.sug = ts.value; touched(); });
  [ta, ts].forEach(function(t){
    t.addEventListener('keydown', function(e){ if (e.key === 'Escape') t.blur(); });
    t.addEventListener('input', function(){ autoGrow(t); });
  });
  el.querySelector('.cm-type').addEventListener('change', function(e){ c.type = e.target.value; el.setAttribute('data-type', c.type); touched(); });
  el.querySelector('.cm-sug-btn').addEventListener('click', function(e){ e.target.hidden = true; ts.hidden = false; ts.focus(); });
  el.querySelector('.cm-x').addEventListener('click', function(){ removeComment(c); });
  var q = el.querySelector('.cm-q');
  if (q) q.addEventListener('click', function(){ focusMark(c); });
  el.addEventListener('mouseenter', function(){ hiMarks(c.cid, true); });
  el.addEventListener('mouseleave', function(){ hiMarks(c.cid, false); });
  return el;
}
function autoGrow(t){ t.style.height = 'auto'; t.style.height = Math.min(t.scrollHeight + 2, 420) + 'px'; }
function sortCards(list, b){
  return list.sort(function(p, q){
    var pa = p.quote && b ? b.text.indexOf(p.quote) : -1, qa = q.quote && b ? b.text.indexOf(q.quote) : -1;
    if (pa !== qa) return (pa < 0 ? 1e9 : pa) - (qa < 0 ? 1e9 : qa);
    return (p.created || 0) - (q.created || 0);
  });
}
function renderRight(b){
  var row = rowEls[b.key];
  if (!row) return;
  var rsw = row.querySelector('.rs-wrap'), cms = row.querySelector('.cms');
  var res = (D.res_by_block[b.id] || []).filter(function(it){ return !dismissed[it.rev + ':' + it.cid]; });
  rsw.innerHTML = '';
  res.forEach(function(it){
    var d = document.createElement('div');
    d.className = 'rs st-' + it.status;
    d.innerHTML = '<div class="rs-hd"><span class="st">' + esc(STATUS_LABEL[it.status] || it.status) + '</span><span class="rs-t"></span><button class="rs-x" type="button">확인</button></div>'
      + (it.text ? '<div class="rs-req"></div>' : '') + (it.note ? '<div class="rs-note"></div>' : '');
    d.querySelector('.rs-t').textContent = (TYPE_LABEL[it.type] || '') + (it.quote ? ' · “' + it.quote + '”' : '');
    if (it.text) d.querySelector('.rs-req').textContent = '요청: ' + it.text;
    if (it.note) d.querySelector('.rs-note').textContent = '에이전트: ' + it.note;
    d.querySelector('.rs-x').addEventListener('click', function(){ dismissed[it.rev + ':' + it.cid] = 1; lsSet('dismissed', dismissed); renderRight(b); });
    rsw.appendChild(d);
  });
  cms.innerHTML = '';
  sortCards(comments.filter(function(c){ return c.key === b.key; }), b).forEach(function(c){ cms.appendChild(cardEl(c)); });
  cms.querySelectorAll('textarea').forEach(function(t){ if (!t.hidden) autoGrow(t); });
  row.classList.toggle('has-c', cms.children.length > 0 || res.length > 0);
}
function refreshBlock(key){
  if (key === 'ALL'){ renderGlobal(); return; }
  var b = byKey[key];
  if (b){ renderSrc(b); renderRight(b); } else renderOrphans();
}
function addComment(key, o){
  var b = key === 'ALL' ? null : byKey[key];
  var c = {cid: newCid(), key: key, bid: b ? b.id : 'ALL', file: b ? b.file : '', s: b ? b.s : 0, e: b ? b.e : 0,
           sec: b ? (b.sec || '') : '', h: b ? b.h : '', ctx: b ? b.text : '', ver: DOC.version,
           type: o.type || (key === 'ALL' ? 'style' : 'rewrite'), quote: o.quote || '', qoff: o.qoff == null ? -1 : o.qoff,
           text: '', sug: '', state: 'draft', created: Date.now()};
  comments.push(c); saveComments(); refreshBlock(key); updateCounts();
  var el = document.querySelector('.cm[data-cid="' + c.cid + '"] .cm-t');
  if (el){ el.focus({preventScroll: true}); el.scrollIntoView({block: 'nearest', behavior: 'smooth'}); }
  return c;
}
var lastRemoved = null;
function removeComment(c){
  var idx = comments.indexOf(c);
  if (idx < 0) return;
  comments.splice(idx, 1); saveComments(); refreshBlock(c.key); updateCounts();
  lastRemoved = {c: c, idx: idx};
  toast('코멘트를 삭제했습니다.', '되돌리기', function(){
    if (!lastRemoved) return;
    comments.splice(Math.min(lastRemoved.idx, comments.length), 0, lastRemoved.c);
    saveComments(); refreshBlock(lastRemoved.c.key); updateCounts(); lastRemoved = null;
  });
}
function hiMarks(cid, on){
  document.querySelectorAll('mark.q').forEach(function(m){
    if ((' ' + m.getAttribute('data-cids') + ' ').indexOf(' ' + cid + ' ') >= 0) m.classList.toggle('on', on);
  });
}
function focusMark(c){
  var row = rowEls[c.key];
  if (!row) return;
  row.scrollIntoView({block: 'center', behavior: 'smooth'});
  row.classList.remove('flash'); void row.offsetWidth; row.classList.add('flash');
  hiMarks(c.cid, true); setTimeout(function(){ hiMarks(c.cid, false); }, 1600);
}
sheet.addEventListener('click', function(e){
  var m = e.target.closest && e.target.closest('mark.q');
  if (!m || !window.getSelection().isCollapsed) return;
  var cid = m.getAttribute('data-cids').split(' ')[0];
  var card = document.querySelector('.cm[data-cid="' + cid + '"]');
  if (card){ card.classList.add('on'); card.querySelector('.cm-t').focus(); setTimeout(function(){ card.classList.remove('on'); }, 1400); }
});

// ---- 전체 지시 / 위치 잃은 코멘트 패널 ----
function renderGlobal(){
  var p = $('global-panel'), list = comments.filter(function(c){ return c.key === 'ALL'; });
  p.hidden = !list.length;
  if (!list.length){ p.innerHTML = ''; return; }
  p.innerHTML = '<div class="p-hd"><b>전체 지시</b><span class="sub">문서 전체에 적용 · ' + list.length + '건</span><button class="btn" type="button">+ 추가</button></div><div class="p-body" style="display:grid;gap:8px;grid-template-columns:repeat(auto-fill,minmax(320px,1fr))"></div>';
  p.querySelector('.btn').addEventListener('click', function(){ addComment('ALL', {}); });
  var body = p.querySelector('.p-body');
  list.forEach(function(c){ body.appendChild(cardEl(c)); });
  body.querySelectorAll('textarea').forEach(function(t){ if (!t.hidden) autoGrow(t); });
}
function orphans(){ return comments.filter(function(c){ return c.key !== 'ALL' && !byKey[c.key]; }); }
function renderOrphans(){
  var p = $('orphan-panel'), list = orphans();
  p.hidden = !list.length;
  if (!list.length){ p.innerHTML = ''; return; }
  p.innerHTML = '<div class="p-hd"><b>위치를 잃은 코멘트</b><span class="sub">원고가 바뀌어 원래 블록을 찾지 못함 · ' + list.length + '건 · 내보내기에 포함되며 에이전트가 원래 문장으로 위치를 찾습니다</span></div><div class="p-body" style="display:grid;gap:8px;grid-template-columns:repeat(auto-fill,minmax(340px,1fr))"></div>';
  var body = p.querySelector('.p-body');
  list.forEach(function(c){
    var wrap = document.createElement('div');
    wrap.appendChild(cardEl(c));
    var d = document.createElement('details');
    d.className = 'orphan-ctx';
    d.innerHTML = '<summary>작성 당시 블록 (v' + pad(c.ver || 0, 3) + ' · ' + esc(c.file) + ':' + c.s + '–' + c.e + ')</summary><pre></pre>';
    d.querySelector('pre').textContent = c.ctx || '';
    wrap.appendChild(d);
    body.appendChild(wrap);
  });
}
renderGlobal(); renderOrphans();
$('btn-global').addEventListener('click', function(){ addComment('ALL', {}); $('global-panel').scrollIntoView({block: 'nearest', behavior: 'smooth'}); });

// ---- 최근 반영 결과 패널 ----
(function renderResults(){
  var L = D.latest, p = $('res-panel');
  if (!L || !L.items || !L.items.length) return;
  var cnt = {};
  L.items.forEach(function(it){ cnt[it.status] = (cnt[it.status] || 0) + 1; });
  var pills = Object.keys(STATUS_LABEL).filter(function(k){ return cnt[k]; }).map(function(k){ return '<span class="st st-' + k + '" style="border:1px solid var(--line)">' + STATUS_LABEL[k] + ' ' + cnt[k] + '</span>'; }).join(' ');
  var collapsed = ui.resCollapsed === L.rev;
  p.hidden = false;
  p.innerHTML = '<div class="p-hd"><b>최근 반영 결과</b><span class="sub">' + esc(L.rev) + (L.applied_at ? ' · ' + esc(String(L.applied_at).replace('T', ' ').slice(0, 16)) : '') + '</span>' + pills + '<button class="btn" type="button">' + (collapsed ? '펼치기' : '접기') + '</button></div>'
    + '<div class="p-body"' + (collapsed ? ' hidden' : '') + '>' + (L.summary ? '<p class="p-sum"></p>' : '') + '<ol class="res-list"></ol></div>';
  if (L.summary) p.querySelector('.p-sum').textContent = L.summary;
  var ol = p.querySelector('.res-list');
  L.items.forEach(function(it){
    var li = document.createElement('li');
    li.className = 'st-' + it.status;
    li.innerHTML = '<span class="st">' + esc(STATUS_LABEL[it.status] || it.status) + '</span><span class="loc"></span>' + (it.target ? '<span class="jump">블록으로 이동</span>' : '') + '<span class="req"></span><span class="note"></span>';
    li.querySelector('.loc').textContent = (TYPE_LABEL[it.type] || '') + ' · ' + (it.block === 'ALL' ? '전체 지시' : (it.block || '') + (it.target && it.target !== it.block ? ' → ' + it.target : '')) + (it.loc ? ' · ' + it.loc : '');
    li.querySelector('.req').textContent = (it.quote ? '“' + it.quote + '” — ' : '') + (it.text || '');
    li.querySelector('.note').textContent = it.note ? '에이전트: ' + it.note : '';
    var j = li.querySelector('.jump');
    if (j) j.addEventListener('click', function(){ var r = $(it.target); if (r){ r.scrollIntoView({block: 'center', behavior: 'smooth'}); r.classList.remove('flash'); void r.offsetWidth; r.classList.add('flash'); } });
    ol.appendChild(li);
  });
  p.querySelector('.btn').addEventListener('click', function(e){
    var body = p.querySelector('.p-body'); body.hidden = !body.hidden;
    e.target.textContent = body.hidden ? '펼치기' : '접기';
    ui.resCollapsed = body.hidden ? L.rev : null; lsSet('ui', ui);
  });
})();

// ---- 보기 전환 · 변경 이동 · 필터 ----
var seg = $('viewseg');
function setView(diff){
  ui.diff = !!diff; lsSet('ui', ui);
  document.body.classList.toggle('show-diff', ui.diff);
  seg.querySelectorAll('button').forEach(function(bt){ bt.classList.toggle('on', (bt.getAttribute('data-v') === 'diff') === ui.diff); });
  BLOCKS.forEach(renderSrc);
  hideFab(); updateHint();
}
seg.querySelectorAll('button').forEach(function(bt){
  if (bt.getAttribute('data-v') === 'diff' && !hasPrev){ bt.disabled = true; bt.title = '비교할 이전 버전이 아직 없습니다'; }
  bt.addEventListener('click', function(){ setView(bt.getAttribute('data-v') === 'diff'); });
});
var nChanged = DOC.changes.mod + DOC.changes.new + DOC.changes.deleted;
if (hasPrev && nChanged){
  $('chnav').innerHTML = '변경 <b>' + nChanged + '</b> <button class="btn ghost" id="ch-prev" title="이전 변경">↑</button><button class="btn ghost" id="ch-next" title="다음 변경">↓</button>';
  var jumpCh = function(dir){
    if (!ui.diff) setView(true);
    var rows = Array.prototype.slice.call(document.querySelectorAll('.row.ch-mod,.row.ch-new,.row.ghost'));
    var y = $('topbar').offsetHeight + 50, pick = null;
    if (dir > 0) pick = rows.filter(function(r){ return r.getBoundingClientRect().top > y + 4; })[0];
    else pick = rows.filter(function(r){ return r.getBoundingClientRect().top < y - 4; }).pop();
    if (pick){ pick.scrollIntoView({block: 'start', behavior: 'smooth'}); pick.classList.remove('flash'); void pick.offsetWidth; pick.classList.add('flash'); }
  };
  $('ch-prev').addEventListener('click', function(){ jumpCh(-1); });
  $('ch-next').addEventListener('click', function(){ jumpCh(1); });
}
$('onlyc').checked = !!ui.onlyC;
document.body.classList.toggle('only-c', !!ui.onlyC);
$('onlyc').addEventListener('change', function(e){ ui.onlyC = e.target.checked; lsSet('ui', ui); document.body.classList.toggle('only-c', ui.onlyC); });
$('btn-help').addEventListener('click', function(){ var h = $('help-panel'); h.hidden = !h.hidden; if (!h.hidden) h.scrollIntoView({block: 'nearest'}); });

function updateCounts(){
  var drafts = comments.filter(function(c){ return c.state === 'draft'; }).length;
  var sent = comments.filter(function(c){ return c.state === 'sent'; }).length;
  $('counts').innerHTML = '코멘트 <b>' + comments.length + '</b>' + (drafts ? ' · 내보낼 초안 <b>' + drafts + '</b>' : '') + (sent ? ' · 반영 대기 ' + sent : '');
  $('btn-export').textContent = '.md 내보내기' + (drafts ? ' (' + drafts + ')' : '');
  updateHint();
}
function updateHint(){
  var h = $('hint'), msg = '';
  if (ui.diff) msg = '변경 표시 중 — 초록 = 추가, 분홍 취소선 = 삭제 (v' + pad(DOC.prev_version, 3) + ' → v' + pad(DOC.version, 3) + '). 구절 코멘트는 정리본에서 드래그하세요.';
  else if (!comments.length) msg = '왼쪽 원고에서 구절을 드래그하면 그 구절에 코멘트를 달 수 있습니다. 블록 전체는 오른쪽 + 코멘트.';
  h.textContent = msg; h.hidden = !msg; measure();
}

// ---- 드래그 선택 → 코멘트 ----
var fab = $('selfab'), pending = null;
function hideFab(){ fab.classList.remove('show'); pending = null; }
function srcOf(node){ var el = node.nodeType === 1 ? node : node.parentElement; return el && el.closest ? el.closest('.row:not(.ghost) .src') : null; }
function checkSel(){
  var sel = window.getSelection();
  if (!sel.rangeCount || sel.isCollapsed || ui.diff){ hideFab(); return; }
  var r = sel.getRangeAt(0), s1 = srcOf(r.startContainer), s2 = srcOf(r.endContainer);
  if (!s1 || s1 !== s2){ hideFab(); return; }
  var raw = r.toString();
  if (!raw.trim()){ hideFab(); return; }
  var pre = document.createRange(); pre.selectNodeContents(s1); pre.setEnd(r.startContainer, r.startOffset);
  var off = pre.toString().length, lead = raw.length - raw.replace(/^\s+/, '').length;
  var row = s1.closest('.row');
  pending = {key: row.getAttribute('data-key'), quote: raw.trim(), qoff: off + lead};
  var rects = r.getClientRects(), rc = rects.length ? rects[rects.length - 1] : r.getBoundingClientRect();
  fab.classList.add('show');
  var left = Math.min(rc.right + window.scrollX - fab.offsetWidth / 2, window.scrollX + document.documentElement.clientWidth - fab.offsetWidth - 10);
  fab.style.left = Math.max(window.scrollX + 8, left) + 'px';
  fab.style.top = (rc.bottom + window.scrollY + 8) + 'px';
}
document.addEventListener('mouseup', function(e){ if (e.target === fab) return; setTimeout(checkSel, 0); });
document.addEventListener('keyup', function(e){ if (e.shiftKey || e.key === 'Shift') checkSel(); });
fab.addEventListener('mousedown', function(e){ e.preventDefault(); });
fab.addEventListener('click', function(){
  if (!pending) return;
  var p = pending; hideFab(); window.getSelection().removeAllRanges();
  addComment(p.key, {quote: p.quote, qoff: p.qoff});
});

// ---- .md 생성 ----
function fenceFor(t){ var m = t.match(/~+/g), n = 3; (m || []).forEach(function(x){ n = Math.max(n, x.length); }); return new Array(n + 2).join('~'); }
function quoteLines(t){ return t.split('\n').map(function(l){ return l ? '> ' + l : '>'; }).join('\n'); }
function stampNow(){
  var d = new Date();
  return String(d.getFullYear()).slice(2) + pad(d.getMonth() + 1, 2) + pad(d.getDate(), 2) + '-' + pad(d.getHours(), 2) + pad(d.getMinutes(), 2);
}
function isoNow(){
  var d = new Date(), tz = -d.getTimezoneOffset(), sg = tz >= 0 ? '+' : '-';
  tz = Math.abs(tz);
  return d.getFullYear() + '-' + pad(d.getMonth() + 1, 2) + '-' + pad(d.getDate(), 2) + 'T' + pad(d.getHours(), 2) + ':' + pad(d.getMinutes(), 2) + ':' + pad(d.getSeconds(), 2) + sg + pad(Math.floor(tz / 60), 2) + ':' + pad(tz % 60, 2);
}
function hasContent(c){ return (c.text || '').trim() || (c.sug || '').trim() || (c.type === 'delete' && c.quote); }
function exportList(incSent){
  var pick = comments.filter(function(c){ return incSent || c.state === 'draft'; });
  var empty = pick.filter(function(c){ return !hasContent(c); }).length;
  pick = pick.filter(hasContent);
  var g = pick.filter(function(c){ return c.key === 'ALL'; });
  var bl = pick.filter(function(c){ return c.key !== 'ALL' && byKey[c.key]; });
  var or = pick.filter(function(c){ return c.key !== 'ALL' && !byKey[c.key]; });
  bl.sort(function(p, q){
    var bp = byKey[p.key], bq = byKey[q.key];
    if (bp.i !== bq.i) return bp.i - bq.i;
    return sortCards([p, q], bp)[0] === p ? -1 : 1;
  });
  return {global: g, blocks: bl, orphans: or, empty: empty, all: g.concat(bl, or)};
}
function buildMd(name, sets, scope){
  var L = [];
  L.push('---');
  L.push('type: manuscript-revision-request');
  L.push('doc: "' + DOC.title.replace(/"/g, '\\"') + '"');
  L.push('rev: ' + name);
  L.push('base_version: ' + DOC.version);
  L.push('created: ' + isoNow());
  L.push('scope: ' + scope + '   # minimal | free');
  L.push('count: ' + sets.all.length);
  L.push('sources:');
  DOC.sources.forEach(function(s){ L.push('  - path: ' + s.path); L.push('    sha1: ' + s.sha1 + '   # 파일 바이트 sha1 앞 10자 (빌드 시점)'); });
  L.push('---', '');
  L.push('# 첨삭 요청 · ' + name, '');
  L.push('> review.html 에서 내보낸 수정 요청. 반영 절차: `prompts/apply_revisions.md`.');
  L.push('> 수정 범위: **' + (scope === 'free' ? '자유 재작성 — 요청 취지 안에서 해당 블록의 문장 재구성 허용' : '최소 수정 — 요청된 부분만 고치고 나머지 문장은 그대로') + '**.');
  L.push('> 반영 후 `revisions/' + name + '.applied.json` 을 쓰고 `python build_review.py` 로 다시 빌드한다.', '');
  var n = 0;
  function item(c, where){
    n++;
    var b = byKey[c.key];
    var blockId = c.key === 'ALL' ? 'ALL' : (b ? b.id : 'ORPHAN');
    var file = b ? b.file : (c.file || ''), s = b ? b.s : c.s, e = b ? b.e : c.e, h = b ? b.h : (c.h || '');
    var sec = b ? (b.sec || '') : (c.sec || '');
    var loc = c.key === 'ALL' ? '문서 전체' : (b ? blockId + ' · ' + file + ':' + s + '-' + e : '위치 잃음 · ' + file + ':' + s + '-' + e + ' (v' + pad(c.ver || 0, 3) + ' 기준)');
    L.push('### ' + n + '. [' + c.cid + '] ' + (TYPE_LABEL[c.type] || c.type) + ' · ' + loc + (sec ? ' · ' + sec : ''));
    L.push('<!-- cid: ' + c.cid + ' | block: ' + blockId + ' | hash: ' + h + ' | file: ' + file + ' | lines: ' + (c.key === 'ALL' ? '' : s + '-' + e) + ' | type: ' + c.type + ' -->', '');
    if (c.quote){ L.push('**대상 구절**'); L.push(quoteLines(c.quote), ''); }
    L.push('**요청**');
    L.push(((c.text || '').trim() || (c.type === 'delete' ? '(대상 구절 삭제)' : '(제안 문구대로 교체)')), '');
    if ((c.sug || '').trim()){ L.push('**제안 문구**'); L.push(quoteLines(c.sug.trim()), ''); }
    if (c.result && c.result.note){ L.push('**이전 에이전트 응답** (' + (c.result.rev || '') + ' · ' + (STATUS_LABEL[c.result.status] || c.result.status) + ')'); L.push(quoteLines(c.result.note), ''); }
    var ctx = b ? b.text : (c.ctx || '');
    if (ctx && c.key !== 'ALL'){
      var f = fenceFor(ctx);
      L.push('<details><summary>원문 블록 (' + file + ':' + s + '-' + e + ')</summary>', '');
      L.push(f + 'text'); L.push(ctx); L.push(f, '');
      L.push('</details>', '');
    }
  }
  if (sets.global.length){ L.push('## 전체 지시', ''); sets.global.forEach(function(c){ item(c); }); }
  if (sets.blocks.length){ L.push('## 블록별 수정 요청', ''); sets.blocks.forEach(function(c){ item(c); }); }
  if (sets.orphans.length){ L.push('## 위치를 잃은 요청', '', '> 작성 이후 원고가 바뀌어 원래 블록을 찾지 못한 요청. 원문 블록·대상 구절로 현재 위치를 찾아 반영하고, 찾지 못하면 skipped 로 보고한다.', ''); sets.orphans.forEach(function(c){ item(c); }); }
  return L.join('\n').replace(/\n{3,}/g, '\n\n') + '\n';
}

// ---- 내보내기 모달 ----
var ov = $('ov'), cur = null;
function openExport(){
  refreshExport();
  ov.classList.add('show');
  ping().then(updateApplyBtn);
  setTimeout(function(){ $('m-text').scrollTop = 0; }, 0);
}
function closeExport(){ ov.classList.remove('show'); }
function refreshExport(){
  var scope = ui.scope, inc = $('m-incsent').checked;
  document.querySelectorAll('input[name=scope]').forEach(function(r){ r.checked = r.value === scope; });
  var sets = exportList(inc);
  var name = 'rev_v' + pad(DOC.version, 3) + '_' + stampNow();
  cur = {name: name, sets: sets, md: sets.all.length ? buildMd(name, sets, scope) : ''};
  $('m-name').textContent = sets.all.length ? name + '.md · 요청 ' + sets.all.length + '건' : '';
  $('m-text').value = cur.md || '(내보낼 코멘트가 없습니다 — 초안 상태의 코멘트만 기본 포함됩니다)';
  var w = [];
  if (sets.empty) w.push('내용이 빈 코멘트 ' + sets.empty + '건은 제외했습니다.');
  var skipped = comments.filter(function(c){ return c.state !== 'draft'; }).length;
  if (!inc && skipped) w.push('전송·보류 상태 ' + skipped + '건은 제외 (위 체크박스로 포함).');
  $('m-warn').textContent = w.join(' ');
  $('m-cmd').innerHTML = cur.md ? '저장 후 Claude Code 에서: <code id="m-cmdtxt">revisions/' + esc(name) + '.md 반영해줘</code><button class="btn ghost" type="button" id="m-cmdcopy">명령 복사</button>' : '';
  var cc = $('m-cmdcopy');
  if (cc) cc.addEventListener('click', function(){ copyText($('m-cmdtxt').textContent).then(function(){ toast('명령을 복사했습니다.'); }); });
  ['m-save', 'm-dl', 'm-copy'].forEach(function(id){ $(id).disabled = !cur.md; });
  updateApplyBtn();
}
function updateApplyBtn(){
  var b = $('m-apply');
  b.disabled = !(cur && cur.md) || !bridgeOk;
  b.title = bridgeOk ? '브릿지(' + BRIDGE + ')로 저장 + 헤드리스 Claude 가 반영' : '브릿지가 꺼져 있습니다 — python review_bridge.py';
}
$('btn-export').addEventListener('click', openExport);
$('m-close').addEventListener('click', closeExport);
ov.addEventListener('mousedown', function(e){ if (e.target === ov) closeExport(); });
document.addEventListener('keydown', function(e){ if (e.key === 'Escape' && ov.classList.contains('show')) closeExport(); });
document.querySelectorAll('input[name=scope]').forEach(function(r){ r.addEventListener('change', function(){ ui.scope = r.value; lsSet('ui', ui); refreshExport(); }); });
$('m-incsent').addEventListener('change', refreshExport);

function markSent(sets, name){
  sets.all.forEach(function(c){ c.state = 'sent'; c.sentRev = name; });
  saveComments();
  var keys = {};
  sets.all.forEach(function(c){ keys[c.key] = 1; });
  Object.keys(keys).forEach(refreshBlock);
  updateCounts();
}
function copyText(t){
  if (navigator.clipboard && window.isSecureContext !== false) return navigator.clipboard.writeText(t).catch(fallbackCopy);
  return fallbackCopy();
  function fallbackCopy(){ var ta = document.createElement('textarea'); ta.value = t; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); return Promise.resolve(); }
}
$('m-copy').addEventListener('click', function(){ copyText(cur.md).then(function(){ toast('클립보드에 복사했습니다 (복사는 전송 표시를 하지 않습니다).'); }); });
$('m-dl').addEventListener('click', function(){
  var blob = new Blob([cur.md], {type: 'text/markdown;charset=utf-8'}), a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = cur.name + '.md';
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(function(){ URL.revokeObjectURL(a.href); }, 4000);
  markSent(cur.sets, cur.name); closeExport();
  toast('다운로드했습니다 → 프로젝트의 revisions/ 폴더로 옮겨 주세요: ' + cur.name + '.md');
});

// 폴더 저장 — 프로젝트 폴더(또는 revisions 폴더)를 한 번 지정하면 이후 다이얼로그 없이 저장
function idb(){ return new Promise(function(res, rej){ var r = indexedDB.open('mrev-fs', 1); r.onupgradeneeded = function(){ r.result.createObjectStore('h'); }; r.onsuccess = function(){ res(r.result); }; r.onerror = function(){ rej(r.error); }; }); }
function idbGet(k){ return idb().then(function(db){ return new Promise(function(res){ var t = db.transaction('h').objectStore('h').get(k); t.onsuccess = function(){ res(t.result || null); }; t.onerror = function(){ res(null); }; }); }).catch(function(){ return null; }); }
function idbSet(k, v){ return idb().then(function(db){ return new Promise(function(res){ var t = db.transaction('h', 'readwrite').objectStore('h').put(v, k); t.onsuccess = function(){ res(); }; t.onerror = function(){ res(); }; }); }).catch(function(){}); }
function pickDir(){ return window.showDirectoryPicker({mode: 'readwrite', id: 'mrev'}).then(function(d){ return idbSet('dir:' + DOC.id, d).then(function(){ return d; }); }); }
function ensurePerm(h){
  if (!h.queryPermission) return Promise.resolve(true);
  return h.queryPermission({mode: 'readwrite'}).then(function(p){ return p === 'granted' ? true : h.requestPermission({mode: 'readwrite'}).then(function(q){ return q === 'granted'; }); });
}
async function revDir(dir){
  if (dir.name === 'revisions') return dir;
  return dir.getDirectoryHandle('revisions', {create: true});
}
$('m-save').addEventListener('click', async function(){
  if (bridgeOk){  // 브릿지가 켜져 있으면 폴더 지정 없이 바로 revisions/ 에 저장
    try {
      var j = await post('/save', {name: cur.name, md: cur.md, project_id: DOC.id});
      if (j.status === 'ok'){ markSent(cur.sets, cur.name); closeExport(); toast('저장했습니다 (브릿지): ' + j.path); return; }
      toast(j.message || '브릿지 저장 실패 — 폴더 저장으로 진행합니다.');
    } catch(e){}
  }
  if (!window.showDirectoryPicker){ toast('이 브라우저는 폴더 저장을 지원하지 않습니다 — 다운로드 또는 브릿지를 쓰세요 (Chrome/Edge 는 지원).'); return; }
  try {
    var dir = await idbGet('dir:' + DOC.id);
    if (!dir){ toast('review.html 이 있는 프로젝트 폴더를 지정하세요 (한 번만).'); dir = await pickDir(); }
    if (!(await ensurePerm(dir))){ toast('폴더 쓰기 권한이 없습니다.'); return; }
    var rd = await revDir(dir);
    var fh = await rd.getFileHandle(cur.name + '.md', {create: true}), w = await fh.createWritable();
    await w.write(cur.md); await w.close();
    markSent(cur.sets, cur.name); closeExport();
    toast('저장했습니다: ' + (rd === dir ? dir.name : dir.name + '/revisions') + '/' + cur.name + '.md');
  } catch(e){ if (!e || e.name !== 'AbortError') toast('저장 실패: ' + (e && e.message || e)); }
});
$('m-setdir').addEventListener('click', async function(){
  if (!window.showDirectoryPicker){ toast('이 브라우저는 폴더 지정을 지원하지 않습니다.'); return; }
  try { var d = await pickDir(); toast('저장 폴더: ' + d.name); } catch(e){}
});

// ---- 브릿지 (선택) ----
var bridgeOk = false;
function bridgeStatus(ok, msg){
  bridgeOk = ok;
  var el = $('bridge-st');
  if (el) el.innerHTML = '<span class="dot' + (ok ? ' on' : '') + '"></span>' + esc(msg);
}
function ping(){
  return fetch(BRIDGE + '/ping', {cache: 'no-store'}).then(function(r){ return r.json(); }).then(function(j){
    if (!j || !j.ok) throw new Error('bad');
    if (j.project_id && j.project_id !== DOC.id){ bridgeStatus(false, '브릿지 연결됨 · 다른 프로젝트(' + j.title + ')'); return false; }
    bridgeStatus(true, '브릿지 연결됨 :' + DOC.bridge_port); return true;
  }).catch(function(){ bridgeStatus(false, '브릿지 꺼짐 (선택 기능)'); return false; });
}
function post(path, body){
  // text/plain = CORS preflight 없는 단순 요청 (file:// 에서도 안정적)
  return fetch(BRIDGE + path, {method: 'POST', headers: {'Content-Type': 'text/plain;charset=utf-8'}, body: JSON.stringify(body)}).then(function(r){ return r.json(); });
}
$('m-apply').addEventListener('click', function(){
  var c = cur;
  $('m-apply').disabled = true;
  post('/apply', {name: c.name, md: c.md, project_id: DOC.id}).then(function(j){
    if (j.job_id && (j.status === 'accepted' || j.status === 'busy')){
      if (j.status === 'accepted') markSent(c.sets, c.name);
      else toast(j.message || '이미 진행 중인 작업이 있습니다.');
      lsSet('job', {id: j.job_id, name: j.status === 'accepted' ? c.name : (j.name || ''), t: Date.now()});
      closeExport(); pollJob();
    } else { toast(j.message || '브릿지 오류'); $('m-apply').disabled = false; }
  }).catch(function(e){ toast('브릿지 요청 실패: ' + e.message); $('m-apply').disabled = false; });
});
var jobEl = $('job');
function jobView(html){ jobEl.innerHTML = html; jobEl.classList.add('show'); var x = jobEl.querySelector('.cm-x'); if (x) x.addEventListener('click', function(){ jobEl.classList.remove('show'); }); }
function fmtSec(s){ s = Math.round(s || 0); return Math.floor(s / 60) + ':' + pad(s % 60, 2); }
function pollJob(){
  var job = lsGet('job', null);
  if (!job) return;
  fetch(BRIDGE + '/job?id=' + encodeURIComponent(job.id), {cache: 'no-store'}).then(function(r){ return r.json(); }).then(function(j){
    if (j.status === 'running'){
      jobView('<div class="j-hd"><span class="spin"></span>에이전트 반영 중 · ' + esc(job.name) + ' · ' + fmtSec(j.elapsed) + '<button class="cm-x" type="button" title="숨기기 (작업은 계속)">×</button></div><pre>' + esc((j.log_tail || []).join('\n')) + '</pre>');
      setTimeout(pollJob, 3000);
      return;
    }
    lsDel('job');
    if (j.status === 'unknown'){ jobView('<div class="j-hd">작업을 찾을 수 없습니다<button class="cm-x" type="button">×</button></div><pre>' + esc(j.message || '') + '</pre>'); return; }
    var ok = j.status === 'ok', r = j.result || {};
    var line = ok ? ('반영 ' + (r.applied || 0) + ' · 부분 ' + (r.partial || 0) + ' · 보류 ' + (r.skipped || 0) + ' · 답변 ' + (r.answered || 0)) : (j.message || '실패');
    jobView('<div class="j-hd">' + (ok ? '반영 완료' : '반영 실패') + ' · ' + esc(job.name) + '<button class="cm-x" type="button">×</button></div><pre>' + esc(line + '\n' + (j.log_tail || []).slice(-6).join('\n')) + '</pre>'
      + (ok ? '<div class="j-ft"><button class="btn primary" type="button" id="j-reload">새로고침해서 결과 보기</button></div>' : ''));
    var rl = $('j-reload');
    if (rl) rl.addEventListener('click', function(){ saveComments(); location.reload(); });
  }).catch(function(){
    jobView('<div class="j-hd"><span class="spin"></span>브릿지 응답 없음 · 재시도 중<button class="cm-x" type="button">×</button></div>');
    setTimeout(pollJob, 5000);
  });
}

// ---- 공용 ----
var toastTimer = null;
function toast(msg, act, fn){
  var t = $('toast');
  t.innerHTML = '<span></span>' + (act ? '<button type="button"></button>' : '');
  t.querySelector('span').textContent = msg;
  if (act){ var b = t.querySelector('button'); b.textContent = act; b.addEventListener('click', function(){ fn(); t.classList.remove('show'); }); }
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function(){ t.classList.remove('show'); }, act ? 7000 : 4200);
}
function measure(){ document.documentElement.style.setProperty('--tb', $('topbar').offsetHeight + 'px'); }
window.addEventListener('resize', measure);

setView(ui.diff);
updateCounts();
ping().then(function(ok){ if (ok && lsGet('job', null)) pollJob(); });
setInterval(function(){ if (!document.hidden && (bridgeOk || lsGet('job', null))) ping(); }, 20000);
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
