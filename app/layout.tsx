import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  metadataBase: new URL(
    process.env.NEXT_PUBLIC_SITE_URL ?? "https://sitetrace.openai.site",
  ),
  title: {
    default: "SiteTrace",
    template: "%s · SiteTrace",
  },
  description:
    "Reconstruct observed work across cameras, compare it with the approved JHA, and prepare an evidence-backed safety investigation.",
  icons: {
    icon: "/favicon.svg",
    shortcut: "/favicon.svg",
  },
  openGraph: {
    title: "SiteTrace",
    description:
      "From source footage and approved plans to one auditable safety investigation.",
    type: "website",
    images: [
      {
        url: "/og.png",
        width: 1200,
        height: 630,
        alt: "SiteTrace evidence-backed incident investigations",
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    title: "SiteTrace",
    description:
      "From source footage and approved plans to one auditable safety investigation.",
    images: ["/og.png"],
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased`}
      >
        {children}
      </body>
    </html>
  );
}
