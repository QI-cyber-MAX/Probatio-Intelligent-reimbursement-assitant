import { ApiError, errorMessage } from "../api/http";
import { Icon } from "./Icon";
export function ErrorNotice({
  error,
  retry,
}: {
  error: unknown;
  retry?: () => void;
}) {
  return (
    <div className="notice danger" role="alert">
      <Icon name="warning" />
      <div>
        <p>{errorMessage(error)}</p>
        {error instanceof ApiError && error.business && (
          <p className="technical">
            {error.business.code} · {error.business.stage}
            {error.business.detail_ref ? ` · ${error.business.detail_ref}` : ""}
          </p>
        )}
        {retry && (
          <button type="button" className="text-button" onClick={retry}>
            重新加载
          </button>
        )}
      </div>
    </div>
  );
}
