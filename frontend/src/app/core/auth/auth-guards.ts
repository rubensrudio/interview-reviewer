import { inject } from '@angular/core';
import { CanActivateFn, Router, UrlTree } from '@angular/router';
import { Observable, catchError, map, of } from 'rxjs';

import { AuthApi, Me } from './auth-api';

const LOGIN_PATH = '/login';
const ACCEPT_TERMS_PATH = '/accept-terms';
const HOME_PATH = '/resumes';

/** Known user, or a session probe when the client has none cached. */
function resolveUser(api: AuthApi): Observable<Me> {
  const known = api.currentUser();
  return known ? of(known) : api.me();
}

function loginTree(router: Router, returnUrl: string): UrlTree {
  return router.createUrlTree([LOGIN_PATH], { queryParams: { returnUrl } });
}

/**
 * Private routes (AUTH-17): a visitor goes to `/login?returnUrl=...`.
 * Any failure to confirm the session counts as "not signed in". UX only: the backend decides.
 */
export const authGuard: CanActivateFn = (_route, state) => {
  const router = inject(Router);
  return resolveUser(inject(AuthApi)).pipe(
    map((): boolean | UrlTree => true),
    catchError(() => of(loginTree(router, state.url))),
  );
};

/** Routes that need the current terms accepted (AUTH-10): otherwise go to `/accept-terms`. */
export const termsGuard: CanActivateFn = (_route, state) => {
  const router = inject(Router);
  return resolveUser(inject(AuthApi)).pipe(
    map((user): boolean | UrlTree =>
      user.terms_accepted ? true : router.createUrlTree([ACCEPT_TERMS_PATH]),
    ),
    catchError(() => of(loginTree(router, state.url))),
  );
};

/** Public-only routes (login, register...): a signed-in user goes to `/resumes`. */
export const guestGuard: CanActivateFn = () => {
  const router = inject(Router);
  return inject(AuthApi)
    .me()
    .pipe(
      map((): boolean | UrlTree => router.createUrlTree([HOME_PATH])),
      catchError(() => of(true)),
    );
};
