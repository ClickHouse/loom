/**
 * Reading an uploaded document in the browser: text out of a PDF (pdf.js) or
 * a .docx (mammoth), both loaded on first use; the original bytes kept as
 * base64 so the server can hand the file back as it was uploaded. Shared by
 * the Library's add dialog and a repository's.
 */

export async function extractPdfText(file: File): Promise<string> {
  const pdfjs = await import("pdfjs-dist");
  const workerUrl = (await import("pdfjs-dist/build/pdf.worker.min.mjs?url")).default;
  pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;
  const buf = await file.arrayBuffer();
  const doc = await pdfjs.getDocument({ data: new Uint8Array(buf) }).promise;
  const parts: string[] = [];
  for (let i = 1; i <= doc.numPages; i++) {
    const page = await doc.getPage(i);
    const content = await page.getTextContent();
    const pageText = content.items.map((it) => ("str" in it ? it.str : "")).join(" ");
    parts.push(pageText);
  }
  return parts.join("\n\n").trim();
}

export async function extractDocxText(file: File): Promise<string> {
  const mammoth = await import("mammoth/mammoth.browser");
  const buf = await file.arrayBuffer();
  const result = await mammoth.extractRawText({ arrayBuffer: buf });
  return (result.value || "").trim();
}

export function arrayBufferToBase64(buf: ArrayBuffer): string {
  // Chunk to avoid call-stack limits on large files (apply with a
  // multi-MB Uint8Array blows the apply args). 32 KB per chunk is a
  // common safe upper bound.
  const bytes = new Uint8Array(buf);
  const chunkSize = 32 * 1024;
  let binary = "";
  for (let i = 0; i < bytes.length; i += chunkSize) {
    const slice = bytes.subarray(i, i + chunkSize);
    binary += String.fromCharCode.apply(null, Array.from(slice));
  }
  return btoa(binary);
}

export function guessContentType(ext: string): string {
  switch (ext) {
    case "pdf":
      return "application/pdf";
    case "docx":
      return "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
    case "md":
      return "text/markdown";
    case "txt":
      return "text/plain";
    case "json":
      return "application/json";
    case "csv":
      return "text/csv";
    case "html":
      return "text/html";
    case "xml":
      return "application/xml";
    case "yaml":
    case "yml":
      return "application/yaml";
    default:
      return "application/octet-stream";
  }
}

const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;
const TEXT_EXT = ["md", "txt", "json", "yaml", "yml", "csv", "html", "xml", "markdown", "log", "rst", "adoc"];

export interface ReadDocument {
  text: string;
  blob: { b64: string; name: string; type: string; size: number };
}

/** A picked or dropped file as text plus its original bytes; throws an
 *  Error whose message a person can act on. */
export async function readDocumentFile(file: File): Promise<ReadDocument> {
  if (file.size > MAX_UPLOAD_BYTES) {
    throw new Error(
      `${file.name} is ${(file.size / 1024 / 1024).toFixed(1)} MB; uploads cap at 25 MB — split it or paste the text.`,
    );
  }
  const ext = file.name.toLowerCase().split(".").pop() || "";
  let text: string;
  if (ext === "pdf" || file.type === "application/pdf") text = await extractPdfText(file);
  else if (ext === "docx" || file.type === "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    text = await extractDocxText(file);
  else if (file.type.startsWith("text/") || TEXT_EXT.includes(ext)) text = await file.text();
  else throw new Error(`${file.name}: unsupported type. Use PDF, Word (.docx), Markdown or text — or paste the text.`);
  if (!text.trim())
    throw new Error(`No text could be read from ${file.name}. A scanned PDF needs OCR — paste the text instead.`);
  const buf = await file.arrayBuffer();
  return {
    text,
    blob: {
      b64: arrayBufferToBase64(buf),
      name: file.name,
      type: file.type || guessContentType(ext),
      size: buf.byteLength,
    },
  };
}

/** A URI-safe slug of a title: lower-case words joined by `-`, at most 60 characters. */
export function slugify(title: string): string {
  return (
    title
      .toLowerCase()
      .normalize("NFKD")
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 60) || "document"
  );
}
