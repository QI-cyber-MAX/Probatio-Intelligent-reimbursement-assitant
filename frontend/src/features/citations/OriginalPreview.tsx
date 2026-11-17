import { useEffect, useRef, useState } from "react";
import type { SourceLocator } from "../../api/contracts";
import { apiPath, responseError } from "../../api/http";
import { ErrorNotice } from "../../ui/ErrorNotice";
import { Icon } from "../../ui/Icon";

function responseFilename(header: string | null): string {
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(header ?? "");
  if (encoded) {
    try {
      return decodeURIComponent(encoded[1]!);
    } catch {
      /* Try the plain filename. */
    }
  }
  return /filename="([^"]+)"/i.exec(header ?? "")?.[1] ?? "制度原件";
}
export function OriginalPreview({ locator }: { locator: SourceLocator }) {
  const [file, setFile] = useState<{
    url: string;
    filename: string;
    pdf: boolean;
  } | null>(null);
  const [loading, setLoading] = useState(false);
  const [downloaded, setDownloaded] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const resource = useRef<string | null>(null);
  const request = useRef<AbortController | null>(null);
  useEffect(
    () => () => {
      request.current?.abort();
      if (resource.current) URL.revokeObjectURL(resource.current);
    },
    [],
  );
  async function read(download = false) {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError(null);
    setDownloaded(false);
    try {
      const path = `/documents/${encodeURIComponent(locator.document_id)}/sources/${encodeURIComponent(locator.source_hash)}/file${download ? "?download=true" : ""}`;
      const response = await fetch(apiPath(path), {
        signal: controller.signal,
      });
      if (!response.ok) throw await responseError(response);
      const blob = await response.blob();
      if (controller.signal.aborted) return;
      const pdf =
        response.headers
          .get("Content-Type")
          ?.split(";")[0]
          ?.trim()
          .toLowerCase() === "application/pdf";
      const filename = responseFilename(
        response.headers.get("Content-Disposition"),
      );
      if (resource.current) URL.revokeObjectURL(resource.current);
      const url = URL.createObjectURL(blob);
      resource.current = url;
      setFile({ url, filename, pdf });
      if (download || !pdf) {
        const link = document.createElement("a");
        link.href = url;
        link.download = filename;
        document.body.append(link);
        link.click();
        link.remove();
        setDownloaded(true);
      }
    } catch (failure) {
      if (!controller.signal.aborted) setError(failure);
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }
  const target = file
    ? `${file.url}${locator.page_start !== null ? `#page=${locator.page_start}` : ""}`
    : "";
  return (
    <div className="original-preview">
      <div className="file-actions">
        <button
          className="button secondary compact"
          disabled={loading}
          onClick={() => {
            void read();
          }}
        >
          <Icon name="document" />
          {loading ? "读取原件中…" : "打开原件"}
        </button>
        <button
          className="button secondary compact"
          disabled={loading}
          onClick={() => {
            void read(true);
          }}
        >
          下载原件
        </button>
      </div>
      {error !== null && (
        <ErrorNotice
          error={error}
          retry={() => {
            void read();
          }}
        />
      )}
      {downloaded && (
        <p className="help" role="status">
          已开始下载：{file?.filename}
        </p>
      )}
      {file?.pdf && (
        <div className="pdf-preview">
          <p className="help">
            PDF 原件
            {locator.page_start !== null &&
              ` · 跳转目标：第 ${locator.page_start} 页`}
          </p>
          <iframe title="PDF 原件预览" src={target} />
          <a href={target} target="_blank" rel="noopener noreferrer">
            在新窗口打开 PDF
          </a>
          <p className="help">无法内嵌浏览时，可在新窗口打开或下载原件。</p>
        </div>
      )}
    </div>
  );
}
