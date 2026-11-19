import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";

export const metadata: Metadata = {
  title: "Incident Ops | 장애 대응 워크스페이스",
  description: "결제 장애 복구 계획과 운영자 승인을 확인하는 모의 워크스페이스",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return <html lang="ko"><body>{children}</body></html>;
}
