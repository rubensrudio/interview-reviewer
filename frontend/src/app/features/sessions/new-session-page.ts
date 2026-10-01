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
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import { Observable, Subscription, forkJoin, switchMap } from 'rxjs';

import { ApiError, NETWORK_ERROR } from '../../core/http/api-error';
import { ConfirmDialog } from '../../shared/confirm-dialog';
import { ResumeApi, ResumeSummary } from '../resumes/resume-api';
import { ExpectedLevel, LanguageOption, SessionApi, SessionView } from './session-api';

const SESSION_IN_PROGRESS = 'SESSION_IN_PROGRESS';
export const SESSION_IN_PROGRESS_MESSAGE =
  'You already have an interview in progress. Resume or cancel it to start a new one.';
const GENERIC_ERROR = 'Something went wrong. Please try again.';

/** Section 9 / catalog 8.3 texts for the errors the options, list and start routes can answer. */
const ERRORS: Readonly<Record<string, string>> = {
  RESUME_NOT_READY: 'This resume is not ready yet.',
  LANGUAGE_NOT_SUPPORTED: 'This language is not supported yet.',
  RESOURCE_NOT_FOUND: 'Not found.',
  SESSION_CLOSED: 'This interview is closed.',
  INVALID_STATE: 'This action is not available at this step.',
  VALIDATION_ERROR: 'Please check the highlighted fields.',
  CSRF_FAILED: 'Your session expired. Please reload the page.',
  [SESSION_IN_PROGRESS]: SESSION_IN_PROGRESS_MESSAGE,
  [NETWORK_ERROR]: 'Unable to reach the server.',
};

const LEVEL_LABELS: Readonly<Record<ExpectedLevel, string>> = {
  junior: 'Junior',
  'mid-level': 'Mid-level',
  senior: 'Senior',
  expert: 'Expert',
};

function errorMessage(err: ApiError): string {
  return ERRORS[err.code] ?? GENERIC_ERROR;
}

function inProgressSessionId(err: ApiError): string | null {
  if (err.code !== SESSION_IN_PROGRESS) {
    return null;
  }
  const id = err.details?.['session_id'];
  return typeof id === 'string' && id !== '' ? id : null;
}

/**
 * New interview page (CV-11, PLAN-01, PLAN-02, LANG-01, LANG-02).
 *
 * Offers the resumes the backend returns for `list("ready")`, the languages and levels of
 * `GET /api/interview-options` and starts the session. When another session is still open
 * (409 `SESSION_IN_PROGRESS`) a dialog lets the user resume it or cancel it and start again.
 */
@Component({
  selector: 'app-new-session-page',
  imports: [ReactiveFormsModule, RouterLink, ConfirmDialog],
  templateUrl: './new-session-page.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
  styles: `
    :host {
      display: block;
      max-width: 36rem;
      margin: 0 auto;
      padding: 2rem 1rem;
      color: #1a1a1a;
    }
    h1 {
      margin: 0 0 1.5rem;
      font-size: 1.75rem;
    }
    p {
      margin: 0;
    }
    .state {
      display: flex;
      flex-direction: column;
      align-items: flex-start;
      gap: 1rem;
    }
    form {
      display: flex;
      flex-direction: column;
      gap: 1.25rem;
    }
    .field {
      display: flex;
      flex-direction: column;
      gap: 0.375rem;
    }
    label {
      font-weight: 600;
    }
    .hint {
      font-size: 0.875rem;
      color: #4b5563;
    }
    select {
      min-height: 2.75rem;
      padding: 0.5rem 0.75rem;
      border: 1px solid #4b5563;
      border-radius: 0.375rem;
      font: inherit;
      color: #1a1a1a;
      background: #ffffff;
      box-sizing: border-box;
      width: 100%;
    }
    select[aria-invalid='true'] {
      border-color: #b91c1c;
    }
    select:disabled {
      cursor: not-allowed;
      opacity: 0.6;
    }
    .field-error {
      font-size: 0.875rem;
      color: #b91c1c;
    }
    .form-error {
      padding: 0.75rem 1rem;
      border: 1px solid #b91c1c;
      border-radius: 0.375rem;
      color: #7f1d1d;
      background: #fef2f2;
    }
    button {
      min-height: 2.75rem;
      padding: 0.5rem 1rem;
      border-radius: 0.375rem;
      font: inherit;
      cursor: pointer;
    }
    .primary {
      align-self: flex-start;
      border: 1px solid #1d4ed8;
      color: #ffffff;
      background: #1d4ed8;
    }
    .secondary {
      border: 1px solid #4b5563;
      color: #1a1a1a;
      background: #ffffff;
    }
    button:disabled {
      cursor: not-allowed;
      opacity: 0.6;
    }
    button[aria-busy='true'] {
      cursor: progress;
    }
    a {
      color: #1d4ed8;
    }
    a:focus-visible,
    button:focus-visible,
    select:focus-visible {
      outline: 3px solid #1d4ed8;
      outline-offset: 2px;
    }
  `,
})
export class NewSessionPage implements OnInit {
  private readonly resumeApi = inject(ResumeApi);
  private readonly sessionApi = inject(SessionApi);
  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);

  protected readonly form = new FormGroup({
    resumeId: new FormControl('', { nonNullable: true, validators: [Validators.required] }),
    language: new FormControl('', { nonNullable: true, validators: [Validators.required] }),
    level: new FormControl<ExpectedLevel | ''>('', { nonNullable: true }),
  });

  protected readonly resumes = signal<readonly ResumeSummary[]>([]);
  protected readonly languages = signal<readonly LanguageOption[]>([]);
  protected readonly levels = signal<readonly ExpectedLevel[]>([]);
  protected readonly loading = signal(true);
  protected readonly loadError = signal<string | null>(null);
  protected readonly submitted = signal(false);
  protected readonly starting = signal(false);
  protected readonly formError = signal<string | null>(null);
  /** Id of the unfinished session reported by 409 `SESSION_IN_PROGRESS`; opens the dialog. */
  protected readonly inProgressId = signal<string | null>(null);
  protected readonly isEmpty = computed(
    () => !this.loading() && this.loadError() === null && this.resumes().length === 0,
  );

  protected readonly levelLabels = LEVEL_LABELS;
  protected readonly inProgressMessage = SESSION_IN_PROGRESS_MESSAGE;

  private loadSub: Subscription | null = null;

  ngOnInit(): void {
    this.destroyRef.onDestroy(() => this.loadSub?.unsubscribe());
    this.load();
  }

  protected retryLoad(): void {
    this.loading.set(true);
    this.loadError.set(null);
    this.load();
  }

  protected showResumeError(): boolean {
    return this.submitted() && this.form.controls.resumeId.invalid;
  }

  protected submit(): void {
    this.submitted.set(true);
    if (this.form.invalid || this.starting()) {
      return;
    }
    this.formError.set(null);
    this.run(this.startRequest());
  }

  /** Dialog "Resume": open the unfinished session instead of creating a new one. */
  protected resumeInProgress(): void {
    const id = this.inProgressId();
    this.inProgressId.set(null);
    if (id) {
      void this.router.navigate(['/sessions', id]);
    }
  }

  /** Dialog "Cancel": cancel the unfinished session, then try to start the new one again. */
  protected cancelInProgress(): void {
    const id = this.inProgressId();
    this.inProgressId.set(null);
    if (!id || this.starting()) {
      return;
    }
    this.formError.set(null);
    this.run(this.sessionApi.cancel(id).pipe(switchMap(() => this.startRequest())));
  }

  private startRequest(): Observable<SessionView> {
    const { resumeId, language, level } = this.form.getRawValue();
    return this.sessionApi.start(resumeId, language, level === '' ? null : level);
  }

  private run(request: Observable<SessionView>): void {
    this.starting.set(true);
    request.pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (session) => {
        this.starting.set(false);
        void this.router.navigate(['/sessions', session.id]);
      },
      error: (err: ApiError) => {
        this.starting.set(false);
        const inProgress = inProgressSessionId(err);
        if (inProgress !== null) {
          this.inProgressId.set(inProgress);
          return;
        }
        this.formError.set(errorMessage(err));
      },
    });
  }

  private load(): void {
    this.loadSub?.unsubscribe();
    // The backend filters by status (CV-11); the page shows exactly what it returns.
    this.loadSub = forkJoin({
      resumes: this.resumeApi.list('ready'),
      options: this.sessionApi.options(),
    }).subscribe({
      next: ({ resumes, options }) => {
        this.resumes.set(resumes);
        this.languages.set(options.languages);
        this.levels.set(options.levels);
        const current = this.form.controls.language.value;
        if (!options.languages.some((l) => l.code === current)) {
          this.form.controls.language.setValue(options.languages[0]?.code ?? '');
        }
        this.loading.set(false);
      },
      error: (err: ApiError) => {
        this.loading.set(false);
        this.loadError.set(errorMessage(err));
      },
    });
  }
}
