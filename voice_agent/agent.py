"""LACCD Multi-Campus Voice Agents -- LiveKit Agents entry point.

A single AgentServer registers one named agent ("campus-assistant").
The campus is determined at runtime from the room name prefix:
  - SIP calls: room prefix "{code}-call-" (e.g. "elac-call-abc123")
  - Web tokens: room prefix "{code}-voice-" (e.g. "lamc-voice-abc123")

Run locally:
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python voice_agent/agent.py dev

Console mode (no browser):
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python voice_agent/agent.py console
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from pathlib import Path

# Ensure project root and backend/ are importable
PROJECT_DIR = Path(__file__).resolve().parents[1]
for _p in [str(PROJECT_DIR), str(PROJECT_DIR / "backend")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dotenv import load_dotenv

load_dotenv(PROJECT_DIR / ".env", override=False)
load_dotenv(PROJECT_DIR / ".env.local", override=True)

# The livekit google plugin reads GOOGLE_API_KEY, not GEMINI_API_KEY
_gemini_key = os.environ.get("GEMINI_API_KEY", "")
if _gemini_key and not os.environ.get("GOOGLE_API_KEY"):
    os.environ["GOOGLE_API_KEY"] = _gemini_key


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


# Must be set BEFORE importing livekit.plugins.google (read at import time).
# The plugin's DEBUG stream logs every incoming audio chunk; leaving it enabled
# in production creates a large amount of synchronous container log I/O.
_voice_debug_logs = _env_truthy("VOICE_DEBUG_LOGS") or _env_truthy("LK_GOOGLE_DEBUG")
if _voice_debug_logs:
    os.environ["LK_GOOGLE_DEBUG"] = "1"
else:
    os.environ.pop("LK_GOOGLE_DEBUG", None)

from google.genai import types
from livekit import agents
from livekit.agents import (
    Agent,
    AgentSession,
    AudioConfig,
    BackgroundAudioPlayer,
    BuiltinAudioClip,
    RunContext,
    function_tool,
    room_io,
)
from livekit.agents import metrics as lk_metrics
from livekit.plugins import google, noise_cancellation, silero

from shared.campus_config import CAMPUSES, get_campus
from voice_agent import tools
from voice_agent.prompts import build_district_prompt, build_voice_prompt
from voice_agent.safety import enforce_session_limit, room_log_id, transcript_log_metadata
from voice_agent.usage import cost_from_usage_summary, rates_from_env

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
# Detailed Google realtime logging is opt-in because it emits an entry for
# every audio frame. Concise latency metrics are logged separately below.
_google_plugin_log = logging.getLogger("livekit.plugins.google")
VOICE_MAX_SESSION_SECONDS = max(
    60,
    min(int(os.getenv("VOICE_MAX_SESSION_SECONDS", "1200")), 3600),
)


class _RealtimeReconnectFilter(logging.Filter):
    """Let the realtime reconnect line through without the per-frame flood.

    The plugin logs "connecting to Gemini Realtime API..." at DEBUG, alongside a
    DEBUG entry for every response frame. Running the logger at INFO therefore
    hides the one line that would reveal a session being re-established
    mid-call, which is what a caller experiences when the agent stops hearing.
    """

    _KEEP = ("connecting to Gemini Realtime API",)

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.INFO:
            return True
        return any(fragment in record.getMessage() for fragment in self._KEEP)


if _voice_debug_logs:
    _google_plugin_log.setLevel(logging.DEBUG)
else:
    # DEBUG level so the reconnect record is emitted at all, plus a filter so
    # only that one survives.
    _google_plugin_log.setLevel(logging.DEBUG)
    _google_plugin_log.addFilter(_RealtimeReconnectFilter())

logger = logging.getLogger("voice_agent")

# The voice agent is a separate process from the FastAPI backend. Its tools in
# voice_agent/tools.py are @observe-wrapped, so tool args (the caller's
# transcribed question, course codes, instructor names) would reach Langfuse
# raw. Construct the Langfuse singleton with the same PII mask so this process
# redacts trace input/output/metadata before export, exactly like the backend.
if os.environ.get("LANGFUSE_SECRET_KEY") and os.environ.get("LANGFUSE_PUBLIC_KEY"):
    from langfuse import Langfuse
    from app.services.privacy import langfuse_mask

    Langfuse(mask=langfuse_mask)
    logger.info("Langfuse tracing enabled with PII masking (voice agent)")

# All 9 campus agents register under this single LiveKit agent name.
# The campus is determined from the room name prefix at runtime.
AGENT_NAME = "campus-assistant"

# Pattern to extract campus code from room name prefix
# Matches: "elac-call-xxx", "lamc-voice-xxx", "lattc-call-xxx", etc.
_ROOM_CAMPUS_RE = re.compile(
    r"^(" + "|".join(code.lower() for code in CAMPUSES) + r")-(?:call|voice)-",
)

# District-mode rooms come from the shared inbound phone number's dispatch rule.
# Room names look like "district-call_+18185551212_aBc123".
_DISTRICT_RE = re.compile(r"^district-call")

DISTRICT_CODE = "DISTRICT"


def _extract_campus_code(room_name: str) -> str:
    """Extract campus code from a room name like 'elac-call-abc123'.

    Returns DISTRICT_CODE for shared-number IVR rooms ('district-call*'),
    a campus code for prefixed rooms, or 'LAMC' as a backward-compat default.
    """
    if _DISTRICT_RE.match(room_name):
        return DISTRICT_CODE
    m = _ROOM_CAMPUS_RE.match(room_name)
    if m:
        return m.group(1).upper()
    # Backward compat: old LAMC rooms without campus prefix (e.g. "voice-xxx")
    return "LAMC"


# ── Agent definition ──────────────────────────────────


class CollegeAssistant(Agent):
    def __init__(self, campus_code: str = "LAMC") -> None:
        self._campus_code = campus_code.upper()
        if self._campus_code == DISTRICT_CODE:
            super().__init__(instructions=build_district_prompt())
        else:
            campus = get_campus(self._campus_code)
            super().__init__(instructions=build_voice_prompt(campus))

    # ── Function tools ────────────────────────────────

    @function_tool()
    async def set_campus(
        self,
        context: RunContext,
        campus_code: str,
    ) -> str:
        """Lock in which LACCD college this call is about. Use once at the start
        of a district call after the caller identifies their campus, and any
        time the caller explicitly switches to a different campus mid-call.
        After calling this, all other tools default to the new campus.

        Args:
            campus_code: One of LAMC, ELAC, LACC, LAHC, LAPC, LASC, LATTC, LAVC, WLAC.
        """
        code = campus_code.upper().strip()
        if code not in CAMPUSES:
            valid = ", ".join(CAMPUSES.keys())
            return f"Unknown campus '{campus_code}'. Valid codes: {valid}."
        self._campus_code = code
        campus = get_campus(code)
        return f"Campus set to {campus.short_name} ({code}). Subsequent tool calls will default to this campus."

    @function_tool()
    async def search_schedule(
        self,
        context: RunContext,
        subject: str,
        course_number: str = "",
        term: str = "",
        campus: str = "",
    ) -> str:
        """Search the live class schedule for available sections at a specific campus.

        Args:
            subject: The subject area, e.g. MATH, ENGL, BIOL, CS, STAT
            course_number: Optional specific course number, e.g. 101, 227, C1000
            term: Optional term like 'Spring 2026' or 'Summer 2026'. Defaults to the current term.
            campus: LACCD campus code. Leave empty to use this campus. Options: LAMC, LAVC, LACC, LAPC, LAHC, LATTC, ELAC, LASC, WLAC
        """
        return await tools.schedule_search(
            subject=subject,
            course_number=course_number,
            term=term or None,
            campus=campus or self._campus_code,
        )

    @function_tool()
    async def search_schedule_all_campuses(
        self,
        context: RunContext,
        subject: str,
        course_number: str = "",
        term: str = "",
    ) -> str:
        """Search for classes across all 9 LACCD colleges in the district.

        Args:
            subject: The subject area, e.g. MATH, ENGL, PHYSICS, ECON
            course_number: Optional specific course number
            term: Optional term like 'Spring 2026'. Defaults to the current term.
        """
        return await tools.schedule_search_all(
            subject=subject,
            course_number=course_number,
            term=term or None,
        )

    @function_tool()
    async def search_programs(
        self,
        context: RunContext,
        query: str,
        campus: str = "",
    ) -> str:
        """Search for academic programs, degrees, and certificates.

        Args:
            query: Program name or area of interest, e.g. 'nursing', 'computer science', 'data analytics'
            campus: LACCD campus code. Leave empty to use this campus.
        """
        return await tools.program_search(
            query=query, campus=campus or self._campus_code,
        )

    @function_tool()
    async def search_programs_all_campuses(
        self,
        context: RunContext,
        query: str,
    ) -> str:
        """Search for programs across all LACCD colleges in the district.

        Args:
            query: Program name or area of interest
        """
        return await tools.program_search_all(query=query)

    @function_tool()
    async def get_course_sequence(
        self,
        context: RunContext,
        query: str,
        campus: str = "",
        pace: str = "",
    ) -> str:
        """Generate a semester-by-semester course sequence plan for a program.
        Use this when students ask about course sequences, course plans, what order
        to take classes, or how to complete a degree or certificate.

        Args:
            query: Program name, e.g. 'Animation', 'Computer Science', 'Nursing'
            campus: LACCD campus code. Leave empty to use this campus.
            pace: Optional pace track. Use "part_time" if the student says they
                are working full-time / can only take 9 units, or "momentum" if
                they want to finish fast (18 units/term). Leave empty (default
                15 units/term) otherwise.
        """
        return await tools.course_sequence(
            query=query, campus=campus or self._campus_code, pace=pace or None,
        )

    @function_tool()
    async def search_ztc_classes(
        self,
        context: RunContext,
        subject: str = "",
        term: str = "",
        campus: str = "",
    ) -> str:
        """Search for Zero Textbook Cost (ZTC) and Low Cost classes at a specific campus.
        Use this when students ask about free textbooks, ZTC, OER, or low-cost materials.

        Args:
            subject: Optional subject area to filter, e.g. MATH, ENGL, BIOL. Leave empty for all subjects.
            term: Optional term like 'Spring 2026'. Defaults to the current term.
            campus: LACCD campus code. Leave empty to use this campus.
        """
        return await tools.ztc_search(
            subject=subject,
            term=term or None,
            campus=campus or self._campus_code,
        )

    @function_tool()
    async def search_ztc_all_campuses(
        self,
        context: RunContext,
        subject: str = "",
        term: str = "",
    ) -> str:
        """Search for Zero Textbook Cost (ZTC) classes across all 9 LACCD colleges.
        Use this when students want ZTC classes district-wide.

        Args:
            subject: Optional subject area to filter, e.g. MATH, ENGL
            term: Optional term like 'Spring 2026'. Defaults to the current term.
        """
        return await tools.ztc_search_all(
            subject=subject,
            term=term or None,
        )

    @function_tool()
    async def ask_verified_academic_question(
        self,
        context: RunContext,
        question: str,
    ) -> str:
        """Answer one question that combines multiple academic facts.

        Use this single tool, with a verbatim copy of the student's complete
        question. Never summarize, rewrite, or shorten the question argument,
        whenever the request combines two or more of: course renumbering,
        transferability, GE areas, prerequisites, live schedules, textbook-cost
        indicators, unit-target enrollment planning, or program sequencing.
        This tool preserves all constraints and uses the same verified routing
        path as web chat.

        Args:
            question: A verbatim copy of the student's complete multi-part
                academic question.
        """
        return await tools.academic_answer(
            question=question,
            campus_code=self._campus_code,
        )

    @function_tool()
    async def lookup_course_info(
        self,
        context: RunContext,
        course_code: str,
    ) -> str:
        """Look up course details including prerequisites, corequisites, and advisories.
        Use this when students ask about prerequisites, what courses they need to take
        before another course, corequisites, advisories, or general course information.

        Args:
            course_code: The course code like 'MATH 261', 'BIOLOGY 006', 'ENGL C1000', 'CHEM 101'
        """
        return await tools.course_info(
            course_code=course_code,
            campus_code=self._campus_code,
        )

    @function_tool()
    async def lookup_transfer_and_ge(
        self,
        context: RunContext,
        course_code: str,
    ) -> str:
        """Look up transfer status, GE areas, and renumbering history for a course.
        Use when students ask if a course is UC/CSU transferable, what GE areas
        it fulfills, or if a course was renamed or renumbered.

        Args:
            course_code: The course code like 'MATH 261', 'ENGL C1000', 'STAT C1000'
        """
        return await tools.transfer_and_ge(
            course_code=course_code,
            campus_code=self._campus_code,
        )

    @function_tool()
    async def search_ge_area_courses(
        self,
        context: RunContext,
        ge_area: str,
        system: str = "IGETC",
    ) -> str:
        """Search for courses that satisfy a GE area requirement.
        Use when students ask what courses fulfill a specific GE area.

        Args:
            ge_area: The GE area code, e.g. '2A', '3B', '1A', 'B4'. Do not include 'Area' prefix.
            system: The GE system. Options: IGETC, CSU-GE, Cal-GETC. Defaults to IGETC.
        """
        return await tools.ge_area_courses(
            ge_area=ge_area,
            system=system,
            campus_code=self._campus_code,
        )

    @function_tool()
    async def search_instructors(
        self,
        context: RunContext,
        course_code: str = "",
        instructor_name: str = "",
        campus: str = "",
    ) -> str:
        """Search for instructors or find who teaches a course. Works both ways:
        give a course code to find its instructors, or give an instructor name to
        find what they teach.

        Args:
            course_code: Optional course code like 'MATH 261'. Use to find who teaches it.
            instructor_name: Optional instructor name like 'Garcia' or 'John Smith'. Use to find their courses.
            campus: LACCD campus code. Leave empty to use this campus.
        """
        return await tools.instructor_search(
            course_code=course_code,
            instructor_name=instructor_name,
            campus_code=campus or self._campus_code,
        )

    @function_tool()
    async def ask_general_question(
        self,
        context: RunContext,
        question: str,
    ) -> str:
        """Answer a question about the college using the campus knowledge base.
        Use this for general campus questions about financial aid, parking, tutoring,
        admissions, student services, office locations, phone numbers, deadlines, etc.

        Args:
            question: The specific question to look up
        """
        return await tools.rag_answer(question, self._campus_code)

    @function_tool()
    async def crawl_website(
        self,
        context: RunContext,
        url: str,
    ) -> str:
        """Look up current information from an LACCD campus website.
        Only use this when the knowledge base didn't have what you need.
        Ask the user for confirmation first since this takes several seconds.

        Args:
            url: The LACCD campus website URL to look up
        """
        return await tools.crawl_website(url)


# ── Agent server & session ────────────────────────────

server = agents.AgentServer()

_VAD_PREFIX_PADDING_MS = 300
_VAD_SILENCE_DURATION_MS = 500


def _build_realtime_model(api_key: str) -> google.realtime.RealtimeModel:
    """Build the low-latency Gemini Live model used by every campus session."""
    return google.realtime.RealtimeModel(
        model="gemini-2.5-flash-native-audio-preview-12-2025",
        voice="Zephyr",
        api_key=api_key,
        # Dynamic thinking added roughly 1-2 seconds before tool calls in the
        # production trace. These are factual routing/retrieval turns, so the
        # extra reasoning tokens are not useful enough to justify that delay.
        thinking_config=types.ThinkingConfig(
            thinking_budget=0,
            include_thoughts=False,
        ),
        # Gemini's server-side default is about 800 ms. Google recommends
        # 500-800 ms; 500 ms keeps natural pauses while trimming dead air.
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                prefix_padding_ms=_VAD_PREFIX_PADDING_MS,
                silence_duration_ms=_VAD_SILENCE_DURATION_MS,
            ),
        ),
        # Ask Gemini to transcribe both directions. Without this there is no
        # record anywhere of what the model actually HEARD, which is exactly the
        # question when a caller reports "it stopped hearing me" -- token counts
        # can show that audio arrived but never what it resolved to, and the
        # plugin logs its reconnects at DEBUG so a dropped stretch leaves no
        # trace at all. Transcripts make the failure legible on the next call.
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        # A sliding window keeps a long session from hitting the context limit.
        # It is a general safeguard, not a substitute for reconnect diagnostics.
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow(),
        ),
    )


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: agents.JobContext):
    # Re-load env in child process (livekit-agents spawns workers via multiprocessing)
    load_dotenv(PROJECT_DIR / ".env", override=False)
    load_dotenv(PROJECT_DIR / ".env.local", override=True)
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
    if api_key and not os.environ.get("GOOGLE_API_KEY"):
        os.environ["GOOGLE_API_KEY"] = api_key

    # Determine which campus this session serves from the room name prefix.
    # DISTRICT_CODE means the call came in on the shared inbound number; the
    # agent will ask the caller which campus before doing anything else.
    room_name = ctx.job.room.name
    room_id = room_log_id(room_name)
    campus_code = _extract_campus_code(room_name)
    is_district = campus_code == DISTRICT_CODE
    campus = None if is_district else get_campus(campus_code)
    logger.info(
        "Starting voice session: room_id=%s campus=%s (%s)",
        room_id, campus_code,
        "district menu" if is_district else campus.name,
    )

    await ctx.connect()

    realtime_model = _build_realtime_model(api_key)
    session = AgentSession(llm=realtime_model)
    session_limit_task = asyncio.create_task(
        enforce_session_limit(
            session,
            seconds=VOICE_MAX_SESSION_SECONDS,
            room_id=room_id,
        )
    )

    async def _cancel_session_limit() -> None:
        session_limit_task.cancel()
        try:
            await session_limit_task
        except asyncio.CancelledError:
            pass

    ctx.add_shutdown_callback(_cancel_session_limit)

    # "The agent can't hear me" was unfalsifiable from production logs: nothing
    # recorded what the model heard, when it was listening, or when it talked
    # over the caller. These five handlers make each of those observable, so the
    # next report can be diagnosed instead of theorised about.
    @session.on("user_input_transcribed")
    def _on_user_input_transcribed(ev):
        metadata = transcript_log_metadata(ev)
        logger.info(
            "Voice heard: room_id=%s final=%s characters=%d",
            room_id,
            metadata["final"],
            metadata["characters"],
        )

    @session.on("user_state_changed")
    def _on_user_state_changed(ev):
        logger.info(
            "Voice user state: room_id=%s %s -> %s",
            room_id, getattr(ev, "old_state", None), getattr(ev, "new_state", None),
        )

    @session.on("agent_state_changed")
    def _on_agent_state_changed(ev):
        # A long run in "speaking" is the agent monologuing; a gap with no state
        # change at all is the session being reconnected underneath us.
        logger.info(
            "Voice agent state: room_id=%s %s -> %s",
            room_id, getattr(ev, "old_state", None), getattr(ev, "new_state", None),
        )

    @session.on("overlapping_speech")
    def _on_overlapping_speech(ev):
        # Fires when the caller speaks while the agent is speaking. If the
        # dropouts are really failed barge-in, this is where it shows up.
        logger.warning("Voice overlapping speech: room_id=%s", room_id)

    @session.on("error")
    def _on_session_error(ev):
        error = getattr(ev, "error", ev)
        logger.error("Voice session error: room_id=%s error_type=%s", room_id, type(error).__name__)

    @session.on("conversation_item_added")
    def _on_conversation_item_added(ev):
        item = ev.item
        if getattr(item, "role", None) != "assistant":
            return
        item_metrics = getattr(item, "metrics", {}) or {}
        e2e_latency = item_metrics.get("e2e_latency")
        if e2e_latency is not None:
            logger.info(
                "Voice turn latency: room_id=%s e2e_ms=%d",
                room_id,
                round(e2e_latency * 1000),
            )

    # Voice spend was previously invisible: the Gemini Live session runs inside
    # the LiveKit google plugin, which never passes through Langfuse, so the
    # tool spans we do trace all report $0. Collect the realtime token counts
    # here and price them at shutdown so each session logs what it actually
    # cost -- the only per-minute service in the stack we couldn't measure.
    usage_collector = lk_metrics.UsageCollector()

    @session.on("metrics_collected")
    def _on_metrics_collected(ev):
        usage_collector.collect(ev.metrics)

    async def _log_session_cost():
        try:
            summary = usage_collector.get_summary()
            cost = cost_from_usage_summary(summary, rates=rates_from_env())
            logger.info(
                "Voice session cost: room_id=%s campus=%s usd=%.6f "
                "in_audio_tok=%d in_text_tok=%d out_audio_tok=%d out_text_tok=%d "
                "cached_in_audio_tok=%d",
                room_id,
                campus_code,
                cost.total_usd,
                summary.llm_input_audio_tokens,
                summary.llm_input_text_tokens,
                summary.llm_output_audio_tokens,
                summary.llm_output_text_tokens,
                summary.llm_input_cached_audio_tokens,
            )
        except Exception as exc:
            # Cost accounting must never affect call teardown.
            logger.warning(
                "Failed to log voice session cost: room_id=%s error_type=%s",
                room_id,
                type(exc).__name__,
            )

    ctx.add_shutdown_callback(_log_session_cost)

    await session.start(
        room=ctx.room,
        agent=CollegeAssistant(campus_code),
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=noise_cancellation.BVC(),
            ),
        ),
    )

    # Thinking sound — plays on a loop whenever the agent is searching /
    # running a tool so the caller hears activity instead of silence during
    # slow backend calls (Neo4j lookups, crawl4ai, RAG, etc.).
    #
    # We intentionally DON'T use BackgroundAudioPlayer's thinking_sound kwarg
    # because its built-in state handler (_agent_state_changed in
    # livekit/agents/voice/background_audio.py) calls play() without loop=True,
    # so a 3s clip ends after one pass even if the tool is still running.
    # Instead we hook agent_state_changed ourselves and call play(loop=True).
    _thinking_sound = [
        AudioConfig(BuiltinAudioClip.KEYBOARD_TYPING, volume=0.55, probability=0.7),
        AudioConfig(BuiltinAudioClip.KEYBOARD_TYPING2, volume=0.5, probability=0.3),
    ]
    bg_audio = BackgroundAudioPlayer()
    await bg_audio.start(room=ctx.room, agent_session=session)
    ctx.add_shutdown_callback(bg_audio.aclose)

    _thinking_handle = {"h": None}

    def _on_agent_state_changed(ev):
        if ev.new_state == "thinking":
            h = _thinking_handle["h"]
            if h is not None and not h.done():
                return
            _thinking_handle["h"] = bg_audio.play(_thinking_sound, loop=True)
        else:
            h = _thinking_handle["h"]
            if h is not None:
                h.stop()
                _thinking_handle["h"] = None

    session.on("agent_state_changed", _on_agent_state_changed)

    # Generate the welcome greeting
    if is_district:
        greeting = (
            "Greet the caller warmly in 1 sentence as the LACCD voice assistant. "
            "Then ask which of the 9 LACCD colleges they're calling about today. "
            "Keep it to 2 short sentences total."
        )
    else:
        greeting = (
            f"Greet the student warmly. Introduce yourself as the {campus.short_name} "
            f"voice assistant. Briefly mention you can help with class schedules, "
            f"academic programs, and campus information. Ask what they need help with. "
            f"Keep it to 2 sentences."
        )
    await session.generate_reply(instructions=greeting)


if __name__ == "__main__":
    agents.cli.run_app(server)
