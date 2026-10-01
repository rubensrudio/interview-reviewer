import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  Injector,
  afterNextRender,
  inject,
  signal,
  viewChild,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { ActivatedRoute, RouterLink } from '@angular/router';

import { AuthApi } from '../../core/auth/auth-api';
import { ApiError, NETWORK_ERROR } from '../../core/http/api-error';

/** Per-field cap accepted by the auth routes (LAC-32). */
export const MAX_FIELD_LENGTH = 1024;

const LINK_INVALID = 'LINK_INVALID';
/** Section 9 / catalog 8.3 text for an expired, used or invalid link (AUTH-94). */
const LINK_INVALID_MESSAGE = 'This link is invalid or has expired. Request a new one.';
const VERIFIED_FALLBACK = 'Your e-mail has been verified. You can now sign in.';

/** Section 9 / catalog 8.3 texts for the other errors these calls can answer. */
const ERRORS: Readonly<Record<string, string>> = {
  [LINK_INVALID]: LINK_INVALID_MESSAGE,
  VALIDATION_ERROR: 'Please check the highlighted fields.',
  [NETWORK_ERROR]: 'Unable to reach the server.',
};
const GENERIC_ERROR = 'Something went wrong. Please try again.';

function errorFor(err: ApiError): string {
  return ERRORS[err.code] ?? GENERIC_ERROR;
}

/** Only a present token within the auth field cap is worth sending; anything else is a bad link. */
function readToken(value: string | null): string | null {
  return value && value.length <= MAX_FIELD_LENGTH ? value : null;
}

type Phase = 'verifying' | 'verified' | 'invalid' | 'failed';

/**
 * Public e-mail verification page (AUTH-03, AUTH-04, AUTH-94).
 *
 * Reads `token` from the link and posts it to the backend, which alone decides whether it is
 * valid. A `LINK_INVALID` answer offers a new link through `resend-verification`, whose reply
 * is neutral and shown as-is.
 */
@Component({
  selector: 'app-verify-email-page',
  imports: [ReactiveFormsModule, RouterLink],
  templateUrl: './verify-email-page.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
  styles: `
    :host {
      display: block;
      max-width: 28rem;
      margin: 0 auto;
      padding: 2rem 1rem;
      color: #1a1a1a;
    }
    h1 {
      margin: 0 0 1.5rem;
      font-size: 1.75rem;
    }
    h1:focus {
      outline: none;
    }
    section,
    form {
      display: flex;
      flex-direction: column;
      gap: 1.25rem;
    }
    section p {
      margin: 0;
    }
    .field {
      display: flex;
      flex-direction: column;
      gap: 0.375rem;
    }
    label {
      font-weight: 600;
    }
    input[type='email'] {
      min-height: 2.75rem;
      padding: 0.5rem 0.75rem;
      border: 1px solid #4b5563;
      border-radius: 0.375rem;
      font: inherit;
      box-sizing: border-box;
      width: 100%;
    }
    input[aria-invalid='true'] {
      border-color: #b91c1c;
    }
    .field-error {
      font-size: 0.875rem;
      color: #b91c1c;
    }
    section .form-error,
    section .resend-error {
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
      border: 1px solid #1d4ed8;
      color: #ffffff;
      background: #1d4ed8;
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
    input:focus-visible {
      outline: 3px solid #1d4ed8;
      outline-offset: 2px;
    }
  `,
})
export class VerifyEmailPage {
  private readonly auth = inject(AuthApi);
  private readonly destroyRef = inject(DestroyRef);
  private readonly injector = inject(Injector);
  private readonly host: ElementRef<HTMLElement> = inject(ElementRef);
  private readonly token = readToken(inject(ActivatedRoute).snapshot.queryParamMap.get('token'));

  protected readonly maxLength = MAX_FIELD_LENGTH;
  protected readonly linkInvalidMessage = LINK_INVALID_MESSAGE;

  protected readonly phase = signal<Phase>('verifying');
  protected readonly verifiedMessage = signal('');
  protected readonly failureMessage = signal('');

  protected readonly form = new FormGroup({
    email: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required, Validators.maxLength(MAX_FIELD_LENGTH)],
    }),
  });
  protected readonly submitted = signal(false);
  protected readonly resending = signal(false);
  protected readonly resendMessage = signal<string | null>(null);
  protected readonly resendError = signal<string | null>(null);

  private readonly heading = viewChild<ElementRef<HTMLElement>>('heading');

  constructor() {
    this.verify();
  }

  protected showEmailError(): boolean {
    const { email } = this.form.controls;
    return email.invalid && (this.submitted() || email.touched);
  }

  /** Posts the link token again after a transient failure (network, server error). */
  protected verify(): void {
    if (this.token === null) {
      this.enter('invalid');
      return;
    }
    this.phase.set('verifying');
    this.auth
      .verifyEmail(this.token)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: ({ message }) => {
          this.verifiedMessage.set(message || VERIFIED_FALLBACK);
          this.enter('verified');
        },
        error: (err: ApiError) => {
          if (err.code === LINK_INVALID) {
            this.enter('invalid');
            return;
          }
          this.failureMessage.set(errorFor(err));
          this.enter('failed');
        },
      });
  }

  /** Asks the backend for a new verification e-mail (AUTH-94, AUTH-04). */
  protected resend(): void {
    if (this.resending()) {
      return;
    }
    this.submitted.set(true);
    this.resendMessage.set(null);
    this.resendError.set(null);
    if (this.form.invalid) {
      this.host.nativeElement.querySelector<HTMLInputElement>('#verify-email')?.focus();
      return;
    }

    this.resending.set(true);
    this.auth
      .resendVerification(this.form.controls.email.value)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: ({ message }) => {
          this.resending.set(false);
          this.resendMessage.set(message);
        },
        error: (err: ApiError) => {
          this.resending.set(false);
          this.resendError.set(errorFor(err));
        },
      });
  }

  private enter(phase: Phase): void {
    this.phase.set(phase);
    afterNextRender(() => this.heading()?.nativeElement.focus(), { injector: this.injector });
  }
}
