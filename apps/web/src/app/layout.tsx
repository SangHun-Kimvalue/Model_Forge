import type { Metadata } from "next";
import type { ReactNode } from "react";

import "./globals.css";

export const metadata: Metadata = {
  title: "Model Forge 작업대",
  description:
    "대화, 계획, 승인, 미리보기, 이벤트 로그를 한 화면에서 다루는 Model Forge 작업대.",
};

export default function RootLayout({
  children,
}: {
  children: ReactNode;
}): JSX.Element {
  return (
    <html lang="ko">
      <body>{children}</body>
    </html>
  );
}
