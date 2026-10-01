import { ChangeDetectionStrategy, Component, DestroyRef, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Router, RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';

import { AuthApi } from '../core/auth/auth-api';
import { ApiError } from '../core/http/api-error';

interface NavItem {
  readonly label: string;
  readonly path: string;
}

const LOGIN_PATH = '/login';

/**
 * Layout for authenticated routes: main navigation, sign out and legal footer.
 * Access control lives in the route (`authGuard`, `termsGuard`); this is UX only.
 */
@Component({
  selector: 'app-shell',
  imports: [RouterOutlet, RouterLink, RouterLinkActive],
  templateUrl: './shell.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
  styles: `
    :host {
      display: flex;
      flex-direction: column;
      min-height: 100vh;
      color: #1a1a1a;
      background: #ffffff;
    }
    .skip-link {
      position: absolute;
      left: 0.5rem;
      top: -3rem;
      padding: 0.5rem 1rem;
      border-radius: 0.375rem;
      color: #ffffff;
      background: #1d4ed8;
      z-index: 10;
    }
    .skip-link:focus {
      top: 0.5rem;
    }
    header {
      border-bottom: 1px solid #c4c4c4;
    }
    .bar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
      max-width: 72rem;
      margin: 0 auto;
      padding: 0.75rem 1rem;
    }
    nav ul {
      display: flex;
      flex-wrap: wrap;
      gap: 0.25rem;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    a {
      color: #1d4ed8;
    }
    nav a {
      display: inline-flex;
      align-items: center;
      min-height: 2.75rem;
      padding: 0 0.75rem;
      border-radius: 0.375rem;
      text-decoration: none;
    }
    nav a:hover {
      text-decoration: underline;
    }
    nav a.active {
      font-weight: 600;
      color: #1a1a1a;
      background: #e5e7eb;
    }
    .sign-out {
      min-height: 2.75rem;
      padding: 0.5rem 1rem;
      border: 1px solid #4b5563;
      border-radius: 0.375rem;
      font: inherit;
      color: #1a1a1a;
      background: #ffffff;
      cursor: pointer;
    }
    .sign-out:disabled {
      cursor: progress;
      opacity: 0.7;
    }
    a:focus-visible,
    button:focus-visible,
    main:focus-visible {
      outline: 3px solid #1d4ed8;
      outline-offset: 2px;
    }
    .error {
      max-width: 72rem;
      margin: 0.75rem auto 0;
      padding: 0.75rem 1rem;
      border: 1px solid #b91c1c;
      border-radius: 0.375rem;
      color: #7f1d1d;
      background: #fef2f2;
    }
    main {
      flex: 1;
      width: 100%;
      max-width: 72rem;
      margin: 0 auto;
      padding: 1.5rem 1rem;
      box-sizing: border-box;
    }
    footer {
      border-top: 1px solid #c4c4c4;
    }
    footer ul {
      display: flex;
      flex-wrap: wrap;
      gap: 1rem;
      max-width: 72rem;
      margin: 0 auto;
      padding: 1rem;
      list-style: none;
    }
    footer a {
      display: inline-block;
      padding: 0.5rem 0;
    }
    @media (max-width: 40rem) {
      .bar {
        flex-direction: column;
        align-items: stretch;
      }
      nav ul {
        flex-direction: column;
      }
      .sign-out {
        width: 100%;
      }
    }
  `,
})
export class Shell {
  private readonly auth = inject(AuthApi);
  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);

  protected readonly navItems: readonly NavItem[] = [
    { label: 'Resumes', path: '/resumes' },
    { label: 'New interview', path: '/sessions/new' },
    { label: 'History', path: '/history' },
    { label: 'Account', path: '/account' },
  ];

  protected readonly signingOut = signal(false);
  protected readonly signOutError = signal<string | null>(null);

  /** Revokes the session on the backend (AUTH-06) and returns to the login page. */
  protected signOut(): void {
    if (this.signingOut()) {
      return;
    }
    this.signingOut.set(true);
    this.signOutError.set(null);
    this.auth
      .logout()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: () => this.leave(),
        error: (err: ApiError) => {
          // 401: the session is already gone, so the user is effectively signed out.
          if (err.code === 'AUTH_REQUIRED') {
            this.leave();
            return;
          }
          this.signingOut.set(false);
          this.signOutError.set('Could not sign out. Please try again.');
        },
      });
  }

  private leave(): void {
    this.signingOut.set(false);
    void this.router.navigateByUrl(LOGIN_PATH);
  }
}
