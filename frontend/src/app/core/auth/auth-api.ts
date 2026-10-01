import { HttpClient } from '@angular/common/http';
import { Injectable, inject, signal } from '@angular/core';
import { Observable, catchError, finalize, map, share, tap, throwError } from 'rxjs';

import { ApiError, toApiError } from '../http/api-error';

/** Authenticated account as returned by `GET /api/auth/me` (plan 8.1). */
export interface Me {
  id: string;
  email: string;
  has_password: boolean;
  google_linked: boolean;
  terms_accepted: boolean;
}

export interface MessageResponse {
  message: string;
}

export type EmailDelivery = 'sent' | 'delayed';

export interface RegisterResponse extends MessageResponse {
  email_delivery: EmailDelivery;
}

interface UserResponse {
  user: Me;
}

const AUTH = '/api/auth';
const ACCOUNT = '/api/account';

/** Rethrows any HTTP failure as a normalized `ApiError` (CT-57). */
function asApiError<T>(source: Observable<T>): Observable<T> {
  return source.pipe(catchError((err: unknown) => throwError(() => toApiError(err))));
}

/**
 * Client for the Auth and Account API (CT-58).
 *
 * The session lives in the `ir_session` HttpOnly cookie: nothing is persisted on the client.
 * `currentUser` is only a UX cache; the backend remains the authority (RNF03).
 */
@Injectable({ providedIn: 'root' })
export class AuthApi {
  private readonly http = inject(HttpClient);
  private readonly user = signal<Me | null>(null);
  private pendingMe: Observable<Me> | null = null;

  /** Last known authenticated user, or `null` for a visitor / unknown session. */
  readonly currentUser = this.user.asReadonly();

  /** Full-page navigation target that starts the Google sign-in flow. */
  readonly googleStartUrl = `${AUTH}/google/start`;

  register(email: string, password: string): Observable<RegisterResponse> {
    return this.http
      .post<RegisterResponse>(`${AUTH}/register`, { email, password, accept_terms: true })
      .pipe(asApiError);
  }

  verifyEmail(token: string): Observable<MessageResponse> {
    return this.http.post<MessageResponse>(`${AUTH}/verify-email`, { token }).pipe(asApiError);
  }

  resendVerification(email: string): Observable<MessageResponse> {
    return this.http
      .post<MessageResponse>(`${AUTH}/resend-verification`, { email })
      .pipe(asApiError);
  }

  login(email: string, password: string): Observable<Me> {
    return this.http.post<UserResponse>(`${AUTH}/login`, { email, password }).pipe(
      asApiError,
      map(({ user }) => user),
      tap((user) => this.user.set(user)),
    );
  }

  /** Revokes the session (AUTH-06). A 401 means it is already gone, so the cache is cleared too. */
  logout(): Observable<void> {
    return this.http.post<null>(`${AUTH}/logout`, null).pipe(
      asApiError,
      map(() => undefined),
      tap({
        next: () => this.user.set(null),
        error: (err: ApiError) => {
          if (err.code === 'AUTH_REQUIRED') {
            this.user.set(null);
          }
        },
      }),
    );
  }

  /**
   * Probes the session. Concurrent calls (e.g. `authGuard` and `termsGuard` on the same
   * navigation) share one request. A 401 clears `currentUser`.
   */
  me(): Observable<Me> {
    if (this.pendingMe === null) {
      this.pendingMe = this.http.get<Me>(`${AUTH}/me`).pipe(
        asApiError,
        tap({
          next: (user) => this.user.set(user),
          error: (err: ApiError) => {
            if (err.code === 'AUTH_REQUIRED') {
              this.user.set(null);
            }
          },
        }),
        finalize(() => (this.pendingMe = null)),
        share(),
      );
    }
    return this.pendingMe;
  }

  forgotPassword(email: string): Observable<MessageResponse> {
    return this.http.post<MessageResponse>(`${AUTH}/forgot-password`, { email }).pipe(asApiError);
  }

  resetPassword(token: string, newPassword: string): Observable<void> {
    return this.http
      .post<null>(`${AUTH}/reset-password`, { token, new_password: newPassword })
      .pipe(
        asApiError,
        map(() => undefined),
      );
  }

  linkGoogle(token: string, password: string): Observable<Me> {
    return this.http.post<UserResponse>(`${AUTH}/google/link`, { token, password }).pipe(
      asApiError,
      map(({ user }) => user),
      tap((user) => this.user.set(user)),
    );
  }

  acceptTerms(): Observable<void> {
    return this.http.post<null>(`${ACCOUNT}/terms`, { accept: true }).pipe(
      asApiError,
      map(() => undefined),
      tap(() => this.user.update((user) => (user ? { ...user, terms_accepted: true } : user))),
    );
  }

  deleteAccount(): Observable<MessageResponse> {
    return this.http.delete<MessageResponse>(ACCOUNT).pipe(
      asApiError,
      tap(() => this.user.set(null)),
    );
  }
}
