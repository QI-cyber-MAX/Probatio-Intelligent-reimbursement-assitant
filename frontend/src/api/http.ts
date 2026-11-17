import { decodeBusinessError, type BusinessError } from "./contracts";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly business: BusinessError | null = null,
    readonly fields: Record<string, string> = {},
    readonly outcomeUnknown = false,
  ) {
    super(message);
  }
}
export function apiPath(path: string): string {
  if (!path.startsWith("/") || path.startsWith("//"))
    throw new Error("API 必须使用站内绝对路径");
  return `/api${path}`;
}
export async function responseError(
  response: Response,
  write = false,
): Promise<ApiError> {
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    return new ApiError(
      "服务暂时无法响应，请稍后重新读取。",
      response.status,
      null,
      {},
      write && response.status >= 500,
    );
  }
  try {
    const business = decodeBusinessError(payload);
    return new ApiError(
      business.user_message,
      response.status,
      business,
      {},
      write && /OUTCOME_UNKNOWN$/.test(business.code),
    );
  } catch {
    const fields: Record<string, string> = {};
    if (
      response.status === 422 &&
      payload &&
      typeof payload === "object" &&
      "detail" in payload &&
      Array.isArray(payload.detail)
    ) {
      for (const error of payload.detail) {
        if (error && Array.isArray(error.loc) && typeof error.msg === "string")
          fields[
            error.loc
              .slice(
                ["body", "query", "path", "header", "cookie"].includes(
                  error.loc[0],
                )
                  ? 1
                  : 0,
              )
              .map(String)
              .join(".")
          ] = error.msg;
      }
    }
    return new ApiError(
      response.status === 422 ? "请核对输入字段。" : "请求失败，请稍后重试。",
      response.status,
      null,
      fields,
      write && response.status >= 500,
    );
  }
}
export async function request<T>(
  path: string,
  decode: (value: unknown, response: Response) => T,
  options: RequestInit = {},
): Promise<T> {
  const write = options.method === "POST" || options.method === "PATCH";
  let response: Response;
  try {
    response = await fetch(apiPath(path), options);
  } catch (error) {
    if (options.signal?.aborted) throw error;
    throw new ApiError(
      write
        ? "请求结果未确认，请先核对服务端状态。"
        : "读取失败，请检查后端连接后重新加载。",
      0,
      null,
      {},
      write,
    );
  }
  if (!response.ok) throw await responseError(response, write);
  try {
    return decode(await response.json(), response);
  } catch {
    throw new ApiError(
      "接口响应无法读取，请核对服务端状态。",
      response.status,
      null,
      {},
      write,
    );
  }
}
export const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : "请求失败，请重新读取。";
