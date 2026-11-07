"use client";

import { yaml as yamlLanguage } from "@codemirror/lang-yaml";
import { linter, lintGutter, type Diagnostic } from "@codemirror/lint";
import CodeMirror from "@uiw/react-codemirror";
import { useMemo } from "react";
import { parseAllDocuments } from "yaml";

/** Syntax errors from the yaml parser as editor diagnostics. */
export function yamlProblems(text: string): Diagnostic[] {
  const diagnostics: Diagnostic[] = [];
  for (const doc of parseAllDocuments(text)) {
    for (const error of [...doc.errors, ...doc.warnings]) {
      const [from, to] = error.pos;
      diagnostics.push({
        from,
        to: Math.max(to, from + 1),
        severity: doc.errors.includes(error) ? "error" : "warning",
        message: error.message.split("\n")[0],
      });
    }
  }
  return diagnostics;
}

const yamlLinter = linter((view) => {
  const length = view.state.doc.length;
  return yamlProblems(view.state.doc.toString()).map((d) => ({ ...d, from: Math.min(d.from, length), to: Math.min(d.to, length) }));
});

export function YamlEditor({
  value,
  onChange,
  height = "480px",
  readOnly = false,
}: {
  value: string;
  onChange?: (value: string) => void;
  height?: string;
  readOnly?: boolean;
}) {
  const extensions = useMemo(() => [yamlLanguage(), lintGutter(), yamlLinter], []);
  return (
    <div className="overflow-hidden rounded-md border border-border text-[13px]">
      <CodeMirror
        value={value}
        height={height}
        theme="dark"
        readOnly={readOnly}
        extensions={extensions}
        onChange={onChange}
        basicSetup={{ lineNumbers: true, foldGutter: true, highlightActiveLine: !readOnly, autocompletion: false }}
      />
    </div>
  );
}
