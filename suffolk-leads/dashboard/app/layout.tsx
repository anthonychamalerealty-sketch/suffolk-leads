import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Suffolk Leads Dashboard",
  description: "Suffolk County real estate leads management system",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
