"use client";

import dynamic from "next/dynamic";

// react-markdown + remark-gfm 体积较大，且只在渲染助手消息时才需要，按需加载。
const MarkdownRenderer = dynamic(() => import("@/components/markdown-renderer"), {
  ssr: false,
  loading: () => <span className="text-xs text-faint">渲染中…</span>,
});

export function MarkdownMessage({ content }: { content: string }) {
  return (
    <div className="markdown-body">
      <MarkdownRenderer content={content} />
    </div>
  );
}
