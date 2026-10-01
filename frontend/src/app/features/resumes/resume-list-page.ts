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
import { Subscription, timer } from 'rxjs';

import { ApiError, NETWORK_ERROR } from '../../core/http/api-error';
import { ConfirmDialog } from '../../shared/confirm-dialog';
import { ResumeApi, ResumeStatus, ResumeSummary } from './resume-api';

/** Interval between list refreshes while a version is still `received` or `processing`. */
export const POLL_INTERVAL_MS = 3000;

const MEBIBYTE = 1024 * 1024;
/**
 * Plan default for `max_pdf_bytes` (LAC-17). Used only for the pre-upload UX check; it is
 * replaced by `details.limit_bytes` as soon as the backend reports its configured limit.
 */
const DEFAULT_MAX_PDF_BYTES = 5 * MEBIBYTE;

const INVALID_PDF_MESSAGE = 'Please upload a valid PDF file.';
const RESUME_LIMIT_FALLBACK =
  'You have reached the limit of 10 resumes. Delete one to upload another.';
const NETWORK_MESSAGE = 'Unable to reach the server.';
const GENERIC_ERROR = 'Something went wrong. Please try again.';

/** Section 9 / catalog 8.3 texts for request errors without parameters. */
const ERRORS: Readonly<Record<string, string>> = {
  INVALID_PDF: INVALID_PDF_MESSAGE,
  VALIDATION_ERROR: 'Please check the highlighted fields.',
  CSRF_FAILED: 'Your session expired. Please reload the page.',
  RESOURCE_NOT_FOUND: 'Not found.',
  [NETWORK_ERROR]: NETWORK_MESSAGE,
};

/** Section 9 texts for asynchronous processing failures, keyed by `failure_code`. */
const NO_TEXT_MESSAGE =
  "We couldn't read text from this PDF. Scanned documents are not supported yet — please upload a text-based PDF.";
const PROCESSING_FAILED_MESSAGE =
  "We couldn't process this resume. Please try uploading it again later.";
const FAILURES: Readonly<Record<string, string>> = {
  NO_TEXT: NO_TEXT_MESSAGE,
  OCR_NO_TEXT: NO_TEXT_MESSAGE,
  NOT_ENGLISH: 'Only resumes in English are supported at the moment.',
};

export const DELETE_RESUME_MESSAGE =
  'This will permanently delete the file, its text and extraction. Sessions that used it will keep only the skills and cited evidence until you delete them.';

const STATUS_LABELS: Readonly<Record<ResumeStatus, string>> = {
  received: 'Received',
  processing: 'Processing',
  ready: 'Ready',
  failed: 'Failed',
};

function isPending(resume: ResumeSummary): boolean {
  return resume.status === 'received' || resume.status === 'processing';
}

function positiveNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null;
}

function formatMegabytes(bytes: number): string {
  const megabytes = bytes / MEBIBYTE;
  return Number.isInteger(megabytes) ? String(megabytes) : megabytes.toFixed(1);
}

function tooLargeMessage(limitBytes: number): string {
  return `The file exceeds the ${formatMegabytes(limitBytes)} MB limit.`;
}

/**
 * Resume list page (CV-01..CV-06, CV-13, CV-14, CV-90, CV-91, CV-93, DATA-03).
 *
 * Lists the user's resume versions, uploads new PDFs and deletes versions after confirmation.
 * The backend decides validity, limits and processing; this page only pre-checks the size for
 * UX, maps error codes and `failure_code` to section 9 texts and polls while a version is
 * still being processed.
 */
@Component({
  selector: 'app-resume-list-page',
  imports: [DatePipe, ConfirmDialog],
  templateUrl: './resume-list-page.html',
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
    h2 {
      margin: 0 0 0.75rem;
      font-size: 1.25rem;
    }
    section {
      display: flex;
      flex-direction: column;
      gap: 1rem;
      margin-bottom: 2rem;
    }
    p {
      margin: 0;
    }
    .hint {
      color: #4b5563;
    }
    .upload {
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
    }
    .upload label {
      font-weight: 600;
    }
    input[type='file'] {
      min-height: 2.75rem;
      font: inherit;
    }
    input[type='file']:disabled {
      cursor: not-allowed;
      opacity: 0.6;
    }
    .form-error {
      padding: 0.75rem 1rem;
      border: 1px solid #b91c1c;
      border-radius: 0.375rem;
      color: #7f1d1d;
      background: #fef2f2;
    }
    ul {
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    li {
      display: flex;
      flex-wrap: wrap;
      align-items: flex-start;
      justify-content: space-between;
      gap: 0.75rem;
      padding: 1rem;
      border: 1px solid #c4c4c4;
      border-radius: 0.5rem;
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
    .status {
      font-weight: 600;
    }
    .status-ready {
      color: #166534;
    }
    .status-failed {
      color: #b91c1c;
    }
    .failure {
      color: #7f1d1d;
    }
    .visually-hidden {
      position: absolute;
      width: 1px;
      height: 1px;
      overflow: hidden;
      clip-path: inset(50%);
      white-space: nowrap;
    }
    button {
      min-height: 2.75rem;
      padding: 0.5rem 1rem;
      border-radius: 0.375rem;
      font: inherit;
      cursor: pointer;
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
    input:focus-visible {
      outline: 3px solid #1d4ed8;
      outline-offset: 2px;
    }
  `,
})
export class ResumeListPage implements OnInit {
  private readonly api = inject(ResumeApi);
  private readonly destroyRef = inject(DestroyRef);

  protected readonly resumes = signal<readonly ResumeSummary[]>([]);
  protected readonly loading = signal(true);
  protected readonly loadError = signal<string | null>(null);
  protected readonly uploading = signal(false);
  protected readonly uploadError = signal<string | null>(null);
  protected readonly deleteError = signal<string | null>(null);
  protected readonly pendingDelete = signal<ResumeSummary | null>(null);
  protected readonly deletingId = signal<string | null>(null);
  protected readonly isEmpty = computed(
    () => !this.loading() && this.loadError() === null && this.resumes().length === 0,
  );

  protected readonly statusLabels = STATUS_LABELS;
  protected readonly deleteMessage = DELETE_RESUME_MESSAGE;

  private maxPdfBytes = DEFAULT_MAX_PDF_BYTES;
  private listSub: Subscription | null = null;
  private pollSub: Subscription | null = null;

  ngOnInit(): void {
    this.destroyRef.onDestroy(() => {
      this.listSub?.unsubscribe();
      this.pollSub?.unsubscribe();
    });
    this.load();
  }

  protected retry(): void {
    this.loading.set(true);
    this.loadError.set(null);
    this.load();
  }

  protected failureMessage(resume: ResumeSummary): string | null {
    if (resume.status !== 'failed') {
      return null;
    }
    return (resume.failure_code && FAILURES[resume.failure_code]) || PROCESSING_FAILED_MESSAGE;
  }

  protected onFileSelected(event: Event): void {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0] ?? null;
    // Clear the selection so choosing the same file again still fires `change`.
    input.value = '';
    if (!file || this.uploading()) {
      return;
    }
    this.uploadError.set(null);

    // UX pre-checks only: the backend validates content and size again (CV-02, CV-03).
    if (file.size === 0) {
      this.uploadError.set(INVALID_PDF_MESSAGE);
      return;
    }
    if (file.size > this.maxPdfBytes) {
      this.uploadError.set(tooLargeMessage(this.maxPdfBytes));
      return;
    }

    this.uploading.set(true);
    this.api
      .upload(file)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (created) => {
          this.uploading.set(false);
          this.resumes.update((list) => [created, ...list.filter((r) => r.id !== created.id)]);
          this.schedulePoll();
        },
        error: (err: ApiError) => {
          this.uploading.set(false);
          this.uploadError.set(this.uploadErrorMessage(err));
        },
      });
  }

  protected askDelete(resume: ResumeSummary): void {
    this.deleteError.set(null);
    this.pendingDelete.set(resume);
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
          this.resumes.update((list) => list.filter((r) => r.id !== target.id));
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
        this.resumes.set(list);
        this.schedulePoll();
      },
      error: (err: ApiError) => {
        this.loading.set(false);
        // A failed background refresh keeps the current list and tries again later.
        if (this.resumes().some(isPending)) {
          this.schedulePoll();
          return;
        }
        this.loadError.set(ERRORS[err.code] ?? GENERIC_ERROR);
      },
    });
  }

  /** Refreshes the list after `POLL_INTERVAL_MS` while any version is still being processed (CV-05). */
  private schedulePoll(): void {
    if (this.pollSub && !this.pollSub.closed) {
      return;
    }
    if (!this.resumes().some(isPending)) {
      return;
    }
    this.pollSub = timer(POLL_INTERVAL_MS).subscribe(() => {
      this.pollSub = null;
      this.load();
    });
  }

  private uploadErrorMessage(err: ApiError): string {
    if (err.code === 'FILE_TOO_LARGE') {
      const limit = positiveNumber(err.details?.['limit_bytes']);
      if (limit !== null) {
        this.maxPdfBytes = limit;
      }
      return tooLargeMessage(limit ?? this.maxPdfBytes);
    }
    if (err.code === 'RESUME_LIMIT_REACHED') {
      const limit = positiveNumber(err.details?.['limit']);
      return limit !== null
        ? `You have reached the limit of ${limit} resumes. Delete one to upload another.`
        : RESUME_LIMIT_FALLBACK;
    }
    return ERRORS[err.code] ?? GENERIC_ERROR;
  }
}
