"""Multi-Agent Supervisor Orchestrator, Gemini 3 Model Router, Safety Guardrails, and HITL Gate."""

from __future__ import annotations

import copy
import datetime as dt
import json
import re
import secrets
import time
import urllib.request
from dataclasses import dataclass
from typing import Any

from .coach import (
    PACE_ZONES,
    RACE_DATE,
    RACE_NAME,
    auto_adapt_plan,
    ensure_plan_initialized,
    evaluate_readiness,
    get_coach_overview,
    manual_swap_days,
)
from .config import ConfigStore
from .memory import BackgroundMemoryConsolidator, ConversationMemoryManager
from .observability import (
    DEFAULT_INTENT_TRACKER,
    DEFAULT_LOGGER,
    DEFAULT_TRACER,
    PIIRedactor,
    log_event,
)
from .storage import OuraStorage

# Current Gemini 3 model tiers (per official Gemini Interactions API guidance)
MODEL_FAST_LITE = "gemini-3.5-flash-lite"
MODEL_BALANCED_AGENT = "gemini-3.8-flash"
MODEL_DEEP_REASONING = "gemini-3.1-pro-preview"

try:
    from google import genai  # type: ignore[import-untyped]

    HAS_GOOGLE_GENAI_SDK = True
except Exception:  # pragma: no cover
    genai = None  # type: ignore[assignment]
    HAS_GOOGLE_GENAI_SDK = False

_INJECTION_PATTERNS = [
    re.compile(r"(?i)ignore\s+(?:all\s+)?(?:previous|prior|system)\s+instructions"),
    re.compile(r"(?i)reveal\s+(?:your\s+)?(?:system\s+prompt|api\s+key|client\s+secret|access\s+token)"),
    re.compile(r"(?i)you\s+are\s+now\s+(?:dan|unrestricted|jailbroken)"),
    re.compile(r"(?i)bypass\s+(?:all\s+)?(?:safety|guardrails|medical)"),
]

_CLINICAL_RED_FLAG_PATTERNS = [
    re.compile(r"(?i)\b(?:chest\s+pain|chest\s+tightness|heart\s+palpitations|irregular\s+heartbeat)\b"),
    re.compile(r"(?i)\b(?:fainted|fainting|syncope|blacked\s+out|dizzy\s+while\s+running)\b"),
    re.compile(r"(?i)\b(?:stress\s+fracture|sharp\s+bone\s+pain|can't\s+bear\s+weight|cannot\s+walk)\b"),
]


@dataclass
class GuardrailVerdict:
    passed: bool
    category: str
    reason: str
    safe_override_response: str = ""


class SafetyGuardrails:
    """Deterministic input and output safety guardrails enforcing security, clinical, and physiological invariants."""

    @staticmethod
    def check_input(user_prompt: str) -> GuardrailVerdict:
        for pat in _INJECTION_PATTERNS:
            if pat.search(user_prompt):
                return GuardrailVerdict(
                    passed=False,
                    category="PROMPT_INJECTION_BLOCKED",
                    reason="Detected adversarial instruction-override or secret-exfiltration attempt.",
                    safe_override_response=(
                        "I can only assist with physiological recovery analysis and training plan adjustments "
                        "for your half-marathon build. Internal prompts and credentials cannot be disclosed."
                    ),
                )

        for pat in _CLINICAL_RED_FLAG_PATTERNS:
            match = pat.search(user_prompt)
            if match:
                symptom = match.group(0)
                return GuardrailVerdict(
                    passed=False,
                    category="CLINICAL_MEDICAL_ESCALATION",
                    reason=f"Detected acute clinical symptom ('{symptom}').",
                    safe_override_response=(
                        f"Pause all running immediately. Because you mentioned '{symptom}', I cannot prescribe "
                        "a training workout. Please seek evaluation from a qualified physician or sports-medicine "
                        "clinician before resuming exercise."
                    ),
                )

        return GuardrailVerdict(passed=True, category="SAFE", reason="Input passed security and clinical checks.")

    @staticmethod
    def verify_plan_invariants(
        baseline_week_days: list[dict[str, Any]],
        candidate_week_days: list[dict[str, Any]],
        readiness_signal: str,
    ) -> GuardrailVerdict:
        """Enforce hard exercise-physiology invariants before staging or committing any schedule mutation."""
        base_miles = sum(float(d.get("planned_miles") or 0.0) for d in baseline_week_days)
        cand_miles = sum(float(d.get("planned_miles") or 0.0) for d in candidate_week_days)

        if base_miles > 0 and cand_miles > base_miles * 1.15 + 0.01:
            return GuardrailVerdict(
                passed=False,
                category="MILEAGE_CAP_VIOLATION",
                reason=(
                    f"Proposed weekly mileage ({cand_miles:.1f} mi) exceeds the 15% tendon-safety ceiling "
                    f"over baseline ({base_miles:.1f} mi)."
                ),
            )

        # Verify >= 48h (>= 2 calendar days) spacing between Quality / Long Run sessions
        quality_dates = sorted(
            dt.date.fromisoformat(str(d["day"]))
            for d in candidate_week_days
            if d.get("is_quality") and float(d.get("planned_miles") or 0.0) > 0
        )
        for idx in range(1, len(quality_dates)):
            gap_days = (quality_dates[idx] - quality_dates[idx - 1]).days
            if gap_days < 2:
                return GuardrailVerdict(
                    passed=False,
                    category="QUALITY_SPACING_VIOLATION",
                    reason=(
                        f"Back-to-back quality sessions detected on {quality_dates[idx - 1]} and "
                        f"{quality_dates[idx]} ({gap_days * 24}h apart; minimum 48h buffer required)."
                    ),
                )

        if readiness_signal == "RED":
            today_iso = dt.date.today().isoformat()
            for d in candidate_week_days:
                if d["day"] == today_iso and d.get("is_quality"):
                    return GuardrailVerdict(
                        passed=False,
                        category="RED_SIGNAL_INTENSITY_VIOLATION",
                        reason="Cannot schedule a high-intensity quality workout on a RED recovery day.",
                    )

        return GuardrailVerdict(passed=True, category="PHYSIOLOGY_VERIFIED", reason="All training invariants satisfied.")


class ModelRouter:
    """Routes each sub-agent step to the optimal Gemini 3 model tier with automatic fallback chains."""

    ROUTING_TABLE: dict[str, tuple[str, list[str]]] = {
        "triage": (MODEL_FAST_LITE, [MODEL_BALANCED_AGENT]),
        "compaction": (MODEL_FAST_LITE, [MODEL_BALANCED_AGENT]),
        "safety_critic": (MODEL_FAST_LITE, [MODEL_BALANCED_AGENT]),
        "physiology_analyst": (MODEL_BALANCED_AGENT, [MODEL_FAST_LITE]),
        "standard_plan": (MODEL_BALANCED_AGENT, [MODEL_DEEP_REASONING, MODEL_FAST_LITE]),
        "complex_replan": (MODEL_DEEP_REASONING, [MODEL_BALANCED_AGENT, MODEL_FAST_LITE]),
    }

    def select_model(self, task_type: str, complexity_score: float = 0.3) -> tuple[str, list[str]]:
        if complexity_score >= 0.75 or task_type in ("INJURY_REPORT", "MULTI_WEEK_REPLAN"):
            primary, fallbacks = self.ROUTING_TABLE["complex_replan"]
        else:
            primary, fallbacks = self.ROUTING_TABLE.get(task_type, self.ROUTING_TABLE["standard_plan"])

        log_event(
            DEFAULT_LOGGER,
            "orchestrator.model_routed",
            f"Routed {task_type} (complexity={complexity_score:.2f}) -> {primary}",
            task_type=task_type,
            complexity_score=round(complexity_score, 2),
            selected_model=primary,
            fallback_chain=fallbacks,
        )
        return primary, fallbacks


class GeminiInteractionsGateway:
    """Executes Gemini 3 Interactions API requests (`client.interactions.create`) with REST and offline fallbacks."""

    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key.strip()
        self._sdk_client = genai.Client(api_key=self.api_key) if (self.api_key and HAS_GOOGLE_GENAI_SDK) else None

    @property
    def is_live_llm_enabled(self) -> bool:
        return bool(self.api_key)

    def generate_json(
        self,
        model: str,
        system_instruction: str,
        prompt: str,
        schema: dict[str, Any],
        fallbacks: list[str] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """Invoke Gemini Interactions API with structured JSON output (`response_format`), falling back cleanly."""
        if not self.api_key:
            return None, f"offline-policy({model})"

        candidates = [model, *(fallbacks or [])]
        for candidate_model in candidates:
            with DEFAULT_TRACER.start_as_current_span(
                "gen_ai.interactions.create",
                {
                    "gen_ai.system": "gemini",
                    "gen_ai.request.model": candidate_model,
                    "gen_ai.operation.name": "interactions.create",
                },
            ) as span:
                try:
                    response_format = {
                        "type": "text",
                        "mime_type": "application/json",
                        "schema": schema,
                    }
                    if self._sdk_client is not None:
                        interaction = self._sdk_client.interactions.create(
                            model=candidate_model,
                            system_instruction=system_instruction,
                            input=prompt,
                            response_format=response_format,
                        )
                        raw_text = str(interaction.output_text or "{}")
                    else:
                        raw_text = self._call_interactions_rest(
                            model=candidate_model,
                            system_instruction=system_instruction,
                            prompt=prompt,
                            response_format=response_format,
                        )
                    parsed = json.loads(raw_text)
                    span.set_attribute("gen_ai.response.status", "completed")
                    return parsed, candidate_model
                except Exception as exc:
                    span.record_exception(exc)
                    log_event(
                        DEFAULT_LOGGER,
                        "orchestrator.llm_fallback",
                        f"Model {candidate_model} failed ({type(exc).__name__}), trying next fallback",
                        failed_model=candidate_model,
                    )
        return None, f"offline-fallback({model})"

    def _call_interactions_rest(
        self,
        model: str,
        system_instruction: str,
        prompt: str,
        response_format: dict[str, Any],
    ) -> str:
        req_body = json.dumps(
            {
                "model": model,
                "system_instruction": system_instruction,
                "input": prompt,
                "response_format": response_format,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/interactions",
            data=req_body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self.api_key,
                "Api-Revision": "2026-05-20",
            },
        )
        with urllib.request.urlopen(req, timeout=25.0) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        for step in reversed(payload.get("steps") or []):
            if step.get("type") == "model_output":
                for part in step.get("content") or []:
                    if part.get("type") == "text" and part.get("text"):
                        return str(part["text"])
        return "{}"


class HITLApprovalGate:
    """Explicit Human-in-the-Loop approval manager gating high-impact training plan mutations."""

    def __init__(self, storage: OuraStorage) -> None:
        self.storage = storage

    def requires_approval(
        self,
        action_type: str,
        previous_days: list[dict[str, Any]],
        proposed_days: list[dict[str, Any]],
        readiness_signal: str = "GREEN",
    ) -> tuple[bool, str]:
        if len(proposed_days) >= 2:
            return True, f"Multi-day schedule reshuffle affecting {len(proposed_days)} calendar days."
        for prev, prop in zip(previous_days, proposed_days):
            if prev.get("workout_type") == "long_run" or prop.get("workout_type") == "long_run":
                return True, "Modifies your anchored weekend Long Run."
            p_mi = float(prev.get("planned_miles") or 0.0)
            n_mi = float(prop.get("planned_miles") or 0.0)
            if p_mi > 0 and abs(n_mi - p_mi) / p_mi > 0.15:
                return True, f"Changes workout volume by >15% ({p_mi:g} mi -> {n_mi:g} mi)."
        if action_type == "OVERRIDE_RED_RECOVERY" or (readiness_signal == "RED" and any(float(d.get("planned_miles") or 0) > 2.0 for d in proposed_days)):
            return True, "Overrides a RED physiological recovery recommendation."
        return False, "Low-risk read or minor adjustment within autonomous envelope."

    def create_pending_approval(
        self,
        session_id: str,
        action_type: str,
        summary: str,
        rationale: str,
        previous_days: list[dict[str, Any]],
        proposed_days: list[dict[str, Any]],
        risk_level: str = "HIGH",
    ) -> dict[str, Any]:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        with self.storage._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO hitl_approvals (
                    session_id, created_at, status, action_type, summary,
                    rationale, risk_level, proposed_days_json, previous_days_json
                ) VALUES (?, ?, 'PENDING', ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    now,
                    action_type,
                    PIIRedactor.redact_text(summary),
                    PIIRedactor.redact_text(rationale),
                    risk_level,
                    json.dumps(proposed_days, sort_keys=True),
                    json.dumps(previous_days, sort_keys=True),
                ),
            )
            approval_id = int(cur.lastrowid or 0)

        log_event(
            DEFAULT_LOGGER,
            "hitl.approval_requested",
            f"Staged HITL approval #{approval_id}: {summary}",
            approval_id=approval_id,
            action_type=action_type,
            risk_level=risk_level,
        )
        return {
            "approval_id": approval_id,
            "status": "PENDING",
            "action_type": action_type,
            "summary": summary,
            "rationale": rationale,
            "affected_dates": [d["day"] for d in proposed_days],
        }

    def list_approvals(self, status: str | None = "PENDING", limit: int = 20) -> list[dict[str, Any]]:
        with self.storage._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM hitl_approvals WHERE status = ? ORDER BY id DESC LIMIT ?",
                    (status, int(limit)),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM hitl_approvals ORDER BY id DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            item["proposed_days"] = json.loads(item.pop("proposed_days_json") or "[]")
            item["previous_days"] = json.loads(item.pop("previous_days_json") or "[]")
            out.append(item)
        return out

    def resolve_approval(self, approval_id: int, approved: bool, note: str = "") -> dict[str, Any]:
        with self.storage._connect() as conn:
            row = conn.execute("SELECT * FROM hitl_approvals WHERE id = ?", (int(approval_id),)).fetchone()
        if not row:
            raise ValueError(f"HITL approval request #{approval_id} not found.")
        if row["status"] != "PENDING":
            raise ValueError(f"HITL approval request #{approval_id} was already resolved ({row['status']}).")

        proposed_days: list[dict[str, Any]] = json.loads(row["proposed_days_json"])
        previous_days: list[dict[str, Any]] = json.loads(row["previous_days_json"])
        new_status = "APPROVED" if approved else "REJECTED"
        resolved_at = dt.datetime.now(dt.timezone.utc).isoformat()

        adaptation_id: int | None = None
        if approved:
            self.storage.upsert_plan_days(proposed_days, overwrite_existing=True)
            adaptation_id = self.storage.record_adaptation(
                target_day=proposed_days[0]["day"] if proposed_days else dt.date.today().isoformat(),
                signal_level="HITL_APPROVED",
                reason=f"Approved HITL #{approval_id}: {row['summary']}",
                metrics={},
                previous_days=previous_days,
                updated_days=proposed_days,
            )

        with self.storage._connect() as conn:
            conn.execute(
                """
                UPDATE hitl_approvals
                SET status = ?, resolved_at = ?, resolver_note = ?
                WHERE id = ?
                """,
                (new_status, resolved_at, PIIRedactor.redact_text(note), int(approval_id)),
            )

        log_event(
            DEFAULT_LOGGER,
            "hitl.approval_resolved",
            f"Resolved HITL #{approval_id} -> {new_status}",
            approval_id=approval_id,
            status=new_status,
            adaptation_id=adaptation_id,
        )
        return {
            "approval_id": int(approval_id),
            "status": new_status,
            "adaptation_id": adaptation_id,
            "affected_dates": [d["day"] for d in proposed_days],
        }


class MultiAgentCoachOrchestrator:
    """4-Agent Supervisor Pipeline coordinating Triage, Physiology Analyst, Training Architect, and Safety Critic."""

    def __init__(self, store: ConfigStore | None = None) -> None:
        self.store = store or ConfigStore()
        self.storage = OuraStorage(self.store.db_path)
        ensure_plan_initialized(self.storage)

        self.router = ModelRouter()
        self.llm = GeminiInteractionsGateway(api_key=self.store.load_gemini_api_key())
        self.memory = ConversationMemoryManager(self.storage, summarizer=self._summarize_with_lite_model)
        self.consolidator = BackgroundMemoryConsolidator(self.storage)
        self.hitl = HITLApprovalGate(self.storage)

    def handle_request(
        self,
        user_prompt: str,
        session_id: str = "default",
        reference_date: str | None = None,
        require_hitl_for_mutations: bool = True,
    ) -> dict[str, Any]:
        start_perf = time.perf_counter()
        correlation_id = f"turn-{secrets.token_hex(6)}"
        today_str = reference_date or dt.date.today().isoformat()

        with DEFAULT_TRACER.start_as_current_span(
            "orchestrator.handle_request",
            {"session.id": session_id, "correlation.id": correlation_id, "target.date": today_str},
        ) as root_span:
            # Step 0: Input Guardrail Check
            guard_in = SafetyGuardrails.check_input(user_prompt)
            root_span.set_attribute("guardrail.input.category", guard_in.category)
            if not guard_in.passed:
                DEFAULT_INTENT_TRACKER.record_intent(
                    correlation_id=correlation_id,
                    declared_intent=guard_in.category,
                    selected_agent="InputSafetyGuardrail",
                    routed_model="deterministic-guardrail",
                )
                latency = (time.perf_counter() - start_perf) * 1000.0
                DEFAULT_INTENT_TRACKER.record_outcome(
                    correlation_id=correlation_id,
                    declared_intent=guard_in.category,
                    actual_outcome=guard_in.reason,
                    status="BLOCKED_BY_GUARDRAIL",
                    guardrail_passed=False,
                    latency_ms=latency,
                )
                return {
                    "correlation_id": correlation_id,
                    "trace_id": root_span.trace_id,
                    "status": "GUARDRAIL_INTERCEPTED",
                    "guardrail": {"passed": False, "category": guard_in.category, "reason": guard_in.reason},
                    "intent": guard_in.category,
                    "routed_models": {},
                    "response": guard_in.safe_override_response,
                    "pending_approval": None,
                }

            self.memory.append_turn(session_id, "user", user_prompt)
            ctx_window = self.memory.build_context_window(session_id)

            # Agent 1: Triage & Intent Router (gemini-3.5-flash-lite)
            triage = self._run_triage_agent(user_prompt, today_str)
            intent = str(triage["intent"])
            complexity = float(triage["complexity"])

            architect_model, architect_fallbacks = self.router.select_model(intent, complexity)
            DEFAULT_INTENT_TRACKER.record_intent(
                correlation_id=correlation_id,
                declared_intent=intent,
                selected_agent="Supervisor->[Triage,PhysiologyAnalyst,TrainingArchitect,SafetyCritic]",
                routed_model=architect_model,
                planned_tools=triage["planned_tools"],
                constraints={"max_weekly_mileage_increase_pct": 15, "min_quality_spacing_hours": 48},
            )

            executed_tools: list[str] = []

            # Agent 2: Physiology Analyst (gemini-3.8-flash)
            physio = self._run_physiology_analyst(today_str, executed_tools)

            # Agent 3: Training Architect (gemini-3.1-pro-preview or gemini-3.8-flash)
            architect_plan, used_architect_model = self._run_training_architect(
                user_prompt=user_prompt,
                intent=intent,
                triage=triage,
                physio=physio,
                ctx_window=ctx_window,
                model=architect_model,
                fallbacks=architect_fallbacks,
                today_str=today_str,
                executed_tools=executed_tools,
            )

            # Agent 4: Safety & Invariant Critic (gemini-3.5-flash-lite + deterministic invariants)
            critic_verdict, pending_approval = self._run_safety_critic_and_commit(
                session_id=session_id,
                architect_plan=architect_plan,
                physio=physio,
                require_hitl=require_hitl_for_mutations,
                executed_tools=executed_tools,
            )

            response_text = str(architect_plan["response_text"])
            if pending_approval is not None:
                response_text += (
                    f"\n\n[Human-in-the-Loop Approval Required — Request #{pending_approval['approval_id']}]: "
                    f"{pending_approval['summary']} ({pending_approval['rationale']}). "
                    "Approve or reject this change in the Coach Dashboard or via `coach_resolve_approval`."
                )
            elif not critic_verdict.passed:
                response_text += f"\n\n[Safety Critic Guardrail Blocked Mutation]: {critic_verdict.reason}"

            self.memory.append_turn(session_id, "assistant", response_text)
            # Non-blocking background memory consolidation job
            bg_future = self.consolidator.submit_consolidation(session_id)
            _ = bg_future

            latency = (time.perf_counter() - start_perf) * 1000.0
            hitl_state = "PENDING_APPROVAL" if pending_approval else "AUTO_COMMITTED_OR_READONLY"
            DEFAULT_INTENT_TRACKER.record_outcome(
                correlation_id=correlation_id,
                declared_intent=intent,
                actual_outcome=architect_plan["action_summary"],
                status="COMPLETED" if critic_verdict.passed else "GUARDRAIL_REJECTED_MUTATION",
                executed_tools=executed_tools,
                guardrail_passed=critic_verdict.passed,
                hitl_status=hitl_state,
                latency_ms=latency,
                intent_satisfied=critic_verdict.passed,
                drift_reason="" if critic_verdict.passed else critic_verdict.reason,
            )

            return {
                "correlation_id": correlation_id,
                "trace_id": root_span.trace_id,
                "status": "PENDING_APPROVAL" if pending_approval else "COMPLETED",
                "intent": intent,
                "complexity": complexity,
                "routed_models": {
                    "triage_agent": triage["model_used"],
                    "physiology_analyst": physio["model_used"],
                    "training_architect": used_architect_model,
                    "safety_critic": MODEL_FAST_LITE,
                },
                "readiness": physio["readiness"],
                "guardrail": {
                    "passed": critic_verdict.passed,
                    "category": critic_verdict.category,
                    "reason": critic_verdict.reason,
                },
                "pending_approval": pending_approval,
                "context_stats": {
                    "estimated_tokens": ctx_window["estimated_tokens"],
                    "has_compacted_summary": bool(ctx_window["compacted_summary"]),
                    "long_term_memories_count": len(ctx_window["long_term_memories"]),
                },
                "response": PIIRedactor.redact_text(response_text),
            }

    def _run_triage_agent(self, user_prompt: str, today_str: str) -> dict[str, Any]:
        with DEFAULT_TRACER.start_as_current_span("agent.triage_router", {"gen_ai.agent.name": "TriageRouterAgent"}):
            model, fallbacks = self.router.select_model("triage", 0.15)
            schema = {
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "enum": [
                            "MORNING_BRIEFING",
                            "WORKOUT_SWAP",
                            "INJURY_REPORT",
                            "MULTI_WEEK_REPLAN",
                            "GENERAL_COACHING_QA",
                        ],
                    },
                    "complexity": {"type": "number"},
                },
                "required": ["intent", "complexity"],
            }
            parsed, used = self.llm.generate_json(
                model=model,
                system_instruction="Classify the runner's request intent and reasoning complexity (0.0 to 1.0).",
                prompt=f"Today: {today_str}\nRunner message: {user_prompt}",
                schema=schema,
                fallbacks=fallbacks,
            )

            low = user_prompt.lower()
            if parsed and parsed.get("intent"):
                intent = str(parsed["intent"])
                complexity = float(parsed.get("complexity") or 0.3)
            elif any(w in low for w in ("calf", "shin", "achilles", "knee", "tight", "sore", "hurt", "pain")):
                intent, complexity = "INJURY_REPORT", 0.85
            elif any(w in low for w in ("replan", "next 4 weeks", "multi-week", "vacation", "goal time")):
                intent, complexity = "MULTI_WEEK_REPLAN", 0.9
            elif any(w in low for w in ("swap", "move", "switch", "shift", "travel", "reschedule", "can't run", "cannot run")):
                intent, complexity = "WORKOUT_SWAP", 0.6
            elif any(w in low for w in ("today", "briefing", "readiness", "workout", "morning", "sleep", "hrv")):
                intent, complexity = "MORNING_BRIEFING", 0.25
            else:
                intent, complexity = "GENERAL_COACHING_QA", 0.35

            planned_tools = ["evaluate_readiness", "get_coach_overview"]
            if intent in ("WORKOUT_SWAP", "MULTI_WEEK_REPLAN"):
                planned_tools.append("propose_schedule_mutation")
            if intent == "INJURY_REPORT":
                planned_tools.extend(["save_checkin", "propose_schedule_mutation"])

            return {
                "intent": intent,
                "complexity": complexity,
                "planned_tools": planned_tools,
                "model_used": used,
            }

    def _run_physiology_analyst(self, today_str: str, executed_tools: list[str]) -> dict[str, Any]:
        with DEFAULT_TRACER.start_as_current_span(
            "agent.physiology_analyst",
            {"gen_ai.agent.name": "PhysiologyAnalystAgent"},
        ):
            model, _ = self.router.select_model("physiology_analyst", 0.35)
            executed_tools.append("evaluate_readiness")
            readiness = evaluate_readiness(self.storage, target_day=today_str)
            executed_tools.append("get_coach_overview")
            overview = get_coach_overview(self.storage, today_str=today_str)
            return {
                "readiness": readiness,
                "overview": overview,
                "model_used": model if self.llm.is_live_llm_enabled else f"offline-policy({model})",
            }

    def _run_training_architect(
        self,
        user_prompt: str,
        intent: str,
        triage: dict[str, Any],
        physio: dict[str, Any],
        ctx_window: dict[str, Any],
        model: str,
        fallbacks: list[str],
        today_str: str,
        executed_tools: list[str],
    ) -> tuple[dict[str, Any], str]:
        with DEFAULT_TRACER.start_as_current_span(
            "agent.training_architect",
            {"gen_ai.agent.name": "TrainingArchitectAgent", "gen_ai.request.model": model},
        ):
            readiness = physio["readiness"]
            overview = physio["overview"]
            week_days: list[dict[str, Any]] = overview["week_summary"]["days"]
            today_w = overview["today_workout"] or (week_days[0] if week_days else {})

            previous_days: list[dict[str, Any]] = []
            proposed_days: list[dict[str, Any]] = []
            action_type = "READ_ONLY_BRIEFING"
            action_summary = f"Delivered {intent} grounded in {readiness['signal']} readiness."

            low = user_prompt.lower()
            if intent == "INJURY_REPORT":
                soreness_val = 7 if any(w in low for w in ("sharp", "limp", "high", "bad", "7", "8")) else 5
                executed_tools.append("save_checkin")
                self.storage.save_checkin(day=today_str, soreness=soreness_val, notes=user_prompt[:200])
                readiness = evaluate_readiness(self.storage, target_day=today_str)
                physio["readiness"] = readiness
                if today_w and float(today_w.get("planned_miles") or 0) > 0:
                    prev = copy.deepcopy(today_w)
                    upd = copy.deepcopy(today_w)
                    upd["workout_type"] = "rest" if soreness_val >= 7 else "recovery"
                    upd["planned_miles"] = 0.0 if soreness_val >= 7 else 2.0
                    upd["title"] = (
                        "Injury-Protection Rest Day"
                        if soreness_val >= 7
                        else "Gentle Tendon-Check Shakeout (2 mi)"
                    )
                    upd["target_pace"] = "-" if soreness_val >= 7 else PACE_ZONES["recovery"]
                    upd["is_quality"] = False
                    previous_days, proposed_days = [prev], [upd]
                    action_type = "INJURY_DOWNSHIFT"
                    action_summary = f"Logged soreness ({soreness_val}/10) and proposed downshifting {today_str} to '{upd['title']}'."

            elif intent in ("WORKOUT_SWAP", "MULTI_WEEK_REPLAN"):
                # Detect explicit weekday swap targets inside active week (e.g., Saturday <-> Sunday or Thu <-> Fri)
                by_dow = {
                    dt.date.fromisoformat(d["day"]).strftime("%A").lower(): d
                    for d in week_days
                }
                mentioned = [dow for dow in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday") if dow in low]
                if len(mentioned) >= 2 and mentioned[0] in by_dow and mentioned[1] in by_dow:
                    d1, d2 = copy.deepcopy(by_dow[mentioned[0]]), copy.deepcopy(by_dow[mentioned[1]])
                    p1, p2 = copy.deepcopy(d1), copy.deepcopy(d2)
                    for k in ("workout_type", "title", "description", "planned_miles", "target_pace", "hill_focus", "strength_addon", "is_quality"):
                        p1[k], p2[k] = p2[k], p1[k]
                    previous_days, proposed_days = [d1, d2], [p1, p2]
                    action_type = "SCHEDULE_SWAP"
                    action_summary = f"Proposed swapping {d1['day']} ({mentioned[0].title()}) and {d2['day']} ({mentioned[1].title()})."
                elif "saturday" in low and "saturday" in by_dow and "sunday" in by_dow:
                    d1, d2 = copy.deepcopy(by_dow["saturday"]), copy.deepcopy(by_dow["sunday"])
                    p1, p2 = copy.deepcopy(d1), copy.deepcopy(d2)
                    for k in ("workout_type", "title", "description", "planned_miles", "target_pace", "hill_focus", "strength_addon", "is_quality"):
                        p1[k], p2[k] = p2[k], p1[k]
                    previous_days, proposed_days = [d1, d2], [p1, p2]
                    action_type = "LONG_RUN_SHIFT"
                    action_summary = f"Proposed shifting Saturday Long Run ({d1['day']}) to Sunday ({d2['day']})."

            # Optional live Gemini synthesis grounded strictly in verified physiology & proposed mutation
            m = readiness.get("metrics") or {}
            grounded_brief = (
                f"Race: {RACE_NAME} ({RACE_DATE}, {overview['race']['days_remaining']}d out) | "
                f"Readiness: {readiness['signal']} ({readiness['score']}/100) — "
                f"HRV {m.get('hrv_ms') or '-'} ms (28d avg {m.get('hrv_28d_mean') or '-'} ms, z={m.get('hrv_z') or 0:+.2f}), "
                f"Min HR {m.get('rhr_bpm') or '-'} bpm, Sleep {m.get('sleep_hours') or '-'}h.\n"
                f"Today ({today_str}): {today_w.get('title', 'Rest')} — {today_w.get('planned_miles', 0):g} mi @ {today_w.get('target_pace', '-')}.\n"
                f"Action: {action_summary}"
            )

            llm_json, used_model = self.llm.generate_json(
                model=model,
                system_instruction=(
                    "You are TrainingArchitectAgent for the Sub-2:00 Publix Atlanta Half Marathon. "
                    "Cite exact HRV/RHR numbers from GROUNDED_STATE—never invent metrics."
                ),
                prompt=(
                    f"COMPACTED_MEMORY:\n{ctx_window['compacted_summary']}\n"
                    f"LONG_TERM_FACTS:\n{json.dumps(ctx_window['long_term_memories'])}\n"
                    f"GROUNDED_STATE:\n{grounded_brief}\n"
                    f"USER_REQUEST:\n{user_prompt}"
                ),
                schema={
                    "type": "object",
                    "properties": {"coaching_response": {"type": "string"}},
                    "required": ["coaching_response"],
                },
                fallbacks=fallbacks,
            )

            response_text = (
                str(llm_json["coaching_response"])
                if (llm_json and llm_json.get("coaching_response"))
                else grounded_brief
            )
            _ = triage
            return (
                {
                    "action_type": action_type,
                    "action_summary": action_summary,
                    "previous_days": previous_days,
                    "proposed_days": proposed_days,
                    "week_days": week_days,
                    "response_text": response_text,
                },
                used_model,
            )

    def _run_safety_critic_and_commit(
        self,
        session_id: str,
        architect_plan: dict[str, Any],
        physio: dict[str, Any],
        require_hitl: bool,
        executed_tools: list[str],
    ) -> tuple[GuardrailVerdict, dict[str, Any] | None]:
        with DEFAULT_TRACER.start_as_current_span(
            "agent.safety_critic",
            {"gen_ai.agent.name": "SafetyCriticAgent", "gen_ai.request.model": MODEL_FAST_LITE},
        ) as span:
            proposed: list[dict[str, Any]] = architect_plan["proposed_days"]
            previous: list[dict[str, Any]] = architect_plan["previous_days"]
            week_days: list[dict[str, Any]] = architect_plan["week_days"]
            signal = str(physio["readiness"]["signal"])

            if not proposed:
                span.set_attribute("guardrail.passed", True)
                return GuardrailVerdict(True, "READ_ONLY_VERIFIED", "No schedule mutations proposed."), None

            prop_by_day = {d["day"]: d for d in proposed}
            candidate_week = [prop_by_day.get(d["day"], d) for d in week_days]

            verdict = SafetyGuardrails.verify_plan_invariants(
                baseline_week_days=week_days,
                candidate_week_days=candidate_week,
                readiness_signal=signal,
            )
            span.set_attribute("guardrail.passed", verdict.passed)
            span.set_attribute("guardrail.category", verdict.category)
            if not verdict.passed:
                return verdict, None

            needs_hitl, hitl_reason = self.hitl.requires_approval(
                action_type=architect_plan["action_type"],
                previous_days=previous,
                proposed_days=proposed,
                readiness_signal=signal,
            )
            if require_hitl and needs_hitl:
                executed_tools.append("hitl.create_pending_approval")
                pending = self.hitl.create_pending_approval(
                    session_id=session_id,
                    action_type=architect_plan["action_type"],
                    summary=architect_plan["action_summary"],
                    rationale=hitl_reason,
                    previous_days=previous,
                    proposed_days=proposed,
                )
                return verdict, pending

            executed_tools.append("storage.upsert_plan_days")
            self.storage.upsert_plan_days(proposed, overwrite_existing=True)
            self.storage.record_adaptation(
                target_day=proposed[0]["day"],
                signal_level=signal,
                reason=architect_plan["action_summary"],
                metrics=physio["readiness"].get("metrics") or {},
                previous_days=previous,
                updated_days=proposed,
            )
            return verdict, None

    def _summarize_with_lite_model(self, prompt: str) -> str:
        parsed, _ = self.llm.generate_json(
            model=MODEL_FAST_LITE,
            system_instruction="Summarize coaching conversation history into compact bullet points.",
            prompt=prompt,
            schema={
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
            fallbacks=[MODEL_BALANCED_AGENT],
        )
        if parsed and parsed.get("summary"):
            return str(parsed["summary"])
        return ConversationMemoryManager._deterministic_compactor(prompt)

    def shutdown(self) -> None:
        self.consolidator.shutdown(wait=True)
        _ = auto_adapt_plan
        _ = manual_swap_days
