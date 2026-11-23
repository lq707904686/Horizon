"""Main orchestrator coordinating the entire workflow."""

import asyncio
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Literal, Optional
from urllib.parse import unquote_plus, urlsplit
import httpx
from rich.console import Console

from .console_icons import get_icons
from .models import Config, ContentItem
from .storage.manager import StorageManager, safe_output_path
from .services.email import EmailManager
from .services.webhook import WebhookNotifier
from .services.wechat import WeChatNotifier
from .scrapers.github import GitHubScraper
from .scrapers.hackernews import HackerNewsScraper
from .scrapers.rss import RSSScraper
from .scrapers.reddit import RedditScraper
from .scrapers.telegram import TelegramScraper
from .scrapers.twitter import TwitterScraper
from .scrapers.twitter_playwright import TwitterPlaywrightScraper
from .scrapers.openbb import OpenBBScraper
from .scrapers.ossinsight import OSSInsightScraper
from .scrapers.gdelt import GDELTScraper
from .scrapers.google_news import GoogleNewsScraper
from .scrapers.bilibili import BilibiliScraper
from .scrapers.youtube import YoutubeScraper
from .ai.client import create_ai_client
from .ai.analyzer import ContentAnalyzer
from .ai.summarizer import DailySummarizer
from .ai.enricher import ContentEnricher, EnrichmentBatchResult
from .ai.tokens import get_usage_snapshot
from .processing import ProfileRegistry
from .processing.events import propose_event_candidates, text_similarity
from .processing.tools import ToolRegistry


_TRACKING_QUERY_PARAMETERS = {
    "_ga",
    "dclid",
    "fbclid",
    "gclid",
    "igshid",
    "li_fat_id",
    "mc_cid",
    "mc_eid",
    "msclkid",
    "ttclid",
    "twclid",
    "vero_id",
}


def _deduplication_url_key(url: str) -> tuple[str, str, str, str, Optional[int], str, str]:
    """Return a conservative URL identity key for cross-source deduplication."""
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if (scheme, port) in {("http", 80), ("https", 443)}:
        port = None

    path = parsed.path.rstrip("/") or "/"
    query_parts = []
    for part in parsed.query.split("&") if parsed.query else []:
        name = unquote_plus(part.partition("=")[0]).lower()
        if name.startswith("utm_") or name in _TRACKING_QUERY_PARAMETERS:
            continue
        query_parts.append(part)

    return (
        scheme,
        parsed.username or "",
        parsed.password or "",
        host,
        port,
        path,
        "&".join(query_parts),
    )


@dataclass
class BalancedDigestResult:
    """Items and selection statistics from balanced digest filtering."""

    items: List[ContentItem]
    enabled: bool = False
    group_counts: Dict[str, int] = field(default_factory=dict)
    group_limits: Dict[str, Optional[int]] = field(default_factory=dict)
    duplicate_categories: List[str] = field(default_factory=list)
    # P319-3：栏目软配额换入换出的条数（0 = 未触发或无可换条目）。
    rebalanced_count: int = 0


@dataclass
class FilteringPipelineResult:
    """Items and statistics from score, topic, and digest filtering."""

    items: List[ContentItem]
    threshold_count: int
    topic_dedup_count: int
    topic_dedup_removed: int
    balanced_digest: BalancedDigestResult
    eligible_count: Optional[int] = None


@dataclass
class SourceFetchOutcome:
    """Result of fetching one configured source."""

    source_name: str
    status: Literal["success", "empty", "failure"]
    items: List[ContentItem] = field(default_factory=list)
    error: Optional[str] = None
    # P318-6：单源抓取耗时（秒），进 fetch_report.json 供归因。
    duration_sec: Optional[float] = None

    def to_dict(self) -> Dict[str, object]:
        result: Dict[str, object] = {
            "source": self.source_name,
            "status": self.status,
            "item_count": len(self.items),
        }
        if self.error is not None:
            result["error"] = self.error
        if self.duration_sec is not None:
            result["duration_sec"] = round(self.duration_sec, 2)
        return result


@dataclass
class FetchReport:
    """Aggregate diagnostics for one fetch across configured sources."""

    outcomes: List[SourceFetchOutcome] = field(default_factory=list)

    @property
    def status(self) -> Literal["not_attempted", "success", "partial_failure", "failure"]:
        if not self.outcomes:
            return "not_attempted"
        if self.failed_count == len(self.outcomes):
            return "failure"
        if self.failed_count:
            return "partial_failure"
        return "success"

    @property
    def failed_count(self) -> int:
        return sum(outcome.status == "failure" for outcome in self.outcomes)

    @property
    def all_failed(self) -> bool:
        return bool(self.outcomes) and self.failed_count == len(self.outcomes)

    def failure_message(self) -> str:
        failures = "; ".join(
            f"{outcome.source_name}: {outcome.error or 'unknown error'}"
            for outcome in self.outcomes
            if outcome.status == "failure"
        )
        return f"All {len(self.outcomes)} attempted sources failed ({failures})"

    def to_dict(self) -> Dict[str, object]:
        return {
            "status": self.status,
            "attempted": len(self.outcomes),
            "successful": len(self.outcomes) - self.failed_count,
            "empty": sum(outcome.status == "empty" for outcome in self.outcomes),
            "failed": self.failed_count,
            "item_count": sum(len(outcome.items) for outcome in self.outcomes),
            "sources": [outcome.to_dict() for outcome in self.outcomes],
        }


# --- P318-9: cross-run event memory (data/event_history.json) ---
# Each entry: {title, summary, event_group, time_anchor, date (ISO UTC)}.
# The 48h/24h decay rule used to be docstring-only ("no persistent event
# store"); the store now exists. Scope is deliberately narrow: a new item
# matching a 48h entry is tagged follow_up and loses 1 point — no merging.
# The 24h weight-halving half of the rule is NOT implemented; both halves
# currently cost the same 1 point (see cluster_events).
EVENT_HISTORY_MAX_ENTRIES = 200
EVENT_HISTORY_WINDOW_HOURS = 48
EVENT_HISTORY_SIMILARITY = 0.10
EVENT_HISTORY_FOLLOW_UP_PENALTY = 1.0


def load_event_history(path) -> List[dict]:
    """Tolerant read of the event-history file: missing/corrupt -> []."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    entries = data.get("events", [])
    return [e for e in entries if isinstance(e, dict)]


def save_event_history(path, events: List[dict]) -> None:
    """Write the event-history file, keeping only the newest entries."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"events": events[-EVENT_HISTORY_MAX_ENTRIES:]},
            f, ensure_ascii=False, indent=2,
        )
        f.write("\n")


def find_history_match(item: ContentItem, history: List[dict], now: datetime,
                       *, window_hours: int = EVENT_HISTORY_WINDOW_HOURS,
                       threshold: float = EVENT_HISTORY_SIMILARITY):
    """Return the closest 48h history entry similar to ``item``, else None.

    Similarity is the same character n-gram score the in-run proposer
    uses (processing/events.text_similarity), threshold 0.10.
    """
    analysis = item.processing.analysis if item.processing else None
    title = item.title or ""
    body = (
        (analysis.summary if analysis and analysis.summary else None)
        or item.content
        or ""
    )
    best = None
    best_sim = 0.0
    for entry in history:
        try:
            seen = datetime.fromisoformat(str(entry.get("date", "")))
        except ValueError:
            continue
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        age_hours = (now - seen).total_seconds() / 3600
        if age_hours < 0 or age_hours > window_hours:
            continue
        sim = text_similarity(
            title, body, entry.get("title") or "", entry.get("summary") or ""
        )
        if sim >= threshold and sim > best_sim:
            best, best_sim = entry, sim
    return best


class HorizonOrchestrator:
    """Orchestrates the complete workflow for content aggregation and analysis."""

    icons = get_icons()

    def __init__(
        self,
        config: Config,
        storage: StorageManager,
        console: Optional[Console] = None,
        profiles: Optional[ProfileRegistry] = None,
    ):
        """Initialize orchestrator.

        Args:
            config: Application configuration
            storage: Storage manager
            console: Shared Rich Console instance
        """
        self.config = config
        self.storage = storage
        self.console = console or Console(stderr=True)
        self.icons = get_icons(config.display.icon_style)
        self.profiles = profiles or ProfileRegistry.load(
            Path(config.processing.profiles_dir), config.processing.default_profile
        )
        self.profiles.validate_source_references(
            config.sources.model_dump(mode="json")
        )
        for profile_id in config.processing.profile_settings:
            self.profiles.get(profile_id)
        if config.digest.profile_order:
            configured_profiles = set(config.digest.profile_order)
            unknown_profiles = configured_profiles - self.profiles.ids
            if unknown_profiles:
                raise ValueError(
                    "digest.profile_order contains unknown profiles "
                    f"({', '.join(sorted(unknown_profiles))})"
                )
            config.digest.profile_order.extend(
                profile.id
                for profile in self.profiles.profiles
                if profile.id not in configured_profiles
            )
        self.email_manager = EmailManager(config.email, console=self.console) if config.email else None
        self.webhook_notifier = (
            WebhookNotifier(config.webhook, console=self.console, icons=self.icons)
            if config.webhook and config.webhook.enabled
            else None
        )
        self.wechat_notifier = (
            WeChatNotifier(config.wechat, self.storage, console=self.console, icons=self.icons)
            if config.wechat and config.wechat.enabled
            else None
        )
        self.last_fetch_report: Optional[FetchReport] = None
        # P318-14：本轮建过的 AI 客户端（分析/富化各一个链式客户端），
        # run() 末尾据此汇总分 provider 调用/回退次数。
        self._ai_clients: List = []

    async def run(self, force_hours: int = None) -> None:
        """Execute the complete workflow.

        Args:
            force_hours: Optional override for time window in hours
        """
        self.console.print(
            f"[bold cyan]{self.icons['start']} Horizon - Starting aggregation...[/bold cyan]\n"
        )

        # Check email subscriptions if configured
        if (
            self.email_manager
            and self.config.email
            and self.config.email.enabled
            and self.config.email.imap_enabled
        ):
            self.console.print(f"{self.icons['email']} Checking for new email subscriptions...")
            self.email_manager.check_subscriptions(self.storage)

        try:
            # 1. Determine time window
            since = self._determine_time_window(force_hours)
            self.console.print(
                f"{self.icons['date']} Fetching content since: "
                f"{since.strftime('%Y-%m-%d %H:%M:%S')}\n"
            )

            # 2. Fetch content from all sources
            all_items = await self.fetch_all_sources(since)
            self.console.print(
                f"{self.icons['fetched']} Fetched {len(all_items)} items from all sources\n"
            )

            if self.last_fetch_report and self.last_fetch_report.all_failed:
                raise RuntimeError(self.last_fetch_report.failure_message())

            if not all_items:
                self.console.print("[yellow]No new content found. Exiting.[/yellow]")
                return

            # 3. Merge cross-source duplicates (same URL from different sources)
            merged_items = self.merge_cross_source_duplicates(all_items)
            if len(merged_items) < len(all_items):
                self.console.print(
                    f"{self.icons['merge']} Merged "
                    f"{len(all_items) - len(merged_items)} cross-source duplicates "
                    f"→ {len(merged_items)} unique items\n"
                )

            # 3b. v3 L7：热榜短条目本地补同事件全文（零外网请求；失败只告警，不拦主流程）
            self._fill_hotlist_fulltext(merged_items)

            # 4. Analyze with AI
            analyzed_items = await self.analyze_items(merged_items)
            # P318-6：漏斗的 analyzed 数补进 fetch_report.json。
            self._update_fetch_report(analyzed=len(merged_items))
            self.console.print(
                f"{self.icons['ai']} Analyzed {len(analyzed_items)} items with AI\n"
            )

            # 5. Filter, deduplicate, and balance the digest
            filtering_result = await self.select_digest_items(
                analyzed_items,
            )
            important_items = filtering_result.items

            # Show per-sub-source selection breakdown
            selected_counts: Dict[str, int] = defaultdict(int)
            for item in important_items:
                key = f"{item.source_type.value}/{self._sub_source_label(item)}"
                selected_counts[key] += 1
            for source_key, count in sorted(selected_counts.items()):
                self.console.print(f"      {self.icons['detail']} {source_key}: {count}")
            self.console.print("")

            # 6. Search related stories + enrich with background knowledge (2nd AI pass)
            await self.enrich_items(important_items)

            # 7. Generate and save daily summaries for each configured language
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            for lang in self.config.ai.languages:
                summarizer = DailySummarizer(
                    profile_names=self.profiles.names,
                    profile_order=self.config.digest.profile_order,
                )
                summary = await summarizer.generate_summary(important_items, today, len(all_items), language=lang)

                # Save to data/summaries/
                summary_path = self.storage.save_daily_summary(today, summary, language=lang)
                self.console.print(
                    f"{self.icons['save']} Saved {lang.upper()} summary to: {summary_path}\n"
                )

                # Copy to docs/ for GitHub Pages
                try:
                    from pathlib import Path

                    post_filename = f"{today}-summary-{lang}.md"
                    posts_dir = Path("docs/_posts")
                    posts_dir.mkdir(parents=True, exist_ok=True)

                    dest_path = safe_output_path(posts_dir, post_filename)

                    # Add Jekyll front matter
                    front_matter = (
                        "---\n"
                        "layout: default\n"
                        f"title: \"Horizon Summary: {today} ({lang.upper()})\"\n"
                        f"date: {today}\n"
                        f"lang: {lang}\n"
                        "---\n\n"
                    )

                    # Strip leading H1 header to avoid duplication with Jekyll title
                    summary_content = summary
                    first_line = summary_content.strip().split("\n")[0]
                    if first_line.startswith("# "):
                        parts = summary_content.split("\n", 1)
                        if len(parts) > 1:
                            summary_content = parts[1].strip()

                    with open(dest_path, "w", encoding="utf-8") as f:
                        f.write(front_matter + summary_content)

                    self.console.print(
                        f"{self.icons['document']} Copied {lang.upper()} summary "
                        f"to GitHub Pages: {dest_path}\n"
                    )
                except Exception as e:
                    self.console.print(
                        f"[yellow]{self.icons['warning']} Failed to copy "
                        f"{lang.upper()} summary to docs/: {e}[/yellow]\n"
                    )

                # Send email if configured
                if self.email_manager and self.config.email and self.config.email.enabled:
                    self.console.print(
                        f"{self.icons['email']} Sending {lang.upper()} email summary..."
                    )
                    subscribers = self.storage.load_subscribers()
                    subject = f"Horizon Summary ({lang.upper()}) - {today}"
                    self.email_manager.send_daily_summary(summary, subject, subscribers)

                # Send webhook notification if configured
                if self.webhook_notifier:
                    await self.webhook_notifier.send_daily_summary(
                        summary=summary,
                        important_items=important_items,
                        all_items_count=len(all_items),
                        date=today,
                        lang=lang,
                        summarizer=summarizer,
                    )
                if self.wechat_notifier:
                    await self.wechat_notifier.send_daily_summary(summary, lang)

            self.console.print(
                f"[bold green]{self.icons['success']} "
                "Horizon completed successfully![/bold green]"
            )
            # P318-14：回退链生产可见性——分 provider 调用/回退次数。
            # 第一次真实回退即被看见（此前回退零生产证明）。
            call_counts: Dict[str, int] = {}
            fallback_total = 0
            for _client in getattr(self, "_ai_clients", []) or []:
                for _provider, _n in (
                    getattr(_client, "provider_call_counts", None) or {}
                ).items():
                    call_counts[_provider] = call_counts.get(_provider, 0) + _n
                fallback_total += getattr(_client, "fallback_count", 0) or 0
            if call_counts:
                calls_text = "、".join(
                    f"{provider} {n} 次"
                    for provider, n in sorted(call_counts.items())
                )
                fallback_text = (
                    f"；回退 {fallback_total} 次" if fallback_total else "；无回退"
                )
                self.console.print(
                    f"\n{self.icons['detail']} Provider 调用："
                    f"{calls_text}{fallback_text}"
                )
            usage = get_usage_snapshot()
            if usage.total_tokens > 0:
                self.console.print(
                    f"\n{self.icons['tokens']} Token usage this run: "
                    f"{usage.total_tokens} tokens "
                    f"(input: {usage.total_input_tokens}, output: {usage.total_output_tokens})"
                )
                for provider, u in sorted(usage.per_provider.items()):
                    if u.total <= 0:
                        continue
                    self.console.print(
                        f"   {self.icons['detail']} {provider}: {u.total} tokens "
                        f"(in: {u.input_tokens}, out: {u.output_tokens})"
                    )

        except Exception as e:
            self.console.print(
                f"[bold red]{self.icons['error']} Error: {e}[/bold red]"
            )

            # Send webhook failure notification if configured
            if self.webhook_notifier:
                await self.webhook_notifier.send_failure(
                    date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    error_message=str(e),
                )
            if self.wechat_notifier:
                await self.wechat_notifier.send_failure(
                    date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    error_message=str(e),
                )

            raise

    def _determine_time_window(self, force_hours: int = None) -> datetime:
        if force_hours:
            since = datetime.now(timezone.utc) - timedelta(hours=force_hours)
        else:
            hours = self.config.collection.time_window_hours
            since = datetime.now(timezone.utc) - timedelta(hours=hours)
        return since

    def _remember_ai_client(self, client) -> None:
        """P318-14：记下本轮建过的 AI 客户端，供 run() 末尾汇总调用量。
        测试替身（无 __init__）也能调：缺 _ai_clients 时现建。"""
        clients = getattr(self, "_ai_clients", None)
        if clients is None:
            clients = []
            self._ai_clients = clients
        clients.append(client)

    def _event_history_path(self):
        """P318-9：事件库路径（storage.data_dir 下）；无 storage 时为 None
        （测试替身走这条，跨轮记忆静默跳过）。"""
        storage = getattr(self, "storage", None)
        data_dir = getattr(storage, "data_dir", None)
        if data_dir is None:
            return None
        return Path(data_dir) / "event_history.json"

    def _append_event_history(self, items: List[ContentItem]) -> None:
        """P318-9：把本轮入选条目追加进事件库（标题 + 摘要 + event_group
        + time_anchor + 日期）。同标题同日不重复记；无 storage 时跳过。"""
        path = self._event_history_path()
        if path is None:
            return
        try:
            history = load_event_history(path)
            today = datetime.now(timezone.utc)
            seen_keys = {
                (e.get("title"), str(e.get("date", ""))[:10]) for e in history
            }
            for item in items:
                key = (item.title, today.strftime("%Y-%m-%d"))
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                analysis = item.processing.analysis if item.processing else None
                history.append({
                    "title": item.title,
                    "summary": (
                        (analysis.summary if analysis and analysis.summary else None)
                        or (item.content or "")[:500]
                    ),
                    "event_group": (item.metadata or {}).get("event_group"),
                    "time_anchor": analysis.time_anchor if analysis else None,
                    "date": today.isoformat(),
                })
            save_event_history(path, history)
        except OSError as exc:
            self.console.print(
                f"[yellow]警告：事件库写失败 ({exc})，跨轮记忆本轮未留存[/yellow]"
            )

    def _save_fetch_report(self) -> None:
        """P318-6：把 FetchReport 写 data/fetch_report.json（每源抓取量/
        成功/失败/耗时），随 hotspots 一起提交——池子再缩水时 24 小时内
        可定位到源。写失败只告警，不拦本轮。"""
        report = getattr(self, "last_fetch_report", None)
        if report is None:
            return
        storage = getattr(self, "storage", None)
        data_dir = getattr(storage, "data_dir", None)
        path = (
            Path(data_dir) / "fetch_report.json"
            if data_dir is not None
            else Path("data/fetch_report.json")
        )
        try:
            data = report.to_dict()
            data["date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
        except OSError as exc:
            self.console.print(
                f"[yellow]警告：fetch_report 写失败 ({exc})[/yellow]"
            )

    def _update_fetch_report(self, **extra) -> None:
        """P318-6：给已落盘的 fetch_report.json 补键（如 analyzed），
        文件不存在时跳过（不补写一份只有 analyzed 的空报告）。"""
        storage = getattr(self, "storage", None)
        data_dir = getattr(storage, "data_dir", None)
        path = (
            Path(data_dir) / "fetch_report.json"
            if data_dir is not None
            else Path("data/fetch_report.json")
        )
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            data.update(extra)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
        except (OSError, json.JSONDecodeError):
            return

    async def fetch_all_sources(self, since: datetime) -> List[ContentItem]:
        """Fetch content from all configured sources.

        This is a stable stage entry point for integrations such as MCP.

        Args:
            since: Fetch items published after this time

        Returns:
            List[ContentItem]: All fetched items
        """
        self.last_fetch_report = None
        async with httpx.AsyncClient(timeout=30.0) as client:
            tasks = []

            # GitHub sources
            if self.config.sources.github:
                github_scraper = GitHubScraper(self.config.sources.github, client)
                tasks.append(self._fetch_with_progress("GitHub", github_scraper, since))

            # Hacker News
            if self.config.sources.hackernews.enabled:
                hn_scraper = HackerNewsScraper(self.config.sources.hackernews, client)
                tasks.append(self._fetch_with_progress("Hacker News", hn_scraper, since))

            # RSS feeds
            if self.config.sources.rss:
                from .extractors import ExtractorRegistry
                rss_scraper = RSSScraper(
                    self.config.sources.rss,
                    client,
                    ExtractorRegistry(self.config.extractors),
                )
                tasks.append(self._fetch_with_progress("RSS Feeds", rss_scraper, since))

            # Reddit
            if self.config.sources.reddit.enabled:
                reddit_scraper = RedditScraper(self.config.sources.reddit, client)
                tasks.append(self._fetch_with_progress("Reddit", reddit_scraper, since))

            # Telegram
            if self.config.sources.telegram.enabled:
                telegram_scraper = TelegramScraper(self.config.sources.telegram, client)
                tasks.append(self._fetch_with_progress("Telegram", telegram_scraper, since))

            # Twitter (Apify or Playwright mode)
            if self.config.sources.twitter and self.config.sources.twitter.enabled:
                tw_cfg = self.config.sources.twitter
                if tw_cfg.mode == "playwright":
                    twitter_scraper = TwitterPlaywrightScraper(tw_cfg)
                else:
                    twitter_scraper = TwitterScraper(tw_cfg, client)
                tasks.append(self._fetch_with_progress("Twitter", twitter_scraper, since))

            # OpenBB (financial news / filings via the OpenBB Platform SDK)
            if self.config.sources.openbb and self.config.sources.openbb.enabled:
                openbb_scraper = OpenBBScraper(self.config.sources.openbb, client)
                tasks.append(self._fetch_with_progress("OpenBB", openbb_scraper, since))

            # OSS Insight trending repos
            if self.config.sources.ossinsight and self.config.sources.ossinsight.enabled:
                oss_scraper = OSSInsightScraper(self.config.sources.ossinsight, client)
                tasks.append(self._fetch_with_progress("OSS Insight", oss_scraper, since))

            # GDELT 2.0 DOC API (key-less global news)
            if self.config.sources.gdelt and self.config.sources.gdelt.enabled:
                gdelt_scraper = GDELTScraper(self.config.sources.gdelt, client)
                tasks.append(self._fetch_with_progress("GDELT", gdelt_scraper, since))

            # Google News RSS (key-less news search)
            if self.config.sources.google_news and self.config.sources.google_news.enabled:
                gn_scraper = GoogleNewsScraper(
                    self.config.sources.google_news,
                    client,
                    ExtractorRegistry(self.config.extractors),
                )
                tasks.append(self._fetch_with_progress("Google News", gn_scraper, since))

            # Bilibili ranking/popular (key-less video lists, no extraction)
            if self.config.sources.bilibili and self.config.sources.bilibili.enabled:
                bilibili_scraper = BilibiliScraper(self.config.sources.bilibili, client)
                tasks.append(self._fetch_with_progress("Bilibili", bilibili_scraper, since))

            # YouTube Data API v3 (official key-based API; no key -> []
            # inside the scraper, other sources keep running)
            if self.config.sources.youtube and self.config.sources.youtube.enabled:
                youtube_scraper = YoutubeScraper(self.config.sources.youtube, client)
                tasks.append(self._fetch_with_progress("YouTube", youtube_scraper, since))

            # Fetch all concurrently
            outcomes = await asyncio.gather(*tasks)
            self.last_fetch_report = FetchReport(outcomes=list(outcomes))
            # P318-6：抓取量与漏斗留存——FetchReport 只打印的日子结束了。
            self._save_fetch_report()

            # Flatten successful and empty outcomes; failures remain in the report.
            all_items: List[ContentItem] = []
            for outcome in outcomes:
                all_items.extend(outcome.items)

            return all_items

    async def _fetch_with_progress(
        self, name: str, scraper, since: datetime
    ) -> SourceFetchOutcome:
        """Fetch from a scraper with progress indication.

        Args:
            name: Source name for display
            scraper: Scraper instance
            since: Fetch items after this time

        Returns:
            SourceFetchOutcome: Named fetch result and diagnostics
        """
        self.console.print(f"{self.icons['fetch']} Fetching from {name}...")
        started = time.monotonic()
        try:
            items = await scraper.fetch(since)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.console.print(f"[red]   Failed to fetch {name}: {error}[/red]")
            return SourceFetchOutcome(
                source_name=name,
                status="failure",
                error=error,
                duration_sec=time.monotonic() - started,
            )

        self.console.print(f"   Found {len(items)} items from {name}")

        # Show per-sub-source breakdown when there are multiple sub-sources
        sub_counts: Dict[str, int] = defaultdict(int)
        for item in items:
            sub_counts[self._sub_source_label(item)] += 1
        if len(sub_counts) > 1:
            for sub, count in sorted(sub_counts.items()):
                self.console.print(f"      {self.icons['detail']} {sub}: {count}")

        return SourceFetchOutcome(
            source_name=name,
            status="success" if items else "empty",
            items=items,
            duration_sec=time.monotonic() - started,
        )

    @staticmethod
    def _sub_source_label(item: ContentItem) -> str:
        """Return a human-readable sub-source label for an item."""
        meta = item.metadata
        if meta.get("subreddit"):
            return f"r/{meta['subreddit']}"
        if meta.get("feed_name"):
            return meta["feed_name"]
        if meta.get("channel"):
            return f"@{meta['channel']}"
        if meta.get("period") and meta.get("repo"):
            return f"ossinsight:{meta.get('primary_language', 'all')}"
        if meta.get("repo"):
            return meta["repo"]
        if meta.get("watchlist"):
            return meta["watchlist"]
        if meta.get("source_name"):
            return meta["source_name"]
        if meta.get("gn_query"):
            return f"google_news:{meta['gn_query']}"
        if meta.get("domain"):
            return meta["domain"]
        return item.author or "unknown"

    def _fill_hotlist_fulltext(self, items) -> None:
        """v3 L7：热榜条目正文 <300 字时，用当天已抓到的全文条目（中新网/澎湃/36氪…）补同事件全文。

        封顶规则（P318-2）不变；只是让同一事件按全文打分。结果写进 fetch_report.json 的
        fulltext_fill 键（只加键）。任何异常只打警告。
        """
        try:
            from .processing.fulltext_fill import fill_hotlist_fulltext
            stats = fill_hotlist_fulltext(items)
            self.console.print(
                f"[cyan]L7 热榜补全文：短条目 {stats['short_total']} 条（热榜 {stats['hotlist_short']}），"
                f"全文池 {stats['pool']} 条，匹配补上 {stats['filled']} 条（热榜 {stats['filled_hotlist']}）[/cyan]"
            )
            for pair in stats["pairs"][:20]:
                self.console.print(f"  · {pair['hot']} ← [{pair['src']}] {pair['title']}")
            self._update_fetch_report(fulltext_fill={k: v for k, v in stats.items()})
        except Exception as exc:  # noqa: BLE001
            self.console.print(f"[yellow]警告: L7 热榜补全文失败（{exc}），按原正文打分[/yellow]")

    def merge_cross_source_duplicates(self, items: List[ContentItem]) -> List[ContentItem]:
        """Merge items that point to the same URL from different sources.

        This is a stable stage helper for integrations such as MCP.

        Keeps the item with the richest content and combines metadata.

        Args:
            items: Items to deduplicate

        Returns:
            List[ContentItem]: Deduplicated items
        """
        # Group by normalized URL
        url_groups: Dict[tuple[object, ...], List[ContentItem]] = {}
        for item in items:
            if isinstance(item.profile, list):
                requested_profile: object = tuple(
                    profile_id.strip() for profile_id in item.profile
                )
            else:
                requested_profile = (item.profile or "auto").strip() or "auto"
            key = (*_deduplication_url_key(str(item.url)), requested_profile)
            url_groups.setdefault(key, []).append(item)

        merged = []
        for group in url_groups.values():
            group_copies = [item.model_copy(deep=True) for item in group]
            if len(group) == 1:
                merged.append(group_copies[0])
                continue

            # Pick the item with the richest content as primary
            primary = max(group_copies, key=lambda x: len(x.content or ""))

            # Merge metadata and source info from other items
            all_sources = []
            for item in group_copies:
                if item.source_type.value not in all_sources:
                    all_sources.append(item.source_type.value)
                # Merge metadata (engagement, discussion, etc.)
                for mk, mv in item.metadata.items():
                    if mk not in primary.metadata or not primary.metadata[mk]:
                        primary.metadata[mk] = mv

                # Append content (e.g., comments from another source)
                if item is not primary and item.content:
                    if primary.content and item.content not in primary.content:
                        primary.content = (primary.content or "") + f"\n\n--- From {item.source_type.value} ---\n" + item.content

            primary.metadata["merged_sources"] = all_sources
            merged.append(primary)

        return merged

    async def cluster_events(
        self,
        items: List[ContentItem],
        *,
        log: bool = True,
    ) -> List[ContentItem]:
        """Cluster items by real-world event: text proposes, model disposes.

        Two-stage, 2026-11-12 终轮整改第二批 (P2-22):
        1. ``propose_event_candidates`` (character n-gram overlap, no AI
           call) proposes groups that might be one event.
        2. One model call verdicts each group as ``same_event`` /
           ``follow_up`` / ``different``. Only ``same_event`` merges, into
           the highest-scored item (items arrive sorted by score desc).
           ``follow_up`` items are kept but tagged with a shared
           ``event_group`` so the follow-up relation stays visible;
           ``different`` is untouched. Unsure means ``follow_up``, never a
           merge. If the model call fails or is unparseable, items return
           unchanged — text similarity alone never merges anything, and the
           later ``merge_topic_duplicates`` pass is still a second net.

        Heat rule, in-run part: a merged same-event group survives as one
        item, so that event's heat is counted once, from one source. The
        cross-run part is P318-9: new items are first compared against the
        persistent event store (``data/event_history.json``) — a match
        within 48h is tagged ``follow_up`` and loses 1 point. No merging
        across runs (only marking + penalty); the 24h weight-halving half
        of the decay rule is still not implemented (both halves cost the
        same 1 point for now). The ``event_group`` tags written here are
        what the history file keys on.
        """
        # P318-9：跨轮事件记忆——先拿 48h 事件库比对：相似度 ≥0.10 的新条目
        # 标 follow_up、分 −1（只标不合并；24h 内权重减半未做，与 48h 同策）。
        # 无 storage（测试替身）时跳过，不拦本轮。
        history_path = self._event_history_path()
        history = (
            load_event_history(history_path) if history_path is not None else []
        )
        if history:
            now = datetime.now(timezone.utc)
            for item in items:
                match = find_history_match(item, history, now)
                if match is None:
                    continue
                analysis = item.processing.analysis if item.processing else None
                if analysis is not None and analysis.score is not None:
                    analysis.score = max(
                        analysis.score - EVENT_HISTORY_FOLLOW_UP_PENALTY, 0.0
                    )
                item.metadata["event_group"] = (
                    match.get("event_group")
                    or f"evt:history:{(match.get('title') or '')[:20]}"
                )
                item.metadata["event_relation"] = "follow_up"
                item.metadata["event_history_match"] = match.get("title")
                if log:
                    self.console.print(
                        "   [dim]event-history: "
                        f"[{item.title[:40]}] 与 48h 内事件"
                        f"「{(match.get('title') or '')[:40]}」相似，"
                        "标 follow_up、分 −1[/dim]"
                    )
        if len(items) <= 1:
            self._append_event_history(items)
            return items

        from .ai.prompting.deduplication import EVENT_CLUSTER_SYSTEM, EVENT_CLUSTER_USER
        from .ai.utils import parse_json_response

        groups = propose_event_candidates(items)
        if not groups:
            self._append_event_history(items)
            return items

        lines = []
        for group in groups:
            parts = []
            for idx in group:
                item = items[idx]
                analysis = item.processing.analysis if item.processing else None
                summary = analysis.summary if analysis and analysis.summary else "—"
                source = item.source_type.value
                published = item.published_at.isoformat() if item.published_at else "—"
                parts.append(
                    f"[{idx}] {item.title}\n    Source: {source} · {published}\n    Summary: {summary}"
                )
            lines.append("\n".join(parts))
        groups_text = "\n\n---\n\n".join(lines)

        try:
            ai_client = create_ai_client(self.config.ai)
            response = await ai_client.complete(
                system=EVENT_CLUSTER_SYSTEM,
                user=EVENT_CLUSTER_USER.format(groups=groups_text),
            )
            result = parse_json_response(response)
            if result is None:
                raise ValueError("could not parse event verdicts")
            verdicts = result.get("verdicts", [])
        except Exception as e:
            if log:
                self.console.print(
                    f"[yellow]  event-cluster: model verdict failed ({e}), keeping items[/yellow]"
                )
            self._append_event_history(items)
            return items

        verdict_by_group = {}
        for entry in verdicts:
            if not isinstance(entry, dict):
                continue
            group = entry.get("group")
            verdict = entry.get("verdict")
            if (
                isinstance(group, list)
                and len(group) >= 2
                and verdict in {"same_event", "follow_up", "different"}
            ):
                verdict_by_group[tuple(sorted(int(i) for i in group))] = verdict

        drop_indices: set[int] = set()
        for group in groups:
            key = tuple(group)
            verdict = verdict_by_group.get(key)
            if verdict is None:
                continue
            primary_idx = group[0]
            primary = items[primary_idx]
            event_id = f"evt:{primary.id}"
            if verdict == "same_event":
                merged_titles = []
                for dup_idx in group[1:]:
                    dup = items[dup_idx]
                    merged_titles.append(dup.title)
                    if dup.content:
                        if not primary.content or dup.content not in primary.content:
                            label = dup.source_type.value
                            primary.content = (primary.content or "") + f"\n\n--- From {label} ---\n{dup.content}"
                    if log:
                        self.console.print(
                            f"   [dim]event-cluster: keep [{primary_idx}] {primary.title}[/dim]\n"
                            f"   [dim]       merge [{dup_idx}] {dup.title} (same event)[/dim]"
                        )
                    drop_indices.add(dup_idx)
                primary.metadata["event_group"] = event_id
                primary.metadata["merged_event_items"] = merged_titles
            elif verdict == "follow_up":
                for idx in group:
                    items[idx].metadata["event_group"] = event_id
                    items[idx].metadata["event_relation"] = "follow_up"
                if log:
                    self.console.print(
                        f"   [dim]event-cluster: group {group} is a follow-up, kept with {event_id}[/dim]"
                    )

        result = [item for i, item in enumerate(items) if i not in drop_indices]
        self._append_event_history(result)
        return result

    async def merge_topic_duplicates(
        self,
        items: List[ContentItem],
        *,
        log: bool = True,
    ) -> List[ContentItem]:
        """Merge items covering the same topic using AI semantic deduplication.

        This is a stable stage helper for integrations such as MCP.

        Sends all item titles, tags, and summaries to AI in a single call.
        Items must already be sorted by analysis score descending so that the first
        item in each duplicate group is always the highest-scored one.
        Content (comments) from duplicate items is merged into the primary.

        Falls back to returning items unchanged if the AI call fails.
        """
        if len(items) <= 1:
            return items

        from .ai.prompting.deduplication import TOPIC_DEDUP_SYSTEM, TOPIC_DEDUP_USER
        from .ai.utils import parse_json_response

        # Build the item list for the prompt
        lines = []
        for i, item in enumerate(items):
            analysis = item.processing.analysis if item.processing else None
            tags = ", ".join(analysis.tags) if analysis and analysis.tags else "—"
            summary = analysis.summary if analysis else "—"
            lines.append(f"[{i}] {item.title}\n    Tags: {tags}\n    Summary: {summary}")
        items_text = "\n\n".join(lines)

        try:
            ai_client = create_ai_client(self.config.ai)
            response = await ai_client.complete(
                system=TOPIC_DEDUP_SYSTEM,
                user=TOPIC_DEDUP_USER.format(items=items_text),
            )
            result = parse_json_response(response)
            if result is None:
                if log:
                    self.console.print("[yellow]  dedup: could not parse AI response, skipping[/yellow]")
                return items

            duplicate_groups = result.get("duplicates", [])
        except Exception as e:
            if log:
                self.console.print(f"[yellow]  dedup: AI call failed ({e}), skipping[/yellow]")
            return items

        if not duplicate_groups:
            return items

        # Build a set of indices to drop (all non-primary duplicates)
        drop_indices: set[int] = set()
        for group in duplicate_groups:
            if not isinstance(group, list) or len(group) < 2:
                continue
            primary_idx = group[0]
            if primary_idx < 0 or primary_idx >= len(items):
                continue
            primary = items[primary_idx]
            for dup_idx in group[1:]:
                if not isinstance(dup_idx, int) or dup_idx < 0 or dup_idx >= len(items):
                    continue
                if dup_idx == primary_idx:
                    continue
                dup = items[dup_idx]
                # Merge comments/content from the duplicate into the primary
                if dup.content:
                    if not primary.content or dup.content not in primary.content:
                        label = dup.source_type.value
                        primary.content = (primary.content or "") + f"\n\n--- From {label} ---\n{dup.content}"
                if log:
                    self.console.print(
                        f"   [dim]dedup: keep [{primary_idx}] {primary.title}[/dim]\n"
                        f"   [dim]       drop [{dup_idx}] {dup.title}[/dim]"
                    )
                drop_indices.add(dup_idx)

        return [item for i, item in enumerate(items) if i not in drop_indices]

    async def filter_items(
        self,
        items: List[ContentItem],
        *,
        threshold: Optional[float] = None,
        topic_dedup: bool = True,
        apply_balance: bool = True,
        log: bool = True,
    ) -> FilteringPipelineResult:
        """Apply score thresholding, optional topic dedup, and digest balancing."""
        threshold_items = []
        for item in items:
            if self.passes_profile_filter(item, threshold):
                threshold_items.append(item)
        threshold_items.sort(
            key=lambda item: (
                item.processing.analysis.score
                if item.processing and item.processing.analysis and item.processing.analysis.score is not None
                else -1
            ),
            reverse=True,
        )

        if log:
            self.console.print(
                f"{self.icons['filter']} Selected {len(threshold_items)} items "
                "with profile filters\n"
            )

        # Event clustering runs before per-profile topic dedup: same-event
        # items from different sources merge here (heat counted once), and
        # follow-ups are tagged with a shared event_group. Items arrive
        # sorted by score desc, so the primary of each group is the best.
        if threshold_items:
            threshold_items = await self.cluster_events(threshold_items, log=log)

        deduped_items = threshold_items
        if topic_dedup and deduped_items:
            profile_groups: Dict[str, List[ContentItem]] = defaultdict(list)
            for item in deduped_items:
                profile_id = (
                    item.processing.classification.profile
                    if item.processing
                    else self.profiles.default_profile
                )
                profile_groups[profile_id].append(item)
            deduped_items = []
            for profile_id, profile_items in profile_groups.items():
                settings = self.config.processing.profile_settings.get(profile_id)
                if settings is None or settings.topic_dedup:
                    deduped_items.extend(
                        await self.merge_topic_duplicates(profile_items, log=log)
                    )
                else:
                    deduped_items.extend(profile_items)
            deduped_items.sort(
                key=lambda item: (
                    item.processing.analysis.score
                    if item.processing
                    and item.processing.analysis
                    and item.processing.analysis.score is not None
                    else -1
                ),
                reverse=True,
            )
        topic_dedup_removed = len(threshold_items) - len(deduped_items)

        if log and topic_dedup_removed:
            self.console.print(
                f"{self.icons['cleanup']} Removed {topic_dedup_removed} topic duplicates "
                f"→ {len(deduped_items)} unique items\n"
            )

        balanced_digest = (
            self.apply_balanced_digest(deduped_items, log=log)
            if apply_balance
            else BalancedDigestResult(items=deduped_items)
        )
        return FilteringPipelineResult(
            items=balanced_digest.items,
            threshold_count=len(threshold_items),
            topic_dedup_count=len(deduped_items),
            topic_dedup_removed=topic_dedup_removed,
            balanced_digest=balanced_digest,
        )

    async def select_digest_items(
        self,
        items: List[ContentItem],
        *,
        threshold: Optional[float] = None,
        topic_dedup: bool = True,
        log: bool = True,
    ) -> FilteringPipelineResult:
        """Select final digest items using the same stages for every entry point."""
        initial = await self.filter_items(
            items,
            threshold=threshold,
            topic_dedup=topic_dedup,
            apply_balance=False,
            log=log,
        )
        candidates = initial.items
        await self._expand_twitter_discussion(candidates)

        # Targeted re-analysis can lower a score, so reapply profile filters.
        eligible = [
            item
            for item in candidates
            if self.passes_profile_filter(item, threshold)
        ]
        eligible.sort(
            key=lambda item: (
                item.processing.analysis.score
                if item.processing
                and item.processing.analysis
                and item.processing.analysis.score is not None
                else -1
            ),
            reverse=True,
        )
        balanced = self.apply_balanced_digest(eligible, log=log)
        return FilteringPipelineResult(
            items=balanced.items,
            threshold_count=initial.threshold_count,
            topic_dedup_count=initial.topic_dedup_count,
            topic_dedup_removed=initial.topic_dedup_removed,
            balanced_digest=balanced,
            eligible_count=len(eligible),
        )

    def passes_profile_filter(
        self,
        item: ContentItem,
        threshold: Optional[float] = None,
    ) -> bool:
        if not item.processing or not item.processing.analysis:
            return False
        profile_id = item.processing.classification.profile
        settings = self.config.processing.profile_settings.get(profile_id)
        effective_threshold = threshold
        if effective_threshold is None and settings is not None:
            effective_threshold = settings.threshold
        if effective_threshold is None:
            return True
        score = item.processing.analysis.score
        return score is not None and score >= effective_threshold

    @staticmethod
    def _digest_group_key(item, category_to_group, default_group):
        category = item.metadata.get("category")
        return (
            category_to_group.get(category, default_group)
            if isinstance(category, str)
            else default_group
        )

    @staticmethod
    def _column_key(item: ContentItem):
        """P319-3：栏目键——优先用分析契约的 category_5 首项（P318-13
        起模型输出），其次用源配置的 metadata["category"]，再其次用
        topic_category；三者皆无为 None（不参与配额计算）。注意：
        renxing 的 metadata["category"] 是 feed 分类（热榜/商业/效率），
        不是 5 真分类——5 栏口径只认 category_5。"""
        analysis = item.processing.analysis if item.processing else None
        cats = getattr(analysis, "category_5", None) if analysis else None
        if cats:
            return cats[0]
        category = item.metadata.get("category")
        if isinstance(category, str) and category:
            return category
        topic_category = (
            getattr(analysis, "topic_category", None) if analysis else None
        )
        return topic_category or None

    @staticmethod
    def _video_score_for_rank(item: ContentItem) -> float:
        """P319-3：顺延排序用的 video_score；缺失按 -1 计（排最后）。"""
        analysis = item.processing.analysis if item.processing else None
        score = getattr(analysis, "video_score", None) if analysis else None
        return score if isinstance(score, (int, float)) else -1.0

    @staticmethod
    def _score_band(item: ContentItem):
        """P319-3：同分档 = score 的整数档（7.x 与 7.y 同档）。"""
        analysis = item.processing.analysis if item.processing else None
        score = getattr(analysis, "score", None) if analysis else None
        return int(score) if isinstance(score, (int, float)) else None

    def _rebalance_columns(self, selected, unselected, group_key_fn, *, log=True):
        """P319-3：栏目配额软 enforcement。

        单栏入选占比 >60% 时，把超额条目（同栏内 video_score 最低者）
        换成零入选栏目的同分档条目（video_score 最高者先补）。同分档
        无可换条目时不换——digest 不缩水，这是“软”的含义。P318-9
        配合：已入选事件的 follow_up 不得当凑数补位（同 event_group
        的跟进条目不进补位池）。少于 5 条不触发（小样本下 60% 无意义）。
        返回 (new_selected, swaps)。
        """
        if len(selected) < 5:
            return selected, 0
        selected = list(selected)
        pool = list(unselected)
        swaps = 0
        while True:
            keys = [self._column_key(item) for item, _ in selected]
            named = [k for k in keys if k is not None]
            if not named:
                break
            counts: Dict[str, int] = defaultdict(int)
            for k in named:
                counts[k] += 1
            dominant, dominant_count = max(
                counts.items(), key=lambda kv: kv[1]
            )
            if dominant_count <= 0.6 * len(selected):
                break
            excess = dominant_count - int(0.6 * len(selected))
            if excess <= 0:
                break
            # 超额条目：同栏内 video_score 最低者先出。
            dominant_positions = [
                i for i, (item, _g) in enumerate(selected)
                if self._column_key(item) == dominant
            ]
            dominant_positions.sort(
                key=lambda i: (
                    self._video_score_for_rank(selected[i][0]),
                    -(selected[i][0].processing.analysis.score or 0)
                    if selected[i][0].processing
                    and selected[i][0].processing.analysis
                    else 0,
                )
            )
            selected_groups = {
                (item.metadata or {}).get("event_group")
                for item, _ in selected
                if (item.metadata or {}).get("event_group")
            }
            swapped = False
            for pos in dominant_positions:
                if excess <= 0:
                    break
                out_item, _out_group = selected[pos]
                band = self._score_band(out_item)
                # 补位池：零入选栏目、同分档、非已入选事件的跟进。
                candidates = []
                for cand in pool:
                    cand_key = self._column_key(cand)
                    if cand_key is None or counts.get(cand_key, 0) != 0:
                        continue
                    if self._score_band(cand) != band:
                        continue
                    cand_group = (cand.metadata or {}).get("event_group")
                    if (
                        cand_group
                        and cand_group in selected_groups
                        and (cand.metadata or {}).get("event_relation")
                        == "follow_up"
                    ):
                        continue
                    candidates.append(cand)
                if not candidates:
                    continue
                candidates.sort(
                    key=lambda c: (
                        -self._video_score_for_rank(c),
                        -(c.processing.analysis.score or 0)
                        if c.processing and c.processing.analysis
                        else 0,
                    )
                )
                in_item = candidates[0]
                pool.remove(in_item)
                pool.append(out_item)
                selected[pos] = (in_item, group_key_fn(in_item))
                # 计数随换入换出实时更新：补位栏不再是零入选栏，下一轮
                # 不再从同一栏连续补位；换入条目的事件组也进已选集合。
                counts[dominant] -= 1
                counts[self._column_key(in_item)] = (
                    counts.get(self._column_key(in_item), 0) + 1
                )
                in_group = (in_item.metadata or {}).get("event_group")
                if in_group:
                    selected_groups.add(in_group)
                if log:
                    self.console.print(
                        f"      {self.icons['balance']} 栏目软配额："
                        f"「{dominant}」超额，换出 {out_item.title[:24]}"
                        f"（video {self._video_score_for_rank(out_item):g}）"
                        f"，换入「{self._column_key(in_item)}」"
                        f"{in_item.title[:24]}"
                        f"（video {self._video_score_for_rank(in_item):g}，"
                        f"同 {band} 分档）"
                    )
                swaps += 1
                excess -= 1
                swapped = True
            if not swapped:
                break
        return selected, swaps

    def apply_balanced_digest(
        self,
        items: List[ContentItem],
        *,
        log: bool = True,
    ) -> BalancedDigestResult:
        """Apply configured category quotas and the final item cap.

        Categories are read from ``item.metadata["category"]``. If a category
        appears in more than one configured group, the first group in config
        order wins.
        """
        digest = self.config.digest
        groups = digest.category_groups
        max_items = digest.max_items

        if not groups and max_items is None:
            return BalancedDigestResult(items=items)

        sorted_items = sorted(
            items,
            key=lambda item: (
                item.processing.analysis.score
                if item.processing and item.processing.analysis and item.processing.analysis.score is not None
                else -1
            ),
            reverse=True,
        )

        category_to_group: Dict[str, str] = {}
        duplicate_categories: List[str] = []
        for group_key, group in groups.items():
            for category in group.categories:
                if category in category_to_group:
                    if category_to_group[category] != group_key:
                        duplicate_categories.append(category)
                    continue
                category_to_group[category] = group_key

        if log:
            for category in sorted(set(duplicate_categories)):
                first_group = category_to_group[category]
                self.console.print(
                    f"[yellow]Warning: category '{category}' is configured in multiple "
                    f"groups; using '{first_group}'.[/yellow]"
                )

        selected: List[tuple[ContentItem, str]] = []
        group_counts: Dict[str, int] = defaultdict(int)
        default_group = digest.default_group

        for item in sorted_items:
            group_key = self._digest_group_key(
                item, category_to_group, default_group
            )

            if group_key in groups:
                limit = groups[group_key].limit
            else:
                limit = digest.default_group_limit

            if limit is not None and group_counts[group_key] >= limit:
                continue

            selected.append((item, group_key))
            group_counts[group_key] += 1

        if max_items is not None:
            selected = selected[:max_items]

        # P319-3：栏目配额软 enforcement——单栏 >60% 时超额按 video_score
        # 顺延给零入选栏目同分档条目（P318-9 事件库配合：已入选事件的
        # follow_up 不得当凑数补位）。未入选的候选即补位池。
        selected_ids = {id(item) for item, _ in selected}
        unselected = [it for it in sorted_items if id(it) not in selected_ids]
        selected, rebalanced_count = self._rebalance_columns(
            selected,
            unselected,
            lambda it: self._digest_group_key(
                it, category_to_group, default_group
            ),
            log=log,
        )
        # 换入换出后按分数重排，保持 digest 的分数降序。
        selected.sort(
            key=lambda pair: (
                pair[0].processing.analysis.score
                if pair[0].processing
                and pair[0].processing.analysis
                and pair[0].processing.analysis.score is not None
                else -1
            ),
            reverse=True,
        )

        final_counts: Dict[str, int] = defaultdict(int)
        for _, group_key in selected:
            final_counts[group_key] += 1

        group_limits: Dict[str, Optional[int]] = {
            group_key: group.limit for group_key, group in groups.items()
        }
        group_limits.setdefault(default_group, digest.default_group_limit)

        if log:
            self.console.print(
                f"{self.icons['balance']} Balanced digest selected "
                f"{len(selected)}/{len(items)} items"
            )
            for group_key, group in groups.items():
                label = group.name or group_key
                self.console.print(
                    f"      {self.icons['detail']} {label}: "
                    f"{final_counts.get(group_key, 0)}/{group.limit}"
                )
            if (
                final_counts.get(default_group, 0)
                or digest.default_group_limit is not None
            ):
                limit_label = (
                    str(digest.default_group_limit)
                    if digest.default_group_limit is not None
                    else "unlimited"
                )
                self.console.print(
                    f"      {self.icons['detail']} {default_group}: "
                    f"{final_counts.get(default_group, 0)}/{limit_label}"
                )
            self.console.print("")

        return BalancedDigestResult(
            items=[item for item, _ in selected],
            enabled=True,
            group_counts=dict(final_counts),
            group_limits=group_limits,
            duplicate_categories=sorted(set(duplicate_categories)),
            rebalanced_count=rebalanced_count,
        )

    async def _expand_twitter_discussion(self, items: List[ContentItem]) -> None:
        """Second-stage: fetch reply text for important Twitter items and re-analyze.

        Only runs when sources.twitter.fetch_reply_text is True.
        Bounded by max_tweets_to_expand to control cost.
        """
        tw_cfg = self.config.sources.twitter
        if not tw_cfg or not tw_cfg.enabled or not tw_cfg.fetch_reply_text:
            return

        from .models import SourceType

        twitter_items = [
            item for item in items
            if item.source_type == SourceType.TWITTER
        ][:tw_cfg.max_tweets_to_expand]

        if not twitter_items:
            return

        self.console.print(
            f"{self.icons['discussion']} Fetching reply text for "
            f"{len(twitter_items)} Twitter items..."
        )

        async with httpx.AsyncClient(timeout=30.0) as client:
            if tw_cfg.mode == "playwright":
                self.console.print(
                    "   [yellow]Reply expansion not yet supported in Playwright mode.[/yellow]"
                )
                return
            scraper = TwitterScraper(tw_cfg, client)
            expanded = []
            for item in twitter_items:
                try:
                    reply_lines = await scraper.fetch_replies_for_item(item)
                    if TwitterScraper.append_discussion_content(item, reply_lines):
                        expanded.append(item)
                        self.console.print(
                            f"   {self.icons['discussion']} {len(reply_lines)} replies "
                            f"added to: {item.title[:60]}"
                        )
                except Exception as exc:
                    self.console.print(
                        f"   [yellow]{self.icons['warning']} Reply fetch failed for "
                        f"{item.id}: {exc}[/yellow]"
                    )

        if not expanded:
            return

        self.console.print(
            f"   Re-analyzing {len(expanded)} Twitter items with reply context...\n"
        )
        ai_client = create_ai_client(self.config.ai)
        analyzer = ContentAnalyzer(ai_client, self.profiles, console=self.console)
        await analyzer.analyze_batch(expanded)

    async def enrich_items(self, items: List[ContentItem]) -> EnrichmentBatchResult:
        """Enrich items with background knowledge (2nd AI pass).

        For each item that passed the score threshold, call AI to generate
        background knowledge based on the item's actual content.

        Args:
            items: Important items to enrich (modified in-place)
        """
        if not items:
            return EnrichmentBatchResult()

        self.console.print(
            f"{self.icons['enrich']} Enriching with background knowledge..."
        )
        ai_client = create_ai_client(self.config.ai)
        self._remember_ai_client(ai_client)
        enricher = ContentEnricher(
            ai_client,
            self.profiles,
            self.config.ai.languages,
            console=self.console,
            tools=ToolRegistry(self.storage.summaries_dir),
        )
        result = await enricher.enrich_batch(items)
        self.console.print(
            f"   Enriched {result.succeeded_count}/{len(items)} items"
        )
        if result.title_fallback_count:
            self.console.print(
                f"   [yellow]标题护栏：{result.title_fallback_count} 条占位标题已回退"
                f"源标题：{', '.join(result.title_fallback_ids)}[/yellow]"
            )
        if result.failed_count:
            self.console.print(
                f"   [yellow]Skipped {result.failed_count} items after enrichment "
                f"failed: {', '.join(result.failed_ids)}[/yellow]"
            )
        self.console.print("")
        return result

    async def analyze_items(self, items: List[ContentItem]) -> List[ContentItem]:
        """Analyze content items with AI.

        Args:
            items: Items to analyze

        Returns:
            List[ContentItem]: Analyzed items
        """
        self.console.print(f"{self.icons['ai']} Analyzing content with AI...")

        ai_client = create_ai_client(self.config.ai)
        self._remember_ai_client(ai_client)
        analyzer = ContentAnalyzer(ai_client, self.profiles, console=self.console)

        return await analyzer.analyze_batch(items)

    async def _generate_summary(
        self,
        items: List[ContentItem],
        date: str,
        total_fetched: int,
        language: str = "en",
    ) -> str:
        """Generate daily summary.

        Args:
            items: Important items to include (already enriched with background/related)
            date: Date string
            total_fetched: Total items fetched
            language: Output language ("en" or "zh")

        Returns:
            str: Markdown summary
        """
        self.console.print(f"{self.icons['summary']} Generating daily summary...")

        summarizer = DailySummarizer(
            profile_names=self.profiles.names,
            profile_order=self.config.digest.profile_order,
        )

        return await summarizer.generate_summary(items, date, total_fetched, language=language)
