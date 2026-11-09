"""Token-budgeted LLM conversation history compaction and async background memory consolidation."""

from __future__ import annotations

import asyncio
import concurrent.futures
import datetime as dt
import hashlib
import json
import re
from typing import Any, Callable

from .observability import DEFAULT_LOGGER, DEFAULT_TRACER, PIIRedactor, log_event
from .storage import OuraStorage

SummarizerFn = Callable[[str], str]


def estimate_tokens(text: str) -> int:
    """Fast heuristic (~4 chars/token) used for proactive LLM context window budgeting."""
    return max(1, (len(text) + 3) // 4)


class ConversationMemoryManager:
    """Manages session dialogue turns, explicit LLM history compaction, and long-term recall."""

    def __init__(
        self,
        storage: OuraStorage,
        summarizer: SummarizerFn | None = None,
        max_context_tokens: int = 1800,
        keep_recent_turns: int = 6,
    ) -> None:
        self.storage = storage
        self.summarizer = summarizer or self._deterministic_compactor
        self.max_context_tokens = max_context_tokens
        self.keep_recent_turns = keep_recent_turns

    def append_turn(self, session_id: str, role: str, content: str) -> int:
        safe_content = PIIRedactor.redact_text(content)
        tokens = estimate_tokens(safe_content)
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        with self.storage._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO conversation_turns (session_id, role, content, token_estimate, compacted, created_at)
                VALUES (?, ?, ?, ?, 0, ?)
                """,
                (session_id, role, safe_content, tokens, now),
            )
            turn_id = int(cur.lastrowid or 0)

        self.maybe_compact(session_id)
        return turn_id

    def maybe_compact(self, session_id: str, force: bool = False) -> dict[str, Any]:
        """Compact older uncompacted turns via LLM summarization when token or turn budget is exceeded."""
        with self.storage._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, role, content, token_estimate
                FROM conversation_turns
                WHERE session_id = ? AND compacted = 0
                ORDER BY id ASC
                """,
                (session_id,),
            ).fetchall()

        total_tokens = sum(int(r["token_estimate"]) for r in rows)
        if not force and (len(rows) <= self.keep_recent_turns and total_tokens <= self.max_context_tokens):
            return {"compacted": False, "active_turns": len(rows), "active_tokens": total_tokens}

        if len(rows) <= 2 and not force:
            return {"compacted": False, "active_turns": len(rows), "active_tokens": total_tokens}

        cutoff = max(1, len(rows) - self.keep_recent_turns) if not force else max(1, len(rows) - 2)
        to_compact = rows[:cutoff]
        if not to_compact:
            return {"compacted": False, "active_turns": len(rows), "active_tokens": total_tokens}

        with DEFAULT_TRACER.start_as_current_span(
            "memory.compact_history",
            {"session.id": session_id, "compacted.turns": len(to_compact)},
        ):
            existing_summary = self.get_compacted_summary(session_id)
            transcript_block = "\n".join(f"{r['role'].upper()}: {r['content']}" for r in to_compact)
            prompt = (
                "Compress the prior conversation summary and evicted dialogue turns into a concise, high-signal "
                "coaching state block preserving: (1) injury/soreness history, (2) schedule/travel constraints, "
                "(3) goal pace/race preferences, and (4) unresolved action items.\n\n"
                f"PRIOR SUMMARY:\n{existing_summary or '(none)'}\n\n"
                f"EVICTED TURNS:\n{transcript_block}"
            )
            new_summary = PIIRedactor.redact_text(self.summarizer(prompt))
            now = dt.datetime.now(dt.timezone.utc).isoformat()
            compact_ids = [int(r["id"]) for r in to_compact]

            with self.storage._connect() as conn:
                placeholders = ",".join("?" for _ in compact_ids)
                conn.execute(
                    f"UPDATE conversation_turns SET compacted = 1 WHERE id IN ({placeholders})",
                    compact_ids,
                )
                prev_row = conn.execute(
                    "SELECT compacted_turn_count FROM compacted_summaries WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                prev_cnt = int(prev_row["compacted_turn_count"]) if prev_row else 0
                conn.execute(
                    """
                    INSERT INTO compacted_summaries (session_id, summary_text, compacted_turn_count, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        summary_text = excluded.summary_text,
                        compacted_turn_count = excluded.compacted_turn_count,
                        updated_at = excluded.updated_at
                    """,
                    (session_id, new_summary, prev_cnt + len(compact_ids), now),
                )

            log_event(
                DEFAULT_LOGGER,
                "memory.history_compacted",
                f"Compacted {len(compact_ids)} turns for session {session_id}",
                session_id=session_id,
                evicted_turns=len(compact_ids),
                summary_tokens=estimate_tokens(new_summary),
            )
            return {
                "compacted": True,
                "evicted_turns": len(compact_ids),
                "summary": new_summary,
            }

    def get_compacted_summary(self, session_id: str) -> str:
        with self.storage._connect() as conn:
            row = conn.execute(
                "SELECT summary_text FROM compacted_summaries WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return str(row["summary_text"]) if row else ""

    def get_long_term_memories(self, limit: int = 15) -> list[dict[str, Any]]:
        with self.storage._connect() as conn:
            rows = conn.execute(
                "SELECT memory_key, category, fact, confidence, updated_at FROM long_term_memories ORDER BY updated_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def build_context_window(self, session_id: str) -> dict[str, Any]:
        """Assemble compacted summary + consolidated long-term athlete facts + active verbatim turns."""
        summary = self.get_compacted_summary(session_id)
        memories = self.get_long_term_memories(limit=12)
        with self.storage._connect() as conn:
            rows = conn.execute(
                """
                SELECT role, content, created_at
                FROM conversation_turns
                WHERE session_id = ? AND compacted = 0
                ORDER BY id ASC
                """,
                (session_id,),
            ).fetchall()
        recent_turns = [dict(r) for r in rows]
        return {
            "session_id": session_id,
            "compacted_summary": summary,
            "long_term_memories": memories,
            "recent_turns": recent_turns,
            "estimated_tokens": estimate_tokens(summary)
            + sum(estimate_tokens(m["fact"]) for m in memories)
            + sum(estimate_tokens(t["content"]) for t in recent_turns),
        }

    @staticmethod
    def _deterministic_compactor(prompt: str) -> str:
        lines = [ln.strip() for ln in prompt.splitlines() if ln.strip()]
        kept: list[str] = []
        for ln in lines:
            low = ln.lower()
            if any(
                kw in low
                for kw in (
                    "calf",
                    "shin",
                    "achilles",
                    "knee",
                    "sore",
                    "pain",
                    "travel",
                    "swap",
                    "friday",
                    "saturday",
                    "sunday",
                    "pace",
                    "goal",
                    "sub-2",
                    "treadmill",
                )
            ):
                kept.append(f"- {ln[:180]}")
        if not kept:
            tail = [ln for ln in lines if ln.startswith(("USER:", "ASSISTANT:"))][-3:]
            kept = [f"- {ln[:160]}" for ln in tail]
        return "Compacted Coaching Context:\n" + "\n".join(kept[:8])


class BackgroundMemoryConsolidator:
    """Asynchronous background worker consolidating episodic session turns into durable semantic athlete facts."""

    def __init__(
        self,
        storage: OuraStorage,
        extractor_fn: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
        max_workers: int = 2,
    ) -> None:
        self.storage = storage
        self.extractor_fn = extractor_fn or self._extract_durable_facts
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="jackie-memory-consolidator",
        )

    def submit_consolidation(self, session_id: str) -> concurrent.futures.Future[dict[str, Any]]:
        """Enqueue a non-blocking background memory consolidation task on the daemon worker pool."""
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        with self.storage._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO memory_consolidation_jobs (session_id, status, extracted_count, created_at)
                VALUES (?, 'QUEUED', 0, ?)
                """,
                (session_id, now),
            )
            job_id = int(cur.lastrowid or 0)

        return self._executor.submit(self._run_job, job_id, session_id)

    async def consolidate_async(self, session_id: str) -> dict[str, Any]:
        """Native asyncio coroutine wrapper awaiting background memory consolidation without blocking the event loop."""
        loop = asyncio.get_running_loop()
        future = self.submit_consolidation(session_id)
        return await loop.run_in_executor(None, future.result)

    def _run_job(self, job_id: int, session_id: str) -> dict[str, Any]:
        with DEFAULT_TRACER.start_as_current_span(
            "memory.background_consolidation",
            {"session.id": session_id, "job.id": job_id},
        ):
            with self.storage._connect() as conn:
                turns = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT role, content FROM conversation_turns WHERE session_id = ? ORDER BY id DESC LIMIT 12",
                        (session_id,),
                    ).fetchall()
                ]

            extracted = self.extractor_fn(list(reversed(turns)))
            now = dt.datetime.now(dt.timezone.utc).isoformat()

            with self.storage._connect() as conn:
                for mem in extracted:
                    fact = PIIRedactor.redact_text(str(mem["fact"]))
                    category = str(mem.get("category") or "preference")
                    key = str(
                        mem.get("memory_key")
                        or f"{category}:{hashlib.sha256(fact.lower().encode('utf-8')).hexdigest()[:12]}"
                    )
                    conf = float(mem.get("confidence") or 0.85)
                    conn.execute(
                        """
                        INSERT INTO long_term_memories (memory_key, category, fact, confidence, source_session, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(memory_key) DO UPDATE SET
                            fact = excluded.fact,
                            confidence = MAX(long_term_memories.confidence, excluded.confidence),
                            source_session = excluded.source_session,
                            updated_at = excluded.updated_at
                        """,
                        (key, category, fact, conf, session_id, now),
                    )
                conn.execute(
                    """
                    UPDATE memory_consolidation_jobs
                    SET status = 'COMPLETED', extracted_count = ?, completed_at = ?
                    WHERE id = ?
                    """,
                    (len(extracted), now, job_id),
                )

            log_event(
                DEFAULT_LOGGER,
                "memory.consolidation_completed",
                f"Consolidated {len(extracted)} durable athlete memories",
                job_id=job_id,
                session_id=session_id,
                extracted_count=len(extracted),
            )
            return {"job_id": job_id, "status": "COMPLETED", "extracted_count": len(extracted), "memories": extracted}

    def shutdown(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait)

    @staticmethod
    def _extract_durable_facts(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        patterns: list[tuple[str, str, re.Pattern[str]]] = [
            (
                "injury_sensitivity",
                "biomechanics",
                re.compile(r"(?i)\b((?:left|right)?\s*(?:calf|achilles|shin|knee|hamstring|hip|plantar)[^.!?\n]{0,80})"),
            ),
            (
                "schedule_constraint",
                "schedule",
                re.compile(r"(?i)\b((?:can't|cannot|traveling|busy|prefer|off-limits)[^.!?\n]{0,90})"),
            ),
            (
                "surface_preference",
                "equipment",
                re.compile(r"(?i)\b((?:treadmill|trail|track|piedmont|hills)[^.!?\n]{0,80})"),
            ),
        ]
        for turn in turns:
            if turn.get("role") != "user":
                continue
            text = str(turn.get("content") or "")
            for prefix, category, regex in patterns:
                match = regex.search(text)
                if match:
                    snippet = match.group(1).strip()
                    digest = hashlib.sha256(snippet.lower().encode("utf-8")).hexdigest()[:10]
                    facts.append(
                        {
                            "memory_key": f"{prefix}:{digest}",
                            "category": category,
                            "fact": f"Athlete reported: {snippet}",
                            "confidence": 0.9,
                        }
                    )
        return facts
