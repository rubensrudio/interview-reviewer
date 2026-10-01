import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable, catchError, throwError } from 'rxjs';

import { toApiError } from '../../core/http/api-error';

/** Frozen copy of a cited knowledge source (plan 7.6, KNOW-08). */
export interface SourceRef {
  url: string;
  title: string;
  collected_at: string;
  excerpt: string;
}

export interface ReferenceAnswer {
  text: string;
  points: string[];
  sources: SourceRef[];
  hypothetical_example: boolean;
  example_text: string | null;
}

/** Per-skill performance. `average` is a decimal string as sent by the backend. */
export interface SkillPerformance {
  skill: string;
  average: string;
  question_positions: number[];
}

export interface ReportItem {
  position: number;
  skill: string;
  question: string;
  answer: string;
  score: number;
  justification: string;
  evidence_quotes: string[];
  satisfactory: boolean;
  gap_explanation: string | null;
  reference_answer: ReferenceAnswer | null;
  no_verified_source: boolean;
}

export interface NonEvaluated {
  nice_to_have: string[];
  non_technical: string[];
}

export interface ReportPlan {
  planned_count: number;
  skills: string[];
}

/**
 * Frozen report content (plan 7.6), mirrored as received. `adherence_percentage` is a string
 * with one decimal place (e.g. "62.5").
 */
export interface ReportContent {
  summary: string;
  adherence_percentage: string;
  disclaimer: string;
  skills: SkillPerformance[];
  items: ReportItem[];
  unsatisfactory_items: number[];
  non_evaluated: NonEvaluated;
  plan: ReportPlan;
  model_version: string;
  rubric_version: string;
  sources_used: SourceRef[];
  completed_at: string;
}

/** One side of a comparison (plan 8.1). Numeric values are decimal strings. */
export interface ComparedSession {
  session_id: string;
  percentage: string;
  rubric_version: string;
  model_version: string;
}

export interface CommonSkill {
  skill: string;
  a_average: string;
  b_average: string;
}

/** Body of `GET /api/reports/compare` (plan 8.1). */
export interface Comparison {
  a: ComparedSession;
  b: ComparedSession;
  common_skills: CommonSkill[];
  comparable: boolean;
  differences: string[];
}

function reportUrl(sessionId: string, suffix: string): string {
  return `/api/sessions/${encodeURIComponent(sessionId)}${suffix}`;
}

/** Rethrows any HTTP failure as a normalized `ApiError` (CT-57). */
function asApiError<T>(source: Observable<T>): Observable<T> {
  return source.pipe(catchError((err: unknown) => throwError(() => toApiError(err))));
}

/**
 * Client for the Reports API (CT-64, plan 8.1). Content is returned exactly as received; errors
 * surface as `ApiError` (e.g. `REPORT_NOT_AVAILABLE`).
 */
@Injectable({ providedIn: 'root' })
export class ReportApi {
  private readonly http = inject(HttpClient);

  get(sessionId: string): Observable<ReportContent> {
    return this.http.get<ReportContent>(reportUrl(sessionId, '/report')).pipe(asApiError);
  }

  /** URL of the PDF export, for a link or download; it does not issue a request. */
  pdfUrl(sessionId: string): string {
    return reportUrl(sessionId, '/report.pdf');
  }

  compare(a: string, b: string): Observable<Comparison> {
    const params = new HttpParams().set('a', a).set('b', b);
    return this.http.get<Comparison>('/api/reports/compare', { params }).pipe(asApiError);
  }
}
