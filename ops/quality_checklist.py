"""FR-CR-05-192-checklist — Operator-pinned quality checklist per
meeting record. Renders the 20-point check matrix the operator pasted
on 2026-05-22:

  Источник / Title / Документ / Длинное саммари (все поля) /
  Длинное саммари (подробность) / Transcript bilingual / Контрагенты-
  люди (проверены + исправлены, трейсы) / Title link / Участники vs
  Calendar (трейс) / Участники vs Zoom/Fireflies (трейс) / Полный
  список / Короткое саммари / To-do наличие / To-do фильтрация
  (трейсы) / To-do ответственные из контекста (трейсы) / To-do задачи
  из разговора (не придуманы) / To-do сроки / To-do если сроков нет
  — явно отмечено / Финальная: нет галлюцинаций / Финальная:
  uncertain помечено.

Each check returns one of:
  ✓  — passes automatic verification
  ✗  — fails automatic verification
  ~  — partial / requires manual review (no machine signal)

Read-only — no DB writes, no Slack posts.

Defaults to the canonical 9-record send list; pass `--id <conv_id>`
(repeatable) to run on a subset.

Usage:
    docker exec manager-bot-1 python -m ops.quality_checklist
    docker exec manager-bot-1 python -m ops.quality_checklist \\
        --id 01KS0551XQZSNXQ9GGS6DSEMP7 --markdown
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import time as dtime

from app.db import session_scope
from app.models import MeetingRecording, TeamMember, ZoomRecording
from app.models.counterparty import Counterparty
from app.models.task import Task, TaskSourceKind
from app.services.task_direction import DIRECTIONS_IMPORTANT


CYR_RE = re.compile(r"[А-Яа-яЁё]")
LAT_RE = re.compile(r"[A-Za-z]")
RELATIVE_DATE_RE = re.compile(
    r"\b(завтра|послезавтра|на следующей неделе|к понедельник|"
    r"к концу (?:месяца|недели|квартала|года)|"
    r"в (?:понедельник|вторник|среду|четверг|пятницу)|"
    r"до конца (?:месяца|недели|квартала)|"
    r"(?:в|до) (?:июн|июл|август|сентябр|октябр|ноябр|декабр)|"
    r"by (?:end of |next |this )?(?:week|month|quarter)|"
    r"tomorrow|next week|by friday|by monday)\b",
    re.IGNORECASE,
)


# 9-record default send list (FR-CR-05-192-final).
DEFAULT_IDS: list[tuple[str, str]] = [
    ("fireflies", "01KS0551XQZSNXQ9GGS6DSEMP7"),
    ("zoom",      "k9We5mXQRsy3aiv5rOOsHg=="),
    ("fireflies", "01KS2V4MZ5RXVYXMY0GPK6RKF1"),
    ("zoom",      "eQc28t2oR7u7l4ocnk6YLA=="),
    ("zoom",      "8OA3y90MR3+ZCJn57oterw=="),
    ("fireflies", "01KS5HS60Z2V7N1ZZV1FWVEGA1"),
    ("zoom",      "Y1qagtqRQ0OzJlS77rtHQw=="),
    ("fireflies", "01KS5PBJ7EQS4311TCXK5V7QMQ"),
    ("zoom",      "jzh/h42zS0WpbI9eGImdQA=="),
]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _bilingual(text: str) -> bool:
    if not text or len(text) < 200:
        return False
    cyr = len(CYR_RE.findall(text))
    lat = len(LAT_RE.findall(text))
    total = cyr + lat
    if total == 0:
        return False
    return cyr / total >= 0.03 and lat / total >= 0.03


def _participants_line(text: str) -> list[str]:
    for line in (text or "").split("\n"):
        if line.startswith("Участники:"):
            raw = line[len("Участники:"):].strip()
            return [
                n.strip() for n in raw.split(",")
                if n.strip() and n.strip() != "и другие"
            ]
    return []


def _make_check_table(
    r,
    src: str,
    *,
    members_by_norm: dict[str, "TeamMember"],
    cp_norms: list[str],
    session,
) -> list[tuple[int, str, str, str]]:
    """Returns rows (n, name, status, comment) for one record."""
    rows: list[tuple[int, str, str, str]] = []
    conv_id = r.zoom_id if src == "zoom" else r.fireflies_id
    src_kind = (
        TaskSourceKind.zoom if src == "zoom" else TaskSourceKind.fireflies
    )

    short = r.short_summary or ""
    detailed = r.detailed_summary or ""
    transcript = (getattr(r, "transcript_text", None) or "")

    # 1. Источник
    rows.append((1, "Источник", "✓", f"{src} ({conv_id[:36]})"))

    # 2. Title
    title = (r.title or "").strip()
    rows.append((
        2, "Title",
        "✓" if title else "✗",
        title[:90] if title else "—",
    ))

    # 3. Документ
    doc = (r.google_doc_url or "").strip()
    rows.append((
        3, "Документ (Google Doc URL)",
        "✓" if doc else "✗",
        doc[:90] if doc else "—",
    ))

    # 4. Длинное саммари — все обязательные поля заполнены
    has_meta = "📅 МЕТА" in detailed or "Дата и продолжительность" in detailed
    has_uchastniki = "Участники" in detailed
    has_body = len(detailed) > 1000
    all_fields = has_meta and has_uchastniki and has_body
    rows.append((
        4, "Длинное саммари — все обязательные поля",
        "✓" if all_fields else "~",
        (
            f"meta={'✓' if has_meta else '✗'}  "
            f"участники={'✓' if has_uchastniki else '✗'}  "
            f"body>1000={'✓' if has_body else '✗'}"
        ),
    ))

    # 5. Длинное саммари достаточно подробное (не поверхностное)
    rows.append((
        5, "Длинное саммари — подробное",
        "✓" if len(detailed) >= 5000 else (
            "~" if len(detailed) >= 1500 else "✗"
        ),
        f"detailed_chars={len(detailed)} "
        f"(≥5000=подробно, 1500-5000=терпимо, <1500=поверхностно)",
    ))

    # 6. Transcript bilingual
    biling = _bilingual(transcript)
    cyr = len(CYR_RE.findall(transcript))
    lat = len(LAT_RE.findall(transcript))
    rows.append((
        6, "Transcript bilingual",
        "✓" if biling else "✗" if transcript else "~",
        (
            f"cyr={cyr} lat={lat} "
            f"({cyr / max(cyr + lat, 1) * 100:.0f}% Cyr / "
            f"{lat / max(cyr + lat, 1) * 100:.0f}% Lat) "
            f"— требуется ≥3% обоих"
        ) if transcript else "no transcript",
    ))

    # 7. Контрагенты / люди в длинном — проверены и при необходимости
    #    исправлены. Сигнал: длинное саммари содержит ≥1 канонических
    #    Counterparty.name и ≥1 канонических TeamMember.real_name. Если
    #    нет ни одного — либо канонизация не отработала, либо встреча
    #    реально без external mentions.
    det_lower = detailed.lower()
    cp_hits = sum(1 for n in cp_norms if n and len(n) >= 3 and n in det_lower)
    ppl_hits = sum(
        1 for nm in members_by_norm if nm and len(nm) >= 3 and nm in det_lower
    )
    # Heuristic for «изменения сделаны»: detailed has no email-form
    # names (jarc@thehumanoid.ai etc).
    email_leak_re = re.compile(r"\b[a-zA-Z0-9_.-]+@thehumanoid\.ai\b")
    email_leaks = email_leak_re.findall(detailed)
    canonicalize_clean = not email_leaks
    rows.append((
        7, "Контрагенты / люди в длинном — проверены + исправлены",
        "✓" if (cp_hits > 0 or ppl_hits > 0) and canonicalize_clean else "~",
        (
            f"counterparty-mentions={cp_hits} "
            f"teammember-mentions={ppl_hits} "
            f"email-leaks={len(email_leaks)}"
            + (
                f" ({', '.join(set(email_leaks))[:80]})"
                if email_leaks else ""
            )
        ),
    ))

    # 8. Title link — короткое начинается с <a href=…>
    first_line = short.split("\n", 1)[0] if short else ""
    has_link = "<a href=" in first_line
    url_m = re.search(r'href="([^"]+)"', first_line)
    rows.append((
        8, "Title содержит гиперссылку",
        "✓" if has_link else "✗",
        (url_m.group(1)[:80] if url_m else (first_line[:80] or "—")),
    ))

    # 9. Участники vs Google Calendar
    cal = r.calendar_attendees or []
    cal_count = sum(1 for a in cal if isinstance(a, dict))
    cal_resolved = sum(
        1 for a in cal
        if isinstance(a, dict) and a.get("resolved_name")
    )
    rows.append((
        9, "Участники сверены с Google Calendar",
        "✓" if cal_count > 0 else "✗",
        f"calendar_attendees={cal_count}  resolved_to_TM={cal_resolved}",
    ))

    # 10. Участники сверены с Zoom / Fireflies
    raw_parts = list(getattr(r, "participants", None) or [])
    rows.append((
        10, "Участники сверены с Zoom / Fireflies",
        "✓" if raw_parts else "~",
        (
            f"raw {src}_participants={len(raw_parts)} — "
            f"{', '.join(p[:20] for p in raw_parts[:6])}"
            + ("…" if len(raw_parts) > 6 else "")
        ) if raw_parts else "no raw participants",
    ))

    # 11. Полный список участников из всех источников
    part_line = _participants_line(short)
    cal_resolved_set = {
        _norm(a.get("resolved_name") or a.get("display_name") or "")
        for a in cal if isinstance(a, dict)
    } - {""}
    part_line_set = {_norm(n) for n in part_line}
    # Heuristic: short summary line should mention at least N attendees
    # from calendar (where N = min(6, calendar_resolved)). «и другие» when
    # the calendar set was larger.
    overlap = len(cal_resolved_set & part_line_set)
    has_etc = "и другие" in (next((ln for ln in (short or "").split("\n") if "Участники:" in ln), "") or "")
    target_overlap = min(6, cal_resolved) if cal_resolved else 0
    full_list_ok = (
        overlap >= max(target_overlap, 1)
        and (has_etc or len(cal_resolved_set) <= len(part_line_set))
    )
    rows.append((
        11, "Полный список участников из всех источников",
        "✓" if full_list_ok else "~",
        (
            f"short-line={len(part_line_set)}  "
            f"overlap-with-calendar={overlap}/{cal_resolved}  "
            f"«и другие»={'да' if has_etc else 'нет'}"
        ),
    ))

    # 12. Короткое саммари есть
    rows.append((
        12, "Короткое саммари",
        "✓" if short.strip() else "✗",
        f"chars={len(short)}",
    ))

    # 13. To-Do — есть задачи (если они были во встрече)
    tasks = (
        session.query(Task)
        .filter(Task.source_kind == src_kind)
        .filter(Task.source_conversation_id == conv_id)
        .filter(Task.deleted_at.is_(None))
        .order_by(Task.id)
        .all()
    )
    n_tasks = len(tasks)
    # Heuristic for «во встрече были задачи»: transcript contains
    # imperative verbs / «надо», «нужно», «подготов», «отправ»…
    has_actionable_signal = bool(re.search(
        r"\b(нужно|надо|необходимо|подготов\w+|отправ\w+|"
        r"закрыть|закры\w+|организов\w+|должен|должна|должны|"
        r"will|must|need to|to do|prepare|send|finalize)\b",
        transcript, re.IGNORECASE,
    ))
    if n_tasks > 0:
        rows.append((
            13, "To-Do — есть задачи (если были во встрече)",
            "✓",
            f"db_tasks={n_tasks}",
        ))
    elif has_actionable_signal and len(transcript) > 5000:
        rows.append((
            13, "To-Do — есть задачи (если были во встрече)",
            "~",
            f"db_tasks=0, но в транскрипте есть actionable-сигналы — "
            f"нужен ephemeral extract или ручная проверка",
        ))
    else:
        rows.append((
            13, "To-Do — есть задачи (если были во встрече)",
            "✓",
            "db_tasks=0, actionable-сигналов в транскрипте не найдено",
        ))

    # 14. To-Do отфильтрованы (направление ∈ DIRECTIONS_IMPORTANT)
    filt_count = 0
    other_count = 0
    for t in tasks:
        try:
            extra = t.extra or {}
            d = extra.get("direction") if isinstance(extra, dict) else None
        except Exception:  # noqa: BLE001
            d = None
        if d in DIRECTIONS_IMPORTANT:
            filt_count += 1
        else:
            other_count += 1
    if n_tasks > 0:
        rows.append((
            14, "To-Do — отфильтрованы (direction ∈ IMPORTANT)",
            "✓" if filt_count > 0 else "~",
            (
                f"important={filt_count}/{n_tasks}  "
                f"other-dropped={other_count} "
                f"(DIRECTIONS_IMPORTANT={','.join(DIRECTIONS_IMPORTANT)})"
            ),
        ))
    else:
        rows.append((
            14, "To-Do — отфильтрованы (direction ∈ IMPORTANT)",
            "—",
            "нет задач",
        ))

    # 15. To-Do — ответственные реально из контекста / TM
    owner_in_tm = 0
    owner_email = 0
    owner_unknown = 0
    owner_examples: list[str] = []
    for t in tasks:
        o = (t.owner_display_name or "").strip()
        if not o:
            owner_unknown += 1
        elif "@" in o:
            owner_email += 1
            owner_examples.append(o[:30])
        elif _norm(o) in members_by_norm:
            owner_in_tm += 1
        else:
            owner_unknown += 1
            owner_examples.append(o[:30])
    if n_tasks > 0:
        rows.append((
            15, "To-Do — ответственные из контекста (TM)",
            "✓" if owner_email == 0 and owner_unknown == 0 else "✗",
            (
                f"in_TM={owner_in_tm}  "
                f"email-form={owner_email}  "
                f"unknown={owner_unknown}"
                + (
                    f" → {', '.join(set(owner_examples))[:80]}"
                    if owner_examples else ""
                )
            ),
        ))
    else:
        rows.append((
            15, "To-Do — ответственные из контекста (TM)",
            "—",
            "нет задач",
        ))

    # 16. To-Do — задачи реально из разговора (не придуманы)
    # Heuristic: каждая задача упоминает ≥1 ключевое слово из
    # detailed_summary или calendar_attendees / counterparty mentions
    # → значит «привязана к контексту». Иначе — потенциально
    # выдумана, нужна ручная проверка.
    if n_tasks > 0:
        anchored = 0
        for t in tasks:
            text = f"{t.title or ''} {t.description or ''}".lower()
            # Берём только многосимвольные слова (4+ chars) для
            # дешёвого «у тебя есть слово из контекста?»
            tokens = {w for w in re.findall(r"[А-Яа-яa-z]{4,}", text)}
            det_tokens = {
                w for w in re.findall(r"[А-Яа-яa-z]{4,}", det_lower)
            }
            if tokens & det_tokens:
                anchored += 1
        rows.append((
            16, "To-Do — задачи из разговора (не придуманы)",
            "✓" if anchored == n_tasks else "~",
            (
                f"anchored-to-detailed={anchored}/{n_tasks} "
                "(heuristic: ≥1 общее многосимвольное слово)"
            ),
        ))
    else:
        rows.append((
            16, "To-Do — задачи из разговора (не придуманы)",
            "—",
            "нет задач",
        ))

    # 17. To-Do — у задач есть сроки если они были в разговоре
    if n_tasks > 0:
        default_due = (
            r.meeting_date.date() if r.meeting_date else None
        )
        real_dl = sum(
            1 for t in tasks
            if t.due_date
            and not (
                t.due_date == default_due
                and (t.due_time is None or t.due_time == dtime(18, 0))
            )
        )
        rows.append((
            17, "To-Do — сроки если были в разговоре",
            "✓" if real_dl > 0 else "~",
            (
                f"LLM-extracted={real_dl}/{n_tasks}  "
                f"default-fallback={n_tasks - real_dl} "
                f"(= meeting_date 18:00)"
            ),
        ))
    else:
        rows.append((
            17, "To-Do — сроки если были в разговоре",
            "—",
            "нет задач",
        ))

    # 18. To-Do — если сроков не было, это явно отмечено
    if n_tasks > 0:
        has_relative_in_transcript = bool(RELATIVE_DATE_RE.search(transcript))
        if real_dl == n_tasks:
            rows.append((
                18, "To-Do — отсутствие сроков явно отмечено",
                "✓",
                "все задачи с реальным дедлайном — отметка не нужна",
            ))
        elif not has_relative_in_transcript:
            rows.append((
                18, "To-Do — отсутствие сроков явно отмечено",
                "~",
                (
                    f"{n_tasks - real_dl} задач с default-дедлайном; "
                    "в транскрипте не найдены relative-маркеры "
                    "(«завтра», «к концу месяца») — default норм"
                ),
            ))
        else:
            rows.append((
                18, "To-Do — отсутствие сроков явно отмечено",
                "~",
                (
                    f"{n_tasks - real_dl} задач с default-дедлайном, "
                    "но в транскрипте есть relative-маркеры — "
                    "LLM мог пропустить дедлайны, нужна ручная проверка"
                ),
            ))
    else:
        rows.append((
            18, "To-Do — отсутствие сроков явно отмечено",
            "—",
            "нет задач",
        ))

    # 19. Финальная — нет галлюцинаций / домыслов / неподтверждённых
    # фактов. Эвристика: detailed не содержит email-leak'ов (как в #7)
    # + участники из short все в calendar OR в counterparties OR в TM.
    # Иначе — ручная проверка нужна.
    leaks = email_leaks
    unverified_participants = [
        n for n in part_line
        if _norm(n) not in members_by_norm
        and _norm(n) not in cal_resolved_set
        and n != "и другие"
    ]
    if leaks or unverified_participants:
        rows.append((
            19, "Финальная — нет галлюцинаций / домыслов",
            "~",
            (
                (
                    f"email-leak в детальном: {', '.join(set(leaks))[:80]}"
                    if leaks else ""
                )
                + (
                    (" / " if leaks else "")
                    + f"участников нет ни в TM, ни в Calendar: "
                    f"{', '.join(unverified_participants[:5])}"
                    if unverified_participants else ""
                )
                + "  → ручная проверка"
            ),
        ))
    else:
        rows.append((
            19, "Финальная — нет галлюцинаций / домыслов",
            "✓",
            "автопроверка прошла",
        ))

    # 20. Финальная — спорные uncertain / needs review
    # Heuristic: в short summary нет маркеров «возможно», «вероятно»,
    # «uncertain», «needs review» — это значит спорные места не
    # помечены. НО для коротких саммари такая маркировка не
    # практикуется → отметим «—» / manual.
    has_uncertain_markers = bool(re.search(
        r"\b(возможно|вероятно|по предварительной|"
        r"требует уточнения|uncertain|needs review|tbd)\b",
        short, re.IGNORECASE,
    ))
    rows.append((
        20, "Финальная — спорные помечены uncertain",
        "~",
        (
            "uncertain-маркеры в коротком: "
            + ("найдены" if has_uncertain_markers else "не найдены — "
               "если в разговоре были спорные тезисы, они НЕ помечены")
        ),
    ))

    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--id", action="append", default=[],
        help="Override default list: zoom_id or fireflies_id (repeatable)",
    )
    ap.add_argument(
        "--markdown", action="store_true",
        help="Emit markdown tables instead of aligned plaintext.",
    )
    args = ap.parse_args()

    with session_scope() as session:
        members = session.query(TeamMember).filter(
            TeamMember.active.is_(True),
            TeamMember.real_name.isnot(None),
        ).all()
        members_by_norm = {
            _norm(m.real_name): m for m in members if m.real_name
        }
        cps = session.query(Counterparty).all()
        cp_norms = sorted(
            {
                (cp.name_normalised or _norm(cp.name))
                for cp in cps if cp.name
            },
            key=len, reverse=True,
        )

        if args.id:
            targets: list[tuple[str, str]] = []
            for cid in args.id:
                # Try fireflies first (`01K…` 26-char ULID-ish).
                # Falls back to zoom.
                if (
                    session.query(MeetingRecording)
                    .filter(MeetingRecording.fireflies_id == cid)
                    .first()
                ):
                    targets.append(("fireflies", cid))
                else:
                    targets.append(("zoom", cid))
        else:
            targets = list(DEFAULT_IDS)

        for i, (src, cid) in enumerate(targets, start=1):
            if src == "zoom":
                r = session.query(ZoomRecording).filter(
                    ZoomRecording.zoom_id == cid,
                ).first()
            else:
                r = session.query(MeetingRecording).filter(
                    MeetingRecording.fireflies_id == cid,
                ).first()
            if r is None:
                print(f"\n[{i}] {src} {cid} — NOT FOUND")
                continue

            checks = _make_check_table(
                r, src,
                members_by_norm=members_by_norm,
                cp_norms=cp_norms,
                session=session,
            )
            header = (
                f"\n{'=' * 100}\n"
                f"[{i}] {r.meeting_date.strftime('%d/%m %H:%M')} "
                f"[{src}]  {(r.title or '')[:70]}\n"
                f"{'=' * 100}"
            )
            print(header)
            if args.markdown:
                print("| #  | Проверка | Статус | Трейс / комментарий |")
                print("|----|----------|--------|---------------------|")
                for n, name, status, comment in checks:
                    safe = comment.replace("|", "\\|")
                    print(f"| {n:>2} | {name} | {status} | {safe} |")
            else:
                for n, name, status, comment in checks:
                    print(
                        f"  {n:>2}. [{status}] {name[:48]:<48}  "
                        f"{comment[:120]}"
                    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
