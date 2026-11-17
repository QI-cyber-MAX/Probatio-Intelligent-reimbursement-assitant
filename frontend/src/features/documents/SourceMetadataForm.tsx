import { useEffect, useState, type FormEvent } from "react";
import type {
  IngestionAccepted,
  IngestionJobView,
  PolicyMetadata,
  SourceMetadataUpdate,
  VersionStatus,
} from "../../api/contracts";
import { getIngestionJob, updateSourceMetadata } from "../../api/documents";
import { ApiError, apiPath } from "../../api/http";
import {
  readPendingMetadata,
  savePendingMetadata,
} from "../../storage/pending-submissions";
import { Dialog } from "../../ui/Dialog";
import { ErrorNotice } from "../../ui/ErrorNotice";
import { Field } from "../../ui/Field";
import { createUuid } from "../../uuid";

export function SourceMetadataForm({
  documentId,
  sourceHash,
  policy,
  onClose,
  onAccepted,
  onJob,
  reload,
  storageUnavailable,
}: {
  documentId: string;
  sourceHash: string;
  policy: PolicyMetadata;
  onClose(): void;
  onAccepted(value: IngestionAccepted): void;
  onJob(value: IngestionJobView): void;
  reload(): void;
  storageUnavailable(): void;
}) {
  const start = policy.effective_from;
  const [restored] = useState(() => {
    let unavailable = false;
    const value = readPendingMetadata(documentId, sourceHash, () => {
      unavailable = true;
    });
    return { value, unavailable };
  });
  const [pending, setPending] = useState(restored.value);
  useEffect(() => {
    if (restored.unavailable) storageUnavailable();
  }, [restored.unavailable, storageUnavailable]);
  const initialUpdate = restored.value;
  const [version, setVersion] = useState<VersionStatus>(initialUpdate?.version_status ?? policy.version_status);
  const [versionChanged, setVersionChanged] = useState(initialUpdate?.version_status !== undefined);
  const [from, setFrom] = useState(
    initialUpdate && "effective_from" in initialUpdate
      ? initialUpdate.effective_from ?? ""
      : start?.value.length === 10 ? start.value : "",
  );
  const [to, setTo] = useState(
    initialUpdate && "effective_to" in initialUpdate
      ? initialUpdate.effective_to ?? ""
      : policy.effective_to?.value ?? "",
  );
  const [fromChanged, setFromChanged] = useState(!!initialUpdate && "effective_from" in initialUpdate);
  const [toChanged, setToChanged] = useState(!!initialUpdate && "effective_to" in initialUpdate);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  function remember(value: SourceMetadataUpdate | null) {
    setPending(value);
    if (!savePendingMetadata(documentId, sourceHash, value)) storageUnavailable();
  }
  async function check() {
    if (!pending || busy) return;
    setBusy(true);
    setError(null);
    try {
      const job = await getIngestionJob(pending.new_job_id);
      remember(null);
      onJob(job);
      reload();
      onClose();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    const update = pending ?? {
      new_job_id: createUuid(),
      ...(versionChanged ? { version_status: version } : {}),
      ...(fromChanged ? { effective_from: from || null } : {}),
      ...(toChanged ? { effective_to: to || null } : {}),
    };
    if (Object.keys(update).length === 1) {
      setError(new ApiError("请修改版本或日期后提交。", 400));
      return;
    }
    if (!pending && from && to && from >= to) {
      setError(new ApiError("截止日期须晚于生效日期。", 400));
      return;
    }
    remember(update);
    setBusy(true);
    setError(null);
    try {
      const accepted = await updateSourceMetadata(documentId, sourceHash, update);
      remember(null);
      onAccepted(accepted);
      onClose();
    } catch (failure) {
      setError(failure);
      if (!(failure instanceof ApiError && failure.outcomeUnknown)) remember(null);
      if (failure instanceof ApiError && failure.business?.code === "INGESTION_SOURCE_BUSY")
        reload();
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog title="修改原件元数据" busy={busy} onClose={onClose}>
      <form onSubmit={submit} aria-busy={busy}>
        <p className="muted form-intro">修改成功后更新原件的版本与适用日期。</p>
        <fieldset disabled={busy || !!pending} className="form-fields">
          <Field id="metadata-version" label="原件版本">
            <select
              id="metadata-version"
              value={version}
              onChange={(event) => {
                setVersion(event.target.value as VersionStatus);
                setVersionChanged(true);
              }}
            >
              <option value="CURRENT">新版本</option>
              <option value="HISTORICAL">历史版本</option>
            </select>
          </Field>
          <button
            type="button"
            className="text-button"
            onClick={() => {
              setVersion("CURRENT");
              setVersionChanged(true);
            }}
          >
            将此原件设为新版本
          </button>
          <p className="help">
            当前生效日期：{start
              ? `${start.value}（${start.source === "USER" ? "用户登记" : "原文提取"}）`
              : "未知"}
          </p>
          {start?.locators.map((loc, i) => (
            <p key={i} className="help">
              <a
                href={`${apiPath(`/documents/${documentId}/sources/${sourceHash}/file`)}${loc.page_start ? `#page=${loc.page_start}` : ""}`}
                target="_blank"
                rel="noreferrer"
              >
                查看日期依据
                {loc.page_start ? ` · 第 ${loc.page_start} 页` : ""}
                {loc.section_path.length ? ` · ${loc.section_path.join(" / ")}` : ""}
                {loc.cell_range ? ` · ${loc.cell_range}` : ""}
              </a>
            </p>
          ))}
          <Field
            id="metadata-from"
            label="生效日期（选填）"
            hint="保持原值可直接修改其他字段；清空后按原文重新提取"
          >
            <input
              id="metadata-from"
              type="date"
              value={from}
              aria-describedby="metadata-from-hint"
              onChange={(event) => {
                setFrom(event.target.value);
                setFromChanged(true);
              }}
            />
          </Field>
          <button
            type="button"
            className="text-button"
            onClick={() => {
              setFrom("");
              setFromChanged(true);
            }}
          >
            按原文重新提取生效日期
          </button>
          <Field
            id="metadata-to"
            label="截止日期（选填）"
            hint="从该日起不再适用；清空可取消截止日期"
          >
            <input
              id="metadata-to"
              type="date"
              value={to}
              aria-describedby="metadata-to-hint"
              onChange={(event) => {
                setTo(event.target.value);
                setToChanged(true);
              }}
            />
          </Field>
        </fieldset>
        {error !== null && <ErrorNotice error={error} />}
        {pending && (
          <div className="notice warning">
            <p>已保留本次修改，查询结果或重传将沿用同一任务。</p>
            <button
              type="button"
              className="text-button"
              disabled={busy}
              onClick={() => void check()}
            >
              查询修改结果
            </button>
          </div>
        )}
        <footer className="form-actions">
          <button type="button" className="button secondary" disabled={busy} onClick={onClose}>
            关闭
          </button>
          <button type="submit" className="button primary" disabled={busy}>
            {busy ? "正在提交…" : pending ? "重传本次修改" : "保存修改"}
          </button>
        </footer>
      </form>
    </Dialog>
  );
}
