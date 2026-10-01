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
import { ActivatedRoute, Router, RouterLink } from '@angular/router';

import { AuthApi } from '../../core/auth/auth-api';
import { ApiError, NETWORK_ERROR } from '../../core/http/api-error';

/** Per-field cap accepted by the auth routes (LAC-32). */
export const MAX_FIELD_LENGTH = 1024;

const HOME_PATH = '/resumes';
const LOGIN_PATH = '/login';
const EMAIL_NOT_VERIFIED = 'EMAIL_NOT_VERIFIED';
const GOOGLE_FAILED_FLAG = 'google_failed';
const GOOGLE_FAILED_MESSAGE = 'Google sign-in failed or was cancelled. Please try again.';

/** Section 9 / catalog 8.3 texts for the errors `POST /api/auth/login` can answer. */
const FORM_ERRORS: Readonly<Record<string, string>> = {
  INVALID_CREDENTIALS: 'Invalid e-mail or password.',
  TOO_MANY_ATTEMPTS: 'Too many attempts. Please try again later.',
  [EMAIL_NOT_VERIFIED]: 'Please verify your e-mail before signing in. Resend verification e-mail?',
  VALIDATION_ERROR: 'Please check the highlighted fields.',
  [NETWORK_ERROR]: 'Unable to reach the server.',
};
const GENERIC_ERROR = 'Something went wrong. Please try again.';

function formErrorFor(err: ApiError): string {
  return FORM_ERRORS[err.code] ?? GENERIC_ERROR;
}

/**
 * Keeps `returnUrl` only when it is an internal app path (no scheme, no protocol-relative
 * `//host`, no backslash tricks, no loop back to the login page). Anything else goes home.
 */
export function safeReturnUrl(value: string | null | undefined): string {
  if (!value || !value.startsWith('/') || value.startsWith('//') || /[\\\s]/.test(value)) {
    return HOME_PATH;
  }
  const path = value.split(/[?#]/, 1)[0];
  if (path === LOGIN_PATH || path.startsWith(`${LOGIN_PATH}/`)) {
    return HOME_PATH;
  }
  return value;
}

/**
 * Public sign-in page (AUTH-04, AUTH-05, AUTH-10, AUTH-90, AUTH-93).
 *
 * The backend decides everything (credentials, verification, throttling, terms): this page
 * only maps the error code it answers to the section 9 text. After signing in, the route
 * guards take over (e.g. `termsGuard` sends a pending account to `/accept-terms`).
 */
@Component({
  selector: 'app-login-page',
  imports: [ReactiveFormsModule, RouterLink],
  templateUrl: './login-page.html',
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
    input[type='email'],
    input[type='password'] {
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
    .form-error,
    .resend-error,
    .google-error {
      margin: 0;
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
    .google {
      display: flex;
      align-items: center;
      justify-content: center;
      box-sizing: border-box;
      min-height: 2.75rem;
      padding: 0.5rem 1rem;
      border-radius: 0.375rem;
      text-decoration: none;
    }
    .divider {
      margin: 1.5rem 0;
      text-align: center;
      color: #4b5563;
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
    input:focus-visible {
      outline: 3px solid #1d4ed8;
      outline-offset: 2px;
    }
    .verify {
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
    }
    .verify p {
      margin: 0;
    }
    .alt {
      margin: 1.5rem 0 0;
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
    }
    .alt p {
      margin: 0;
    }
  `,
})
export class LoginPage {
  private readonly auth = inject(AuthApi);
  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);
  private readonly host: ElementRef<HTMLElement> = inject(ElementRef);
  private readonly query = inject(ActivatedRoute).snapshot.queryParamMap;

  protected readonly maxLength = MAX_FIELD_LENGTH;
  protected readonly googleStartUrl = this.auth.googleStartUrl;
  protected readonly googleError =
    this.query.get('error') === GOOGLE_FAILED_FLAG ? GOOGLE_FAILED_MESSAGE : null;

  protected readonly form = new FormGroup({
    email: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required, Validators.maxLength(MAX_FIELD_LENGTH)],
    }),
    password: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required, Validators.maxLength(MAX_FIELD_LENGTH)],
    }),
  });

  protected readonly submitted = signal(false);
  protected readonly submitting = signal(false);
  protected readonly formError = signal<string | null>(null);
  protected readonly needsVerification = signal(false);

  protected readonly resending = signal(false);
  protected readonly resendMessage = signal<string | null>(null);
  protected readonly resendError = signal<string | null>(null);

  private unverifiedEmail = '';

  protected showFieldError(name: 'email' | 'password'): boolean {
    const control = this.form.controls[name];
    return control.invalid && (this.submitted() || control.touched);
  }

  protected submit(): void {
    if (this.submitting()) {
      return;
    }
    this.submitted.set(true);
    this.clearFeedback();
    if (this.form.invalid) {
      this.focusFirstInvalid();
      return;
    }

    const { email, password } = this.form.getRawValue();
    this.submitting.set(true);
    this.auth
      .login(email, password)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: () => {
          this.submitting.set(false);
          void this.router.navigateByUrl(safeReturnUrl(this.query.get('returnUrl')));
        },
        error: (err: ApiError) => {
          this.submitting.set(false);
          if (err.code === EMAIL_NOT_VERIFIED) {
            this.unverifiedEmail = email;
            this.needsVerification.set(true);
          }
          this.formError.set(formErrorFor(err));
        },
      });
  }

  /** Asks the backend for a new verification e-mail for the account that tried to sign in (AUTH-04). */
  protected resend(): void {
    if (this.resending()) {
      return;
    }
    this.resending.set(true);
    this.resendMessage.set(null);
    this.resendError.set(null);
    this.auth
      .resendVerification(this.unverifiedEmail)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: ({ message }) => {
          this.resending.set(false);
          this.resendMessage.set(message);
        },
        error: (err: ApiError) => {
          this.resending.set(false);
          this.resendError.set(formErrorFor(err));
        },
      });
  }

  private clearFeedback(): void {
    this.formError.set(null);
    this.needsVerification.set(false);
    this.resendMessage.set(null);
    this.resendError.set(null);
  }

  private focusFirstInvalid(): void {
    const { email, password } = this.form.controls;
    if (email.invalid) {
      this.focusField('login-email');
    } else if (password.invalid) {
      this.focusField('login-password');
    }
  }

  private focusField(id: string): void {
    this.host.nativeElement.querySelector<HTMLInputElement>(`#${id}`)?.focus();
  }
}
