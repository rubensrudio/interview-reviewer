import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  inject,
  signal,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { RouterLink } from '@angular/router';

import { AuthApi } from '../../core/auth/auth-api';
import { ApiError, NETWORK_ERROR } from '../../core/http/api-error';

/** Per-field cap accepted by the auth routes (LAC-32). */
export const MAX_FIELD_LENGTH = 1024;

/** Section 9 neutral text for a password recovery request (AUTH-07, AUTH-09). */
export const FORGOT_PASSWORD_NEUTRAL =
  'If an account exists for this e-mail, we sent instructions to reset your password.';

/** Section 9 / catalog 8.3 texts for the errors this call can answer. */
const ERRORS: Readonly<Record<string, string>> = {
  VALIDATION_ERROR: 'Please check the highlighted fields.',
  CSRF_FAILED: 'Your session expired. Please reload the page.',
  TOO_MANY_ATTEMPTS: 'Too many attempts. Please try again later.',
  [NETWORK_ERROR]: 'Unable to reach the server.',
};
const GENERIC_ERROR = 'Something went wrong. Please try again.';

/**
 * Public password recovery page (AUTH-07, AUTH-09).
 *
 * Posts the typed e-mail and shows the backend's neutral answer unchanged, whether or not an
 * account exists or is Google-only: the backend alone decides what, if anything, is sent.
 */
@Component({
  selector: 'app-forgot-password-page',
  imports: [ReactiveFormsModule, RouterLink],
  templateUrl: './forgot-password-page.html',
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
      margin: 0 0 1rem;
      font-size: 1.75rem;
    }
    .intro {
      margin: 0 0 1.5rem;
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
      margin: 0;
      font-size: 0.875rem;
      color: #b91c1c;
    }
    .form-error {
      margin: 1.25rem 0 0;
      padding: 0.75rem 1rem;
      border: 1px solid #b91c1c;
      border-radius: 0.375rem;
      color: #7f1d1d;
      background: #fef2f2;
    }
    .sent {
      margin: 1.25rem 0 0;
      padding: 0.75rem 1rem;
      border: 1px solid #15803d;
      border-radius: 0.375rem;
      color: #14532d;
      background: #f0fdf4;
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
    .back {
      margin: 1.5rem 0 0;
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
export class ForgotPasswordPage {
  private readonly auth = inject(AuthApi);
  private readonly destroyRef = inject(DestroyRef);
  private readonly host: ElementRef<HTMLElement> = inject(ElementRef);

  protected readonly maxLength = MAX_FIELD_LENGTH;

  protected readonly form = new FormGroup({
    email: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required, Validators.maxLength(MAX_FIELD_LENGTH)],
    }),
  });
  protected readonly submitted = signal(false);
  protected readonly sending = signal(false);
  protected readonly sentMessage = signal<string | null>(null);
  protected readonly errorMessage = signal<string | null>(null);

  protected showEmailError(): boolean {
    const { email } = this.form.controls;
    return email.invalid && (this.submitted() || email.touched);
  }

  protected submit(): void {
    if (this.sending()) {
      return;
    }
    this.submitted.set(true);
    this.sentMessage.set(null);
    this.errorMessage.set(null);
    if (this.form.invalid) {
      this.host.nativeElement.querySelector<HTMLInputElement>('#forgot-email')?.focus();
      return;
    }

    this.sending.set(true);
    this.auth
      .forgotPassword(this.form.controls.email.value)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: ({ message }) => {
          this.sending.set(false);
          this.sentMessage.set(message || FORGOT_PASSWORD_NEUTRAL);
        },
        error: (err: ApiError) => {
          this.sending.set(false);
          this.errorMessage.set(ERRORS[err.code] ?? GENERIC_ERROR);
        },
      });
  }
}
