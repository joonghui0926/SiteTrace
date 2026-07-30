import type { Metadata } from "next";
import { SiteTraceApp } from "./SiteTraceApp";

export const metadata: Metadata = {
  title: "Evidence-backed incident investigations",
  description:
    "Upload the JHA and multi-camera footage to reconstruct observed work, review cited findings, and approve the final investigation report.",
};

export default function Home() {
  return <SiteTraceApp />;
}
