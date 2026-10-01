import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable, catchError, map, throwError } from 'rxjs';

import { toApiError } from '../../core/http/api-error';

/** Processing state of a resume version (plan 7.3). */
export type ResumeStatus = 'received' | 'processing' | 'ready' | 'failed';

export type ExtractionKind = 'experience' | 'education' | 'skill';

export type ExtractionOrigin = 'explicit' | 'inferred' | 'user_provided';

/**
 * One extracted resume item (plan 7.3). `fields` keys depend on `kind`: title, organization,
 * start, end, degree, institution, name, description. `evidence` is empty only for
 * `user_provided` items.
 */
export interface ExtractionItem {
  id: string;
  kind: ExtractionKind;
  fields: Record<string, string>;
  origin: ExtractionOrigin;
  evidence: string[];
}

/** Resume version as listed by `GET /api/resumes` (plan 8.1). */
export interface ResumeSummary {
  id: string;
  filename: string;
  /** ISO 8601 timestamp. */
  uploaded_at: string;
  status: ResumeStatus;
  failure_code: string | null;
  failure_message: string | null;
}

/** `GET /api/resumes/{id}`: `items` is `null` while the version is not `ready`. */
export interface ResumeDetail extends ResumeSummary {
  items: ExtractionItem[] | null;
}

/** Body of `POST /api/resumes/{id}/items`. */
export interface NewExtractionItem {
  kind: ExtractionKind;
  fields: Record<string, string>;
}

/** Body of `PATCH /api/resumes/{id}/items/{item_id}`; an omitted `kind` keeps the current one. */
export interface ExtractionItemUpdate {
  kind?: ExtractionKind;
  fields: Record<string, string>;
}

const RESUMES = '/api/resumes';

/** Rethrows any HTTP failure as a normalized `ApiError` (CT-57). */
function asApiError<T>(source: Observable<T>): Observable<T> {
  return source.pipe(catchError((err: unknown) => throwError(() => toApiError(err))));
}

function toVoid(source: Observable<unknown>): Observable<void> {
  return source.pipe(
    asApiError,
    map(() => undefined),
  );
}

function resumeUrl(id: string): string {
  return `${RESUMES}/${encodeURIComponent(id)}`;
}

function itemUrl(resumeId: string, itemId: string): string {
  return `${resumeUrl(resumeId)}/items/${encodeURIComponent(itemId)}`;
}

/**
 * Client for the Resumes API (CT-60). Every failure is emitted as an `ApiError` (CT-57).
 * Polling of processing versions belongs to the pages, not to this client.
 */
@Injectable({ providedIn: 'root' })
export class ResumeApi {
  private readonly http = inject(HttpClient);

  /** Own resume versions, most recent first, optionally filtered by status (CV-13). */
  list(status?: ResumeStatus): Observable<ResumeSummary[]> {
    const params = status ? new HttpParams().set('status', status) : undefined;
    return this.http.get<ResumeSummary[]>(RESUMES, { params }).pipe(asApiError);
  }

  get(id: string): Observable<ResumeDetail> {
    return this.http.get<ResumeDetail>(resumeUrl(id)).pipe(asApiError);
  }

  /** Uploads a PDF as multipart field `file` (CV-01). The browser sets the multipart boundary. */
  upload(file: File): Observable<ResumeSummary> {
    const body = new FormData();
    body.append('file', file, file.name);
    return this.http.post<ResumeSummary>(RESUMES, body).pipe(asApiError);
  }

  addItem(resumeId: string, item: NewExtractionItem): Observable<ExtractionItem> {
    return this.http.post<ExtractionItem>(`${resumeUrl(resumeId)}/items`, item).pipe(asApiError);
  }

  updateItem(
    resumeId: string,
    itemId: string,
    update: ExtractionItemUpdate,
  ): Observable<ExtractionItem> {
    return this.http.patch<ExtractionItem>(itemUrl(resumeId, itemId), update).pipe(asApiError);
  }

  removeItem(resumeId: string, itemId: string): Observable<void> {
    return toVoid(this.http.delete<null>(itemUrl(resumeId, itemId)));
  }

  delete(id: string): Observable<void> {
    return toVoid(this.http.delete<null>(resumeUrl(id)));
  }
}
