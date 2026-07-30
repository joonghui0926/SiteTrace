"use client";

import Image from "next/image";
import {
  ChangeEvent,
  FormEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

type CaseStatus =
  | "UPLOADED"
  | "PROCESSING"
  | "AWAITING_APPROVAL"
  | "APPROVED"
  | "COMPLETED"
  | "FAILED";

type FindingStatus =
  | "COMPLIANT"
  | "CONFIRMED_DEVIATION"
  | "REQUIRED_CONTROL_NOT_OBSERVED"
  | "UNVERIFIABLE";

type PlannedStep = {
  step_id: string;
  sequence: number;
  name: string;
  description: string;
  required_controls: string[];
  must_avoid_zones: string[];
  required_roles: string[];
};

type ObservedEvent = {
  event_id: string;
  event_type: string;
  summary: string;
  camera_id: string;
  start_sec: number;
  end_sec: number;
  actor_ids: string[];
  object_ids: string[];
  zone_ids: string[];
  evidence_clip_ids: string[];
  confidence: number;
  observation_type: "observed" | "inferred" | "human_confirmed";
};

type EvidenceClip = {
  evidence_clip_id: string;
  item_id?: string | null;
  asset_id?: string | null;
  camera_id: string;
  source_filename: string;
  start_sec: number;
  end_sec: number;
  summary: string;
  transcript: string;
  visible_text: string;
  confidence: number;
  source_url?: string | null;
};

type DeviationFinding = {
  finding_id: string;
  jha_step_id: string;
  status: FindingStatus;
  title: string;
  planned_control: string;
  observed_work?: string | null;
  evidence_clip_ids: string[];
  graph_path_node_ids: string[];
  graph_path_relationships: string[];
  confidence: number;
  evidence_gap_reason?: string | null;
  requires_human_review: boolean;
};

type CorrectiveAction = {
  action_id: string;
  finding_id: string;
  action_type: "immediate" | "corrective" | "preventive";
  description: string;
  owner_role: string;
  due_date?: string | null;
  status: string;
};

type SponsorTraceEntry = Record<string, unknown>;

type CitedNarrativeClaim = {
  claim_id: string;
  text: string;
  evidence_clip_ids: string[];
  finding_ids: string[];
};

type Investigation = {
  case_id: string;
  title: string;
  incident_summary: string;
  incident_overview?: CitedNarrativeClaim[];
  event_timeline?: CitedNarrativeClaim[];
  deviation_summary?: CitedNarrativeClaim[];
  planned_steps: PlannedStep[];
  events: ObservedEvent[];
  evidence_clips: EvidenceClip[];
  findings: DeviationFinding[];
  corrective_actions: CorrectiveAction[];
  limitations: string[];
  graph_metrics: Record<string, number>;
  sponsor_trace: SponsorTraceEntry[];
  generated_at?: string;
};

type CaseRecord = {
  case_id: string;
  title: string;
  status: CaseStatus;
  created_at?: string;
  updated_at?: string;
  investigation?: Investigation | null;
  approved_by?: string | null;
  approved_at?: string | null;
  report_path?: string | null;
  report_s3_uri?: string | null;
  error?: string | null;
};

type WorkspaceView = "evidence" | "comparison" | "report";
type RunState =
  | "idle"
  | "uploading"
  | "investigating"
  | "ready"
  | "approving";

const investigationPhases = [
  { label: "Secure uploads", sponsor: "SiteTrace · AWS" },
  { label: "Read the plan", sponsor: "OpenAI Terra" },
  { label: "Index footage", sponsor: "TwelveLabs" },
  { label: "Connect cameras", sponsor: "TwelveLabs Jockey" },
  { label: "Normalize events", sponsor: "OpenAI Luna" },
  { label: "Build the graph", sponsor: "Neo4j" },
  { label: "Compare work", sponsor: "Neo4j · Terra" },
  { label: "Verify evidence", sponsor: "AWS Strands" },
  { label: "Draft & approve", sponsor: "Terra · Strands" },
] as const;

const terminalStatuses = new Set<CaseStatus>([
  "AWAITING_APPROVAL",
  "APPROVED",
  "COMPLETED",
  "FAILED",
]);

function apiUrl(base: string, path: string) {
  return `${base}${path}`;
}

function casePath(caseId: string) {
  return `/cases/${encodeURIComponent(caseId)}`;
}

async function responseError(response: Response) {
  const fallback = `Request failed with status ${response.status}.`;
  try {
    const payload = (await response.json()) as {
      detail?: unknown;
      error?: string;
      message?: string;
    };
    if (typeof payload.detail === "string") return payload.detail;
    if (
      payload.detail &&
      typeof payload.detail === "object" &&
      "message" in payload.detail &&
      typeof payload.detail.message === "string"
    ) {
      return payload.detail.message;
    }
    return payload.error ?? payload.message ?? fallback;
  } catch {
    const message = await response.text().catch(() => "");
    return message || fallback;
  }
}

async function requestJson<T>(
  base: string,
  path: string,
  init: RequestInit,
): Promise<T> {
  const response = await fetch(apiUrl(base, path), init);
  if (!response.ok) {
    throw new Error(await responseError(response));
  }
  return (await response.json()) as T;
}

function abortableDelay(milliseconds: number, signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    const finish = () => {
      signal.removeEventListener("abort", cancel);
      resolve();
    };
    const cancel = () => {
      window.clearTimeout(timeout);
      reject(new DOMException("Investigation cancelled", "AbortError"));
    };
    const timeout = window.setTimeout(finish, milliseconds);
    signal.addEventListener("abort", cancel, { once: true });
  });
}

function humanize(value: string) {
  return value
    .replaceAll("_", " ")
    .toLowerCase()
    .replace(/(^|\s)\S/g, (letter) => letter.toUpperCase());
}

function formatSeconds(value: number) {
  if (!Number.isFinite(value)) return "—";
  const rounded = Math.max(0, Math.floor(value));
  const hours = Math.floor(rounded / 3600);
  const minutes = Math.floor((rounded % 3600) / 60);
  const seconds = rounded % 60;
  if (hours > 0) {
    return [hours, minutes, seconds]
      .map((part) => String(part).padStart(2, "0"))
      .join(":");
  }
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function formatFileSize(size: number) {
  if (size < 1024 * 1024) return `${Math.max(1, Math.round(size / 1024))} KB`;
  return `${(size / (1024 * 1024)).toFixed(size > 10 * 1024 * 1024 ? 0 : 1)} MB`;
}

function fileNames(files: File[]) {
  if (!files.length) return "No file selected";
  if (files.length === 1) {
    return `${files[0].name} · ${formatFileSize(files[0].size)}`;
  }
  return `${files.length} files · ${formatFileSize(
    files.reduce((total, file) => total + file.size, 0),
  )}`;
}

function traceText(entry: SponsorTraceEntry) {
  return Object.values(entry)
    .filter((value) => typeof value === "string")
    .join(" ")
    .toLowerCase();
}

function tracePhase(entry: SponsorTraceEntry) {
  const value = traceText(entry);
  if (/publish|approval|report/.test(value)) return 8;
  if (/verify|compliance|evidence/.test(value)) return 7;
  if (/compare|deviation|graph diff/.test(value)) return 6;
  if (/graph|neo4j|write context/.test(value)) return 5;
  if (/normalize|luna/.test(value)) return 4;
  if (/connect|cross.camera|jockey/.test(value)) return 3;
  if (/video|camera|twelvelabs|footage/.test(value)) return 2;
  if (/jha|plan|parse|terra/.test(value)) return 1;
  if (/upload|storage|s3/.test(value)) return 0;
  return -1;
}

function entryValue(entry: SponsorTraceEntry, keys: string[]) {
  for (const key of keys) {
    const value = entry[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return "";
}

function statusLabel(status: CaseStatus) {
  if (status === "AWAITING_APPROVAL") return "Awaiting approval";
  return humanize(status);
}

function preferredScrollBehavior(): ScrollBehavior {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches
    ? "auto"
    : "smooth";
}

function isCaseRecord(value: unknown): value is CaseRecord {
  return Boolean(
    value &&
      typeof value === "object" &&
      "case_id" in value &&
      "status" in value,
  );
}

export function SiteTraceApp() {
  const [title, setTitle] = useState("");
  const [jha, setJha] = useState<File | null>(null);
  const [supportingDocuments, setSupportingDocuments] = useState<File[]>([]);
  const [siteMetadata, setSiteMetadata] = useState<File | null>(null);
  const [siteMap, setSiteMap] = useState<File | null>(null);
  const [videos, setVideos] = useState<File[]>([]);
  const [caseRecord, setCaseRecord] = useState<CaseRecord | null>(null);
  const [runState, setRunState] = useState<RunState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [activeView, setActiveView] = useState<WorkspaceView>("evidence");
  const [activeEventId, setActiveEventId] = useState<string | null>(null);
  const [activeClipId, setActiveClipId] = useState<string | null>(null);
  const [reviewer, setReviewer] = useState("");
  const [reviewConfirmed, setReviewConfirmed] = useState(false);
  const [approvalError, setApprovalError] = useState<string | null>(null);
  const [approvedLocally, setApprovedLocally] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const apiBase = useMemo(
    () =>
      (process.env.NEXT_PUBLIC_SITETRACE_API_URL ?? "").replace(/\/+$/, ""),
    [],
  );
  const investigation = caseRecord?.investigation ?? null;
  const events = useMemo(
    () =>
      [...(investigation?.events ?? [])].sort(
        (left, right) =>
          left.start_sec - right.start_sec ||
          left.camera_id.localeCompare(right.camera_id),
      ),
    [investigation?.events],
  );
  const evidenceClips = investigation?.evidence_clips ?? [];
  const activeEvent =
    events.find((event) => event.event_id === activeEventId) ?? events[0] ?? null;
  const activeClip =
    evidenceClips.find(
      (clip) => clip.evidence_clip_id === activeClipId,
    ) ??
    evidenceClips.find((clip) =>
      activeEvent?.evidence_clip_ids.includes(clip.evidence_clip_id),
    ) ??
    evidenceClips[0] ??
    null;
  const busy =
    runState === "uploading" ||
    runState === "investigating" ||
    runState === "approving";
  const approved =
    approvedLocally ||
    caseRecord?.status === "APPROVED" ||
    caseRecord?.status === "COMPLETED";

  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  const activePhase = useMemo(() => {
    if (investigation) return investigationPhases.length;
    if (runState === "uploading") return 0;
    if (runState !== "investigating") return -1;
    const traced = Math.max(
      -1,
      ...(caseRecord?.investigation?.sponsor_trace ?? []).map(tracePhase),
    );
    return Math.max(1, traced);
  }, [caseRecord?.investigation?.sponsor_trace, investigation, runState]);

  function selectSingleFile(
    event: ChangeEvent<HTMLInputElement>,
    setter: (file: File | null) => void,
  ) {
    setter(event.target.files?.[0] ?? null);
  }

  function selectFiles(
    event: ChangeEvent<HTMLInputElement>,
    setter: (files: File[]) => void,
  ) {
    setter(Array.from(event.target.files ?? []));
  }

  function validateUpload() {
    if (title.trim().length < 3) {
      return "Give this investigation a title with at least three characters.";
    }
    if (!jha) return "Add the approved JHA PDF.";
    if (!jha.name.toLowerCase().endsWith(".pdf")) {
      return "The approved JHA must be a PDF.";
    }
    if (!videos.length) return "Add at least one camera video.";
    if (
      supportingDocuments.some(
        (file) => !file.name.toLowerCase().endsWith(".pdf"),
      )
    ) {
      return "Supporting plans must be PDF files.";
    }
    if (siteMetadata && !siteMetadata.name.toLowerCase().endsWith(".json")) {
      return "Site metadata must be a JSON file.";
    }
    return null;
  }

  async function pollCase(caseId: string, signal: AbortSignal) {
    let latest: CaseRecord | null = null;
    for (let attempt = 0; attempt < 720; attempt += 1) {
      await abortableDelay(2500, signal);
      latest = await requestJson<CaseRecord>(
        apiBase,
        casePath(caseId),
        { method: "GET", signal },
      );
      setCaseRecord(latest);
      if (latest.investigation || terminalStatuses.has(latest.status)) {
        return latest;
      }
    }
    throw new Error(
      "The investigation is still processing. Keep the case ID and try again shortly.",
    );
  }

  async function submitInvestigation(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    const validationError = validateUpload();
    if (validationError) {
      setError(validationError);
      return;
    }

    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setError(null);
    setApprovedLocally(false);
    setReviewConfirmed(false);
    setCaseRecord(null);
    setRunState("uploading");

    const body = new FormData();
    body.append("title", title.trim());
    body.append("jha", jha as File);
    supportingDocuments.forEach((file) =>
      body.append("supporting_documents", file),
    );
    if (siteMetadata) body.append("site_metadata", siteMetadata);
    if (siteMap) body.append("site_map", siteMap);
    videos.forEach((file) => body.append("videos", file));

    try {
      const created = await requestJson<CaseRecord>(apiBase, "/cases", {
        method: "POST",
        body,
        signal: controller.signal,
      });
      if (!created.case_id) {
        throw new Error("The server did not return a case ID.");
      }
      setCaseRecord(created);
      setRunState("investigating");

      let investigated = await requestJson<CaseRecord>(
        apiBase,
        `${casePath(created.case_id)}/investigate`,
        { method: "POST", signal: controller.signal },
      );
      setCaseRecord(investigated);
      if (
        !investigated.investigation &&
        !terminalStatuses.has(investigated.status)
      ) {
        investigated = await pollCase(created.case_id, controller.signal);
      }
      if (investigated.status === "FAILED") {
        throw new Error(
          investigated.error || "The investigation could not be completed.",
        );
      }
      const completedInvestigation = investigated.investigation;
      if (!completedInvestigation) {
        throw new Error(
          "The case completed without an investigation package.",
        );
      }
      setCaseRecord(investigated);
      const firstEvent = [...completedInvestigation.events].sort(
        (left, right) => left.start_sec - right.start_sec,
      )[0];
      setActiveEventId(firstEvent?.event_id ?? null);
      setActiveClipId(
        firstEvent?.evidence_clip_ids[0] ??
          completedInvestigation.evidence_clips[0]?.evidence_clip_id ??
          null,
      );
      setActiveView("evidence");
      setRunState("ready");
      window.requestAnimationFrame(() => {
        const workspace = document.getElementById("investigation-workspace");
        workspace?.focus({ preventScroll: true });
        workspace?.scrollIntoView({
          behavior: preferredScrollBehavior(),
          block: "start",
        });
      });
    } catch (requestFailure) {
      if (
        requestFailure instanceof DOMException &&
        requestFailure.name === "AbortError"
      ) {
        return;
      }
      setError(
        requestFailure instanceof Error
          ? requestFailure.message
          : "The investigation request failed.",
      );
      setRunState("idle");
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
    }
  }

  function chooseEvent(event: ObservedEvent) {
    setActiveEventId(event.event_id);
    setActiveClipId(event.evidence_clip_ids[0] ?? null);
  }

  function openEvidence(clipId: string) {
    const event = events.find((item) =>
      item.evidence_clip_ids.includes(clipId),
    );
    if (event) setActiveEventId(event.event_id);
    setActiveClipId(clipId);
    setActiveView("evidence");
    window.requestAnimationFrame(() => {
      const content = document.getElementById("workspace-content");
      content?.focus({ preventScroll: true });
      content?.scrollIntoView({
        behavior: preferredScrollBehavior(),
        block: "start",
      });
    });
  }

  async function approveReport() {
    if (!caseRecord || !reviewConfirmed || !reviewer.trim() || busy) return;
    setRunState("approving");
    setApprovalError(null);
    try {
      const approval = await requestJson<unknown>(
        apiBase,
        `${casePath(caseRecord.case_id)}/approve`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            reviewer: reviewer.trim(),
            approved: true,
          }),
        },
      );
      const refreshed = isCaseRecord(approval)
        ? approval
        : await requestJson<CaseRecord>(apiBase, casePath(caseRecord.case_id), {
            method: "GET",
          });
      setCaseRecord(refreshed);
      setApprovedLocally(true);
      setRunState("ready");
    } catch (approvalFailure) {
      setApprovalError(
        approvalFailure instanceof Error
          ? approvalFailure.message
          : "Approval could not be recorded.",
      );
      setRunState("ready");
    }
  }

  function resetWorkspace() {
    abortRef.current?.abort();
    abortRef.current = null;
    setTitle("");
    setJha(null);
    setSupportingDocuments([]);
    setSiteMetadata(null);
    setSiteMap(null);
    setVideos([]);
    setCaseRecord(null);
    setRunState("idle");
    setError(null);
    setActiveView("evidence");
    setActiveEventId(null);
    setActiveClipId(null);
    setReviewer("");
    setReviewConfirmed(false);
    setApprovalError(null);
    setApprovedLocally(false);
    window.requestAnimationFrame(() => {
      const titleInput = document.getElementById("investigation-title");
      titleInput?.focus({ preventScroll: true });
      window.scrollTo({ top: 0, behavior: preferredScrollBehavior() });
    });
  }

  return (
    <main className="site-shell">
      <header className="site-header">
        <a className="brand" href="#top" aria-label="SiteTrace home">
          <Image
            src="/sitetrace-logo.png"
            alt=""
            width={36}
            height={36}
            priority
          />
          <span>SiteTrace</span>
        </a>
        {caseRecord ? (
          <div className="case-pill" aria-label="Current case">
            <span>{caseRecord.case_id}</span>
            <strong>{statusLabel(caseRecord.status)}</strong>
          </div>
        ) : (
          <p className="header-purpose">Evidence-led safety investigations</p>
        )}
        <button
          className="text-button"
          type="button"
          onClick={resetWorkspace}
          disabled={busy && runState !== "investigating"}
        >
          {runState === "investigating"
            ? "Cancel investigation"
            : caseRecord
              ? "New investigation"
              : "Clear files"}
        </button>
      </header>

      <section className="hero" id="top">
        <p className="eyebrow">Post-incident investigation workspace</p>
        <h1>
          Reconstruct the work.
          <br />
          Keep every finding traceable.
        </h1>
        <p className="hero-copy">
          Upload the approved plan and all relevant camera footage. SiteTrace
          rebuilds the observed sequence, compares it with the JHA, and prepares
          a reviewable investigation document with source-video citations.
        </p>
      </section>

      <form
        className="upload-panel"
        onSubmit={submitInvestigation}
        aria-labelledby="upload-title"
        aria-busy={runState === "uploading" || runState === "investigating"}
      >
        <div className="upload-intro">
          <div>
            <p className="step-label">01 · Investigation inputs</p>
            <h2 id="upload-title">Start with the source material</h2>
          </div>
          <p>
            Only the JHA and camera footage are required. Add site context when
            it is available.
          </p>
        </div>

        <label className="title-field" htmlFor="investigation-title">
          <span>Investigation title</span>
          <input
            id="investigation-title"
            type="text"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="Name the event or issue under review"
            minLength={3}
            maxLength={160}
            required
            disabled={busy}
          />
        </label>

        <div className="upload-grid">
          <UploadField
            id="jha-upload"
            label="Approved JHA"
            hint="One PDF · required"
            value={jha ? fileNames([jha]) : "Choose the governing JHA"}
            selected={Boolean(jha)}
            required
          >
            <input
              id="jha-upload"
              type="file"
              accept=".pdf,application/pdf"
              onChange={(event) => selectSingleFile(event, setJha)}
              required
              disabled={busy}
            />
          </UploadField>

          <UploadField
            id="video-upload"
            label="Camera footage"
            hint="Multiple video files · required"
            value={
              videos.length
                ? fileNames(videos)
                : "Choose all relevant camera videos"
            }
            selected={videos.length > 0}
            required
          >
            <input
              id="video-upload"
              type="file"
              accept="video/*"
              multiple
              onChange={(event) => selectFiles(event, setVideos)}
              required
              disabled={busy}
            />
          </UploadField>

          <UploadField
            id="support-upload"
            label="Supporting plans"
            hint="Additional PDFs · optional"
            value={
              supportingDocuments.length
                ? fileNames(supportingDocuments)
                : "Add lift plans or related documents"
            }
            selected={supportingDocuments.length > 0}
          >
            <input
              id="support-upload"
              type="file"
              accept=".pdf,application/pdf"
              multiple
              onChange={(event) =>
                selectFiles(event, setSupportingDocuments)
              }
              disabled={busy}
            />
          </UploadField>

          <UploadField
            id="metadata-upload"
            label="Site metadata"
            hint="Camera IDs and start times · optional"
            value={
              siteMetadata
                ? fileNames([siteMetadata])
                : "Add a site_metadata.json file"
            }
            selected={Boolean(siteMetadata)}
          >
            <input
              id="metadata-upload"
              type="file"
              accept=".json,application/json"
              onChange={(event) => selectSingleFile(event, setSiteMetadata)}
              disabled={busy}
            />
          </UploadField>

          <UploadField
            id="map-upload"
            label="Site map"
            hint="PNG, JPG, or WebP · optional"
            value={siteMap ? fileNames([siteMap]) : "Add a marked-up site map"}
            selected={Boolean(siteMap)}
          >
            <input
              id="map-upload"
              type="file"
              accept="image/png,image/jpeg,image/webp"
              onChange={(event) => selectSingleFile(event, setSiteMap)}
              disabled={busy}
            />
          </UploadField>
        </div>

        {error && (
          <div className="error-banner" role="alert">
            <strong>We couldn&apos;t continue.</strong>
            <span>{error}</span>
          </div>
        )}

        <div className="upload-actions">
          <p>
            {jha && videos.length
              ? `${videos.length} camera file${videos.length === 1 ? "" : "s"} ready for investigation`
              : "Your files remain unchanged until you start the investigation."}
          </p>
          <button className="primary-button" type="submit" disabled={busy}>
            {runState === "uploading"
              ? "Uploading source material…"
              : runState === "investigating"
                ? "Investigation in progress…"
                : "Start investigation"}
          </button>
        </div>
      </form>

      <section className="phase-section" aria-labelledby="phase-title">
        <div className="section-kicker">
          <p className="step-label">02 · Evidence workflow</p>
          <h2 id="phase-title">Nine bounded phases, one audit trail</h2>
          <span role="status" aria-live="polite" aria-atomic="true">
            {runState === "investigating"
              ? "Processing"
              : investigation
                ? "Analysis complete"
                : "Waiting for source files"}
          </span>
        </div>
        <ol
          className="phase-list"
          aria-label="Investigation phases"
          tabIndex={0}
        >
          {investigationPhases.map((phase, index) => {
            const state =
              activePhase > index
                ? "complete"
                : activePhase === index
                  ? "active"
                  : "pending";
            return (
              <li
                className={state}
                key={phase.label}
                aria-current={state === "active" ? "step" : undefined}
              >
                <span className="phase-number">
                  {state === "complete" ? "✓" : String(index + 1).padStart(2, "0")}
                </span>
                <strong>{phase.label}</strong>
                <small>{phase.sponsor}</small>
              </li>
            );
          })}
        </ol>
      </section>

      {investigation && caseRecord ? (
        <InvestigationWorkspace
          apiBase={apiBase}
          record={caseRecord}
          investigation={investigation}
          events={events}
          activeEvent={activeEvent}
          activeClip={activeClip}
          activeClipId={activeClipId}
          activeView={activeView}
          setActiveView={setActiveView}
          chooseEvent={chooseEvent}
          setActiveClipId={setActiveClipId}
          openEvidence={openEvidence}
          reviewer={reviewer}
          setReviewer={setReviewer}
          reviewConfirmed={reviewConfirmed}
          setReviewConfirmed={setReviewConfirmed}
          approveReport={approveReport}
          approvalError={approvalError}
          approved={approved}
          approving={runState === "approving"}
        />
      ) : (
        <section className="empty-workspace" aria-labelledby="empty-title">
          <div className="empty-marker" aria-hidden="true">
            <span />
            <span />
            <span />
          </div>
          <p className="step-label">03 · Investigation workspace</p>
          <h2 id="empty-title">Results begin with your evidence.</h2>
          <p>
            The timeline, plan comparison, graph paths, and report will appear
            here only after your files have been analyzed. SiteTrace does not
            preload sample findings or infer facts from missing footage.
          </p>
        </section>
      )}

      <footer className="site-footer">
        <p>
          SiteTrace reconstructs observed contributing events. It does not
          determine organizational root cause from video alone.
        </p>
        <div>
          <span>TwelveLabs</span>
          <span>OpenAI</span>
          <span>Neo4j</span>
          <span>AWS Strands</span>
        </div>
      </footer>
    </main>
  );
}

function UploadField({
  id,
  label,
  hint,
  value,
  selected,
  required = false,
  children,
}: {
  id: string;
  label: string;
  hint: string;
  value: string;
  selected: boolean;
  required?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className={`upload-field ${selected ? "selected" : ""}`}>
      <div>
        <label htmlFor={id}>
          {label}
          {required && <span aria-hidden="true"> *</span>}
        </label>
        <small>{hint}</small>
      </div>
      <p>{value}</p>
      {children}
      <label className="file-trigger" htmlFor={id}>
        {selected ? "Replace" : "Choose file"}
      </label>
    </div>
  );
}

function InvestigationWorkspace({
  apiBase,
  record,
  investigation,
  events,
  activeEvent,
  activeClip,
  activeClipId,
  activeView,
  setActiveView,
  chooseEvent,
  setActiveClipId,
  openEvidence,
  reviewer,
  setReviewer,
  reviewConfirmed,
  setReviewConfirmed,
  approveReport,
  approvalError,
  approved,
  approving,
}: {
  apiBase: string;
  record: CaseRecord;
  investigation: Investigation;
  events: ObservedEvent[];
  activeEvent: ObservedEvent | null;
  activeClip: EvidenceClip | null;
  activeClipId: string | null;
  activeView: WorkspaceView;
  setActiveView: (view: WorkspaceView) => void;
  chooseEvent: (event: ObservedEvent) => void;
  setActiveClipId: (clipId: string) => void;
  openEvidence: (clipId: string) => void;
  reviewer: string;
  setReviewer: (value: string) => void;
  reviewConfirmed: boolean;
  setReviewConfirmed: (value: boolean) => void;
  approveReport: () => void;
  approvalError: string | null;
  approved: boolean;
  approving: boolean;
}) {
  const graphMetrics = Object.entries(investigation.graph_metrics ?? {});

  return (
    <section
      className="workspace"
      id="investigation-workspace"
      aria-labelledby="workspace-title"
      tabIndex={-1}
    >
      <header className="workspace-header">
        <div>
          <div className="workspace-meta">
            <span>{record.case_id}</span>
            <span className={`record-status status-${record.status.toLowerCase()}`}>
              {statusLabel(record.status)}
            </span>
          </div>
          <h2 id="workspace-title">{investigation.title || record.title}</h2>
          <p>{investigation.incident_summary}</p>
        </div>
        <div className="workspace-counts" aria-label="Investigation counts">
          <Metric value={events.length} label="Observed events" />
          <Metric
            value={investigation.evidence_clips.length}
            label="Evidence clips"
          />
          <Metric
            value={investigation.findings.length}
            label="JHA findings"
          />
          <Metric
            value={graphMetrics.length}
            label="Graph metrics"
          />
        </div>
      </header>

      <nav className="workspace-tabs" aria-label="Investigation views">
        <button
          type="button"
          className={activeView === "evidence" ? "active" : ""}
          onClick={() => setActiveView("evidence")}
          aria-pressed={activeView === "evidence"}
        >
          Evidence timeline
        </button>
        <button
          type="button"
          className={activeView === "comparison" ? "active" : ""}
          onClick={() => setActiveView("comparison")}
          aria-pressed={activeView === "comparison"}
        >
          Plan vs. observed
        </button>
        <button
          type="button"
          className={activeView === "report" ? "active" : ""}
          onClick={() => setActiveView("report")}
          aria-pressed={activeView === "report"}
        >
          Report & approval
          {approved && (
            <>
              <i aria-hidden="true" />
              <span className="sr-only">Approved</span>
            </>
          )}
        </button>
      </nav>

      <div id="workspace-content" tabIndex={-1}>
        {activeView === "evidence" && (
          <EvidenceView
            apiBase={apiBase}
            caseId={record.case_id}
            events={events}
            evidenceClips={investigation.evidence_clips}
            activeEvent={activeEvent}
            activeClip={activeClip}
            activeClipId={activeClipId}
            chooseEvent={chooseEvent}
            setActiveClipId={setActiveClipId}
          />
        )}
        {activeView === "comparison" && (
          <ComparisonView
            steps={investigation.planned_steps}
            events={events}
            findings={investigation.findings}
            graphMetrics={investigation.graph_metrics}
            openEvidence={openEvidence}
          />
        )}
        {activeView === "report" && (
          <ReportView
            apiBase={apiBase}
            record={record}
            investigation={investigation}
            reviewer={reviewer}
            setReviewer={setReviewer}
            reviewConfirmed={reviewConfirmed}
            setReviewConfirmed={setReviewConfirmed}
            approveReport={approveReport}
            approvalError={approvalError}
            approved={approved}
            approving={approving}
            openEvidence={openEvidence}
          />
        )}
      </div>
    </section>
  );
}

function Metric({ value, label }: { value: number; label: string }) {
  return (
    <div>
      <strong>{value.toLocaleString()}</strong>
      <span>{label}</span>
    </div>
  );
}

function EvidenceView({
  apiBase,
  caseId,
  events,
  evidenceClips,
  activeEvent,
  activeClip,
  activeClipId,
  chooseEvent,
  setActiveClipId,
}: {
  apiBase: string;
  caseId: string;
  events: ObservedEvent[];
  evidenceClips: EvidenceClip[];
  activeEvent: ObservedEvent | null;
  activeClip: EvidenceClip | null;
  activeClipId: string | null;
  chooseEvent: (event: ObservedEvent) => void;
  setActiveClipId: (clipId: string) => void;
}) {
  const eventClips = activeEvent
    ? activeEvent.evidence_clip_ids
        .map((clipId) =>
          evidenceClips.find((clip) => clip.evidence_clip_id === clipId),
        )
        .filter((clip): clip is EvidenceClip => Boolean(clip))
    : [];

  return (
    <div className="evidence-layout">
      <aside className="timeline-panel" aria-labelledby="timeline-title">
        <div className="panel-heading">
          <div>
            <p className="step-label">Observed sequence</p>
            <h3 id="timeline-title">
              {events.length
                ? `${events.length} evidence-backed event${events.length === 1 ? "" : "s"}`
                : "No observed events"}
            </h3>
          </div>
          <span>{new Set(events.map((event) => event.camera_id)).size} cameras</span>
        </div>
        {events.length ? (
          <ol className="event-list">
            {events.map((event, index) => (
              <li key={event.event_id}>
                <button
                  type="button"
                  className={
                    activeEvent?.event_id === event.event_id ? "active" : ""
                  }
                  onClick={() => chooseEvent(event)}
                  aria-pressed={activeEvent?.event_id === event.event_id}
                >
                  <span className="event-index">
                    {String(index + 1).padStart(2, "0")}
                  </span>
                  <span className="event-copy">
                    <small>
                      {event.camera_id} · {formatSeconds(event.start_sec)}–
                      {formatSeconds(event.end_sec)}
                    </small>
                    <strong>{humanize(event.event_type)}</strong>
                    <p>{event.summary}</p>
                  </span>
                  <span className={`observation-type ${event.observation_type}`}>
                    {humanize(event.observation_type)}
                  </span>
                </button>
              </li>
            ))}
          </ol>
        ) : (
          <InlineEmpty message="No evidence-backed events were returned." />
        )}
      </aside>

      <article className="player-panel" aria-labelledby="player-title">
        <div className="panel-heading player-heading">
          <div>
            <p className="step-label">Cited source video</p>
            <h3 id="player-title">
              {activeClip?.summary || "Select an evidence clip"}
            </h3>
          </div>
          {activeClip && (
            <span>{Math.round(activeClip.confidence * 100)}% confidence</span>
          )}
        </div>

        {activeClip ? (
          <>
            <div className="video-frame">
              <video
                key={activeClip.evidence_clip_id}
                controls
                preload="metadata"
                aria-label={`Evidence clip ${activeClip.evidence_clip_id}: ${activeClip.summary}`}
                src={apiUrl(
                  apiBase,
                  `${casePath(caseId)}/evidence/${encodeURIComponent(
                    activeClip.evidence_clip_id,
                  )}#t=${activeClip.start_sec},${activeClip.end_sec}`,
                )}
              >
                Your browser does not support video playback.
              </video>
              <div className="video-caption">
                <span>{activeClip.camera_id}</span>
                <span>
                  {formatSeconds(activeClip.start_sec)}–
                  {formatSeconds(activeClip.end_sec)}
                </span>
              </div>
            </div>

            {eventClips.length > 1 && (
              <div className="clip-switcher" aria-label="Evidence clips for event">
                {eventClips.map((clip) => (
                  <button
                    type="button"
                    key={clip.evidence_clip_id}
                    className={
                      activeClipId === clip.evidence_clip_id ? "active" : ""
                    }
                    onClick={() => setActiveClipId(clip.evidence_clip_id)}
                    aria-pressed={activeClipId === clip.evidence_clip_id}
                  >
                    {clip.camera_id} · {formatSeconds(clip.start_sec)}
                  </button>
                ))}
              </div>
            )}

            <dl className="clip-facts">
              <div>
                <dt>Evidence ID</dt>
                <dd>{activeClip.evidence_clip_id}</dd>
              </div>
              <div>
                <dt>Source</dt>
                <dd>{activeClip.source_filename}</dd>
              </div>
              <div>
                <dt>Camera</dt>
                <dd>{activeClip.camera_id}</dd>
              </div>
            </dl>

            {(activeClip.transcript || activeClip.visible_text) && (
              <div className="clip-observations">
                {activeClip.transcript && (
                  <div>
                    <span>Audio transcript</span>
                    <p>{activeClip.transcript}</p>
                  </div>
                )}
                {activeClip.visible_text && (
                  <div>
                    <span>Visible text</span>
                    <p>{activeClip.visible_text}</p>
                  </div>
                )}
              </div>
            )}
          </>
        ) : (
          <div className="video-empty">
            <span aria-hidden="true">▶</span>
            <p>No playable evidence clip was returned for this event.</p>
          </div>
        )}
      </article>
    </div>
  );
}

function ComparisonView({
  steps,
  events,
  findings,
  graphMetrics,
  openEvidence,
}: {
  steps: PlannedStep[];
  events: ObservedEvent[];
  findings: DeviationFinding[];
  graphMetrics: Record<string, number>;
  openEvidence: (clipId: string) => void;
}) {
  const linkedStepIds = new Set(steps.map((step) => step.step_id));
  const unlinkedFindings = findings.filter(
    (finding) => !linkedStepIds.has(finding.jha_step_id),
  );
  const pathFindings = findings.filter(
    (finding) =>
      finding.graph_path_node_ids.length ||
      finding.graph_path_relationships.length,
  );

  return (
    <div className="comparison-layout">
      <section className="plan-panel" aria-labelledby="plan-title">
        <div className="panel-heading">
          <div>
            <p className="step-label">JHA comparison</p>
            <h3 id="plan-title">Approved plan vs. observed work</h3>
          </div>
          <span>{steps.length} planned steps</span>
        </div>

        {steps.length ? (
          <div className="step-list">
            {[...steps]
              .sort((left, right) => left.sequence - right.sequence)
              .map((step) => {
                const stepFindings = findings.filter(
                  (finding) => finding.jha_step_id === step.step_id,
                );
                return (
                  <article className="plan-step" key={step.step_id}>
                    <header>
                      <span>{String(step.sequence).padStart(2, "0")}</span>
                      <div>
                        <small>{step.step_id}</small>
                        <h4>{step.name}</h4>
                        {step.description && <p>{step.description}</p>}
                      </div>
                    </header>
                    <div className="control-lines">
                      {step.required_controls.map((control) => (
                        <span key={`control-${control}`}>
                          <b>Control</b>
                          {control}
                        </span>
                      ))}
                      {step.required_roles.map((role) => (
                        <span key={`role-${role}`}>
                          <b>Role</b>
                          {role}
                        </span>
                      ))}
                      {step.must_avoid_zones.map((zone) => (
                        <span key={`zone-${zone}`}>
                          <b>Avoid</b>
                          {zone}
                        </span>
                      ))}
                    </div>
                    <div className="step-findings">
                      {stepFindings.length ? (
                        stepFindings.map((finding) => (
                          <FindingRow
                            key={finding.finding_id}
                            finding={finding}
                            openEvidence={openEvidence}
                          />
                        ))
                      ) : (
                        <p className="no-linked-finding">
                          No finding is linked to this plan step.
                        </p>
                      )}
                    </div>
                  </article>
                );
              })}
          </div>
        ) : (
          <InlineEmpty message="No planned JHA steps were returned." />
        )}

        {unlinkedFindings.length > 0 && (
          <section className="unlinked-findings">
            <h4>Findings not linked to a parsed step</h4>
            {unlinkedFindings.map((finding) => (
              <FindingRow
                key={finding.finding_id}
                finding={finding}
                openEvidence={openEvidence}
              />
            ))}
          </section>
        )}
      </section>

      <aside className="graph-panel" aria-labelledby="graph-title">
        <div className="panel-heading">
          <div>
            <p className="step-label">Neo4j context graph</p>
            <h3 id="graph-title">Metrics & evidence paths</h3>
          </div>
        </div>
        {Object.keys(graphMetrics).length ? (
          <dl className="graph-metrics">
            {Object.entries(graphMetrics).map(([label, value]) => (
              <div key={label}>
                <dt>{humanize(label)}</dt>
                <dd>{value.toLocaleString()}</dd>
              </div>
            ))}
          </dl>
        ) : (
          <InlineEmpty message="No graph metrics were returned." />
        )}

        <div className="graph-paths">
          <h4>Evidence-linked paths</h4>
          {pathFindings.length ? (
            pathFindings.map((finding) => (
              <GraphPath key={finding.finding_id} finding={finding} />
            ))
          ) : (
            <p className="graph-empty">
              No graph path was attached to the current findings.
            </p>
          )}
        </div>

        <div className="observation-summary">
          <span>{events.length}</span>
          <p>
            observed event{events.length === 1 ? "" : "s"} available to the
            graph comparison
          </p>
        </div>
      </aside>
    </div>
  );
}

function FindingRow({
  finding,
  openEvidence,
}: {
  finding: DeviationFinding;
  openEvidence: (clipId: string) => void;
}) {
  return (
    <article className={`finding-row finding-${finding.status.toLowerCase()}`}>
      <div className="finding-title">
        <span>{humanize(finding.status)}</span>
        <strong>{finding.title}</strong>
        <small>{Math.round(finding.confidence * 100)}% confidence</small>
      </div>
      <div className="finding-comparison">
        <div>
          <span>Planned control</span>
          <p>{finding.planned_control}</p>
        </div>
        <div>
          <span>Observed work</span>
          <p>
            {finding.observed_work ||
              finding.evidence_gap_reason ||
              "No observed-work statement was returned."}
          </p>
        </div>
      </div>
      {finding.evidence_clip_ids.length > 0 && (
        <div className="citation-row">
          <span>Evidence</span>
          {finding.evidence_clip_ids.map((clipId) => (
            <button
              type="button"
              key={clipId}
              onClick={() => openEvidence(clipId)}
            >
              {clipId}
            </button>
          ))}
        </div>
      )}
    </article>
  );
}

function GraphPath({ finding }: { finding: DeviationFinding }) {
  const path: { type: "node" | "relationship"; value: string }[] = [];
  finding.graph_path_node_ids.forEach((node, index) => {
    path.push({ type: "node", value: node });
    const relationship = finding.graph_path_relationships[index];
    if (relationship) path.push({ type: "relationship", value: relationship });
  });
  finding.graph_path_relationships
    .slice(finding.graph_path_node_ids.length)
    .forEach((relationship) =>
      path.push({ type: "relationship", value: relationship }),
    );

  return (
    <article className="graph-path">
      <span>{finding.finding_id}</span>
      <strong>{finding.title}</strong>
      <div>
        {path.map((item, index) => (
          <span
            className={item.type}
            key={`${item.type}-${item.value}-${index}`}
          >
            {item.value}
          </span>
        ))}
      </div>
    </article>
  );
}

function ReportView({
  apiBase,
  record,
  investigation,
  reviewer,
  setReviewer,
  reviewConfirmed,
  setReviewConfirmed,
  approveReport,
  approvalError,
  approved,
  approving,
  openEvidence,
}: {
  apiBase: string;
  record: CaseRecord;
  investigation: Investigation;
  reviewer: string;
  setReviewer: (value: string) => void;
  reviewConfirmed: boolean;
  setReviewConfirmed: (value: boolean) => void;
  approveReport: () => void;
  approvalError: string | null;
  approved: boolean;
  approving: boolean;
  openEvidence: (clipId: string) => void;
}) {
  return (
    <div className="report-layout">
      <article className="report-document">
        <header className="report-document-header">
          <div className="brand report-brand">
            <Image
              src="/sitetrace-logo.png"
              alt=""
              width={30}
              height={30}
            />
            <span>SiteTrace</span>
          </div>
          <div>
            <small>Case ID</small>
            <strong>{record.case_id}</strong>
          </div>
        </header>

            <div className="report-title">
              <p className="step-label">Near-miss / incident investigation</p>
              <h2>{investigation.title}</h2>
              <p>{investigation.incident_summary}</p>
            </div>

            {!!investigation.incident_overview?.length && (
              <ReportSection title="Incident statement and scope">
                <CitedNarrative
                  claims={investigation.incident_overview}
                  openEvidence={openEvidence}
                />
              </ReportSection>
            )}

            {!!investigation.event_timeline?.length && (
              <ReportSection title="Multi-camera chronology">
                <CitedNarrative
                  claims={investigation.event_timeline}
                  openEvidence={openEvidence}
                />
              </ReportSection>
            )}

            {!!investigation.deviation_summary?.length && (
              <ReportSection title="JHA deviation analysis">
                <CitedNarrative
                  claims={investigation.deviation_summary}
                  openEvidence={openEvidence}
                />
              </ReportSection>
            )}

            <ReportSection title="JHA variance review">
          {investigation.findings.length ? (
            <div className="report-findings">
              {investigation.findings.map((finding, index) => (
                <article key={finding.finding_id}>
                  <span>{String(index + 1).padStart(2, "0")}</span>
                  <div>
                    <small>{humanize(finding.status)}</small>
                    <h4>{finding.title}</h4>
                    <p>
                      {finding.observed_work ||
                        finding.evidence_gap_reason ||
                        finding.planned_control}
                    </p>
                    <div className="report-citations">
                      {finding.evidence_clip_ids.map((clipId) => (
                        <button
                          type="button"
                          key={clipId}
                          onClick={() => openEvidence(clipId)}
                        >
                          {clipId}
                        </button>
                      ))}
                    </div>
                  </div>
                </article>
              ))}
            </div>
          ) : (
            <InlineEmpty message="No JHA variance findings were returned." />
          )}
        </ReportSection>

        <ReportSection title="Corrective and preventive actions">
          {investigation.corrective_actions.length ? (
            <div className="action-list">
              {investigation.corrective_actions.map((action) => (
                <article key={action.action_id}>
                  <div>
                    <span>{humanize(action.action_type)}</span>
                    <small>{action.status}</small>
                  </div>
                  <h4>{action.description}</h4>
                  <p>
                    Owner: {action.owner_role}
                    {action.due_date ? ` · Due ${action.due_date}` : ""}
                  </p>
                </article>
              ))}
            </div>
          ) : (
            <InlineEmpty message="No corrective actions were returned." />
          )}
        </ReportSection>

        <ReportSection title="Known limitations">
          {investigation.limitations.length ? (
            <ul className="limitation-list">
              {investigation.limitations.map((limitation, index) => (
                <li key={`${limitation}-${index}`}>{limitation}</li>
              ))}
            </ul>
          ) : (
            <p className="report-plain">No limitations were recorded.</p>
          )}
        </ReportSection>

        <ReportSection title="Evidence annex">
          {investigation.evidence_clips.length ? (
            <div className="annex-list">
              {investigation.evidence_clips.map((clip) => (
                <button
                  type="button"
                  key={clip.evidence_clip_id}
                  onClick={() => openEvidence(clip.evidence_clip_id)}
                >
                  <span>{clip.evidence_clip_id}</span>
                  <strong>{clip.summary}</strong>
                  <small>
                    {clip.camera_id} · {formatSeconds(clip.start_sec)}–
                    {formatSeconds(clip.end_sec)}
                  </small>
                </button>
              ))}
            </div>
          ) : (
            <InlineEmpty message="No evidence annex clips were returned." />
          )}
        </ReportSection>

        {investigation.sponsor_trace.length > 0 && (
          <ReportSection title="Processing audit trail">
            <div className="trace-list">
              {investigation.sponsor_trace.map((entry, index) => {
                const stage =
                  entryValue(entry, ["stage", "name", "action"]) ||
                  `Phase ${index + 1}`;
                const sponsor =
                  entryValue(entry, ["sponsor", "owner", "service"]) ||
                  "SiteTrace";
                const detail = entryValue(entry, [
                  "detail",
                  "message",
                  "status",
                ]);
                return (
                  <div key={`${stage}-${index}`}>
                    <span>{sponsor}</span>
                    <strong>{humanize(stage)}</strong>
                    {detail && <p>{detail}</p>}
                  </div>
                );
              })}
            </div>
          </ReportSection>
        )}
      </article>

      <aside className="approval-panel" aria-labelledby="approval-title">
        <p className="step-label">Human review gate</p>
        <h3 id="approval-title" aria-live="polite">
          {approved ? "Report approved" : "Approve before PDF generation"}
        </h3>
        <p>
          Confirm the cited clips and findings. Approval records the reviewer
          and releases the final investigation PDF.
        </p>

        <label>
          Reviewer
          <input
            type="text"
            value={reviewer}
            onChange={(event) => setReviewer(event.target.value)}
            placeholder="Safety manager name"
            disabled={approved || approving}
          />
        </label>
        <label className="review-check">
          <input
            type="checkbox"
            checked={reviewConfirmed}
            onChange={(event) => setReviewConfirmed(event.target.checked)}
            disabled={approved || approving}
          />
          <span>I reviewed the findings and their cited evidence.</span>
        </label>

        {approvalError && (
          <div className="error-banner approval-error" role="alert">
            <span>{approvalError}</span>
          </div>
        )}

        {!approved ? (
          <button
            className="primary-button full-width"
            type="button"
            onClick={approveReport}
            disabled={!reviewer.trim() || !reviewConfirmed || approving}
          >
            {approving ? "Recording approval…" : "Approve investigation"}
          </button>
        ) : (
          <a
            className="primary-button report-link"
            href={apiUrl(
              apiBase,
              `/reports/${encodeURIComponent(record.case_id)}.pdf`,
            )}
            target="_blank"
            rel="noreferrer"
          >
            Open final PDF
            <span className="sr-only"> (opens in a new tab)</span>
          </a>
        )}

        <dl className="approval-details">
          <div>
            <dt>Status</dt>
            <dd>{statusLabel(record.status)}</dd>
          </div>
          {record.approved_by && (
            <div>
              <dt>Approved by</dt>
              <dd>{record.approved_by}</dd>
            </div>
          )}
          {record.approved_at && (
            <div>
              <dt>Approved at</dt>
              <dd>{new Date(record.approved_at).toLocaleString()}</dd>
            </div>
          )}
        </dl>
      </aside>
    </div>
  );
}

function ReportSection({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="report-section">
      <h3>{title}</h3>
      {children}
    </section>
  );
}

function CitedNarrative({
  claims,
  openEvidence,
}: {
  claims: CitedNarrativeClaim[];
  openEvidence: (clipId: string) => void;
}) {
  return (
    <div className="cited-narrative">
      {claims.map((claim) => (
        <article key={claim.claim_id}>
          <p>{claim.text}</p>
          <div className="report-citations" aria-label="Supporting evidence">
            {claim.evidence_clip_ids.map((clipId) => (
              <button
                type="button"
                key={clipId}
                onClick={() => openEvidence(clipId)}
              >
                {clipId}
              </button>
            ))}
          </div>
        </article>
      ))}
    </div>
  );
}

function InlineEmpty({ message }: { message: string }) {
  return <p className="inline-empty">{message}</p>;
}
