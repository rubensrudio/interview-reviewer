import { HttpClient, HttpHeaders } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable, catchError, map, throwError } from 'rxjs';

import { toApiError } from '../../core/http/api-error';

/** Lifecycle state of an interview session (plan 7.5). */
export type SessionStatus =
  | 'collecting_requirements'
  | 'awaiting_confirmation'
  | 'preparing_questions'
  | 'preparation_failed'
  | 'in_interview'
  | 'evaluating'
  | 'evaluation_failed'
  | 'completed'
  | 'cancelled'
  | 'expired';

export type ExpectedLevel = 'junior' | 'mid-level' | 'senior' | 'expert';

export type RequirementClassification = 'required' | 'nice_to_have';

export type MessageRole = 'candidate' | 'assistant';

export type MessageKind =
  'requirements' | 'requirements_reply' | 'clarification_request' | 'clarification_reply' | 'info';

/** One job requirement interpreted from the pasted text (plan 7.5). */
export interface RequirementItem {
  id: string;
  name: string;
  original_terms: string[];
  classification: RequirementClassification;
  level: ExpectedLevel | null;
  pending_clarification: boolean;
  clarification_question: string | null;
}

/**
 * Requirement as sent when editing the list (`RequirementItemInput`). `id` is `null` for a new
 * item; extra fields of a received `RequirementItem` are ignored by the backend.
 */
export interface RequirementItemInput {
  id: string | null;
  name: string;
  original_terms: string[];
  classification: RequirementClassification;
  level: ExpectedLevel | null;
}

export interface SessionMessage {
  id: string;
  role: MessageRole;
  kind: MessageKind;
  content: string;
  created_at: string;
}

export interface SessionRequirements {
  items: RequirementItem[];
  non_technical: string[];
}

export interface PlanProposal {
  planned_count: number;
  skills: string[];
}

export interface QuestionCounter {
  planned: number;
  answered: number;
  remaining: number;
}

export interface CurrentQuestion {
  id: string;
  position: number;
  skill: string;
  text: string;
}

export interface AnsweredQuestion {
  question_id: string;
  position: number;
  skill: string;
  question: string;
  answer: string;
}

/**
 * Safe projection of a session (CT-40), returned by every session route. It never carries
 * reference points, expected answers or sources (INTV-14).
 */
export interface SessionView {
  id: string;
  status: SessionStatus;
  created_at: string;
  language: string;
  interview_level: ExpectedLevel | null;
  resume_name: string | null;
  messages: SessionMessage[];
  requirements: SessionRequirements | null;
  proposal: PlanProposal | null;
  counter: QuestionCounter | null;
  current_question: CurrentQuestion | null;
  answered: AnsweredQuestion[];
  report_available: boolean;
}

/** Row of `GET /api/sessions`. */
export interface SessionSummary {
  id: string;
  created_at: string;
  status: SessionStatus;
  resume_name: string | null;
  required_skills: string[];
  completed_at: string | null;
}

export interface LanguageOption {
  code: string;
  label: string;
}

/** Body of `GET /api/interview-options`. */
export interface InterviewOptions {
  languages: LanguageOption[];
  levels: ExpectedLevel[];
}

interface ClarificationResponse {
  message: SessionMessage;
}

const SESSIONS = '/api/sessions';

/** Rethrows any HTTP failure as a normalized `ApiError` (CT-57). */
function asApiError<T>(source: Observable<T>): Observable<T> {
  return source.pipe(catchError((err: unknown) => throwError(() => toApiError(err))));
}

function sessionUrl(id: string, suffix = ''): string {
  return `${SESSIONS}/${encodeURIComponent(id)}${suffix}`;
}

/** Client for the Sessions and Interview API (CT-61, plan 8.1). Errors surface as `ApiError`. */
@Injectable({ providedIn: 'root' })
export class SessionApi {
  private readonly http = inject(HttpClient);

  options(): Observable<InterviewOptions> {
    return this.http.get<InterviewOptions>('/api/interview-options').pipe(asApiError);
  }

  start(
    resumeId: string,
    language: string,
    interviewLevel: ExpectedLevel | null,
  ): Observable<SessionView> {
    return this.http
      .post<SessionView>(SESSIONS, {
        resume_id: resumeId,
        language,
        interview_level: interviewLevel,
      })
      .pipe(asApiError);
  }

  list(): Observable<SessionSummary[]> {
    return this.http.get<SessionSummary[]>(SESSIONS).pipe(asApiError);
  }

  get(id: string): Observable<SessionView> {
    return this.http.get<SessionView>(sessionUrl(id)).pipe(asApiError);
  }

  cancel(id: string): Observable<SessionView> {
    return this.postView(sessionUrl(id, '/cancel'));
  }

  delete(id: string): Observable<void> {
    return this.http.delete<null>(sessionUrl(id)).pipe(
      asApiError,
      map(() => undefined),
    );
  }

  sendRequirements(id: string, text: string): Observable<SessionView> {
    return this.postView(sessionUrl(id, '/requirements'), { text });
  }

  replaceRequirementList(
    id: string,
    items: readonly (RequirementItem | RequirementItemInput)[],
  ): Observable<SessionView> {
    return this.http
      .put<SessionView>(sessionUrl(id, '/requirement-list'), { items })
      .pipe(asApiError);
  }

  confirmList(id: string): Observable<SessionView> {
    return this.postView(sessionUrl(id, '/requirement-list/confirm'));
  }

  confirmPlan(id: string): Observable<SessionView> {
    return this.postView(sessionUrl(id, '/plan/confirm'));
  }

  retryPreparation(id: string): Observable<SessionView> {
    return this.postView(sessionUrl(id, '/preparation/retry'));
  }

  /**
   * Submits the answer to the current question. The caller owns `idempotencyKey` and must
   * reuse it when retrying the same submission.
   */
  answer(
    id: string,
    questionId: string,
    content: string,
    idempotencyKey: string,
  ): Observable<SessionView> {
    const headers = new HttpHeaders({ 'Idempotency-Key': idempotencyKey });
    return this.http
      .post<SessionView>(
        sessionUrl(id, '/answers'),
        { question_id: questionId, content },
        { headers },
      )
      .pipe(asApiError);
  }

  clarify(id: string, text: string): Observable<SessionMessage> {
    return this.http.post<ClarificationResponse>(sessionUrl(id, '/clarifications'), { text }).pipe(
      asApiError,
      map(({ message }) => message),
    );
  }

  retryEvaluation(id: string): Observable<SessionView> {
    return this.postView(sessionUrl(id, '/evaluation/retry'));
  }

  private postView(url: string, body: object | null = null): Observable<SessionView> {
    return this.http.post<SessionView>(url, body).pipe(asApiError);
  }
}
