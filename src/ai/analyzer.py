"""Content analysis using AI."""

import asyncio
import logging
from typing import List, Optional
from pydantic import ValidationError
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, MofNCompleteColumn
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

from .client import AIClient
from .classifier import ContentClassifier
from .prompting.analysis import analysis_system_prompt, analysis_user_prompt
from .utils import parse_json_response
from .video_feasibility import assess_video_feasibility
from ..models import ContentAnalysis, ContentItem
from ..processing.content import select_content, split_content
from ..processing.profiles import ProfileRegistry

DEFAULT_THROTTLE_SEC = 0.0

class ContentAnalyzer:
    """Analyzes content items using AI to determine importance."""

    def __init__(
        self,
        ai_client: AIClient,
        profiles: ProfileRegistry,
        console: Optional[Console] = None,
    ):
        self.client = ai_client
        self.profiles = profiles
        self.classifier = ContentClassifier(ai_client, profiles)
        self.console = console or Console(stderr=True)

    @staticmethod
    def _parse_json_response(response: str) -> Optional[dict]:
        """Try multiple strategies to extract a JSON object from an AI response.

        Returns the parsed dict, or None if all strategies fail.
        """
        return parse_json_response(response)

    def _get_throttle_sec(self) -> float:
        """Return the configured inter-item throttle, clamped to zero or above."""
        config = getattr(self.client, "config", None)
        throttle_sec = getattr(config, "throttle_sec", DEFAULT_THROTTLE_SEC)
        return max(throttle_sec, 0.0)

    def _get_concurrency(self) -> int:
        """Return the configured analysis concurrency, clamped to 1 or above."""
        config = getattr(self.client, "config", None)
        concurrency = getattr(config, "analysis_concurrency", 1)
        return max(concurrency, 1)

    async def analyze_batch(self, items: List[ContentItem]) -> List[ContentItem]:
        throttle_sec = self._get_throttle_sec()
        concurrency = self._get_concurrency()
        semaphore = asyncio.Semaphore(concurrency)

        async def _process(item: ContentItem, index: int, progress_task) -> ContentItem:
            async with semaphore:
                try:
                    await self._analyze_item(item)
                except Exception as e:
                    logger.error("Error analyzing item %s: %s", item.id, e)
                    if item.processing:
                        item.processing.analysis = ContentAnalysis(
                            score=None,
                            reason="Analysis failed",
                            summary=item.title,
                        )
                if throttle_sec > 0 and index < len(items) - 1:
                    await asyncio.sleep(throttle_sec)
            progress.advance(progress_task)
            return item

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            transient=True,
            console=self.console,
        ) as progress:
            task = progress.add_task("Analyzing", total=len(items))
            coros = [
                _process(item, i, task) for i, item in enumerate(items)
            ]
            analyzed_items = await asyncio.gather(*coros)

        return analyzed_items

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=2, max=10)
    )
    async def _analyze_item(self, item: ContentItem) -> None:
        """Analyze a single content item.

        Args:
            item: Content item to analyze (modified in-place)
        """
        profile = await self.classifier.resolve(item)
        if item.processing:
            item.processing.artifacts.clear()

        content_parts = split_content(item.content)
        selected_content = select_content(
            content_parts.main,
            profile.definition.content.analysis_max_chars,
            profile.definition.content.sampling,
        )
        content_section = f"Content: {selected_content}" if selected_content else ""

        # Prepare discussion section (comments, engagement)
        discussion_parts = []
        if content_parts.comments:
            discussion_parts.append(
                f"Community Comments:\n{content_parts.comments[:1500]}"
            )

        meta = item.metadata
        engagement_items = []
        if meta.get("score"):
            engagement_items.append(f"score: {meta['score']}")
        if meta.get("descendants"):
            engagement_items.append(f"{meta['descendants']} comments")
        if meta.get("favorite_count"):
            engagement_items.append(f"{meta['favorite_count']} likes")
        if meta.get("retweet_count"):
            engagement_items.append(f"{meta['retweet_count']} retweets")
        if meta.get("reply_count"):
            engagement_items.append(f"{meta['reply_count']} replies")
        if meta.get("views"):
            engagement_items.append(f"{meta['views']} views")
        if meta.get("bookmarks"):
            engagement_items.append(f"{meta['bookmarks']} bookmarks")
        if meta.get("upvote_ratio"):
            engagement_items.append(f"upvote ratio: {meta['upvote_ratio']:.0%}")
        if engagement_items:
            discussion_parts.append(f"Engagement: {', '.join(engagement_items)}")
        if meta.get("discussion_url"):
            discussion_parts.append(f"Discussion: {meta['discussion_url']}")
        if meta.get("community_note"):
            discussion_parts.append(f"Community Note: {meta['community_note']}")

        discussion_section = "\n".join(discussion_parts) if discussion_parts else ""

        # Generate user prompt
        user_prompt = analysis_user_prompt(item, content_section, discussion_section)
        system_prompt = analysis_system_prompt(profile)

        # Dual-eval (AIHOT-style, 2026-11-12 终轮整改第二批): score the same
        # item twice against the same standard and take the mean, so the
        # existing ``score >= threshold`` filter is exactly the AIHOT rule
        # ``round1 + round2 >= 2 * threshold``. Cost: analysis calls double.
        # Independence limit, stated honestly: the client has no seed
        # parameter (complete() takes system/user/temperature/max_tokens
        # only) and both rounds use the identical prompt and temperature,
        # so the two rounds are two samples from the same distribution,
        # not decorrelated runs. A single-round jitter is still visible
        # afterwards because both round scores are recorded.
        first = await self._analyze_once(item, system_prompt, user_prompt)
        second = await self._analyze_once(item, system_prompt, user_prompt)
        result = self._combine_dual_eval(first, second)

        if result is None:
            logger.warning(
                "Could not parse analysis response for %s after two rounds",
                item.id,
            )
            if item.processing:
                item.processing.analysis = ContentAnalysis(
                    score=None,
                    reason="Analysis response parse failed",
                    summary=item.title,
                )
            return

        # Script-visualization feasibility (deterministic, model-independent):
        # judged on the same text the model scored (title + selected
        # content slice), not the full article. Set here, after the dual
        # rounds combine, so it is computed once per item and never dropped
        # (third-round review R4 lesson: score_rounds was computed then
        # lost downstream — this field is rendered into the digest heading
        # by the summarizer and carried into hotspots.json sets[]).
        result.video_feasibility = assess_video_feasibility(
            f"{item.title}\n{selected_content}"
        )

        if item.processing:
            item.processing.analysis = result

    @staticmethod
    def _combine_dual_eval(
        first: Optional[ContentAnalysis],
        second: Optional[ContentAnalysis],
    ) -> Optional[ContentAnalysis]:
        """Combine two scoring rounds into one analysis.

        Both rounds: score is the mean (each round was already capped by
        ``_validate_analysis_response``); ``score_rounds`` and ``dual_eval``
        record what happened. One round: the surviving round is used alone
        and marked ``single_eval`` — a failed round never silently drops
        the item. Neither round: None.
        """
        rounds = [r for r in (first, second) if r is not None]
        if not rounds:
            return None
        primary = rounds[0]
        if len(rounds) == 2:
            scores = [r.score for r in rounds]
            primary.score = sum(scores) / len(scores)
            primary.score_rounds = scores
            primary.dual_eval = "dual"
        else:
            primary.score_rounds = [primary.score]
            primary.dual_eval = "single_eval"
            primary.reason = f"{primary.reason}（双评仅一轮成功，单轮评分）"
        return primary

    async def _analyze_once(
        self,
        item: ContentItem,
        system_prompt: str,
        user_prompt: str,
    ) -> Optional[ContentAnalysis]:
        """One scoring round: a single completion plus one repair attempt."""
        response = await self.client.complete(
            system=system_prompt,
            user=user_prompt,
        )

        result, failure = self._validate_analysis_response(response)
        if result is None:
            repair_response = await self.client.complete(
                system=system_prompt,
                user=(
                    user_prompt
                    + "\n\nYour previous response did not satisfy the output contract "
                    f"({failure}). Analyze the item again and return only the required JSON object."
                ),
                temperature=0,
            )
            result, failure = self._validate_analysis_response(repair_response)
        return result

    # Hard score caps (analysis.md 三条封顶规则，终轮整改 2026-11-12).
    # Prompt text alone never enforced these: a title-only item scored 9 used
    # to pass through unchanged. The caps are applied here, in code, after
    # validation, so a model that ignores the cap cannot smuggle a high score
    # into the digest. video_score is deliberately NOT capped (analysis.md
    # L110 exemption): a capped item can still be worth filming, but then it
    # must be handled as 传闻/待核实, which we record on video_reason.
    CAP_LIMITS = {
        "title_only": 6.0,
        "opinion_only": 6.0,
        "unconfirmed": 7.0,
    }
    CAP_NAMES = {
        "title_only": "标题级6分",
        "opinion_only": "纯观点6分",
        "unconfirmed": "未证实7分",
    }
    # video_score at/above the filming line on a capped item: the footage must
    # be shot as hearsay, never as verified fact.
    RUMOR_NOTE = "传闻/待核实口径：只拍冲突本身，不坐实未证实数字"

    @classmethod
    def _validate_analysis_response(
        cls,
        response: str,
    ) -> tuple[Optional[ContentAnalysis], str]:
        parsed = cls._parse_json_response(response)
        if not isinstance(parsed, dict):
            return None, "response was not a JSON object"
        try:
            result = ContentAnalysis.model_validate(parsed)
        except ValidationError as exc:
            first_error = exc.errors(include_url=False)[0]
            location = ".".join(str(part) for part in first_error["loc"])
            return None, f"invalid field {location or '<root>'}: {first_error['type']}"
        if result.score is None:
            return None, "score is required by the analysis contract"
        cls._apply_score_caps(result)
        return result, ""

    @classmethod
    def _apply_score_caps(cls, result: ContentAnalysis) -> None:
        """Enforce the three hard caps on ``result`` in place.

        ``evidence_level`` is authoritative: a cap applies when it names one
        of the three capped levels, and ``cap_triggered`` is (re)derived from
        it so the two fields can never disagree. ``verified`` clears any cap
        the model may have set by mistake. ``video_score`` is never touched.
        """
        level = result.evidence_level.value if result.evidence_level else None
        if level == "verified":
            result.cap_triggered = None
            return
        limit = cls.CAP_LIMITS.get(level)
        if limit is None:
            # No evidence level (old contract): trust an explicit cap_triggered
            # if the model set one, otherwise there is nothing to enforce.
            if result.cap_triggered:
                name_to_limit = {v: k for k, v in cls.CAP_NAMES.items()}
                stated = name_to_limit.get(result.cap_triggered)
                if stated is not None and result.score > cls.CAP_LIMITS[stated]:
                    original = result.score
                    result.score = cls.CAP_LIMITS[stated]
                    result.reason = (
                        f"{result.reason}（触发封顶：{result.cap_triggered}，"
                        f"原分 {original} 已截断至 {result.score}）"
                    )
            return
        cap_name = cls.CAP_NAMES[level]
        result.cap_triggered = cap_name
        capped = False
        if result.score > limit:
            original = result.score
            result.score = limit
            capped = True
        if capped or cap_name not in result.reason:
            suffix = f"（触发封顶：{cap_name}"
            if capped:
                suffix += f"，原分 {original} 已截断至 {limit}"
            suffix += "）"
            if suffix not in result.reason:
                result.reason = f"{result.reason}{suffix}"
        # Cap exemption for video: keep video_score, but mark the footage
        # as hearsay when it clears the filming line.
        if (
            result.video_score is not None
            and result.video_score >= 7
        ):
            if result.video_reason:
                if "传闻" not in result.video_reason and "待核实" not in result.video_reason:
                    result.video_reason = f"{result.video_reason}（{cls.RUMOR_NOTE}）"
            else:
                result.video_reason = cls.RUMOR_NOTE
