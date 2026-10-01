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
import { ActivatedRoute, Router, RouterLink } from '@angular/router';

import { AuthApi } from '../../core/auth/auth-api';
import { ApiError, NETWORK_ERROR } from '../../core/http/api-error';

/** Per-field cap accepted by the auth routes (LAC-32). */
export const MAX_FIELD_LENGTH = 1024;

const LINK_INVALID = 'LINK_INVALID';
const HOME_PATH = '/resumes';

/** Section 9 text shown while asking for the local password (AUTH-11). */
const INTRO_MESSAGE =
  'An account with this e-mail already exists. Enter its password to link your Google account.';
/** Section 9 / catalog 8.3 text for an expired, used or invalid link. */
const LINK_INVALID_MESSAGE = 'This link is invalid or has expired. Request a new one.';

/** Section 9 / catalog 8.3 texts for the other errors this call can answer. */
const ERRORS: Readonly<Record<string, string>> = {
  INVALID_CREDENTIALS: 'Invalid e-mail or password.',
  TOO_MANY_ATTEMPTS: 'Too many attempts. Please try again later.',
  VALIDATION_ERROR: 'Please check the highlighted fields.',
  CSRF_FAILED: 'Your session expired. Please reload the page.',
  [NETWORK_ERROR]: 'Unable to reach the server.',
};
const GENERIC_ERROR = 'Something went wrong. Please try again.';

/** Only a present token within the auth field cap is worth sending; anything else is a bad link. */
function readToken(value: string | null): string | null {
  return value && value.length <= MAX_FIELD_LENGTH ? value : null;
}

/**
 * Public Google account link page (AUTH-11, AUTH-12).
 *
 * Reached from the Google callback with a pending `token` when the e-mail already belongs to a
 * local account. Posts the token with the local password; the backend alone decides whether the
 * password is right and the link still valid. The token is never rendered nor logged.
 */
@Component({
  selector: 'app-link-google-page',
  imports: [ReactiveFormsModule, RouterLink],
  templateUrl: './link-google-page.html',
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
      font-size: 0.875rem;
      color: #b91c1c;
    }
    section .form-error {
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
export class LinkGooglePage {
  private readonly auth = inject(AuthApi);
  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);
  private readonly injector = inject(Injector);
  private readonly host: ElementRef<HTMLElement> = inject(ElementRef);
  private readonly token = readToken(inject(ActivatedRoute).snapshot.queryParamMap.get('token'));

  protected readonly maxLength = MAX_FIELD_LENGTH;
  protected readonly introMessage = INTRO_MESSAGE;
  protected readonly linkInvalidMessage = LINK_INVALID_MESSAGE;

  protected readonly linkInvalid = signal(this.token === null);
  protected readonly form = new FormGroup({
    password: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required, Validators.maxLength(MAX_FIELD_LENGTH)],
    }),
  });
  protected readonly submitted = signal(false);
  protected readonly saving = signal(false);
  protected readonly errorMessage = signal<string | null>(null);

  private readonly heading = viewChild<ElementRef<HTMLElement>>('heading');

  protected showPasswordError(): boolean {
    const { password } = this.form.controls;
    return password.invalid && (this.submitted() || password.touched);
  }

  protected submit(): void {
    if (this.saving() || this.token === null) {
      return;
    }
    this.submitted.set(true);
    this.errorMessage.set(null);
    if (this.form.invalid) {
      this.focusPassword();
      return;
    }

    this.saving.set(true);
    this.auth
      .linkGoogle(this.token, this.form.controls.password.value)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: () => {
          this.saving.set(false);
          // Replace the history entry so the link token does not stay reachable via "back".
          void this.router.navigateByUrl(HOME_PATH, { replaceUrl: true });
        },
        error: (err: ApiError) => {
          this.saving.set(false);
          if (err.code === LINK_INVALID) {
            this.linkInvalid.set(true);
            afterNextRender(() => this.heading()?.nativeElement.focus(), {
              injector: this.injector,
            });
            return;
          }
          this.errorMessage.set(ERRORS[err.code] ?? GENERIC_ERROR);
        },
      });
  }

  private focusPassword(): void {
    this.host.nativeElement.querySelector<HTMLInputElement>('#link-password')?.focus();
  }
}
