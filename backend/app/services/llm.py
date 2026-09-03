"""LLM client for OpenAI-compatible APIs (llama.cpp server, Ollama, OpenAI)."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Optional

import httpx

from app.config import get_settings

logger = logging.getLogger("ghostwriter.llm")

MODE_SYSTEM_PROMPTS = {
    "brainstorm": (
        "You are Arthur, a creative writing partner for novelists. "
        "Help brainstorm plot twists, scene ideas, character motivations, and dialogue. "
        "Stay consistent with the provided story context. Be specific and actionable. "
        "Do not rewrite the entire chapter unless asked — offer focused suggestions."
    ),
    "continue": (
        "You are Arthur, a skilled ghostwriter. Continue the prose in the author's "
        "voice and style. Match tone, tense, and POV from the context. "
        "Write 1–3 polished paragraphs that advance the scene naturally. "
        "Do not add meta commentary — only the continued prose."
    ),
    "consistency": (
        "You are Arthur, a story continuity editor. Analyze the provided text "
        "against character profiles and story memory. Flag contradictions in "
        "personality, appearance, knowledge, relationships, timeline, or world rules. "
        "If nothing is wrong, say so. Structure findings as a short bullet list."
    ),
    "lore": (
        "You are Arthur, a world-building archivist. Answer questions using the "
        "story's established lore, world notes, and characters. Infer carefully from "
        "what is written; label speculation clearly. If something is unknown, say so."
    ),
    "series": (
        "You are Arthur, the keeper of a shared story universe. Your job is to "
        "keep worldbuilding and character relationships consistent ACROSS an entire "
        "series of books — an anthology where the books share a world but are not a "
        "single linear plot.\n\n"
        "You deliberately work from the series bible, world notes, and cast of every "
        "book, and you stay effectively ignorant of plot: never speculate about what "
        "happens in a specific book's story, only about the world and the people in it.\n\n"
        "You can: answer lore questions, flag contradictions in world rules, geography, "
        "history, or character relationships between books; propose how a new character "
        "or concept could fit the established universe; suggest relationship dynamics "
        "between characters across books; and help expand the bible consistently. "
        "Label speculation clearly and say so when something is unknown."
    ),
    "canon": (
        "You are Arthur, a series canon editor. The author is bringing a complete "
        "manuscript into an existing shared universe and wants to know whether it fits "
        "before committing.\n\n"
        "Compare the supplied draft against the series bible, the canon world notes, "
        "and the cast of every book already in the universe. Identify conflicts that "
        "would break continuity if this book joined the series:\n"
        "- world rules, geography, history, magic/tech, factions that contradict canon\n"
        "- renamed or re-described people/places/things that already exist in the bible\n"
        "- character relationships, appearances, or backstories that clash with the bible\n"
        "- where the book would simply introduce NEW canon (this book adds rules/places "
        "that the bible does not yet record — worth adding to the bible)\n\n"
        "For each finding, name the rule placed and give the exact reason. Distinguish "
        "conflict (breaks canon) from new canon (adds to canon). If it is consistent, "
        "say so plainly. Use a concise bullet list."
    ),
    "extract": (
        "You are Arthur, the application's universe extractor. You are reading an early draft of a "
        "brand-new book in an existing series. Characters and locations already in the "
        "series bible may appear, but newly introduced ones are exactly what matters "
        "most.\n\n"
        "Read the supplied story prose and extract the cast, the named locations, and "
        "the worldbuilding it establishes — NOT the plot.\n\n"
        "Return ONLY a JSON object with exactly three keys, no commentary and no "
        "markdown code fences:\n"
        '{"characters": [{"name": "...", "role": "...", "physical_traits": "...", '
        '"personality": "...", "motivations": "...", "speech_patterns": "...", '
        '"backstory": "...", "relationships": "...", "notes": "..."}], '
        '"locations": [{"name": "...", "type": "...", "description": "...", '
        '"notes": "..."}], "world_facts": ["...", "..."]}\n\n'
        "Rules:\n"
        "- Include EVERY named character the prose establishes something about, "
        "especially newly introduced ones — never restrict yourself to characters you "
        "already know from a series bible, and never omit a new or secondary character.\n"
        "- Use the exact full name from the prose when it appears (e.g. \"Dukkat "
        "Blane\"), not just a nickname.\n"
        "- Every character field is a short phrase or blank if unknown — never invent.\n"
        "- locations: include EVERY named place the prose establishes something about — "
        "cities, districts, buildings, rooms, landmarks, regions, planets. Never invent "
        "a location that is not in the prose. \"type\" is a short label (city, district, "
        "building, room, landmark, region, planet, or blank), \"description\" is a short "
        "phrase about what it is, and \"notes\" is extra detail or blank.\n"
        "- world_facts are short canonical statements about places, rules, magic, "
        "technology, factions, history, or setting that the story establishes or "
        "strongly implies. Give at most 8, each a complete sentence with a subject "
        "and verb — never single words or fragments.\n"
        "- Never repeat or restate a fact.\n"
        "- Keep it plot-ignorant: no events, no scene summaries, no spoilers.\n"
        "- Emit strictly valid JSON: every character and location object must live "
        "inside its array, separated by commas, every bracket closed, and nothing "
        "after the final closing brace. Output the complete JSON with the full "
        "arrays intact — do not trim or shorten them.\n"
        "- Output the JSON immediately; do not reason, plan, or narrate."
    ),
    "plot": (
        "You are Arthur, a narrative structure analyst. Evaluate plot threads, "
        "pacing, unresolved hooks, and arc structure. Identify potential plot holes "
        "and suggest how to resolve or deepen them. Be constructive and concrete."
    ),
    "influence": (
        "You are Arthur, the Influence Analyzer — a literary critic who helps authors "
        "see their own creative DNA without judgment.\n\n"
        "Influence is neither good nor bad. Naming it is a tool for self-awareness: "
        "where the work echoes predecessors, where it transforms them, and where the "
        "author's own voice is most distinct.\n\n"
        "When analyzing the provided manuscript, characters, and world notes:\n"
        "1. **Fingerprint** — tone, diction, sentence rhythm, POV habits, thematic obsessions, "
        "genre posture (in 3–6 concrete observations tied to the text).\n"
        "2. **Resonances** — likely literary, cinematic, philosophical, or cultural echoes "
        "(authors, works, movements, or traditions). For each, give:\n"
        "   - the influence hypothesis\n"
        "   - strength (faint / clear / strong)\n"
        "   - evidence: short quotes or close paraphrase from *this* manuscript\n"
        "   - what is borrowed vs. transformed\n"
        "3. **Original signal** — moments that feel least derivative; the author's own bias, "
        "humor, moral temperature, or formal risks.\n"
        "4. **Creative options** (optional, only if useful) — ways to lean into an influence "
        "deliberately, or to push away from it, without prescribing a 'correct' style.\n\n"
        "Rules:\n"
        "- Be specific. No vague name-dropping without textual warrant.\n"
        "- Prefer 'this recalls X because…' over 'you copied X'.\n"
        "- If the sample is too thin, say what you need more of.\n"
        "- Never shame the author for influence; frame everything as craft awareness.\n"
        "- Use clear markdown headings and bullets."
    ),
}


class LLMService:
    def __init__(self):
        self.settings = get_settings()
        self._available: Optional[bool] = None
        self._available_checked_at: float = 0.0
        self._resolved_model: Optional[str] = None

    @property
    def base_url(self) -> str:
        return self.settings.llm_base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.llm_api_key}",
            "Content-Type": "application/json",
        }

    async def check_available(self, force: bool = False) -> bool:
        now = time.monotonic()
        if (
            not force
            and self._available is not None
            and now - self._available_checked_at < 30
        ):
            return self._available

        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get(
                    f"{self.base_url}/models",
                    headers=self._headers(),
                )
                if r.status_code < 500:
                    self._available = True
                    self._available_checked_at = now
                    try:
                        data = r.json()
                        models = [
                            m.get("id") for m in data.get("data", []) if m.get("id")
                        ]
                        cfg = self.settings.llm_model
                        if cfg and cfg != "local-model" and (not models or cfg in models):
                            self._resolved_model = cfg
                        elif models:
                            self._resolved_model = models[0]
                        else:
                            self._resolved_model = cfg or "local-model"
                        logger.info(
                            "LLM available at %s model=%s",
                            self.base_url,
                            self._resolved_model,
                        )
                    except Exception:  # noqa: BLE001
                        self._resolved_model = self.settings.llm_model
                    return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM health check failed: %s", exc)

        self._available = False
        self._available_checked_at = now
        return False

    def _model_name(self) -> str:
        return self._resolved_model or self.settings.llm_model or "local-model"

    def build_user_message(
        self, *, prompt: str, context: str, context_text: str = ""
    ) -> str:
        parts = ["### Story Context\n" + (context or "(no story context yet)")]
        if context_text.strip():
            parts.append("### Current Draft Excerpt\n" + context_text.strip())
        parts.append("### Request\n" + prompt.strip())
        return "\n\n".join(parts)

    def _payload(
        self,
        *,
        user_message: str,
        system_prompt: str,
        stream: bool,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> dict:
        max_tok = (
            max_tokens
            if max_tokens is not None
            else int(self.settings.llm_max_tokens)
        )
        return {
            "model": self._model_name(),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "temperature": temperature
            if temperature is not None
            else self.settings.llm_temperature,
            "max_tokens": max_tok,
            "stream": stream,
        }

    async def complete(
        self,
        *,
        user_message: str,
        system_prompt: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        payload = self._payload(
            user_message=user_message,
            system_prompt=system_prompt,
            stream=False,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        url = f"{self.base_url}/chat/completions"
        logger.info(
            "POST %s model=%s max_tokens=%s prompt_chars=%s stream=false",
            url,
            payload["model"],
            payload["max_tokens"],
            len(user_message),
        )
        timeout = httpx.Timeout(
            connect=10.0,
            read=self.settings.llm_request_timeout,
            write=150000.0,
            pool=25.0,
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, headers=self._headers(), json=payload)
            if r.status_code >= 400:
                logger.error("LLM error %s: %s", r.status_code, r.text[:500])
                r.raise_for_status()
            data = r.json()
            msg = data["choices"][0].get("message") or {}
            text = self._message_text(msg)
            logger.info("LLM response chars=%s", len(text))
            return text

    @staticmethod
    def _message_text(msg: dict) -> str:
        """Prefer visible content; fall back to reasoning (thinking models)."""
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        # Some servers return content as a list of parts
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, str):
                    parts.append(p)
                elif isinstance(p, dict) and p.get("text"):
                    parts.append(str(p["text"]))
            joined = "".join(parts).strip()
            if joined:
                return joined
        for key in (
            "reasoning_content",
            "reasoning",
            "thinking",
            "//reasoning_content",
        ):
            val = msg.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return (content or "").strip() if isinstance(content, str) else ""

    @staticmethod
    def _delta_pieces(delta: dict) -> list[tuple[str, str]]:
        """Return [(kind, text)] where kind is content|reasoning."""
        out: list[tuple[str, str]] = []
        if not isinstance(delta, dict):
            return out
        # Visible answer tokens
        c = delta.get("content")
        if isinstance(c, str) and c:
            out.append(("content", c))
        elif isinstance(c, list):
            for p in c:
                if isinstance(p, str) and p:
                    out.append(("content", p))
                elif isinstance(p, dict) and p.get("text"):
                    out.append(("content", str(p["text"])))
        # Thinking / chain-of-thought (llama.cpp reasoning models)
        for key in (
            "reasoning_content",
            "reasoning",
            "thinking",
            "reasoning_text",
        ):
            r = delta.get(key)
            if isinstance(r, str) and r:
                out.append(("reasoning", r))
        return out

    async def stream_complete(
        self,
        *,
        user_message: str,
        system_prompt: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[tuple[str, str]]:
        """Yield (kind, text) deltas: kind is 'content' or 'reasoning'."""
        payload = self._payload(
            user_message=user_message,
            system_prompt=system_prompt,
            stream=True,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        url = f"{self.base_url}/chat/completions"
        logger.info(
            "POST %s model=%s max_tokens=%s prompt_chars=%s stream=true",
            url,
            payload["model"],
            payload["max_tokens"],
            len(user_message),
        )
        timeout = httpx.Timeout(
            connect=10.0,
            read=self.settings.llm_request_timeout,
            write=30000.0,
            pool=10.0,
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", url, headers=self._headers(), json=payload
            ) as r:
                if r.status_code >= 400:
                    body = (await r.aread()).decode("utf-8", errors="replace")
                    logger.error("LLM stream error %s: %s", r.status_code, body[:500])
                    raise httpx.HTTPStatusError(
                        f"LLM error {r.status_code}: {body[:200]}",
                        request=r.request,
                        response=r,
                    )

                async for line in r.aiter_lines():
                    if not line:
                        continue
                    if line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0] or {}
                    delta = choice.get("delta") or {}
                    # Some servers put final message on the last chunk
                    if not delta and choice.get("message"):
                        text = self._message_text(choice["message"])
                        if text:
                            yield ("content", text)
                        continue
                    for kind, piece in self._delta_pieces(delta):
                        yield (kind, piece)

    async def assist(
        self,
        *,
        mode: str,
        prompt: str,
        context: str,
        context_text: str = "",
    ) -> tuple[str, bool]:
        available = await self.check_available()
        system = MODE_SYSTEM_PROMPTS.get(mode, MODE_SYSTEM_PROMPTS["brainstorm"])
        user_message = self.build_user_message(
            prompt=prompt, context=context, context_text=context_text
        )

        if not available:
            return self._offline_response(mode, prompt, context, context_text), False

        try:
            text = await self.complete(
                user_message=user_message,
                system_prompt=system,
            )
            if not text:
                return (
                    "The model returned an empty response. Check llama.cpp logs "
                    f"and model id (`{self._model_name()}`).",
                    True,
                )
            return text, True
        except Exception as exc:  # noqa: BLE001
            logger.exception("LLM assist failed")
            fallback = self._offline_response(mode, prompt, context, context_text)
            return f"{fallback}\n\n_(LLM call failed: {exc})_", False

    async def assist_stream(
        self,
        *,
        mode: str,
        prompt: str,
        context: str,
        context_text: str = "",
    ) -> AsyncIterator[tuple[str, str]]:
        """Yield (event_type, payload): token|thinking|error|done."""
        available = await self.check_available()
        system = MODE_SYSTEM_PROMPTS.get(mode, MODE_SYSTEM_PROMPTS["brainstorm"])
        user_message = self.build_user_message(
            prompt=prompt, context=context, context_text=context_text
        )

        if not available:
            text = self._offline_response(mode, prompt, context, context_text)
            yield ("token", text)
            yield ("done", "")
            return

        try:
            saw_content = False
            reasoning_buf: list[str] = []
            async for kind, piece in self.stream_complete(
                user_message=user_message,
                system_prompt=system,
            ):
                if kind == "content":
                    saw_content = True
                    yield ("token", piece)
                else:
                    reasoning_buf.append(piece)
                    yield ("thinking", piece)
            # Models that only emit reasoning_content: promote to answer
            if not saw_content and reasoning_buf:
                # Already streamed as thinking; signal client to promote
                yield ("promote_thinking", "")
            yield ("done", "")
        except Exception as exc:  # noqa: BLE001
            logger.exception("LLM stream assist failed")
            yield ("error", str(exc))

    def _offline_response(
        self,
        mode: str,
        prompt: str,
        context: str,
        context_text: str,
    ) -> str:
        char_hint = ""
        if "Character:" in context:
            names = [
                line.replace("Character:", "").strip()
                for line in context.splitlines()
                if line.startswith("Character:")
            ]
            if names:
                char_hint = ", ".join(names[:8])

        if mode == "continue":
            excerpt = (context_text or "").strip()
            tail = excerpt[-280:] if excerpt else ""
            return (
                "**Offline mode** — no LLM is connected.\n\n"
                "Start a local model server to enable prose continuation.\n\n"
                + (f"Last draft snippet:\n> …{tail}" if tail else "")
            )

        if mode == "consistency":
            return (
                "**Offline consistency checklist**\n\n"
                f"- Characters on file: {char_hint or 'none yet'}\n"
                "- Check dialogue against speech patterns.\n"
                "- Verify physical descriptions match profiles.\n"
                "- Cross-check world notes.\n"
            )

        if mode == "plot":
            return (
                "**Offline plot review scaffold**\n\n"
                f"Your question: {prompt}\n\n"
                "Connect an LLM for full narrative arc analysis."
            )

        if mode == "lore":
            return (
                "**Offline lore lookup**\n\n"
                f"Query: {prompt}\n"
                f"Known characters: {char_hint or 'none'}\n"
            )

        if mode == "series":
            return (
                "**Offline series check**\n\n"
                f"Question: {prompt}\n"
                "With an LLM connected, GhostWriter cross-checks the whole series — "
                "worldbuilding, lore, and character relationships across every book — "
                "while ignoring plot.\n\n"
                f"Cast on file: {char_hint or 'none yet'}\n"
            )

        if mode == "canon":
            return (
                "**Offline canon review**\n\n"
                "With an LLM connected, GhostWriter compares this manuscript against "
                "the series bible, world notes, and cast of every book in the universe, "
                "and reports what fits, what conflicts, and what would be **new canon**.\n\n"
                f"Cast on file: {char_hint or 'none yet'}\n"
            )

        if mode == "influence":
            return (
                "**Offline influence checklist**\n\n"
                "With a model connected, GhostWriter maps stylistic and thematic resonances "
                "in your manuscript — not as praise or blame, but as craft awareness.\n\n"
                "Self-scan while offline:\n"
                "- Which sentences could only be *yours*?\n"
                "- Which beats feel like a genre template you love?\n"
                "- Name three writers/films you reread or rewatch; hunt for their fingerprints "
                "in diction, structure, or moral temperature.\n"
                "- Where do you transform an influence instead of repeating it?\n\n"
                f"Cast on file: {char_hint or 'none yet'}\n"
                f"Your focus: {prompt or '(full manuscript fingerprint)'}\n"
            )

        return (
            "**Offline brainstorm**\n\n"
            f"Prompt: {prompt}\n\n"
            f"Expected endpoint: `{self.base_url}/chat/completions`"
        )


_llm: Optional[LLMService] = None


def get_llm() -> LLMService:
    global _llm
    if _llm is None:
        _llm = LLMService()
    return _llm
