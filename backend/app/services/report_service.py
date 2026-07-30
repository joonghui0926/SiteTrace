"""Generate a controlled, evidence-backed construction investigation report.

The PDF is intentionally an investigation work product, not an AI summary. It
uses the same sections a safety professional needs to review the incident,
understand the evidence boundary, approve actions, and preserve an audit trail.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, Sequence
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    Image,
    KeepTogether,
    LongTable,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents

from ..schemas import (
    CitedNarrativeClaim,
    CorrectiveAction,
    DeviationFinding,
    EvidenceClip,
    FindingStatus,
    InvestigationPackage,
    ObservedEvent,
    PlannedStep,
)


GREEN = colors.HexColor("#73C580")
GREEN_DARK = colors.HexColor("#27764B")
GREEN_SOFT = colors.HexColor("#EEF8F1")
INK = colors.HexColor("#171A1D")
TEXT = colors.HexColor("#30363B")
MUTED = colors.HexColor("#687077")
LINE = colors.HexColor("#DDE3DF")
PAPER = colors.white
SOFT = colors.HexColor("#F6F8F7")
AMBER = colors.HexColor("#9A6500")
RED = colors.HexColor("#A33A3A")


class _SiteTraceDocument(BaseDocTemplate):
    """ReportLab document with a real table of contents and controlled pages."""

    def __init__(
        self,
        filename: str,
        *,
        case_id: str,
        report_title: str,
        **kwargs: object,
    ) -> None:
        self.case_id = case_id
        self.report_title = report_title
        super().__init__(filename, **kwargs)
        frame = Frame(
            self.leftMargin,
            self.bottomMargin,
            self.width,
            self.height,
            id="body",
        )
        self.addPageTemplates(
            [
                PageTemplate(
                    id="report",
                    frames=[frame],
                    onPage=self._draw_page,
                )
            ]
        )
        self._heading_key = 0

    def beforeDocument(self) -> None:
        # multiBuild performs more than one layout pass for the table of
        # contents. Stable bookmark keys are required for the passes to agree.
        self._heading_key = 0
        super().beforeDocument()

    def _draw_page(self, canvas: object, doc: object) -> None:
        page = canvas.getPageNumber()
        canvas.saveState()
        canvas.setTitle(self.report_title)
        canvas.setAuthor("SiteTrace")
        canvas.setSubject("Evidence-backed construction incident investigation")
        width, height = LETTER
        if page > 1:
            canvas.setStrokeColor(LINE)
            canvas.setLineWidth(0.6)
            canvas.line(0.72 * inch, height - 0.55 * inch, width - 0.72 * inch, height - 0.55 * inch)
            canvas.setFillColor(INK)
            canvas.setFont("Helvetica-Bold", 8)
            canvas.drawString(0.72 * inch, height - 0.42 * inch, "SiteTrace")
            canvas.setFillColor(MUTED)
            canvas.setFont("Helvetica", 7.5)
            canvas.drawRightString(
                width - 0.72 * inch,
                height - 0.42 * inch,
                f"Controlled Investigation Report | {self.case_id}",
            )
        canvas.setStrokeColor(LINE)
        canvas.setLineWidth(0.5)
        canvas.line(0.72 * inch, 0.5 * inch, width - 0.72 * inch, 0.5 * inch)
        canvas.setFillColor(MUTED)
        canvas.setFont("Helvetica", 7.2)
        canvas.drawString(
            0.72 * inch,
            0.34 * inch,
            "DRAFT UNTIL HUMAN APPROVAL | Evidence-bounded analysis",
        )
        canvas.drawRightString(
            width - 0.72 * inch,
            0.34 * inch,
            f"Page {page}",
        )
        canvas.restoreState()

    def afterFlowable(self, flowable: Flowable) -> None:
        if not isinstance(flowable, Paragraph):
            return
        style_name = flowable.style.name
        if style_name not in {"ReportH1", "ReportH2"}:
            return
        level = 0 if style_name == "ReportH1" else 1
        text = flowable.getPlainText()
        if text == "Contents":
            return
        self._heading_key += 1
        key = f"section-{self._heading_key}"
        self.canv.bookmarkPage(key)
        self.canv.addOutlineEntry(text, key, level=level, closed=False)
        self.notify("TOCEntry", (level, text, self.page, key))


class ReportService:
    """Render a realistic English investigation report from validated evidence."""

    def __init__(self, *, logo_path: Path | None = None) -> None:
        repository_root = Path(__file__).resolve().parents[3]
        self.logo_path = logo_path or repository_root / "public" / "sitetrace-logo.png"
        self.styles = self._build_styles()

    def generate(
        self,
        investigation: InvestigationPackage,
        *,
        output_directory: Path | str,
    ) -> Path:
        output_root = Path(output_directory)
        output_root.mkdir(parents=True, exist_ok=True)
        filename = f"{self._safe_filename(investigation.case_id)}-investigation-report.pdf"
        output_path = output_root / filename
        document = _SiteTraceDocument(
            str(output_path),
            case_id=investigation.case_id,
            report_title=investigation.title,
            pagesize=LETTER,
            leftMargin=0.72 * inch,
            rightMargin=0.72 * inch,
            topMargin=0.78 * inch,
            bottomMargin=0.68 * inch,
            title=investigation.title,
            author="SiteTrace",
        )
        story = self._build_story(investigation)
        document.multiBuild(story)
        return output_path

    def _build_story(self, package: InvestigationPackage) -> list[Flowable]:
        story: list[Flowable] = []
        story.extend(self._cover(package))
        story.append(PageBreak())
        story.extend(self._contents())
        story.append(PageBreak())
        story.extend(self._document_control(package))
        story.extend(self._executive_summary(package))
        story.extend(self._scope_and_methodology(package))
        story.extend(self._source_materials(package))
        story.extend(self._chronology(package))
        story.extend(self._involved_resources(package))
        story.extend(self._jha_analysis(package))
        story.extend(self._contributing_conditions(package))
        story.extend(self._limitations(package))
        story.extend(self._actions(package))
        story.extend(self._conclusion(package))
        story.append(PageBreak())
        story.extend(self._approval(package))
        story.append(PageBreak())
        story.extend(self._evidence_annex(package))
        story.extend(self._planned_work_annex(package))
        story.extend(self._system_provenance(package))
        return story

    def _cover(self, package: InvestigationPackage) -> list[Flowable]:
        elements: list[Flowable] = [Spacer(1, 0.42 * inch)]
        if self.logo_path.exists():
            elements.append(Image(str(self.logo_path), width=0.68 * inch, height=0.68 * inch))
            elements.append(Spacer(1, 0.22 * inch))
        elements.extend(
            [
                Paragraph("SITETRACE", self.styles["CoverBrand"]),
                Spacer(1, 0.32 * inch),
                Paragraph(
                    "CONSTRUCTION INCIDENT / NEAR-MISS",
                    self.styles["CoverEyebrow"],
                ),
                Paragraph("INVESTIGATION REPORT", self.styles["CoverTitle"]),
                Spacer(1, 0.25 * inch),
                Paragraph(escape(package.title), self.styles["CoverCaseTitle"]),
                Spacer(1, 0.55 * inch),
                self._metadata_table(
                    [
                        ("Case identifier", package.case_id),
                        ("Report status", "Controlled draft - human approval required"),
                        ("Generated", self._format_datetime(package.generated_at)),
                        ("Evidence model", "Claim-level video citations and graph provenance"),
                    ],
                    label_width=1.48 * inch,
                ),
                Spacer(1, 0.55 * inch),
                Paragraph(
                    "PURPOSE OF THIS REPORT",
                    self.styles["SmallHeading"],
                ),
                Paragraph(
                    "This report preserves the observed event sequence, compares the "
                    "work shown in the available footage with the approved job hazard "
                    "analysis, records evidentiary limitations, and presents proposed "
                    "corrective and preventive actions for qualified human review. It "
                    "does not make a legal determination, assign blame, or establish "
                    "root cause from video evidence alone.",
                    self.styles["CoverPurpose"],
                ),
            ]
        )
        return elements

    def _contents(self) -> list[Flowable]:
        toc = TableOfContents()
        toc.levelStyles = [
            ParagraphStyle(
                "TOCLevel1",
                parent=self.styles["Body"],
                fontName="Helvetica-Bold",
                fontSize=9.5,
                leading=13,
                textColor=INK,
                leftIndent=0,
                firstLineIndent=0,
                spaceBefore=8,
            ),
            ParagraphStyle(
                "TOCLevel2",
                parent=self.styles["Body"],
                fontSize=8.5,
                leading=12,
                textColor=MUTED,
                leftIndent=18,
                firstLineIndent=0,
                spaceBefore=3,
            ),
        ]
        return [
            Paragraph("Contents", self.styles["ReportH1"]),
            Paragraph(
                "The main report is followed by evidence, planned-work, and system "
                "provenance annexes so that each conclusion can be reviewed against "
                "its source.",
                self.styles["Lead"],
            ),
            Spacer(1, 0.2 * inch),
            toc,
        ]

    def _document_control(self, package: InvestigationPackage) -> list[Flowable]:
        return [
            Paragraph("Document control", self.styles["ReportH1"]),
            Paragraph(
                "This document is a controlled investigation draft generated from "
                "the evidence package identified below. The named reviewer remains "
                "responsible for confirming factual accuracy, determining whether "
                "additional interviews or records are required, accepting or revising "
                "the proposed actions, and authorizing release.",
                self.styles["Body"],
            ),
            Spacer(1, 0.12 * inch),
            self._metadata_table(
                [
                    ("Document title", package.title),
                    ("Case identifier", package.case_id),
                    ("Prepared by", "SiteTrace Evidence Investigation Workflow"),
                    ("Review state", "Pending qualified safety-manager approval"),
                    ("Generation time", self._format_datetime(package.generated_at)),
                    (
                        "Controlled output",
                        "Evidence-backed Incident / Near-Miss Investigation Report",
                    ),
                ],
                label_width=1.5 * inch,
            ),
            Spacer(1, 0.2 * inch),
        ]

    def _executive_summary(self, package: InvestigationPackage) -> list[Flowable]:
        elements: list[Flowable] = [
            Paragraph("1. Incident statement and executive summary", self.styles["ReportH1"])
        ]
        if package.incident_overview:
            elements.extend(self._claim_paragraphs(package.incident_overview))
        else:
            evidence_ids = [clip.evidence_clip_id for clip in package.evidence_clips[:4]]
            elements.append(
                self._evidence_paragraph(
                    package.incident_summary,
                    evidence_ids,
                )
            )
        confirmed = sum(
            finding.status == FindingStatus.CONFIRMED_DEVIATION
            for finding in package.findings
        )
        unverified = sum(
            finding.status
            in {
                FindingStatus.UNVERIFIABLE,
                FindingStatus.REQUIRED_CONTROL_NOT_OBSERVED,
            }
            for finding in package.findings
        )
        elements.append(
            Paragraph(
                (
                    f"The structured review contains {len(package.events)} observed or "
                    f"bounded-inference events, {len(package.evidence_clips)} cited "
                    f"video segments, {confirmed} confirmed plan-versus-work "
                    f"deviations, and {unverified} control questions that require "
                    "additional verification. These counts describe the evidence "
                    "package and do not substitute for the reviewer's professional "
                    "judgment."
                ),
                self.styles["Body"],
            )
        )
        return elements

    def _scope_and_methodology(self, package: InvestigationPackage) -> list[Flowable]:
        camera_count = len({clip.camera_id for clip in package.evidence_clips})
        return [
            Paragraph("2. Investigation scope and methodology", self.styles["ReportH1"]),
            Paragraph(
                (
                    "The scope of this investigation was limited to the uploaded job "
                    f"hazard analysis, {camera_count} available camera source"
                    f"{'s' if camera_count != 1 else ''}, the machine-readable site "
                    "metadata supplied with the case, and the relationships derived "
                    "from those records. SiteTrace did not infer information from "
                    "interviews, medical records, equipment telemetry, training files, "
                    "or physical inspection unless such material was explicitly "
                    "included in the uploaded source package."
                ),
                self.styles["Body"],
            ),
            Paragraph(
                "TwelveLabs segmented the source video across visual activity, speech, "
                "audio, and visible on-screen text and returned second-level source "
                "references. OpenAI converted the approved work plan into structured "
                "steps and controls, normalized the cited observations, and drafted "
                "evidence-bounded narrative. Neo4j retained the planned-work graph and "
                "the observed-event graph, linked common people, objects, zones, and "
                "clips, and executed deterministic Cypher comparisons. Strands "
                "orchestrated ingestion, evidence verification, comparison, report "
                "drafting, and the required human-approval interruption.",
                self.styles["Body"],
            ),
            Paragraph(
                "The analytical standard used throughout this report distinguishes "
                "direct observation from bounded inference and from matters that could "
                "not be verified. A control is classified as a confirmed deviation "
                "only where cited footage positively shows a condition incompatible "
                "with the approved requirement. An absence from camera footage is "
                "treated as an evidence gap, not proof that an activity did not occur. "
                "Chronological proximity is described as sequence and is not presented "
                "as proof of causation.",
                self.styles["Body"],
            ),
        ]

    def _source_materials(self, package: InvestigationPackage) -> list[Flowable]:
        cameras = sorted({clip.camera_id for clip in package.evidence_clips})
        data = [
            [
                self._table_paragraph("Source class", bold=True),
                self._table_paragraph("Material reviewed", bold=True),
                self._table_paragraph("Use in this investigation", bold=True),
            ],
            [
                self._table_paragraph("Approved work plan"),
                self._table_paragraph(
                    f"{len(package.planned_steps)} parsed JHA step"
                    f"{'s' if len(package.planned_steps) != 1 else ''}"
                ),
                self._table_paragraph(
                    "Defines the planned sequence, required controls, required roles, "
                    "and prohibited or restricted zones."
                ),
            ],
            [
                self._table_paragraph("Video evidence"),
                self._table_paragraph(
                    f"{len(package.evidence_clips)} cited clips from "
                    f"{len(cameras)} camera source{'s' if len(cameras) != 1 else ''}"
                ),
                self._table_paragraph(
                    "Establishes the visible and audible event sequence with source "
                    "timestamps, transcripts, visible text, and confidence metadata."
                ),
            ],
            [
                self._table_paragraph("Context graph"),
                self._table_paragraph(
                    f"{package.graph_metrics.get('nodes', 0)} nodes and "
                    f"{package.graph_metrics.get('relationships', 0)} relationships"
                ),
                self._table_paragraph(
                    "Connects plan controls, events, entities, zones, evidence clips, "
                    "findings, and action records for deterministic review."
                ),
            ],
        ]
        return [
            Paragraph("2.1 Source materials reviewed", self.styles["ReportH2"]),
            self._long_table(data, [1.25 * inch, 2.0 * inch, 3.78 * inch]),
            Spacer(1, 0.14 * inch),
        ]

    def _chronology(self, package: InvestigationPackage) -> list[Flowable]:
        elements: list[Flowable] = [
            Paragraph("3. Detailed multi-camera chronology", self.styles["ReportH1"]),
            Paragraph(
                "The chronology below consolidates observations from all available "
                "camera sources. Relative clip times are retained where an absolute "
                "recording start time was not available. Cross-camera links reflect "
                "the evidence and graph relationships stored in this case; they do not "
                "assert that temporal order alone establishes cause.",
                self.styles["Body"],
            ),
        ]
        if package.event_timeline:
            elements.extend(self._claim_paragraphs(package.event_timeline))
        elif package.events:
            for event in self._sorted_events(package.events):
                elements.append(self._event_paragraph(event))
        else:
            elements.append(
                Paragraph(
                    "No event chronology was returned from the validated evidence "
                    "workflow. The reviewer should confirm the ingestion status and "
                    "camera coverage before the report is released.",
                    self.styles["Body"],
                )
            )
        if package.events:
            data = [
                [
                    self._table_paragraph("Time / camera", bold=True),
                    self._table_paragraph("Observed event", bold=True),
                    self._table_paragraph("Evidence", bold=True),
                ]
            ]
            for event in self._sorted_events(package.events):
                data.append(
                    [
                        self._table_paragraph(
                            f"{event.camera_id}<br/>{self._format_seconds(event.start_sec)}"
                            f" to {self._format_seconds(event.end_sec)}"
                        ),
                        self._table_paragraph(
                            f"<b>{escape(self._humanize(event.event_type))}</b><br/>"
                            f"{escape(event.summary)}"
                        ),
                        self._table_paragraph(
                            ", ".join(event.evidence_clip_ids)
                            + f"<br/>Confidence {event.confidence:.0%}"
                        ),
                    ]
                )
            elements.extend(
                [
                    Paragraph("3.1 Event register", self.styles["ReportH2"]),
                    self._long_table(data, [1.25 * inch, 4.15 * inch, 1.63 * inch]),
                ]
            )
        return elements

    def _involved_resources(self, package: InvestigationPackage) -> list[Flowable]:
        actors = sorted({value for event in package.events for value in event.actor_ids})
        objects = sorted({value for event in package.events for value in event.object_ids})
        zones = sorted({value for event in package.events for value in event.zone_ids})
        categories = [
            ("People / role candidates", actors),
            ("Equipment and material", objects),
            ("Work areas and zones", zones),
        ]
        rows = [
            [
                self._table_paragraph("Category", bold=True),
                self._table_paragraph("Entities referenced by cited events", bold=True),
            ]
        ]
        for label, values in categories:
            rows.append(
                [
                    self._table_paragraph(label),
                    self._table_paragraph(
                        ", ".join(values)
                        if values
                        else "No distinct entity identifier was resolved."
                    ),
                ]
            )
        return [
            Paragraph("4. People, equipment, material, and work areas", self.styles["ReportH1"]),
            Paragraph(
                "Entity labels in this report are operational identifiers derived "
                "from the uploaded records. They are used to connect observations "
                "across clips and should not be interpreted as biometric identity "
                "determinations. The reviewer must reconcile these labels with the "
                "site roster, equipment log, and location plan where those records "
                "are available.",
                self.styles["Body"],
            ),
            self._long_table(rows, [1.85 * inch, 5.18 * inch]),
        ]

    def _jha_analysis(self, package: InvestigationPackage) -> list[Flowable]:
        elements: list[Flowable] = [
            Paragraph("5. Approved JHA compared with work observed", self.styles["ReportH1"]),
            Paragraph(
                "This section compares the control relationships defined by the "
                "approved JHA with the relationships positively established by the "
                "cited footage. Confirmed deviations require affirmative evidence of "
                "an incompatible condition. Required controls that were not visible "
                "throughout the available coverage remain unverified unless another "
                "clip positively establishes the relevant condition.",
                self.styles["Body"],
            ),
        ]
        if package.deviation_summary:
            elements.extend(self._claim_paragraphs(package.deviation_summary))
        if not package.findings:
            elements.append(
                Paragraph(
                    "The graph comparison did not return a reportable finding. This "
                    "does not establish full JHA compliance; it means that no validated "
                    "finding was available in the current evidence package.",
                    self.styles["Body"],
                )
            )
            return elements
        for index, finding in enumerate(package.findings, start=1):
            elements.extend(self._finding_block(index, finding))
        return elements

    def _contributing_conditions(self, package: InvestigationPackage) -> list[Flowable]:
        confirmed = [
            finding
            for finding in package.findings
            if finding.status == FindingStatus.CONFIRMED_DEVIATION
        ]
        elements: list[Flowable] = [
            Paragraph("6. Evidence-supported contributing conditions", self.styles["ReportH1"]),
            Paragraph(
                "The items in this section are conditions or actions observed in the "
                "recorded sequence and relevant to the approved controls. The term "
                "\"contributing condition\" is used as an investigation category; it "
                "does not mean that the footage alone proves a root cause, assigns "
                "fault, or establishes that any single event produced the outcome.",
                self.styles["Body"],
            ),
        ]
        if not confirmed:
            elements.append(
                Paragraph(
                    "No contributing condition was elevated to a confirmed deviation "
                    "under the evidence policy. Additional records may still be needed "
                    "to complete the qualified safety review.",
                    self.styles["Body"],
                )
            )
            return elements
        for finding in confirmed:
            evidence = self._evidence_label(finding.evidence_clip_ids)
            elements.append(
                Paragraph(
                    (
                        f"<b>{escape(finding.title)}.</b> The approved control was "
                        f"\"{escape(finding.planned_control)}.\" "
                        f"{escape(finding.observed_work or 'The cited footage records an incompatible condition.')} "
                        "This observation is retained as part of the time-ordered "
                        "investigation record and must be assessed together with the "
                        f"remaining evidence and interviews. {evidence}"
                    ),
                    self.styles["Body"],
                )
            )
        return elements

    def _limitations(self, package: InvestigationPackage) -> list[Flowable]:
        elements: list[Flowable] = [
            Paragraph("7. Unknowns, evidence gaps, and investigation limitations", self.styles["ReportH1"]),
            Paragraph(
                "Video evidence is inherently bounded by camera placement, field of "
                "view, recording continuity, image and audio quality, clock "
                "synchronization, and the periods supplied for review. An activity "
                "that is not visible cannot be classified as not performed. Entity "
                "resolution across cameras is an evidence-linked candidate match and "
                "must be confirmed against site records when identity is material.",
                self.styles["Body"],
            ),
            Paragraph(
                "This review does not independently establish medical outcome, exact "
                "separation distance, equipment condition, operator qualification, "
                "training effectiveness, supervisory expectations, production "
                "pressure, or organizational root cause. Those matters ordinarily "
                "require interviews, measurements, equipment data, inspection records, "
                "and management-system review outside the video context graph.",
                self.styles["Body"],
            ),
        ]
        if package.limitations:
            for index, limitation in enumerate(package.limitations, start=1):
                elements.append(
                    Paragraph(
                        f"<b>Recorded limitation {index}.</b> {escape(limitation)}",
                        self.styles["Body"],
                    )
                )
        else:
            elements.append(
                Paragraph(
                    "No case-specific limitation was supplied by the reasoning "
                    "workflow. The standard evidence limitations above still apply and "
                    "must be reviewed before approval.",
                    self.styles["Body"],
                )
            )
        return elements

    def _actions(self, package: InvestigationPackage) -> list[Flowable]:
        elements: list[Flowable] = [
            Paragraph("8. Immediate, corrective, and preventive actions", self.styles["ReportH1"]),
            Paragraph(
                "The actions below are draft controls proposed for accountable human "
                "review. Approval should confirm that each action addresses a validated "
                "finding, assigns an owner with authority to complete the work, has a "
                "realistic due date, and includes an effectiveness check capable of "
                "showing whether the intended condition is sustained.",
                self.styles["Body"],
            ),
        ]
        finding_lookup = {finding.finding_id: finding for finding in package.findings}
        if not package.corrective_actions:
            elements.append(
                Paragraph(
                    "No action was generated from the current validated findings. The "
                    "reviewer must determine whether immediate controls are necessary "
                    "before affected work resumes.",
                    self.styles["Body"],
                )
            )
            return elements
        rows = [
            [
                self._table_paragraph("Type", bold=True),
                self._table_paragraph("Proposed action", bold=True),
                self._table_paragraph("Accountability", bold=True),
            ]
        ]
        for action in package.corrective_actions:
            rows.append(
                [
                    self._table_paragraph(self._humanize(action.action_type)),
                    self._table_paragraph(action.description),
                    self._table_paragraph(
                        f"Owner: {escape(action.owner_role)}<br/>"
                        f"Due: {escape(action.due_date or 'To be assigned')}<br/>"
                        f"Status: {escape(action.status)}"
                    ),
                ]
            )
        elements.append(self._long_table(rows, [1.0 * inch, 4.15 * inch, 1.88 * inch]))
        elements.append(Paragraph("8.1 Action rationale and effectiveness verification", self.styles["ReportH2"]))
        for index, action in enumerate(package.corrective_actions, start=1):
            finding = finding_lookup.get(action.finding_id)
            evidence_ids = finding.evidence_clip_ids if finding else []
            elements.append(
                Paragraph(
                    (
                        f"<b>Action {index}: {escape(self._humanize(action.action_type))}.</b> "
                        f"{escape(action.description)} The proposed owner is "
                        f"{escape(action.owner_role)}, and the target completion date is "
                        f"{escape(action.due_date or 'to be assigned during review')}. "
                        f"{escape(self._effectiveness_statement(action))} "
                        f"{self._evidence_label(evidence_ids) if evidence_ids else ''}"
                    ),
                    self.styles["Body"],
                )
            )
        return elements

    def _conclusion(self, package: InvestigationPackage) -> list[Flowable]:
        evidence_ids: list[str] = []
        for finding in package.findings:
            for clip_id in finding.evidence_clip_ids:
                if clip_id not in evidence_ids:
                    evidence_ids.append(clip_id)
        status_counts: dict[FindingStatus, int] = defaultdict(int)
        for finding in package.findings:
            status_counts[finding.status] += 1
        return [
            Paragraph("9. Investigation conclusion", self.styles["ReportH1"]),
            Paragraph(
                (
                    "The available evidence was sufficient to reconstruct a bounded "
                    f"sequence of {len(package.events)} event"
                    f"{'s' if len(package.events) != 1 else ''} and compare the sequence "
                    f"with {len(package.planned_steps)} approved JHA step"
                    f"{'s' if len(package.planned_steps) != 1 else ''}. The deterministic "
                    "comparison identified "
                    f"{status_counts[FindingStatus.CONFIRMED_DEVIATION]} confirmed "
                    "deviation(s), "
                    f"{status_counts[FindingStatus.COMPLIANT]} positively supported "
                    "compliant condition(s), and "
                    f"{status_counts[FindingStatus.UNVERIFIABLE] + status_counts[FindingStatus.REQUIRED_CONTROL_NOT_OBSERVED]} "
                    "matter(s) requiring additional verification. "
                    f"{self._evidence_label(evidence_ids) if evidence_ids else ''}"
                ),
                self.styles["Body"],
            ),
            Paragraph(
                "The report therefore supports a qualified safety review of what was "
                "recorded and how the recorded work compared with the approved plan. "
                "It does not establish legal liability or root cause. Final incident "
                "classification, action acceptance, work-release decisions, and any "
                "external reporting remain the responsibility of authorized site and "
                "company personnel.",
                self.styles["Body"],
            ),
        ]

    def _approval(self, package: InvestigationPackage) -> list[Flowable]:
        rows = [
            [
                self._table_paragraph("Prepared by", bold=True),
                self._table_paragraph("Reviewed by", bold=True),
                self._table_paragraph("Approved by", bold=True),
            ],
            [
                self._table_paragraph(
                    "SiteTrace Evidence Investigation Workflow<br/><br/>"
                    f"Date: {escape(self._format_datetime(package.generated_at))}"
                ),
                self._table_paragraph(
                    "Name: __________________________<br/><br/>"
                    "Role: ___________________________<br/><br/>"
                    "Date: ___________________________"
                ),
                self._table_paragraph(
                    "Name: __________________________<br/><br/>"
                    "Role: ___________________________<br/><br/>"
                    "Date: ___________________________"
                ),
            ],
        ]
        return [
            Paragraph("10. Review and approval", self.styles["ReportH1"]),
            Paragraph(
                "By signing, the reviewer confirms that the source evidence and "
                "material findings were examined, unresolved matters were either "
                "addressed or explicitly accepted as limitations, and the approved "
                "actions were entered into the organization's accountable tracking "
                "process. Approval does not convert uncertain model output into fact.",
                self.styles["Body"],
            ),
            Table(
                rows,
                colWidths=[2.35 * inch, 2.35 * inch, 2.35 * inch],
                rowHeights=[0.32 * inch, 1.18 * inch],
                style=TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), GREEN_SOFT),
                        ("TEXTCOLOR", (0, 0), (-1, -1), TEXT),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LINEBELOW", (0, 0), (-1, 0), 0.7, GREEN),
                        ("BOX", (0, 0), (-1, -1), 0.5, LINE),
                        ("INNERGRID", (0, 0), (-1, -1), 0.35, LINE),
                        ("LEFTPADDING", (0, 0), (-1, -1), 8),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                        ("TOPPADDING", (0, 0), (-1, -1), 7),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                    ]
                ),
            ),
        ]

    def _evidence_annex(self, package: InvestigationPackage) -> list[Flowable]:
        elements: list[Flowable] = [
            Paragraph("Appendix A. Evidence register", self.styles["ReportH1"]),
            Paragraph(
                "Each evidence identifier below resolves to a source video, camera, "
                "and exact clip interval. Transcript and visible-text fields are "
                "included where supplied by the video intelligence workflow. The "
                "reviewer should open the original clip before accepting any material "
                "finding that relies on it.",
                self.styles["Body"],
            ),
        ]
        if not package.evidence_clips:
            elements.append(
                Paragraph(
                    "No evidence clip was retained in this package.",
                    self.styles["Body"],
                )
            )
            return elements
        for clip in sorted(
            package.evidence_clips,
            key=lambda value: (value.camera_id, value.start_sec, value.evidence_clip_id),
        ):
            elements.extend(self._evidence_block(clip))
        return elements

    def _planned_work_annex(self, package: InvestigationPackage) -> list[Flowable]:
        elements: list[Flowable] = [
            Paragraph("Appendix B. Approved planned-work register", self.styles["ReportH1"]),
            Paragraph(
                "The following structured steps were extracted from the approved JHA "
                "or lift plan supplied with the case. Exact source documents remain "
                "the controlling records where any interpretation differs.",
                self.styles["Body"],
            ),
        ]
        if not package.planned_steps:
            elements.append(
                Paragraph("No planned step was available.", self.styles["Body"])
            )
            return elements
        for step in sorted(package.planned_steps, key=lambda value: value.sequence):
            elements.extend(self._planned_step_block(step))
        return elements

    def _system_provenance(self, package: InvestigationPackage) -> list[Flowable]:
        elements: list[Flowable] = [
            Paragraph("Appendix C. Context-graph and system provenance", self.styles["ReportH1"]),
            Paragraph(
                "The identifiers and metrics below preserve the computational lineage "
                "of this draft. Sponsor-feature entries describe the services involved "
                "in producing the evidence package; they are not substitutes for the "
                "source video, the approved JHA, or professional review.",
                self.styles["Body"],
            ),
            self._metadata_table(
                [
                    ("TwelveLabs knowledge store", package.knowledge_store_id or "Not recorded"),
                    ("Jockey session", package.jockey_session_id or "Not recorded"),
                    (
                        "Context graph",
                        ", ".join(
                            f"{self._humanize(key)}: {value}"
                            for key, value in sorted(package.graph_metrics.items())
                        )
                        or "No graph metrics recorded",
                    ),
                    ("Evidence clips", str(len(package.evidence_clips))),
                    ("Observed events", str(len(package.events))),
                    ("JHA findings", str(len(package.findings))),
                ],
                label_width=1.75 * inch,
            ),
        ]
        for trace in package.sponsor_trace:
            sponsor = str(trace.get("sponsor", "System component"))
            features = trace.get("features", [])
            feature_text = ", ".join(str(value) for value in features) if features else "Execution metadata retained"
            elements.append(
                Paragraph(
                    f"<b>{escape(sponsor)}.</b> {escape(feature_text)}.",
                    self.styles["Body"],
                )
            )
        return elements

    def _claim_paragraphs(
        self,
        claims: Sequence[CitedNarrativeClaim],
    ) -> list[Flowable]:
        return [
            self._evidence_paragraph(claim.text, claim.evidence_clip_ids)
            for claim in claims
        ]

    def _event_paragraph(self, event: ObservedEvent) -> Paragraph:
        return Paragraph(
            (
                f"<b>{escape(event.camera_id)}, "
                f"{escape(self._format_seconds(event.start_sec))} to "
                f"{escape(self._format_seconds(event.end_sec))}.</b> "
                f"{escape(event.summary)} The record is classified as "
                f"{escape(self._humanize(event.observation_type))} with "
                f"{event.confidence:.0%} model confidence. "
                f"{self._evidence_label(event.evidence_clip_ids)}"
            ),
            self.styles["Body"],
        )

    def _finding_block(
        self,
        index: int,
        finding: DeviationFinding,
    ) -> list[Flowable]:
        color = {
            FindingStatus.CONFIRMED_DEVIATION: RED,
            FindingStatus.COMPLIANT: GREEN_DARK,
            FindingStatus.REQUIRED_CONTROL_NOT_OBSERVED: AMBER,
            FindingStatus.UNVERIFIABLE: AMBER,
        }[finding.status]
        status = self._humanize(finding.status.value)
        evidence = self._evidence_label(finding.evidence_clip_ids)
        observed = finding.observed_work or (
            f"The matter remains unverified because {finding.evidence_gap_reason}"
            if finding.evidence_gap_reason
            else "The available evidence does not establish an observed-work statement."
        )
        path = ""
        if finding.graph_path_node_ids or finding.graph_path_relationships:
            path_values: list[str] = []
            for idx, node_id in enumerate(finding.graph_path_node_ids):
                path_values.append(node_id)
                if idx < len(finding.graph_path_relationships):
                    path_values.append(f"-[{finding.graph_path_relationships[idx]}]->")
            path = " ".join(path_values)
        body = Paragraph(
            (
                f"<font color='{color.hexval()}'><b>{escape(status)}</b></font><br/>"
                f"<b>Approved control.</b> {escape(finding.planned_control)}<br/>"
                f"<b>Work observed or evidence gap.</b> {escape(observed)}<br/>"
                f"<b>Assessment.</b> This finding is recorded with "
                f"{finding.confidence:.0%} confidence and remains subject to qualified "
                f"human review. {evidence}"
                + (
                    f"<br/><b>Graph path.</b> {escape(path)}"
                    if path
                    else ""
                )
            ),
            self.styles["FindingBody"],
        )
        title = Paragraph(
            f"Finding {index}. {escape(finding.title)}",
            self.styles["FindingTitle"],
        )
        return [KeepTogether([title, body]), Spacer(1, 0.08 * inch)]

    def _evidence_block(self, clip: EvidenceClip) -> list[Flowable]:
        rows = [
            [
                self._table_paragraph("Camera / source", bold=True),
                self._table_paragraph(f"{clip.camera_id} / {clip.source_filename}"),
                self._table_paragraph("Clip interval", bold=True),
                self._table_paragraph(
                    f"{self._format_seconds(clip.start_sec)} to "
                    f"{self._format_seconds(clip.end_sec)}"
                ),
            ],
            [
                self._table_paragraph("Observation", bold=True),
                self._table_paragraph(clip.summary),
                self._table_paragraph("Confidence", bold=True),
                self._table_paragraph(f"{clip.confidence:.0%}"),
            ],
            [
                self._table_paragraph("Transcript", bold=True),
                self._table_paragraph(clip.transcript or "No speech transcript retained."),
                self._table_paragraph("Visible text", bold=True),
                self._table_paragraph(clip.visible_text or "No visible text retained."),
            ],
            [
                self._table_paragraph("Source IDs", bold=True),
                self._table_paragraph(
                    "<br/>".join(
                        value
                        for value in [
                            f"Item: {escape(clip.item_id)}" if clip.item_id else "",
                            f"Asset: {escape(clip.asset_id)}" if clip.asset_id else "",
                            f"URL: {escape(clip.source_url)}" if clip.source_url else "",
                        ]
                        if value
                    )
                    or "No external source identifier retained."
                ),
                self._table_paragraph("Evidence ID", bold=True),
                self._table_paragraph(clip.evidence_clip_id),
            ],
        ]
        return [
            Paragraph(
                f"{escape(clip.evidence_clip_id)}",
                self.styles["RecordH2"],
            ),
            self._long_table(
                rows,
                [1.02 * inch, 2.72 * inch, 0.9 * inch, 2.39 * inch],
                header=False,
            ),
            Spacer(1, 0.12 * inch),
        ]

    def _planned_step_block(self, step: PlannedStep) -> list[Flowable]:
        requirements: list[str] = []
        if step.required_controls:
            requirements.append(
                "<b>Required controls:</b> "
                + "; ".join(escape(value) for value in step.required_controls)
            )
        if step.required_roles:
            requirements.append(
                "<b>Required roles:</b> "
                + "; ".join(escape(value) for value in step.required_roles)
            )
        if step.must_avoid_zones:
            requirements.append(
                "<b>Zones to avoid:</b> "
                + "; ".join(escape(value) for value in step.must_avoid_zones)
            )
        if not requirements:
            requirements.append(
                "No explicit control, role, or restricted-zone requirement was parsed."
            )
        return [
            Paragraph(
                f"Step {step.sequence}. {escape(step.name)}",
                self.styles["RecordH2"],
            ),
            Paragraph(
                escape(step.description) if step.description else "No additional step description was retained.",
                self.styles["Body"],
            ),
            Paragraph("<br/>".join(requirements), self.styles["Body"]),
        ]

    def _metadata_table(
        self,
        values: Sequence[tuple[str, str]],
        *,
        label_width: float,
    ) -> Table:
        rows = [
            [
                self._table_paragraph(label.upper(), bold=True, small=True),
                self._table_paragraph(value),
            ]
            for label, value in values
        ]
        return Table(
            rows,
            colWidths=[label_width, 7.03 * inch - label_width],
            style=TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINE),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 7),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ]
            ),
        )

    def _long_table(
        self,
        data: Sequence[Sequence[object]],
        widths: Sequence[float],
        *,
        header: bool = True,
    ) -> LongTable:
        commands: list[tuple] = [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TEXTCOLOR", (0, 0), (-1, -1), TEXT),
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]
        if header:
            commands.extend(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), GREEN_SOFT),
                    ("LINEBELOW", (0, 0), (-1, 0), 0.8, GREEN),
                ]
            )
        return LongTable(
            data,
            colWidths=widths,
            repeatRows=1 if header else 0,
            splitByRow=1,
            style=TableStyle(commands),
        )

    def _table_paragraph(
        self,
        value: str,
        *,
        bold: bool = False,
        small: bool = False,
    ) -> Paragraph:
        text = value if any(token in value for token in ("<b>", "<br/>")) else escape(value)
        if bold and not text.startswith("<b>"):
            text = f"<b>{text}</b>"
        return Paragraph(
            text,
            self.styles["TableSmall" if small else "TableBody"],
        )

    def _evidence_paragraph(
        self,
        text: str,
        evidence_clip_ids: Iterable[str],
    ) -> Paragraph:
        return Paragraph(
            f"{escape(text)} {self._evidence_label(evidence_clip_ids)}",
            self.styles["Body"],
        )

    @staticmethod
    def _evidence_label(evidence_clip_ids: Iterable[str]) -> str:
        identifiers = [escape(value) for value in evidence_clip_ids if value]
        if not identifiers:
            return ""
        return (
            "<font color='#27764B' size='8'><b>Evidence: "
            + ", ".join(identifiers)
            + "</b></font>"
        )

    @staticmethod
    def _sorted_events(events: Sequence[ObservedEvent]) -> list[ObservedEvent]:
        return sorted(
            events,
            key=lambda value: (value.start_sec, value.camera_id, value.event_id),
        )

    @staticmethod
    def _effectiveness_statement(action: CorrectiveAction) -> str:
        if action.action_type == "immediate":
            return (
                "Effectiveness should be confirmed by a documented field inspection "
                "before the affected work area or activity is released."
            )
        if action.action_type == "corrective":
            return (
                "Effectiveness should be verified through a subsequent observed work "
                "cycle and closure evidence showing that the control remained in place."
            )
        return (
            "Effectiveness should be reviewed after implementation using a defined "
            "sampling period, repeat-observation criteria, and documented management "
            "acceptance."
        )

    @staticmethod
    def _format_seconds(seconds: float) -> str:
        whole_minutes, remainder = divmod(seconds, 60)
        whole_hours, minutes = divmod(int(whole_minutes), 60)
        if whole_hours:
            return f"{whole_hours:02d}:{minutes:02d}:{remainder:04.1f}"
        return f"{minutes:02d}:{remainder:04.1f}"

    @staticmethod
    def _format_datetime(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")

    @staticmethod
    def _humanize(value: str) -> str:
        return value.replace("_", " ").strip().title()

    @staticmethod
    def _safe_filename(value: str) -> str:
        safe = "".join(character if character.isalnum() else "-" for character in value)
        safe = "-".join(part for part in safe.split("-") if part)
        return safe[:80] or "sitetrace-case"

    @staticmethod
    def _build_styles() -> dict[str, ParagraphStyle]:
        sample = getSampleStyleSheet()
        return {
            "CoverBrand": ParagraphStyle(
                "CoverBrand",
                parent=sample["Normal"],
                fontName="Helvetica-Bold",
                fontSize=12,
                leading=14,
                textColor=GREEN_DARK,
                spaceAfter=0,
            ),
            "CoverEyebrow": ParagraphStyle(
                "CoverEyebrow",
                parent=sample["Normal"],
                fontName="Helvetica-Bold",
                fontSize=9,
                leading=12,
                textColor=GREEN_DARK,
                tracking=1.2,
                spaceAfter=8,
            ),
            "CoverTitle": ParagraphStyle(
                "CoverTitle",
                parent=sample["Title"],
                fontName="Helvetica-Bold",
                fontSize=28,
                leading=31,
                textColor=INK,
                alignment=TA_LEFT,
                spaceAfter=14,
            ),
            "CoverCaseTitle": ParagraphStyle(
                "CoverCaseTitle",
                parent=sample["Normal"],
                fontName="Helvetica",
                fontSize=15,
                leading=20,
                textColor=TEXT,
            ),
            "CoverPurpose": ParagraphStyle(
                "CoverPurpose",
                parent=sample["Normal"],
                fontName="Helvetica",
                fontSize=9.5,
                leading=15,
                textColor=TEXT,
                spaceAfter=8,
            ),
            "SmallHeading": ParagraphStyle(
                "SmallHeading",
                parent=sample["Normal"],
                fontName="Helvetica-Bold",
                fontSize=8,
                leading=10,
                textColor=GREEN_DARK,
                tracking=0.8,
                spaceAfter=8,
            ),
            "ReportH1": ParagraphStyle(
                "ReportH1",
                parent=sample["Heading1"],
                fontName="Helvetica-Bold",
                fontSize=17,
                leading=21,
                textColor=INK,
                spaceBefore=16,
                spaceAfter=9,
                keepWithNext=True,
            ),
            "ReportH2": ParagraphStyle(
                "ReportH2",
                parent=sample["Heading2"],
                fontName="Helvetica-Bold",
                fontSize=11,
                leading=15,
                textColor=INK,
                spaceBefore=14,
                spaceAfter=6,
                keepWithNext=True,
            ),
            "RecordH2": ParagraphStyle(
                "RecordH2",
                parent=sample["Heading3"],
                fontName="Helvetica-Bold",
                fontSize=10,
                leading=14,
                textColor=INK,
                spaceBefore=11,
                spaceAfter=5,
                keepWithNext=True,
            ),
            "Lead": ParagraphStyle(
                "Lead",
                parent=sample["Normal"],
                fontName="Helvetica",
                fontSize=10.5,
                leading=16,
                textColor=TEXT,
                spaceAfter=8,
            ),
            "Body": ParagraphStyle(
                "Body",
                parent=sample["BodyText"],
                fontName="Helvetica",
                fontSize=9.25,
                leading=14.2,
                textColor=TEXT,
                alignment=TA_LEFT,
                spaceAfter=8.5,
                allowWidows=0,
                allowOrphans=0,
            ),
            "FindingTitle": ParagraphStyle(
                "FindingTitle",
                parent=sample["Heading3"],
                fontName="Helvetica-Bold",
                fontSize=10,
                leading=14,
                textColor=INK,
                spaceBefore=8,
                spaceAfter=3,
                keepWithNext=True,
            ),
            "FindingBody": ParagraphStyle(
                "FindingBody",
                parent=sample["BodyText"],
                fontName="Helvetica",
                fontSize=8.8,
                leading=13.4,
                textColor=TEXT,
                leftIndent=9,
                borderColor=GREEN,
                borderWidth=0,
                borderPadding=(0, 0, 0, 8),
                spaceAfter=8,
            ),
            "TableBody": ParagraphStyle(
                "TableBody",
                parent=sample["Normal"],
                fontName="Helvetica",
                fontSize=7.6,
                leading=10.4,
                textColor=TEXT,
                wordWrap="CJK",
            ),
            "TableSmall": ParagraphStyle(
                "TableSmall",
                parent=sample["Normal"],
                fontName="Helvetica-Bold",
                fontSize=6.8,
                leading=9.2,
                textColor=MUTED,
                wordWrap="CJK",
            ),
        }
