"""
Retrieval of relevant curriculum + Past_Examples from Data_Training.json, and
optional appending of new Q&A pairs for continuous learning.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Dict, List, Tuple

from filelock import FileLock

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
KNOWLEDGE_FILE = os.path.join(CURRENT_DIR, "Json", "Data_Training.json")

# Total retrieved text cap (characters) to stay within model context limits
DEFAULT_MAX_RETRIEVAL_CHARS = 14_000
# How many top-scoring items to consider before char budget stops
_MAX_CHUNKS = 60
# Auto-learn: max stored pairs under Past_Examples (oldest trimmed first)
_MAX_PAST_EXAMPLES = 2_000


def _tokenize_for_match(text: str) -> List[str]:
    if not text:
        return []
    t = text.strip().lower()
    out: List[str] = []
    for m in re.finditer(r"[\u1780-\u17ff0-9]+|[a-z0-9]+", t, re.I):
        s = m.group(0)
        # Keep >=2 tokens, but also keep common single-letter physics symbols.
        # This improves retrieval for curriculum formulas (V, I, R, Q, W, etc.)
        if len(s) >= 2 or s in {"v", "i", "r", "q", "w", "u", "p", "t", "s", "a", "f", "m", "g"}:
            out.append(s)
    if not out and len(t) >= 2:
        out.append(t[:120])
    return out


def _score_chunk(query: str, chunk_text: str) -> int:
    if not query or not chunk_text:
        return 0
    q = query.strip()
    cl = chunk_text.lower()
    score = 0
    if len(q) > 2 and q.lower() in cl:
        score += 40
    toks = _tokenize_for_match(q)
    for tok in toks:
        if tok in cl:
            score += 3
    for tok in toks:
        if len(tok) > 3 and tok in cl:
            score += 2
    return score


def _serialize_lesson(grade_label: str, ch_name: str, lesson: Dict[str, Any]) -> str:
    parts: List[str] = [f"[[ {grade_label} / {ch_name} / {lesson.get('title', 'មេរៀន')} ]]"]
    if lesson.get("introduction"):
        parts.append(lesson["introduction"])
    for sec in lesson.get("sections") or []:
        if sec.get("heading"):
            parts.append("— " + str(sec["heading"]))
        if sec.get("body"):
            parts.append(str(sec["body"]))
    fm = lesson.get("formulas")
    if isinstance(fm, dict) and fm:
        formula_lines = ["រូបមន្តស្តង់ដារ (យកតាម dataset):"]
        for k, v in fm.items():
            formula_lines.append(f"- {k}: {v}")
        parts.append("\n".join(formula_lines))
    de = lesson.get("definitions")
    if isinstance(de, dict) and de:
        def_lines = ["និមិត្តសញ្ញា និងន័យ (យកតាម dataset):"]
        for k, v in de.items():
            def_lines.append(f"- {k} = {v}")
        parts.append("\n".join(def_lines))
    if lesson.get("summary"):
        parts.append("សង្ខេប៖ " + str(lesson["summary"]))
    for ex in lesson.get("examples") or []:
        if isinstance(ex, dict):
            if ex.get("question"):
                parts.append("សំណួរ៖ " + str(ex["question"]))
            if ex.get("answer"):
                parts.append("ចម្លើយ៖ " + str(ex["answer"]))
    return "\n\n".join(parts)


def _iter_dataset_chunks(data: Any) -> List[Tuple[str, str, str]]:
    """Return list of (kind, short_label, text) for scoring."""
    out: List[Tuple[str, str, str]] = []

    if not isinstance(data, dict):
        return out

    pe = data.get("Past_Examples")
    if isinstance(pe, list):
        for i, item in enumerate(pe):
            if not isinstance(item, dict):
                continue
            u = item.get("User_Asked") or item.get("user_asked") or ""
            a = item.get("Perfect_Answer") or item.get("perfect_answer") or ""
            blob = f"Past_Examples[{i}]\nQ: {u}\nA: {a}"
            out.append(("past", f"PE-{i}", blob))

    for gkey, ginfo in data.items():
        if gkey == "Past_Examples" or not isinstance(ginfo, dict):
            continue
        g_label = str(ginfo.get("grade", gkey))
        for ch in ginfo.get("chapters") or []:
            if not isinstance(ch, dict):
                continue
            cname = str(ch.get("chapter_name", ""))
            for les in ch.get("lessons") or []:
                if not isinstance(les, dict):
                    continue
                out.append(
                    (
                        "lesson",
                        f"{g_label}/{cname}/{les.get('title', '')}",
                        _serialize_lesson(g_label, cname, les),
                    )
                )
    return out


def retrieve_relevant_context(
    query: str, max_chars: int = DEFAULT_MAX_RETRIEVAL_CHARS
) -> str:
    if not query or not str(query).strip():
        return ""
    lock_path = KNOWLEDGE_FILE + ".lock"
    with FileLock(lock_path, timeout=5):
        if not os.path.exists(KNOWLEDGE_FILE):
            return ""
        try:
            with open(KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return ""

    # Expand query with curriculum aliases so we retrieve the correct dataset notation.
    # (Example: Khmer "តង់ស្យុង" should strongly retrieve chunks containing V-based formulas.)
    q_raw = str(query)
    q = q_raw
    q_low = q_raw.lower()
    if any(k in q_low for k in ["តង់ស្យុង", "វ៉ុល", "voltage", "potential"]):
        q += " V វ៉ុល voltmeter V = W / Q"
    if any(k in q_low for k in ["អូម", "ohm", "រេស៊ីស្តង់", "ច្បាប់អូម"]):
        q += " ច្បាប់អូម V = R × I I = V / R R = V / I"
    chunks = _iter_dataset_chunks(data)
    scored: List[Tuple[int, str, str, str]] = []
    for kind, label, text in chunks:
        if not text.strip():
            continue
        s = _score_chunk(q, text)
        # Prefer curriculum lessons over Past_Examples to reduce drift.
        if kind == "lesson":
            s += 8
        elif kind == "past":
            s -= 4
        scored.append((s, kind, label, text))

    scored.sort(key=lambda x: -x[0])
    picked = [x for x in scored if x[0] > 0]
    if not picked:
        lessons = [x for x in scored if x[1] == "lesson"][:6]
        pasts = [x for x in scored if x[1] == "past"][:3]
        picked = (lessons + pasts) or scored[:4]

    parts: List[str] = []
    used = 0
    n = 0
    for s, kind, label, text in picked:
        if n >= _MAX_CHUNKS:
            break
        block = f"— ({kind}) {label} —\n{text.strip()}\n"
        if used + len(block) > max_chars:
            if not parts and len(text) > max_chars:
                parts.append(text[: max_chars - 200] + "\n[…ខ្លួនត្រូវបានកាត់…]\n")
            break
        parts.append(block)
        used += len(block)
        n += 1

    if not parts:
        return ""
    return "\n".join(parts)


def _norm_q(s: str) -> str:
    t = re.sub(r"\s+", " ", (s or "").strip().lower())[:200]
    return t


def try_auto_learn_from_chat(user_question: str, bot_answer: str) -> bool:
    if os.environ.get("AUTO_LEARN_FROM_CHATS", "").lower() not in (
        "1",
        "true",
        "yes",
    ):
        return False
    uq = (user_question or "").strip()
    ba = (bot_answer or "").strip()
    if len(uq) < 5 or len(ba) < 20:
        return False

    h = hashlib.sha256(_norm_q(uq).encode("utf-8")).hexdigest()
    lock_path = KNOWLEDGE_FILE + ".lock"
    with FileLock(lock_path, timeout=5):
        data: Dict[str, Any] = {}
        if os.path.exists(KNOWLEDGE_FILE):
            try:
                with open(KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                return False
        if not isinstance(data, dict):
            data = {}
        pe = data.get("Past_Examples")
        if not isinstance(pe, list):
            pe = []
        for item in pe:
            if not isinstance(item, dict):
                continue
            oq = item.get("User_Asked") or item.get("user_asked") or ""
            if hashlib.sha256(_norm_q(str(oq)).encode("utf-8")).hexdigest() == h:
                return False

        pe.append({"User_Asked": uq, "Perfect_Answer": ba})
        if len(pe) > _MAX_PAST_EXAMPLES:
            pe = pe[-_MAX_PAST_EXAMPLES :]
        data["Past_Examples"] = pe

        try:
            with open(KNOWLEDGE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            return False
    return True
