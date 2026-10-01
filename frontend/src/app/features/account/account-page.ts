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

import { AuthApi, Me } from '../../core/auth/auth-api';
import { ApiError, NETWORK_ERROR } from '../../core/http/api-error';
import { ConfirmDialog } from '../../shared/confirm-dialog';

const AUTH_REQUIRED = 'AUTH_REQUIRED';
const LOGIN_PATH = '/login';

/** Section 9 dialog text (DATA-06, LAC-12). */
const DELETE_ACCOUNT_MESSAGE =
  'This will permanently delete your account and all your data now. Backup copies expire within 30 days.';

/** Plan 8.1 success text of `DELETE /api/account`; fixed on the client, never the raw response. */
const ACCOUNT_DELETED_MESSAGE =
  'Your account and data were deleted. Backup copies expire within 30 days.';

/** Catalog 8.3 texts for the errors `GET /api/auth/me` and `DELETE /api/account` can answer. */
const ERRORS: Readonly<Record<string, string>> = {
  CSRF_FAILED: 'Your session expired. Please reload the page.',
  [NETWORK_ERROR]: 'Unable to reach the server.',
};
const GENERIC_ERROR = 'Something went wrong. Please try again.';

function signInMethods(user: Me): string {
  const methods: string[] = [];
  if (user.has_password) {
    methods.push('E-mail and password');
  }
  if (user.google_linked) {
    methods.push('Google');
  }
  return methods.join(', ');
}

/**
 * Account page (DATA-06): shows the e-mail and sign-in methods and lets the user delete the
 * account after an explicit confirmation. The backend deletes the data and ends the session;
 * this page only asks for it and maps the answer code. No temporary deactivation (LAC-12).
 * It lives outside the Shell (LAC-51), so it carries its own landmark and way back.
 */
@Component({
  selector: 'app-account-page',
  imports: [ConfirmDialog, RouterLink],
  templateUrl: './account-page.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
  styles: `
    :host {
      display: block;
      color: #1a1a1a;
      background: #ffffff;
    }
    main {
      max-width: 40rem;
      margin: 0 auto;
      padding: 2rem 1rem;
    }
    .back {
      display: inline-flex;
      align-items: center;
      min-height: 2.75rem;
      margin: 0 0 1rem;
    }
    a {
      color: #1d4ed8;
    }
    a:focus-visible {
      outline: 3px solid #1d4ed8;
      outline-offset: 2px;
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
      margin: 0 0 2rem;
    }
    p {
      margin: 0;
      line-height: 1.5;
    }
    dl {
      display: grid;
      grid-template-columns: max-content 1fr;
      gap: 0.5rem 1rem;
      margin: 0;
    }
    dt {
      font-weight: 600;
    }
    dd {
      margin: 0;
      overflow-wrap: anywhere;
    }
    .danger-zone {
      padding: 1rem;
      border: 1px solid #b91c1c;
      border-radius: 0.375rem;
    }
    .form-error {
      padding: 0.75rem 1rem;
      border: 1px solid #b91c1c;
      border-radius: 0.375rem;
      color: #7f1d1d;
      background: #fef2f2;
    }
    .success {
      padding: 0.75rem 1rem;
      border: 1px solid #15803d;
      border-radius: 0.375rem;
      color: #14532d;
      background: #f0fdf4;
    }
    button {
      align-self: flex-start;
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
      color: #ffffff;
      background: #b91c1c;
    }
    button:disabled {
      cursor: not-allowed;
      opacity: 0.6;
    }
    button[aria-busy='true'] {
      cursor: progress;
    }
    button:focus-visible {
      outline: 3px solid #1d4ed8;
      outline-offset: 2px;
    }
    @media (max-width: 40rem) {
      dl {
        grid-template-columns: 1fr;
      }
      button {
        align-self: stretch;
      }
    }
  `,
})
export class AccountPage implements OnInit {
  private readonly auth = inject(AuthApi);
  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);

  protected readonly user = this.auth.currentUser;
  protected readonly methods = computed(() => {
    const user = this.user();
    return user ? signInMethods(user) : '';
  });

  protected readonly loading = signal(false);
  protected readonly loadError = signal<string | null>(null);
  protected readonly confirming = signal(false);
  protected readonly deleting = signal(false);
  protected readonly deleted = signal(false);
  protected readonly deleteError = signal<string | null>(null);

  protected readonly deleteMessage = DELETE_ACCOUNT_MESSAGE;
  protected readonly deletedMessage = ACCOUNT_DELETED_MESSAGE;

  ngOnInit(): void {
    if (this.user() === null) {
      this.load();
    }
  }

  protected retry(): void {
    this.load();
  }

  protected askDelete(): void {
    if (this.deleting() || this.deleted()) {
      return;
    }
    this.deleteError.set(null);
    this.confirming.set(true);
  }

  protected cancelDelete(): void {
    this.confirming.set(false);
  }

  protected confirmDelete(): void {
    this.confirming.set(false);
    if (this.deleting() || this.deleted()) {
      return;
    }
    this.deleting.set(true);
    this.deleteError.set(null);
    this.auth
      .deleteAccount()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: () => {
          this.deleting.set(false);
          this.deleted.set(true);
          void this.router.navigateByUrl(LOGIN_PATH, { replaceUrl: true });
        },
        error: (err: ApiError) => {
          this.deleting.set(false);
          if (err.code === AUTH_REQUIRED) {
            void this.router.navigateByUrl(LOGIN_PATH);
            return;
          }
          this.deleteError.set(ERRORS[err.code] ?? GENERIC_ERROR);
        },
      });
  }

  private load(): void {
    this.loading.set(true);
    this.loadError.set(null);
    this.auth
      .me()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: () => this.loading.set(false),
        error: (err: ApiError) => {
          this.loading.set(false);
          if (err.code === AUTH_REQUIRED) {
            void this.router.navigateByUrl(LOGIN_PATH);
            return;
          }
          this.loadError.set(ERRORS[err.code] ?? GENERIC_ERROR);
        },
      });
  }
}
