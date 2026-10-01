import { ChangeDetectionStrategy, Component, DestroyRef, inject, signal } from '@angular/core';
import { takeUntilDestroyed, toSignal } from '@angular/core/rxjs-interop';
import { FormControl, FormGroup, ReactiveFormsModule } from '@angular/forms';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';

import { AuthApi } from '../../core/auth/auth-api';
import { ApiError, NETWORK_ERROR } from '../../core/http/api-error';
import { safeReturnUrl } from './login-page';

const AUTH_REQUIRED = 'AUTH_REQUIRED';
const LOGIN_PATH = '/login';

/** Section 9 / catalog 8.3 texts for the errors `POST /api/account/terms` can answer. */
const ERRORS: Readonly<Record<string, string>> = {
  VALIDATION_ERROR: 'Please check the highlighted fields.',
  CSRF_FAILED: 'Your session expired. Please reload the page.',
  [NETWORK_ERROR]: 'Unable to reach the server.',
};
const GENERIC_ERROR = 'Something went wrong. Please try again.';
const SIGN_OUT_ERROR = 'Could not sign out. Please try again.';

/**
 * Terms acceptance page (AUTH-10, AUTH-15).
 *
 * Shown to new Google accounts and to users whose acceptance is missing or outdated; `termsGuard`
 * redirects here. The backend alone decides whether acceptance is required and records the date
 * and the document versions: this page only posts the acceptance and maps the answer code.
 */
@Component({
  selector: 'app-accept-terms-page',
  imports: [ReactiveFormsModule, RouterLink],
  templateUrl: './accept-terms-page.html',
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
    section,
    form {
      display: flex;
      flex-direction: column;
      gap: 1.25rem;
    }
    p {
      margin: 0;
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
  `,
})
export class AcceptTermsPage {
  private readonly auth = inject(AuthApi);
  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);
  private readonly query = inject(ActivatedRoute).snapshot.queryParamMap;

  protected readonly form = new FormGroup({
    accept: new FormControl(false, { nonNullable: true }),
  });
  protected readonly accepted = toSignal(this.form.controls.accept.valueChanges, {
    initialValue: this.form.controls.accept.value,
  });
  protected readonly saving = signal(false);
  protected readonly signingOut = signal(false);
  protected readonly errorMessage = signal<string | null>(null);

  protected submit(): void {
    if (this.saving() || this.signingOut() || !this.form.controls.accept.value) {
      return;
    }
    this.saving.set(true);
    this.errorMessage.set(null);
    this.auth
      .acceptTerms()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: () => {
          this.saving.set(false);
          void this.router.navigateByUrl(safeReturnUrl(this.query.get('returnUrl')), {
            replaceUrl: true,
          });
        },
        error: (err: ApiError) => {
          this.saving.set(false);
          if (err.code === AUTH_REQUIRED) {
            void this.router.navigateByUrl(LOGIN_PATH);
            return;
          }
          this.errorMessage.set(ERRORS[err.code] ?? GENERIC_ERROR);
        },
      });
  }

  /** Lets a user who does not accept leave: revokes the session and returns to sign in. */
  protected signOut(): void {
    if (this.saving() || this.signingOut()) {
      return;
    }
    this.signingOut.set(true);
    this.errorMessage.set(null);
    this.auth
      .logout()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: () => this.leave(),
        error: (err: ApiError) => {
          // 401: the session is already gone, so the user is effectively signed out.
          if (err.code === AUTH_REQUIRED) {
            this.leave();
            return;
          }
          this.signingOut.set(false);
          this.errorMessage.set(SIGN_OUT_ERROR);
        },
      });
  }

  private leave(): void {
    this.signingOut.set(false);
    void this.router.navigateByUrl(LOGIN_PATH);
  }
}
