import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  Injector,
  afterNextRender,
  computed,
  inject,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';

import { ApiError } from '../../core/http/api-error';
import { SessionApi, SessionMessage, SessionView } from './session-api';

/** Default answer limit (LAC-27); replaced by `details.limit` when the server reports another. */
const DEFAULT_ANSWER_LIMIT = 5000;
const TOAST_DURATION_MS = 8000;

const EMPTY_ANSWER_MESSAGE = 'Please type an answer before submitting.';
const ALREADY_ANSWERED_MESSAGE =
  'This question has already been answered. Showing the current question.';
const CLARIFICATION_UNAVAILABLE_MESSAGE =
  'Clarifications are temporarily unavailable. You can still submit your answer.';
const ANSWER_FAILED_MESSAGE = "We couldn't submit your answer. Please try again.";
const CLARIFICATION_FAILED_MESSAGE = "We couldn't send your question. Please try again.";

const STALE_QUESTION_CODES: ReadonlySet<string> = new Set([
  'QUESTION_ALREADY_ANSWERED',
  'NOT_CURRENT_QUESTION',
]);

/** One submission attempt; its key is reused while the same answer to the same question is retried. */
interface AnswerAttempt {
  readonly questionId: string;
  readonly content: string;
  readonly key: string;
}

function formatCount(value: number): string {
  return value.toLocaleString('en-US');
}

function limitMessage(limit: number): string {
  return `Answers are limited to ${formatCount(limit)} characters.`;
}

function isSessionView(value: unknown): value is SessionView {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as Record<string, unknown>)['id'] === 'string' &&
    typeof (value as Record<string, unknown>)['status'] === 'string'
  );
}

let nextPanelId = 0;

/**
 * Interview step of a session (CT-63): current question, counter, accepted answers (read only)
 * and the answer field with distinct "submit" and "clarification" actions.
 *
 * The counter always comes from the `SessionView`; this component never computes it.
 * Every server-side change is reported through `viewChange` for the page to apply.
 */
@Component({
  selector: 'app-interview-panel',
  templateUrl: './interview-panel.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
  styles: `
    :host {
      display: block;
      color: #1a1a1a;
    }
    .counter {
      margin: 0 0 1rem;
      font-weight: 600;
    }
    .question {
      margin: 0 0 1.5rem;
      padding: 1rem;
      border: 1px solid #c4c4c4;
      border-radius: 0.5rem;
      background: #f9fafb;
    }
    .skill {
      display: inline-block;
      margin: 0 0 0.5rem;
      padding: 0.125rem 0.5rem;
      border-radius: 999px;
      font-size: 0.875rem;
      color: #1e3a8a;
      background: #dbeafe;
    }
    h2,
    h3 {
      margin: 0;
      font-size: 1.125rem;
      line-height: 1.5;
    }
    h2:focus {
      outline: none;
    }
    h2:focus-visible {
      outline: 3px solid #1d4ed8;
      outline-offset: 2px;
    }
    label {
      display: block;
      margin-bottom: 0.5rem;
      font-weight: 600;
    }
    textarea {
      display: block;
      width: 100%;
      min-height: 10rem;
      padding: 0.75rem;
      border: 1px solid #4b5563;
      border-radius: 0.375rem;
      box-sizing: border-box;
      font: inherit;
      resize: vertical;
    }
    textarea[aria-invalid='true'] {
      border-color: #b91c1c;
    }
    .char-count {
      margin: 0.25rem 0 0;
      font-size: 0.875rem;
      text-align: right;
      color: #4b5563;
    }
    .char-count.over {
      color: #b91c1c;
      font-weight: 600;
    }
    .error {
      margin: 0.5rem 0 0;
      padding: 0.5rem 0.75rem;
      border: 1px solid #b91c1c;
      border-radius: 0.375rem;
      color: #7f1d1d;
      background: #fef2f2;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 0.75rem;
      margin-top: 1rem;
    }
    button {
      min-height: 2.75rem;
      padding: 0.5rem 1rem;
      border-radius: 0.375rem;
      font: inherit;
      cursor: pointer;
    }
    button:disabled {
      cursor: not-allowed;
      opacity: 0.7;
    }
    button:focus-visible,
    textarea:focus-visible {
      outline: 3px solid #1d4ed8;
      outline-offset: 2px;
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
    .answered {
      margin-top: 2rem;
    }
    .answered ol {
      margin: 0.75rem 0 0;
      padding: 0;
      list-style: none;
    }
    .answered li {
      margin-bottom: 0.75rem;
      padding: 0.75rem 1rem;
      border: 1px solid #e5e7eb;
      border-radius: 0.5rem;
    }
    .answered p {
      margin: 0.25rem 0 0;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    .visually-hidden {
      position: absolute;
      width: 1px;
      height: 1px;
      margin: -1px;
      padding: 0;
      overflow: hidden;
      clip: rect(0 0 0 0);
      white-space: nowrap;
      border: 0;
    }
    .toast {
      position: fixed;
      right: 1rem;
      bottom: 1rem;
      z-index: 20;
      display: flex;
      align-items: center;
      gap: 0.75rem;
      max-width: min(28rem, calc(100vw - 2rem));
      padding: 0.75rem 1rem;
      border-radius: 0.5rem;
      color: #ffffff;
      background: #1f2937;
      box-shadow: 0 4px 12px rgb(0 0 0 / 0.25);
    }
    .toast button {
      min-height: 2.75rem;
      border: 1px solid #ffffff;
      color: #ffffff;
      background: transparent;
    }
    @media (max-width: 40rem) {
      .actions {
        flex-direction: column;
      }
      .actions button {
        width: 100%;
      }
    }
  `,
})
export class InterviewPanel {
  private readonly api = inject(SessionApi);
  private readonly destroyRef = inject(DestroyRef);
  private readonly injector = inject(Injector);

  readonly view = input.required<SessionView>();
  readonly viewChange = output<SessionView>();

  private readonly uid = nextPanelId++;
  protected readonly ids = {
    question: `interview-question-${this.uid}`,
    answer: `interview-answer-${this.uid}`,
    count: `interview-answer-count-${this.uid}`,
    error: `interview-answer-error-${this.uid}`,
    answered: `interview-answered-${this.uid}`,
  };

  protected readonly draft = signal('');
  protected readonly submitting = signal(false);
  protected readonly clarifying = signal(false);
  protected readonly answerError = signal<string | null>(null);
  protected readonly clarificationError = signal<string | null>(null);
  protected readonly toast = signal<string | null>(null);
  protected readonly limit = signal(DEFAULT_ANSWER_LIMIT);

  protected readonly busy = computed(() => this.submitting() || this.clarifying());
  protected readonly length = computed(() => this.draft().length);
  protected readonly overLimit = computed(() => this.length() > this.limit());
  protected readonly charCount = computed(
    () => `${formatCount(this.length())} / ${formatCount(this.limit())}`,
  );
  protected readonly canClarify = computed(() => !this.busy() && this.draft().trim() !== '');

  private readonly questionHeading = viewChild<ElementRef<HTMLElement>>('questionHeading');
  private attempt: AnswerAttempt | null = null;
  private toastTimer: ReturnType<typeof setTimeout> | null = null;

  constructor() {
    this.destroyRef.onDestroy(() => this.clearToastTimer());
  }

  protected onInput(event: Event): void {
    this.draft.set((event.target as HTMLTextAreaElement).value);
    this.answerError.set(null);
  }

  protected submit(): void {
    const question = this.view().current_question;
    if (this.busy() || !question) {
      return;
    }
    const content = this.draft();
    if (content.trim() === '') {
      this.answerError.set(EMPTY_ANSWER_MESSAGE);
      return;
    }
    if (this.overLimit()) {
      this.answerError.set(limitMessage(this.limit()));
      return;
    }

    const attempt = this.attemptFor(question.id, content);
    this.submitting.set(true);
    this.answerError.set(null);
    this.clarificationError.set(null);
    this.api
      .answer(this.view().id, question.id, content, attempt.key)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (view) => {
          this.attempt = null;
          this.submitting.set(false);
          this.draft.set('');
          this.viewChange.emit(view);
          this.focusQuestion();
        },
        error: (err: ApiError) => {
          this.submitting.set(false);
          this.handleAnswerError(err);
        },
      });
  }

  protected askClarification(): void {
    const text = this.draft();
    if (!this.canClarify()) {
      return;
    }
    const sessionId = this.view().id;
    this.clarifying.set(true);
    this.answerError.set(null);
    this.clarificationError.set(null);
    this.api
      .clarify(sessionId, text)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (reply) => {
          this.draft.set('');
          this.reloadAfterClarification(sessionId, reply);
        },
        error: (err: ApiError) => {
          this.clarifying.set(false);
          this.clarificationError.set(
            err.code === 'CLARIFICATION_UNAVAILABLE'
              ? CLARIFICATION_UNAVAILABLE_MESSAGE
              : err.message || CLARIFICATION_FAILED_MESSAGE,
          );
        },
      });
  }

  protected dismissToast(): void {
    this.clearToastTimer();
    this.toast.set(null);
  }

  /** Reuses the pending key only when the same answer to the same question is retried. */
  private attemptFor(questionId: string, content: string): AnswerAttempt {
    const pending = this.attempt;
    if (pending && pending.questionId === questionId && pending.content === content) {
      return pending;
    }
    this.attempt = { questionId, content, key: crypto.randomUUID() };
    return this.attempt;
  }

  private handleAnswerError(err: ApiError): void {
    if (STALE_QUESTION_CODES.has(err.code)) {
      this.attempt = null;
      this.showToast(ALREADY_ANSWERED_MESSAGE);
      const session = err.details?.['session'];
      if (isSessionView(session)) {
        this.viewChange.emit(session);
        this.focusQuestion();
      }
      return;
    }
    if (err.code === 'ANSWER_TOO_LONG') {
      const limit = err.details?.['limit'];
      if (typeof limit === 'number' && limit > 0) {
        this.limit.set(limit);
      }
      this.answerError.set(limitMessage(this.limit()));
      return;
    }
    if (err.code === 'EMPTY_ANSWER') {
      this.answerError.set(EMPTY_ANSWER_MESSAGE);
      return;
    }
    // Transient failures keep `attempt`, so a retry of the same answer reuses its key.
    this.answerError.set(err.message || ANSWER_FAILED_MESSAGE);
  }

  /** The backend stores both clarification messages; reload so the page shows them in order. */
  private reloadAfterClarification(sessionId: string, reply: SessionMessage): void {
    this.api
      .get(sessionId)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (view) => {
          this.clarifying.set(false);
          this.viewChange.emit(view);
        },
        error: () => {
          this.clarifying.set(false);
          const current = this.view();
          this.viewChange.emit({ ...current, messages: [...current.messages, reply] });
        },
      });
  }

  private focusQuestion(): void {
    afterNextRender(() => this.questionHeading()?.nativeElement.focus(), {
      injector: this.injector,
    });
  }

  private showToast(message: string): void {
    this.clearToastTimer();
    this.toast.set(message);
    this.toastTimer = setTimeout(() => {
      this.toastTimer = null;
      this.toast.set(null);
    }, TOAST_DURATION_MS);
  }

  private clearToastTimer(): void {
    if (this.toastTimer !== null) {
      clearTimeout(this.toastTimer);
      this.toastTimer = null;
    }
  }
}
