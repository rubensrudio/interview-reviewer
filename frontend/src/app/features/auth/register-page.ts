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
import { RouterLink } from '@angular/router';

import { AuthApi, RegisterResponse } from '../../core/auth/auth-api';
import { ApiError, NETWORK_ERROR } from '../../core/http/api-error';

/** Per-field cap accepted by the auth routes (LAC-32). */
export const MAX_FIELD_LENGTH = 1024;

const NEUTRAL_MESSAGE =
  'Check your inbox to continue. If you already have an account, sign in or reset your password.';
const DELAYED_MESSAGE = 'The e-mail may take a while to arrive.';
const PASSWORD_POLICY_MESSAGE =
  'Password must have at least 8 characters and must not be a common password.';

/** Section 9 / catalog 8.3 texts for the form-level errors this page can receive. */
const FORM_ERRORS: Readonly<Record<string, string>> = {
  TERMS_NOT_ACCEPTED: 'You must accept the Terms of Use and the Privacy Policy.',
  VALIDATION_ERROR: 'Please check the highlighted fields.',
  [NETWORK_ERROR]: 'Unable to reach the server.',
};
const GENERIC_ERROR = 'Something went wrong. Please try again.';

function formErrorFor(err: ApiError): string {
  return FORM_ERRORS[err.code] ?? GENERIC_ERROR;
}

/**
 * Public registration page (AUTH-01, AUTH-02, AUTH-14, AUTH-15, AUTH-92).
 *
 * The backend owns every rule (password policy, e-mail normalization, consent recording):
 * this page only collects the input and shows what the API answers.
 */
@Component({
  selector: 'app-register-page',
  imports: [ReactiveFormsModule, RouterLink],
  templateUrl: './register-page.html',
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
    .consent {
      display: flex;
      align-items: flex-start;
      gap: 0.5rem;
    }
    .consent input {
      width: 1.25rem;
      height: 1.25rem;
      margin: 0.125rem 0 0;
      flex: none;
    }
    .consent label {
      font-weight: 400;
    }
    .field-error {
      margin: 0;
      font-size: 0.875rem;
      color: #b91c1c;
    }
    .form-error,
    .resend-error {
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
    .notice {
      display: flex;
      flex-direction: column;
      gap: 1rem;
    }
    .notice p {
      margin: 0;
    }
    .alt {
      margin: 1.5rem 0 0;
    }
  `,
})
export class RegisterPage {
  private readonly auth = inject(AuthApi);
  private readonly destroyRef = inject(DestroyRef);
  private readonly injector = inject(Injector);
  private readonly host: ElementRef<HTMLElement> = inject(ElementRef);

  protected readonly maxLength = MAX_FIELD_LENGTH;
  protected readonly neutralMessage = NEUTRAL_MESSAGE;
  protected readonly delayedMessage = DELAYED_MESSAGE;

  protected readonly form = new FormGroup({
    email: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required, Validators.maxLength(MAX_FIELD_LENGTH)],
    }),
    password: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required, Validators.maxLength(MAX_FIELD_LENGTH)],
    }),
    accept: new FormControl(false, { nonNullable: true, validators: [Validators.requiredTrue] }),
  });

  protected readonly submitted = signal(false);
  protected readonly submitting = signal(false);
  protected readonly passwordPolicyError = signal<string | null>(null);
  protected readonly formError = signal<string | null>(null);
  protected readonly result = signal<RegisterResponse | null>(null);

  protected readonly resending = signal(false);
  protected readonly resendMessage = signal<string | null>(null);
  protected readonly resendError = signal<string | null>(null);

  private readonly resultHeading = viewChild<ElementRef<HTMLElement>>('resultHeading');
  private registeredEmail = '';

  constructor() {
    this.form.controls.password.valueChanges
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe(() => this.passwordPolicyError.set(null));
  }

  protected showFieldError(name: 'email' | 'password'): boolean {
    const control = this.form.controls[name];
    return control.invalid && (this.submitted() || control.touched);
  }

  protected submit(): void {
    if (this.submitting() || !this.form.controls.accept.value) {
      return;
    }
    this.submitted.set(true);
    this.formError.set(null);
    this.passwordPolicyError.set(null);
    if (this.form.invalid) {
      this.focusFirstInvalid();
      return;
    }

    const { email, password } = this.form.getRawValue();
    this.submitting.set(true);
    this.auth
      .register(email, password)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (response) => {
          this.submitting.set(false);
          this.registeredEmail = email;
          this.form.reset();
          this.result.set(response);
          afterNextRender(() => this.resultHeading()?.nativeElement.focus(), {
            injector: this.injector,
          });
        },
        error: (err: ApiError) => {
          this.submitting.set(false);
          if (err.code === 'PASSWORD_POLICY') {
            this.passwordPolicyError.set(PASSWORD_POLICY_MESSAGE);
            this.focusField('register-password');
            return;
          }
          this.formError.set(formErrorFor(err));
        },
      });
  }

  /** Asks the backend for a new verification e-mail when delivery was delayed (AUTH-92). */
  protected resend(): void {
    if (this.resending()) {
      return;
    }
    this.resending.set(true);
    this.resendMessage.set(null);
    this.resendError.set(null);
    this.auth
      .resendVerification(this.registeredEmail)
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

  private focusFirstInvalid(): void {
    const { email, password } = this.form.controls;
    if (email.invalid) {
      this.focusField('register-email');
    } else if (password.invalid) {
      this.focusField('register-password');
    }
  }

  private focusField(id: string): void {
    this.host.nativeElement.querySelector<HTMLInputElement>(`#${id}`)?.focus();
  }
}
