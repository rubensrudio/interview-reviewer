import { DatePipe } from '@angular/common';
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
import { Router, RouterLink } from '@angular/router';
import { Subscription } from 'rxjs';

import { ApiError, NETWORK_ERROR } from '../../core/http/api-error';
import { ConfirmDialog } from '../../shared/confirm-dialog';
import { SessionApi, SessionStatus, SessionSummary } from '../sessions/session-api';

const GENERIC_ERROR = 'Something went wrong. Please try again.';

/** Section 9 / catalog 8.3 texts for request errors without parameters, keyed by `code`. */
const ERRORS: Readonly<Record<string, string>> = {
  CSRF_FAILED: 'Your session expired. Please reload the page.',
  RESOURCE_NOT_FOUND: 'Not found.',
  [NETWORK_ERROR]: 'Unable to reach the server.',
};

export const EMPTY_HISTORY_MESSAGE = 'No interviews yet. Start your first one.';
export const DELETED_RESUME_LABEL = 'Deleted resume';
export const DELETE_SESSION_MESSAGE =
  'This will permanently delete this interview, its messages, answers and report. This cannot be undone.';

/** Route of the comparison page (TASK-094); ids go in `a` and `b`, as in `GET /api/reports/compare`. */
export const COMPARE_PATH = '/compare';

const COMPARE_COUNT = 2;

const STATUS_LABELS: Readonly<Record<SessionStatus, string>> = {
  collecting_requirements: 'Collecting requirements',
  awaiting_confirmation: 'Awaiting confirmation',
  preparing_questions: 'Preparing questions',
  preparation_failed: 'Preparation failed',
  in_interview: 'In progress',
  evaluating: 'Evaluating',
  evaluation_failed: 'Evaluation failed',
  completed: 'Completed',
  cancelled: 'Cancelled',
  expired: 'Expired',
};

/**
 * Interview history page (DATA-01, DATA-05, DATA-92, CMP-01).
 *
 * Lists the user's sessions as returned by the backend, links completed ones to their report,
 * deletes after confirmation and lets the user pick two completed sessions to compare. The
 * comparison itself and every rule about it stay on the backend.
 */
@Component({
  selector: 'app-history-page',
  imports: [DatePipe, RouterLink, ConfirmDialog],
  templateUrl: './history-page.html',
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
      margin: 0 0 1.5rem;
      font-size: 1.75rem;
    }
    section {
      display: flex;
      flex-direction: column;
      gap: 1rem;
    }
    p {
      margin: 0;
    }
    .hint {
      color: #4b5563;
    }
    .form-error {
      padding: 0.75rem 1rem;
      border: 1px solid #b91c1c;
      border-radius: 0.375rem;
      color: #7f1d1d;
      background: #fef2f2;
    }
    .toolbar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
    }
    ul.sessions {
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    li.session {
      display: flex;
      flex-wrap: wrap;
      align-items: flex-start;
      justify-content: space-between;
      gap: 0.75rem;
      padding: 1rem;
      border: 1px solid #c4c4c4;
      border-radius: 0.5rem;
    }
    .select {
      display: flex;
      align-items: center;
      min-width: 2.75rem;
      min-height: 2.75rem;
    }
    .select input {
      width: 1.25rem;
      height: 1.25rem;
      margin: 0;
    }
    .info {
      display: flex;
      flex: 1 1 16rem;
      flex-direction: column;
      gap: 0.25rem;
      min-width: 0;
    }
    .name {
      font-weight: 600;
      overflow-wrap: anywhere;
    }
    .deleted {
      font-style: italic;
      color: #4b5563;
    }
    .skills {
      display: flex;
      flex-wrap: wrap;
      gap: 0.375rem;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .skills li {
      padding: 0.125rem 0.5rem;
      border: 1px solid #c4c4c4;
      border-radius: 999px;
      font-size: 0.875rem;
      overflow-wrap: anywhere;
    }
    .status {
      font-weight: 600;
    }
    .status-completed {
      color: #166534;
    }
    .status-cancelled,
    .status-expired,
    .status-preparation_failed,
    .status-evaluation_failed {
      color: #b91c1c;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.5rem;
    }
    .visually-hidden {
      position: absolute;
      width: 1px;
      height: 1px;
      overflow: hidden;
      clip-path: inset(50%);
      white-space: nowrap;
    }
    a {
      color: #1d4ed8;
    }
    .actions a {
      display: inline-flex;
      align-items: center;
      min-height: 2.75rem;
      padding: 0 0.5rem;
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
    a:focus-visible,
    button:focus-visible,
    input:focus-visible {
      outline: 3px solid #1d4ed8;
      outline-offset: 2px;
    }
  `,
})
export class HistoryPage implements OnInit {
  private readonly api = inject(SessionApi);
  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);

  protected readonly sessions = signal<readonly SessionSummary[]>([]);
  protected readonly loading = signal(true);
  protected readonly loadError = signal<string | null>(null);
  protected readonly deleteError = signal<string | null>(null);
  protected readonly pendingDelete = signal<SessionSummary | null>(null);
  protected readonly deletingId = signal<string | null>(null);
  /** Ids picked for comparison, in the order the user picked them. */
  protected readonly selected = signal<readonly string[]>([]);

  protected readonly isEmpty = computed(
    () => !this.loading() && this.loadError() === null && this.sessions().length === 0,
  );
  protected readonly hasCompleted = computed(() =>
    this.sessions().some((s) => s.status === 'completed'),
  );
  protected readonly canCompare = computed(() => this.selected().length === COMPARE_COUNT);

  protected readonly statusLabels = STATUS_LABELS;
  protected readonly emptyMessage = EMPTY_HISTORY_MESSAGE;
  protected readonly deletedResumeLabel = DELETED_RESUME_LABEL;
  protected readonly deleteMessage = DELETE_SESSION_MESSAGE;

  private listSub: Subscription | null = null;

  ngOnInit(): void {
    this.destroyRef.onDestroy(() => this.listSub?.unsubscribe());
    this.load();
  }

  protected retry(): void {
    this.loading.set(true);
    this.loadError.set(null);
    this.load();
  }

  protected isSelected(id: string): boolean {
    return this.selected().includes(id);
  }

  protected toggleSelection(session: SessionSummary): void {
    if (session.status !== 'completed') {
      return;
    }
    this.selected.update((ids) =>
      ids.includes(session.id) ? ids.filter((id) => id !== session.id) : [...ids, session.id],
    );
  }

  protected compare(): void {
    const ids = this.selected();
    if (ids.length !== COMPARE_COUNT) {
      return;
    }
    void this.router.navigate([COMPARE_PATH], { queryParams: { a: ids[0], b: ids[1] } });
  }

  protected resumeLabel(session: SessionSummary): string {
    return session.resume_name ?? DELETED_RESUME_LABEL;
  }

  protected askDelete(session: SessionSummary): void {
    this.deleteError.set(null);
    this.pendingDelete.set(session);
  }

  protected cancelDelete(): void {
    this.pendingDelete.set(null);
  }

  protected confirmDelete(): void {
    const target = this.pendingDelete();
    this.pendingDelete.set(null);
    if (!target) {
      return;
    }
    this.deletingId.set(target.id);
    this.api
      .delete(target.id)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: () => {
          this.deletingId.set(null);
          this.sessions.update((list) => list.filter((s) => s.id !== target.id));
          this.selected.update((ids) => ids.filter((id) => id !== target.id));
        },
        error: (err: ApiError) => {
          this.deletingId.set(null);
          this.deleteError.set(ERRORS[err.code] ?? GENERIC_ERROR);
        },
      });
  }

  private load(): void {
    this.listSub?.unsubscribe();
    this.listSub = this.api.list().subscribe({
      next: (list) => {
        this.loading.set(false);
        this.loadError.set(null);
        this.sessions.set(list);
        const completed = new Set(list.filter((s) => s.status === 'completed').map((s) => s.id));
        this.selected.update((ids) => ids.filter((id) => completed.has(id)));
      },
      error: (err: ApiError) => {
        this.loading.set(false);
        this.loadError.set(ERRORS[err.code] ?? GENERIC_ERROR);
      },
    });
  }
}
