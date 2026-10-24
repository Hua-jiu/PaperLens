"""Document workflows. All background results are cached in the local store."""

from __future__ import annotations

import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from .ai_service import AIService, ConfigurationError, ModelResponseError
from .pdf_engine import (DEFAULT_VISION_SCALE, EXTRACTION_VERSION, annotate_translation_scopes, apply_layout_review,
                         apply_vision_layout, extract_document, layout_review_candidates, plan_regions,
                         read_page_lines, read_page_resources, render_page)
from .storage import DEFAULT_MODULES, Store, timestamp

MAX_UPLOAD_BYTES = 60 * 1024 * 1024
TRANSLATION_BATCH_SIZE = 8
# Pages the layout planner arranges per document, one model request each.
DEFAULT_PLAN_MAX_PAGES = 40


class APIError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def serialized_document(function):
    """Keep replacements and AI results for one paper in the same layout."""
    @wraps(function)
    def run(self, paper_id: str):
        with self.document_locks_lock:
            lock = self.document_locks.get(paper_id)
            if lock is None:
                lock = self.document_locks[paper_id] = threading.RLock()
        with lock:
            return function(self, paper_id)
    return run


class Application:
    def __init__(self, data_dir: Path, dryrun: bool = False):
        self.dryrun = dryrun
        self.store = Store(data_dir, dryrun=dryrun)
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="paperlens")
        self.jobs: set[tuple[str, str]] = set()
        self.jobs_lock = threading.Lock()
        self.closing = False
        self.translation_scope_cache: dict[str, dict] = {}
        self.translation_scope_lock = threading.Lock()
        self.layout_upgrade_failures: set[str] = set()
        # Initialize before resuming persisted jobs. Retain each lock for this
        # application lifetime, including after deletion, so a late worker can
        # never acquire a different lock for the same document ID.
        self.document_locks: dict[str, threading.RLock] = {}
        self.document_locks_lock = threading.Lock()
        for paper in self.store.list_papers():
            if paper["status"] == "processing":
                self.submit(paper["id"], "parse", self.parse)
            elif paper["status"] == "ready" and paper.get("ai_status") in ("pending", "processing"):
                self.submit(paper["id"], "ai", self.enrich)
            if paper.get("translation_status") in ("queued", "processing"):
                self.submit(paper["id"], "translate", self.translate)
            for item in self.store.get(paper["id"])["highlights"]:
                if item.get("translation_status") in ("pending", "processing"):
                    self.queue_highlight(paper["id"], item["id"])

    def service(self) -> AIService:
        return AIService(self.store.settings(), dryrun=self.dryrun)

    def detail(self, paper_id: str) -> dict:
        """Read cached content and queue one local upgrade for older layouts."""
        record = self.store.get(paper_id)
        document = record["document"]
        paper = record["paper"]
        if (paper.get("status") == "ready" and document.get("extraction_version", 0) < EXTRACTION_VERSION
                and paper_id not in self.layout_upgrade_failures
                and paper.get("ai_status") not in ("pending", "processing")
                and paper.get("translation_status") not in ("pending", "processing")):
            self.submit(paper_id, "layout", self.upgrade_layout)
        if document.get("translation_scope_version") == 1 and all(
                "visual_regions" in figure for figure in document.get("figures", [])) and all(
                "translation_scope" in block for page in document.get("pages", []) for block in page.get("blocks", [])):
            apply_translation_scope_metadata(document, translation_scope_metadata(document))
            return update_translation_state(record)
        signature = translation_layout_signature(document)
        with self.translation_scope_lock:
            cached = self.translation_scope_cache.get(paper_id)
            if not cached or cached["signature"] != signature:
                try:
                    source = self.store.source(paper_id) if any(
                        "visual_regions" not in figure for figure in document.get("figures", [])) else None
                    annotate_translation_scopes(document, source)
                except (ValueError, OSError):
                    # A damaged original should not prevent opening its cached
                    # text. Only explicit graphic types can be excluded safely.
                    annotate_translation_scopes(document)
                cached = {"signature": signature, "metadata": translation_scope_metadata(document)}
                self.translation_scope_cache[paper_id] = cached
            apply_translation_scope_metadata(document, cached["metadata"])
        return update_translation_state(record)

    @serialized_document
    def upgrade_layout(self, paper_id: str) -> None:
        """Repair local geometry while retaining current annotations and AI caches."""
        try:
            if self.store.get(paper_id)["document"].get("extraction_version", 0) >= EXTRACTION_VERSION:
                return
            source = self.store.source(paper_id)
            extracted = extract_document(source)
            # Pages a model planned or re-segmented keep that structure.
            saved = self.store.get(paper_id)["document"].get("vision_layout", {})
            for number, regions in saved.items():
                try:
                    apply_vision_layout(extracted, read_page_lines(source, int(number)), int(number), regions)
                except (ValueError, StopIteration):
                    continue
            extracted["vision_layout"] = saved

            def merge(record):
                current = record["document"]
                if current.get("extraction_version", 0) >= EXTRACTION_VERSION:
                    return
                record["document"] = merge_extracted_document(current, extracted)
                remap_document_anchors(record, current)
                record["paper"]["page_count"] = extracted["page_count"]
                record["paper"].pop("layout_error", None)
                update_translation_state(record)

            self.store.update(paper_id, merge)
            self.layout_upgrade_failures.discard(paper_id)
            with self.translation_scope_lock:
                self.translation_scope_cache.pop(paper_id, None)
        except KeyError:
            return
        except Exception as exc:
            self.layout_upgrade_failures.add(paper_id)
            try:
                self.update_paper(paper_id, layout_error=safe_error(exc))
            except KeyError:
                pass

    def delete(self, paper_id: str) -> None:
        self.store.delete(paper_id)
        self.layout_upgrade_failures.discard(paper_id)
        with self.translation_scope_lock:
            self.translation_scope_cache.pop(paper_id, None)

    def module_configured(self, name: str) -> bool:
        settings = self.store.settings()
        module = settings["modules"].get(name, {})
        provider = next((p for p in settings["providers"] if p["id"] == module.get("provider_id")), None)
        return bool(provider and provider.get("base_url") and provider.get("api_key")
                    and (module.get("model") or provider.get("default_model")))

    def require_module(self, name: str) -> None:
        if not self.module_configured(name):
            raise APIError("请先在右上角设置中配置该模块的供应商和模型", 503)
        if self.dryrun:
            raise APIError("当前为 dryrun 验证模式，AI 网络请求已停用", 503)

    def start_translation(self, paper_id: str) -> dict:
        """Resume missing text, or report the existing job without hiding a click."""
        record = self.detail(paper_id)
        if record["paper"].get("status") != "ready":
            raise APIError("请等待文献解析完成", 409)
        needs_layout_upgrade = record["document"].get("extraction_version", 0) < EXTRACTION_VERSION
        coverage = translation_coverage(record["document"])
        # Older layouts may contain split sentences or no useful prose at all.
        # Let the serialized worker prepare current paragraphs before deciding
        # whether there is anything left to translate.
        if not needs_layout_upgrade and not coverage["translation_total"]:
            message = "未检测到可翻译的正文或图注；扫描件需要先进行 OCR，可先查看原版 PDF。"
            self.update_paper(paper_id, translation_status="error", translation_error=message,
                              **coverage)
            raise APIError(message, 422)
        if not needs_layout_upgrade and not coverage["translation_remaining"]:
            self.store.update(paper_id, lambda current: update_translation_state(current, "complete"))
            return {"ok": True, "queued": False, "already_complete": True,
                    "paper": self.detail(paper_id)["paper"]}
        try:
            self.require_module("translation")
        except APIError as exc:
            status = "needs_configuration" if not self.module_configured("translation") else "error"
            self.update_paper(paper_id, translation_status=status, translation_error=str(exc), **coverage)
            raise
        try:
            queued = self.submit(
                paper_id, "translate", self.translate,
                on_queued=lambda: self.store.update(
                    paper_id, lambda current: update_translation_state(current, "queued")),
            )
        except RuntimeError as exc:
            self.update_paper(paper_id, translation_status="error", translation_error="后台任务暂不可用，请稍后重试。")
            raise APIError("后台任务暂不可用，请稍后重试。", 503) from exc
        if not queued:
            with self.jobs_lock:
                active = (paper_id, "translate") in self.jobs
            if not active:
                raise APIError("阅读器正在关闭，请重新启动后再翻译。", 503)
        return {"ok": True, "queued": queued, "already_complete": False,
                "paper": self.detail(paper_id)["paper"]}

    def submit(self, paper_id: str, kind: str, function, on_queued=None) -> bool:
        key = (paper_id, kind)
        with self.jobs_lock:
            if self.closing or key in self.jobs:
                return False
            self.jobs.add(key)
            try:
                if on_queued:
                    on_queued()
            except Exception:
                self.jobs.discard(key)
                raise

        def work():
            try:
                function(paper_id)
            except KeyError:
                pass  # A deleted paper must never be resurrected by a worker.
            except Exception:
                # Workflow functions record useful errors without exposing credentials.
                pass
            finally:
                with self.jobs_lock:
                    self.jobs.discard(key)

        try:
            self.pool.submit(work)
        except RuntimeError:
            with self.jobs_lock:
                self.jobs.discard(key)
            if not self.closing:
                raise
            return False
        return True

    def update_paper(self, paper_id: str, **fields) -> dict:
        return self.store.update(paper_id, lambda r: r["paper"].update(fields))["paper"]

    def upload(self, filename: str, data: bytes) -> dict:
        if not filename.lower().endswith(".pdf"):
            raise APIError("请选择 PDF 文件")
        if not data or len(data) > MAX_UPLOAD_BYTES:
            raise APIError("PDF 不能为空且大小不能超过 60 MB")
        if b"%PDF-" not in data[:1024]:
            raise APIError("文件内容不是有效的 PDF")
        record, created = self.store.add(Path(filename).name, data)
        if created:
            self.submit(record["paper"]["id"], "parse", self.parse)
        return {"paper": record["paper"], "duplicate": not created}

    def import_path(self, path: str) -> dict:
        if self.dryrun:
            raise APIError("dryrun 模式仅接受内存上传，不读取本机文件地址")
        source = Path(path.strip().strip('"').strip("'")).expanduser()
        if not source.is_file() or source.suffix.lower() != ".pdf":
            raise APIError("该地址不存在或不是 PDF 文件")
        if source.stat().st_size > MAX_UPLOAD_BYTES:
            raise APIError("PDF 大小不能超过 60 MB")
        return self.upload(source.name, source.read_bytes())

    @serialized_document
    def parse(self, paper_id: str) -> None:
        try:
            self.update_paper(paper_id, status="processing", stage="提取正文与图表", progress=5, error="")
            document = extract_document(
                self.store.source(paper_id),
                progress=lambda percent: self.update_paper(paper_id, progress=min(95, 5 + round(percent * .9))),
            )
            document["reading_points"] = []
            def parsed(record):
                record["document"] = document
                record["paper"].update(
                    title=document.get("title") or record["paper"]["title"], authors=document.get("authors", ""),
                    abstract=document.get("abstract", ""), page_count=document["page_count"],
                    status="ready", stage="本地重排已完成", progress=100,
                )
            self.store.update(paper_id, parsed)
            self.submit(paper_id, "ai", self.enrich)
        except KeyError:
            return
        except Exception as exc:
            self.update_paper(paper_id, status="error", stage="解析失败", error=safe_error(exc), progress=0)

    @serialized_document
    def enrich(self, paper_id: str) -> None:
        if not any(self.module_configured(name) for name in ("summary", "translation", "reflow")):
            self.update_paper(paper_id, ai_status="needs_configuration", stage="可阅读 · 待配置 AI 模型", ai_error="")
            return
        if self.dryrun:
            self.update_paper(paper_id, ai_status="needs_configuration", stage="本地重排完成 · dryrun 模式", ai_error="dryrun 模式不发送 AI 请求")
            return
        if self.store.get(paper_id)["document"].get("extraction_version", 0) < EXTRACTION_VERSION:
            self.upgrade_layout(paper_id)
            if self.store.get(paper_id)["document"].get("extraction_version", 0) < EXTRACTION_VERSION:
                self.update_paper(paper_id, ai_status="partial", stage="段落整理待重试",
                                  ai_error="段落结构更新失败，已保留原文；请重新整理文献后再试。")
                return
        self.update_paper(paper_id, ai_status="processing", stage="处理 AI 阅读任务", ai_error="")
        failures = []
        service = self.service()
        if self.module_configured("summary"):
            try:
                self.update_paper(paper_id, stage="生成中文总结")
                record = self.store.get(paper_id)
                result = service.summarize(record["document"])
                def save_summary(current):
                    current["paper"].update(title_zh=result["title_zh"], summary=result["summary"])
                    current["document"]["reading_points"] = result.get("reading_points", [])
                self.store.update(paper_id, save_summary)
            except Exception as exc:
                failures.append(safe_error(exc))
        if self.module_configured("reflow"):
            try:
                self.update_paper(paper_id, stage="AI 抽查版面解析")
                failures.extend(self.inspect_layout(paper_id, service))
            except Exception as exc:
                failures.append(safe_error(exc))
            try:
                failures.extend(self.plan_layout(paper_id, service))
            except Exception as exc:
                failures.append(safe_error(exc))
            try:
                self.update_paper(paper_id, stage="AI 校对段落与参考文献")
                self.review_layout(paper_id, service)
                failures.extend(service.review_warnings)
            except Exception as exc:
                failures.append(safe_error(exc))
            try:
                self.update_paper(paper_id, stage="AI 优化段落与阅读顺序")
                document = self.store.get(paper_id)["document"]
                for page in document["pages"]:
                    # A model-rebuilt page already carries its reading order.
                    if not page["blocks"] or page.get("vision_layout"):
                        continue
                    try:
                        blocks = service.reflow_blocks(page["blocks"])
                    except ModelResponseError as exc:
                        # A malformed answer on one page must not prevent the
                        # remaining pages from being processed. Provider and
                        # configuration errors still stop this workflow.
                        failures.append(f"第 {page['number']} 页：{safe_error(exc)}")
                        continue
                    failures.extend(getattr(service, "reflow_warnings", []))
                    def save_reflow(current):
                        target = next(p for p in current["document"]["pages"] if p["number"] == page["number"])
                        previous = {b["id"]: b for b in target["blocks"]}
                        # Keep already translated paragraphs if a translation job ran concurrently.
                        for block in blocks:
                            old = previous.get(block["id"], {})
                            if old.get("translation_scope"):
                                block["translation_scope"] = old["translation_scope"]
                            if block.get("translation_scope") == "figure_content":
                                block.pop("text_zh", None)
                            elif old.get("text_zh"):
                                block["text_zh"] = old["text_zh"]
                        target["blocks"] = blocks
                    self.store.update(paper_id, save_reflow)
            except Exception as exc:
                failures.append(safe_error(exc))
        # Captions are translated after the layout steps have settled the figures.
        if self.module_configured("translation"):
            try:
                self.update_paper(paper_id, stage="翻译目录与图注")
                document = self.store.get(paper_id)["document"]
                items = [{"id": "toc:" + item["id"], "text": item["title"]} for item in document["toc"]]
                items += [{"id": "fig:" + item["id"], "text": item["caption"]} for item in document["figures"] if item.get("caption")]
                for batch in batches(items):
                    translated = service.translate_batch(batch)
                    def save_metadata(current):
                        for item in current["document"]["toc"]:
                            item["title_zh"] = translated.get("toc:" + item["id"], item.get("title_zh", ""))
                        for item in current["document"]["figures"]:
                            item["caption_zh"] = translated.get("fig:" + item["id"], item.get("caption_zh", ""))
                    self.store.update(paper_id, save_metadata)
            except Exception as exc:
                failures.append(safe_error(exc))
        self.update_paper(paper_id, ai_status="partial" if failures else "complete", stage="可阅读 · 部分 AI 任务失败" if failures else "准备就绪", ai_error="；".join(failures))

    def inspect_layout(self, paper_id: str, service: AIService) -> list[str]:
        """Spot-check locally flagged pages; plan_layout rebuilds the broken ones.

        Returns warnings for pages that could not be inspected.
        """
        document = self.store.get(paper_id)["document"]
        inspected = set(document.get("layout_inspection", {}).get("inspected", []))
        pages = [{"page": page["number"], "signals": page["layout_flags"],
                  "blocks": [{"type": block.get("type", "paragraph"), "text": block.get("text", "")}
                             for block in page.get("blocks", []) if not block.get("hidden") and block.get("text")]}
                 for page in document.get("pages", [])
                 if page.get("layout_flags") and not page.get("vision_layout") and page["number"] not in inspected]
        if not pages:
            return []
        broken = service.inspect_pages(pages)
        warnings = list(service.inspection_warnings)
        judged = [page["page"] for page in pages if not any(str(page["page"]) in warning for warning in warnings)]

        def save_inspection(record):
            state = record["document"].setdefault("layout_inspection", {"inspected": [], "broken": {}})
            state["inspected"] = sorted(set(state["inspected"]) | set(judged))
            state["broken"].update({str(number): reason for number, reason in broken.items()})

        self.store.update(paper_id, save_inspection)
        return warnings

    def plan_layout(self, paper_id: str, service: AIService) -> list[str]:
        """Let the model arrange pages whose figures or structure need it.

        Pages holding figures, captions, figure text or a local or AI warning
        are planned from their parsed resources; pages a model already
        rebuilt are kept. A page the spot check found broken falls back to
        the vision model when its plan is unusable. Returns warnings for
        broken pages left as extracted.
        """
        document = self.store.get(paper_id)["document"]
        figure_pages = {int(figure.get("page", 0)) for figure in document.get("figures", [])}
        broken = {int(number) for number in document.get("layout_inspection", {}).get("broken", {})}
        pages = [page["number"] for page in document.get("pages", []) if not page.get("vision_layout") and (
            page["number"] in figure_pages | broken or page.get("layout_flags")
            or any(block.get("type") == "caption" or block.get("translation_scope") == "figure_content"
                   for block in page.get("blocks", [])))]
        warnings = []
        for number in pages[:DEFAULT_PLAN_MAX_PAGES]:
            self.update_paper(paper_id, stage=f"AI 规划第 {number} 页版面")
            try:
                self.arrange_page(paper_id, number, service, vision=number in broken)
            except (ModelResponseError, ValueError) as exc:
                # One unusable plan leaves that page as extracted; provider
                # and configuration errors stop the remaining pages. A page
                # that only holds figures reads acceptably as extracted.
                if number in broken:
                    warnings.append(f"第 {number} 页版面重新整理未采用：{safe_error(exc)}")
        return warnings

    def arrange_page(self, paper_id: str, page: int, service: AIService | None = None, vision: bool = True) -> None:
        """Plan a page from its text; with ``vision``, fall back to the vision model.

        The plan works from the PDF's own text and graphics and is checked
        group by group, so it comes first. Raises the last failure.
        """
        if not self.module_configured("reflow"):
            return self.revise_page_layout(paper_id, page, service)
        try:
            self.plan_page_layout(paper_id, page, service)
        except (ModelResponseError, ValueError):
            if not (vision and self.module_configured("vision")):
                raise
            self.update_paper(paper_id, stage=f"AI 重新识别第 {page} 页版面")
            self.revise_page_layout(paper_id, page, service)

    def plan_page_layout(self, paper_id: str, page: int, service: AIService | None = None) -> None:
        """Arrange one page with the text model from its parsed resources."""
        service = service or self.service()
        resources = read_page_resources(self.store.source(paper_id), page)
        document = self.store.get(paper_id)["document"]
        linked = {figure.get("caption_block_id") for figure in document.get("figures", [])}
        # A figure's caption may sit on the facing page.
        captions = [{"page": other["number"], "text": block["text"]} for other in document.get("pages", [])
                    if abs(other["number"] - page) == 1 for block in other.get("blocks", [])
                    if block.get("type") == "caption" and block["id"] not in linked and block.get("text")]
        plan = service.plan_page({**resources, "page": page, "captions": captions})
        self.save_page_layout(paper_id, page, resources["lines"], plan_regions(resources, plan))

    def revise_page_layout(self, paper_id: str, page: int, service: AIService | None = None) -> None:
        """Re-segment one page with the vision model and store its regions."""
        service = service or self.service()
        source = self.store.source(paper_id)
        regions = service.segment_page(render_page(source, page, DEFAULT_VISION_SCALE))
        # PDF access stays outside the store lock; the locks are never nested
        # in the reverse order.
        self.save_page_layout(paper_id, page, read_page_lines(source, page), regions)

    def save_page_layout(self, paper_id: str, page: int, lines: list[dict], regions: list[dict]) -> None:
        """Rebuild a page from model regions; a trial run rejects them before saving.

        Vision and planned regions share ``vision_layout`` so both are applied
        again after a re-extraction.
        """
        apply_vision_layout(copy.deepcopy(self.store.get(paper_id)["document"]), lines, page, regions)

        def save_regions(record):
            before = copy.deepcopy(record["document"])
            apply_vision_layout(record["document"], lines, page, regions)
            record["document"].setdefault("vision_layout", {})[str(page)] = regions
            record["document"].get("layout_inspection", {}).get("broken", {}).pop(str(page), None)
            remap_document_anchors(record, before)
            update_translation_state(record)

        self.store.update(paper_id, save_regions)

    def queue_page_layout(self, paper_id: str, page: int) -> dict:
        self.require_module("reflow" if self.module_configured("reflow") else "vision")
        record = self.store.get(paper_id)
        if not 1 <= page <= max(1, record["paper"].get("page_count") or 0):
            raise APIError("页码超出范围")

        def work(identifier: str) -> None:
            try:
                self.arrange_page(identifier, page)
                self.update_paper(identifier, ai_status="complete", stage="准备就绪", ai_error="")
            except Exception as exc:
                self.update_paper(identifier, ai_status="partial", stage="可阅读 · 部分 AI 任务失败",
                                  ai_error=f"第 {page} 页版面重新识别失败：{safe_error(exc)}")

        queued = self.submit(paper_id, f"vision-{page}", work, on_queued=lambda: self.update_paper(
            paper_id, ai_status="processing", stage=f"AI 重新识别第 {page} 页版面", ai_error=""))
        return {"ok": True, "queued": queued}

    def review_layout(self, paper_id: str, service: AIService) -> None:
        """Ask the model about uncertain boundaries and store its decisions.

        Decisions identify blocks by page and text, so a later re-extraction
        applies them again. Rejected candidates are remembered as reviewed.
        """
        service.review_warnings = []
        candidates = layout_review_candidates(self.store.get(paper_id)["document"])
        if not candidates:
            return
        review = service.review_layout(candidates)
        decided = [candidate for candidate in candidates if candidate["id"] in review["decided"]]
        accepted = []
        for candidate in decided:
            if candidate["kind"] == "join" and candidate["id"] in review["join"]:
                accepted.append({"kind": "join", "before": candidate["before"], "after": candidate["after"]})
            elif candidate["kind"] == "split" and candidate["id"] in review["split"]:
                accepted.append({"kind": "split", "block": candidate["block"], "starts": review["split"][candidate["id"]]})
            elif (candidate["kind"] == "role" and candidate["id"] in review["role"]
                  and review["role"][candidate["id"]] != {"paragraph": "body"}.get(candidate["type"], candidate["type"])):
                accepted.append({"kind": "role", "block": candidate["block"], "role": review["role"][candidate["id"]]})

        def save_review(record):
            document = record["document"]
            state = document.setdefault("layout_review", {"decisions": [], "reviewed": []})
            state["reviewed"] = list(dict.fromkeys(state["reviewed"] + [candidate["signature"] for candidate in decided]))
            state["decisions"].extend(accepted)
            previous = copy.deepcopy(document)
            if apply_layout_review(document, accepted):
                remap_document_anchors(record, previous)
                update_translation_state(record)

        self.store.update(paper_id, save_review)

    @serialized_document
    def translate(self, paper_id: str) -> None:
        try:
            if self.store.get(paper_id)["document"].get("extraction_version", 0) < EXTRACTION_VERSION:
                # This worker already owns the document's reentrant lock. The
                # local upgrade can run here without blocking the HTTP caller
                # or racing a queued layout replacement against model results.
                self.upgrade_layout(paper_id)
                if self.store.get(paper_id)["document"].get("extraction_version", 0) < EXTRACTION_VERSION:
                    raise ValueError("文献段落结构更新失败，原文和已有译文已保留；请重新整理文献后再尝试全文翻译。")
            self.require_module("translation")
            self.store.update(paper_id, lambda current: update_translation_state(current, "processing"))
            document = self.detail(paper_id)["document"]
            metadata = translation_scope_metadata(document)

            def prepare_translation(record):
                # Metadata was computed outside the store lock: PDF extraction
                # and persistence must never acquire their locks in reverse.
                current = record["document"]
                apply_translation_scope_metadata(current, metadata)
                blocks = {block["id"]: block for page in current["pages"] for block in page["blocks"]}
                for group in paragraph_translation_groups(current):
                    # Fragment translations are not a translation of the
                    # complete paragraph and must never be shown between its
                    # source fragments or counted as a completed group.
                    for identifier in group["block_ids"]:
                        blocks[identifier].pop("text_zh", None)
                caption_blocks = figure_caption_blocks(current)
                for figure in current.get("figures", []):
                    block = blocks.get(caption_blocks.get(figure["id"]))
                    cached = figure.get("caption_zh") or (block or {}).get("text_zh")
                    if figure.get("caption") and cached:
                        figure["caption_zh"] = cached
                        if block is not None:
                            block["text_zh"] = cached

            document = self.store.update(paper_id, prepare_translation)["document"]
            targets = translation_targets(document)
            items = [{"id": item["id"], "text": item["text"]}
                     for item in targets if not item["translated"]]
            if not targets:
                raise ValueError("未检测到可翻译的正文或图注；扫描件需要先进行 OCR，可先查看原版 PDF。")
            service = self.service()
            for batch in batches(items):
                translations = service.translate_batch(batch)
                translations = {item["id"]: str(translations[item["id"]]).strip()
                                for item in batch if isinstance(translations, dict)
                                and isinstance(translations.get(item["id"]), str)
                                and translations[item["id"]].strip()}
                def save_translation(record):
                    current = record["document"]
                    apply_translation_scope_metadata(current, metadata)
                    groups = paragraph_translation_groups(current)
                    grouped = {identifier for group in groups for identifier in group["block_ids"]}
                    for page in current["pages"]:
                        for block in page["blocks"]:
                            if block["id"] in translations and block["id"] not in grouped and block.get("translation_scope") != "figure_content":
                                block["text_zh"] = translations[block["id"]]
                    for group in groups:
                        translation = translations.get("para:" + group["id"])
                        if translation:
                            group["text_zh"] = translation
                    by_id = {block["id"]: block for page in current["pages"] for block in page["blocks"]}
                    linked = figure_caption_blocks(current)
                    for figure in current.get("figures", []):
                        translation = translations.get("fig:" + figure["id"])
                        if translation:
                            figure["caption_zh"] = translation
                            block = by_id.get(linked.get(figure["id"]))
                            if block is not None:
                                block["text_zh"] = translation
                    update_translation_state(record, "processing")
                self.store.update(paper_id, save_translation)
                if len(translations) != len(batch):
                    raise ValueError("模型未返回本批次的全部译文；已保存成功部分，点击全文翻译可继续。")
            remaining = translation_coverage(self.store.get(paper_id)["document"])["translation_remaining"]
            if remaining:
                raise ValueError("部分正文尚未翻译完成；已保存成功部分，点击全文翻译可继续。")
            self.store.update(paper_id, lambda current: update_translation_state(current, "complete"))
        except KeyError:
            return
        except Exception as exc:
            status = "needs_configuration" if isinstance(exc, ConfigurationError) or (
                isinstance(exc, APIError) and not self.module_configured("translation")) else "error"
            def failed(current):
                update_translation_state(current, status)
                current["paper"]["translation_error"] = safe_error(exc)
            try:
                self.store.update(paper_id, failed)
            except KeyError:
                pass

    def translate_highlight(self, paper_id: str, highlight_id: str) -> None:
        try:
            record = self.store.get(paper_id)
            highlight = next(h for h in record["highlights"] if h["id"] == highlight_id)
            if not self.module_configured("translation") or self.dryrun:
                return
            translation = self.service().translate_text(highlight["text"])
            def save(current):
                for item in current["highlights"]:
                    if item["id"] == highlight_id:
                        item.update(text_zh=translation, translation_status="complete")
            self.store.update(paper_id, save)
        except Exception as exc:
            def failed(current):
                for item in current["highlights"]:
                    if item["id"] == highlight_id:
                        item.update(translation_status="error", translation_error=safe_error(exc))
            try:
                self.store.update(paper_id, failed)
            except KeyError:
                pass

    def queue_highlight(self, paper_id: str, highlight_id: str) -> bool:
        return self.submit(paper_id, "highlight:" + highlight_id,
                           lambda identifier: self.translate_highlight(identifier, highlight_id))

    def add_highlight(self, paper_id: str, payload: dict) -> dict:
        text = str(payload.get("text", "")).strip()
        if not text or len(text) > 15000:
            raise APIError("高亮内容不能为空且不能超过 15000 字符")
        record = self.store.get(paper_id)
        page = max(1, min(int(payload.get("page", 1)), max(1, record["paper"]["page_count"])))
        item = {
            "id": uuid4().hex, "text": text, "text_zh": "", "page": page,
            "block_id": str(payload.get("block_id", "")), "note": str(payload.get("note", ""))[:10000],
            "color": normalize_color(payload.get("color")), "created_at": timestamp(),
            "translation_status": "pending" if self.module_configured("translation") and not self.dryrun else "needs_configuration",
        }
        self.store.update(paper_id, lambda r: r["highlights"].append(item))
        self.queue_highlight(paper_id, item["id"])
        return {"highlight": item}

    def save_settings(self, payload: dict) -> dict:
        providers = payload.get("providers")
        if not isinstance(providers, list) or len(providers) > 20:
            raise APIError("供应商配置格式不正确，最多支持 20 个供应商")
        previous_settings = self.store.settings()
        old = {p["id"]: p for p in previous_settings["providers"]}
        clean = []
        seen = set()
        for provider in providers:
            provider_id = str(provider.get("id") or uuid4().hex)
            if provider_id in seen:
                raise APIError("供应商 ID 不能重复")
            seen.add(provider_id)
            name = str(provider.get("name", "")).strip()
            url = str(provider.get("base_url", "")).strip().rstrip("/")
            parsed = urlsplit(url)
            if not name or parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query:
                raise APIError("请填写供应商名称和有效的 HTTP(S) 访问 URL")
            key = str(provider.get("api_key", "")).strip() or old.get(provider_id, {}).get("api_key", "")
            if provider.get("clear_key"):
                key = ""
            default_model = str(provider.get("default_model") or "").strip()
            if len(default_model) > 200:
                raise APIError("默认模型名称不能超过 200 字符")
            clean.append({"id": provider_id, "name": name[:120], "base_url": url, "api_key": key, "default_model": default_model})
        by_id = {provider["id"]: provider for provider in clean}
        modules = {}
        for name in DEFAULT_MODULES:
            module = payload.get("modules", {}).get(name, {})
            provider_id = str(module.get("provider_id", ""))
            model = str(module.get("model", "")).strip()
            if provider_id and provider_id not in seen:
                raise APIError("模块引用了不存在的供应商")
            if provider_id and not model and not by_id[provider_id]["default_model"]:
                raise APIError("请填写模块模型名称，或先设置该供应商的默认模型")
            modules[name] = {"provider_id": provider_id, "model": model[:200]}
        new_settings = {"providers": clean, "modules": modules}
        changed = any(new_settings[key] != previous_settings[key] for key in ("providers", "modules"))
        result = self.store.save_settings(new_settings)
        for paper in self.store.list_papers():
            if paper["status"] == "ready" and (changed or paper.get("ai_status") in ("needs_configuration", "pending", "error", "partial")):
                self.submit(paper["id"], "ai", self.enrich)
            for item in self.store.get(paper["id"])["highlights"]:
                if not item.get("text_zh"):
                    self.queue_highlight(paper["id"], item["id"])
        return result

    def close(self):
        with self.jobs_lock:
            self.closing = True
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.store.close()


def merge_extracted_document(current: dict, extracted: dict) -> dict:
    """Merge against the latest record, so concurrent notes or translations survive."""
    review = current.get("layout_review")
    if review:
        # AI layout decisions name blocks by text and apply to the new layout.
        extracted["layout_review"] = review
        apply_layout_review(extracted, review.get("decisions", []))
    normalized = lambda text: " ".join(str(text or "").split())
    translations = {(page["number"], normalized(block.get("text"))): block.get("text_zh", "")
                    for page in current.get("pages", []) for block in page.get("blocks", []) if block.get("text_zh")}
    for page in extracted.get("pages", []):
        for block in page.get("blocks", []):
            cached = translations.get((page["number"], normalized(block.get("text"))))
            if cached and block.get("translation_scope") != "figure_content":
                block["text_zh"] = cached
    old_pages = {block["id"]: page["number"] for page in current.get("pages", [])
                 for block in page.get("blocks", [])}
    new_blocks = {block["id"]: block for page in extracted.get("pages", [])
                  for block in page.get("blocks", [])}
    new_pages = {block["id"]: page["number"] for page in extracted.get("pages", [])
                 for block in page.get("blocks", [])}
    paragraph_cache = {
        (tuple(dict.fromkeys(old_pages[identifier] for identifier in group["block_ids"])), normalized(group["text"])): group["text_zh"]
        for group in paragraph_translation_groups(current) if group.get("text_zh")
    }
    for group in paragraph_translation_groups(extracted):
        pages = tuple(dict.fromkeys(new_pages[identifier] for identifier in group["block_ids"]))
        cached = paragraph_cache.get((pages, normalized(group["text"])))
        # An older layout may already have held the entire paragraph as one
        # block. Reuse only an exact full-text translation, never its pieces.
        cached = cached or translations.get((pages[0], normalized(group["text"])))
        if cached:
            group["text_zh"] = cached
        else:
            group.pop("text_zh", None)
        for identifier in group["block_ids"]:
            new_blocks[identifier].pop("text_zh", None)
    captions = {(figure.get("page"), normalized(figure.get("caption"))): figure.get("caption_zh", "")
                for figure in current.get("figures", []) if figure.get("caption_zh") and figure.get("caption")}
    for figure in extracted.get("figures", []):
        cached = captions.get((figure.get("page"), normalized(figure.get("caption"))))
        if cached:
            figure["caption_zh"] = cached
    titles = {(item.get("page"), normalized(item.get("title"))): item.get("title_zh", "")
              for item in current.get("toc", []) if item.get("title_zh")}
    for item in extracted.get("toc", []):
        cached = titles.get((item.get("page"), normalized(item.get("title"))))
        if cached:
            item["title_zh"] = cached
    extracted["reading_points"] = current.get("reading_points", [])
    return extracted


def remap_document_anchors(record: dict, previous: dict) -> None:
    """Match saved anchors by page and source text, since IDs can be reused."""
    normalized = lambda text: " ".join(str(text or "").split())
    old_blocks = {block["id"]: (page["number"], block)
                  for page in previous.get("pages", []) for block in page.get("blocks", [])}
    pages = {page["number"]: page.get("blocks", []) for page in record["document"].get("pages", [])}

    def target(identifier, page, selection=""):
        source_page, source = old_blocks.get(identifier, (None, {}))
        if source_page != page:
            source = {}
        selected = normalized(selection)
        original = normalized(source.get("text"))
        candidates = list(pages.get(page, []))
        if selected:
            candidates = [block for block in candidates if selected in normalized(block.get("text"))]
        exact = [block for block in candidates if original and normalized(block.get("text")) == original]
        if exact:
            candidates = exact
        elif not selected:
            # An extractor may split or join a paragraph. Only use a substantial
            # original-text overlap, never the coincidence of a reused ID.
            candidates = [block for block in candidates
                          if original and (text := normalized(block.get("text")))
                          and min(len(text), len(original)) >= min(20, len(original))
                          and (text in original or original in text)]
        if not candidates:
            return None
        bbox = source.get("bbox")
        if bbox and len(bbox) == 4:
            def distance(block):
                other = block.get("bbox") or bbox
                return sum((other[index] - bbox[index]) ** 2 for index in range(4))
            return min(candidates, key=distance)
        return candidates[0]

    for highlight in record.get("highlights", []):
        block = target(highlight.get("block_id"), highlight.get("page"), highlight.get("text"))
        # A page-only anchor is preferable to pointing at unrelated text; the
        # selected text, translation and note remain available in the sidebar.
        highlight["block_id"] = block["id"] if block else ""
    reading = record.get("reading", {})
    if reading.get("block_id"):
        block = target(reading["block_id"], reading.get("page"))
        reading["block_id"] = block["id"] if block else ""


def translation_layout_signature(document: dict) -> tuple:
    """Exclude AI output so incremental translations reuse scope metadata."""
    return (
        document.get("title", ""),
        tuple(item.get("block_id") for item in document.get("toc", [])),
        tuple((page.get("number"), tuple(
            (block.get("id"), block.get("type"), block.get("text"), tuple(block.get("bbox") or ()))
            for block in page.get("blocks", [])
        )) for page in document.get("pages", [])),
        tuple((figure.get("id"), figure.get("page"), tuple(figure.get("bbox") or ()),
               figure.get("caption"), figure.get("caption_block_id"),
               tuple(tuple(region) for region in figure["visual_regions"]) if "visual_regions" in figure else None)
              for figure in document.get("figures", [])),
        tuple((group.get("id"), tuple(group.get("block_ids", [])), group.get("text", ""))
              for group in document.get("paragraph_groups", [])),
    )


def translation_scope_metadata(document: dict) -> dict:
    return {
        "scopes": {block["id"]: block["translation_scope"]
                   for page in document.get("pages", []) for block in page.get("blocks", [])
                   if "translation_scope" in block},
        "visual_regions": {figure["id"]: [list(region) for region in figure["visual_regions"]]
                           for figure in document.get("figures", []) if "visual_regions" in figure},
        "version": document.get("translation_scope_version"),
    }


def apply_translation_scope_metadata(document: dict, metadata: dict) -> None:
    """Apply only metadata; safe to call while the store's lock is held."""
    for page in document.get("pages", []):
        for block in page.get("blocks", []):
            scope = metadata["scopes"].get(block["id"])
            if scope:
                block["translation_scope"] = scope
            if block.get("translation_scope") == "figure_content":
                block.pop("text_zh", None)
    for figure in document.get("figures", []):
        if figure["id"] in metadata["visual_regions"]:
            figure["visual_regions"] = [list(region) for region in metadata["visual_regions"][figure["id"]]]
    if metadata.get("version") == 1 and all(
            "visual_regions" in figure for figure in document.get("figures", [])) and all(
            "translation_scope" in block for page in document.get("pages", []) for block in page.get("blocks", [])):
        document["translation_scope_version"] = 1
    else:
        document.pop("translation_scope_version", None)


def figure_caption_blocks(document: dict) -> dict[str, str]:
    """Pair old and new figure records with their original caption blocks."""
    blocks = {block["id"]: block for page in document.get("pages", []) for block in page.get("blocks", [])}
    linked = {}
    for figure in document.get("figures", []):
        if not figure.get("caption"):
            continue
        identifier = figure.get("caption_block_id")
        if identifier in blocks:
            linked[figure["id"]] = identifier
            continue
        caption = " ".join(figure["caption"].split())
        match = next((block for page in document.get("pages", []) if page.get("number") == figure.get("page")
                      for block in page.get("blocks", []) if " ".join(block.get("text", "").split()) == caption), None)
        if match is not None:
            linked[figure["id"]] = match["id"]
    return linked


def paragraph_translation_groups(document: dict) -> list[dict]:
    """Return complete, nonoverlapping source groups without hiding stray text."""
    blocks = {block["id"]: block for page in document.get("pages", [])
              for block in page.get("blocks", [])}
    captions = set(figure_caption_blocks(document).values())
    accepted, used, group_ids = [], set(), set()
    for group in document.get("paragraph_groups", []):
        if not isinstance(group, dict) or not isinstance(group.get("id"), str):
            continue
        identifiers = group.get("block_ids")
        if (not isinstance(identifiers, list) or len(identifiers) < 2
                or any(not isinstance(identifier, str) for identifier in identifiers)
                or len(set(identifiers)) != len(identifiers)
                or group["id"] in group_ids or used.intersection(identifiers)
                or not isinstance(group.get("text"), str) or not group["text"].strip()
                or any(identifier not in blocks or identifier in captions
                       or blocks[identifier].get("translation_scope") != "body"
                       or blocks[identifier].get("type") != "paragraph" for identifier in identifiers)):
            continue
        accepted.append(group)
        used.update(identifiers)
        group_ids.add(group["id"])
    return accepted


def translation_targets(document: dict) -> list[dict]:
    """Translate a logical paragraph once, while retaining physical PDF blocks."""
    caption_blocks = figure_caption_blocks(document)
    associated = set(caption_blocks.values())
    blocks = {block["id"]: block for page in document.get("pages", [])
              for block in page.get("blocks", [])}
    visual_types = {"figure", "image", "table", "table_cell", "figure_label", "chart"}
    has_text = lambda value: isinstance(value, str) and bool(value.strip())
    groups = paragraph_translation_groups(document)
    membership = {identifier: group for group in groups for identifier in group["block_ids"]}
    targets, emitted = [], set()
    for block in blocks.values():
        if block["id"] in membership:
            group = membership[block["id"]]
            if group["id"] not in emitted:
                targets.append({"id": "para:" + group["id"], "text": group["text"],
                                "translated": has_text(group.get("text_zh"))})
                emitted.add(group["id"])
        # Reference entries are read in their original language.
        elif (has_text(block.get("text")) and block["id"] not in associated and not block.get("hidden")
              and block.get("translation_scope") != "figure_content"
              and block.get("type") not in visual_types | {"reference"}):
            targets.append({"id": block["id"], "text": block["text"],
                            "translated": has_text(block.get("text_zh"))})
    for figure in document.get("figures", []):
        if not has_text(figure.get("caption")):
            continue
        linked = blocks.get(caption_blocks.get(figure["id"]), {})
        targets.append({"id": "fig:" + figure["id"], "text": figure["caption"],
                        "translated": has_text(figure.get("caption_zh")) or has_text(linked.get("text_zh"))})
    return targets


def translation_coverage(document: dict) -> dict:
    targets = translation_targets(document)
    total = len(targets)
    completed = sum(item["translated"] for item in targets)
    return {"translation_total": total, "translation_completed": completed,
            "translation_remaining": total - completed,
            "translation_progress": round(completed / total * 100) if total else 0}


def update_translation_state(record: dict, status: str | None = None) -> dict:
    """Expose real coverage even for cached records with an obsolete status."""
    paper = record["paper"]
    coverage = translation_coverage(record["document"])
    paper.update(coverage)
    if status:
        paper["translation_status"] = status
        if status in ("queued", "processing", "complete"):
            paper["translation_error"] = ""
    elif paper.get("translation_status") == "complete" and coverage["translation_remaining"]:
        paper["translation_status"] = "partial"
    elif coverage["translation_total"] and not coverage["translation_remaining"] and paper.get("translation_status") not in ("queued", "processing"):
        paper.update(translation_status="complete", translation_error="")
    return record


def batches(items: list[dict]):
    batch, size = [], 0
    for item in items:
        if batch and (len(batch) >= TRANSLATION_BATCH_SIZE or size + len(item["text"]) > 14000):
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += len(item["text"])
    if batch:
        yield batch


def normalize_color(color) -> str:
    return color if color in ("yellow", "green", "purple", "blue", "pink") else "yellow"


def safe_error(exc: Exception) -> str:
    # urllib errors never include request headers; redact accidental bearer strings too.
    import re
    text = str(exc)
    text = re.sub(r"(?i)(bearer\s+|sk-)[A-Za-z0-9_.-]+", "[已隐藏]", text)
    return text[:500] or "任务失败，请检查配置后重试"
