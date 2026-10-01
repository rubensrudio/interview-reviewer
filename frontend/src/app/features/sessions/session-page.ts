import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  OnInit,
  computed,
  inject,
  signal,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { NgTemplateOutlet } from '@angular/common';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { Observable, Subscription, timer } from 'rxjs';

import { ApiError, NETWORK_ERROR } from '../../core/http/api-error';
import { ConfirmDialog } from '../../shared/confirm-dialog';
import { InterviewPanel } from './interview-panel';
import { RequirementDraft, RequirementListEditor } from './requirement-list-editor';
import { MessageKind, SessionApi, SessionMessage, SessionStatus, SessionView } from './session-api';

/** Interval between reloads while the backend works on the session (same as the resume pages). */
const POLL_INTERVAL_MS = 3000;

const GENERIC_ERROR = 'Something went wrong. Please try again.';
const EMPTY_REQUIREMENTS_MESSAGE =
  'Please paste the job requirements for the position you are preparing for.';

/** Section 9 / catalog 8.3 texts for the errors of the session routes, keyed by `code`. */
const ERRORS: Readonly<Record<string, string>> = {
  VALIDATION_ERROR: 'Please check the highlighted fields.',
  CSRF_FAILED: 'Your session expired. Please reload the page.',
  RESOURCE_NOT_FOUND: 'Not found.',
  INVALID_STATE: 'This action is not available at this step.',
  SESSION_CLOSED: 'This interview is closed.',
  EMPTY_REQUIREMENTS: EMPTY_REQUIREMENTS_MESSAGE,
  NO_REQUIRED_SKILLS: 'Define at least one required technical skill to continue.',
  TOO_MANY_REQUIRED_SKILLS:
    'This job lists {count} required skills. The limit is 20 — review the list and remove or merge {excess} before continuing.',
  PENDING_CLARIFICATION: 'Some requirements need clarification before you continue.',
  LLM_UNAVAILABLE: 'The assistant is temporarily unavailable. Please try again.',
  [NETWORK_ERROR]: 'Unable to reach the server.',
};

/** Section 9 texts shown on the session screen. */
export const PREPARATION_FAILED_MESSAGE =
  "We couldn't prepare your questions. Your requirements are saved — try again.";
export const EVALUATING_MESSAGE =
  'Evaluating your answers. You can leave this page; the report will appear in your history.';
export const EVALUATION_FAILED_MESSAGE =
  "We couldn't finish evaluating your answers. Your answers are saved — try again.";
export const CANCEL_SESSION_TITLE = 'Cancel interview';
export const CANCEL_SESSION_MESSAGE =
  'Cancel this interview? It cannot be resumed and no report will be generated.';
const CLOSED_MESSAGE = 'This interview is closed.';

const STATUS_LABELS: Readonly<Record<SessionStatus, string>> = {
  collecting_requirements: 'Collecting requirements',
  awaiting_confirmation: 'Awaiting confirmation',
  preparing_questions: 'Preparing questions',
  preparation_failed: 'Preparation failed',
  in_interview: 'In interview',
  evaluating: 'Evaluating',
  evaluation_failed: 'Evaluation failed',
  completed: 'Completed',
  cancelled: 'Cancelled',
  expired: 'Expired',
};

/** States in which the backend is working and the page refreshes the view by itself. */
const POLLED_STATUSES: ReadonlySet<SessionStatus> = new Set(['preparing_questions', 'evaluating']);

/** Terminal states (plan 7.5): read only, no actions offered. */
const TERMINAL_STATUSES: ReadonlySet<SessionStatus> = new Set([
  'completed',
  'cancelled',
  'expired',
]);

const CLARIFICATION_KINDS: ReadonlySet<MessageKind> = new Set([
  'clarification_request',
  'clarification_reply',
]);

/** Catalog text for `err`, with `{placeholders}` filled from `details` (numbers only). */
function errorText(err: ApiError): string {
  const template = ERRORS[err.code];
  if (!template) {
    return GENERIC_ERROR;
  }
  const details = err.details ?? {};
  return template.replace(/\{(\w+)\}/g, (match, key: string) => {
    const value = details[key];
    return typeof value === 'number' ? String(value) : match;
  });
}

/**
 * Interview session page (PLAN-01, PLAN-10, PLAN-12, INTV-08, INTV-11..13, EVAL-13, EVAL-14).
 *
 * A container that renders the step matching the `status` returned by the backend: the
 * requirements chat, the requirement list editor, the preparation wait, the interview panel,
 * the evaluation wait, retries after failures, the completed state and the read-only terminal
 * states. Transitions are never decided here: every action applies the `SessionView` returned by
 * the API, and an `INVALID_STATE` answer reloads the session.
 */
@Component({
  selector: 'app-session-page',
  imports: [NgTemplateOutlet, RouterLink, ConfirmDialog, InterviewPanel, RequirementListEditor],
  templateUrl: './session-page.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
  styles: `
    :host {
      display: block;
      max-width: 48rem;
      margin: 0 auto;
      padding: 2rem 1rem;
      color: #1a1a1a;
    }
    h1 {
      margin: 0 0 0.5rem;
      font-size: 1.75rem;
    }
    h2 {
      margin: 0 0 0.75rem;
      font-size: 1.25rem;
    }
    p {
      margin: 0;
    }
    a {
      color: #1d4ed8;
    }
    .meta {
      display: flex;
      flex-wrap: wrap;
      gap: 0.5rem 1rem;
      margin-bottom: 1.5rem;
      color: #4b5563;
    }
    .meta span {
      overflow-wrap: anywhere;
    }
    .status {
      font-weight: 600;
    }
    section {
      margin-bottom: 1.5rem;
    }
    ol.chat {
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
      margin: 0 0 1rem;
      padding: 0;
      list-style: none;
    }
    ol.chat li {
      max-width: 90%;
      padding: 0.75rem 1rem;
      border-radius: 0.5rem;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    ol.chat li.assistant {
      align-self: flex-start;
      border: 1px solid #c4c4c4;
      background: #f9fafb;
    }
    ol.chat li.candidate {
      align-self: flex-end;
      border: 1px solid #1e3a8a;
      background: #eff6ff;
    }
    .author {
      display: block;
      margin-bottom: 0.25rem;
      font-size: 0.875rem;
      font-weight: 600;
      color: #374151;
    }
    form {
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
    }
    label {
      font-weight: 600;
    }
    textarea {
      min-height: 8rem;
      padding: 0.75rem;
      border: 1px solid #4b5563;
      border-radius: 0.375rem;
      font: inherit;
      resize: vertical;
    }
    textarea[aria-invalid='true'] {
      border-color: #b91c1c;
    }
    .notice {
      padding: 1rem;
      border: 1px solid #c4c4c4;
      border-radius: 0.5rem;
      background: #f9fafb;
    }
    .error {
      padding: 0.75rem 1rem;
      border: 1px solid #b91c1c;
      border-radius: 0.375rem;
      color: #7f1d1d;
      background: #fef2f2;
    }
    .stack {
      display: flex;
      flex-direction: column;
      align-items: flex-start;
      gap: 0.75rem;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 0.75rem;
      margin-top: 1rem;
    }
    ol.answered {
      margin: 0;
      padding: 0;
      list-style: none;
    }
    ol.answered li {
      margin-bottom: 0.75rem;
      padding: 0.75rem 1rem;
      border: 1px solid #e5e7eb;
      border-radius: 0.5rem;
    }
    ol.answered p {
      margin-top: 0.25rem;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    button {
      min-height: 2.75rem;
      padding: 0.5rem 1rem;
      border-radius: 0.375rem;
      font: inherit;
      cursor: pointer;
    }
    .primary {
      border: 1px solid #1d4ed8;
      color: #ffffff;
      background: #1d4ed8;
    }
    .secondary {
      border: 1px solid #4b5563;
      color: #1a1a1a;
      background: #ffffff;
    }
    .danger {
      border: 1px solid #b91c1c;
      color: #b91c1c;
      background: #ffffff;
    }
    button:disabled {
      cursor: not-allowed;
      opacity: 0.6;
    }
    button[aria-busy='true'] {
      cursor: progress;
    }
    button:focus-visible,
    textarea:focus-visible,
    a:focus-visible {
      outline: 3px solid #1d4ed8;
      outline-offset: 2px;
    }
    .cancel-area {
      margin-top: 2rem;
      padding-top: 1rem;
      border-top: 1px solid #e5e7eb;
    }
    @media (max-width: 40rem) {
      .actions button,
      .cancel-area button {
        width: 100%;
      }
    }
  `,
})
export class SessionPage implements OnInit {
  private readonly api = inject(SessionApi);
  private readonly destroyRef = inject(DestroyRef);
  private readonly sessionId = inject(ActivatedRoute).snapshot.paramMap.get('id') ?? '';

  protected readonly view = signal<SessionView | null>(null);
  protected readonly loading = signal(true);
  protected readonly loadError = signal<string | null>(null);
  /** Error of the last page-level action (retry, cancel). */
  protected readonly actionError = signal<string | null>(null);
  protected readonly acting = signal(false);
  protected readonly confirmingCancel = signal(false);

  protected readonly requirementsDraft = signal('');
  protected readonly requirementsError = signal<string | null>(null);
  protected readonly sending = signal(false);

  /**
   * Error given to the editor. Always a new object per failure: the editor re-enables its
   * confirmation buttons whenever any of its inputs changes (CT-62).
   */
  protected readonly editorError = signal<ApiError | null>(null);

  protected readonly statusLabels = STATUS_LABELS;
  protected readonly preparationFailedMessage = PREPARATION_FAILED_MESSAGE;
  protected readonly evaluatingMessage = EVALUATING_MESSAGE;
  protected readonly evaluationFailedMessage = EVALUATION_FAILED_MESSAGE;
  protected readonly cancelTitle = CANCEL_SESSION_TITLE;
  protected readonly cancelMessage = CANCEL_SESSION_MESSAGE;
  protected readonly closedMessage = CLOSED_MESSAGE;

  protected readonly status = computed(() => this.view()?.status ?? null);
  protected readonly isTerminal = computed(() => {
    const status = this.status();
    return status !== null && TERMINAL_STATUSES.has(status);
  });
  protected readonly requirementItems = computed(() => this.view()?.requirements?.items ?? []);
  protected readonly nonTechnical = computed(() => this.view()?.requirements?.non_technical ?? []);
  /** Requirements chat: every message except interview clarifications. */
  protected readonly chatMessages = computed(() =>
    (this.view()?.messages ?? []).filter((m) => !CLARIFICATION_KINDS.has(m.kind)),
  );
  /** Clarification exchange of the interview step (INTV-08). */
  protected readonly clarifications = computed(() =>
    (this.view()?.messages ?? []).filter((m) => CLARIFICATION_KINDS.has(m.kind)),
  );

  private loadSub: Subscription | null = null;
  private pollSub: Subscription | null = null;

  ngOnInit(): void {
    this.destroyRef.onDestroy(() => {
      this.loadSub?.unsubscribe();
      this.pollSub?.unsubscribe();
    });
    this.load();
  }

  protected reload(): void {
    this.loading.set(true);
    this.loadError.set(null);
    this.load();
  }

  protected authorLabel(message: SessionMessage): string {
    return message.role === 'candidate' ? 'You' : 'Assistant';
  }

  /** Applies a view emitted by a child or returned by the API. */
  protected applyView(view: SessionView): void {
    this.view.set(view);
    this.schedulePoll();
  }

  protected onRequirementsInput(event: Event): void {
    this.requirementsDraft.set((event.target as HTMLTextAreaElement).value);
    this.requirementsError.set(null);
  }

  protected sendRequirements(event: Event): void {
    event.preventDefault();
    if (this.sending()) {
      return;
    }
    const text = this.requirementsDraft();
    if (text.trim() === '') {
      this.requirementsError.set(EMPTY_REQUIREMENTS_MESSAGE);
      return;
    }
    this.sending.set(true);
    this.requirementsError.set(null);
    this.api
      .sendRequirements(this.sessionId, text)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (view) => {
          this.sending.set(false);
          this.requirementsDraft.set('');
          this.applyView(view);
        },
        error: (err: ApiError) => {
          this.sending.set(false);
          this.requirementsError.set(errorText(err));
          this.reloadIfInvalidState(err);
        },
      });
  }

  protected onListChanged(items: RequirementDraft[]): void {
    this.runEditorAction(this.api.replaceRequirementList(this.sessionId, items));
  }

  protected onConfirmList(): void {
    this.runEditorAction(this.api.confirmList(this.sessionId));
  }

  protected onConfirmPlan(): void {
    this.runEditorAction(this.api.confirmPlan(this.sessionId));
  }

  protected retryPreparation(): void {
    this.runAction(this.api.retryPreparation(this.sessionId));
  }

  protected retryEvaluation(): void {
    this.runAction(this.api.retryEvaluation(this.sessionId));
  }

  protected askCancel(): void {
    this.actionError.set(null);
    this.confirmingCancel.set(true);
  }

  protected keepSession(): void {
    this.confirmingCancel.set(false);
  }

  protected confirmCancel(): void {
    this.confirmingCancel.set(false);
    this.runAction(this.api.cancel(this.sessionId));
  }

  private runAction(request: Observable<SessionView>): void {
    if (this.acting()) {
      return;
    }
    this.acting.set(true);
    this.actionError.set(null);
    request.pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (view) => {
        this.acting.set(false);
        this.applyView(view);
      },
      error: (err: ApiError) => {
        this.acting.set(false);
        this.actionError.set(errorText(err));
        this.reloadIfInvalidState(err);
      },
    });
  }

  private runEditorAction(request: Observable<SessionView>): void {
    request.pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (view) => {
        this.editorError.set(null);
        this.applyView(view);
      },
      error: (err: ApiError) => {
        // New object with the catalog text, so the editor never shows a raw server message.
        this.editorError.set({ ...err, message: errorText(err) });
        this.reloadIfInvalidState(err);
      },
    });
  }

  private reloadIfInvalidState(err: ApiError): void {
    if (err.code === 'INVALID_STATE' || err.code === 'SESSION_CLOSED') {
      this.load();
    }
  }

  private load(): void {
    this.loadSub?.unsubscribe();
    this.loadSub = this.api.get(this.sessionId).subscribe({
      next: (view) => {
        this.loading.set(false);
        this.loadError.set(null);
        this.applyView(view);
      },
      error: (err: ApiError) => {
        this.loading.set(false);
        // A failed background refresh keeps the current view and tries again later.
        if (this.isPolled() && err.code !== 'RESOURCE_NOT_FOUND') {
          this.schedulePoll();
          return;
        }
        if (this.view() !== null && err.code !== 'RESOURCE_NOT_FOUND') {
          return;
        }
        this.view.set(null);
        this.loadError.set(errorText(err));
      },
    });
  }

  private isPolled(): boolean {
    const status = this.status();
    return status !== null && POLLED_STATUSES.has(status);
  }

  /** Reloads after `POLL_INTERVAL_MS` while the backend is preparing or evaluating. */
  private schedulePoll(): void {
    if ((this.pollSub && !this.pollSub.closed) || !this.isPolled()) {
      return;
    }
    this.pollSub = timer(POLL_INTERVAL_MS).subscribe(() => {
      this.pollSub = null;
      this.load();
    });
  }
}
