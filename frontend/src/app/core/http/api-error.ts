import { HttpErrorResponse } from '@angular/common/http';

/** Normalized API error (CT-57), built from the CT-3 envelope `{"error": {code, message, details}}`. */
export interface ApiError {
  code: string;
  message: string;
  details?: Record<string, unknown>;
}

export const NETWORK_ERROR = 'NETWORK_ERROR';
export const HTTP_ERROR = 'HTTP_ERROR';
export const UNKNOWN_ERROR = 'UNKNOWN_ERROR';

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isApiError(value: unknown): value is ApiError {
  return (
    isRecord(value) &&
    typeof value['code'] === 'string' &&
    typeof value['message'] === 'string' &&
    (value['details'] === undefined || isRecord(value['details']))
  );
}

function parseBody(body: unknown): unknown {
  if (typeof body !== 'string') {
    return body;
  }
  try {
    return JSON.parse(body) as unknown;
  } catch {
    return undefined;
  }
}

function fromEnvelope(body: unknown): ApiError | null {
  if (!isRecord(body) || !isRecord(body['error'])) {
    return null;
  }
  const { code, message, details } = body['error'];
  if (typeof code !== 'string') {
    return null;
  }
  const apiError: ApiError = { code, message: typeof message === 'string' ? message : '' };
  if (isRecord(details)) {
    apiError.details = details;
  }
  return apiError;
}

function fromHttpError(err: HttpErrorResponse): ApiError {
  if (err.status === 0) {
    return { code: NETWORK_ERROR, message: 'Unable to reach the server.' };
  }

  const body = parseBody(err.error);
  const envelope = fromEnvelope(body);
  if (envelope) {
    return envelope;
  }

  // Starlette HTTPException (unknown route, method not allowed, invalid UTF-8) answers
  // `{"detail": ...}` outside the CT-3 envelope.
  if (isRecord(body) && 'detail' in body) {
    const detail = body['detail'];
    return {
      code: err.status === 404 ? 'RESOURCE_NOT_FOUND' : HTTP_ERROR,
      message: typeof detail === 'string' ? detail : err.statusText || err.message,
      details: { status: err.status, detail },
    };
  }

  return {
    code: err.status === 404 ? 'RESOURCE_NOT_FOUND' : HTTP_ERROR,
    message: err.statusText || err.message,
    details: { status: err.status },
  };
}

/** Converts any thrown value (usually an `HttpErrorResponse`) into an `ApiError`. */
export function toApiError(err: unknown): ApiError {
  if (err instanceof HttpErrorResponse) {
    return fromHttpError(err);
  }
  if (isApiError(err)) {
    return err;
  }
  if (err instanceof Error) {
    return { code: UNKNOWN_ERROR, message: err.message };
  }
  return { code: UNKNOWN_ERROR, message: 'Unexpected error.' };
}
